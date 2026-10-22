"""固定来源提交边界上的分段检查副本复用与生命周期事实。

复用集合不从当前等待者重算：查询先读取分段来源已发布事件对应的
完整事务 H，再只接受 H 时正在运行且选择仍 PENDING 的取回动作。
之后创建的取回即使使用同一原片，也不会进入原检查副本的绑定集合。
"""

from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterator

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError, ObjectId, new_operation_key
from camctl.host_files.io import HashResult, hash_target
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.tasks import (
    AsyncFileTask, FileTaskError, FileTaskExecutor, FileTaskId,
)
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import read_transaction_range, row_facts
from camctl.outputs.read_discovery import DEFAULT_BATCH_SIZE


_ACTION_STATUS = enum_for("actions.status")
_SELECTION_STATUS = enum_for("obtain_source_selections.status")
_ACTION_TYPE = enum_for("actions.type")
_RESULT_PUBLICATION = enum_for("segment_sets.publication_kind")
_COPY_VERIFICATION = enum_for("file_copies.verification_state")
_FILE_RETENTION = enum_for("intermediate_files.retention_state")
_FILE_CLEANUP = enum_for("intermediate_files.cleanup_state")


@dataclass(frozen=True)
class BoundObtainWaiter:
    """来源提交边界 H 上固定的一个取回动作与其原选择。"""

    action_id: int
    selection_id: int


@dataclass(frozen=True)
class BoundCheckCopy:
    """固定等待取回可使用的一份完整分段检查副本。"""

    source_action_id: int
    source_device_file_id: int
    obtain_action_id: int
    selection_id: int
    published_boundary: HistoryBoundary
    inspection: object
    copy: object


def waiter_was_pending_at_boundary(*, action_status: int, selection_status: int) -> bool:
    """只把 H 时正在执行且仍等待本来源的选择绑定到检查副本。"""
    return (action_status == int(_ACTION_STATUS.RUNNING)
            and selection_status == int(_SELECTION_STATUS.PENDING))


def copy_is_reusable(facts) -> bool:
    """仅接受检查成功且完整、校验通过并仍处于来源保留状态的副本。"""
    if facts is None:
        return False
    inspection_state = enum_for("segment_inspections.state")
    return (
        facts.inspection_state == int(inspection_state.COMPLETED)
        and facts.verification_state in (
            int(_COPY_VERIFICATION.MATCHED),
            int(_COPY_VERIFICATION.SOURCE_CHECKSUM_UNAVAILABLE),
        )
        and facts.committed_bytes == facts.source_size
        and facts.retention_state == int(_FILE_RETENTION.REQUIRED)
        and facts.cleanup_state == int(_FILE_CLEANUP.NOT_NEEDED)
    )


@contextmanager
def _read_scope(connection) -> Iterator[None]:
    """单连接读取期间固定 H 与当前边界；沿用调用方已有事务。"""
    if connection.in_transaction:
        yield
        return
    with closing(connection.execute("BEGIN")):
        pass
    try:
        yield
        with closing(connection.execute("COMMIT")):
            pass
    except BaseException:
        connection.rollback()
        raise


def _current_boundary(connection) -> HistoryBoundary:
    with closing(connection.execute(
        "SELECT t.id,t.last_event_id FROM history_transactions t"
        " JOIN history_events e ON e.id=t.last_event_id"
        " ORDER BY t.id DESC LIMIT 1"
    )) as cursor:
        row = cursor.fetchone()
    with closing(connection.execute("SELECT MAX(id) FROM history_events")) as cursor:
        maximum_event = cursor.fetchone()[0]
    if row is None:
        if maximum_event is not None:
            raise ConsistencyError("固定复用截点发现未归组历史事件")
        return HistoryBoundary(0, 0)
    transaction = read_transaction_range(connection, row[0])
    if transaction.last_event_id != row[1] or row[1] != maximum_event:
        raise ConsistencyError("固定复用截点的当前历史边界不完整")
    return HistoryBoundary(transaction.txn_id, transaction.last_event_id)


def _publication_boundary(connection, published_event_id: int) -> HistoryBoundary:
    with closing(connection.execute(
        "SELECT transaction_id FROM history_events WHERE id=?",
        (published_event_id,),
    )) as cursor:
        event = cursor.fetchone()
    if event is None:
        raise ConsistencyError("分段结果发布标记缺少历史事件")
    transaction = read_transaction_range(connection, event[0])
    if transaction.last_event_id != published_event_id:
        raise ConsistencyError("分段结果发布标记不是其完整事务的末事件")
    return HistoryBoundary(transaction.txn_id, transaction.last_event_id)


