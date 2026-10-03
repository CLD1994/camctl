"""完整性判定与有限重拷的纯规则测试。

验证[读取正确性与文件校验](../../../../architecture/file-copy.md#读取正确性与文件校验)
的五分区及[摘要不一致后的有限重拷](../../../../architecture/file-copy.md#摘要不一致后的有限重拷)
的预算规则：读取额与重拷额独立，决定只依赖重拷次数与本次上限。
"""

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.copy import (
    IntegrityDecision, IntegrityFacts, IntegrityOutcome, PreparedRequest,
    RecopyPlan, RecopyRegistration, SourceChecksumSupport, VerificationSave,
    decide_integrity,
)

_DIGEST = "a" * 64
_OTHER = "b" * 64


def _facts(**overrides) -> IntegrityFacts:
    """完整读取后的默认事实：支持源摘要且两者一致。"""
    values = dict(
        source_size=10, committed_bytes=10,
        source_support=SourceChecksumSupport.SUPPORTED,
        source_sha256=_DIGEST, target_sha256=_DIGEST,
        owner_running=True, recopies_used=0, max_recopies=1,
    )
    values.update(overrides)
    return IntegrityFacts(**values)


# ---- 五分区 ----


def test_decide_integrity_matched_when_digests_equal():
    decision = decide_integrity(_facts())
    assert decision == IntegrityDecision(outcome=IntegrityOutcome.MATCHED)


def test_decide_integrity_mismatched_registers_recopy():
    """读取额与重拷额独立：不一致时只看重拷次数，不看读取尝试次数。"""
    decision = decide_integrity(_facts(source_sha256=_OTHER))
    assert decision.outcome is IntegrityOutcome.MISMATCHED
    assert decision.recopy is RecopyPlan.REGISTER


def test_decide_integrity_source_unavailable_when_unsupported():
    decision = decide_integrity(
        _facts(source_support=SourceChecksumSupport.UNSUPPORTED, source_sha256=None))
    assert decision.outcome is IntegrityOutcome.SOURCE_UNAVAILABLE
    assert decision.recopy is None


def test_decide_integrity_failed_when_source_query_failed():
    decision = decide_integrity(_facts(source_sha256=None, source_error="query failed"))
    assert decision.outcome is IntegrityOutcome.VERIFICATION_FAILED
    assert decision.error == "query failed"


def test_decide_integrity_capability_undetermined_blocks_decision():
    decision = decide_integrity(
        _facts(source_support=SourceChecksumSupport.UNDETERMINED, source_sha256=None))
    assert decision.outcome is IntegrityOutcome.CAPABILITY_UNDETERMINED
    assert decision.recopy is None


def test_decide_integrity_failed_when_host_digest_missing():
    decision = decide_integrity(_facts(target_sha256=None, target_error="read failed"))
    assert decision.outcome is IntegrityOutcome.VERIFICATION_FAILED
    assert decision.error == "read failed"


# ---- 重拷子分区 ----


def test_mismatched_with_exhausted_recopies_reports_exhausted():
    decision = decide_integrity(_facts(source_sha256=_OTHER, recopies_used=1))
    assert decision.outcome is IntegrityOutcome.MISMATCHED
    assert decision.recopy is RecopyPlan.EXHAUSTED


def test_mismatched_without_owner_does_not_start_recopy():
    decision = decide_integrity(_facts(source_sha256=_OTHER, owner_running=False))
    assert decision.outcome is IntegrityOutcome.MISMATCHED
    assert decision.recopy is RecopyPlan.OWNER_NOT_ELIGIBLE


def test_owner_takes_precedence_over_exhausted():
    decision = decide_integrity(
        _facts(source_sha256=_OTHER, owner_running=False, recopies_used=1))
    assert decision.recopy is RecopyPlan.OWNER_NOT_ELIGIBLE


def test_zero_recopy_budget_exhausts_on_first_mismatch():
    decision = decide_integrity(_facts(source_sha256=_OTHER, max_recopies=0))
    assert decision.recopy is RecopyPlan.EXHAUSTED


# ---- 前提与输入矛盾 ----


@pytest.mark.parametrize("committed", [0, 9])
def test_incomplete_copy_cannot_enter_integrity(committed):
    with pytest.raises(ConsistencyError):
        decide_integrity(_facts(committed_bytes=committed))


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_sha256": None, "target_error": None},
        {"source_support": SourceChecksumSupport.SUPPORTED, "source_sha256": None,
         "source_error": None},
        {"source_support": SourceChecksumSupport.UNSUPPORTED, "source_sha256": _DIGEST},
    ],
)
def test_contradictory_facts_are_rejected(overrides):
    with pytest.raises(ConsistencyError):
        decide_integrity(_facts(**overrides))


def test_decide_integrity_requires_facts_type():
    with pytest.raises(TypeError):
        decide_integrity({"source_size": 10})


# ---- 类型校验 ----


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_size": -1}, {"committed_bytes": -1},
        {"source_support": "supported"},
        {"source_sha256": "zz"}, {"target_sha256": 64 * "A"},
        {"owner_running": None}, {"recopies_used": -1}, {"max_recopies": -1},
        {"recopies_used": True},
    ],
)
def test_integrity_facts_rejects_invalid_values(overrides):
    with pytest.raises(ValueError):
        _facts(**overrides)


@pytest.mark.parametrize("state", [1, 2, 7, "3", True])
def test_verification_save_rejects_non_terminal_state(state):
    with pytest.raises(ValueError):
        VerificationSave(
            copy_id=1, state=state, source_sha256=_DIGEST, target_sha256=_DIGEST,
            error_json=None, occurred_at=1,
        )


def test_verification_save_rejects_mismatched_error_facts():
    with pytest.raises(ValueError):
        VerificationSave(
            copy_id=1, state=6, source_sha256=_DIGEST, target_sha256=_DIGEST,
            error_json=None, occurred_at=1,
        )
    with pytest.raises(ValueError):
        VerificationSave(
            copy_id=1, state=3, source_sha256=None, target_sha256=_DIGEST,
            error_json=None, occurred_at=1,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"copy_id": 0}, {"copy_id": True},
        {"source_sha256": "short"}, {"target_sha256": None},
        {"max_recopies": -1}, {"max_recopies": True}, {"occurred_at": None},
    ],
)
def test_recopy_registration_rejects_invalid_values(overrides):
    values = dict(
        copy_id=1, source_sha256=_DIGEST, target_sha256=_OTHER,
        max_recopies=1, occurred_at=1,
    )
    values.update(overrides)
    with pytest.raises((ValueError, TypeError)):
        RecopyRegistration(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"copy_id": 0}, {"target_sha256": "short"}, {"size_bytes": -1},
        {"size_bytes": True}, {"occurred_at": None},
    ],
)
def test_prepared_request_rejects_invalid_values(overrides):
    values = dict(copy_id=1, target_sha256=_DIGEST, size_bytes=10, occurred_at=1)
    values.update(overrides)
    with pytest.raises((ValueError, TypeError)):
        PreparedRequest(**values)
