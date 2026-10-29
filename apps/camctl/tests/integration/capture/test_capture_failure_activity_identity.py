"""实际失败的错误目标是设备活动，动作与原调用历史各自保持。"""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import result_driver as script_result_driver
from ..support.result_cases import result_case, result_case_for

pytestmark = pytest.mark.asyncio


def _start_outcome(*, failed):
    return CallOutcome(
        status=AttemptStatus.FAILED if failed else AttemptStatus.SUCCEEDED,
        error=ErrorValue("transport_timeout", "transport", {"received_bytes": 23})
        if failed else None,
        effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED,
                              EvidenceValue("operation_returned", 1, {})),
        observations=(), call_info=CallInfo(local_exit_code=7 if failed else 0),
    )


def _file_outcome(kind):
    return CallOutcome(
        status=AttemptStatus.SUCCEEDED, error=None, effect=EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED,
                              EvidenceValue("results_returned", 1, {})),
        observations=(DeviceObservation("result_files_listed", 1, {
            "activity_id": "1", "entries": [{
                "identity": "original", "locator": {"path": "/DCIM/original"},
                "complete": True, "size_bytes": 41, "kind": kind,
                "original_name": "original", "media_type": "image/jpeg"
                if kind == "photo" else "video/mp4", "paired_identity": None,
            }],
        }),), call_info=CallInfo(local_exit_code=0),
    )


async def _failed_start_world(tmp_path, consumer, actual, monkeypatch):
    """正式 C1 保存原 START 返回，在业务收尾前交还可靠回执。"""
    support = result_case_for()
    owned, runtime, action_id, handler = await support.world(
        tmp_path, consumer, independent_activity=True, start=False)
    support.driver.control.return_value = DeviceCallResult.from_outcome(actual)
    class StartSaved(Exception):
        pass
    save = CaptureRepository.finish_start_result
    def after_save(repository, *args, **kwargs):
        receipt = save(repository, *args, **kwargs)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=StartSaved())
    try:
        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, "finish_start_result", after_save)
            with pytest.raises(StartSaved):
                await capture_handler(handler)(action_id, runtime)
        assert (await runtime.action(action_id))["status"] == 2
        assert owned.connection.execute("SELECT sent_at FROM device_activities WHERE id=1").fetchone() == (None,)
        return owned, runtime, handler
    except BaseException:
        owned.connection.close()
        raise


def _attempts(owned):
    return owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()


def _history(owned):
    return owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()


async def _reopen(owned, previous):
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    metadata = owned.metadata
    owned.connection.close()
    fresh = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    assert fresh.metadata == metadata
    return fresh, await result_case_for().reopen(fresh, cold=True)


def _assert_start_input(owned, *, failed):
    row = owned.connection.execute(
        "SELECT t.status,t.effect_state,t.result_json,t.error_json FROM operation_attempts t"
        " JOIN operation_runs r ON r.id=t.run_id WHERE r.responsibility_key='start/2'"
    ).fetchone()
    assert row[:2] == (3 if failed else 2, 1)
    assert json.loads(row[2]) == {
        "format_version": 1,
        "settlement": {"basis": "observed", "evidence": {
            "type": "operation_returned", "version": 1, "data": {}}},
        "observations": [], "call_info": {"local_exit": {"exit_code": 7 if failed else 0}},
    }
    assert (json.loads(row[3]) if row[3] else None) == (
        {"code": "transport_timeout", "stage": "transport", "details": {"received_bytes": 23}}
        if failed else None)


def _assert_failure(owned, *, code, reason):
    status, error_code, details = owned.connection.execute(
        "SELECT status,error_code,error_details_json FROM actions WHERE id=2").fetchone()
    assert (status, error_code) == (4, code)
    assert json.loads(details) == {"activity_id": "1", "reason": reason}


@pytest.mark.parametrize("failed,code,reason,start_status", [
    (True, 13, "device_failed", 4),
    (False, 12, "start_unknown", 6),
], ids=["failed-start-without-sent-at", "unknown-effect-without-sent-at"])
async def test_start_without_sent_at_reports_actual_activity(
    tmp_path, monkeypatch, failed, code, reason, start_status,
):
    actual = _start_outcome(failed=failed)
    owned, runtime, handler = await _failed_start_world(tmp_path, "timelapse", actual, monkeypatch)
    try:
        _assert_start_input(owned, failed=failed)
        original_attempts, original_history = _attempts(owned), _history(owned)
        result_driver = script_result_driver(result_case_for(), _file_outcome("video"))
        result_driver.list_results.side_effect = AssertionError("无发送时刻收场不能查询设备")
        owned, resumed = (await _reopen(owned, runtime))
        await capture_handler(handler)(2, resumed)

        _assert_failure(owned, code=code, reason=reason)
        assert _attempts(owned) == original_attempts
        assert _history(owned)[:len(original_history)] == original_history
        assert owned.connection.execute(
            "SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='start/2'"
        ).fetchone() == (start_status, 1)
        assert owned.connection.execute(
            "SELECT sent_at,occupancy_state FROM device_activities WHERE id=1"
        ).fetchone() == (None, 1)
        runtime.driver.control.assert_awaited_once()
        result_driver.list_results.assert_not_awaited()
        terminal_history = _history(owned)
        await capture_handler(handler)(2, resumed)
        assert _history(owned) == terminal_history
        _assert_start_input(owned, failed=failed)
        runtime.driver.control.assert_awaited_once()
        result_driver.list_results.assert_not_awaited()
    finally:
        owned.connection.close()


