"""结果策略任务在 START 失败及取消时仍发布同一来源边界。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.operations.models import ErrorValue
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.contracts.values import ConsistencyError

from .result_consumer_fixtures import consumer_world
from .test_native_segment_completion import NativeSegmentCatalog
from .test_record_segment_scope_recovery import SegmentWaitCatalog, _cancel
from .test_recording_start_runtime import StartDriver, _result
from .test_action6_segmented_normal import Action6NativeCatalog, Action6RecordCatalog

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("kind", ["record", "timelapse"])
@pytest.mark.parametrize("lost", [False, True])
@pytest.mark.parametrize("driver_error", [False, True])
@pytest.mark.parametrize("action6", [False, True])
async def test_start_failure_publishes_empty_unknown_source_and_resumes_without_start(
    tmp_path, kind, lost, driver_error, action6
):
    if action6:
        catalog = Action6RecordCatalog() if kind == "record" else Action6NativeCatalog(0)
    else:
        catalog = SegmentWaitCatalog() if kind == "record" else NativeSegmentCatalog(0)
    owned, runtime, aid, handler = await consumer_world(
        tmp_path, kind, catalog=catalog, start=False
    )
    driver = StartDriver(
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
    runtime.driver = driver
    runtime.start_config = AttemptConfig(1, Decimal("10"), Decimal(0))
    original = runtime.capture.finish_segment_recording
    if lost:
        runtime.capture.finish_segment_recording = lambda *args, **kwargs: DbOutcome(
            DbOutcomeKind.ROLLED_BACK, error=OSError("来源终态尚未提交")
        )
    try:
        if lost:
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(aid, runtime)
            runtime.capture.finish_segment_recording = original
            runtime = replace(
                runtime, pending_start_results={}, pending_segment_writes={}
            )
        await capture_handler(handler)(aid, runtime)
        assert runtime.action(aid)["status"] == 4
        expected_error = (10 if kind == "record" else 13) if driver_error else 11
        assert runtime.action(aid)["error_code"] == expected_error
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
        assert len(driver.calls) == 1
    finally:
        runtime.capture.finish_segment_recording = original
        owned.connection.close()


@pytest.mark.parametrize("lost", [False, True])
async def test_unknown_driver_start_failure_keeps_occupancy_and_original_error(
    tmp_path, lost
):
    from camctl.contracts.json_values import parse_exact_json

    owned, runtime, aid, handler = await consumer_world(
        tmp_path, "timelapse", catalog=NativeSegmentCatalog(0), start=False
    )
    driver = StartDriver(_result(error=ErrorValue(
        "action6_response_unconfirmed", "device_response",
        {"reason": "response_missing"},
    )))
    runtime.driver = driver
    original = runtime.capture.finish_segment_recording
    if lost:
        runtime.capture.finish_segment_recording = lambda *args, **kwargs: DbOutcome(
            DbOutcomeKind.ROLLED_BACK, error=OSError("来源终态尚未提交")
        )
    try:
        if lost:
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(aid, runtime)
            runtime.capture.finish_segment_recording = original
            runtime = replace(
                runtime, pending_start_results={}, pending_segment_writes={}
            )
        await capture_handler(handler)(aid, runtime)
        assert runtime.action(aid)["status"] == 4
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
        assert len(driver.calls) == 1
    finally:
        runtime.capture.finish_segment_recording = original
        owned.connection.close()


@pytest.mark.parametrize("kind", ["record", "timelapse"])
async def test_cancel_before_start_publishes_empty_cancel_version_without_device_call(
    tmp_path, kind
):
    catalog = SegmentWaitCatalog() if kind == "record" else NativeSegmentCatalog(0)
    owned, runtime, aid, handler = await consumer_world(
        tmp_path, kind, catalog=catalog, start=False
    )
    driver = StartDriver(_result(no_effect=True))
    runtime.driver = driver
    try:
        _cancel(owned, runtime, aid)
        await capture_handler(handler)(aid, runtime)
        assert runtime.action(aid)["status"] == 6
        assert not driver.calls
        assert owned.connection.execute(
            "SELECT publication_kind,member_count,published_event_id IS NOT NULL FROM segment_sets"
        ).fetchone() == (2, 0, 1)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities"
        ).fetchone() == (2,)
    finally:
        owned.connection.close()
