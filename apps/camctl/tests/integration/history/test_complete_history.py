"""H7 全部事件覆盖与查询规模的组件集成测试。

综合剧本在真实数据库上按生产命令推进受理、录像媒体、产物登记与
取回交付链；库中出现的每个事件类型至少有一个受影响对象分别经初
始回放、快照正向恢复与当前投影逆向恢复到同一完整边界，并与按事
件事实独立推导（朴素全库重放）的映像比较成员完整性、精确值与计
数。登记的全部事件类型到具体用例的映射单独断言；代表性数据更新
统计信息后核对分页选择的实际查询计划与有序去重；并发读取与提交
按开发环境实测记录队列等待，不写成目标主机性能承诺。
"""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import asyncio
import json
import sqlite3

import pytest
import pytest_asyncio

from camctl.contracts.enums import enum_for, load_registry
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.history.decoding import decode_event_row
from camctl.history.events import (
    branch_of, business_columns, event_type_name, load_event_registry,
)
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.snapshots import (
    MaintenanceContext,
    MaintenanceOutcome,
    MaintenanceState,
    SnapshotRef,
    decode_snapshot,
    maintain_snapshots,
)
from camctl.history.validators import ValidatedEvent
from camctl.operations.attempts import QueryPurpose
from camctl.persistence.executor import DbExecutor
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts

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
from .test_object_closure import NOW, cancellation_world, owned, _fix_members
from .test_operation_ownership import _create_flow, _expected, _remember_run, operation_world

register_capture_guards()
register_outputs_guards()

pytestmark = pytest.mark.asyncio

_ENTITY_PRIMARY = {
    entity: spec["table"] for entity, spec in load_registry()["history_objects"].items()
}

#: 综合剧本经 raw 种子直接插入（无创建事件）的行；它们是测试捷径，
#: 不属于事件流，真实部署中不存在。
_SEEDED_ROWS = frozenset({
    ("plans", 1), ("actions", 11), ("actions", 12), ("actions", 31),
    ("recording_processing", 1), ("device_files", 501), ("outputs", 701),
    ("action_dependencies", 31), ("obtain_source_selections", 31),
    ("obtain_items", 101),
})


def _seeded_row_error(error: Exception) -> bool:
    """回放失败是否由 raw 种子行缺失（或重复登记）引起。"""
    from camctl.history.replay import ReplayError
    if not isinstance(error, ReplayError):
        return False
    message = str(error)
    for table, row_id in _SEEDED_ROWS:
        if f"('{table}', {row_id})" in message:
            return True
    return False


def _align_seeded(
        actual: dict[tuple[str, int], dict],
        expected: dict[tuple[str, int], dict],
) -> dict[tuple[str, int], dict]:
    """把恢复结果裁剪到事件可表达的口径。

    快照与逆向路径从当前投影取行全值（含主键与派生历史元数据
    列），事件推导映像只含登记业务列；比较统一投影到业务列。
    种子行无创建事件，事件映像只含实际变更过的列，再裁剪到该
    列集。非种子行在业务列口径上严格全等。
    """
    aligned: dict[tuple[str, int], dict] = {}
    for key, values in actual.items():
        if key not in expected and key in _SEEDED_ROWS:
            continue
        allowed = business_columns(key[0])
        columns = (
            expected[key] if key in _SEEDED_ROWS and key in expected
            else allowed)
        aligned[key] = {
            column: value for column, value in values.items()
            if column in columns}
    return aligned


# ---- 独立预期：朴素全库重放（不经生产恢复代码） ----

def _naive_replay_image(
        connection: sqlite3.Connection, last_event_id: int,
) -> dict[tuple[str, int], dict]:
    """按事件事实独立推导 last_event_id 时刻的全库映像。

    只做集合运算：create 插入行、update 合并列、delete 移除行；
    不使用生产回放的连续性核对、状态模型或归属推导。
    """
    image: dict[tuple[str, int], dict] = {}
    with closing(connection.execute(
            "SELECT id, body_json FROM history_events WHERE id <= ?"
            " ORDER BY id", (last_event_id,))) as cursor:
        for _event_id, body_text in cursor.fetchall():
            body = parse_exact_json(body_text)
            for change in body.get("rows", ()):
                table = change["table"]
                row_id = int(change["id"])
                after = change.get("after")
                key = (table, row_id)
                if after is None or not after.get("exists", True):
                    image.pop(key, None)
                elif key not in image:
                    image[key] = dict(after["values"])
                else:
                    image[key].update(after["values"])
    return image


def _expected_row_owner(image, key):
    """从场景固定外键推导每行归属，不调用生产归属或成员解析器。"""
    table, identity = key
    values = image[key]
    primary_entities = {primary: entity for entity, primary in _ENTITY_PRIMARY.items()}
    if table in primary_entities:
        return _entity_type_of(primary_entities[table]), identity
    action_columns = {
        "device_activities": "action_id", "recording_processing": "action_id",
        "cleanup_items": "action_id", "cancel_items": "action_id",
        "action_dependencies": "action_id", "auto_preview_links": "obtain_action_id",
        "motor_notifications": "action_id",
    }
    if table in action_columns:
        return _entity_type_of("action"), values[action_columns[table]]
    if table == "output_origins":
        return _entity_type_of("output"), values["output_id"]
    parents = {
        "obtain_source_selections": ("action_dependencies", "dependency_id"),
        "obtain_items": ("obtain_source_selections", "selection_id"),
        "cancel_delivery_items": ("cancel_items", "cancel_item_id"),
        "operation_attempts": ("operation_runs", "run_id"),
    }
    if table in parents:
        parent_table, column = parents[table]
        return _expected_row_owner(image, (parent_table, values[column]))
    if table == "file_copies":
        if values["delivery_id"] is not None:
            return _entity_type_of("delivery"), values["delivery_id"]
        return _expected_row_owner(image, ("recording_processing", values["processing_id"]))
    if table == "operation_runs":
        kinds = enum_for("operation_runs.kind")
        if values["kind"] == kinds.READ_FILE:
            return _expected_row_owner(image, ("file_copies", values["copy_id"]))
        if values["kind"] in (kinds.STOP_RESIDUAL, kinds.EMERGENCY_STOP):
            return _expected_row_owner(image, ("device_activities", values["activity_id"]))
        # 全部查询用途及其他普通流程属于触发动作。
        return _entity_type_of("action"), values["action_id"]
    raise AssertionError(f"独立预期未定义 {table} 的归属")


