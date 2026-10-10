"""主机到期停止与失去连续计时依据分别保存实际事实和动作结果。"""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.handlers import _listing_round
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.bindings import BindingResult, BindingStatus
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.devices.tasks import EndControl, StartReturn
from camctl.capture.models import HostTimerStopSave
from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import AttemptConfig, RunFinish, RunOutcome
from camctl.operations.models import EffectState, ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import RESULT_EVIDENCE, consumer_world, returned
from .test_result_device_completion import DeviceCompletionCatalog, _EVIDENCE, device_pages

pytestmark = pytest.mark.asyncio


class HostCatalog(DeviceCompletionCatalog):
    def __init__(self, meaning=StartReturn.SENT, *, baseline=False, end_control=EndControl.HOST_TIMER):
        self.meaning = meaning
        self.baseline = baseline
        self.end_control = end_control

    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if action_type == "camera_record" and self.baseline:
            return replace(definition, task_factory=lambda params: replace(
                definition.task_factory(params), ownership_mode=2,
                output_scope={"directories": ["/DCIM"]}))
        if action_type != "camera_timelapse":
            return definition
        return replace(definition, task_factory=lambda params: replace(
            definition.task_factory(params), target_duration_s=Decimal("10"),
            end_control=self.end_control, start_return_meaning=self.meaning,
            ownership_mode=2 if self.baseline else 1,
            output_scope={"directories": ["/DCIM"]} if self.baseline else {"task": "own"},
            product_rules=({"kind": "video", "format_id": "mp4", "min_count": 1,
                "exact_count": None, "require_pairing": False},)))


async def world(tmp_path, *, meaning=StartReturn.SENT, case="video", consumer="timelapse", completion_page=None,
                baseline=False, end_control=EndControl.HOST_TIMER):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, consumer,
        independent_activity=True, catalog=HostCatalog(meaning, baseline=baseline, end_control=end_control), start=False)
    if baseline:
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryRead
        from .test_baseline_prepare import Directory
        runtime.baseline_directory = Directory([lambda request: DirectoryRead(None, None, Page((), None))])
    results = device_pages(runtime, completion_page=completion_page, case=case)
    runtime.evidence = EvidenceRegistry(tuple(RESULT_EVIDENCE.contract(name, 1) for name in (
        "operation_returned", "timelapse_sent", "start_confirmed", "stop_returned", "stop_confirmed")) + (
        _EVIDENCE.contract("result_files_listed", 2), _EVIDENCE.contract("results_returned", 1),
        _EVIDENCE.contract("timelapse_completed", 1)))
    stop = create_autospec(StopDriver, instance=True)
    stop.stop.return_value = DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", 1))
    runtime.stopper = stop
    clock = [5_000_000_000]
    runtime.monotonic_ns = lambda: clock[0]
    return owned, runtime, action_id, handler, results, clock


@pytest.mark.parametrize("meaning", [StartReturn.SENT, StartReturn.STARTED])
async def test_waits_until_original_host_target_without_listing_or_stop(tmp_path, meaning):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, meaning=meaning)
    try:
        await capture_handler(handler)(action_id, runtime)
        clock[0] = 14_999_999_999
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        runtime.stopper.stop.assert_not_awaited()
        results.list_results.assert_not_awaited()
        runtime.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("meaning", [StartReturn.SENT, StartReturn.STARTED])
@pytest.mark.parametrize("case,status,reason", [
    ("video", 3, None), ("empty", 4, "no_outputs"), ("wrong_kind", 4, "invalid_outputs")])
async def test_host_target_stop_then_qualifies_result(tmp_path, meaning, case, status, reason):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, meaning=meaning, case=case)
    try:
        await capture_handler(handler)(action_id, runtime)
        clock[0] = 15_000_000_000
        await capture_handler(handler)(action_id, runtime)
        row = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert row[0] == status
        if reason is not None:
            assert json.loads(row[1]) == {"activity_id": "1", "reason": reason}
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns,occupancy_state,expected_check_at,wait_completed_event_id FROM device_activities").fetchone() == (3, 10_000_000_000, 2, None, None)
        stop_event, stop_txn, stop_time = owned.connection.execute(
            "SELECT h.id,h.transaction_id,h.occurred_at FROM history_events h"
            " JOIN operation_attempts a ON a.result_event_id=h.id JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.responsibility_key=?", (f"stop/{action_id}",)).fetchone()
        ends = [(txn, time, json.loads(body)) for txn, time, body in owned.connection.execute(
            "SELECT transaction_id,occurred_at,body_json FROM history_events WHERE event_type=13")
            if any(row["after"]["values"].get("activity_state") == 3 for row in json.loads(body)["rows"])]
        assert len(ends) == 1 and ends[0][:2] == (stop_txn, stop_time)
        assert ends[0][2]["evidence"]["observation"] == {
            "stop_result_event_id": stop_event, "control_elapsed_ns": 10_000_000_000}
        capture = json.loads(owned.connection.execute("SELECT capture_json FROM device_activities").fetchone()[0])
        assert "elapsed_s" not in capture and "captured_count" not in capture
        runtime.stopper.stop.assert_awaited_once()
        assert runtime.stopper.stop.call_args.args[0].operation == "stop_timelapse"
        assert results.list_results.await_count == 2
        assert runtime.timelapse_deadlines == {}
    finally:
        owned.connection.close()


