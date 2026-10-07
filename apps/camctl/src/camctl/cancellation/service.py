"""取消生效编排：保存目标取消事实并组织逐目标独立收场。

对固定集合逐项按取消资格保存生效（或拒绝明细），已启动且支持停止
的目标经收场端口推进实际有限处理；取消动作只等待自身固定范围，原
停止、读取及撤回预算由目标流程复用。保存取消标记不是完成：仍在收
场的项保持处理中，逐项失败不放弃其他项的有限处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from camctl.cancellation.models import (
    ApplyCancelTarget,
    CancelActionDisposition,
    CancelActionFinished,
    CancelApplyMode,
    CancelItemProgress,
    CancelOriginFacts,
    CancelOutcomeChoice,
    CancelProgress,
    FinishCancelAction,
    OriginCancelDecision,
    RecordCancelResult,
    StopWaitCancelItems,
)
from camctl.cancellation.ports import TargetSettlementPort
from camctl.cancellation.rules import (
    CancelEligibility,
    decide_cancel_eligibility,
    decide_origin_cancel,
    load_eligibility_facts,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.transaction import TransactionError
from camctl.persistence.models import DbOutcomeKind

__all__ = [
    "ApplyCancel",
    "CancellationRuntime",
    "OriginSettle",
    "apply_cancel",
    "settle_origin_cancel",
]


@dataclass(frozen=True)
class ApplyCancel:
    """一次取消生效推进的输入：原发起者与固定集合成员。"""

    origin_action_id: int
    item_ids: tuple[int, ...]


@dataclass
class CancellationRuntime:
    """取消生效编排的端口集合。"""

    owned: Any
    repository: Any
    settlement: TargetSettlementPort
    occurred_at: Callable[[], int]


#: 资格分区到生效方式的映射（拒绝与不可靠不入表）。
_MODE_BY_ELIGIBILITY = {
    CancelEligibility.TERMINAL: CancelApplyMode.TERMINAL,
    CancelEligibility.ALREADY_CANCELED: CancelApplyMode.ALREADY,
    CancelEligibility.ALLOW_PRE_START: CancelApplyMode.PRE_START,
    CancelEligibility.ALLOW_WITH_STOP: CancelApplyMode.WITH_STOP,
}


async def apply_cancel(command: ApplyCancel, runtime: CancellationRuntime) -> CancelProgress:
    """推进一轮取消生效与逐目标收场，返回逐项进度快照。

    单项目标事实不可靠按一致性错误停止（不猜测）；一项拒绝或收场失
    败只保存该明细，不影响其他目标的适用处理。已在处理的项重入时
    沿用原状态继续收场，不重复施加取消。
    """
    repository = runtime.repository
    owned = runtime.owned
    connection = owned.connection
    progress: list[CancelItemProgress] = []
    for item_id in command.item_ids:
        occurred = runtime.occurred_at()
        row = connection.execute(
            "SELECT target_action_id, status, cancellation_effect"
            " FROM cancel_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ConsistencyError(f"取消成员不存在: {item_id}")
        target_id, status, effect = row
        if status == 1:
            eligibility = decide_cancel_eligibility(
                load_eligibility_facts(connection, target_id))
            if eligibility is CancelEligibility.REJECT_UNSUPPORTED:
                _completed(repository.record_cancel_result(
                    RecordCancelResult(
                        item_id, occurred, code="task_cancel_unsupported",
                        details={"action_instance_id": str(target_id)}),
                    new_operation_key(), owned))
            elif eligibility is CancelEligibility.UNVERIFIED:
                raise ConsistencyError(
                    f"目标取消事实不可靠，先核实再施加取消: {target_id}")
            else:
                _completed(repository.apply_cancel_target(
                    ApplyCancelTarget(
                        item_id, _MODE_BY_ELIGIBILITY[eligibility], occurred),
                    new_operation_key(), owned))
        row = connection.execute(
            "SELECT status, target_action_id FROM cancel_items"
            " WHERE id = ?", (item_id,)).fetchone()
        if row[0] == 2:
            # 成员处理中（取消已生效或终态目标的剩余收场）经结算
            # 端口按目标类型推进。端口自行判断剩余工作（拍摄的停
            # 止等待、取回的交付撤回、清理的成员收场与动作终态
            # 化），不以目标是否终态为前提；全部完成后按结算后的
            # 目标终态选择完成依据：目标以 canceled 结束证明取消
            # 达成，其他终态保持原结果。
            outcome = await runtime.settlement.settle(target_id)
            if outcome.complete:
                target_status = connection.execute(
                    "SELECT status FROM actions WHERE id = ?",
                    (target_id,)).fetchone()
                if target_status is None:
                    raise ConsistencyError(
                        f"取消目标动作不存在: {target_id}")
                if outcome.failed:
                    _completed(repository.record_cancel_result(
                        RecordCancelResult(
                            item_id, runtime.occurred_at(),
                            code="target_cleanup_failed",
                            details={"action_instance_id": str(target_id)}),
                        new_operation_key(), owned))
                else:
                    basis = (
                        CancelOutcomeChoice.CANCELED
                        if target_status[0] == 6
                        else CancelOutcomeChoice.ALREADY_TERMINAL)
                    _completed(repository.record_cancel_result(
                        RecordCancelResult(
                            item_id, runtime.occurred_at(), outcome=basis),
                        new_operation_key(), owned))
        final = connection.execute(
            "SELECT status, cancellation_effect, outcome, error_code,"
            " error_details_json, target_action_id FROM cancel_items"
            " WHERE id = ?", (item_id,)).fetchone()
        progress.append(CancelItemProgress(
            item_id=item_id, target_action_id=final[5], status=final[0],
            outcome=final[2], error_code=final[3],
            error_details_json=final[4]))
    return CancelProgress(items=tuple(progress))


def _completed(outcome) -> None:
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"取消生效事务未提交: {outcome.error}")


@dataclass(frozen=True)
class OriginSettle:
    """一次取消发起者自身收场的输入。"""

    origin_action_id: int


async def settle_origin_cancel(
        command: OriginSettle, runtime: CancellationRuntime):
    """取消发起者的自身收场：停止施加与等待，以 canceled 结束。

    按进度决策：已终态保留原结果；自身取消未生效时无收场可做；未
    确认事务先核实。自身取消已生效时未结束项转入取消收场（保留取
    消效果，目标责任独立继续），随后保存 canceled 终态。
    """
    from camctl.persistence.models import DbOutcome, DbOutcomeKind

    repository = runtime.repository
    owned = runtime.owned
    connection = owned.connection
    row = connection.execute(
        "SELECT status, cancel_requested FROM actions WHERE id = ?",
        (command.origin_action_id,)).fetchone()
    if row is None:
        raise ConsistencyError(f"取消动作不存在: {command.origin_action_id}")
    decision = decide_origin_cancel(CancelOriginFacts(
        origin_terminal=row[0] in (3, 4, 5, 6),
        origin_cancel_applied=bool(row[1])))
    if decision is OriginCancelDecision.VERIFY_FIRST:
        raise ConsistencyError(
            "取消发起者事实不可靠，先核实再收场"
            f": {command.origin_action_id}")
    if decision is OriginCancelDecision.CONTINUE:
        raise TransactionError(
            "自身取消未生效的取消动作无自身收场可做"
            f": {command.origin_action_id}")
    if decision is OriginCancelDecision.KEEP_TERMINAL:
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=CancelActionFinished(
            disposition=CancelActionDisposition.ALREADY,
            action_status=row[0], plan_status=0, succeeded=0, failed=0))
    occurred = runtime.occurred_at()
    stopped = repository.stop_wait_cancel_items(
        StopWaitCancelItems(command.origin_action_id, occurred),
        new_operation_key(), owned)
    if stopped.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"结束等待事务未提交: {stopped.error}")
    finished = repository.finish_origin_canceled(
        FinishCancelAction(command.origin_action_id, runtime.occurred_at()),
        new_operation_key(), owned)
    return finished
