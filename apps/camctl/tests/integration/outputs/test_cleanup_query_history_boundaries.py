"""清理查询事实在冻结边界 H 的三路径恢复一致性。

查询结果保存之后、本地成员结果之前冻结的边界只包含当时实际已
提交事实：初始回放、快照正向、当前逆向与路径选择的恢复结果一
致，后来的成员成功不改写冻结边界的历史或恢复结果。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import INITIAL_BOUNDARY, HistoryBoundary
from camctl.contracts.values import ConsistencyError
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, event_type_name
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.snapshots import SnapshotRef
from camctl.history.validators import ValidatedEvent
from camctl.outputs.cleanup_flow import delete_source_file
from camctl.persistence.executor import DbExecutor
from camctl.persistence.repositories.history import (
    HistoryRepository, SqliteSnapshotStore,
)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..bootstrap.test_output_binding_changes import _save_photos, environment
from ..history.test_object_closure import _business_rows
from .test_cleanup_query_cancel_before_delete import (
    _CancelQueryPort, _c1_unknown_delete_terminal,
)
from .test_cleanup_query_commit_recovery import _CommitFaultConnection
from .test_cleanup_query_save_recovery import (
    _MatrixDriver, _new_runtime, _start_cleanup,
)

pytestmark = pytest.mark.asyncio


class _CommittedQueryFault(OperationRepository):
    """目标 QUERY 结果事务真实提交后丢失完成回执；其余事务真实执行。"""

    def __init__(self, owned, item_id):
        self.owned, self.item_id = owned, item_id
        self.fault = None

    def finish_attempt(self, finish, key, owned):
        if (finish.ticket.operation != "query"
                or finish.ticket.target_id != str(self.item_id)):
            return super().finish_attempt(finish, key, owned)
        if self.fault is None:
            self.fault = _CommitFaultConnection(owned.connection, after_commit=True)
            return super().finish_attempt(finish, key, replace(owned, connection=self.fault))
        return super().finish_attempt(finish, key, owned)


def _entity_ref(name: str) -> int:
    return load_enum_registry()["history_objects"][name]["id"]


# 各表行归属的推导规则；值为（对象种类, 归属列或 None 表示自身主键），
# 尝试行沿所属流程的动作归属。
_OWNER_DERIVATION = {
    "actions": ("action", None),
    "cleanup_items": ("action", "action_id"),
    "operation_runs": ("action", "action_id"),
    "operation_attempts": ("action", "@run"),
    "device_activities": ("action", "action_id"),
    "outputs": ("output", None),
    "output_origins": ("output", "output_id"),
    "device_files": ("device_file", None),
    "intermediate_files": ("intermediate_file", None),
    "plans": ("plan", None),
}


def _owner_ref(connection, table: str, row_id: int):
    rule = _OWNER_DERIVATION.get(table)
    if rule is None:
        return None
    entity, column = rule
    if column == "@run":
        with closing(connection.execute(
                "SELECT r.action_id FROM operation_attempts a"
                " JOIN operation_runs r ON r.id = a.run_id WHERE a.id = ?",
                (row_id,))) as cursor:
            row = cursor.fetchone()
    else:
        with closing(connection.execute(
                f"SELECT {column or 'id'} FROM {table} WHERE id = ?",
                (row_id,))) as cursor:
            row = cursor.fetchone()
    if row is None:
        return None
    return (_entity_ref(entity), int(row[0]))


def _entity_events(connection, entity: str, entity_id: int, boundary):
    """按目录取得对象在边界前的全部事件并按行事实标注归属。"""
    ref = (_entity_ref(entity), entity_id)
    with closing(connection.execute(
            "SELECT e.id, e.transaction_id, e.event_type, e.event_version,"
            " e.occurred_at, e.clock_status, e.change_seq, e.body_json"
            " FROM entity_event_links l JOIN history_events e ON e.id = l.event_id"
            " WHERE l.entity_type = ? AND l.entity_id = ? AND l.event_id <= ?"
            " ORDER BY e.id", (*ref, boundary.last_event_id))) as cursor:
        stored = cursor.fetchall()
    events = []
    for row in stored:
        envelope = decode_event_row(row)
        owners = {}
        for change in envelope.rows:
            owner = _owner_ref(connection, change.table, change.row_id)
            if owner is not None:
                owners[(change.table, change.row_id)] = owner
        events.append(ValidatedEvent(
            envelope=envelope,
            event_name=event_type_name(envelope.event_type),
            branch_name=branch_of(envelope.event_type, envelope.reason)[0],
            references=(ref,),
            row_owners=owners,
        ))
    return events


def _image(entity: str, entity_id: int, rows, boundary: HistoryBoundary,
           change_count: int, *, exists: bool = True) -> EntityImage:
    return EntityImage(
        _entity_ref(entity), entity_id, exists, dict(rows),
        boundary.last_event_id, change_count)


async def test_cleanup_query_frozen_boundary_three_paths_match(environment):
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    outputs = dict(owned.connection.execute(
        "SELECT a.device_id, o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id IN ('cam-a','cam-b')").fetchall())
    driver = _MatrixDriver(owned)

    # 成员 B：仅自己的 QUERY，取消在结果保存后生效（原取消与限制低于 H）。
    await _c1_unknown_delete_terminal(cfg, owned, driver, outputs["cam-b"])
    b_action, b_item = _start_cleanup(owned, outputs["cam-b"], "3")
    b_runtime = _new_runtime(cfg, owned, driver)
    b_runtime.operations = _CancelQueryPort(owned, b_item, b_action, "after")
    driver.matrix_enabled, driver.present = True, None
    b_step = await delete_source_file(b_runtime.for_item(b_item), b_item)
    assert (b_step.phase, b_step.detail) == ("canceled", None), b_step

    # 成员 A：删除未知后查询确认缺席；结果事务真实提交后丢失回执。
    a_action, a_item = _start_cleanup(owned, outputs["cam-a"], "5")
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    history = HistoryRepository(path)
    a_runtime = _new_runtime(cfg, owned, driver)
    a_runtime.operations = _CommittedQueryFault(owned, a_item)
    executor = DbExecutor(
        lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    store = SqliteSnapshotStore(path, executor)
    try:
        seeds = dict(zip(
            ("action", "output"),
            await store.load_entity_images((
                SnapshotRef(_entity_ref("action"), a_action),
                SnapshotRef(_entity_ref("output"), outputs["cam-a"])))))
        driver.matrix_enabled, driver.present = True, False
        with pytest.raises(ConsistencyError):
            await delete_source_file(a_runtime.for_item(a_item), a_item)

        txn_id, last_event = owned.connection.execute(
            "SELECT id, last_event_id FROM history_transactions"
            " ORDER BY id DESC LIMIT 1").fetchone()
        frozen = HistoryBoundary(int(txn_id), int(last_event))
        frozen_history = tuple(owned.connection.execute(
            "SELECT id, body_json FROM history_events WHERE id <= ? ORDER BY id",
            (frozen.last_event_id,)))
        calls_before_resume = (len(driver.deletes), len(driver.queries))

        resumed = _new_runtime(cfg, owned, driver)
        c_step = await delete_source_file(resumed.for_item(a_item), a_item)

        assert (c_step.phase, c_step.detail) == ("succeeded", "ABSENCE_CONFIRMED"), c_step
        assert (len(driver.deletes), len(driver.queries)) == calls_before_resume
    finally:
        await executor.close()

    current = history.current_boundary()
    assert current.last_event_id > frozen.last_event_id
    # 后来的成功不改写冻结边界的历史。
    assert tuple(owned.connection.execute(
        "SELECT id, body_json FROM history_events WHERE id <= ? ORDER BY id",
        (frozen.last_event_id,))) == frozen_history

    connection = owned.connection
    for entity, entity_id in (("action", a_action), ("output", outputs["cam-a"])):
        events = _entity_events(connection, entity, entity_id, current)
        initial = restore(
            RestoreSeed(
                _image(entity, entity_id, {}, INITIAL_BOUNDARY, 0, exists=False),
                INITIAL_BOUNDARY),
            events, frozen)
        seed = seeds[entity]
        from_snapshot = restore(RestoreSeed(
            _image(entity, entity_id,
                   _business_rows({(row.table, row.row_id): row.values
                                   for row in seed.rows}),
                   seed.boundary, seed.change_count),
            seed.boundary), events, frozen)
        current_rows = _business_rows(
            history.restore_entity(entity, entity_id, current))
        counted = connection.execute(
            "SELECT MAX(change_count) FROM entity_event_links"
            " WHERE entity_type = ? AND entity_id = ?",
            (_entity_ref(entity), entity_id)).fetchone()[0]
        from_current = restore(RestoreSeed(
            _image(entity, entity_id, current_rows, current, int(counted)),
            current), events, frozen)
        selected = _business_rows(history.restore_entity(
            entity, entity_id, frozen, event_batch_size=1))
        assert from_snapshot.rows == initial.rows
        assert from_current.rows == initial.rows
        assert selected == _business_rows(initial.rows)

    restored_action = _business_rows(history.restore_entity(
        "action", a_action, frozen, event_batch_size=1))
    member = restored_action[("cleanup_items", a_item)]
    assert member["status"] == 3 and member["outcome"] is None
    assert member["final_event_id"] is None
    assert member["restriction_state"] == 4
    query_run_id = connection.execute(
        "SELECT id FROM operation_runs WHERE cleanup_item_id=? AND kind=5",
        (a_item,)).fetchone()[0]
    query_run = restored_action[("operation_runs", query_run_id)]
    assert query_run["status"] == 2 and query_run["attempts_used"] == 1
    query_attempt_id = connection.execute(
        "SELECT id FROM operation_attempts WHERE run_id=?", (query_run_id,)).fetchone()[0]
    query_attempt = restored_action[("operation_attempts", query_attempt_id)]
    assert query_attempt["result_event_id"] is not None
    assert query_attempt["result_json"] is not None

    restored_output = _business_rows(history.restore_entity(
        "output", outputs["cam-a"], frozen, event_batch_size=1))
    output_row = restored_output[("outputs", outputs["cam-a"])]
    assert output_row["availability"] != 3 and output_row["cleanup_status"] != 4

    # 成员 B 的原取消与限制完全低于冻结边界：H 与 C 恢复一致。
    assert _business_rows(history.restore_entity(
        "action", b_action, frozen)) == _business_rows(
        history.restore_entity("action", b_action, current))
    b_member = _business_rows(history.restore_entity(
        "action", b_action, frozen))[("cleanup_items", b_item)]
    assert b_member["status"] == 6 and b_member["restriction_state"] == 3