def bound_waiters_at_publication(
        source_action_id: int, published_event_id: int, owned,
) -> Generator[BoundObtainWaiter, None, None]:
    """从不可变历史 H 重建当时实际等待该来源的正式取回集合。"""
    ObjectId(source_action_id)
    ObjectId(published_event_id)
    connection = owned.connection
    with _read_scope(connection):
        boundary = _publication_boundary(connection, published_event_id)
        current = _current_boundary(connection)
        if boundary.txn_id > current.txn_id:
            raise ConsistencyError("分段来源发布截点晚于当前完整历史")
        after_action_id = after_selection_id = 0
        while True:
            with closing(connection.execute(
                "SELECT a.id,d.id,s.id FROM actions a"
                " JOIN action_dependencies d ON d.action_id=a.id"
                " JOIN obtain_source_selections s ON s.dependency_id=d.id"
                " WHERE d.depends_on_action_id=? AND a.type=?"
                " AND (a.id>? OR (a.id=? AND s.id>?))"
                " AND a.created_event_id<=? ORDER BY a.id,s.id LIMIT ?",
                (source_action_id, int(_ACTION_TYPE.OBTAIN_ACTION_OUTPUTS),
                 after_action_id, after_action_id, after_selection_id,
                 boundary.last_event_id, DEFAULT_BATCH_SIZE),
            )) as cursor:
                rows = cursor.fetchall()
            if not rows:
                break
            for action_id, _dependency_id, selection_id in rows:
                action = row_facts(connection, "actions", action_id)
                selection = row_facts(
                    connection, "obtain_source_selections", selection_id)
                if action is None or selection is None:
                    raise ConsistencyError("固定等待取回的动作或来源选择缺失")
                action_at_h = read_row_values_at_boundary(
                    connection, owner=("action", action_id), table="actions",
                    row_id=action_id, columns=frozenset({"status"}),
                    current_values=action, boundary=boundary,
                    current_boundary=current)
                selection_at_h = read_row_values_at_boundary(
                    connection, owner=("action", action_id),
                    table="obtain_source_selections", row_id=selection_id,
                    columns=frozenset({"status"}), current_values=selection,
                    boundary=boundary, current_boundary=current)
                if waiter_was_pending_at_boundary(
                        action_status=action_at_h["status"],
                        selection_status=selection_at_h["status"]):
                    yield BoundObtainWaiter(int(action_id), int(selection_id))
            after_action_id = int(rows[-1][0])
            after_selection_id = int(rows[-1][2])


def _selection_is_bound_at_publication(
        *, source_action_id: int, obtain_action_id: int, selection_id: int,
        published_event_id: int, owned,
) -> tuple[HistoryBoundary, bool]:
    """按原依赖核对本次选择是否属于来源提交时的等待集合。"""
    connection = owned.connection
    selection = row_facts(connection, "obtain_source_selections", selection_id)
    if selection is None:
        raise ConsistencyError(f"取回来源选择缺失: {selection_id}")
    with closing(connection.execute(
        "SELECT action_id,depends_on_action_id FROM action_dependencies WHERE id=?",
        (selection["dependency_id"],),
    )) as cursor:
        dependency = cursor.fetchone()
    if dependency != (obtain_action_id, source_action_id):
        raise ConsistencyError("分段检查副本与本次取回的原来源依赖不一致")
    with closing(connection.execute(
        "SELECT transaction_id FROM history_events WHERE id=?",
        (published_event_id,),
    )) as cursor:
        marker = cursor.fetchone()
    if marker is None:
        raise ConsistencyError("分段来源发布标记缺少历史事件")
    transaction = read_transaction_range(connection, marker[0])
    if transaction.last_event_id != published_event_id:
        raise ConsistencyError("分段来源发布标记不是其完整事务的末事件")
    boundary = HistoryBoundary(transaction.txn_id, transaction.last_event_id)
    action = row_facts(connection, "actions", obtain_action_id)
    if action is None:
        raise ConsistencyError(f"取回动作缺失: {obtain_action_id}")
    if (action["type"] != int(_ACTION_TYPE.OBTAIN_ACTION_OUTPUTS)
            or action["created_event_id"] > boundary.last_event_id):
        return boundary, False
    current = _current_boundary(connection)
    action_at_h = read_row_values_at_boundary(
        connection, owner=("action", obtain_action_id), table="actions",
        row_id=obtain_action_id, columns=frozenset({"status"}),
        current_values=action, boundary=boundary, current_boundary=current)
    selection_at_h = read_row_values_at_boundary(
        connection, owner=("action", obtain_action_id),
        table="obtain_source_selections", row_id=selection_id,
        columns=frozenset({"status"}), current_values=selection,
        boundary=boundary, current_boundary=current)
    return boundary, waiter_was_pending_at_boundary(
        action_status=action_at_h["status"],
        selection_status=selection_at_h["status"])


