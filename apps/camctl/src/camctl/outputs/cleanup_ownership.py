"""清理会话的原申请保存责任；集合只保存数据，不保存连接或回调。"""

from dataclasses import dataclass
from typing import Any

from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_outcome_for
from camctl.persistence.business import settle_required


@dataclass
class PendingCleanupSave:
    database_id: str
    method: str
    request: Any
    key: OperationKey
    last_kind: DbOutcomeKind | None = None


@dataclass(frozen=True)
class CleanupSaveNotApplicable:
    """原请求已可靠未生效且取消分支已失效；成员尚未保存。"""

    request: Any
    key: OperationKey


def verify_cleanup_transaction(owned, key):
    """在旧写事务不能迟到提交的边界核实完整原事务，不新增历史。"""
    connection = owned.connection
    if connection.in_transaction:
        raise ConsistencyError("原清理申请仍有未决事务，须先恢复可靠连接")
    from camctl.persistence.transaction import saved_transaction_events
    connection.execute("BEGIN IMMEDIATE")
    try:
        return saved_transaction_events(connection, key)
    finally:
        connection.execute("ROLLBACK")


async def _no_longer_applicable(runtime, pending):
    if pending.method != "fail_cleanup_binding" or pending.last_kind is None:
        return False
    row, in_transaction = await runtime.database.read(lambda owned: (
        owned.connection.execute(
            "SELECT a.cancel_requested,c.status FROM cleanup_items c"
            " JOIN actions a ON a.id=c.action_id WHERE c.id=?", (pending.request.item_id,)).fetchone(),
        owned.connection.in_transaction), description="核实清理申请当前适用性")
    if row is None or row[1] in (4, 5, 6) or bool(row[0]) == pending.request.canceled:
        return False
    if in_transaction:
        raise ConsistencyError("原清理成员申请仍有未决事务，不能处置为未生效")
    return await runtime.database.read(
        lambda owned: verify_cleanup_transaction(owned, pending.key),
        description="核实原清理申请未生效") is None


async def save_cleanup_request(runtime, method: str, request, *, action=False):
    """数据库回执与原申请的事件循环应用共同结束后才交付取消。"""
    return await settle_required(_save_cleanup_request(runtime, method, request, action=action))


async def _save_cleanup_request(runtime, method: str, request, *, action=False):
    """每个阶段持有一个原申请，可靠完成前不替换 key 或发生时刻。"""
    target_id = request.action_id if action else request.item_id
    identity = (method, target_id)
    pending = runtime.pending_saves.get(identity)
    database_id = runtime.database.metadata.instance_id
    if pending is None:
        pending = PendingCleanupSave(database_id, method, request, new_operation_key())
        runtime.pending_saves[identity] = pending
    elif pending.database_id != database_id or pending.method != method:
        raise ConsistencyError("原清理保存申请不属于本次数据库或保存阶段")
    if await _no_longer_applicable(runtime, pending):
        del runtime.pending_saves[identity]
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         CleanupSaveNotApplicable(pending.request, pending.key))

    def apply_completed(result):
        pending.last_kind = None
        from camctl.outputs.cleanup_flow import (
            CleanupItemSaved, CleanupStartResult, CleanupTargetsSaved, CleanupActionFinished)
        from camctl.persistence.repositories.outputs import ResolveSourcesOutcome
        expected = ({"start_cleanup_action": CleanupStartResult,
                     "resolve_sources": ResolveSourcesOutcome,
                     "fix_cleanup_targets": CleanupTargetsSaved,
                     "finish_cleanup_action": CleanupActionFinished}.get(method, CleanupItemSaved))
        if (not isinstance(result.value, expected)
                or (isinstance(result.value, CleanupItemSaved) and result.value.item_id != target_id)):
            raise ConsistencyError("原清理申请的可靠保存回执与阶段或目标不对应")
        del runtime.pending_saves[identity]

    try:
        result = await runtime.database.write(
            lambda owned: getattr(runtime.outputs, method)(pending.request, pending.key, owned),
            key=pending.key, description=f"清理保存:{method}")
    except BaseException as error:
        pending.last_kind = None
        actual = db_outcome_for(error)
        if actual is not None and actual.kind is DbOutcomeKind.COMPLETED:
            try:
                apply_completed(actual)
            except BaseException as application_error:
                raise error from application_error
        raise
    if not isinstance(result, DbOutcome):
        pending.last_kind = None
        raise ConsistencyError("清理保存未返回合法数据库结果")
    if result.kind is DbOutcomeKind.COMPLETED:
        apply_completed(result)
    else:
        pending.last_kind = result.kind
    return result