@pytest.mark.parametrize("baseline", [False, True])
@pytest.mark.parametrize("end_control", [EndControl.HOST_TIMER, EndControl.DEVICE])
async def test_business_cancel_before_target_keeps_original_canceled_result(tmp_path, baseline, end_control):
    from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixedCancelSet, FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from camctl.contracts.values import new_operation_key

    owned, runtime, action_id, handler, results, clock = await world(tmp_path, consumer="cancel",
        baseline=baseline, end_control=end_control)
    try:
        await capture_handler(handler)(action_id, runtime)
        repository = CancellationRepository()
        cancel_id = action_id + 1
        receipt = repository.start_cancel_action(StartCancelAction(cancel_id, runtime.wall_us()), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        receipt = repository.fix_cancel_targets(FixCancelTargets(cancel_id, FixedCancelSet((FixedTarget(
            action_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), runtime.wall_us()), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        item_id, = receipt.value.item_ids
        receipt = repository.apply_cancel_target(ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP,
            runtime.wall_us()), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        clock[0] = 7_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?", (action_id,)).fetchone() == (6, None)
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns FROM device_activities").fetchone() == (
            3, 2_000_000_000 if end_control is EndControl.HOST_TIMER else None)
        assert owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
        assert runtime.capture.read_result_completion(action_id, owned) is None
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("unknown_files", [False, True])
async def test_actual_goal_completion_after_unknown_duration_still_carries_its_guarantee(tmp_path, unknown_files):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path,
        case="unknown_format" if unknown_files else "video", completion_page=1)
    try:
        await capture_handler(handler)(action_id, runtime)
        runtime.timelapse_deadlines.clear()
        await capture_handler(handler)(action_id, runtime)
        if unknown_files:
            assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
            assert runtime.capture.read_result_completion(action_id, owned) is not None
            evidence = runtime.evidence
            device_pages(runtime, completion_page=None)
            runtime.evidence = evidence
            clock[0] += 3_000_000_000
            await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert owned.connection.execute("SELECT control_elapsed_ns FROM device_activities").fetchone() == (None,)
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("binding", [BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH])
async def test_held_stop_is_saved_before_new_binding_failure(tmp_path, binding):
    from camctl.contracts.workflow_errors import registered_error
    class UnknownStop(CaptureRepository):
        def finish_host_timer_stop(self, *args, **kwargs):
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原停止未保存"))
    owned, runtime, action_id, handler, results, clock = await world(tmp_path)
    try:
        await capture_handler(handler)(action_id, runtime)
        clock[0] = 15_000_000_000
        runtime.capture = UnknownStop()
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(action_id, runtime)
        pending, = runtime.pending_start_results.values()
        runtime.capture = CaptureRepository()
        runtime.evidence = None
        runtime.stopper = None
        runtime.binding_check = lambda saved: BindingResult(binding, saved,
            "other" if binding is BindingStatus.DRIVER_MISMATCH else None)
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns FROM device_activities").fetchone() == (3, 10_000_000_000)
        assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?", (action_id,)).fetchone() == (4, registered_error("device_binding_unavailable")["action_error_id"])
        assert runtime.pending_start_results == {}
        assert pending.finish.occurred_at == runtime.wall_us()
        results.list_results.assert_not_awaited()
    finally:
        owned.connection.close()


async def test_typed_stop_confirmation_does_not_require_a_generic_observation_name(tmp_path):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path)
    contract = EvidenceContract("native_stop_returned", 1, "stop",
        frozenset({"activity_id"}), identity_field="activity_id")
    runtime.evidence = EvidenceRegistry((*(runtime.evidence.contract(name, version)
        for name, version in (("operation_returned", 1), ("timelapse_sent", 1),
            ("stop_returned", 1), ("result_files_listed", 2), ("results_returned", 1))), contract))
    runtime.stopper.stop.return_value = DeviceCallResult.from_outcome(replace(
        returned("stop", "stop_confirmed", 1), observations=(
            DeviceObservation(contract.type, 1, {"activity_id": "1"}),)))
    try:
        await capture_handler(handler)(action_id, runtime)
        clock[0] = 15_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns FROM device_activities").fetchone() == (3, 10_000_000_000)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("duration_known", [False, True])
@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_stop_save_preserves_original_result_time_and_duration(tmp_path, duration_known, committed):
    class UnknownStop(CaptureRepository):
        def __init__(self):
            self.requests = []

        def finish_host_timer_stop(self, finish, stop, key, owned):
            self.requests.append((finish, stop, key))
            if committed:
                receipt = super().finish_host_timer_stop(finish, stop, key, owned)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原停止回执未知"))

    owned, runtime, action_id, handler, results, clock = await world(tmp_path)
    try:
        await capture_handler(handler)(action_id, runtime)
        if duration_known:
            clock[0] = 15_000_000_000
        else:
            runtime.timelapse_deadlines.clear()
        repository = UnknownStop()
        runtime.capture = repository
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(action_id, runtime)
        assert len(repository.requests) == 2 and repository.requests[0] == repository.requests[1]
        pending, = runtime.pending_start_results.values()
        assert pending.host_timer_stop.control_elapsed_ns == (10_000_000_000 if duration_known else None)
        if committed:
            for changed in (HostTimerStopSave(None if duration_known else 10_000_000_000),
                            HostTimerStopSave(10_000_000_001)):
                rejected = CaptureRepository().finish_host_timer_stop(pending.finish, changed, pending.key, owned)
                assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        runtime.owned, runtime.capture = owned, CaptureRepository()
        runtime.timelapse_deadlines.clear()
        clock[0] = 99_000_000_000
        runtime.stop_config = AttemptConfig(1, Decimal("0.01"), Decimal("0"))
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3 if duration_known else 4,)
        runtime.stopper.stop.assert_awaited_once()
        runtime.driver.control.assert_awaited_once()
        assert owned.connection.execute("SELECT occurred_at FROM history_events WHERE id=(SELECT result_event_id FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?)",
            (f"stop/{action_id}",)).fetchone() == (pending.finish.occurred_at,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("duration_known", [False, True])
async def test_stop_exhaustion_keeps_unknown_activity_and_complete_files(tmp_path, duration_known):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path)
    runtime.stop_config = AttemptConfig(2, Decimal("1"), Decimal("0"))
    runtime.stopper.stop.return_value = DeviceCallResult.from_outcome(replace(
        returned("stop", "stop_confirmed", 1), effect=EffectState.UNKNOWN, observations=()))
    try:
        await capture_handler(handler)(action_id, runtime)
        if duration_known:
            clock[0] = 15_000_000_000
        else:
            runtime.timelapse_deadlines.clear()
        for _ in range(3):
            await capture_handler(handler)(action_id, runtime)
        row = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert row[0] == 4
        assert json.loads(row[1]) == {"activity_id": "1", "reason": "completion_unknown" if duration_known else "duration_unknown"}
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns,occupancy_state FROM device_activities").fetchone() == (1, None, 1)
        assert owned.connection.execute("SELECT count(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert runtime.stopper.stop.await_count == 2
    finally:
        owned.connection.close()


async def test_duration_unknown_preserves_complete_files_when_listing_exhausts(tmp_path):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, case="read_error")
    runtime.check_config = AttemptConfig(1, Decimal("1"), Decimal("0"))
    try:
        await capture_handler(handler)(action_id, runtime)
        runtime.timelapse_deadlines.clear()
        await capture_handler(handler)(action_id, runtime)
        await capture_handler(handler)(action_id, runtime)
        row = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert row[0] == 4 and json.loads(row[1])["reason"] == "duration_unknown"
        assert owned.connection.execute("SELECT count(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (1,)
        assert owned.connection.execute("SELECT a.error_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=7").fetchone()[0] is not None
    finally:
        owned.connection.close()


async def test_stop_fact_keeps_occupancy_until_files_are_qualified(tmp_path):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, case="unknown_format")
    try:
        await capture_handler(handler)(action_id, runtime)
        clock[0] = 15_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns,occupancy_state FROM device_activities").fetchone() == (3, 10_000_000_000, 1)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        assert runtime.capture.read_result_completion(action_id, owned) is not None
    finally:
        owned.connection.close()


