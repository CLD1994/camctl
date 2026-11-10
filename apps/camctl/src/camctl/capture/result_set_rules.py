"""结果集合的状态、结论与采集判定规则。"""

from collections.abc import Mapping

from camctl.capture.models import ResultSetPhase
from camctl.contracts.values import ConsistencyError

RESULT_COMPLETE_REASON = 1
RESULT_UNSATISFIED_REASON = 2
RESULT_UNCONFIRMED_REASON = 3
RESULT_BEGIN_REASON = 4

RESULT_PHASE_TARGETS = {
    ResultSetPhase.COMPLETE: (RESULT_COMPLETE_REASON, 3, 1),
    ResultSetPhase.UNSATISFIED: (RESULT_UNSATISFIED_REASON, 3, 2),
    ResultSetPhase.UNCONFIRMED: (RESULT_UNCONFIRMED_REASON, 4, 3),
    ResultSetPhase.BEGIN: (RESULT_BEGIN_REASON, 2, None),
}
RESULT_SET_NEXT = {1: frozenset({2, 3, 4}), 2: frozenset({3, 4})}
TIME_AND_OUTPUTS_METHOD = "time_and_outputs"
DEVICE_EVIDENCE_METHOD = "device_evidence"


def decide_result_basis(facts, phase, evidence, capture):
    """根据固定活动事实及本次结论确定采集判定；不读取结果页。"""
    current = facts["completion_basis"]
    if phase is ResultSetPhase.BEGIN:
        return current, None
    if phase is ResultSetPhase.UNSATISFIED:
        if current not in (None, 1):
            raise ConsistencyError("采集判定已确定，不能再保存明确不满足")
        return 4, dict(evidence)
    if phase is ResultSetPhase.UNCONFIRMED:
        if current not in (None, 1):
            raise ConsistencyError("采集判定已确定，不能再变为无法确认")
        if capture is None or current == 1:
            return current, None
        return 1, None
    method = evidence["method"]
    if method == TIME_AND_OUTPUTS_METHOD:
        if facts["completion_mode"] != 2:
            raise ConsistencyError("固定完成方式不是时间与产物，判定不适用")
        if (facts["dispatch_state"] != 3 or facts["sent_at"] is None
                or facts["expected_check_at"] is None or facts["wait_completed_event_id"] is None):
            raise ConsistencyError("时间与产物判定缺少可靠发送与等待完成事实")
        if evidence.get("wait_completed_event_id") != facts["wait_completed_event_id"]:
            raise ConsistencyError("完成依据引用的等待事件与本活动不符")
        if facts["activity_state"] == 2:
            raise ConsistencyError("不能以时间与产物判定覆盖进行中的实际观察")
        return 3, dict(evidence)
    if method == DEVICE_EVIDENCE_METHOD:
        if facts["activity_state"] != 3:
            raise ConsistencyError("设备证据判定要求原活动已经结束")
        return 2, dict(evidence)
    raise ConsistencyError(f"未登记的采集判定方法: {method!r}")


def decide_result_error(phase, error):
    if phase is ResultSetPhase.COMPLETE:
        return None
    return None if error is None else dict(error)


def result_begin_error(before, after):
    if (before.get("result_set_state"), after.get("result_set_state")) != (1, 2):
        return "开始分支只把未核实集合推进到核实中"
    extra = set(after) - {"result_set_state"}
    if extra:
        return f"开始分支不携带结论事实: {sorted(extra)}"
    return None


def result_conclusion_error(reason, state_before, after):
    expected = next(((target, outcome) for phase, (branch, target, outcome) in RESULT_PHASE_TARGETS.items()
                     if phase is not ResultSetPhase.BEGIN and branch == reason), None)
    if expected is None or state_before not in (1, 2) or after.get("result_set_state") != expected[0]:
        return "结论分支与核实状态转换不符"
    check = after.get("result_check_json")
    if not isinstance(check, Mapping):
        return "结论分支必须保存结果集合核实依据"
    if (not isinstance(check.get("contract"), str) or not check["contract"]
            or check.get("outcome") != expected[1] or not isinstance(check.get("observation"), Mapping)):
        return "结果集合核实依据的结构或判定编码非法"
    return None
