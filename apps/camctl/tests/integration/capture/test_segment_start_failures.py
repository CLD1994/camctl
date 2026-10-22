"""结果策略任务在 START 失败及取消时仍发布同一来源边界。"""

from dataclasses import replace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.contracts.values import ConsistencyError

from .result_consumer_fixtures import consumer_world
from .test_native_segment_completion import NativeSegmentCatalog
from .test_record_segment_scope_recovery import SegmentWaitCatalog, _cancel
from .test_recording_start_runtime import StartDriver, _result

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("kind", ["record", "timelapse"])
@pytest.mark.parametrize("lost", [False, True])
async def test_start_failure_publishes_empty_unknown_source_and_resumes_without_start(
    tmp_path, kind, lost
):
    catalog = SegmentWaitCatalog() if kind == "record" else NativeSegmentCatalog(0)
    owned, runtime, aid, handler = await consumer_world(
        tmp_path, kind, catalog=catalog, start=False
    )
    driver = StartDriver(
        _result(
            no_effect=True,
            error=ErrorValue(
                "device_start_failed", "execution", {"operation_run_id": "1"}
            ),
        )
    )
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
