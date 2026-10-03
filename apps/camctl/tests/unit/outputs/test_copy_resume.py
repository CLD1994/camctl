"""共用拷贝的续传位置决策与读取尝试计划；纯规则不访问资源。"""

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.copy import (
    AttemptDecision, AttemptPlan, AttemptRecord, CopyDecision, CopyFacts,
    ResumeOutcome, TargetFileObservation, decide_attempt, decide_resume,
)


def _facts(
    size: int, committed: int, *, present: bool | None = True,
    length: int | None = None, reset_pending: bool = False,
) -> CopyFacts:
    return CopyFacts(
        source_size=size,
        committed_bytes=committed,
        reset_pending=reset_pending,
        target=TargetFileObservation(observed=True, present=present, length=length),
    )


# ---- 七个续传分区（docs/architecture/file-copy.md 重启后选择续传位置） ----


def test_resume_preserves_confirmed_prefix():
    """C=3、L=5、N=6：截去未确认尾部到 3，再从 3 继续。"""
    decision = decide_resume(_facts(6, 3, length=5))
    assert decision == CopyDecision(
        outcome=ResumeOutcome.TRUNCATE, offset=3, truncate_to=3,
    )


def test_missing_target_with_zero_progress_creates():
    decision = decide_resume(_facts(6, 0, present=False))
    assert decision == CopyDecision(
        outcome=ResumeOutcome.CREATE, offset=0, truncate_to=None,
    )


def test_confirmed_length_continues_in_place():
    decision = decide_resume(_facts(6, 3, length=3))
    assert decision == CopyDecision(
        outcome=ResumeOutcome.CONTINUE, offset=3, truncate_to=None,
    )


def test_complete_target_enters_verification():
    """L=C=N：全部字节已经保存，进入完整性确认，不新增空段。"""
    decision = decide_resume(_facts(6, 6, length=6))
    assert decision == CopyDecision(
        outcome=ResumeOutcome.VERIFY, offset=None, truncate_to=None,
    )


def test_missing_target_with_confirmed_progress_is_rejected():
    with pytest.raises(ConsistencyError):
        decide_resume(_facts(6, 3, present=False))


def test_target_shorter_than_confirmed_progress_is_rejected():
    with pytest.raises(ConsistencyError):
        decide_resume(_facts(6, 3, length=2))


def test_target_longer_than_source_is_rejected():
    with pytest.raises(ConsistencyError):
        decide_resume(_facts(6, 3, length=7))


def test_progress_beyond_source_is_rejected():
    """C 越界（超过固定源长度）属于状态矛盾，按一致性错误处理。"""
    with pytest.raises(ConsistencyError):
        decide_resume(_facts(6, 7, length=7))


def test_unreliable_observation_stops_resume_decision():
    """观察不可靠时停止判断，不当作文件不存在或进度为零。"""
    facts = CopyFacts(
        source_size=6, committed_bytes=3,
        target=TargetFileObservation(observed=False, error=OSError("stat 失败")),
    )
    with pytest.raises(ConsistencyError):
        decide_resume(facts)


# ---- 目标重置分区（摘要不一致后的有限重拷先登记重置意图） ----


@pytest.mark.parametrize("present, length", [(True, 6), (True, 0), (False, None)])
def test_reset_pending_resumes_from_zero_regardless_of_file(present, length):
    """重置未完成时一律归零，不按文件现状续传旧轮数据。"""
    decision = decide_resume(_facts(6, 0, present=present, length=length, reset_pending=True))
    assert decision == CopyDecision(
        outcome=ResumeOutcome.RESET_TARGET, offset=0, truncate_to=0,
    )


def test_reset_pending_with_confirmed_progress_is_rejected():
    """重置意图登记后可靠进度必须已经归零，否则为矛盾状态。"""
    with pytest.raises(ConsistencyError):
        decide_resume(_facts(6, 3, length=5, reset_pending=True))


def test_reset_pending_ignores_unreliable_observation():
    """重置路径不依赖文件观察，观察不可约不影响归零决定。"""
    facts = CopyFacts(
        source_size=6, committed_bytes=0, reset_pending=True,
        target=TargetFileObservation(observed=False, error=OSError("stat 失败")),
    )
    assert decide_resume(facts).outcome is ResumeOutcome.RESET_TARGET


# ---- 输入类型校验 ----


@pytest.mark.parametrize("size", [True, -1, 1.0, "6", None])
def test_copy_facts_rejects_invalid_source_size(size):
    with pytest.raises(ValueError):
        CopyFacts(source_size=size, committed_bytes=0,
                  target=TargetFileObservation(observed=True, present=False))


@pytest.mark.parametrize("committed", [True, -1, 1.0, "0", None])
def test_copy_facts_rejects_invalid_committed_bytes(committed):
    with pytest.raises(ValueError):
        CopyFacts(source_size=6, committed_bytes=committed,
                  target=TargetFileObservation(observed=True, present=False))


def test_copy_facts_requires_target_observation():
    with pytest.raises(ValueError):
        CopyFacts(source_size=6, committed_bytes=0, target=None)


# ---- 读取尝试计划：未保存失败沿原尝试，已明确失败只能新增合法尝试 ----


def _attempt(no: int, status: int, round_: int = 1) -> AttemptRecord:
    return AttemptRecord(attempt_id=900 + no, attempt_no=no, status=status, copy_round=round_)


def test_in_flight_attempt_is_resumed():
    decision = decide_attempt((_attempt(1, 3), _attempt(2, 1)), current_round=1)
    assert decision == AttemptDecision(
        plan=AttemptPlan.RESUME_EXISTING, attempt_id=902, attempt_no=2,
    )


def test_all_attempts_finished_require_new_legal_attempt():
    decision = decide_attempt((_attempt(1, 2), _attempt(2, 3)), current_round=1)
    assert decision == AttemptDecision(plan=AttemptPlan.NEW_REQUIRED)


def test_without_attempts_a_new_attempt_is_required():
    assert decide_attempt((), current_round=1).plan is AttemptPlan.NEW_REQUIRED


def test_two_in_flight_attempts_are_rejected():
    with pytest.raises(ConsistencyError):
        decide_attempt((_attempt(1, 1), _attempt(2, 1)), current_round=1)


def test_attempts_of_earlier_rounds_do_not_resume():
    """旧轮次的尝试属于历史，不沿原尝试，也不报重复。"""
    decision = decide_attempt((_attempt(1, 1), _attempt(2, 2)), current_round=2)
    assert decision.plan is AttemptPlan.NEW_REQUIRED


def test_attempt_from_unknown_later_round_is_rejected():
    """早于已存在尝试轮次的当前轮次无法解释，不能忽略未来事实。"""
    with pytest.raises(ConsistencyError):
        decide_attempt((_attempt(1, 1, round_=2),), current_round=1)


@pytest.mark.parametrize("status", [0, 5, True, "1"])
def test_attempt_record_rejects_invalid_status(status):
    with pytest.raises(ValueError):
        AttemptRecord(attempt_id=901, attempt_no=1, status=status, copy_round=1)


@pytest.mark.parametrize("round_", [0, -1, True, "1", 1.0])
def test_attempt_record_rejects_invalid_round(round_):
    with pytest.raises(ValueError):
        AttemptRecord(attempt_id=901, attempt_no=1, status=1, copy_round=round_)
