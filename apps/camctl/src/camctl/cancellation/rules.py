"""取消资格决策表与设备任务资格事实装配。

按《设备任务的取消资格》顺序判断：终态保持并处理关联责任；取消已
生效复用原责任及预算；可靠未启动允许取消且不要求停止能力；已启动
或可能启动时按首次固定停止能力分区，无停止能力拒绝本项且原任务继
续。事实或提交不可靠时先核实，不猜测未启动或取消成功。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from camctl.cancellation.models import (
    CancelOriginFacts,
    CancelApplyMode,
    CancelProgress,
    CancellationResult,
    CancellationStatus,
    OriginCancelDecision,
)
from camctl.contracts.values import ConsistencyError

__all__ = [
    "CancelEligibility",
    "DispatchPhase",
    "OriginCancelDecision",
    "decide_origin_cancel",
    "EligibilityFacts",
    "decide_cancel_eligibility",
    "LoadedEligibility",
    "current_eligibility_facts",
    "may_apply_cancel",
    "may_link_auto_preview",
    "summarize_cancel",
]


#: cancel_items.status 的登记编号。
_ITEM_PENDING, _ITEM_RUNNING, _ITEM_SUCCEEDED, _ITEM_FAILED = 1, 2, 3, 4
_ITEM_CANCELED = 5


def summarize_cancel(progress: CancelProgress) -> CancellationResult:
    """按逐项进度判定取消动作的结果分类。

    保存取消标记只表示生效到执行控制：任一项仍在处理即保持运行，
    不能仅凭标记提前成功。全部项结束后，任一失败按登记的机器错误
    cancel_items_failed 汇总（具体原因由逐项结果表达）；无失败才成
    功。项 CANCELED 属取消发起者收场语义，普通汇总拒绝解释。
    """
    succeeded = failed = 0
    for item in progress.items:
        if item.status == _ITEM_SUCCEEDED:
            succeeded += 1
        elif item.status == _ITEM_FAILED:
            failed += 1
        elif item.status in (_ITEM_PENDING, _ITEM_RUNNING):
            return CancellationResult(
                status=CancellationStatus.RUNNING, succeeded=succeeded,
                failed=failed)
        else:
            raise ValueError(
                f"取消项状态不可由普通汇总解释: {item.item_id}"
                f" status={item.status!r}")
    if failed:
        return CancellationResult(
            status=CancellationStatus.FAILED, succeeded=succeeded,
            failed=failed, error={
                "code": "cancel_items_failed", "stage": "execution",
                "details": {}})
    return CancellationResult(
        status=CancellationStatus.SUCCEEDED, succeeded=succeeded,
        failed=failed)


class DispatchPhase(Enum):
    """目标设备任务的真实派发阶段。"""

    #: 可靠确认尚未启动，且没有可能生效的在途启动调用。
    NOT_STARTED = "not_started"
    #: 启动调用在途或发送结果未知，无法排除任务已经启动。
    START_PENDING = "start_pending"
    #: 启动已确认成功返回，任务已经启动。
    STARTED = "started"
    #: 派发事实或相关提交不可靠，须先核实。
    UNVERIFIED = "unverified"


class CancelEligibility(Enum):
    """取消资格判定结果；只表达本次请求对该目标的处理方式。"""

    #: 目标已终态：保持终态，处理仍适用的关联取回或交付责任。
    TERMINAL = "terminal"
    #: 目标取消已生效：复用原取消责任及累计预算，不重复施加。
    ALREADY_CANCELED = "already_canceled"
    #: 可靠未启动：允许取消并阻止普通启动，不要求停止能力。
    ALLOW_PRE_START = "allow_pre_start"
    #: 已启动或可能启动且支持停止：允许，按原归属与预算停止收场。
    ALLOW_WITH_STOP = "allow_with_stop"
    #: 已启动或可能启动且不支持停止：拒绝本项，原任务继续。
    REJECT_UNSUPPORTED = "reject_unsupported"
    #: 事实或提交不可靠：先按恢复规则核实，不猜测结果。
    UNVERIFIED = "unverified"


class EligibilityFacts:
    """取消资格判定的输入事实。

    stop_supported 是目标固定停止能力（首次建立活动或受理时保存），
    不因恢复时的默认值改变；仅已启动或可能启动的分区使用它。
    """

    __slots__ = ("terminal", "cancel_applied", "dispatch",
                 "stop_supported", "facts_reliable")

    def __init__(self, *, terminal: bool, cancel_applied: bool,
                 dispatch: DispatchPhase, stop_supported: bool,
                 facts_reliable: bool = True) -> None:
        if not isinstance(dispatch, DispatchPhase):
            raise TypeError(f"派发阶段必须使用 DispatchPhase: {dispatch!r}")
        self.terminal = bool(terminal)
        self.cancel_applied = bool(cancel_applied)
        self.dispatch = dispatch
        self.stop_supported = bool(stop_supported)
        self.facts_reliable = bool(facts_reliable)

    def __repr__(self) -> str:  # pragma: no cover - 诊断辅助
        return (f"EligibilityFacts(terminal={self.terminal},"
                f" cancel_applied={self.cancel_applied},"
                f" dispatch={self.dispatch.value},"
                f" stop_supported={self.stop_supported},"
                f" facts_reliable={self.facts_reliable})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EligibilityFacts):
            return NotImplemented
        return (self.terminal == other.terminal
                and self.cancel_applied == other.cancel_applied
                and self.dispatch is other.dispatch
                and self.stop_supported == other.stop_supported
                and self.facts_reliable == other.facts_reliable)


def decide_cancel_eligibility(facts: EligibilityFacts) -> CancelEligibility:
    """按取消资格表自上而下判定；不允许折叠分区。"""
    if not facts.facts_reliable:
        return CancelEligibility.UNVERIFIED
    if facts.terminal:
        return CancelEligibility.TERMINAL
    if facts.cancel_applied:
        return CancelEligibility.ALREADY_CANCELED
    if facts.dispatch is DispatchPhase.UNVERIFIED:
        return CancelEligibility.UNVERIFIED
    if facts.dispatch is DispatchPhase.NOT_STARTED:
        return CancelEligibility.ALLOW_PRE_START
    if facts.stop_supported:
        return CancelEligibility.ALLOW_WITH_STOP
    return CancelEligibility.REJECT_UNSUPPORTED


def may_apply_cancel(eligibility: CancelEligibility) -> bool:
    """是否允许本次请求对该目标施加取消。"""
    return eligibility in (CancelEligibility.ALLOW_PRE_START,
                           CancelEligibility.ALLOW_WITH_STOP)


def may_link_auto_preview(action, eligibility: CancelEligibility) -> bool:
    """有效 HB 原取消责任可继续处理关联，其他目标沿用新施加资格。"""
    from camctl.devices.start_entry import is_hasselblad_photo
    return may_apply_cancel(eligibility) or (
        eligibility is CancelEligibility.ALREADY_CANCELED
        and is_hasselblad_photo(action) and action["cancel_requested"] == 1)


#: 拍摄动作类型（资格表只适用设备任务）。
_CAPTURE_TYPES = frozenset({1, 2, 3})
_CANCEL_TASK_TYPE = 6
#: 取回、清理与报告动作：取消按各自模块规则收场，装配视为恒允许标记。
_OBTAIN_TASK_TYPE, _CLEANUP_TASK_TYPE, _REPORT_TASK_TYPE = 4, 5, 7
#: device_activities.dispatch_state 的登记编号。
_DISPATCH_NOT_DISPATCHED, _DISPATCH_MAY_HAVE, _DISPATCH_RETURNED = 1, 2, 3
_DISPATCH_REJECTED = 4
#: 动作终态集合。
_ACTION_TERMINAL = (3, 4, 5, 6)
_TIME_LAPSE_TYPE = 3


@dataclass(frozen=True)
class LoadedEligibility:
    """一次数据库读取得到的资格输入；入口许可由事件循环随后应用。"""

    action: dict[str, Any]
    facts: EligibilityFacts
    entered_dispatch: DispatchPhase | None = None


def current_eligibility_facts(loaded: LoadedEligibility, entry_phase) -> EligibilityFacts:
    """在数据库等待结束后，应用当前 START 入口阶段。"""
    if (loaded.facts.terminal or loaded.facts.cancel_applied
            or loaded.entered_dispatch is None or entry_phase is None):
        return loaded.facts
    from camctl.devices.start_entry import EntryPhase
    if not isinstance(entry_phase, EntryPhase):
        raise ConsistencyError("START 入口阶段类型不可解释")
    if entry_phase in (EntryPhase.GRANTING, EntryPhase.UNKNOWN, EntryPhase.RESERVED):
        raise ConsistencyError("START 授予或取消事务尚未核实")
    if entry_phase is EntryPhase.ENTERED:
        dispatch = loaded.entered_dispatch
    elif entry_phase is EntryPhase.READY:
        dispatch = DispatchPhase.NOT_STARTED
    else:
        return loaded.facts
    facts = loaded.facts
    return EligibilityFacts(
        terminal=facts.terminal, cancel_applied=facts.cancel_applied,
        dispatch=dispatch, stop_supported=facts.stop_supported,
        facts_reliable=facts.facts_reliable)


def action_eligibility_facts(action, *, motor_facts=None, motor_permit=None):
    """按动作种类和已读取的事实装配非拍摄动作资格。"""
    kind = action["type"]
    if kind == 8:
        from camctl.motor.rules import owns_send_permit
        local = owns_send_permit(motor_facts, motor_permit)
        return EligibilityFacts(
            terminal=action['status'] in _ACTION_TERMINAL,
            cancel_applied=bool(action['cancel_requested']),
            dispatch=DispatchPhase.NOT_STARTED if motor_facts.notification is None or local else DispatchPhase.START_PENDING,
            stop_supported=False)
    if kind not in (_OBTAIN_TASK_TYPE, _CANCEL_TASK_TYPE, _CLEANUP_TASK_TYPE, _REPORT_TASK_TYPE):
        raise ConsistencyError(
            f"取消资格判断只适用已定义的目标类型: {action['id']} type={kind!r}")
    return EligibilityFacts(
        terminal=action["status"] in _ACTION_TERMINAL,
        cancel_applied=bool(action["cancel_requested"]),
        dispatch=(DispatchPhase.STARTED if action["execution_started"] == 1
                  else DispatchPhase.NOT_STARTED),
        stop_supported=True)


def cancel_status_after_apply(target, mode):
    """首次取消按原派发分区确定目标是否立即进入取消终态。"""
    if mode is CancelApplyMode.PRE_START:
        if target["status"] == 1:
            return 6
        if target["status"] != 2 or target["type"] not in _CAPTURE_TYPES:
            raise ValueError("未启动取消要求待执行或可靠未启动的拍摄目标")
    elif mode is CancelApplyMode.WITH_STOP:
        if target["status"] != 2:
            raise ValueError("停止收场取消要求目标运行中")
    else:
        raise ValueError("只有首次取消确定目标状态")
    return None


def needs_capture_start_facts(action):
    """终态或已取消时优先保留原责任，不必再次读取 START。"""
    return action["status"] not in _ACTION_TERMINAL and not action["cancel_requested"]


def capture_eligibility_facts(action, activity, *, not_started=None):
    """拍摄动作原持久事实共用资格装配；本地许可只由 SQL 外层协调。"""
    kind = action["type"]
    if kind not in _CAPTURE_TYPES:
        raise ConsistencyError("拍摄资格装配只适用于拍摄动作")
    terminal = action["status"] in _ACTION_TERMINAL
    applied = bool(action["cancel_requested"])
    if terminal or applied:
        dispatch = DispatchPhase.UNVERIFIED
    else:
        if type(not_started) is not bool:
            raise ConsistencyError("拍摄取消缺少原 START 判定")
        dispatch = DispatchPhase.NOT_STARTED if not_started else dispatch_phase(activity)
    return EligibilityFacts(terminal=terminal, cancel_applied=applied,
        dispatch=dispatch, stop_supported=_stop_supported(action, kind, activity))


def dispatch_phase(activity) -> DispatchPhase:
    if activity is None:
        return DispatchPhase.UNVERIFIED
    state = activity["dispatch_state"]
    if state == _DISPATCH_MAY_HAVE:
        return DispatchPhase.START_PENDING
    if state == _DISPATCH_RETURNED:
        return DispatchPhase.STARTED
    if state in (_DISPATCH_NOT_DISPATCHED, _DISPATCH_REJECTED):
        # 活动行不能覆盖尚在途的原尝试或未完成的启动核实责任。
        return DispatchPhase.START_PENDING
    raise ConsistencyError(
        f"派发阶段编号不可解释: {state!r}")


def _stop_supported(action, kind: int, activity) -> bool:
    if activity is not None:
        return bool(activity["stop_supported"])
    if kind != _TIME_LAPSE_TYPE:
        # 未建立活动的录像或照片尚在未启动分区，能力值不参与判定。
        return kind == 2
    spec = action["execution_spec_json"]
    if not isinstance(spec, dict) or "stop_supported" not in spec:
        raise ConsistencyError(
            f"延时摄影执行定义缺少停止能力: {action['id']}")
    value = spec["stop_supported"]
    if not isinstance(value, bool):
        raise ConsistencyError(f"延时摄影停止能力非法: {value!r}")
    return value


def decide_origin_cancel(facts: CancelOriginFacts) -> OriginCancelDecision:
    """按取消发起者的进度与自身取消状态选择处理分支。

    已可靠保存终态优先：保留原终态，不重新取消或重开处理。自身取
    消已生效时停止新增目标影响并结束等待，未结束项转入取消收场，
    发起者以 canceled 结束；已生效目标的责任由目标流程独立继续。
    相关事务尚未确认或事实不可靠时先核实，不猜测分支。
    """
    if not facts.facts_reliable or facts.pending_transactions:
        return OriginCancelDecision.VERIFY_FIRST
    if facts.origin_terminal:
        return OriginCancelDecision.KEEP_TERMINAL
    if facts.origin_cancel_applied:
        return OriginCancelDecision.SETTLE_CANCELED
    return OriginCancelDecision.CONTINUE


def validate_stored_cancel_mode(target, mode):
    """核对既定生效方式对原目标状态的要求，不重新决定停止能力。"""
    if mode is CancelApplyMode.PRE_START:
        if target["status"] not in (1, 2) or target["cancel_requested"]:
            raise ConsistencyError("未启动取消要求目标仍有效且未取消")
    elif mode is CancelApplyMode.WITH_STOP:
        if target["status"] != 2 or target["cancel_requested"]:
            raise ConsistencyError("停止收场取消要求目标执行中且未取消")
    elif mode is CancelApplyMode.TERMINAL:
        if target["status"] not in _ACTION_TERMINAL:
            raise ConsistencyError("终态取消要求目标已终态")
    elif mode is CancelApplyMode.ALREADY:
        if not target["cancel_requested"]:
            raise ConsistencyError("复用取消责任要求目标取消已生效")
    else:
        raise ConsistencyError(f"取消生效方式不可解释: {mode!r}")


def existing_photo_preview_decision(action, activity):
    """终态及已生效取消先决定关联；其余情况继续判断入口事实。"""
    if action['status'] in _ACTION_TERMINAL:
        return True
    if action['cancel_requested']:
        return may_link_auto_preview(action, decide_cancel_eligibility(
            capture_eligibility_facts(action, activity)))
    return None


def photo_preview_entry_decision(phase, attempts, *, reserved, allow_pending_reserved,
                                 confirmation_pending=None):
    """需要原 START 确认事实时返回 None；否则返回关联许可。"""
    from camctl.contracts.enums import enum_for
    from camctl.devices.start_entry import EntryPhase
    if phase is EntryPhase.ENTERED:
        return False
    if phase is EntryPhase.BLOCKED:
        if confirmation_pending is None:
            return None
        return not confirmation_pending and (bool(attempts) or reserved)
    if not (allow_pending_reserved and reserved and attempts):
        return False
    if confirmation_pending is None:
        return None
    return (not confirmation_pending
            and all(attempt['status'] == int(enum_for('operation_attempts.status').RUNNING)
                    and attempt['result_json'] is None for attempt in attempts))


def report_cancel_settled(action):
    """报告目标只有终态可作为取消成功依据。"""
    return action['type'] != _REPORT_TASK_TYPE or action['status'] in _ACTION_TERMINAL


def preparation_cancel_pending(*, not_started, preparation_resolved, activity):
    """原启动未发生时，准备调用或占用未收场就继续等待。"""
    return not_started and (not preparation_resolved or (
        activity is not None and activity['occupancy_state'] == 1))


def pre_start_cancel_finishes_target(target):
    """待执行目标和可靠未发送的电机可在生效时终态取消。"""
    return target['status'] == 1 or target['type'] == 8


def immediate_cancel_member_outcome(mode, target, *, withdrawal_pending, preparation_pending):
    """撤回及原准备责任结束前不生成成员成功结果。"""
    from camctl.cancellation.models import CancelOutcomeChoice
    if mode is CancelApplyMode.PRE_START:
        if pre_start_cancel_finishes_target(target) and not preparation_pending:
            return CancelOutcomeChoice.CANCELED
    elif mode is CancelApplyMode.TERMINAL:
        if not withdrawal_pending and not preparation_pending:
            return CancelOutcomeChoice.ALREADY_TERMINAL
    return None