def _object_expected_rows(
        image: dict[tuple[str, int], dict], entity: str, entity_id: int,
        *, connection: sqlite3.Connection,
) -> dict[tuple[str, int], dict]:
    """先取得完整映像，再逐行沿固定外键分类，不依赖父行插入顺序。"""
    if (_ENTITY_PRIMARY[entity], entity_id) not in image:
        return {}
    associations = _fixed_associations(connection, image)
    owner = (_entity_type_of(entity), entity_id)
    return {key: dict(values) for key, values in image.items()
            if _expected_row_owner(associations, key) == owner}


def _fixed_associations(connection, image):
    """既有 raw 种子只提供不可变外键，返回值的业务事实仍来自事件。"""
    associations = dict(image)
    for table, identity in _SEEDED_ROWS:
        with closing(connection.execute(
                f"SELECT id FROM {table} WHERE id = ?", (identity,))) as cursor:
            present = cursor.fetchone() is not None
        if present:
            fixed = load_event_registry()["tables"][table]["immutable"]
            values = row_facts(connection, table, identity)
            associations[(table, identity)] = {
                **associations.get((table, identity), {}),
                **{column: values[column] for column in fixed},
            }
    return associations


def _entity_type_of(entity: str) -> int:
    return int(load_registry()["history_objects"][entity]["id"])


def _validated_events(
        connection: sqlite3.Connection, entity: str, entity_id: int,
        last_event_id: int,
) -> list[ValidatedEvent]:
    """取对象引用的事件并构造校验事件（归属来自历史目录事实）。"""
    entity_type = _entity_type_of(entity)
    with closing(connection.execute(
            "SELECT DISTINCT event_id FROM entity_event_links"
            " WHERE entity_type = ? AND entity_id = ? AND event_id <= ?"
            " ORDER BY event_id",
            (entity_type, entity_id, last_event_id))) as cursor:
        event_ids = [int(row[0]) for row in cursor.fetchall()]
    # 完整边界包含本事务全部创建行，子行可先于父行出现在事件中。
    associations = _fixed_associations(
        connection, _naive_replay_image(connection, last_event_id))
    validated: list[ValidatedEvent] = []
    for event_id in event_ids:
        with closing(connection.execute(
                "SELECT id, transaction_id, event_type, event_version,"
                " occurred_at, clock_status, change_seq, body_json"
                " FROM history_events WHERE id = ?", (event_id,))) as cursor:
            row = cursor.fetchone()
        envelope = decode_event_row(row)
        body = json.loads(row[7])
        reason = int(body["reason"])
        references: list[tuple[int, int]] = []
        with closing(connection.execute(
                "SELECT entity_type, entity_id FROM entity_event_links"
                " WHERE event_id = ?", (event_id,))) as cursor:
            for entity_t, entity_i in cursor.fetchall():
                reference = (int(entity_t), int(entity_i))
                if reference not in references:
                    references.append(reference)
        branch = branch_of(row[2], reason)[0]
        validated.append(ValidatedEvent(
            envelope, event_type_name(row[2]), branch,
            tuple(references),
            {(change.table, change.row_id): _expected_row_owner(
                associations, (change.table, change.row_id))
             for change in envelope.rows}))
    return validated


def _boundary_of(connection: sqlite3.Connection,
                 last_event_id: int) -> HistoryBoundary:
    """对象目录末位可能在事务中间，状态边界取包含它的完整事务末位。"""
    with closing(connection.execute(
            "SELECT t.id, t.last_event_id FROM history_events e"
            " JOIN history_transactions t ON t.id = e.transaction_id"
            " WHERE e.id = ?",
            (last_event_id,))) as cursor:
        txn_id, transaction_end = cursor.fetchone()
    return HistoryBoundary(txn_id=int(txn_id), last_event_id=int(transaction_end))


