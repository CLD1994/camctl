"""通过正式会话装配创建产物协作者，不复制业务推进。"""

from decimal import Decimal
from datetime import datetime, timezone
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path
import sqlite3

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.bootstrap.obtain_assembly import session_obtain_assembly
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration

from .database import business_database, database_path


_current_cleanup_assembly = ContextVar("output_cleanup_assembly", default=None)


@contextmanager
def output_test_session():
    """由测试会话拥有正式装配共享的原保存责任，业务用例只提供外部事实。"""
    state = {name: {} for name in (
        "pending_queries", "pending_companions", "pending_intents", "pending_deletes",
        "pending_saves", "consumed_queries",
    )}
    state["active_calls"] = set()
    token = _current_cleanup_assembly.set(state)
    try:
        yield
    finally:
        _current_cleanup_assembly.reset(token)


def cleanup_factory(**options):
    """以生产工厂保存会话责任；新连接、新配置不替换已经取得的原结果。"""
    state = _current_cleanup_assembly.get()
    if state is None:
        raise RuntimeError("清理测试须使用 output_test_session 资源作用域")
    return session_cleanup_assembly(**options, **state)


def prepare_cancel_target(owned, *, catalog, now, action_id, request_id="4"):
    """在存储专项的同一线程明确保存取消寻址与生效，不重入数据库队列。"""
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.cancellation.models import (
        ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixCancelTargets,
        FixedCancelSet, FixedTarget, SelectionBasis, StartCancelAction,
    )
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from camctl.persistence.repositories.cancellation import CancellationRepository

    accepted = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("cancel.json", {
            "request_id": request_id, "created_at": "2026-01-15 08:00:00", "name": "取消清理",
            "actions": [{"name": "取消当前清理", "type": "cancel_task",
                         "scheduled_at": "2026-01-15 09:00:00",
                         "params": {"target": {"action_instance_id": str(action_id)}}}],
        }), catalog, CommandMode.RUN, now), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert accepted.value.plan_id is not None, accepted.value
    origin, = owned.connection.execute(
        "SELECT id FROM actions WHERE plan_id=? AND type=6", (accepted.value.plan_id,),
    ).fetchone()
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(origin, now), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(
        origin, FixedCancelSet((FixedTarget(action_id, SelectionBasis.DIRECT,
                                           CancellationEffect.NOT_APPLIED),)), now),
        new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    applied = repository.apply_cancel_target(ApplyCancelTarget(
        fixed.value.item_ids[0], CancelApplyMode.WITH_STOP, now), new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error


async def submit_output_document(database, *, catalog, body, occurred_at):
    """经正式受理入口提交产物业务输入，返回已完整提交的结果。"""
    from types import SimpleNamespace
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.acceptance import AcceptanceRepository

    return await accept_input(
        ParsedInput("outputs.json", body),
        AcceptanceContext(CommandMode.RUN, catalog, AcceptanceRepository(),
                          clock=SimpleNamespace(utc_micros=lambda: occurred_at)),
        new_operation_key(), database,
    )


def prepare_obtain_selection(owned, *, catalog, now, source_id, mode, request_id,
                             exclude_output_ids=None):
    """为选择和报告存储专项建立已受理、已开始且来源固定的取回。

    本入口只准备正式存储契约规定的前置事实；取回功能场景使用
    accept_input 和 obtain_flow，文件准备与发布仍由生产流程执行。
    """
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.sources import read_source_spec
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from camctl.persistence.repositories.outputs import (
        OutputsRepository, ResolveSources, StartObtainAction,
    )

    instant = datetime.fromtimestamp(now // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    params = {"source": source_id if isinstance(source_id, dict)
              else {"action_instance_id": str(source_id)}}
    if mode == "preview":
        params["filter"] = "preview"
    elif isinstance(mode, list):
        params["output_ids"] = [str(identity) for identity in mode]
    if exclude_output_ids is not None:
        params["exclude_output_ids"] = [str(identity) for identity in exclude_output_ids]
    accepted = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("obtain.json", {
            "request_id": str(request_id), "created_at": instant, "name": "分段取回",
            "actions": [{"name": "取回", "type": "obtain_action_outputs",
                         "scheduled_at": instant, "params": params}],
        }), catalog, CommandMode.RUN, now), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert accepted.value.plan_id is not None, accepted.value
    action_id, fields = owned.connection.execute(
        "SELECT id,input_fields_json FROM actions WHERE plan_id=? AND type=4",
        (accepted.value.plan_id,),
    ).fetchone()
    repository = OutputsRepository()
    for method, request in (
        (repository.start_obtain_action, StartObtainAction(action_id, now)),
        (repository.resolve_sources, ResolveSources(action_id, read_source_spec(fields), now)),
    ):
        saved = method(request, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    selection, = owned.connection.execute(
        "SELECT s.id FROM obtain_source_selections s JOIN action_dependencies d"
        " ON d.id=s.dependency_id WHERE d.action_id=?", (action_id,),
    ).fetchone()
    return action_id, selection


def prepare_cleanup_member(owned, *, catalog, now, output_id, request_id):
    """为清理存储专项建立受理、开始、目标固定及可撤销限制。"""
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.cleanup_flow import (
        FixCleanupTargets, RestrictCleanupItem, StartCleanupAction,
    )
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from camctl.persistence.repositories.outputs import OutputsRepository

    accepted = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("plan.json", {
            "request_id": str(request_id), "created_at": "2026-01-15 08:00:00", "name": "双设备",
            "actions": [{"name": "清理目标照片", "type": "delete_action_outputs",
                         "scheduled_at": "2026-01-15 09:00:00",
                         "params": {"output_ids": [str(output_id)]}}],
        }), catalog, CommandMode.RUN, now), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert accepted.value.plan_id is not None, accepted.value
    action_id, = owned.connection.execute(
        "SELECT id FROM actions WHERE plan_id=? AND type=5", (accepted.value.plan_id,),
    ).fetchone()
    repository = OutputsRepository()
    started = repository.start_cleanup_action(
        StartCleanupAction(action_id, now), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cleanup_targets(
        FixCleanupTargets(action_id, now), new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    item_id, = fixed.value.item_ids
    restricted = repository.restrict_cleanup_item(
        RestrictCleanupItem(item_id, now), new_operation_key(), owned)
    assert restricted.kind is DbOutcomeKind.COMPLETED, restricted.error
    return action_id, item_id


async def segmented_output_source(tmp_path, *, catalog, entries, known=True,
                                  finalized=True, pending_capture_group=None):
    """以正式拍摄处理器消费 START、STOP 和完整分页证据，建立报告来源。"""
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.capture.handlers import capture_handler
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.ports import DeviceCallResult
    from camctl.operations.attempts import AttemptConfig

    from .capture import capture_runtime
    from ..capture.result_consumer_fixtures import consumer_world, returned
    from ..capture.test_capture_contract import _NOW
    from ..capture.test_record_file_completion_wait import (
        _REGISTRY, _completed_result, _stop_context,
    )

    state = {"wall": _NOW, "mono": 5_000_000_000}
    world = {}

    class Stop:
        async def stop(self, _request):
            activity, = world["owned"].connection.execute(
                "SELECT id FROM device_activities WHERE action_id=?", (world["action"],),
            ).fetchone()
            return DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", activity))

    class Results:
        async def list_results(self, request, _batch):
            return _completed_result(
                request.ticket, _stop_context(world["owned"], world["action"]),
                entries=entries, finalized=finalized,
            )

    options = {
        "results": DriverResultListing(Results(), DeviceBinding("cam-1", "camctl-adb"), _REGISTRY),
        "stopper": Stop(), "evidence": _REGISTRY,
        "check_config": AttemptConfig(1, Decimal("1"), Decimal(0)),
        "wall_us": lambda: state["wall"], "monotonic_ns": lambda: state["mono"],
    }
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=catalog, start=False,
        pending_capture_group=pending_capture_group, runtime_options=options,
    )
    world.update(owned=owned, action=action_id)
    await capture_handler(handler)(action_id, runtime)
    if not known:
        # 新工厂只读取持久事实，无法取得上一次工厂的连续控制锚点。
        runtime = await capture_runtime(owned, driver=runtime.driver, **options)
    state.update(wall=_NOW + 12_000_000, mono=17_000_000_000)
    await capture_handler(handler)(action_id, runtime)
    return owned, runtime, action_id


async def output_operation(owned, operation, identity, context_type, /, **options):
    """以真实数据库线程调用一个正式文件操作，完整关闭该次资源。"""
    async with business_database(database_path(owned)) as database:
        return await operation(identity, context_type(database=database, **options))


class SqliteOperationFault:
    """在 SQLite 实际授权边界拒绝指定表操作，不依赖 SQL 书写格式。"""

    def __init__(self, connection, operation, table):
        self.connection, self.operation, self.table = connection, operation, table
        self.triggered = False

    def execute(self, sql, parameters=()):
        def authorize(operation, table, _column, _database, _source):
            if operation == self.operation and table == self.table:
                self.triggered = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(authorize)
        try:
            return self.connection.execute(sql, parameters)
        finally:
            self.connection.set_authorizer(None)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def observed_query(connection, sql, parameters=()):
    """用 SQLite 提供的表列读取范围和真实游标字段识别存储观察。"""
    reads = set()

    def authorize(operation, table, column, _database, _source):
        if operation == sqlite3.SQLITE_READ:
            reads.add((table, column))
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        cursor = connection.execute(sql, parameters)
    finally:
        connection.set_authorizer(None)
    columns = frozenset(item[0] for item in cursor.description or ())
    return cursor, columns, frozenset(reads)


@asynccontextmanager
async def sqlite_operation_fault(database, operation, table):
    """在原真实 worker 安装 SQLite 授权故障，退出后恢复实际连接。"""
    state = {"triggered": False}

    def authorize(actual, actual_table, _column, _database, _source):
        if actual == operation and actual_table == table:
            state["triggered"] = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    await database.read(lambda owned: owned.connection.set_authorizer(authorize),
                        description="安装测试SQLite操作故障")
    try:
        yield state
    finally:
        await database.read(lambda owned: owned.connection.set_authorizer(None),
                            description="恢复测试SQLite授权器")


def output_drivers(driver, evidence, *, driver_id="camctl-adb", read=False,
                   digest=False, delete=False, query=False, local_read_recovery=False):
    """按用例明确声明的文件能力登记受契约约束的设备替身。"""
    return DriverRegistry((DriverEntry(
        driver_id, driver, DriverDeclaration(
            control_supported=False, stop_supported=False, query_supported=False,
            result_supported=False, read_supported=read, digest_supported=digest,
            delete_supported=delete, file_query_supported=query,
            local_read_recovery_supported=local_read_recovery),
        evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))


async def cleanup_runtime(database, driver, evidence, *, item_id,
                          device_id="cam-1", driver_id="camctl-adb",
                          delete_attempts=1, query_attempts=1,
                          delete_interval=Decimal("1"), query_interval=Decimal("1"),
                          occurred_at, monotonic_ns=None, staging=None):
    """窄删除协作仍由正式工厂解析原绑定、证据、预算及本次配置。"""
    factory = session_cleanup_assembly(
        devices={device_id: {"driver": driver_id, "cleanup": {
            "delete_timeout_s": Decimal("10"), "query_timeout_s": Decimal("10"),
            "delete_retry_interval_s": delete_interval,
            "query_retry_interval_s": query_interval,
        }}},
        drivers=output_drivers(driver, evidence, driver_id=driver_id,
                               delete=True, query=True),
        max_delete_attempts=delete_attempts, max_query_attempts=query_attempts,
        staging=staging, occurred_at=occurred_at, monotonic_ns=monotonic_ns,
    )
    runtime = await factory(database)
    return await runtime.for_item(item_id)


def obtain_factory(*, devices, drivers, staging: Path, ready: Path,
                   processing: Path, occurred_at, monotonic_ns=None,
                   segment_size=1024, **options):
    """使用当前配置创建取回会话，工厂拥有跨轮次的原保存责任。"""
    return session_obtain_assembly(
        devices=devices, drivers=drivers, staging=staging, ready=ready,
        processing=processing, occurred_at=occurred_at,
        monotonic_ns=monotonic_ns, segment_size=segment_size, **options)


@asynccontextmanager
async def read_test_runtime(owned, driver, evidence, *, staging, internal=False,
                              max_read_attempts=3, segment_size=5,
                              occurred_at, device_id="cam-1", driver_id="camctl-adb",
                              read_interval=Decimal(0), file_executor=None, **options):
    """为取回和录像媒体读取提供正式工厂与实际文件执行器的作用域。"""
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor
    from .capture import capture_database

    for relative in ("deliveries", "recording-inputs", "derived"):
        (Path(staging) / relative).mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor()
    executor = file_executor or FileTaskExecutor(supervisor)
    devices = {device_id: {"driver": driver_id, "copy": {
        "max_read_attempts": max_read_attempts, "read_idle_timeout_s": Decimal(10),
        "retry_interval_s": read_interval}}}
    registry = output_drivers(driver, evidence, driver_id=driver_id, read=True)
    if internal:
        from dataclasses import replace
        from camctl.bootstrap.capture_assembly import session_capture_assembly
        entry = registry.entry(driver_id)
        registry = DriverRegistry((replace(entry, declaration=replace(entry.declaration,
            control_supported=True)),))
        capture_factory = session_capture_assembly(devices=devices, drivers=registry,
            staging=Path(staging), wait_config=lambda _: None, wall_us=occurred_at,
            monotonic_ns=lambda: 0, segment_size=segment_size,
            file_executor=executor, **options)
        capture = await capture_factory(await capture_database(owned), device_id)
        assert capture is not None and capture.media is not None
        runtime = capture.media
    else:
        factory = obtain_factory(devices=devices, drivers=registry,
            staging=Path(staging), ready=Path(staging).parent / "ready",
            processing=Path(staging).parent / "processing", segment_size=segment_size,
            occurred_at=occurred_at, monotonic_ns=lambda: 0,
            file_executor=executor, **options)
        runtime = factory(await capture_database(owned))
    try:
        yield runtime
    finally:
        results = await supervisor.drain_required()
        failures = tuple(result.exception or RuntimeError(result.error) for result in results.failed)
        if failures:
            raise BaseExceptionGroup("取回专项文件责任收场失败", failures)
