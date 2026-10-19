"""截止提示只安排重查，不能刷新计时或反复提前唤醒。"""

import pytest

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
