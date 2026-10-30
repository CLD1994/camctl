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
from camctl.devices.tasks import CaptureResultPolicy, VideoResultMode
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
from ..support.native_capture_cases import native_world, request_cancel
from ..support.result_cases import ResultCase
from .result_consumer_fixtures import result_driver

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
                    video_result_mode=VideoResultMode.SEGMENTED,
                ),
            )
        return definition


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("zero_position", ["tail", "middle"])
async def test_native_empty_tail_uses_original_natural_target_and_retains_per_file_identity(
    tmp_path, canceled, zero_position
):
    catalog = NativeSegmentCatalog(0)
    owned, runtime, aid, advance, wall, mono, driver, stop = await native_world(tmp_path, catalog)
    try:
        if canceled:
            wall[0] = _NOW + 100000
            await request_cancel(runtime, aid, wall[0], catalog)
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

        driver.list_results.side_effect = read
        wall[0] = _NOW + 1200000
        mono[0] += 1200000000
        await advance(aid, runtime)
        assert (await runtime.action(aid))["status"] == (
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


async def test_native_recovery_before_first_result_page_keeps_result_unknown(tmp_path, capture_resources):
    case = ResultCase(capture_resources)
    owned, runtime, aid, handler = await case.world(tmp_path, "timelapse", start=False,
        catalog=NativeSegmentCatalog(0),
        evidence_contracts=(_REGISTRY.contract("adb_foreground_recovery", 1),),
        assembly_options={"wait_config": lambda action: CaptureWaitConfig(1000, 200, 0, 0),
            "check_config": AttemptConfig(1, Decimal("1"), Decimal(0)),
            "result_recovery": True})
    entered = asyncio.Event()

    async def read(request, batch):
        entered.set()
        await asyncio.Future()

    driver = result_driver(case, side_effect=read)
    try:
        await capture_handler(handler)(aid, runtime)
        case.clock.wall = _NOW + 1_200_000
        case.clock.monotonic = 6_200_000_000
        task = asyncio.create_task(capture_handler(handler)(aid, runtime))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        horizon, = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()
        resumed = await case.reopen(owned, cold=True, assembly_options={
            "recovery_boundary": RecoveryBoundary.HOST_LOCAL_SETTLED,
            "recovery_max_event_id": lambda: horizon})
        await capture_handler(handler)(aid, resumed)
        action = await resumed.action(aid)
        assert action["status"] == 4
        assert action["error_details_json"]["reason"] == "outputs_unknown"
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM result_page_files").fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_sets WHERE published_event_id IS NOT NULL").fetchone() == (1,)
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE kind=7").fetchone() == (1,)
        driver.list_results.assert_awaited_once()
        case.driver.control.assert_awaited_once()
    finally:
        await case.close()
        owned.connection.close()
