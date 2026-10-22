"""原生策略沿真实自然等待发布非零原片，空尾只保留观察。"""

from dataclasses import replace
from decimal import Decimal
import asyncio
from pathlib import Path
from unittest.mock import create_autospec
import pytest

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import capture_handler
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import new_operation_key
from camctl.devices.tasks import CaptureResultPolicy
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    CallOutcome,
    AttemptStatus,
    EffectState,
    ErrorValue,
    Settlement,
    SettlementBasis,
    EvidenceValue,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from .result_consumer_fixtures import consumer_world
from .test_natural_wait_time_eligibility import SmallNativeCatalog
from .test_capture_contract import _NOW, _runtime
from .test_record_file_completion_wait import _result_port
from .test_record_file_completion_wait import _REGISTRY
from .test_result_page_history import _actual
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from ..persistence.test_segment_file_pairing import _entry

pytestmark = pytest.mark.asyncio


class NativeSegmentCatalog(SmallNativeCatalog):
    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        if args[1] == "camera_timelapse":
            factory = definition.task_factory
            return replace(
                definition,
                task_factory=lambda params: replace(
                    factory(params),
                    ownership_mode=2,
                    output_scope={"directories": ["/DCIM"]},
                    result_policy=CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1,
                ),
            )
        return definition


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("zero_position", ["tail", "middle"])
async def test_native_empty_tail_uses_original_natural_target_and_retains_per_file_identity(
    tmp_path, canceled, zero_position
):
    owned, runtime, aid, handler = await consumer_world(
        tmp_path, "timelapse", catalog=NativeSegmentCatalog(0), start=False
    )
    try:
        (activity,) = owned.connection.execute(
            "SELECT id FROM device_activities"
        ).fetchone()
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity, 0, 0, _NOW), new_operation_key(), owned
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        wall = [_NOW]
        mono = [5000000000]
        runtime.wall_us = lambda: wall[0]
        runtime.monotonic_ns = lambda: mono[0]
        runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, 0, 0)
        runtime.check_config = AttemptConfig(1, Decimal("1"), Decimal(0))
        advance = capture_handler(handler)
        await advance(aid, runtime)
        if canceled:
            _apply_public_cancel(owned, aid, _NOW + 100000)
            stop = create_autospec(StopDriver, instance=True)
            stop.stop.return_value = DeviceCallResult.from_outcome(
                CallOutcome(
                    status=AttemptStatus.FAILED,
                    effect=EffectState.UNKNOWN,
                    error=ErrorValue("device_error", "device"),
                    settlement=Settlement(
                        SettlementBasis.OBSERVED, EvidenceValue("stop_returned", 1, {})
                    ),
                )
            )
            runtime.stopper = stop
            await advance(aid, runtime)

        async def read(request, batch):
            entries = sorted(
                [
                    _entry(
                        f"CAM_001/CAM_20261022100000_{n:04d}_D.{suffix}",
                        size=0 if n == (9 if zero_position == "tail" else 8) else 20,
                    )
                    for n in (7, 8, 9)
                    for suffix in ("OSV", "LRF")
                ],
                key=lambda e: e["identity"],
            )
            return DeviceCallResult.from_outcome(
                _actual(request.ticket, entries=entries, finalized=True)
            )

        driver = _result_port(runtime, read)
        wall[0] = _NOW + 1200000
        mono[0] += 1200000000
        await advance(aid, runtime)
        assert runtime.action(aid)["status"] == (
            6 if canceled else 3 if zero_position == "tail" else 4
        )
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            4,
        )
        assert owned.connection.execute(
            "SELECT facts_json,assessment_json,published_event_id IS NOT NULL FROM segment_sets"
        ).fetchone() == (None, None, 1)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies"
        ).fetchone() == (0,)
        driver.list_results.assert_awaited_once()
        if canceled:
            stop.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_native_recovery_before_first_result_page_keeps_result_unknown(tmp_path):
    owned, runtime, aid, handler = await consumer_world(
        tmp_path, "timelapse", catalog=NativeSegmentCatalog(0), start=False
    )
    try:
        (activity,) = owned.connection.execute("SELECT id FROM device_activities").fetchone()
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity, 0, 0, _NOW), new_operation_key(), owned
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        wall, mono = [_NOW], [5_000_000_000]
        runtime.wall_us = lambda: wall[0]
        runtime.monotonic_ns = lambda: mono[0]
        runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, 0, 0)
        runtime.check_config = AttemptConfig(1, Decimal("1"), Decimal(0))
        await capture_handler(handler)(aid, runtime)
        entered = asyncio.Event()

        async def read(request, batch):
            entered.set()
            await asyncio.Future()

        driver = _result_port(runtime, read)
        wall[0], mono[0] = _NOW + 1_200_000, 6_200_000_000
        task = asyncio.create_task(capture_handler(handler)(aid, runtime))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        (horizon,) = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = _runtime(owned, driver=runtime.driver, check_config=runtime.check_config)
        resumed.wall_us = lambda: wall[0]
        resumed.monotonic_ns = lambda: mono[0]
        resumed.wait_config = runtime.wait_config
        resumed.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
        resumed.recovery_max_event_id = horizon
        resumed.recovery_evidence_for = lambda binding, operation: _REGISTRY
        _result_port(resumed, read)
        await capture_handler(handler)(aid, resumed)
        assert resumed.action(aid)["status"] == 4
        assert resumed.action(aid)["error_details_json"]["reason"] == "outputs_unknown"
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM result_page_files").fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_sets WHERE published_event_id IS NOT NULL"
        ).fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE kind=7"
        ).fetchone() == (1,)
        driver.list_results.assert_awaited_once()
        runtime.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()