@pytest.mark.parametrize("baseline", [False, True])
async def test_restart_without_continuous_duration_stops_and_keeps_complete_files(tmp_path, baseline):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, baseline=baseline)
    try:
        await capture_handler(handler)(action_id, runtime)
        runtime.timelapse_deadlines.clear()
        clock[0] = 100_000_000
        await capture_handler(handler)(action_id, runtime)
        row = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert row[0] == 4
        assert json.loads(row[1]) == {"activity_id": "1", "reason": "duration_unknown"}
        assert owned.connection.execute("SELECT activity_state,control_elapsed_ns,completion_basis,occupancy_state FROM device_activities").fetchone() == (3, None, 1, 2)
        assert owned.connection.execute("SELECT count(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert runtime.capture.read_result_completion(action_id, owned) is None
        runtime.driver.control.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_output_finalization_keeps_original_page_and_failed_goal(tmp_path, committed):
    class UnknownFinalization(CaptureRepository):
        def __init__(self):
            self.requests = []

        def finish_result_check(self, finish, confirm, key, owned, **options):
            self.requests.append((finish, confirm, key, options))
            if committed:
                receipt = super().finish_result_check(finish, confirm, key, owned, **options)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原集合确定回执未知"))

    owned, runtime, action_id, handler, results, clock = await world(tmp_path, baseline=True)
    try:
        await capture_handler(handler)(action_id, runtime)
        runtime.timelapse_deadlines.clear()
        repository = UnknownFinalization()
        runtime.capture = repository
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(action_id, runtime)
        assert len(repository.requests) == 2 and repository.requests[0] == repository.requests[1]
        pending, = runtime.pending_start_results.values()
        assert pending.finalized_page is not None
        original = CaptureRepository()
        if committed:
            head = original.read_result_pages(pending.finish.ticket, None, 1, owned).items[0].ref
            for changed in (None, head, replace(pending.finalized_page, page_no=99)):
                rejected = original.finish_result_check(pending.finish, pending.result_set, pending.key,
                    owned, finalized_page=changed)
                assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        runtime.owned, runtime.capture = owned, original
        runtime.wall_us = lambda: 1_750_009_000_000_000
        runtime.check_config = AttemptConfig(1, Decimal("0.01"), Decimal("0"))
        await capture_handler(handler)(action_id, runtime)
        status, details = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?",
            (action_id,)).fetchone()
        assert status == 4 and json.loads(details)["reason"] == "duration_unknown"
        finalized, occupancy, basis = owned.connection.execute(
            "SELECT output_set_finalized_event_id,occupancy_state,completion_basis FROM device_activities").fetchone()
        assert finalized is not None and (occupancy, basis) == (2, 1)
        event_time, transaction_id = owned.connection.execute(
            "SELECT occurred_at,transaction_id FROM history_events WHERE id=?", (finalized,)).fetchone()
        assert event_time == pending.finish.occurred_at
        assert owned.connection.execute("SELECT transaction_id FROM history_events WHERE id=(SELECT result_event_id"
            " FROM operation_attempts WHERE run_id=? AND attempt_no=?)", (pending.finish.ticket.run_id,
            pending.finish.ticket.attempt_id)).fetchone() == (transaction_id,)
        assert results.list_results.await_count == 2
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_output_finalization_refuses_unowned_original_page_members(tmp_path):
    from camctl.contracts.values import new_operation_key
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, baseline=True)
    try:
        await capture_handler(handler)(action_id, runtime)
        listing = await _listing_round(runtime, action_id)
        pending, = runtime.pending_start_results.values()
        last = runtime.capture.read_last_result_page(listing.ticket, owned)
        finish = replace(pending.finish, run_finish=RunFinish(RunOutcome.SUCCEEDED))
        receipt = runtime.capture.finish_result_check(finish, None, new_operation_key(), owned,
            finalized_page=last.ref)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT output_set_finalized_event_id FROM device_activities").fetchone() == (None,)
        assert owned.connection.execute("SELECT status FROM operation_attempts WHERE run_id=?",
            (listing.ticket.run_id,)).fetchone() == (1,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [None, False, True])
