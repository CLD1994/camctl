"""截止提示只安排重查，不能刷新计时或反复提前唤醒。"""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from camctl.capture.wakeups import RecordingWakeups


@pytest.mark.parametrize("margin", [0, 0.05])
def test_deadline_is_consumed_once_and_early_wakeup_does_not_spin(margin):
    wakeups = RecordingWakeups()
    wakeups.schedule("camera", 12, 18_000_000_000, now_ns=7_000_000_000, now_s=100)
    assert wakeups.next_at(100, margin) == 111 - margin
    if margin:
        assert wakeups.next_at(110.95, margin) == 111
        # 另一次推进仍使用原截止，不再回到提前唤醒点。
        wakeups.schedule("camera", 12, 18_000_000_000, now_ns=17_970_000_000, now_s=110.97)
        assert wakeups.next_at(110.97, margin) == 111
    assert wakeups.next_at(111, margin) == 111
    assert wakeups.next_at(111, margin) is None


def test_save_delay_does_not_extend_original_deadline():
    wakeups = RecordingWakeups()
    wakeups.schedule("camera", 12, 18_000_000_000, now_ns=12_000_000_000, now_s=105)
    assert wakeups.next_at(105, 0) == 111


def test_overdue_deadline_requests_one_recheck():
    wakeups = RecordingWakeups()
    wakeups.schedule("camera", 12, 18_000_000_000, now_ns=20_000_000_000, now_s=113)
    assert wakeups.next_at(113, 0.05) == 113
    assert wakeups.next_at(113, 0.05) is None


def test_old_action_cannot_clear_new_action_on_same_device():
    wakeups = RecordingWakeups()
    wakeups.schedule("camera", 12, 18_000_000_000, now_ns=7_000_000_000, now_s=100)
    wakeups.schedule("camera", 13, 28_000_000_000, now_ns=7_000_000_000, now_s=100)
    wakeups.discard("camera", 12)
    assert wakeups.next_at(100, 0) == 121
    wakeups.discard("camera", 13)
    assert wakeups.next_at(100, 0) is None


def test_devices_keep_independent_deadlines_and_shutdown_clears_hints():
    wakeups = RecordingWakeups()
    wakeups.schedule("first", 12, 18_000_000_000, now_ns=7_000_000_000, now_s=100)
    wakeups.schedule("second", 13, 16_000_000_000, now_ns=7_000_000_000, now_s=100)
    assert wakeups.next_at(100, 0) == 109
    wakeups.discard("second", 13)
    assert wakeups.next_at(100, 0) == 111
    wakeups.clear()
    assert wakeups.next_at(100, 0) is None


def _waiting_context(mocker):
    from camctl.capture.recording import RecordingState

    action = {"id": 1, "status": 2, "cancel_requested": 0, "execution_spec_json": {}}
    context = SimpleNamespace(
        resume_result_check_closes=AsyncMock(), resume_capture_completions=AsyncMock(),
        resume_baselines=AsyncMock(), resume_file_observations=AsyncMock(),
        resume_start_results=AsyncMock(return_value=False),
        action=AsyncMock(return_value=action), last_attempt=AsyncMock(return_value=(3, 3)),
        on_recording_deadline=Mock(),
        recording_state=SimpleNamespace(recording_state=AsyncMock(return_value=RecordingState(
            action_terminal=False, started_confirmed=True, anchor_from_current_session=True,
            stop_target_ns=100, monotonic_now_ns=50, stop_confirmed=False,
            file_complete_guaranteed=False, stop_attempts_used=0, stop_max_attempts=3, stop_in_flight=False))),
        pending_result_closes={}, pending_capture_completions={}, pending_baselines={},
        pending_file_observations={}, pending_start_results={}, pending_segment_writes={},
        pending_read_business={}, pending_read_results={}, pending_read_ends={})
    mocker.patch("camctl.capture.handlers._handle_binding_failure", autospec=True, return_value=False)
    mocker.patch("camctl.capture.handlers._advance_record_start", autospec=True, return_value=False)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("has_pending", [False, True])
async def test_recording_wait_only_skips_completion_notice_without_original_saves(mocker, has_pending):
    from camctl.capture.handlers import _record_handler

    context = _waiting_context(mocker)
    if has_pending:
        context.pending_capture_completions[1] = object()

    result = await _record_handler(1, context)

    assert result.wakeup_required is has_pending
    context.on_recording_deadline.assert_called_once_with(1, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", [
    "pending_result_closes", "pending_capture_completions", "pending_baselines",
    "pending_segment_writes", "pending_file_observations",
])
@pytest.mark.parametrize("owner", [1, 2])
async def test_recording_wait_uses_only_its_original_action_responsibilities(mocker, mapping, owner):
    from camctl.capture.handlers import _record_handler

    context = _waiting_context(mocker)
    key = (owner, "file") if mapping == "pending_file_observations" else owner
    getattr(context, mapping)[key] = SimpleNamespace(in_call=True)

    result = await _record_handler(1, context)

    assert result.wakeup_required is (owner == 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [1, 2])
