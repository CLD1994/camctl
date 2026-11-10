"""软件恢复责任的阶段和可靠结束规则。"""

from collections.abc import Mapping

from camctl.capture.recovery_types import RecoveryPhase
from camctl.contracts.json_values import json_equal

_IMMUTABLE = ("version", "origin_trigger_id", "device_id", "driver_id", "serial", "fingerprint",
    "created_at", "deadline_at", "timeout_s", "retry_interval_s", "max_reboots", "max_checks",
    "max_pages", "baseline_first", "baseline_last")
def _phase_transitions():
    from camctl.history.events import load_event_registry
    from camctl.contracts.enums import enum_for
    codes = enum_for("software_recoveries.phase")
    result = {phase: {phase} for phase in RecoveryPhase}
    for edge in load_event_registry()["state_models"]["software_recoveries.phase"]["edges"]:
        result[RecoveryPhase[codes(edge["from"]).name]].add(RecoveryPhase[codes(edge["to"]).name])
    return result


_NEXT = _phase_transitions()



def validate_recovery_transition(previous, state, *, phase, ended, evidence):
    """校验新状态相对原责任、证据和可靠设备结束的业务关系。"""
    if previous is None:
        if phase is not RecoveryPhase.CHECKING or any(state[key] for key in ("reboots", "checks", "pages")):
            raise ValueError("首次责任不是空预算检查阶段")
    else:
        old_phase = RecoveryPhase(previous["phase"])
        if any(not json_equal(previous[key], state[key]) for key in _IMMUTABLE):
            raise ValueError("原身份、基准或预算发生变化")
        if phase not in _NEXT[old_phase]:
            raise ValueError("恢复阶段不能重新开始")
        if state["trigger_id"] != previous["trigger_id"] and not (
                old_phase is RecoveryPhase.ABANDONED and phase is RecoveryPhase.CHECKING
                and previous["reboots"] == 0):
            raise ValueError("只允许未派发责任由新的有效动作接续")
        if "directory" not in evidence and any(not json_equal(previous[key], state[key])
                for key in ("cursor", "scope_pages", "scope_members", "scope_first_event_id", "scope_last_event_id")):
            raise ValueError("范围游标和累计事实只能随可靠目录页推进")
        for key in ("boot_before", "boot_after"):
            if previous[key] is not None and previous[key] != state[key]:
                raise ValueError("已保存的启动代次不得改写")
        for key in ("prepared", "scope_finalized"):
            if previous[key] is True and state[key] is not True:
                raise ValueError("可靠阶段事实不得回退")
        for key in ("reboots", "checks", "pages"):
            if not previous[key] <= state[key] <= previous[key] + 1:
                raise ValueError("累计次数不能回退或跳过")
        if state["reboots"] != previous["reboots"] and not (
                old_phase is RecoveryPhase.CHECKING and phase is RecoveryPhase.INTENT
                and state["boot_before"] is not None):
            raise ValueError("只有首个可靠意图可以增加派发次数")
    if ended:
        identity = evidence.get("identity")
        if (previous is None or RecoveryPhase(previous["phase"]) not in (
                RecoveryPhase.INTENT, RecoveryPhase.CONFIRMING)
                or phase is not RecoveryPhase.PREPARING or state["reboots"] < 1
                or not state["boot_before"] or not state["boot_after"]
                or state["boot_before"] == state["boot_after"]
                or not isinstance(identity, list) or len(identity) != 3
                or identity[:2] != [state["boot_after"], state["fingerprint"]]
                or (state["reported_serial"] is not None and identity[2] is not None
                    and identity[2] != state["reported_serial"])):
            raise ValueError("设备结束没有同一绑定的新启动代次")
    elif previous is not None and phase is RecoveryPhase.PREPARING and RecoveryPhase(previous["phase"]) is not RecoveryPhase.PREPARING:
        raise ValueError("新启动依据必须伴随设备结束保存")
    if phase in (RecoveryPhase.SCANNING, RecoveryPhase.READY) and not state["prepared"]:
        raise ValueError("目录阶段必须先有可靠会话准备")
    if phase is RecoveryPhase.READY and not state["scope_finalized"]:
        raise ValueError("就绪阶段必须具有完整范围事实")
    if state["prepared"] and (previous is None or not previous["prepared"]):
        if (previous is None or previous["phase"] != RecoveryPhase.PREPARING.value
                or phase is not RecoveryPhase.SCANNING or evidence.get("preparation", {}).get("ready") is not True):
            raise ValueError("会话准备完成缺少原阶段的可靠设置与存储结果")
    if state["scope_finalized"] and (previous is None or not previous["scope_finalized"]):
        directory = evidence.get("directory")
        if (not state["prepared"] or phase is not RecoveryPhase.READY or previous is None
                or RecoveryPhase(previous["phase"]) is not RecoveryPhase.SCANNING
                or not isinstance(directory, Mapping) or directory.get("next_cursor") is not None):
            raise ValueError("文件范围必须沿准备后的完整分页末页确定")


def validate_recovery_owner(state, activity, action, *, ended):
    """恢复责任只属于失败或取消后仍保留占用的原设备活动。"""
    if action["status"] not in (4, 6) or activity["occupancy_state"] != 1:
        raise ValueError("旧动作没有失败或取消后的保留占用")
    if state["device_id"] != action["device_id"] or state["driver_id"] != action["driver_id"]:
        raise ValueError("恢复绑定与原活动不一致")
    if ended and activity["dispatch_state"] not in (2, 3):
        raise ValueError("可靠未启动活动不需要重启结束依据")
