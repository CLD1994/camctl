"""取消资格决策表与设备任务资格事实装配。

按《设备任务的取消资格》顺序判断：终态保持并处理关联责任；取消已
生效复用原责任及预算；可靠未启动允许取消且不要求停止能力；已启动
或可能启动时按首次固定停止能力分区，无停止能力拒绝本项且原任务继
续。事实或提交不可靠时先核实，不猜测未启动或取消成功。
"""

from __future__ import annotations

from contextlib import closing
from enum import Enum
from typing import Any

from camctl.cancellation.models import (
    CancelOriginFacts,
    CancelProgress,
    CancellationResult,
    CancellationStatus,
    OriginCancelDecision,
)
from camctl.contracts.values import ConsistencyError
from camctl.persistence.transaction import row_facts

__all__ = [
    "CancelEligibility",
    "DispatchPhase",
    "OriginCancelDecision",
    "decide_origin_cancel",
    "EligibilityFacts",
    "decide_cancel_eligibility",
    "load_eligibility_facts",
    "may_apply_cancel",
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
    if not facts.facts_reliable or facts.dispatch is DispatchPhase.UNVERIFIED:
        return CancelEligibility.UNVERIFIED
    if facts.terminal:
        return CancelEligibility.TERMINAL
    if facts.cancel_applied:
        return CancelEligibility.ALREADY_CANCELED
    if facts.dispatch is DispatchPhase.NOT_STARTED:
        return CancelEligibility.ALLOW_PRE_START
    if facts.stop_supported:
        return CancelEligibility.ALLOW_WITH_STOP
    return CancelEligibility.REJECT_UNSUPPORTED


def may_apply_cancel(eligibility: CancelEligibility) -> bool:
    """是否允许本次请求对该目标施加取消。"""
    return eligibility in (CancelEligibility.ALLOW_PRE_START,
                           CancelEligibility.ALLOW_WITH_STOP)


#: 拍摄动作类型（资格表只适用设备任务）。
_CAPTURE_TYPES = frozenset({1, 2, 3})
_CANCEL_TASK_TYPE = 6
#: 清理与报告动作：取消按各自模块规则收场，装配视为恒允许标记。
_CLEANUP_TASK_TYPE, _REPORT_TASK_TYPE = 5, 7
#: device_activities.dispatch_state 的登记编号。
_DISPATCH_NOT_DISPATCHED, _DISPATCH_MAY_HAVE, _DISPATCH_RETURNED = 1, 2, 3
_DISPATCH_REJECTED = 4
#: 动作终态集合。
_ACTION_TERMINAL = (3, 4, 5, 6)
_TIME_LAPSE_TYPE = 3


def load_eligibility_facts(connection, action_id: int) -> EligibilityFacts:
    """从已保存事实装配目标设备任务的取消资格输入。

    停止能力优先取首次建立活动时保存的 `device_activities.stop_
    supported`；尚未建立活动的延时摄影按受理时固定的执行定义读取，
    定义缺失或非法按状态库错误处理，不用新默认值补齐。
    """
    action = _action_row(connection, action_id)
    if action is None:
        raise ConsistencyError(f"取消目标动作不存在: {action_id}")
    kind = action["type"]
    if (kind not in _CAPTURE_TYPES and kind not in
            (_CANCEL_TASK_TYPE, _CLEANUP_TASK_TYPE, _REPORT_TASK_TYPE)):
        raise ConsistencyError(
            f"取消资格判断只适用已定义的目标类型: {action_id} type={kind!r}")
    if kind in (_CANCEL_TASK_TYPE, _CLEANUP_TASK_TYPE, _REPORT_TASK_TYPE):
        # 取消/清理/报告动作的收场按各自模块规则（停止等待、解除
        # 限制或同步责任分类）：终态保持、取消已生效则复用原责任，
        # 否则总是允许标记取消。
        return EligibilityFacts(
            terminal=action["status"] in _ACTION_TERMINAL,
            cancel_applied=bool(action["cancel_requested"]),
            dispatch=DispatchPhase.STARTED,
            stop_supported=True)
    activity = _activity_row(connection, action_id)
    return EligibilityFacts(
        terminal=action["status"] in _ACTION_TERMINAL,
        cancel_applied=bool(action["cancel_requested"]),
        dispatch=_dispatch_phase(action, activity),
        stop_supported=_stop_supported(action, kind, activity),
    )


def _action_row(connection, action_id: int) -> dict[str, Any] | None:
    return row_facts(connection, "actions", action_id)


def _activity_row(connection, action_id: int) -> dict[str, Any] | None:
    with closing(connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?", (action_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        return None
    return row_facts(connection, "device_activities", int(row[0]))


def _dispatch_phase(action, activity) -> DispatchPhase:
    if action["execution_started"] == 1:
        return DispatchPhase.STARTED
    if activity is None:
        return DispatchPhase.NOT_STARTED
    state = activity["dispatch_state"]
    if state == _DISPATCH_MAY_HAVE:
        return DispatchPhase.START_PENDING
    if state == _DISPATCH_RETURNED:
        return DispatchPhase.STARTED
    if state in (_DISPATCH_NOT_DISPATCHED, _DISPATCH_REJECTED):
        return DispatchPhase.NOT_STARTED
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
