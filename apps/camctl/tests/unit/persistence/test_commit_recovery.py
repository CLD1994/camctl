"""P4 提交未知身份核实的单元测试。

期望独立来自事务恢复边界：五个核实分区各有唯一决策，未知和错
误不解释为空或可重做；决策本身不产生副作用。
"""

from __future__ import annotations

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.recovery import (
    CommitFinding,
    CommitFindingKind,
    OperationIdentity,
    RecoveryError,
    RecoveryKind,
    resolve_commit,
)

IDENTITY = OperationIdentity(inputs={"request_id": 7}, stage="accept", target="plans")
KEY = new_operation_key()


class FixedLookup:
    def __init__(self, finding: CommitFinding) -> None:
        self.finding = finding
        self.calls = 0

    def lookup(self, key):
        self.calls += 1
        return self.finding


class TestPartitions:
    def test_present_and_consistent_reuses_original_result(self) -> None:
        lookup = FixedLookup(
            CommitFinding(
                kind=CommitFindingKind.PRESENT,
                value={"plan_id": 3},
                identity=OperationIdentity(
                    inputs={"request_id": 7}, stage="accept", target="plans"
                ),
            )
        )
        decision = resolve_commit(KEY, IDENTITY, lookup)
        assert decision.kind is RecoveryKind.REUSE
        assert decision.value == {"plan_id": 3}
        assert decision.can_retry is False

    def test_present_with_inconsistent_inputs_rejected(self) -> None:
        lookup = FixedLookup(
            CommitFinding(
                kind=CommitFindingKind.PRESENT,
                value={"plan_id": 3},
                identity=OperationIdentity(
                    inputs={"request_id": 9}, stage="accept", target="plans"
                ),
            )
        )
        decision = resolve_commit(KEY, IDENTITY, lookup)
        assert decision.kind is RecoveryKind.ERROR
        assert "不一致" in str(decision.error)

    def test_present_with_inconsistent_stage_rejected(self) -> None:
        lookup = FixedLookup(
            CommitFinding(
                kind=CommitFindingKind.PRESENT,
                value=None,
                identity=OperationIdentity(
                    inputs={"request_id": 7}, stage="start", target="plans"
                ),
            )
        )
        decision = resolve_commit(KEY, IDENTITY, lookup)
        assert decision.kind is RecoveryKind.ERROR

    def test_inflight_unknown_cannot_retry(self) -> None:
        # 查询暂时不存在，但原线程仍可能提交：不能按原资格重做。
        lookup = FixedLookup(CommitFinding(kind=CommitFindingKind.IN_FLIGHT))
        decision = resolve_commit(KEY, IDENTITY, lookup)
        assert decision.can_retry is False
        assert decision.kind is RecoveryKind.WAIT

    def test_absent_and_cannot_arrive_late_allows_eligibility_check(self) -> None:
        lookup = FixedLookup(CommitFinding(kind=CommitFindingKind.ABSENT))
        decision = resolve_commit(KEY, IDENTITY, lookup)
        assert decision.kind is RecoveryKind.RETRY
        assert decision.can_retry is True
        assert decision.value is None

    def test_lookup_failure_preserves_unknown(self) -> None:
        error = RuntimeError("database is locked")
        lookup = FixedLookup(
            CommitFinding(kind=CommitFindingKind.LOOKUP_FAILED, error=error)
        )
        decision = resolve_commit(KEY, IDENTITY, lookup)
        assert decision.kind is RecoveryKind.WAIT
        assert decision.error is error
        assert decision.can_retry is False

    def test_present_without_identity_is_rejected_as_incomplete(self) -> None:
        lookup = FixedLookup(CommitFinding(kind=CommitFindingKind.PRESENT, value={"x": 1}))
        with pytest.raises(RecoveryError, match="缺少"):
            resolve_commit(KEY, IDENTITY, lookup)

    def test_resolve_has_no_side_effects_or_repeat_invocation(self) -> None:
        lookup = FixedLookup(CommitFinding(kind=CommitFindingKind.IN_FLIGHT))
        resolve_commit(KEY, IDENTITY, lookup)
        resolve_commit(KEY, IDENTITY, lookup)
        # 决策是纯核实：两次调用各查询一次，不自动重做原操作。
        assert lookup.calls == 2
