"""依据已取得的活动和恢复事实判断占用释放资格。"""

from typing import Any, Mapping

from camctl.capture.recovery_types import RecoveryPhase


def release_basis_holds(facts: Mapping[str, Any]) -> bool:
    """活动结束、可靠未派发、无效果或适用完成依据允许解除占用。"""
    return (facts.get("activity_state") == 3
            or facts.get("dispatch_state") in (1, 4)
            or facts.get("completion_basis") == 3)


def output_scope_resolved(facts: Mapping[str, Any], recovery=None) -> bool:
    """基准已固定，且原集合结论或独立集合事实证明范围确定。"""
    ordinary = (facts.get("baseline_state") == 3
            and (facts.get("result_set_state") == 3
                 or facts.get("output_set_finalized_event_id") is not None))
    if ordinary:
        return True
    if not isinstance(recovery, Mapping):
        return False
    return (facts.get("baseline_state") == 3 and facts.get("activity_state") == 3
        and recovery.get("phase") == RecoveryPhase.READY.value
        and recovery.get("prepared") is True and recovery.get("scope_finalized") is True
        and recovery.get("cursor") is None
        and recovery.get("scope_pages", 0) > 0
        and recovery.get("scope_first_event_id") is not None and recovery.get("scope_last_event_id") is not None
        and recovery.get("boot_before") is not None and recovery.get("boot_after") is not None
        and recovery["boot_before"] != recovery["boot_after"]
        and recovery.get("baseline_first") == facts.get("baseline_first_event_id")
        and recovery.get("baseline_last") == facts.get("baseline_last_event_id"))


def occupancy_release_allowed(facts: Mapping[str, Any], *, unstarted: bool = False,
                              preparation_resolved: bool = False, preparation_calls_resolved: bool = True) -> bool:
    """释放依据与输出范围限制都满足，且占用尚未释放。"""
    return (preparation_calls_resolved and facts["occupancy_state"] == 1 and release_basis_holds(facts)
            and (facts["ownership_mode"] != 2 or (unstarted and preparation_resolved)
                 or output_scope_resolved(facts)))
