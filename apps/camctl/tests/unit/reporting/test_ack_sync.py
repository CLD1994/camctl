"""R6 累计确认与同步规则的单元测试。

期望独立来自 ACK 与业务变化规则：水位单调累计，同水位 ACK 可结
束合格同步；较旧报告 ID 不代表旧水位；未知报告与读取失败分别
分类；同步取消不停止共享生成。
"""

from __future__ import annotations

import pytest

from camctl.contracts.values import ConsistencyError

from camctl.reporting.ack import (
    AckDisposition,
    AckFacts,
    AckInput,
    AckReport,
    SyncResponsibility,
    decide_ack,
    decide_sync_cancel,
    qualifies_sync,
)

def _report(report_id: int, to_wm: int, *, from_wm=0, frozen=20):
    return AckReport(report_id, from_wm, to_wm, frozen)


def _facts(
    *,
    acknowledged_wm: int = 0,
    acknowledged_report_id: int | None = None,
    report=None,
    read_failed: bool = False,
) -> AckFacts:
    return AckFacts(
        acknowledged_wm=acknowledged_wm,
        acknowledged_report_id=acknowledged_report_id,
        report=report,
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
            AckInput(report_id=404), _facts(report=None)
        )
        assert decision.disposition is AckDisposition.INVALID

    def test_read_failure_is_error_not_invalid_or_missing(self) -> None:
        decision = decide_ack(AckInput(report_id=1), _facts(read_failed=True))
        assert decision.disposition is AckDisposition.READ_ERROR


class TestSyncQualification:
    def test_report_before_sync_start_does_not_qualify(self) -> None:
        report = _report(5, 800, frozen=4)
        sync = SyncResponsibility(1, 8, 100, 5)
        assert qualifies_sync(report, sync) is False

    @pytest.mark.parametrize("frozen,from_wm,want", [
        (19, 101, False), (19, 100, False), (20, 101, False),
        (20, 100, True), (21, 99, True),
    ])
    def test_history_and_origin_are_independent(self, frozen, from_wm, want) -> None:
        report = _report(5, 300, from_wm=from_wm, frozen=frozen)
        sync = SyncResponsibility(1, 8, 100, 20)
        assert qualifies_sync(report, sync) is want

    def test_equal_watermark_ack_can_end_sync(self) -> None:
        # 水位等于当前：不推进累计身份，但满足完整同步即结束责任。
        report = _report(6, 800)
        sync = SyncResponsibility(1, 8, 0, 20)
        assert qualifies_sync(report, sync) is True


@pytest.mark.parametrize("field,bad", [
    ("from_wm", True), ("from_wm", "0"), ("to_wm", 1.5),
    ("to_wm", -1), ("to_wm", 2**53), ("frozen_event_id", False),
    ("frozen_event_id", "20"), ("frozen_event_id", -1),
    ("frozen_event_id", 2**63),
])
def test_report_basis_rejects_unreliable_fields(field, bad):
    values = {"report_id": 1, "from_wm": 0, "to_wm": 300, "frozen_event_id": 20}
    values[field] = bad
    with pytest.raises(ConsistencyError):
        AckReport(**values)


def test_report_basis_rejects_inverted_range():
    with pytest.raises(ConsistencyError):
        AckReport(1, 301, 300, 20)


@pytest.mark.parametrize("field,bad", [
    ("acknowledged_wm", True), ("acknowledged_wm", "0"),
    ("acknowledged_wm", -1), ("acknowledged_wm", 2**53),
    ("read_failed", 1), ("report", {"to_wm": 20}),
])
def test_ack_facts_reject_unreliable_fields(field, bad):
    values = {"acknowledged_wm": 0, "acknowledged_report_id": None}
    values[field] = bad
    with pytest.raises(ConsistencyError):
        AckFacts(**values)


def test_nonzero_ack_watermark_requires_identity():
    with pytest.raises(ConsistencyError):
        AckFacts(1, None)


def test_ack_report_identity_must_match_lookup():
    with pytest.raises(ConsistencyError):
        decide_ack(AckInput(1), _facts(report=_report(2, 300)))


def test_large_watermarks_remain_exact():
    decision = decide_ack(AckInput(5), _facts(report=_report(5, 2**53 - 1)))
    assert decision.new_acknowledged_wm == 9007199254740991


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