async def test_failed_photo_response_keeps_file_with_actual_activity_error(tmp_path, monkeypatch):
    owned, runtime, handler = await _failed_start_world(tmp_path, "photo", _start_outcome(failed=True), monkeypatch)
    actual = _file_outcome("photo")
    driver = script_result_driver(result_case_for(), actual)
    try:
        start_attempts, original_history = _attempts(owned), _history(owned)
        await capture_handler(handler)(2, runtime)

        _assert_failure(owned, code=13, reason="device_failed")
        assert _attempts(owned)[:1] == start_attempts
        assert _history(owned)[:len(original_history)] == original_history
        _assert_start_input(owned, failed=True)
        assert owned.connection.execute(
            "SELECT o.source_action_id,f.source_action_id,f.completion_state,f.size_bytes"
            " FROM outputs o JOIN device_files f ON f.id=o.device_file_id"
        ).fetchall() == [(2, 2, 3, 41)]
        saved = owned.connection.execute(
            "SELECT t.result_json FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.responsibility_key='results/1'"
        ).fetchone()
        assert json.loads(saved[0])["observations"] == [{
            "type": "result_files_listed", "version": 1, "data": dict(actual.observations[0].data),
        }]
        driver.list_results.assert_awaited_once()
        assert driver.list_results.call_args.args[0].ticket.target_id == "1"
        runtime.driver.control.assert_awaited_once()
        terminal_attempts, terminal_history = _attempts(owned), _history(owned)
        owned, resumed = (await _reopen(owned, runtime))
        await capture_handler(handler)(2, resumed)
        assert _attempts(owned) == terminal_attempts and _history(owned) == terminal_history
        driver.list_results.assert_awaited_once()
        runtime.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_closed_unsatisfied_timelapse_reports_actual_activity_without_query(tmp_path, monkeypatch):
    owned, runtime, action_id, handler = await result_case_for().world(
        tmp_path, "timelapse", independent_activity=True)
    assert action_id == 2
    actual = _file_outcome("photo")
    driver = script_result_driver(result_case_for(), actual)
    try:
        observation, = actual.observations
        actual = replace(actual, observations=(DeviceObservation(observation.type, 2, {
            **observation.data, "cursor": None, "next_cursor": None,
            "set_finalized": True, "completion_evidence": None}),))
        driver.list_results.return_value = DeviceCallResult.from_outcome(actual)
        class ResultClosed(Exception):
            pass
        original_finish = CaptureRepository.finish_result_check
        def finish_after_saved(repository, *args, **kwargs):
            receipt = original_finish(repository, *args, **kwargs)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=ResultClosed())
        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, "finish_result_check", finish_after_saved)
            with pytest.raises(ResultClosed):
                await capture_handler(handler)(action_id, runtime)
        assert (await runtime.action(2))["status"] == 2
        assert owned.connection.execute(
            "SELECT id,action_id,result_set_state,completion_basis FROM device_activities"
        ).fetchone() == (1, 2, 3, 4)
        original_attempts, original_history = _attempts(owned), _history(owned)
        driver.list_results.side_effect = AssertionError("正式结论恢复不能再次查询设备")
        owned, resumed = (await _reopen(owned, runtime))
        await capture_handler(handler)(2, resumed)

        _assert_failure(owned, code=13, reason="invalid_outputs")
        assert _attempts(owned) == original_attempts
        assert _history(owned)[:len(original_history)] == original_history
        assert owned.connection.execute(
            "SELECT source_action_id FROM outputs").fetchall() == [(2,)]
        assert owned.connection.execute(
            "SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='results/1'"
        ).fetchone() == (3, 1)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities WHERE id=1").fetchone() == (1,)
        terminal_history = _history(owned)
        await capture_handler(handler)(2, resumed)
        assert _history(owned) == terminal_history
        driver.list_results.assert_awaited_once()
        runtime.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()
