"""拷贝进度、摘要与重拷的业务事实判定。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.outputs.copy import (
    RecopyPlan, decide_recopy, read_owner_eligible, segment_advances,
    segment_exceeds_source, validate_segment_save_state, verification_result_error,
)


def _segment(**changes):
    facts = dict(reset_state=int(enum_for("file_copies.reset_state").READY),
                 current_round=2, committed_bytes=4, source_size=10,
                 requested_round=2, requested_before=4, end=10)
    facts.update(changes)
    return dict(copy={"reset_state": facts["reset_state"], "round": facts["current_round"],
                      "committed_bytes": facts["committed_bytes"], "source_size": facts["source_size"]},
                requested_round=facts["requested_round"], requested_before=facts["requested_before"], end=facts["end"])


def test_segment_checks_progress_and_fixed_source_boundary():
    assert segment_advances(4, 10)
    assert not segment_advances(4, 4)
    assert not segment_exceeds_source(10, 10)
    assert segment_exceeds_source(11, 10)
    validate_segment_save_state(**_segment())


@pytest.mark.parametrize("changes,message", [
    ({"reset_state": 2, "current_round": 3}, "重置意图未完成前不能保存拷贝进度"),
    ({"current_round": 3, "committed_bytes": 5}, "段所属轮次 2 与当前拷贝轮次 3 不一致"),
    ({"committed_bytes": 5, "end": 11}, "当前进度 5 与段申请的旧进度 4 不一致"),
    ({"end": 11}, "段范围越过固定源长度"),
])
def test_segment_save_rejects_stale_facts_in_original_order(changes, message):
    with pytest.raises(ConsistencyError, match=message):
        validate_segment_save_state(**_segment(**changes))


def test_segment_save_checks_reset_before_missing_later_fields():
    with pytest.raises(ConsistencyError, match="重置意图未完成前"):
        validate_segment_save_state(copy={"reset_state": 2}, requested_round=2,
                                    requested_before=4, end=10)


def test_read_owner_distinguishes_canceled_from_invalid_state():
    running = int(enum_for("actions.status").RUNNING)
    assert read_owner_eligible({"status": running, "cancel_requested": 0})
    assert not read_owner_eligible({"status": running, "cancel_requested": 1})
    with pytest.raises(ConsistencyError, match="状态或取消标志无效"):
        read_owner_eligible({"status": running, "cancel_requested": True})
    with pytest.raises(ConsistencyError, match="状态未登记"):
        read_owner_eligible({"status": 999, "cancel_requested": 0})


@pytest.mark.parametrize("name,source,target,error,expected", [
    ("MATCHED", "a", "a", None, None),
    ("MATCHED", "a", "b", None, "校验通过要求源摘要与主机摘要一致"),
    ("MISMATCHED", "a", "b", None, None),
    ("MISMATCHED", None, "b", None, "摘要不一致要求两个摘要都存在且不等"),
    ("MISMATCHED", "a", "a", None, "摘要不一致要求两个摘要都存在且不等"),
    ("SOURCE_CHECKSUM_UNAVAILABLE", None, "b", None, None),
    ("SOURCE_CHECKSUM_UNAVAILABLE", "a", "b", None, "明确不支持源端摘要不能携带源摘要"),
    ("FAILED", None, "b", {"code": "error"}, None),
    ("FAILED", None, "b", None, "校验失败必须携带失败诊断"),
])
def test_verification_result_matches_actual_digest_facts(name, source, target, error, expected):
    state = int(enum_for("file_copies.verification_state")[name])
    assert verification_result_error(state=state, source=source, target=target, error=error) == expected


def test_recopy_keeps_owner_eligibility_before_budget():
    assert decide_recopy(owner_running=False, recopies_used=1, max_recopies=1) is RecopyPlan.OWNER_NOT_ELIGIBLE
    assert decide_recopy(owner_running=True, recopies_used=1, max_recopies=1) is RecopyPlan.EXHAUSTED
    assert decide_recopy(owner_running=True, recopies_used=0, max_recopies=1) is RecopyPlan.REGISTER