@pytest.mark.parametrize("consumed", [False, True])
async def test_resume_start_results_reports_only_consumed_results_for_requested_action(owner, consumed):
    from camctl.capture.handlers import CaptureRuntime

    ticket = object()
    pending = SimpleNamespace(result_disposition_ready=True, finish=SimpleNamespace(ticket=ticket))
    context = SimpleNamespace(pending_start_results={(7, 1): pending},
        action_id_of_ticket=AsyncMock(return_value=owner))

    async def save(identity, original):
        assert original is pending
        if consumed:
            del context.pending_start_results[identity]

    context._save_call_result = AsyncMock(side_effect=save)
    assert await CaptureRuntime.resume_start_results(context, 1) is (owner == 1 and consumed)
    assert context._save_call_result.await_count == (1 if owner == 1 else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [1, 2])
async def test_recording_wait_preserves_real_start_result_consumption(mocker, owner):
    from camctl.capture.handlers import CaptureRuntime, _record_handler

    context = _waiting_context(mocker)
    pending = SimpleNamespace(result_disposition_ready=True, finish=SimpleNamespace(ticket=object()))
    context.pending_start_results[(7, 1)] = pending
    context.action_id_of_ticket = AsyncMock(return_value=owner)

    async def save(identity, original):
        del context.pending_start_results[identity]

    async def resume(action_id):
        return await CaptureRuntime.resume_start_results(context, action_id)

    context._save_call_result = AsyncMock(side_effect=save)
    context.resume_start_results.side_effect = resume
    result = await _record_handler(1, context)
    assert result.wakeup_required is (owner == 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", ["pending_read_business", "pending_read_results", "pending_read_ends"])
@pytest.mark.parametrize("change", ["unchanged", "consumed", "replaced"])
async def test_internal_read_wakes_only_after_original_pending_changes(mocker, mapping, change):
    from camctl.capture.handlers import _resume_internal_read_results

    context = SimpleNamespace(database=object(), operations=object(), wall_us=lambda: 150,
        pending_read_business={}, pending_read_results={}, pending_read_ends={}, continuing_read_tickets={})
    original = object()
    pending = getattr(context, mapping)
    pending[7] = original

    async def advance(*args):
        if change == "consumed":
            pending.pop(7, None)
        elif change == "replaced":
            pending[7] = object()

    mocker.patch("camctl.outputs.read_attempts.retry_read_business", autospec=True,
                 side_effect=advance if mapping == "pending_read_business" else None)
    mocker.patch("camctl.capture.media_flow._save_internal_read_and_settle", autospec=True,
                 side_effect=advance if mapping == "pending_read_results" else None)
    mocker.patch("camctl.outputs.read_attempts.retry_read_ends", autospec=True,
                 side_effect=advance if mapping == "pending_read_ends" else None)

    assert await _resume_internal_read_results(context) is (change != "unchanged")


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
async def test_recording_wait_keeps_global_read_progress_without_waking_for_observation(mocker, consumed):
    from camctl.capture.handlers import _record_handler

    context = _waiting_context(mocker)
    context.database, context.operations = object(), object()
    context.wall_us = lambda: 150
    context.continuing_read_tickets = {}
    context.pending_read_business[(7, "release_read_slot")] = object()

    async def advance(scope):
        if consumed:
            scope.pending_read_business.clear()

    mocker.patch("camctl.outputs.read_attempts.retry_read_business", autospec=True, side_effect=advance)
    mocker.patch("camctl.outputs.read_attempts.retry_read_ends", autospec=True)
    result = await _record_handler(1, context)
    assert result.wakeup_required is consumed


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["absent", "same", "other_ticket"])
async def test_internal_read_recopy_qualification_wakes_once_without_time_refresh_loop(prior):
    from camctl.capture.handlers import _resume_internal_read_results
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.capture.media_flow import _finish_internal_read_result
    from camctl.operations.models import AttemptTicket
    from camctl.outputs.copy import SourceChecksumSupport
    from camctl.outputs.read_attempts import HeldReadEnd, ReadContinuation
    from camctl.persistence.business import BusinessDatabase
    from unittest.mock import create_autospec

    ticket = AttemptTicket(1, "read", "7", "read/7", 20)
    facts = SimpleNamespace(verification_state=1, source_support=SourceChecksumSupport.UNSUPPORTED,
        source_sha256=None, target_sha256=None, committed_bytes=8, source_size=8)
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.side_effect = [facts, (ticket, 1)] * 2
    clock = [100]
    context = SimpleNamespace(database=database, operations=object(), evidence=object(),
        wall_us=lambda: clock[0], occurred_at=lambda: clock[0], pending_read_business={},
        pending_read_results={}, pending_read_ends={}, continuing_read_tickets={})
    step = InputStep(InputPhase.RECOPY_PENDING, copy_id=7)

    async def resume(scope, held):
        await _finish_internal_read_result(context, held.ticket, step, max_attempts=None)

    held = HeldReadEnd(ticket, object(), 100, resume=resume)
    context.pending_read_ends[7] = held
    if prior != "absent":
        earlier = ticket if prior == "same" else AttemptTicket(1, "read", "7", "read/7", 19)
        context.continuing_read_tickets[7] = ReadContinuation(
            earlier, False, step, 99, evidence=context.evidence)

    assert await _resume_internal_read_results(context) is (prior != "same")
    assert context.pending_read_ends[7] is held
    assert context.continuing_read_tickets[7].ticket == ticket
    clock[0] = 101
    assert await _resume_internal_read_results(context) is False
    database.write.assert_not_awaited()
