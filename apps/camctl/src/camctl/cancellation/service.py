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
    CancelApplyMode,
    CancelItemProgress,
    CancelOutcomeChoice,
    CancelProgress,
    RecordCancelResult,
)
from camctl.cancellation.ports import TargetSettlementPort
from camctl.cancellation.rules import (
    CancelEligibility,
    decide_cancel_eligibility,
    load_eligibility_facts,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind

__all__ = ["ApplyCancel", "CancellationRuntime", "apply_cancel"]


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
            "SELECT status, cancellation_effect, target_action_id"
            " FROM cancel_items WHERE id = ?", (item_id,)).fetchone()
        _, effect, target_id = row
        if row[0] == 2 and effect == 2:
            # 取消已生效且目标尚未终态：推进本次有限收场。
            target_status = connection.execute(
                "SELECT status FROM actions WHERE id = ?",
                (target_id,)).fetchone()
            if target_status is not None and target_status[0] not in (3, 4, 5, 6):
                outcome = await runtime.settlement.settle(target_id)
                if outcome.complete:
                    if outcome.failed:
                        _completed(repository.record_cancel_result(
                            RecordCancelResult(
                                item_id, runtime.occurred_at(),
                                code="target_cleanup_failed",
                                details={"action_instance_id": str(target_id)}),
                            new_operation_key(), owned))
                    else:
                        _completed(repository.record_cancel_result(
                            RecordCancelResult(
                                item_id, runtime.occurred_at(),
                                outcome=CancelOutcomeChoice.CANCELED),
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
