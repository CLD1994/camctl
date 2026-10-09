"""普通延时摄影首次耗尽核实时，原完整文件与失败终态共同登记。"""

from decimal import Decimal
import json
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.models import ResultSetPhase
from camctl.contracts.workflow_errors import action_error_id
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .result_consumer_fixtures import RESULT_EVIDENCE, consumer_world
from .test_capture_contract import _runtime
from .test_result_consumer_saves import _actual, _result_port


pytestmark = pytest.mark.asyncio


def _rows(owned, table):
    return owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


async def _exercise_first_exhaustion(tmp_path, monkeypatch, *, kind="video",
                                     later_failed=False, empty=False, complete=True):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", independent_activity=True)
    reopened = None
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert activity_id != action_id
        name, media_type = {
            "video": ("retained-video.mp4", "video/mp4"),
            "photo": ("retained-photo.jpg", "image/jpeg"),
        }[kind]
        identity = f"retained-{kind}"
        entries = [] if empty else [{"identity": identity, "kind": kind,
            "complete": complete, "size_bytes": 41 if complete else None,
            "locator": {"path": f"/DCIM/{name}"},
            "original_name": name, "media_type": media_type}]
        actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": str(activity_id), "entries": entries}),))
        driver = _result_port(runtime, actual)
        maximum = 2 if later_failed else 1
        runtime.check_config = AttemptConfig(maximum, Decimal("1.25"), Decimal(0))
        advance = capture_handler(handler)
        await advance(action_id, runtime)
        responsibility = f"results/{activity_id}"
        run_id, status, used, retry = owned.connection.execute(
            "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key=?", (responsibility,)).fetchone()
        assert (status, used, retry) == (2, 1, 1)
        assert runtime.action(action_id)["status"] == 2
        wait_id, expected_check_at = owned.connection.execute(
            "SELECT wait_completed_event_id,expected_check_at FROM device_activities WHERE id=?",
            (activity_id,)).fetchone()
        assert wait_id is not None and expected_check_at <= runtime.wall_us()
        assert owned.connection.execute(
            "SELECT occurred_at FROM history_events WHERE id=?", (wait_id,)).fetchone() == (runtime.wall_us(),)
        first_result, = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id=?", (run_id,)).fetchone()
        assert json.loads(first_result) == {"format_version": 1,
            "settlement": {"basis": "observed", "evidence": {
                "type": "results_returned", "version": 1, "data": {}}},
            "observations": [{"type": "result_files_listed", "version": 1,
                "data": {"activity_id": str(activity_id), "entries": entries}}]}
        file_facts = owned.connection.execute(
            "SELECT id,source_action_id,presence_state,completion_state,size_bytes FROM device_files").fetchall()
        if empty:
            assert file_facts == []
            file_id = None
        else:
            (file_id, source_action, presence, completion, size), = file_facts
            assert (source_action, presence) == (action_id, 2)
            if complete:
                assert (completion, size) == (3, 41)
            else:
                assert completion != 3 and size is None
        if later_failed:
            driver.list_results.return_value = DeviceCallResult.from_outcome(
                _actual(activity_id, with_files=False))
            await advance(action_id, runtime)
            latest_status, latest_result, latest_error = owned.connection.execute(
                "SELECT status,result_json,error_json FROM operation_attempts WHERE run_id=?"
                " ORDER BY attempt_no DESC LIMIT 1", (run_id,)).fetchone()
            assert latest_status == 3 and json.loads(latest_result)["observations"] == []
            assert json.loads(latest_error) == {"code": "transport_timeout", "stage": "transport",
                "details": {"received_bytes": 23}}
        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == (2, maximum, 1)
        assert runtime.action(action_id)["status"] == 2
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        attempts, files = _rows(owned, "operation_attempts"), _rows(owned, "device_files")
        prefix = _rows(owned, "history_events")
        original_config = owned.connection.execute(
            "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_runs WHERE id=?",
            (run_id,)).fetchone()
        original_activity = owned.connection.execute(
            "SELECT activity_state,occupancy_state,wait_completed_event_id FROM device_activities WHERE id=?",
            (activity_id,)).fetchone()
        original_basis, original_evidence = owned.connection.execute(
            "SELECT completion_basis,completion_evidence_json FROM device_activities WHERE id=?",
            (activity_id,)).fetchone()
        assert (original_basis, original_evidence) == (1, None)
        original_close = CaptureRepository.close_result_check_unconfirmed
        closes, receipts = [], []

        def close(repository, request, key, current):
            closes.append((request, key))
            receipt = original_close(repository, request, key, current)
            receipts.append(receipt)
            return receipt

        monkeypatch.setattr(CaptureRepository, "close_result_check_unconfirmed", close)
        formed_at = runtime.wall_us() + 10
        runtime.wall_us = lambda: formed_at
        # G 正常返回，不截停；本次 EXHAUSTED 必须把文件与失败终态一并保存。
        await advance(action_id, runtime)
        assert len(closes) == len(receipts) == 1
        assert receipts[0].kind is DbOutcomeKind.COMPLETED and receipts[0].value is not None
        request, key = closes[0]
        expected_error = {"code": "capture_result_unconfirmed", "stage": "execution",
            "details": {"activity_id": str(activity_id), "reason": "outputs_unknown"}}
        assert request.phase is ResultSetPhase.UNCONFIRMED
        assert request.action_id == action_id and request.occurred_at == formed_at
        assert request.capture == {"status": "unconfirmed", "error": expected_error}
        assert request.error == expected_error
        group = saved_transaction_events(owned.connection, key)
        assert group is not None and len(group) == 2
        assert all(event["occurred_at"] == formed_at for event in group)
        assert not runtime.pending_result_closes
        action_status, error_code, error_details = owned.connection.execute(
            "SELECT status,error_code,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert (action_status, error_code) == (4, action_error_id("capture_result_unconfirmed"))
        assert json.loads(error_details) == expected_error["details"]
        status, used, retry, run_error = owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required,error_json FROM operation_runs WHERE id=?",
            (run_id,)).fetchone()
        assert (status, used, retry) == (6, maximum, 0)
        assert json.loads(run_error) == expected_error
        set_state, capture_json, last_error, completion_basis, completion_evidence = owned.connection.execute(
            "SELECT result_set_state,capture_json,last_error_json,completion_basis,completion_evidence_json"
            " FROM device_activities WHERE id=?", (activity_id,)).fetchone()
        assert (set_state, completion_basis, completion_evidence) == (4, original_basis, original_evidence)
        assert json.loads(capture_json) == {"status": "unconfirmed", "error": expected_error}
        assert json.loads(last_error) == expected_error
        assert _rows(owned, "operation_attempts") == attempts
        assert _rows(owned, "device_files") == files
        assert _rows(owned, "history_events")[:len(prefix)] == prefix
        assert owned.connection.execute(
            "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == original_config
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state,wait_completed_event_id FROM device_activities WHERE id=?",
            (activity_id,)).fetchone() == original_activity
        driver.list_results.assert_awaited()
        assert driver.list_results.await_count == maximum
        runtime.driver.control.assert_awaited_once()
        final_dump = tuple(owned.connection.iterdump())
        await advance(action_id, runtime)
        assert tuple(owned.connection.iterdump()) == final_dump
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        metadata = owned.metadata
        owned.connection.close()
        reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == metadata
        fresh_runtime = _runtime(reopened, driver=runtime.driver, results=runtime.results,
            wall=formed_at + 5_000_000)
        fresh_runtime.evidence = RESULT_EVIDENCE
        assert not fresh_runtime.pending_result_closes and not fresh_runtime.pending_start_results
        assert fresh_runtime.listing_cache is None and not fresh_runtime.timelapse_deadlines
        await advance(action_id, fresh_runtime)
        assert tuple(reopened.connection.iterdump()) == final_dump
        assert saved_transaction_events(reopened.connection, key) == group
        assert driver.list_results.await_count == maximum
        runtime.driver.control.assert_awaited_once()
        # 预期来自本测试的原文件事实，不调用生产草稿生成器。
        expected_outputs = [] if empty or not complete else [(1, file_id, name, media_type)]
        outputs = reopened.connection.execute(
            "SELECT kind,device_file_id,original_name,media_type FROM outputs WHERE source_action_id=?",
            (action_id,)).fetchall()
        assert outputs == expected_outputs, "首次耗尽须保留已写完且可靠归属的全部原文件"
        transactions = reopened.connection.execute(
            "SELECT oe.transaction_id,ae.transaction_id FROM outputs o"
            " JOIN history_events oe ON oe.id=o.created_event_id"
            " JOIN actions a ON a.id=o.source_action_id"
            " JOIN history_events ae ON ae.id=a.last_event_id WHERE o.source_action_id=?",
            (action_id,)).fetchall()
        assert len(transactions) == len(expected_outputs)
        assert all(output_txn == action_txn for output_txn, action_txn in transactions)
    finally:
        owned.connection.close()
        if reopened is not None:
            reopened.connection.close()


@pytest.mark.parametrize("kind", ["video", "photo"])
@pytest.mark.parametrize("later_failed", [False, True], ids=["single-file-round", "prior-file-latest-failed"])
async def test_first_timelapse_exhaustion_keeps_saved_complete_files(
        tmp_path, monkeypatch, kind, later_failed):
    await _exercise_first_exhaustion(tmp_path, monkeypatch, kind=kind, later_failed=later_failed)


@pytest.mark.parametrize("file_case", ["empty", "incomplete"])
@pytest.mark.parametrize("later_failed", [False, True], ids=["single-round", "latest-failed"])
async def test_first_timelapse_exhaustion_does_not_register_unqualified_files(
        tmp_path, monkeypatch, file_case, later_failed):
    await _exercise_first_exhaustion(tmp_path, monkeypatch, later_failed=later_failed,
        empty=file_case == "empty", complete=file_case != "incomplete")
