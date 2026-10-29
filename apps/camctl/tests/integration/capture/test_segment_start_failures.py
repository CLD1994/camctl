"""结果策略任务在 START 失败及取消时仍发布同一来源边界。"""

from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest

from camctl.capture.handlers import capture_handler
from camctl.operations.models import ErrorValue
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.devices.ports import DeviceCallResult
from camctl.contracts.values import ConsistencyError

from ..support.result_cases import result_case, result_case_for
from .test_natural_wait_time_eligibility import _request_cancel
from .test_native_segment_completion import NativeSegmentCatalog
from .test_record_segment_scope_recovery import SegmentWaitCatalog
from .test_recording_start_runtime import _result
from .test_action6_segmented_normal import Action6NativeCatalog, Action6RecordCatalog

pytestmark = pytest.mark.asyncio


def _reject_segment_projection(monkeypatch):
    enabled = [True]
    original = CaptureRepository.finish_segment_recording
    def save(repository, command, key, owned):
        if not enabled[0]:
            return original(repository, command, key, owned)
        denied = []
        def authorize(operation, table, _column, _database, _source):
            if operation in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE) and table == "segment_sets":
                denied.append(table)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        owned.connection.set_authorizer(authorize)
        try:
            receipt = original(repository, command, key, owned)
        finally:
            owned.connection.set_authorizer(None)
        assert denied and receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        assert not owned.connection.in_transaction
        return receipt
    monkeypatch.setattr(CaptureRepository, "finish_segment_recording", save)
    return enabled


@pytest.mark.parametrize("kind", ["record", "timelapse"])
@pytest.mark.parametrize("lost", [False, True])
@pytest.mark.parametrize("driver_error", [False, True])
@pytest.mark.parametrize("action6", [False, True])
async def test_start_failure_publishes_empty_unknown_source_and_resumes_without_start(
    tmp_path, monkeypatch, kind, lost, driver_error, action6
):
    if action6:
        catalog = Action6RecordCatalog() if kind == "record" else Action6NativeCatalog(0)
    else:
        catalog = SegmentWaitCatalog() if kind == "record" else NativeSegmentCatalog(0)
    owned, runtime, aid, handler = await result_case_for().world(
        tmp_path, kind, catalog=catalog, start=False,
        assembly_options={"start_config": AttemptConfig(1, Decimal("10"), Decimal(0))}
    )
    driver = result_case_for().driver
    driver.control.return_value = DeviceCallResult.from_outcome(
        _result(
            no_effect=True,
            error=ErrorValue(
                "action6_response_unconfirmed", "device_settings",
                {"reason": "simulation_service_unavailable"},
            ) if driver_error else ErrorValue(
                "device_start_failed", "execution", {"operation_run_id": "1"}
            ),
        )
    )
    enabled = _reject_segment_projection(monkeypatch) if lost else None
    try:
        if lost:
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(aid, runtime)
            enabled[0] = False
            runtime = await result_case_for().reopen(owned, cold=True)
        await capture_handler(handler)(aid, runtime)
        assert (await runtime.action(aid))["status"] == 4
        expected_error = (10 if kind == "record" else 13) if driver_error else 11
        assert (await runtime.action(aid))["error_code"] == expected_error
        if driver_error:
            from camctl.contracts.json_values import parse_exact_json

            saved = owned.connection.execute(
                "SELECT error_json FROM operation_attempts"
            ).fetchone()[0]
            expected_driver_error = {
                "code": "action6_response_unconfirmed",
                "stage": "device_settings",
                "details": {"reason": "simulation_service_unavailable"},
            }
            assert parse_exact_json(saved) == expected_driver_error
            if kind == "timelapse":
                saved_run = owned.connection.execute(
                    "SELECT error_json FROM operation_runs WHERE kind=1"
                ).fetchone()[0]
                assert parse_exact_json(saved_run) == expected_driver_error
        assert owned.connection.execute(
            "SELECT source_kind,member_count,published_event_id IS NOT NULL FROM segment_sets"
        ).fetchone() == (3, 0, 1)
        assert owned.connection.execute(
            "SELECT reason FROM segment_check_conclusions"
        ).fetchone() == (9,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            0,
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE kind=4"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities"
        ).fetchone() == (2,)
        assert driver.control.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("lost", [False, True])
async def test_unknown_driver_start_failure_keeps_occupancy_and_original_error(
    tmp_path, monkeypatch, lost
):
    from camctl.contracts.json_values import parse_exact_json

    owned, runtime, aid, handler = await result_case_for().world(
        tmp_path, "timelapse", catalog=NativeSegmentCatalog(0), start=False
    )
    driver = result_case_for().driver
    driver.control.return_value = DeviceCallResult.from_outcome(_result(error=ErrorValue(
        "action6_response_unconfirmed", "device_response",
        {"reason": "response_missing"},
    )))
    enabled = _reject_segment_projection(monkeypatch) if lost else None
    try:
        if lost:
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(aid, runtime)
            enabled[0] = False
            runtime = await result_case_for().reopen(owned, cold=True)
        await capture_handler(handler)(aid, runtime)
        assert (await runtime.action(aid))["status"] == 4
        failure = parse_exact_json(owned.connection.execute(
            "SELECT current_failure_json FROM segment_check_conclusions"
        ).fetchone()[0])
        assert failure == {
            "code": "capture_result_unconfirmed",
            "details": {"activity_id": "1", "reason": "start_unknown"},
        }
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities"
        ).fetchone() == (1,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        saved = parse_exact_json(owned.connection.execute(
            "SELECT error_json FROM operation_attempts"
        ).fetchone()[0])
        assert saved == {
            "code": "action6_response_unconfirmed",
            "stage": "device_response",
            "details": {"reason": "response_missing"},
        }
        assert driver.control.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("kind", ["record", "timelapse"])
async def test_cancel_before_start_publishes_empty_cancel_version_without_device_call(
    tmp_path, kind
):
    catalog = SegmentWaitCatalog() if kind == "record" else NativeSegmentCatalog(0)
    owned, runtime, aid, handler = await result_case_for().world(
        tmp_path, kind, catalog=catalog, start=False
    )
    driver = result_case_for().driver
    driver.control.return_value = DeviceCallResult.from_outcome(_result(no_effect=True))
    try:
        await _request_cancel(runtime, aid, [result_case_for().clock.wall])
        await capture_handler(handler)(aid, runtime)
        assert (await runtime.action(aid))["status"] == 6
        assert driver.control.await_count == 0
        assert owned.connection.execute(
            "SELECT publication_kind,member_count,published_event_id IS NOT NULL FROM segment_sets"
        ).fetchone() == (2, 0, 1)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities"
        ).fetchone() == (2,)
    finally:
        owned.connection.close()
