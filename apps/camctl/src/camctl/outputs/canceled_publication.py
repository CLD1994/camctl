"""取消取回的原发布对账与撤回责任交接。"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.host_files.tasks import AsyncFileTask, FileTaskId
from camctl.logging_runtime.diagnostics import record_error
from camctl.outputs.handoff import (
    AdvanceWithdrawal, DeliveryDirectories, PublicationSaveRequest,
    WithdrawalChoice, _observe_locations, reconcile_canceled_publication,
)
from camctl.outputs.withdrawal import _failure, withdraw_delivery
from camctl.outputs.read_discovery import DEFAULT_BATCH_SIZE
from camctl.persistence.models import DbOutcomeKind, db_failure
from camctl.persistence.repositories.outputs import EnsureWithdrawalWaiter

_DELIVERY = enum_for("deliveries.status")
_WITHDRAWAL = enum_for("deliveries.withdrawal_state")


@dataclass(frozen=True)
class _PendingSave:
    action_id: int
    delivery_id: int
    method: str
    request: Any
    key: Any


@dataclass
class CanceledPublicationContext:
    database: Any
    repository: Any
    directories: DeliveryDirectories
    withdrawal: Any
    executor: Any
    occurred_at: Any
    pending: dict
    scans: dict = field(default_factory=dict)
    batch_size: int = DEFAULT_BATCH_SIZE
    wake: Any = None
    remaining: int | None = None
    waiter_remaining: int | None = None

    def __post_init__(self):
        if self.remaining is None:
            self.remaining = self.batch_size
        if self.waiter_remaining is None:
            self.waiter_remaining = self.batch_size


@dataclass
class _Scan:
    after: int = 0
    staging: dict = field(default_factory=dict)


async def _save(context, identity, action_id, delivery_id, method, request):
    pending = context.pending.get(identity)
    if pending is None:
        pending = _PendingSave(action_id, delivery_id, method, request, new_operation_key())
        context.pending[identity] = pending
    elif (pending.action_id, pending.delivery_id, pending.method) != (action_id, delivery_id, method):
        raise ConsistencyError("取消发布的原保存责任身份不一致")
    outcome = await context.database.write(
        lambda owned: getattr(context.repository, pending.method)(pending.request, pending.key, owned),
        key=pending.key, description="保存取消发布的原结果")
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, "取消发布的原结果未可靠保存", outcome)
    context.pending.pop(identity)
    return outcome.value


def _candidate_rows(owned, action_id, after, limit):
    with closing(owned.connection.execute(
        "SELECT d.id,c.id,c.target_file_id,d.withdrawal_state,d.status FROM deliveries d"
        " JOIN file_copies c ON c.delivery_id=d.id"
        " WHERE d.action_id=? AND d.id>? AND (d.status=? OR (d.status=? AND (d.withdrawal_state IN (?, ?)"
        " OR EXISTS (SELECT 1 FROM cancel_items ci JOIN actions origin ON origin.id=ci.action_id"
        " WHERE ci.target_action_id=d.action_id AND ci.status=2 AND origin.status=2"
        " AND origin.cancel_requested=0 AND NOT EXISTS (SELECT 1 FROM cancel_delivery_items cdi"
        " WHERE cdi.cancel_item_id=ci.id AND cdi.delivery_id=d.id AND cdi.status<>1)))))"
        " ORDER BY d.id,c.id LIMIT ?",
        (action_id, after, int(_DELIVERY.PUBLISHING), int(_DELIVERY.PUBLISHED),
         int(_WITHDRAWAL.NOT_REQUESTED), int(_WITHDRAWAL.PENDING), limit),
    )) as cursor:
        return cursor.fetchall()


def _waiter_ids(owned, action_id, delivery_id, limit):
    with closing(owned.connection.execute(
        "SELECT ci.id FROM cancel_items ci JOIN actions origin ON origin.id=ci.action_id"
        " WHERE ci.target_action_id=? AND ci.status=2 AND origin.status=2"
        " AND origin.cancel_requested=0 AND NOT EXISTS (SELECT 1 FROM cancel_delivery_items cdi"
        " WHERE cdi.cancel_item_id=ci.id AND cdi.delivery_id=?) ORDER BY ci.id LIMIT ?",
        (action_id, delivery_id, limit),
    )) as cursor:
        return tuple(int(row[0]) for row in cursor.fetchall())


def _locked_state(owned, repository, delivery_id):
    state = repository.load_delivery_state(delivery_id, owned)
    with closing(owned.connection.execute(
        "SELECT status,withdrawal_state FROM deliveries WHERE id=?", (delivery_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None or row[0] != state.facts.status:
        raise ConsistencyError("交付状态与原文件事实不一致")
    return state, int(row[0]), int(row[1])


async def reconcile_canceled_obtain(action_id: int, context: CanceledPublicationContext):
    """核实原发布中的各份文件；文件仍被占用时保留本轮责任。"""
    for identity, pending in tuple(context.pending.items()):
        if pending.action_id == action_id:
            await _save(context, identity, pending.action_id, pending.delivery_id,
                        pending.method, pending.request)
    scan = context.scans.setdefault(action_id, _Scan())
    limit = min(context.batch_size, context.remaining)
    if limit == 0:
        if context.wake is not None:
            context.wake()
        return None
    rows = await context.database.read(
        lambda owned: _candidate_rows(owned, action_id, scan.after, limit),
        description="发现取消取回的原发布责任")
    if len({int(row[0]) for row in rows}) != len(rows):
        raise ConsistencyError("取消发布的原交付对应多个拷贝")
    if any(int(row[2]) in context.executor.unfinished_files() for row in rows):
        return None
    for delivery_id, copy_id, file_id, withdrawal_state, status in rows:
        state = await context.database.read(
            lambda owned: context.repository.load_delivery_state(int(delivery_id), owned),
            description="读取取消发布的原交付")
        if state.action_id != action_id or state.target.file_id != file_id:
            raise ConsistencyError("取消发布观察的原文件身份不一致")

        async def confirm(identity):
            request = PublicationSaveRequest(identity, context.occurred_at())
            await _save(context, (identity, "publication"), action_id, identity,
                        "save_publication", request)

        async def unknown(identity, locations):
            failures = tuple(item.error for item in (locations.staging, locations.ready, locations.processing)
                             if isinstance(item.error, BaseException))
            for error in failures:
                record_error(error, stage="delivery_observation", context={"delivery_id": identity})
            detail = []
            present = []
            for name in ("staging", "ready", "processing"):
                observation = getattr(locations, name)
                if not observation.observed:
                    detail.append(f"{name} 观察失败: {observation.error}")
                elif observation.present:
                    present.append(name)
                    if observation.length != state.facts.prepared_size:
                        detail.append(f"{name} 长度与原准备记录不符")
                    if (observation.sha256 is not None
                            and observation.sha256 != state.facts.prepared_sha256):
                        detail.append(f"{name} 摘要与原准备记录不符")
                    if name != "staging" and observation.sha256 is None:
                        detail.append(f"{name} 缺少内容摘要")
            if len(present) > 1:
                detail.append("多个交接位置同时存在副本")
            if not present and not detail:
                detail.append("三个交接位置均无副本")
            if not detail:
                detail.append("原撤回结果尚未可靠保存")
            error = _failure(identity, "; ".join(detail), unknown=True)
            if withdrawal_state == int(_WITHDRAWAL.NOT_REQUESTED):
                request = AdvanceWithdrawal(identity, WithdrawalChoice.REQUESTED, context.occurred_at())
                await _save(context, (identity, "withdrawal_request"), action_id, identity,
                            "advance_withdrawal", request)
            request = AdvanceWithdrawal(identity, WithdrawalChoice.UNKNOWN, context.occurred_at(), error)
            await _save(context, (identity, "withdrawal_unknown"), action_id, identity,
                        "advance_withdrawal", request)

        async def waiters(identity):
            waiter_limit = min(context.batch_size, context.waiter_remaining)
            if waiter_limit == 0:
                return False
            parents = await context.database.read(
                lambda owned: _waiter_ids(owned, action_id, identity, waiter_limit),
                description="读取仍有效的取消交付等待者")
            for parent in parents:
                request = EnsureWithdrawalWaiter(parent, identity, context.occurred_at())
                await _save(context, (identity, "waiter", parent), action_id, identity,
                            "ensure_withdrawal_waiter", request)
                context.waiter_remaining -= 1
            return len(parents) < waiter_limit

        withdrawal_needed = []

        async def withdraw(identity):
            withdrawal_needed.append(identity)

        async def body(_control):
            nonlocal state, status, withdrawal_state
            state, status, withdrawal_state = await context.database.read(
                lambda owned: _locked_state(owned, context.repository, int(delivery_id)),
                description="持有原文件资格后核对取消发布状态")
            if state.action_id != action_id or state.target.file_id != file_id:
                raise ConsistencyError("持有文件资格后的交付身份发生变化")
            if status not in (int(_DELIVERY.PUBLISHING), int(_DELIVERY.PUBLISHED)):
                return None
            if status == int(_DELIVERY.PUBLISHED) or withdrawal_state == int(_WITHDRAWAL.UNKNOWN):
                if not await waiters(int(delivery_id)):
                    return False
                await withdraw(int(delivery_id))
                return None
            locations = await asyncio.to_thread(_observe_locations, state, context.directories)
            return await reconcile_canceled_publication(
                state.facts, locations, int(copy_id), int(file_id),
                confirm_handoff=confirm, save_unknown=unknown,
                ensure_waiters=waiters, withdraw=withdraw)

        result = await context.executor.run_owned_async_file_task(AsyncFileTask(
            FileTaskId(f"cancel-publication/{delivery_id}/{new_operation_key()}"),
            (int(file_id),), "cancel_publication", "取消发布的原文件对账", body,
            resources=("state_db",)))
        context.remaining -= 1
        if result is False:
            if context.wake is not None:
                context.wake()
            return None
        if result is not None:
            scan.staging[int(delivery_id)] = result
        for identity in withdrawal_needed:
            if not await withdraw_delivery(identity, context.withdrawal):
                return None
        scan.after = int(delivery_id)
    if len(rows) == limit:
        if context.wake is not None:
            context.wake()
        return None
    context.scans.pop(action_id)
    return tuple(scan.staging[identity] for identity in sorted(scan.staging))
