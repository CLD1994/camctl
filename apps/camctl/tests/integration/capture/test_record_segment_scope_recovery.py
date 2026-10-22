"""分段策略沿实际 START、STOP、原始分页和发布入口完成录像。"""

from dataclasses import replace
from decimal import Decimal
import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import SessionRecordingState, _stop_call, capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.directory import DirectoryCursor
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.capture.recovery import RecoveryBoundary
from camctl.history.events import load_event_registry

from .result_consumer_fixtures import consumer_world, returned
from .test_capture_contract import _NOW, _runtime
from .test_record_file_completion_wait import (
    _completed_result,
    _result_port,
    _stop_context,
)
from .test_record_file_completion_wait import _wait_world, _REGISTRY
from .test_result_page_history import _entry as _single_entry
from .test_result_page_history import _BINDING
from ..persistence.test_segment_file_pairing import _BaselineCatalog, _entry

pytestmark = pytest.mark.asyncio


async def test_segmented_baseline_failure_publishes_no_results_and_releases_occupancy(
    tmp_path,
):
    from camctl.devices.directory import DirectoryRead
    from camctl.operations.models import ErrorValue
    from .test_baseline_prepare import Directory

    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=SegmentWaitCatalog(), start=False
    )
    try:
        error = ErrorValue(
            "adb_directory_failed", "device_file_access", {"reason": "actual_error"}
        )
        runtime.baseline_directory = Directory(
            [lambda request: DirectoryRead(None, error, None)]
        )
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute(
            "SELECT status,error_code FROM actions"
        ).fetchone() == (4, 13)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities"
        ).fetchone() == (2,)
        assert owned.connection.execute(
            "SELECT source_kind,published_event_id IS NOT NULL FROM segment_sets"
        ).fetchone() == (3, 1)
        assert owned.connection.execute(
            "SELECT reason FROM segment_check_conclusions"
        ).fetchone() == (9,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts"
        ).fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            0,
        )
    finally:
        owned.connection.close()


@pytest.mark.parametrize("cause", ["stop_exhausted", "binding_unavailable"])
async def test_segmented_external_failure_publishes_explicit_no_results_source(
    tmp_path, cause
):
    from camctl.capture.handlers import _settle_stop_exhausted, _handle_binding_failure
    from camctl.devices.bindings import check_binding
    from types import SimpleNamespace
    from .test_call_result_save import ReturnedCall, _outcome

    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=SegmentWaitCatalog(), start=False
    )
    try:
        (activity,) = owned.connection.execute(
            "SELECT id FROM device_activities"
        ).fetchone()
        saved = runtime.capture.fix_baseline(
            BaselineFixSave(activity, 0, 0, runtime.wall_us()),
            new_operation_key(),
            owned,
        )
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        await capture_handler(handler)(action_id, runtime)
        if cause == "stop_exhausted":
            runtime.stopper = ReturnedCall(_outcome("stop"))
            await _stop_call(runtime, runtime.action(action_id))
            _settle_stop_exhausted(runtime, runtime.action(action_id))
        else:
            runtime.binding_check = lambda binding: check_binding(
                binding, SimpleNamespace(devices={})
            )
            assert _handle_binding_failure(runtime, runtime.action(action_id))
        assert runtime.action(action_id)["status"] == 4
        assert owned.connection.execute(
            "SELECT source_kind,published_event_id IS NOT NULL FROM segment_sets"
        ).fetchone() == (3, 1)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            0,
        )
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities"
        ).fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT reason FROM segment_check_conclusions"
        ).fetchone() == (8 if cause == "stop_exhausted" else 7,)
    finally:
        owned.connection.close()


