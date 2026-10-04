"""取消生效进度与汇总规则的单元测试。

覆盖《取消动作的完成时机》：保存取消标记只表示生效到执行控制，停
止仍在途时保持 running；全部有限处理结束且无拒绝/失败才成功；任一
失败按机器错误 cancel_items_failed 汇总，具体原因在逐项结果。
"""

from __future__ import annotations

import pytest

from camctl.cancellation.models import (
    CancelItemProgress,
    CancelProgress,
    CancellationStatus,
)
from camctl.cancellation.rules import summarize_cancel


def _item(item_id, status, *, outcome=None, error_code=None, details=None):
    return CancelItemProgress(
        item_id=item_id, target_action_id=item_id + 10, status=status,
        outcome=outcome, error_code=error_code, error_details_json=details)


class TestSummarizeCancel:
    def test_cancel_mark_is_not_settlement(self):
        """标记已保存（效果 APPLIED、项进入处理中）但停止仍在途：

        汇总保持 running，不能仅凭取消标记提前成功。
        """
        progress = CancelProgress(items=(
            _item(91, 2, outcome=None),))
        assert summarize_cancel(progress).status \
            is CancellationStatus.RUNNING

    def test_any_in_flight_item_keeps_running(self):
        progress = CancelProgress(items=(
            _item(91, 3, outcome=1), _item(92, 2), _item(93, 4, error_code=1)))
        assert summarize_cancel(progress).status \
            is CancellationStatus.RUNNING

    def test_all_succeeded_without_rejection_succeeds(self):
        progress = CancelProgress(items=(
            _item(91, 3, outcome=1), _item(92, 3, outcome=2)))
        result = summarize_cancel(progress)
        assert result.status is CancellationStatus.SUCCEEDED
        assert result.succeeded == 2 and result.failed == 0

    def test_any_failure_fails_with_registered_machine_error(self):
        progress = CancelProgress(items=(
            _item(91, 3, outcome=1),
            _item(92, 4, error_code=1,
                  details={"action_instance_id": "12"})))
        result = summarize_cancel(progress)
        assert result.status is CancellationStatus.FAILED
        assert result.succeeded == 1 and result.failed == 1
        assert result.error == {
            "code": "cancel_items_failed", "stage": "execution", "details": {}}

    def test_canceled_item_is_not_interpreted_by_plain_summary(self):
        """项 CANCELED 属取消发起者收场语义（N4），普通汇总拒绝解释。"""
        progress = CancelProgress(items=(_item(91, 5),))
        with pytest.raises(ValueError):
            summarize_cancel(progress)

    def test_empty_progress_is_rejected(self):
        with pytest.raises(ValueError):
            summarize_cancel(CancelProgress(items=()))
