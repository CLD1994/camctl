"""R6 累计确认与同步规则的单元测试。

期望独立来自 ACK 与业务变化规则：水位单调累计，同水位 ACK 可结
束合格同步；较旧报告 ID 不代表旧水位；未知报告与读取失败分别
分类；同步取消不停止共享生成。
"""

from __future__ import annotations

import pytest

from camctl.reporting.ack import (
    AckDisposition,
    AckFacts,
    AckInput,
    SyncResponsibility,
    decide_ack,
    decide_sync_cancel,
    qualifies_sync,
)

_REPORT = type("Report", (), {})  # FrozenReport 替身由字段构造


def _report(report_id: int, to_wm: int):
    return {"report_id": report_id, "from_wm": 0, "to_wm": to_wm}


def _facts(
    *,
    acknowledged_wm: int = 0,
    acknowledged_report_id: int | None = None,
    report=None,
    report_known: bool = True,
    read_failed: bool = False,
) -> AckFacts:
    return AckFacts(
        acknowledged_wm=acknowledged_wm,
        acknowledged_report_id=acknowledged_report_id,
        report=report,
        report_known=report_known,
        read_failed=read_failed,
    )


class TestDecideAck:
    def test_valid_ack_advances_watermark(self) -> None:
        decision = decide_ack(
            AckInput(report_id=5), _facts(report=_report(5, 800))
        )
        assert decision.disposition is AckDisposition.ABSORBED
        assert decision.new_acknowledged_wm == 800
        assert decision.new_acknowledged_report_id == 5

    def test_equal_watermark_still_absorbs_identity_rule(self) -> None:
        # 水位不推进：有效但不覆盖累计报告身份。
        decision = decide_ack(
            AckInput(report_id=6),
            _facts(
                acknowledged_wm=800, acknowledged_report_id=5, report=_report(6, 800)
            ),
        )
        assert decision.disposition is AckDisposition.VALID_NOT_ADVANCING
        assert decision.new_acknowledged_wm == 800
        assert decision.new_acknowledged_report_id == 5

    def test_older_report_id_may_carry_newer_watermark(self) -> None:
        # 较旧报告 ID 不代表旧水位：以保存的覆盖水位判断。
        decision = decide_ack(
            AckInput(report_id=2),
            _facts(
                acknowledged_wm=800, acknowledged_report_id=9, report=_report(2, 900)
            ),
        )
        assert decision.disposition is AckDisposition.ABSORBED
        assert decision.new_acknowledged_report_id == 2

    def test_unknown_report_is_invalid(self) -> None:
        decision = decide_ack(
            AckInput(report_id=404), _facts(report_known=False)
        )
        assert decision.disposition is AckDisposition.INVALID

    def test_read_failure_is_error_not_invalid_or_missing(self) -> None:
        decision = decide_ack(AckInput(report_id=1), _facts(read_failed=True))
        assert decision.disposition is AckDisposition.READ_ERROR


class TestSyncQualification:
    def test_report_covering_sync_range_qualifies(self) -> None:
        report = _report(5, 800)
        sync = SyncResponsibility(sync_id=1, action_id=8, from_wm=100, to_wm=700)
        assert qualifies_sync(report, sync) is True

    def test_partial_coverage_does_not_qualify(self) -> None:
        report = _report(5, 300)
        sync = SyncResponsibility(sync_id=1, action_id=8, from_wm=100, to_wm=700)
        assert qualifies_sync(report, sync) is False

    def test_equal_watermark_ack_can_end_sync(self) -> None:
        # 水位等于当前：不推进累计身份，但满足完整同步即结束责任。
        report = _report(6, 800)
        sync = SyncResponsibility(sync_id=1, action_id=8, from_wm=0, to_wm=800)
        assert qualifies_sync(report, sync) is True


class TestSyncCancel:
    def test_cancel_does_not_stop_shared_generation(self) -> None:
        # 取消同步等待：报告动作与共享生成不受影响。
        changes = decide_sync_cancel(
            {
                "cancelled_sync_ids": (1, 2),
                "shared_report_action_ids": (8,),
            }
        )
        assert changes.ended_sync_ids == (1, 2)
        assert changes.stopped_action_ids == ()

    def test_report_action_states_map_to_cancel_scope(self) -> None:
        # 未执行的报告动作可取消；已开始与已成功的不改结果。
        changes = decide_sync_cancel(
            {
                "cancelled_sync_ids": (),
                "report_actions": {
                    10: "pending",
                    11: "running",
                    12: "succeeded",
                },
            }
        )
        assert changes.stopped_action_ids == (10,)
        assert changes.preserved_action_ids == (11, 12)
