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


@pytest.mark.asyncio
@pytest.mark.parametrize("has_pending", [False, True])
async def test_recording_wait_only_skips_completion_notice_without_original_saves(mocker, has_pending):
    from camctl.capture.handlers import _record_handler
    from camctl.capture.recording import RecordingState

    action = {"id": 1, "status": 2, "cancel_requested": 0, "execution_spec_json": {}}
    context = SimpleNamespace(
        resume_result_check_closes=AsyncMock(), resume_capture_completions=AsyncMock(),
        resume_baselines=AsyncMock(), resume_file_observations=AsyncMock(), resume_start_results=AsyncMock(),
        action=AsyncMock(return_value=action), last_attempt=AsyncMock(return_value=(3, 3)),
        on_recording_deadline=Mock(),
        recording_state=SimpleNamespace(recording_state=AsyncMock(return_value=RecordingState(
            action_terminal=False, started_confirmed=True, anchor_from_current_session=True,
            stop_target_ns=100, monotonic_now_ns=50, stop_confirmed=False,
            file_complete_guaranteed=False, stop_attempts_used=0, stop_max_attempts=3, stop_in_flight=False))),
        pending_result_closes={}, pending_capture_completions={}, pending_baselines={},
        pending_file_observations={}, pending_start_results={}, pending_segment_writes={},
        pending_read_business={}, pending_read_results={}, pending_read_ends={})
    if has_pending:
        context.pending_start_results[(1, 1)] = object()
    mocker.patch("camctl.capture.handlers._handle_binding_failure", autospec=True, return_value=False)
    mocker.patch("camctl.capture.handlers._advance_record_start", autospec=True, return_value=False)

    result = await _record_handler(1, context)

    assert result.wakeup_required is has_pending
    context.on_recording_deadline.assert_called_once_with(1, 100)
