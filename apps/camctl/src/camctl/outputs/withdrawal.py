"""已发布交付的实际撤回及完整原结果保存。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.host_files.handoff import HandoffIdentity, WithdrawStage, withdraw_file
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.outputs.handoff import AdvanceWithdrawal, WithdrawalChoice
from camctl.persistence.models import DbOutcomeKind

_WITHDRAWAL = enum_for("deliveries.withdrawal_state")
_DELIVERY = enum_for("deliveries.status")


@dataclass(frozen=True)
class PendingWithdrawal:
    """已固定的原输入；保存不确定时不重新执行外部副作用。"""

    action_id: int
    file_id: int
    file_name: str
    request: AdvanceWithdrawal
    key: OperationKey


@dataclass(frozen=True)
class WithdrawalContext:
    owned: Any
    repository: Any
    ready: Path
    processing: Path
    occurred_at: Callable[[], int]
    executor: FileTaskExecutor
    pending: dict[int, PendingWithdrawal]


def _identity(delivery_id: int, context: WithdrawalContext):
    rows = context.owned.connection.execute(
        "SELECT d.action_id,d.file_name,d.status,d.withdrawal_state,d.withdrawal_error_json,"
        " c.target_file_id,f.owner_delivery_id FROM deliveries d"
        " JOIN file_copies c ON c.delivery_id=d.id JOIN intermediate_files f ON f.id=c.target_file_id"
        " WHERE d.id=?", (delivery_id,)).fetchall()
    if len(rows) != 1 or rows[0][6] != delivery_id:
        raise ConsistencyError("交付撤回缺少原交付的唯一目标文件归属")
    if rows[0][2] not in (int(_DELIVERY.PUBLISHED), int(_DELIVERY.WITHDRAWN)):
        raise ConsistencyError("交付撤回要求可靠的原发布事实")
    HandoffIdentity(context.ready, rows[0][1])
    return rows[0]


def _position(name: str, context: WithdrawalContext) -> str:
    try:
        if (context.processing / name).exists():
            return "processing"
        if (context.ready / name).exists():
            return "ready"
    except OSError:
        return "unknown"
    return "unknown"


def _save(pending: PendingWithdrawal, context: WithdrawalContext) -> None:
    result = context.repository.advance_withdrawal(pending.request, pending.key, context.owned)
    if result.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"撤回原结果事务未可靠完成（{result.kind.value}）: {result.error}")
    context.pending.pop(pending.request.delivery_id)


def _remember(delivery_id, identity, choice, error, context):
    pending = PendingWithdrawal(identity[0], identity[5], identity[1],
        AdvanceWithdrawal(delivery_id, choice, context.occurred_at(), error), new_operation_key())
    context.pending[delivery_id] = pending
    _save(pending, context)


def _failure(delivery_id: int, error: str | None, *, unknown: bool) -> dict:
    return {"code": "withdrawal_unconfirmed" if unknown else "withdrawal_failed",
            "stage": "publication", "details": {"delivery_id": str(delivery_id),
                "error": error or "delivery_position_unconfirmed"}}


async def withdraw_delivery(delivery_id: int, context: WithdrawalContext) -> bool:
    """消费一份撤回，实际占用时保持未完成；取消后完成原保存。"""
    identity = _identity(delivery_id, context)
    pending = context.pending.get(delivery_id)
    if pending is not None and (pending.action_id, pending.file_id, pending.file_name) != (
            identity[0], identity[5], identity[1]):
        raise ConsistencyError("撤回待保存原结果与交付文件身份不同")
    if identity[5] in context.executor.unfinished_files():
        return False

    async def body(_control):
        if pending is not None:
            _save(pending, context)
            return
        state = identity[3]
        if state == int(_WITHDRAWAL.NOT_REQUESTED):
            _remember(delivery_id, identity, WithdrawalChoice.REQUESTED, None, context)
            state = int(_WITHDRAWAL.PENDING)
        if state != int(_WITHDRAWAL.PENDING):
            # 后到请求只采用既有独立责任结论，不第二次删除。
            choices = {_WITHDRAWAL.WITHDRAWN: WithdrawalChoice.WITHDRAWN,
                       _WITHDRAWAL.NOT_RETRACTABLE: WithdrawalChoice.NOT_RETRACTABLE,
                       _WITHDRAWAL.FAILED: WithdrawalChoice.FAILED,
                       _WITHDRAWAL.UNKNOWN: WithdrawalChoice.UNKNOWN}
            try:
                choice = choices[_WITHDRAWAL(state)]
            except ValueError as failure:
                raise ConsistencyError("原交付撤回状态无法解释") from failure
            from camctl.contracts.json_values import parse_exact_json
            error = parse_exact_json(identity[4]) if identity[4] is not None else None
            _remember(delivery_id, identity, choice, error, context)
            return
        position = await asyncio.to_thread(_position, identity[1], context)
        if position == "processing":
            choice, error = WithdrawalChoice.NOT_RETRACTABLE, None
        elif position == "ready":
            actual = await withdraw_file(HandoffIdentity(context.ready, identity[1]))
            if actual.stage is WithdrawStage.WITHDRAWN:
                choice, error = WithdrawalChoice.WITHDRAWN, None
            elif actual.stage is WithdrawStage.FAILED:
                choice, error = WithdrawalChoice.FAILED, _failure(delivery_id, actual.error, unknown=False)
            elif actual.stage is WithdrawStage.NOT_PRESENT and await asyncio.to_thread(
                    _position, identity[1], context) == "processing":
                choice, error = WithdrawalChoice.NOT_RETRACTABLE, None
            else:
                choice, error = WithdrawalChoice.UNKNOWN, _failure(delivery_id, actual.error, unknown=True)
        else:
            choice, error = WithdrawalChoice.UNKNOWN, _failure(delivery_id, None, unknown=True)
        _remember(delivery_id, identity, choice, error, context)

    await context.executor.run_owned_async_file_task(AsyncFileTask(
        FileTaskId(f"withdrawal/{delivery_id}/{new_operation_key()}"), (identity[5],),
        "delivery_withdrawal", "交付撤回实际结果及原事务保存", body, resources=("state_db",)))
    return True


async def resume_withdrawals(action_id: int, context: WithdrawalContext) -> bool:
    """先核已取得原结果，即使提交后当前等待明细已经为空。"""
    for delivery_id, pending in tuple(context.pending.items()):
        if pending.action_id == action_id and not await withdraw_delivery(delivery_id, context):
            return False
    return True
