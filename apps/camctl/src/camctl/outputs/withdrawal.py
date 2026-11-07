"""已发布交付的实际撤回及完整原结果保存。"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from uuid import uuid4

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.host_files.handoff import HandoffIdentity, WithdrawStage, withdraw_file
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.outputs.handoff import AdvanceWithdrawal, WithdrawalChoice
from camctl.persistence.business import settle_required
from camctl.persistence.models import DbOutcomeKind, db_failure
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import canonical_directory_binding
from camctl.logging_runtime.models import LogLevel

if TYPE_CHECKING:
    from camctl.persistence.business import BusinessDatabase

_WITHDRAWAL = enum_for("deliveries.withdrawal_state")
_DELIVERY = enum_for("deliveries.status")


class WithdrawalProgress(Enum):
    WAITING_FOR_FILE = "waiting_for_file"
    STEP_FINISHED = "step_finished"


@dataclass(frozen=True)
class _DeliveryForWithdrawal:
    action_id: int
    file_name: str
    status: int
    withdrawal_state: int
    withdrawal_error: str | None
    file_id: int
    owner_delivery_id: int


@dataclass(frozen=True)
class _PendingWithdrawal:
    action_id: int
    file_id: int
    file_name: str
    request: AdvanceWithdrawal
    key: OperationKey


def _load_delivery_rows(delivery_id, owned):
    with closing(owned.connection.execute(
        "SELECT d.action_id,d.file_name,d.status,d.withdrawal_state,d.withdrawal_error_json,"
        " c.target_file_id,f.owner_delivery_id FROM deliveries d"
        " JOIN file_copies c ON c.delivery_id=d.id JOIN intermediate_files f ON f.id=c.target_file_id"
        " WHERE d.id=?", (delivery_id,))) as cursor:
        return cursor.fetchall()


def _observe_position(name: str, ready: Path, processing: Path) -> str:
    try:
        if (processing / name).exists():
            return "processing"
        if (ready / name).exists():
            return "ready"
    except OSError:
        return "unknown"
    return "unknown"


def build_withdrawal_error(delivery_id: int, error: str | None, *, unknown: bool) -> dict:
    return {"code": "withdrawal_unconfirmed" if unknown else "withdrawal_failed",
            "stage": "publication", "details": {"delivery_id": str(delivery_id),
                "error": error or "delivery_position_unconfirmed"}}


class WithdrawalSession:
    """一次运行内保留原撤回申请，文件任务使用调用方的共享执行器。"""

    def __init__(self, *, ready: Path, processing: Path, executor: FileTaskExecutor):
        self._ready = ready
        self._processing = processing
        self._executor = executor
        self._repository = OutputsRepository()
        self._pending: dict[int, _PendingWithdrawal] = {}
        self._database_id = None

    def _check_binding(self, database: BusinessDatabase) -> None:
        metadata = database.metadata
        if (metadata.ready_path != canonical_directory_binding(self._ready)
                or metadata.processing_path != canonical_directory_binding(self._processing)):
            raise ConsistencyError("撤回会话与状态库目录绑定不同")
        if self._database_id is None:
            self._database_id = metadata.instance_id
        elif self._database_id != metadata.instance_id:
            raise ConsistencyError("撤回会话属于另一状态库")

    async def _load_delivery(self, delivery_id, database):
        rows = await database.read(lambda owned: _load_delivery_rows(delivery_id, owned),
                                   description="交付撤回身份读取")
        if len(rows) != 1:
            raise ConsistencyError("交付撤回缺少原交付的唯一目标文件归属")
        delivery = _DeliveryForWithdrawal(*rows[0])
        if delivery.owner_delivery_id != delivery_id:
            raise ConsistencyError("交付撤回缺少原交付的唯一目标文件归属")
        if (delivery.status not in (int(_DELIVERY.PUBLISHED), int(_DELIVERY.WITHDRAWN))
                and not (delivery.status in (int(_DELIVERY.CANCELED), int(_DELIVERY.PUBLISHING))
                         and delivery.withdrawal_state == int(_WITHDRAWAL.UNKNOWN))):
            raise ConsistencyError("交付撤回要求可靠的原发布事实或已保存的位置未知结论")
        HandoffIdentity(self._ready, delivery.file_name)
        return delivery

    async def _save_pending(self, pending, database):
        async def save_and_apply():
            result = await database.write(
                lambda owned: self._repository.advance_withdrawal(pending.request, pending.key, owned),
                key=pending.key, description="交付撤回原结果保存")
            if result.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, "撤回原结果事务未可靠完成", result)
            self._pending.pop(pending.request.delivery_id)
        await settle_required(save_and_apply())

    async def _save_new_request(self, delivery_id, delivery, choice, error, database, occurred_at):
        pending = _PendingWithdrawal(delivery.action_id, delivery.file_id, delivery.file_name,
            AdvanceWithdrawal(delivery_id, choice, occurred_at(), error), new_operation_key())
        self._pending[delivery_id] = pending
        await self._save_pending(pending, database)

    async def withdraw_delivery(self, delivery_id: int, *, database: BusinessDatabase,
                               occurred_at: Callable[[], int]) -> WithdrawalProgress:
        self._check_binding(database)
        return await self._advance_withdrawal(delivery_id, database, occurred_at)

    async def resume_pending_saves(self, obtain_action_id: int, *, database: BusinessDatabase) -> WithdrawalProgress:
        self._check_binding(database)
        for delivery_id, pending in tuple(self._pending.items()):
            if pending.action_id == obtain_action_id:
                result = await self._advance_withdrawal(delivery_id, database, None)
                if result is WithdrawalProgress.WAITING_FOR_FILE:
                    return result
        return WithdrawalProgress.STEP_FINISHED

    async def _advance_withdrawal(self, delivery_id, database, occurred_at):
        delivery = await self._load_delivery(delivery_id, database)
        pending = self._pending.get(delivery_id)
        if pending is not None and (pending.action_id, pending.file_id, pending.file_name) != (
                delivery.action_id, delivery.file_id, delivery.file_name):
            raise ConsistencyError("撤回待保存原结果与交付文件身份不同")
        if delivery.file_id in self._executor.unfinished_files():
            return WithdrawalProgress.WAITING_FOR_FILE

        async def execute_withdrawal(_control):
            if pending is not None:
                await self._save_pending(pending, database)
                return
            state = delivery.withdrawal_state
            if state == int(_WITHDRAWAL.NOT_REQUESTED):
                await self._save_new_request(delivery_id, delivery, WithdrawalChoice.REQUESTED, None, database, occurred_at)
                state = int(_WITHDRAWAL.PENDING)
            if state != int(_WITHDRAWAL.PENDING):
                choices = {_WITHDRAWAL.WITHDRAWN: WithdrawalChoice.WITHDRAWN,
                           _WITHDRAWAL.NOT_RETRACTABLE: WithdrawalChoice.NOT_RETRACTABLE,
                           _WITHDRAWAL.FAILED: WithdrawalChoice.FAILED,
                           _WITHDRAWAL.UNKNOWN: WithdrawalChoice.UNKNOWN}
                try:
                    choice = choices[_WITHDRAWAL(state)]
                except (ValueError, KeyError) as failure:
                    raise ConsistencyError("原交付撤回状态无法解释") from failure
                from camctl.contracts.json_values import parse_exact_json
                error = parse_exact_json(delivery.withdrawal_error) if delivery.withdrawal_error is not None else None
                await self._save_new_request(delivery_id, delivery, choice, error, database, occurred_at)
                return
            position = await asyncio.to_thread(_observe_position, delivery.file_name, self._ready, self._processing)
            if position == "processing":
                choice, error = WithdrawalChoice.NOT_RETRACTABLE, None
            elif position == "ready":
                actual = await withdraw_file(HandoffIdentity(self._ready, delivery.file_name))
                if actual.stage is WithdrawStage.WITHDRAWN:
                    choice, error = WithdrawalChoice.WITHDRAWN, None
                elif actual.stage is WithdrawStage.FAILED:
                    choice, error = WithdrawalChoice.FAILED, build_withdrawal_error(delivery_id, actual.error, unknown=False)
                elif actual.stage is WithdrawStage.NOT_PRESENT and await asyncio.to_thread(
                        _observe_position, delivery.file_name, self._ready, self._processing) == "processing":
                    choice, error = WithdrawalChoice.NOT_RETRACTABLE, None
                else:
                    choice, error = WithdrawalChoice.UNKNOWN, build_withdrawal_error(delivery_id, actual.error, unknown=True)
            else:
                choice, error = WithdrawalChoice.UNKNOWN, build_withdrawal_error(delivery_id, None, unknown=True)
            await self._save_new_request(delivery_id, delivery, choice, error, database, occurred_at)

        await self._executor.run_owned_async_file_task(AsyncFileTask(
            FileTaskId(f"withdrawal/{delivery_id}/{uuid4()}"), (delivery.file_id,),
            "delivery_withdrawal", "交付撤回实际结果及原事务保存", execute_withdrawal, resources=("state_db",),
            log_level=None if pending is not None or delivery.withdrawal_state not in (
                int(_WITHDRAWAL.NOT_REQUESTED), int(_WITHDRAWAL.PENDING)) else LogLevel.INFO))
        return WithdrawalProgress.STEP_FINISHED
