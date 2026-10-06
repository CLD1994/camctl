"""历史快照维护的组件集成测试。

真实数据库线程与 SQLite 组合：候选按索引排序，依据读取在同一读
事务取得一致 S；交错写入不改写已取得的 S 与 S 处计数，保存按写
事务最新计数更新剩余次数；六类快照对象各自建档并与同边界独立
恢复比对；入队前超时只停用本次会话的维护，普通业务继续。
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import asyncio
import time

import pytest
import pytest_asyncio

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import new_operation_key
from camctl.history.snapshots import (
    MaintenanceContext,
    MaintenanceOutcome,
    MaintenanceState,
    SnapshotRef,
    decode_snapshot,
    maintain_snapshots,
    prepare_snapshot,
)
from camctl.persistence.models import DbJob, DbJobKind, DbOutcome, DbOutcomeKind, DbPriority
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.executor import DbExecutor
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..outputs.test_qualification import (
    _seed_action,
    _seed_device_file,
    _seed_output,
    _seed_selection_and_item,
)
from ..outputs.test_read_associations import _command
from .test_file_history import (
    _environment,
    _finish_output,
    _observe,
    _presence,
    _seed_processing,
    _seed_repair_pending,
    _start_repair,
    _submit_plan,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session(tmp_path: Path):
    owned, target = _environment(tmp_path)
    connection = owned.connection
    # 交付链前提：取回动作 31 引用动作 11 的产物 701 与设备文件 501。
    _seed_action(connection, 31, 1, action_type=4)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(
        connection, dependency_id=31, selection_id=31, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701)
    connection.commit()
    executor = DbExecutor(
        lambda: open_existing(target, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=8, enqueue_timeout_seconds=9.0)
    store = SqliteSnapshotStore(target, executor)
    try:
        yield SimpleNamespace(
            owned=owned, target=target, executor=executor, store=store,
            capture=CaptureRepository(), history=HistoryRepository(target))
    finally:
        await executor.close()
        owned.connection.close()


def _maintain(store, state=None, *, threshold: int = 1, batch_size: int = 16):
    return maintain_snapshots(MaintenanceContext(
        threshold=threshold, batch_size=batch_size,
        state=state if state is not None else MaintenanceState(), store=store))


def _value(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


async def test_all_object_types_snapshot_and_match_replay(session) -> None:
    """六类对象创建即达标：快照建档，内容与同边界独立恢复一致。"""
    owned, store, capture = session.owned, session.store, session.capture
    action_id = await _submit_plan(owned)
    plan_id = int(_value(owned, "SELECT plan_id FROM actions WHERE id=?",
                         action_id)[0])
    file_id = _observe(capture, owned, 11, "task-a/original.mp4")
    _seed_processing(owned.connection, 1, 11, file_id)
    _seed_repair_pending(capture, owned)
    repair_id = _start_repair(capture, owned)
    _finish_output(capture, owned, 11, file_id)
    grant = OutputsRepository().grant_file(_command(), new_operation_key(), owned)
    assert grant.kind.value == "completed", grant.error
    delivery_id = grant.value.delivery_id
    grant_target_id = grant.value.target_file_id

    result = await _maintain(store)
    assert result.outcome is MaintenanceOutcome.DRAINED
    saved_refs = {item.ref for item in result.saved}
    # 候选是全部实际建立维护进度的对象：受理创建的计划与动作、完成
    # 登记推进的动作与产物、交付创建推进的取回动作与交付、两类文
    # 件各自的创建与推进。六类对象全部出现。
    with closing(owned.connection.execute(
            "SELECT entity_type, entity_id FROM entity_snapshot_progress")) as cursor:
        all_progress = {
            SnapshotRef(int(row[0]), int(row[1])) for row in cursor.fetchall()}
    assert saved_refs == all_progress
    assert {ref.entity_type for ref in saved_refs} == {1, 2, 3, 4, 9, 10}
    assert SnapshotRef(4, plan_id) in saved_refs
    assert SnapshotRef(1, action_id) in saved_refs
    assert SnapshotRef(2, delivery_id) in saved_refs
    assert SnapshotRef(9, file_id) in saved_refs
    assert SnapshotRef(10, repair_id) in saved_refs
    assert SnapshotRef(10, grant_target_id) in saved_refs

    total = int(_value(
        owned, "SELECT COUNT(*) FROM entity_snapshots")[0])
    assert total == len(all_progress)
    for ref in all_progress:
        row = _value(
            owned,
            "SELECT s.boundary_event_id, s.change_count, p.pending_changes,"
            " p.snapshot_change_count FROM entity_snapshots s"
            " JOIN entity_snapshot_progress p ON p.latest_snapshot_id = s.id"
            " WHERE p.entity_type=? AND p.entity_id=?",
            ref.entity_type, ref.entity_id)
        assert row is not None, ref
        boundary_event, change_count, pending, snapshot_count = row
        assert pending == 0
        assert int(snapshot_count) == int(change_count)
        counted = _value(
            owned,
            "SELECT MAX(change_count) FROM entity_event_links"
            " WHERE entity_type=? AND entity_id=? AND event_id<=?",
            ref.entity_type, ref.entity_id, boundary_event)[0]
        assert int(counted) == int(change_count)

        content = _value(
            owned,
            "SELECT content FROM entity_snapshots WHERE entity_type=?"
            " AND entity_id=? AND boundary_event_id=?",
            ref.entity_type, ref.entity_id, boundary_event)[0]
        header, snapshot_rows = decode_snapshot(content, expect_ref=ref)
        assert header["boundary_event_id"] == int(boundary_event)
        txn_id = int(_value(
            owned,
            "SELECT id FROM history_transactions WHERE last_event_id=?",
            boundary_event)[0])
        repository = session.history
        entity_name = {
            1: "action", 2: "delivery", 3: "output", 4: "plan",
            9: "device_file", 10: "intermediate_file"}[ref.entity_type]
        replayed = repository.restore_entity(
            entity_name, ref.entity_id,
            HistoryBoundary(txn_id=txn_id, last_event_id=int(boundary_event)))
        restored = {
            (row.table, row.row_id): dict(row.values) for row in snapshot_rows}
        assert restored == replayed, ref


async def test_snapshot_preserves_later_changes_real_db(session) -> None:
    """S 处计数 5、保存前业务推进到 8：快照标记 5，剩余次数 3。"""
    owned, store, capture = session.owned, session.store, session.capture
    file_id = _observe(capture, owned, 11, "task-a/original.mp4")
    ref = SnapshotRef(9, file_id)
    # 观察计 1、确认来源 2、在场 3、缺席 4、完成 5。
    from .test_file_history import _complete, _confirm
    _confirm(capture, owned, file_id, 11)
    _presence(capture, owned, file_id, 2)
    _presence(capture, owned, file_id, 3)
    _complete(capture, owned, file_id)

    images = await store.load_entity_images((ref,))
    assert len(images) == 1
    assert images[0].change_count == 5
    prepared = prepare_snapshot(images[0])

    # 保存前业务继续变化三次：在场 3→2→3→2，最新计数 8。
    _presence(capture, owned, file_id, 2)
    _presence(capture, owned, file_id, 3)
    _presence(capture, owned, file_id, 2)

    saved = await store.save_snapshots((prepared,))
    assert saved[0].ref == ref
    assert saved[0].remaining_changes == 3
    progress = _value(
        owned,
        "SELECT current_change_count, snapshot_change_count, pending_changes"
        " FROM entity_snapshot_progress WHERE entity_type=9 AND entity_id=?",
        file_id)
    assert progress == (8, 5, 3)
    stored = _value(
        owned,
        "SELECT change_count FROM entity_snapshots WHERE entity_type=9"
        " AND entity_id=? ", file_id)
    assert int(stored[0]) == 5


async def test_candidate_order_follows_pending_type_and_id(session) -> None:
    """候选按剩余次数降序、类型标识字典序、ID 升序选取。"""
    owned, store, capture = session.owned, session.store, session.capture
    action_id = await _submit_plan(owned)
    plan_id = int(_value(owned, "SELECT plan_id FROM actions WHERE id=?",
                         action_id)[0])
    first = _observe(capture, owned, 11, "task-a/first.mp4")
    second = _observe(capture, owned, 11, "task-a/second.mp4")
    busy = _observe(capture, owned, 11, "task-a/busy.mp4")
    # busy 推进到 4 次，其余保持 1 次。
    _presence(capture, owned, busy, 2)
    _presence(capture, owned, busy, 3)
    _presence(capture, owned, busy, 2)

    candidates = await store.snapshot_candidates(threshold=1, limit=10)
    assert candidates == (
        SnapshotRef(9, busy),
        SnapshotRef(1, action_id),      # 同 Δ=1：action 字典序最先
        SnapshotRef(9, first),          # 同类型按 ID 升序
        SnapshotRef(9, second),
        SnapshotRef(4, plan_id),        # plan 字典序最后
    )


async def test_repeated_save_same_boundary_reuses_without_double_reduction(
        session) -> None:
    """同一对象与边界重复保存：核对内容一致复用，不重复建档扣减。"""
    owned, store, capture = session.owned, session.store, session.capture
    file_id = _observe(capture, owned, 11, "task-a/original.mp4")
    ref = SnapshotRef(9, file_id)
    images = await store.load_entity_images((ref,))
    prepared = prepare_snapshot(images[0])

    first = await store.save_snapshots((prepared,))
    assert first[0].remaining_changes == 0
    count_first = int(_value(
        owned, "SELECT COUNT(*) FROM entity_snapshots WHERE entity_type=9"
        " AND entity_id=?", file_id)[0])
    assert count_first == 1

    again = await store.save_snapshots((prepared,))
    assert again[0].remaining_changes == 0
    count_again = int(_value(
        owned, "SELECT COUNT(*) FROM entity_snapshots WHERE entity_type=9"
        " AND entity_id=?", file_id)[0])
    assert count_again == 1


async def test_enqueue_timeout_disables_session_and_business_continues(
        tmp_path: Path) -> None:
    """快照操作入队前超时停用本次会话；业务操作不受影响继续执行。"""
    owned, target = _environment(tmp_path)
    _seed_action(owned.connection, 31, 1, action_type=4)
    _seed_device_file(owned.connection, 501, 11)
    _seed_output(owned.connection, 701, 11, 501)
    _seed_selection_and_item(
        owned.connection, dependency_id=31, selection_id=31, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701)
    owned.connection.commit()
    capture = CaptureRepository()
    _observe(capture, owned, 11, "task-a/original.mp4")
    try:
        executor = DbExecutor(
            lambda: open_existing(target, DbOpenMode.EXISTING_RW, DbConfig()),
            capacity=1, enqueue_timeout_seconds=0.05)
        store = SqliteSnapshotStore(target, executor)

        def slow(owned_connection):
            time.sleep(0.4)
            return DbOutcome(kind=DbOutcomeKind.COMPLETED)

        def quick(owned_connection):
            return DbOutcome(kind=DbOutcomeKind.COMPLETED)

        def read_latest(owned_connection):
            with closing(owned_connection.connection.execute(
                    "SELECT MAX(id) FROM history_events")) as cursor:
                return cursor.fetchone()[0]

        holder = executor.submit_write(DbJob(
            key=new_operation_key(), description="占位慢写", kind=DbJobKind.WRITE,
            priority=DbPriority.BUSINESS, execute=slow))
        await asyncio.sleep(0.05)  # 让占位开始执行（占用线程，队列空位仍在）
        waiter = executor.submit_write(DbJob(
            key=new_operation_key(), description="占位等待", kind=DbJobKind.WRITE,
            priority=DbPriority.BUSINESS, execute=quick))

        state = MaintenanceState()
        result = await _maintain(store, state, threshold=1)
        assert result.outcome is MaintenanceOutcome.DISABLED
        assert state.disabled is True
        assert result.saved == ()

        # 停用后同一会话不再安排维护：达标候选留给后续会话。
        again = await _maintain(store, state, threshold=1)
        assert again.outcome is MaintenanceOutcome.DISABLED
        pending = int(_value(
            owned, "SELECT pending_changes FROM entity_snapshot_progress"
            " WHERE entity_type=9")[0])
        assert pending >= 1

        # 占位操作结束后，普通业务操作照常排队执行。
        outcomes = [await holder, await waiter]
        assert all(item.kind is DbOutcomeKind.COMPLETED for item in outcomes)
        business = executor.submit_read(DbJob(
            key=new_operation_key(), description="业务读取", kind=DbJobKind.READ,
            priority=DbPriority.BUSINESS, execute=read_latest))
        receipt = await business
        assert receipt.error is None
        assert receipt.value >= 1
        await executor.close()
    finally:
        owned.connection.close()