async def test_canceled_capture_finalizes_original_closed_result_without_relisting(tmp_path, committed):
    from camctl.capture.handlers import _register_listing, _finish_listing_result
    from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixedCancelSet, FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from camctl.contracts.values import new_operation_key
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, baseline=True,
        consumer="cancel")
    try:
        await capture_handler(handler)(action_id, runtime)
        listing = await _listing_round(runtime, action_id)
        _register_listing(runtime, action_id, listing)
        _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED)
        repository = CancellationRepository()
        cancel_id = action_id + 1
        assert repository.start_cancel_action(StartCancelAction(cancel_id, runtime.wall_us()),
            new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
        receipt = repository.fix_cancel_targets(FixCancelTargets(cancel_id, FixedCancelSet((FixedTarget(
            action_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), runtime.wall_us()), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        item_id, = receipt.value.item_ids
        assert repository.apply_cancel_target(ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP,
            runtime.wall_us()), new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
        if committed is not None:
            class UnknownFinalization(CaptureRepository):
                def __init__(self):
                    self.requests = []

                def finalize_output_set(self, request, key, owned):
                    self.requests.append((request, key))
                    if committed:
                        receipt = super().finalize_output_set(request, key, owned)
                        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
                    return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("保存回执未知"))

            unknown = UnknownFinalization()
            runtime.capture = unknown
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(action_id, runtime)
            pending, = runtime.pending_result_closes.values()
            assert unknown.requests == [(pending.request, pending.key)]
            if committed:
                head = CaptureRepository().read_result_pages(pending.request.page.ticket, None, 1, owned).items[0].ref
                for changed in (replace(pending.request, page=head),
                                replace(pending.request, action_id=cancel_id),
                                replace(pending.request, occurred_at=pending.request.occurred_at + 1)):
                    receipt = CaptureRepository().finalize_output_set(changed, pending.key, owned)
                    assert receipt.kind is DbOutcomeKind.ROLLED_BACK
            path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
            owned.connection.close()
            owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            runtime.owned, runtime.capture = owned, CaptureRepository()
            runtime.wall_us = lambda: 1_750_009_000_000_000
            original_binding_check = runtime.binding_check
            runtime.binding_check = lambda saved: BindingResult(BindingStatus.DEVICE_MISSING, saved, None)
            runtime.resume_result_check_closes(action_id)
            assert runtime.pending_result_closes == {}
            event_time, = owned.connection.execute("SELECT occurred_at FROM history_events"
                " WHERE id=(SELECT output_set_finalized_event_id FROM device_activities)").fetchone()
            assert event_time == pending.request.occurred_at
            runtime.binding_check = original_binding_check
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (6,)
        assert owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (2,)
        assert results.list_results.await_count == 2
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE kind=7").fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_baseline_recording_stops_then_finalizes_scope_and_succeeds(tmp_path):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, baseline=True,
        consumer="record")
    try:
        await capture_handler(handler)(action_id, runtime)
        clock[0] = 65_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?", (action_id,)).fetchone() == (3, None)
        activity = owned.connection.execute("SELECT activity_state,occupancy_state,output_set_finalized_event_id,completion_basis"
            " FROM device_activities").fetchone()
        assert activity[:2] == (3, 2) and activity[2] is not None and activity[3] is None
        assert runtime.capture.read_result_completion(action_id, owned) is None
        assert owned.connection.execute("SELECT count(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        runtime.stopper.stop.assert_awaited_once()
        assert results.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["timelapse", "cancel"])
async def test_unknown_collection_keeps_baseline_scope_after_actual_stop(tmp_path, consumer):
    owned, runtime, action_id, handler, results, clock = await world(tmp_path, baseline=True,
        consumer=consumer)
    original = results.list_results.side_effect
    async def incomplete(request, batch):
        response = await original(request, batch)
        observation, = response.outcome.observations
        return DeviceCallResult.from_outcome(replace(response.outcome, observations=(replace(
            observation, data={**observation.data, "set_finalized": False}),)))
    results.list_results.side_effect = incomplete
    runtime.check_config = AttemptConfig(1, Decimal("1"), Decimal("0"))
    try:
        await capture_handler(handler)(action_id, runtime)
        if consumer == "cancel":
            from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixedCancelSet, FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction
            from camctl.persistence.repositories.cancellation import CancellationRepository
            from camctl.contracts.values import new_operation_key
            repository = CancellationRepository()
            cancel_id = action_id + 1
            receipt = repository.start_cancel_action(StartCancelAction(cancel_id, runtime.wall_us()), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            receipt = repository.fix_cancel_targets(FixCancelTargets(cancel_id, FixedCancelSet((FixedTarget(
                action_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), runtime.wall_us()), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            item_id, = receipt.value.item_ids
            receipt = repository.apply_cancel_target(ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP,
                runtime.wall_us()), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        else:
            runtime.timelapse_deadlines.clear()
        for _ in range(3):
            await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (
            6 if consumer == "cancel" else 4,)
        assert owned.connection.execute("SELECT activity_state,occupancy_state,output_set_finalized_event_id"
            " FROM device_activities").fetchone() == (3, 1, None)
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["timelapse", "record"])
async def test_stopped_finalized_scope_has_identical_forward_reverse_and_snapshot_history(tmp_path, consumer):
    from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
    from camctl.history.decoding import decode_event_row
    from camctl.history.events import branch_of, business_columns, event_type_name
    from camctl.history.replay import EntityImage, RestoreSeed, restore
    from camctl.history.snapshots import SnapshotRef, decode_snapshot, prepare_snapshot
    from camctl.history.validators import ValidatedEvent
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.repositories.history import SqliteSnapshotStore

    owned, runtime, action_id, handler, results, clock = await world(tmp_path, baseline=True,
        consumer=consumer)
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    store = SqliteSnapshotStore(path, executor)
    ref = SnapshotRef(1, action_id)
    try:
        await capture_handler(handler)(action_id, runtime)
        start_image, = await store.load_entity_images((ref,))
        await store.save_snapshots((prepare_snapshot(start_image),))
        if consumer == "record":
            clock[0] = 65_000_000_000
        else:
            runtime.timelapse_deadlines.clear()
        await capture_handler(handler)(action_id, runtime)
        end_image, = await store.load_entity_images((ref,))
        finalized, = owned.connection.execute("SELECT output_set_finalized_event_id FROM device_activities"
            " WHERE action_id=?", (action_id,)).fetchone()
        boundary = HistoryBoundary(*owned.connection.execute(
            "SELECT t.id,t.last_event_id FROM history_events h JOIN history_transactions t"
            " ON t.id=h.transaction_id WHERE h.id=?", (finalized,)).fetchone())
        assert start_image.boundary.last_event_id < boundary.last_event_id < end_image.boundary.last_event_id
        content, = owned.connection.execute("SELECT content FROM entity_snapshots WHERE entity_type=1"
            " AND entity_id=? AND boundary_event_id=?", (action_id, start_image.boundary.last_event_id)).fetchone()
        header, snapshot_rows = decode_snapshot(content, expect_ref=ref)

        def seed_image(records, count):
            full = {(row.table, row.row_id): dict(row.values) for row in records}
            business = {key: {name: value for name, value in values.items()
                if name in business_columns(key[0])} for key, values in full.items()}
            return EntityImage(1, action_id, True, business,
                full[("actions", action_id)]["last_event_id"], count)

        members = {(row.table, row.row_id) for row in end_image.rows}
        events = []
        for raw in owned.connection.execute(
            "SELECT h.id,h.transaction_id,h.event_type,h.event_version,h.occurred_at,h.clock_status,h.change_seq,h.body_json"
            " FROM history_events h JOIN entity_event_links l ON l.event_id=h.id"
            " WHERE l.entity_type=1 AND l.entity_id=? ORDER BY h.id", (action_id,)):
            event = decode_event_row(raw)
            owners = {(row.table, row.row_id): (1, action_id) for row in event.rows
                if (row.table, row.row_id) in members}
            events.append(ValidatedEvent(event, event_type_name(event.event_type),
                branch_of(event.event_type, event.reason)[0], ((1, action_id),), owners))
        forward = restore(RestoreSeed(EntityImage(1, action_id, False, {}, 0, 0), INITIAL_BOUNDARY),
            events, boundary)
        snapshot = restore(RestoreSeed(seed_image(snapshot_rows, header["change_count"]), start_image.boundary),
            events, boundary)
        reverse = restore(RestoreSeed(seed_image(end_image.rows, end_image.change_count), end_image.boundary),
            events, boundary)
        assert forward.rows == snapshot.rows == reverse.rows
        activity = forward.rows[("device_activities", 1)]
        assert activity["activity_state"] == 3 and activity["output_set_finalized_event_id"] == finalized
        assert activity["completion_evidence_json"] is None
        assert activity["control_elapsed_ns"] is None
        assert activity["occupancy_state"] == 1
        original_stop, = owned.connection.execute("SELECT a.result_event_id FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?",
            (f"stop/{action_id}",)).fetchone()
        assert any(values["result_event_id"] == original_stop for (table, _), values in forward.rows.items()
            if table == "operation_attempts")
    finally:
        await executor.close()
        owned.connection.close()