def read_bound_check_copy(
        *, source_action_id: int, source_device_file_id: int,
        obtain_action_id: int, selection_id: int, owned,
) -> BoundCheckCopy | None:
    """读取本取回在 H 绑定的同原片完整检查副本；无效时返回空。"""
    ObjectId(source_action_id)
    ObjectId(source_device_file_id)
    ObjectId(obtain_action_id)
    ObjectId(selection_id)
    connection = owned.connection
    from camctl.persistence.repositories.segment_inspections import (
        read_inspection_for_member,
    )

    with _read_scope(connection):
        with closing(connection.execute(
            "SELECT m.id,s.published_event_id FROM segment_members m"
            " JOIN segment_sets s ON s.id=m.set_id"
            " WHERE s.action_id=? AND s.publication_kind=?"
            " AND m.file_id=? AND s.published_event_id IS NOT NULL LIMIT 2",
            (source_action_id, int(_RESULT_PUBLICATION.RESULT),
             source_device_file_id),
        )) as cursor:
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise ConsistencyError("同一原片对应多个已发布的分段结果成员")
        if not rows:
            return None
        member_id, published_event_id = rows[0]
        boundary, bound = _selection_is_bound_at_publication(
            source_action_id=source_action_id,
            obtain_action_id=obtain_action_id,
            selection_id=selection_id,
            published_event_id=published_event_id,
            owned=owned)
        if not bound:
            return None
        facts = read_inspection_for_member(
            source_action_id, member_id, owned)
        if facts.inspection is None or not copy_is_reusable(facts.copy):
            return None
        if facts.inspection.set_id != row_facts(
                connection, "segment_members", member_id)["set_id"]:
            raise ConsistencyError("检查副本与发布的分段集合不一致")
        if facts.copy.source_device_file_id != source_device_file_id:
            raise ConsistencyError("检查副本与正式产物的原片身份不一致")
        return BoundCheckCopy(
            source_action_id, source_device_file_id, obtain_action_id,
            selection_id, boundary, facts.inspection, facts.copy)


async def check_copy_is_locally_usable(
        copy, staging: Path, executor: FileTaskExecutor) -> bool:
    """在受管文件任务持有源文件期间，核对副本长度与登记摘要。"""
    if not copy_is_reusable(copy):
        return False
    ref = FileRef(
        file_id=copy.target_file_id,
        purpose=FilePurpose.RECORDING_INPUT,
        relative_path=copy.input_relative_path,
        root=staging,
    )
    async def hash_file(_control) -> HashResult:
        return await asyncio.to_thread(
            hash_target, ref, BoundDirectories(staging=staging))

    try:
        observed: HashResult = await executor.run_owned_async_file_task(AsyncFileTask(
            FileTaskId(f"segment_copy_hash/{new_operation_key()}"),
            (copy.target_file_id,), "segment_copy_hash", "取回来源资格校验", hash_file))
    except FileTaskError:
        # 文件已有受管操作时，不能证明本次可安全复用；维持原设备源
        # 候选，避免资格冲突让整个取回推进中断。
        return False
    return (
        observed.error is None
        and observed.size_bytes == copy.source_size
        and observed.digest == copy.target_sha256
    )


def all_bound_waiters_terminal(
        source_action_id: int, published_event_id: int, owned,
) -> bool:
    """检查 H 固定的每个取回都已终态；H 之后的新取回不延长保留。"""
    waiters = bound_waiters_at_publication(
        source_action_id, published_event_id, owned)
    terminal = {
        int(_ACTION_STATUS.SUCCEEDED), int(_ACTION_STATUS.FAILED),
        int(_ACTION_STATUS.EXPIRED), int(_ACTION_STATUS.CANCELED),
    }
    try:
        for waiter in waiters:
            with closing(owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (waiter.action_id,),
            )) as cursor:
                row = cursor.fetchone()
            if row is None:
                raise ConsistencyError(f"已绑定的取回动作缺失: {waiter.action_id}")
            if row[0] not in terminal:
                return False
        return True
    finally:
        waiters.close()