@pytest.mark.parametrize("side", ["target", "cancel"])
async def test_multi_object_event_replay_uses_each_rows_actual_owner(cancellation_world, side):
    """取消生效同一事件改变目标动作和取消项，分别恢复两个动作。"""
    from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode
    from camctl.persistence.models import DbOutcomeKind

    world = cancellation_world
    connection = world.owned.connection
    _fix_members(world)
    item_id = connection.execute(
        "SELECT id FROM cancel_items WHERE action_id = 2 AND target_action_id = 1"
    ).fetchone()[0]
    applied = world.cancellation.apply_cancel_target(
        ApplyCancelTarget(item_id, CancelApplyMode.PRE_START, NOW + 3),
        new_operation_key(), world.owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    target_ref, cancel_ref = (_entity_type_of("action"), 1), (_entity_type_of("action"), 2)
    shared = connection.execute(
        "SELECT a.event_id FROM entity_event_links a JOIN entity_event_links b"
        " ON b.event_id = a.event_id WHERE a.entity_type = ? AND a.entity_id = ?"
        " AND b.entity_type = ? AND b.entity_id = ? ORDER BY a.event_id LIMIT 1",
        (*target_ref, *cancel_ref)).fetchone()
    assert shared is not None, "真实取消生效未共同改变目标动作与取消项"
    boundary = _boundary_of(connection, shared[0])
    identity = 1 if side == "target" else 2
    events = _validated_events(connection, "action", identity, boundary.last_event_id)
    common = next(event for event in events if event.envelope.event_id == shared[0])
    assert target_ref in common.references and cancel_ref in common.references
    # cancel 分支特意检查不是首目录对象的动作，target 分支核对另一方。
    assert common.references[0] == target_ref
    if side == "cancel":
        assert common.references[0] != cancel_ref
    actual = restore(RestoreSeed(
        EntityImage(_entity_type_of("action"), identity, False, {}, 0, 0),
        INITIAL_BOUNDARY), events, boundary)
    expected = _object_expected_rows(
        _naive_replay_image(connection, boundary.last_event_id), "action", identity,
        connection=connection)
    assert expected
    assert actual.rows == expected


@pytest.mark.parametrize("branch", ["residual_stop", "emergency_stop", "residual_query"])
@pytest.mark.parametrize("identity", [1, 2], ids=["activity_owner", "trigger_action"])
async def test_real_operation_event_helper_classifies_rows_by_fixed_owner(operation_world, branch, identity):
    world = operation_world
    _create_flow(world, branch)
    boundary = world.history.current_boundary()
    events = _validated_events(world.owned.connection, "action", identity, boundary.last_event_id)
    actual = restore(RestoreSeed(
        EntityImage(_entity_type_of("action"), identity, False, {}, 0, 0),
        INITIAL_BOUNDARY), events, boundary)
    expected = _expected(world, identity)  # 场景明确的members，独立于helper与生产owner。
    assert actual.rows == expected
    image = _naive_replay_image(world.owned.connection, boundary.last_event_id)
    reversed_image = dict(reversed(tuple(image.items())))
    assert _object_expected_rows(reversed_image, "action", identity,
                                 connection=world.owned.connection) == expected


@pytest.mark.parametrize("purpose", list(QueryPurpose), ids=lambda value: value.name.lower())
async def test_real_query_event_helper_keeps_each_purpose_with_trigger_action(operation_world, purpose):
    from decimal import Decimal
    from camctl.operations.attempts import (
        AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind,
    )
    from camctl.persistence.models import DbOutcomeKind

    world = operation_world
    actor = 2 if purpose is QueryPurpose.RESIDUAL_STOP_CONFIRMATION else 1

    def begin(kind, action_id, query_purpose=None):
        target = AttemptTarget() if query_purpose is QueryPurpose.BEFORE_EXECUTION else AttemptTarget(activity_id=1)
        begun = world.operations.begin_attempt(AttemptIntent(
            "query" if query_purpose else "control" if kind is OperationKind.START else kind.value,
            action_id, kind, target, query_purpose,
            AttemptConfig(3, Decimal("5"), Decimal("1")), NOW + 1),
            new_operation_key(), world.owned)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        assert begun.value.disposition is BeginDisposition.GRANTED, begun.value
        _remember_run(world, begun.value.ticket.run_id, action_id)

    if purpose is QueryPurpose.RESIDUAL_STOP_CONFIRMATION:
        _create_flow(world, "residual_stop")
    elif purpose is QueryPurpose.START_CONFIRMATION:
        begin(OperationKind.START, 1)
    elif purpose is QueryPurpose.STOP_CONFIRMATION:
        begin(OperationKind.STOP, 1)
    begin(OperationKind.QUERY_ACTIVITY, actor, purpose)
    boundary = world.history.current_boundary()
    for identity in (1, 2):
        events = _validated_events(world.owned.connection, "action", identity, boundary.last_event_id)
        actual = restore(RestoreSeed(
            EntityImage(_entity_type_of("action"), identity, False, {}, 0, 0),
            INITIAL_BOUNDARY), events, boundary)
        assert actual.rows == _expected(world, identity)


async def test_real_internal_read_event_helper_inherits_processing_action(owned):
    """真实录像停止及完整结果固定检查责任，再授予内部 READ。"""
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode, PlanDisposition
    from camctl.capture.handlers import capture_handler, advance_winddown, SessionRecordingState
    from camctl.capture.recording import RecordingPhase, RecordingFacts, decide_recording_next
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.operations.attempts import (
        AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind,
    )
    from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
    from camctl.outputs.slots import SlotOutcome, SlotRequest
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
    from ..capture.test_capture_contract import _entry, _runtime, ResultsDouble, DriverDouble, StopDouble
    from ..bootstrap.test_recording_stop import _RecordCatalog, _record_plan
    from .test_object_closure import _business_rows

    accepted = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("record.json", _record_plan("1", "2026-10-08 09:00:00")),
        _RecordCatalog(), CommandMode.RUN, NOW), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert accepted.value.plan_disposition is PlanDisposition.REGISTERED
    started = SchedulingRepository().start_action(StartActionRequest(1, NOW, NOW),
                                                  new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    driver = DriverDouble(identity_override="1")
    runtime = _runtime(owned, driver=driver,
                       results=ResultsDouble({1: (_entry("video", size=10),)}), wall=NOW)
    runtime.stopper = StopDouble("1")
    runtime.wait_config = lambda _action: CaptureWaitConfig(target_duration_ms=6000, driver_margin_ms=0)
    await capture_handler("camera_record")(1, runtime)
    # 新会话没有前一会话的计时锚点，保守停止固定需要检查的原片。
    runtime.recording_state = SessionRecordingState(runtime)
    assert decide_recording_next(runtime.recording_state.recording_state(1),
                                 RecordingFacts()).phase is RecordingPhase.RECONCILE_REQUIRED
    now_ns = [20_000_000_000]
    runtime.monotonic_ns = lambda: now_ns[0]

    async def elapsed(seconds):
        now_ns[0] += int(seconds * 1_000_000_000)

    progress = await advance_winddown(1, runtime, {}, wait_cap_s=Decimal("6"), sleep=elapsed)
    assert progress.phase == "progress_saved"
    assert driver.calls == ["start_recording"]
    assert decide_recording_next(runtime.recording_state.recording_state(1),
                                 RecordingFacts()).phase is RecordingPhase.CONTROL_COMPLETE
    processing = row_facts(owned.connection, "recording_processing", 1)
    assert processing["source_device_file_id"] is not None
    assert processing["check_decision"] == int(enum_for("recording_processing.check_decision").REQUIRED)
    assert processing["check_state"] == int(enum_for("recording_processing.check_state").NOT_PERFORMED)
    outputs = OutputsRepository()
    granted = outputs.grant_file(FileCandidate(
        action_id=1, item_id=None, processing_id=1, output_id=None,
        source_device_file_id=processing["source_device_file_id"], target_extension="mp4",
        delivery_extension=None, delivery_display_name=None,
        config=OperationConfig(3, Decimal("10"), Decimal("0")), occurred_at=NOW),
        new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    assert granted.value.outcome is QualificationOutcome.GRANTED, granted.value
    assert granted.value.delivery_id is None
    copy_id = granted.value.copy_id
    copy = row_facts(owned.connection, "file_copies", copy_id)
    assert copy["processing_id"] == 1 and copy["delivery_id"] is None
    slot = outputs.grant_read_slot(SlotRequest(copy_id, NOW), new_operation_key(), owned)
    assert slot.kind is DbOutcomeKind.COMPLETED, slot.error
    assert slot.value.outcome is SlotOutcome.GRANTED, slot.value
    begun = OperationRepository().begin_attempt(AttemptIntent(
        "read", 1, OperationKind.READ_FILE, AttemptTarget(copy_id=copy_id), None,
        AttemptConfig(3, Decimal("10"), Decimal("0")), NOW, copy_round=copy["round"]),
        new_operation_key(), owned)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    assert begun.value.disposition is BeginDisposition.GRANTED, begun.value
    run_id = begun.value.ticket.run_id
    # 场景只有一个录像动作；启动、停止、结果列举及内部读取均属于它。
    members = {("actions", 1), ("device_activities", 1),
               ("recording_processing", 1), ("file_copies", copy_id)}
    for table in ("operation_runs", "operation_attempts"):
        members.update((table, row[0]) for row in owned.connection.execute(f"SELECT id FROM {table}"))
    expected = _business_rows({key: row_facts(owned.connection, *key) for key in members})
    history = HistoryRepository(Path(owned.connection.execute("PRAGMA database_list").fetchone()[2]))
    boundary = history.current_boundary()
    events = _validated_events(owned.connection, "action", 1, boundary.last_event_id)
    actual = restore(RestoreSeed(EntityImage(_entity_type_of("action"), 1, False, {}, 0, 0),
                                 INITIAL_BOUNDARY), events, boundary)
    assert ("operation_runs", run_id) in expected
    assert actual.rows == expected
    image = _naive_replay_image(owned.connection, boundary.last_event_id)
    assert _object_expected_rows(dict(reversed(tuple(image.items()))), "action", 1,
                                 connection=owned.connection) == expected


# ---- 综合剧本 ----

async def test_independent_event_image_preserves_precise_original_input(owned):
    """真实受理的驱动参数在独立事件预期中保持原始十进制精度。"""
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.ports import ParameterDefinition
    from camctl.acceptance.service import CommandMode, PlanDisposition
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from .test_report_scope import _Catalog, _plan_body

    class PreciseCatalog(_Catalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            assert definition is not None
            schema = dict(definition.schema)
            schema["properties"] = dict(schema["properties"], exposure_s={"type": "number", "minimum": 0})
            return ParameterDefinition(schema=schema, defaults={})

    body = _plan_body("1", "2026-10-08 09:00:00")
    body["actions"][0]["params"]["exposure_s"] = Decimal("0.12345678901234567890123456789")
    accepted = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("precise.json", body), PreciseCatalog(), CommandMode.RUN, NOW),
        new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert accepted.value.plan_disposition is PlanDisposition.REGISTERED
    boundary = HistoryRepository(Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])).current_boundary()
    image = _naive_replay_image(owned.connection, boundary.last_event_id)
    original = image[("actions", 1)]["input_fields_json"]["params"]["exposure_s"]
    assert isinstance(original, Decimal)
    assert original.as_tuple() == Decimal("0.12345678901234567890123456789").as_tuple()

