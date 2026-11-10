"""检查资格使用拍摄允许时段和已排序的原候选。"""
from decimal import Decimal
from types import SimpleNamespace

from camctl.capture.selection import earlier_capture_preempts, within_prepare_window


def test_prepare_window_includes_both_boundaries():
    action = {'scheduled_at': 10_000_000, 'max_delay_ms': 1000}
    assert within_prepare_window(action, Decimal('2'), 8_000_000)
    assert within_prepare_window(action, Decimal('2'), 11_000_000)
    assert not within_prepare_window(action, Decimal('2'), 7_999_999)
    assert not within_prepare_window(action, Decimal('2'), 11_000_001)


def test_read_yields_to_other_capture_but_capture_only_to_earlier_one():
    action = {'id': 2, 'scheduled_at': 100, 'plan_id': 2, 'input_index': 0}
    later = SimpleNamespace(action_id=3, scheduled_at=101, plan_id=1, input_index=0)
    earlier = SimpleNamespace(action_id=3, scheduled_at=100, plan_id=1, input_index=0)
    assert earlier_capture_preempts(later, action, for_read=True)
    assert not earlier_capture_preempts(later, action, for_read=False)
    assert earlier_capture_preempts(earlier, action, for_read=False)
    assert not earlier_capture_preempts(None, action, for_read=True)