def _cancel(owned, runtime, action_id):
    from datetime import datetime, timezone
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.persistence.repositories.acceptance import (
        AcceptanceRepository,
        ProcessInput,
    )
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from camctl.cancellation.models import (
        StartCancelAction,
        FixCancelTargets,
        FixedCancelSet,
        FixedTarget,
        SelectionBasis,
        CancellationEffect,
        ApplyCancelTarget,
        CancelApplyMode,
    )

    instant = datetime.fromtimestamp(
        runtime.wall_us() // 1000000, timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")
    saved = AcceptanceRepository().process_input(
        ProcessInput(
            ParsedInput(
                "cancel.json",
                {
                    "request_id": "2",
                    "created_at": instant,
                    "name": "取消录像",
                    "actions": [
                        {
                            "name": "取消",
                            "type": "cancel_task",
                            "params": {
                                "target": {"action_instance_id": str(action_id)}
                            },
                        }
                    ],
                },
            ),
            SegmentWaitCatalog(),
            CommandMode.RUN,
            runtime.wall_us(),
        ),
        new_operation_key(),
        owned,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    cancel_id = owned.connection.execute("SELECT MAX(id) FROM actions").fetchone()[0]
    repository = CancellationRepository()
    started = repository.start_cancel_action(
        StartCancelAction(cancel_id, runtime.wall_us()), new_operation_key(), owned
    )
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(
        FixCancelTargets(
            cancel_id,
            FixedCancelSet(
                (
                    FixedTarget(
                        action_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED
                    ),
                )
            ),
            runtime.wall_us(),
        ),
        new_operation_key(),
        owned,
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    applied = repository.apply_cancel_target(
        ApplyCancelTarget(
            fixed.value.item_ids[0], CancelApplyMode.WITH_STOP, runtime.wall_us()
        ),
        new_operation_key(),
        owned,
    )
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error


@pytest.mark.parametrize(
    "stage", ["before_results", "during_results", "before_publication"]
)
async def test_segmented_cancel_publishes_zero_outputs_and_preserves_original_stop(
    tmp_path, stage
):
    owned, runtime, action_id, handler, stopper = await _stopped_world(tmp_path)
    try:
        context = _stop_context(owned, action_id)

        async def read(request, batch):
            if stage == "during_results":
                _cancel(owned, runtime, action_id)
            return _completed_result(
                request.ticket,
                context,
                finalized=True,
                entries=[_entry("CAM001/CAM_20261022100000_0007_D.OSV")],
            )

        driver = _result_port(runtime, read)
        if stage == "before_results":
            _cancel(owned, runtime, action_id)
        if stage == "before_publication":
            finish = runtime.capture.finish_segment_recording
            runtime.capture.finish_segment_recording = lambda *args: DbOutcome(
                DbOutcomeKind.ROLLED_BACK, error=OSError("提交前中断")
            )
            with pytest.raises(ConsistencyError, match="提交前中断"):
                await capture_handler(handler)(action_id, runtime)
            runtime.capture.finish_segment_recording = finish
            _cancel(owned, runtime, action_id)
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 6
        assert owned.connection.execute(
            "SELECT publication_kind FROM segment_sets WHERE published_event_id IS NOT NULL"
        ).fetchall() == [(2,)]
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            0,
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE output_id IS NOT NULL"
        ).fetchone() == (0,)
        stopper.stop.assert_awaited_once()
        assert driver.list_results.await_count == (
            0 if stage == "before_results" else 1
        )
        before = owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone()
        await capture_handler(handler)(action_id, runtime)
        assert (
            owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
            == before
        )
    finally:
        owned.connection.close()


class SegmentWaitCatalog(_BaselineCatalog):
    duration_s = Decimal("12")

    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        if args[1] == "camera_record":
            factory = definition.task_factory
            return replace(
                definition,
                task_factory=lambda params: replace(
                    factory(params), file_completion_wait_s=Decimal("5")
                ),
            )
        return definition


async def _stopped_world(tmp_path, *, control_known=True, pending_capture_group=None):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path,
        "record",
        catalog=SegmentWaitCatalog(),
        start=False,
        pending_capture_group=pending_capture_group,
    )
    (activity,) = owned.connection.execute(
        "SELECT id FROM device_activities"
    ).fetchone()
    fixed = runtime.capture.fix_baseline(
        BaselineFixSave(activity, 0, 0, runtime.wall_us()), new_operation_key(), owned
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    await capture_handler(handler)(action_id, runtime)
    if not control_known:
        runtime.recording_state = SessionRecordingState(runtime)
    runtime.wall_us = lambda: _NOW + 12_000_000
    runtime.monotonic_ns = lambda: 17_000_000_000
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(
        returned("stop", "stop_confirmed", activity)
    )
    runtime.stopper = stopper
    assert (await _stop_call(runtime, runtime.action(action_id))).phase == "confirmed"
    return owned, runtime, action_id, handler, stopper


async def test_frozen_segment_policy_publishes_cross_page_originals_and_preview_without_media(
    tmp_path,
):
    owned, runtime, action_id, handler, stopper = await _stopped_world(tmp_path)
    try:
        context = _stop_context(owned, action_id)
        names = [
            f"CAM001/CAM_20261022100000_{index:04d}_D.OSV" for index in (7, 8, 9, 10)
        ]
        preview = names[0].removesuffix("OSV") + "LRF"
        cursor = DirectoryCursor(_BINDING, ("/DCIM",), 0, "/DCIM/" + names[1])

        async def read(request, batch):
            runtime.wall_us = lambda: _NOW + 17_000_000
            runtime.monotonic_ns = lambda: 22_000_000_000
            if request.params.get("cursor") is None:
                return _completed_result(
                    request.ticket,
                    context,
                    entries=[_entry(preview), _entry(names[0]), _entry(names[1])],
                    next_cursor=cursor,
                )
            return _completed_result(
                request.ticket,
                context,
                cursor=cursor,
                entries=[_entry(names[2]), _entry(names[3])],
                finalized=True,
            )

        driver = _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 3
        assert owned.connection.execute(
            "SELECT occupancy_state,output_set_finalized_event_id IS NOT NULL FROM device_activities"
        ).fetchone() == (2, 1)
        assert owned.connection.execute(
            "SELECT kind,COUNT(*) FROM outputs GROUP BY kind ORDER BY kind"
        ).fetchall() == [(1, 4), (3, 1)]
        assert owned.connection.execute(
            "SELECT COUNT(DISTINCT device_file_id) FROM outputs"
        ).fetchone() == (5,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE output_id IS NOT NULL"
        ).fetchone() == (3,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM intermediate_files"
        ).fetchone() == (0,)
        assert driver.list_results.await_count == 2
        stopper.stop.assert_awaited_once()
        before = owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone()
        await capture_handler(handler)(action_id, runtime)
        assert (
            owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
            == before
        )
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize(
    (
        "names",
        "status",
        "original_count",
        "preview_count",
        "scope_state",
        "scope_count",
    ),
    [
        (
            (
                "CAM001/CAM_20261022100000_0999_D.LRF",
                "CAM001/CAM_20261022100000_0999_D.OSV",
                "CAM002/CAM_20261022110000_0001_D.LRF",
                "CAM002/CAM_20261022110000_0001_D.OSV",
            ),
            3,
            2,
            2,
            "nonempty",
            2,
        ),
        (
            (
                "CAM001/CAM_20261022100000_0007_D.LRF",
                "CAM001/CAM_20261022100000_0008_D.LRF",
                "CAM001/CAM_20261022100000_0008_D.OSV",
            ),
            4,
            1,
            1,
            "empty",
            0,
        ),
        (("CAM001/unknown.LRF", "CAM001/unknown.OSV"), 4, 1, 1, "unknown", 0),
    ],
)
async def test_handler_publishes_directory_rollover_preview_only_start_and_unknown_order(
    tmp_path, names, status, original_count, preview_count, scope_state, scope_count
):
    owned, runtime, action_id, handler, stopper = await _stopped_world(tmp_path)
    try:
        runtime.check_config = AttemptConfig(1, Decimal("1"), Decimal(0))
        context = _stop_context(owned, action_id)

        async def read(request, batch):
            return _completed_result(
                request.ticket,
                context,
                finalized=True,
                entries=[_entry(name) for name in names],
            )

        driver = _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)
        action = runtime.action(action_id)
        assert action["status"] == status
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE kind=1"
        ).fetchone() == (original_count,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE kind=3"
        ).fetchone() == (preview_count,)
        assert owned.connection.execute(
            "SELECT json_extract(assessment_json,'$.default_scope.state') FROM segment_sets"
            " WHERE published_event_id IS NOT NULL"
        ).fetchone() == (
            int(
                enum_for("segment_sets.assessment_json.scope_state")[
                    scope_state.upper()
                ]
            ),
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (scope_count,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM intermediate_files"
        ).fetchone() == (0,)
        stopper.stop.assert_awaited_once()
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize(
    ("indexes", "sizes", "control_known", "status", "count", "scope"),
    [
        ((7, 8), (10, 10), True, 3, 2, 2),
        ((7, 8, 9, 10, 11), (10, 10, 10, 10, 0), False, 3, 4, 3),
        ((7, 8, 9, 10), (10, 10, 10, 0), False, 4, 3, 3),
        ((7, 8, 10), (10, 10, 10), True, 4, 3, 2),
        ((7, 8, 9), (0, 10, 10), True, 4, 2, 0),
        ((7,), (0,), True, 4, 0, 0),
    ],
)
async def test_segmented_completion_and_default_scope_remain_independent(
    tmp_path, indexes, sizes, control_known, status, count, scope
):
    owned, runtime, action_id, handler, stopper = await _stopped_world(
        tmp_path, control_known=control_known
    )
    try:
        runtime.check_config = AttemptConfig(1, Decimal("2"), Decimal("0"))
        context = _stop_context(owned, action_id)

        async def read(request, batch):
            return _completed_result(
                request.ticket,
                context,
                finalized=True,
                entries=[
                    _entry(f"CAM001/CAM_20261022100000_{index:04d}_D.OSV", size=size)
                    for index, size in zip(indexes, sizes)
                ],
            )

        driver = _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == status
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            count,
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (scope,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM intermediate_files"
        ).fetchone() == (0,)
        if indexes == (7, 8, 10) or sizes[0] == 0 and count:
            assert runtime.action(action_id)["error_details_json"]["gap_count"] > 0
        elif not control_known and status == 4:
            assert (
                runtime.action(action_id)["error_details_json"]["reason"]
                == "completion_unknown"
            )
        stopper.stop.assert_awaited_once()
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_segment_finish_lost_receipt_recovers_original_request_without_duplicate_outputs(
    tmp_path,
):
    owned, runtime, action_id, handler, stopper = await _stopped_world(tmp_path)
    try:
        context = _stop_context(owned, action_id)

        async def read(request, batch):
            return _completed_result(
                request.ticket,
                context,
                entries=[_entry("CAM001/CAM_20261022100000_0007_D.OSV")],
                finalized=True,
            )

        driver = _result_port(runtime, read)
        original = runtime.capture.finish_segment_recording
        requests = []

        def lost(request, key, owned):
            requests.append((request, key))
            result = original(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("已提交，回执丢失"))

        runtime.capture.finish_segment_recording = lost
        with pytest.raises(ConsistencyError, match="回执丢失"):
            await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 3
        before = owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone()
        runtime.capture.finish_segment_recording = original
        await capture_handler(handler)(action_id, runtime)
        assert not runtime.pending_segment_writes
        assert (
            owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
            == before
        )
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            1,
        )
        assert len(requests) == 1
        stopper.stop.assert_awaited_once()
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_segment_release_lost_receipt_keeps_original_request_before_publication(
    tmp_path,
):
    owned, runtime, action_id, handler, stopper = await _stopped_world(tmp_path)
    try:
        context = _stop_context(owned, action_id)

        async def read(request, batch):
            return _completed_result(
                request.ticket,
                context,
                finalized=True,
                entries=[_entry("CAM001/CAM_20261022100000_0007_D.OSV")],
            )

        driver = _result_port(runtime, read)
        original = runtime.capture.release_occupancy
        requests = []

        def lost_once(request, key, owned):
            requests.append((request, key))
            result = original(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return (
                DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("释放回执丢失"))
                if len(requests) == 1
                else result
            )

        runtime.capture.release_occupancy = lost_once
        with pytest.raises(ConsistencyError, match="释放回执丢失"):
            await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 2
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            0,
        )
        await capture_handler(handler)(action_id, runtime)
        assert requests[0] == requests[1]
        assert runtime.action(action_id)["status"] == 3
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities"
        ).fetchone() == (2,)
        assert not runtime.pending_segment_writes
        stopper.stop.assert_awaited_once()
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("latest", ["missing", "gap", "error"])
async def test_last_scan_keeps_qualified_prior_files_and_its_own_failure(
    tmp_path, latest
):
    from camctl.operations.models import ErrorValue

    owned, runtime, action_id, handler, stopper = await _stopped_world(tmp_path)
    runtime.check_config = AttemptConfig(2, Decimal("1"), Decimal(0))
    context = _stop_context(owned, action_id)
    count = 0

    async def read(request, batch):
        nonlocal count
        count += 1
        index = 7 if count == 1 else 9
        return _completed_result(
            request.ticket,
            context,
            entries=[_entry(f"CAM001/CAM_20261022100000_{index:04d}_D.OSV")],
            finalized=count > 1,
            error=ErrorValue("device_error", "device")
            if count > 1 and latest == "error"
            else None,
        )

    if latest == "missing":

        async def read(request, batch):
            nonlocal count
            count += 1
            return _completed_result(
                request.ticket,
                context,
                entries=[_entry("CAM001/CAM_20261022100000_0007_D.OSV")]
                if count == 1
                else [],
                finalized=count > 1,
            )

    driver = _result_port(runtime, read)
    try:
        advance = capture_handler(handler)
        await advance(action_id, runtime)
        assert runtime.action(action_id)["status"] == 2
        await advance(action_id, runtime)
        assert runtime.action(action_id)["status"] == 4
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            1 if latest == "missing" else 2,
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (0,)
        details = runtime.action(action_id)["error_details_json"]
        if latest == "gap":
            assert (
                details["gap_count"] == 1
                and details["default_scope"]["state"] == "unknown"
            )
        else:
            assert details["reason"] == (
                "no_outputs" if latest == "missing" else "device_failed"
            )
        assert driver.list_results.await_count == 2
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_incomplete_segment_scan_keeps_qualified_files_without_complete_default_scope(
    tmp_path,
):
    owned, runtime, action_id, handler, stopper = await _stopped_world(tmp_path)
    try:
        runtime.check_config = AttemptConfig(1, Decimal("2"), Decimal("0"))
        context = _stop_context(owned, action_id)

        async def read(request, batch):
            return _completed_result(
                request.ticket,
                context,
                entries=[_entry("CAM001/CAM_20261022100000_0007_D.OSV")],
                finalized=False,
            )

        driver = _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 4
        assert (
            runtime.action(action_id)["error_details_json"]["reason"]
            == "outputs_unknown"
        )
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            1,
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT published_event_id IS NOT NULL,facts_json,assessment_json FROM segment_sets"
        ).fetchone() == (1, None, None)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies"
        ).fetchone() == (0,)
        stopper.stop.assert_awaited_once()
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_single_original_frozen_task_keeps_its_existing_handler(
    tmp_path, monkeypatch
):
    from camctl.capture import segment_completion

    async def reject_segment_dispatch(*args):
        raise AssertionError("单原片任务不得进入分段处理器")

    monkeypatch.setattr(
        segment_completion, "advance_segmented_recording", reject_segment_dispatch
    )
    owned, runtime, action_id, handler = await _wait_world(tmp_path)
    try:
        assert (
            runtime.action(action_id)["execution_spec_json"].get("result_policy")
            is None
        )
        context = _stop_context(owned, action_id)

        async def read(request, batch):
            return _completed_result(
                request.ticket,
                context,
                entries=[_single_entry("single")],
                finalized=True,
            )

        _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 3
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            1,
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_sets"
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_segment_scan_resume_reuses_original_baseline_and_saved_pages(tmp_path):
    owned, runtime, action_id, handler, stopper = await _stopped_world(tmp_path)
    try:
        runtime.check_config = AttemptConfig(1, Decimal("2"), Decimal("0"))
        context = _stop_context(owned, action_id)
        original = "/DCIM/CAM001/CAM_20261022100000_0007_D.OSV"
        cursor = DirectoryCursor(_BINDING, ("/DCIM",), 0, original)
        entered = asyncio.Event()

        async def read(request, batch):
            if request.params.get("cursor") is not None:
                entered.set()
                await asyncio.Future()
            return _completed_result(
                request.ticket,
                context,
                entries=[_entry(original.removeprefix("/DCIM/"))],
                next_cursor=cursor,
            )

        driver = _result_port(runtime, read)
        task = asyncio.create_task(capture_handler(handler)(action_id, runtime))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        (page,) = owned.connection.execute(
            "SELECT t.result_last_page_event_id FROM operation_attempts t"
            " JOIN operation_runs r ON r.id=t.run_id WHERE r.responsibility_key=?",
            (f"results/{action_id}",),
        ).fetchone()
        (horizon,) = owned.connection.execute(
            "SELECT MAX(id) FROM history_events"
        ).fetchone()
        (baseline,) = owned.connection.execute(
            "SELECT id FROM history_events WHERE event_type=?",
            (load_event_registry()["events"]["BASELINE_CHUNK"]["id"],),
        ).fetchone()
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = _runtime(owned, check_config=runtime.check_config)
        resumed.wall_us = lambda: _NOW + 30_000_000
        resumed.monotonic_ns = lambda: 1_000_000_000
        resumed.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
        resumed.recovery_max_event_id = horizon
        resumed.recovery_evidence_for = lambda binding, operation: _REGISTRY
        _result_port(resumed, read)
        await capture_handler(handler)(action_id, resumed)
        assert resumed.action(action_id)["status"] == 4
        assert (
            resumed.action(action_id)["error_details_json"]["reason"]
            == "outputs_unknown"
        )
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            1,
        )
        assert owned.connection.execute(
            "SELECT page_event_id FROM segment_members"
        ).fetchone() == (page,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE id=?", (baseline,)
        ).fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (0,)
        assert driver.list_results.await_count == 2
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()