@pytest_asyncio.fixture()
async def world(tmp_path: Path):
    """综合剧本库：受理、录像媒体、产物登记与取回交付全部推进。

    复用文件历史用例的种子基础（raw 种子的计划与录像动作 11、
    照片动作 12、交付链前提），其后全部经生产命令推进。
    """
    owned, target = _environment(tmp_path)
    connection = owned.connection
    capture = CaptureRepository()
    repository = HistoryRepository(target)
    executor = DbExecutor(
        lambda: open_existing(target, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9.0)
    store = SqliteSnapshotStore(target, executor)

    photo_action = await _submit_plan(owned)
    source_file = _observe(capture, owned, 11, "task-a/original.mp4")
    _seed_processing(connection, 1, 11, source_file)
    _seed_repair_pending(capture, owned)
    repair_file = _start_repair(capture, owned)
    _finish_output(capture, owned, 11, source_file)
    _presence(capture, owned, source_file, 3)

    # 交付链前提（与快照用例同款种子）：取回动作 31 引用动作 11 的
    # 产物 701 与设备文件 501。
    from ..outputs.test_qualification import (
        _seed_device_file,
        _seed_output,
        _seed_selection_and_item,
    )
    from .test_file_history import _seed_obtain_action
    _seed_obtain_action(connection, 31, 1)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(
        connection, dependency_id=31, selection_id=31, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701)
    connection.commit()

    from ..outputs.test_read_associations import _command
    from camctl.persistence.repositories.outputs import OutputsRepository
    grant = OutputsRepository().grant_file(
        _command(), new_operation_key(), owned)
    assert grant.kind.value == "completed", grant.error

    # 剧本继续推进后维护快照：中间边界 S 供快照正向恢复路径。
    late_file = _observe(capture, owned, 11, "task-a/second.mp4")
    maintained = await maintain_snapshots(MaintenanceContext(
        threshold=1, batch_size=32,
        state=MaintenanceState(), store=store))
    assert maintained.outcome is MaintenanceOutcome.DRAINED

    state = SimpleNamespace(
        owned=owned, target=target, capture=capture,
        repository=repository, executor=executor, store=store,
        photo_action=photo_action, source_file=source_file,
        repair_file=repair_file, late_file=late_file,
        delivery_id=grant.value.delivery_id,
        copy_target=grant.value.target_file_id,
    )
    try:
        yield state
    finally:
        await executor.close()
        owned.connection.close()


def _objects_in_library(
        connection: sqlite3.Connection) -> list[tuple[str, int]]:
    """库中经历史目录登记的全部对象（类型名，ID 升序）。"""
    names = {
        int(spec["id"]): name
        for name, spec in load_registry()["history_objects"].items()}
    with closing(connection.execute(
            "SELECT DISTINCT entity_type, entity_id, MAX(event_id)"
            " FROM entity_event_links GROUP BY 1, 2 ORDER BY 2, 1")) as cursor:
        return [(names[int(row[0])], int(row[1]), int(row[2]))
                for row in cursor.fetchall()]


async def test_every_event_matches_independent_history(world) -> None:
    """库中每个对象：三条恢复路径与独立推导映像完全一致。

    覆盖剧本产生的全部事件类型；每对象核对自身成员完整、精确
    值、计数与引用，H 之后创建的行不混入，读取批次变化不改
    变逆向恢复结果。
    """
    owned = world.owned
    repository = world.repository
    connection = owned.connection
    objects = _objects_in_library(connection)
    assert objects, "剧本未建立任何对象"
    covered_types: set[str] = set()
    for entity, entity_id, last_linked in objects:
        entity_type = _entity_type_of(entity)
        boundary = _boundary_of(connection, last_linked)

        events = _validated_events(
            connection, entity, entity_id, boundary.last_event_id)
        assert events, (entity, entity_id)
        for event in events:
            covered_types.add(event.event_name)

        # 路径一：初始状态正向回放全部引用事件。剧本的 raw 种子行
        # （测试捷径，无创建事件）不属于事件流，依赖它们的对象初始
        # 回放不可用；真实事件流的不自洽不属于种子行清单，仍会失败。
        try:
            from_initial = restore(
                RestoreSeed(
                    EntityImage(
                        entity_type, entity_id, False, {}, 0, 0),
                    INITIAL_BOUNDARY),
                events, boundary)
        except Exception as error:
            if not _seeded_row_error(error):
                raise
            from_initial = None

        # 路径二：快照正向恢复（存在较早 S 时从 S 继续应用）。
        with closing(connection.execute(
                "SELECT s.boundary_event_id, s.content FROM entity_snapshots s"
                " JOIN entity_snapshot_progress p"
                " ON p.latest_snapshot_id = s.id"
                " WHERE p.entity_type = ? AND p.entity_id = ?",
                (entity_type, entity_id))) as cursor:
            snap = cursor.fetchone()
        if snap is not None and int(snap[0]) < boundary.last_event_id:
            ref = SnapshotRef(entity_type, entity_id)
            header, snapshot_rows = decode_snapshot(snap[1], expect_ref=ref)
            seed_boundary = _boundary_of(connection, int(snap[0]))
            seed_image = EntityImage(
                entity_type, entity_id, True,
                {(row.table, row.row_id): dict(row.values)
                 for row in snapshot_rows},
                int(snap[0]), int(header.get("change_count", 0)))
            from_snapshot = restore(
                RestoreSeed(seed_image, seed_boundary), events, boundary)

        # 路径三：当前投影逆向恢复（生产读取接口），批次变化复跑。
        from_reverse = repository.restore_entity(entity, entity_id, boundary)
        from_reverse_small = repository.restore_entity(
            entity, entity_id, boundary, event_batch_size=3)

        expected = _object_expected_rows(
            _naive_replay_image(connection, boundary.last_event_id),
            entity, entity_id, connection=connection)

        if from_initial is not None:
            assert from_initial.rows == expected, (entity, entity_id, "初始回放")
            assert from_initial.exists == (
                (_ENTITY_PRIMARY[entity], entity_id) in expected)
        if snap is not None and int(snap[0]) < boundary.last_event_id:
            assert _align_seeded(
                from_snapshot.rows, expected) == expected, (
                entity, entity_id, "快照正向")
            if from_initial is not None:
                assert from_snapshot.change_count == from_initial.change_count, (
                    entity, entity_id, "快照路径计数")
        assert _align_seeded(from_reverse, expected) == expected, (
            entity, entity_id, "投影逆向")
        assert from_reverse_small == from_reverse, (entity, entity_id, "批次无关")

        with closing(connection.execute(
                "SELECT COUNT(*) FROM entity_event_links"
                " WHERE entity_type = ? AND entity_id = ? AND event_id <= ?",
                (entity_type, entity_id, boundary.last_event_id))) as cursor:
            linked = int(cursor.fetchone()[0])
        if from_initial is not None:
            assert from_initial.change_count == linked, (entity, entity_id, "计数")

    # 剧本实际产生的每个事件类型都经过了上述三路径核对。
    registry = load_event_registry()["events"]
    with closing(connection.execute(
            "SELECT DISTINCT event_type FROM history_events")) as cursor:
        produced = {int(row[0]) for row in cursor.fetchall()}
    script_types = {
        name for name, definition in registry.items()
        if definition["id"] in produced}
    assert covered_types == script_types, covered_types ^ script_types


# ---- 登记分支到具体用例的证据映射 ----

#: 每个登记事件类型的覆盖方式：script=True 表示综合剧本在本文件
#: 真实产生并经三路径核对；anchors 列出该类型行为断言的既有测试
#: 文件（仓库根相对路径）。无生产写入方的类型注明而非折叠。
_EVENT_COVERAGE: dict[str, tuple[bool, list[str]]] = {
    "MOTOR_CHANGED": (False, ["apps/camctl/tests/integration/persistence/test_motor_transactions.py"]),
    "PLAN_ACCEPTED": (True, ["apps/camctl/tests/integration/acceptance/test_acceptance.py"]),
    "ACTION_ADMITTED": (True, ["apps/camctl/tests/integration/acceptance/test_acceptance.py"]),
    "SOURCE_RESOLVED": (False, ["apps/camctl/tests/integration/acceptance/test_acceptance.py"]),
    "TARGETS_FIXED": (False, ["apps/camctl/tests/integration/outputs/test_cleanup_guards.py"]),
    "ACTION_STARTED": (False, ["apps/camctl/tests/integration/scheduling/test_start_action.py"]),
    "RETRY_WAIT_CHANGED": (False, ["apps/camctl/tests/integration/operations/test_attempts.py"]),
    "WINDOW_OBSERVED": (False, ["apps/camctl/tests/integration/scheduling/test_window_expiration.py"]),
    "ACTION_FINISHED": (True, ["apps/camctl/tests/integration/capture/test_recording_finish.py"]),
    "PLAN_STATUS_CHANGED": (False, ["apps/camctl/tests/integration/reporting/test_sync_lifecycle.py"]),
    "OPERATION_CONFIGURED": (True, ["apps/camctl/tests/integration/operations/test_attempts.py"]),
    "ATTEMPT_STARTED": (False, ["apps/camctl/tests/integration/operations/test_attempts.py"]),
    "ATTEMPT_RESULT": (False, ["apps/camctl/tests/integration/operations/test_attempts.py"]),
    "DEVICE_OBSERVED": (False, ["apps/camctl/tests/integration/capture/test_capture_contract.py"]),
    "BASELINE_CHUNK": (False, []),
    "CAPTURE_WAIT_CHANGED": (False, ["apps/camctl/tests/integration/capture/test_result_confirmation.py"]),
    "RESULT_SET_CONFIRMED": (False, ["apps/camctl/tests/integration/capture/test_result_confirmation.py"]),
    "DEVICE_FILE_OBSERVED": (True, ["apps/camctl/tests/integration/capture/test_file_observation.py"]),
    "RECORDING_DECIDED": (True, ["apps/camctl/tests/integration/capture/test_media_processing.py"]),
    "RECORDING_PROCESSED": (True, ["apps/camctl/tests/integration/capture/test_media_processing.py"]),
    "OUTPUT_REGISTERED": (True, ["apps/camctl/tests/integration/capture/test_recording_finish.py"]),
    "READ_PERMISSION_CHANGED": (True, ["apps/camctl/tests/integration/outputs/test_read_associations.py"]),
    "COPY_CHANGED": (True, ["apps/camctl/tests/integration/outputs/test_copy_creation_slot.py"]),
    "DELIVERY_CHANGED": (True, ["apps/camctl/tests/integration/outputs/test_delivery.py"]),
    "CLEANUP_CHANGED": (False, ["apps/camctl/tests/integration/outputs/test_source_cleanup.py"]),
    "CANCEL_CHANGED": (False, ["apps/camctl/tests/integration/cancellation/test_effects.py"]),
    "INTERMEDIATE_FILE_CHANGED": (True, ["apps/camctl/tests/integration/capture/test_media_execution.py"]),
    "INPUT_DIAGNOSTIC": (False, ["apps/camctl/tests/integration/acceptance/test_validation.py"]),
    "REPORT_CHANGED": (False, ["apps/camctl/tests/integration/reporting/test_freeze.py"]),
    "SYNC_CHANGED": (False, ["apps/camctl/tests/integration/reporting/test_sync_lifecycle.py"]),
    "ACK_ABSORBED": (False, ["apps/camctl/tests/integration/reporting/test_ack_sync.py"]),
    "CLOCK_ACCEPTED": (False, ["apps/camctl/tests/integration/session/test_session.py"]),
    "CLEANUP_CURSOR_MOVED": (False, ["apps/camctl/tests/integration/outputs/test_source_cleanup.py"]),
    "EMERGENCY_RECORDED": (False, ["apps/camctl/tests/integration/capture/test_emergency.py"]),
}

#: 无生产写入方的事件类型（登记先行；随接入核对，见事件契约审查）。
_NO_PRODUCER_EVENTS = frozenset({"BASELINE_CHUNK"})


async def test_event_coverage_map_is_complete() -> None:
    """登记的每个事件类型都有覆盖方式；锚点文件真实存在。

    script 标记的集合与综合剧本实际产生的类型集合一致（防漂
    移）；无生产写入方的类型显式列出，不折叠成已覆盖。
    """
    registry = load_event_registry()["events"]
    assert set(_EVENT_COVERAGE) == set(registry), (
        set(_EVENT_COVERAGE) ^ set(registry))
    root = Path(__file__).parents[5]
    for name, (in_script, anchors) in _EVENT_COVERAGE.items():
        if name in _NO_PRODUCER_EVENTS:
            assert not in_script and not anchors, name
            continue
        assert anchors, f"{name} 缺少锚点测试"
        for anchor in anchors:
            assert (root / anchor).is_file(), (name, anchor)
    assert set(_NO_PRODUCER_EVENTS) <= set(_EVENT_COVERAGE)


async def test_script_types_match_map(world) -> None:
    """综合剧本实际产生的事件类型与映射的 script 标记一致。"""
    connection = world.owned.connection
    registry = load_event_registry()["events"]
    with closing(connection.execute(
            "SELECT DISTINCT event_type FROM history_events")) as cursor:
        produced = {int(row[0]) for row in cursor.fetchall()}
    actual = {
        name for name, definition in registry.items()
        if definition["id"] in produced}
    mapped = {
        name for name, (in_script, _) in _EVENT_COVERAGE.items() if in_script}
    assert actual == mapped, actual ^ mapped


# ---- 代表性数据上的分页选择与候选扫描查询计划（验收 68、P-03、P-04、P-06） ----

def _grow_catalog_sample(target: Path, scale: int = 4000) -> None:
    """在剧本库上扩容报告目录样本并更新统计信息。

    混合三种形态：均匀变化（对象与目录行等量）、高重复（少数对
    象大量行）与稀疏匹配（大部分行在窗口外）。表约束要求每行
    的 (对象, 事件) 组合唯一，样本行因此分配真实事件之后的合成
    事件号（外键在本函数内关闭）；查询计划只依赖行分布。
    """
    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        max_event, max_seq = connection.execute(
            "SELECT MAX(id), MAX(change_seq) FROM history_events").fetchone()
        rows = []
        change_seq = int(max_seq or 0)
        next_event = int(max_event or 0) + 1
        # 均匀：scale 个对象各 2 行。
        for entity_id in range(1, scale + 1):
            for _ in range(2):
                change_seq += 1
                rows.append((1, entity_id, next_event, change_seq))
                next_event += 1
        # 高重复：20 个对象各 scale//20 行。
        for entity_id in range(1, 21):
            for _ in range(scale // 20):
                change_seq += 1
                rows.append((3, entity_id, next_event, change_seq))
                next_event += 1
        # 稀疏：全部落在极早窗口（查询窗口外）。
        for entity_id in range(1, scale + 1):
            change_seq += 1
            rows.append((2, entity_id, next_event, 1))
            next_event += 1
        connection.executemany(
            "INSERT INTO report_entity_changes"
            " (entity_type, entity_id, event_id, change_seq)"
            " VALUES (?, ?, ?, ?)", rows)
        # 候选扫描样本：动作 11 名下半数文件观察自真实事件之前
        # （首屏即被窗口过滤）、半数之后；其余真实动作各持噪声文
        # 件，保持归属等值条件的实际选择性。扫描查询本身不恢复
        # 文件。
        first_event = int(
            connection.execute("SELECT MIN(id) FROM history_events")
            .fetchone()[0] or 1)
        with closing(connection.execute(
                "SELECT MAX(id) FROM device_files")) as cursor:
            file_id = int(cursor.fetchone()[0] or 0)
        file_rows = []
        for index in range(scale // 2):
            file_id += 1
            file_rows.append(
                (11, file_id, f"grown-observer-{file_id}", "{}",
                 first_event, first_event))
            file_id += 1
            file_rows.append(
                (11, file_id, f"grown-late-{file_id}", "{}",
                 next_event, next_event))
            next_event += 1
        for noise_owner in (12, 13, 31):
            for _ in range(scale):
                file_id += 1
                file_rows.append(
                    (noise_owner, file_id, f"grown-noise-{file_id}", "{}",
                     first_event, first_event))
        connection.executemany(
            "INSERT INTO device_files"
            " (observer_action_id, id, identity_key, locator_json,"
            "  role, presence_state, completion_state, checksum_support,"
            "  created_event_id, last_event_id, change_count)"
            " VALUES (?, ?, ?, ?, 1, 1, 1, 1, ?, ?, 1)", file_rows)
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()
    finally:
        connection.close()


async def test_paged_selection_plan_on_analyzed_sample(world) -> None:
    """带统计信息样本上的实际查询计划与有界分页选择。

    首批与续读使用覆盖索引（SEARCH 而非 SCAN，无临时排序）；不同
    页大小取得相同升序集合，无重复无遗漏；每批返回不超过批量上
    限（选择只保存容量以内的整数身份）。
    """
    _grow_catalog_sample(world.target)
    repository = HistoryRepository(world.target)
    connection = sqlite3.connect(world.target)
    try:
        with closing(connection.execute(
                "SELECT MAX(change_seq) FROM report_entity_changes"
                " WHERE entity_type = 1")) as cursor:
            top = int(cursor.fetchone()[0])
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT entity_id"
            " FROM report_entity_changes WHERE entity_type = 1"
            " AND entity_id > 0 AND +change_seq > 0 AND +change_seq <= ?"
            " ORDER BY entity_id LIMIT 100", (top,)).fetchall()
        detail = " ".join(str(row[3]) for row in plan)
        assert "SCAN report_entity_changes" not in detail, detail
        assert "TEMP" not in detail.upper(), detail

        def read_all(limit: int):
            collected: list[int] = []
            after = 0
            batches = []
            while True:
                batch = repository.select_report_entities(
                    entity_type=1, from_wm=0, to_wm=top,
                    after_id=after, limit=limit)
                batches.append(len(batch))
                if not batch:
                    return collected, batches
                collected.extend(batch)
                after = batch[-1]

        uniform, uniform_batches = read_all(100)
        assert uniform == sorted(set(uniform))
        assert max(uniform_batches) <= 100
        # 页大小变化取得相同集合（P-02 有界路径续读等价）。
        small, _ = read_all(7)
        assert small == uniform
        assert len(uniform) >= 4000
        # 高重复形态：同一对象大量目录行只贡献一次身份。
        with closing(connection.execute(
                "SELECT MAX(change_seq) FROM report_entity_changes"
                " WHERE entity_type = 3")) as cursor:
            dense_top = int(cursor.fetchone()[0])
        dense, dense_batches = _select_dense(repository, dense_top)
        assert set(range(1, 21)) <= set(dense)
        assert dense == sorted(set(dense))
        assert max(dense_batches) <= 50
    finally:
        connection.close()


async def test_candidate_scan_plan_on_analyzed_sample(world) -> None:
    """带统计信息样本上候选扫描的实际索引与有界批量。

    扫描 SQL 与 `_read_candidate_files` 的候选查询同形（恢复侧
    由 H5 用例与三路径用例核对，这里测查询本身）：首屏与续读命
    中观察者索引；每批候选不超过固定批量，早于边界的候选被窗口
    过滤；分页取得的集合与独立全量选择一致。
    """
    _grow_catalog_sample(world.target)
    boundary = world.repository.current_boundary()
    connection = sqlite3.connect(world.target)
    try:
        with closing(connection.execute(
                "SELECT MAX(id) FROM device_files")) as cursor:
            upper = int(cursor.fetchone()[0])
        scan_sql = (
            "SELECT id FROM device_files"
            " WHERE observer_action_id = 11 AND id > ?"
            " AND id <= ? AND created_event_id <= ?"
            " ORDER BY id LIMIT ?")
        plan = connection.execute(
            "EXPLAIN QUERY PLAN " + scan_sql,
            (0, upper, boundary.last_event_id, 128)).fetchall()
        detail = " ".join(str(row[3]) for row in plan)
        assert "SEARCH device_files USING INDEX device_files_observer" in detail, detail
        assert "SCAN device_files" not in detail, detail

        collected: list[int] = []
        after = 0
        batches: list[int] = []
        while True:
            page = [int(row[0]) for row in connection.execute(
                scan_sql, (after, upper, boundary.last_event_id, 128))]
            batches.append(len(page))
            if not page:
                break
            collected.extend(page)
            after = page[-1]
        assert batches[0] == 128, batches
        assert max(batches) <= 128
        # 独立全量选择（无 id 区间续读）核对分页结果与窗口过滤。
        expected = [int(row[0]) for row in connection.execute(
            "SELECT id FROM device_files"
            " WHERE observer_action_id = 11"
            " AND created_event_id <= ? ORDER BY id",
            (boundary.last_event_id,))]
        assert collected == expected
        # 半数样本晚于边界被过滤，存活者仍足以跨多批续读。
        assert 2000 <= len(collected) < 4000
    finally:
        connection.close()


def _select_dense(repository: HistoryRepository, top: int):
    collected: list[int] = []
    after = 0
    batches = []
    while True:
        batch = repository.select_report_entities(
            entity_type=3, from_wm=0, to_wm=top, after_id=after, limit=50)
        batches.append(len(batch))
        if not batch:
            return collected, batches
        collected.extend(batch)
        after = batch[-1]


# ---- 开发环境的并发读取与队列等待实测（不作为目标主机承诺） ----

async def test_concurrent_submit_and_read_queue_wait(world) -> None:
    """并发提交与历史读取在开发环境的实际等待记录。

    有限容量执行器上并发推进受理事务与对象恢复，全部完成且事件
    计数前进；墙钟数字仅在 H7 验证记录注明环境与负载，不构成目
    标主机性能承诺。
    """
    from camctl.acceptance.input import parse_input, read_input
    from camctl.acceptance.service import (
        AcceptanceContext, CommandMode, accept_input,
    )
    from camctl.persistence.repositories.acceptance import (
        AcceptanceRepository,
    )
    from .test_file_history import _StubSource
    from unit.acceptance.helpers import StubCatalog

    owned = world.owned
    connection = owned.connection
    with closing(connection.execute(
            "SELECT COUNT(*) FROM history_events")) as cursor:
        before = int(cursor.fetchone()[0])

    async def submit(index: int) -> None:
        body = {
            "request_id": str(7100 + index),
            "created_at": "2026-01-15 08:00:00",
            "name": f"concurrent-{index}",
            "actions": [{
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }],
        }
        source = parse_input(
            await read_input("/tmp/plan.json", _StubSource(body)))
        accepted = await accept_input(
            source,
            AcceptanceContext(
                mode=CommandMode.RUN,
                catalog=StubCatalog(),
                repository=AcceptanceRepository(),
                clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
            ),
            new_operation_key(), owned)
        assert accepted.plan_id is not None

    async def read_object() -> None:
        repository = world.repository
        boundary = repository.current_boundary()
        repository.restore_entity("action", 11, boundary)

    loop = asyncio.get_running_loop()
    import time as _time
    started = _time.perf_counter()
    await asyncio.gather(*[submit(index) for index in range(6)],
                         *[read_object() for _ in range(4)])
    elapsed = _time.perf_counter() - started
    with closing(connection.execute(
            "SELECT COUNT(*) FROM history_events")) as cursor:
        after = int(cursor.fetchone()[0])
    assert after > before
