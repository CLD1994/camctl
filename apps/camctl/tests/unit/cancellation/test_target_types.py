"""N5 目标类型取消规则的单元测试：同步责任分类。"""

from __future__ import annotations

from camctl.reporting.ack import decide_sync_cancel


class TestDecideSyncCancel:
    def test_pending_and_running_report_actions_stop(self):
        changes = decide_sync_cancel({
            "cancelled_sync_ids": (7, 3),
            "report_actions": {11: "pending", 12: "running"},
        })
        assert changes.stopped_action_ids == (11, 12)
        assert changes.preserved_action_ids == ()
        assert changes.ended_sync_ids == (3, 7)

    def test_terminal_report_actions_preserve_results(self):
        changes = decide_sync_cancel({
            "report_actions": {11: "succeeded", 12: "failed"},
        })
        assert changes.stopped_action_ids == ()
        assert changes.preserved_action_ids == (11, 12)

    def test_shared_generation_stays_out_of_target_scope(self):
        """共享的报告生成不是目标动作，不在停止或保持范围内。"""
        changes = decide_sync_cancel({
            "cancelled_sync_ids": (),
            "report_actions": {11: "running"},
        })
        assert changes.stopped_action_ids == (11,)
        assert changes.ended_sync_ids == ()
