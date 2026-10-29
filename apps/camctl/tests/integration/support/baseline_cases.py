"""基准准备与原尝试恢复的正式工厂、时钟和资源作用域。"""

from contextvars import ContextVar
from contextlib import ExitStack
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.host_files.tasks import FileTaskExecutor
from camctl.session.supervision import Supervisor

from .result_cases import CaseClock

_current_case = ContextVar("baseline_case", default=None)


class BaselineClock(CaseClock):
    def __init__(self, wall, monotonic):
        super().__init__(wall, monotonic)
        self.wall_input = None

    def wall_us(self):
        return super().wall_us() if self.wall_input is None else self.wall_input()


class BaselineCase:
    def __init__(self, session):
        from ..scheduling.test_start_action import _SCHEDULED
        self.session = session
        self.clock = BaselineClock(int(_SCHEDULED), 0)
        self.factory = None
        self.supervisor = None
        self.diagnostics = []

    async def runtime(self, owned_or_database, *, directory, driver, driver_id="dji-action6",
                      evidence=None, devices=None, drivers=None, capture_repository=None,
                      scheduling_repository=None, **options):
        from ..capture.test_capture_contract import ResultsDouble, _PAGE_EVIDENCE
        self.repositories = {name: repository for name, repository in (
            ("CaptureRepository", capture_repository), ("SchedulingRepository", scheduling_repository))
            if repository is not None}
        self.database = await self.session.database(owned_or_database)
        self.devices = {"cam-1": {"driver": driver_id}} if devices is None else devices
        self.evidence = _PAGE_EVIDENCE if evidence is None else evidence
        if drivers is None:
            async def unexpected_control(request):
                raise AssertionError("基准准备尚未允许实际START")
            ports = SimpleNamespace(control=unexpected_control if driver is None else driver.control)
            if directory is not None:
                ports.read_directory = directory.read_directory
            declaration = DriverDeclaration(control_supported=True, stop_supported=False,
                query_supported=False, result_supported=False, read_supported=False,
                digest_supported=False, delete_supported=False,
                directory_supported=directory is not None)
            drivers = DriverRegistry((DriverEntry(driver_id, ports, declaration, self.evidence,
                DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        self.options = dict(devices=self.devices, drivers=drivers, results=ResultsDouble({}),
            staging=Path(self.database.metadata.staging_path), wait_config=execution_wait_config,
            wall_us=self.clock.wall_us, monotonic_ns=self.clock.monotonic_ns,
            on_recovery_diagnostic=self.diagnostics.append)
        self.options.update(options)
        self._build_factory()
        return await self.view()

    def _build_factory(self):
        if hasattr(self, "residual"):
            del self.residual
        self.supervisor = Supervisor()
        self.factory = session_capture_assembly(**self.options,
            file_executor=FileTaskExecutor(self.supervisor))

    async def view(self, *, device_id="cam-1"):
        """同一会话工厂重入，保留该工厂拥有的原保存责任。"""
        with ExitStack() as collaborators:
            for name, repository in self.repositories.items():
                collaborators.enter_context(patch(
                    "camctl.bootstrap.capture_assembly." + name, return_value=repository))
            runtime = await self.factory(self.database, device_id)
        assert runtime is not None
        return runtime

    async def reopen(self, owned_or_path, *, cold=False, **options):
        await self.drain()
        self.database = await self.session.restart_database(owned_or_path)
        if options:
            assert cold, "工厂配置变化须使用新的冷会话"
            self.options.update(options)
        if cold:
            self._build_factory()
        return await self.view()

    async def drain(self):
        if self.supervisor is None:
            return
        result = await self.supervisor.drain_required()
        errors = [failure.exception or RuntimeError(failure.error) for failure in result.failed]
        if errors:
            raise BaseExceptionGroup("基准测试实际文件责任收场失败", errors)


def baseline_case_for():
    case = _current_case.get()
    if case is None:
        raise RuntimeError("基准专项须使用baseline_case资源作用域")
    return case


@pytest_asyncio.fixture(autouse=True)
async def baseline_case(capture_resources):
    case = BaselineCase(capture_resources)
    token = _current_case.set(case)
    try:
        try:
            yield case
        finally:
            await case.drain()
    finally:
        _current_case.reset(token)


async def request_baseline_cancel(owned, tmp_path, action_id, occurred_at, *, apply=True):
    """受理真实取消动作，再由正式目标事务标记未派发的准备责任。"""
    from datetime import datetime, timezone
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
    from camctl.cancellation.models import (StartCancelAction, FixCancelTargets, FixedCancelSet,
        FixedTarget, SelectionBasis, CancellationEffect, ApplyCancelTarget, CancelApplyMode)
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from ..capture.result_consumer_fixtures import ResultCatalog

    occurred_at = int(occurred_at)
    database = await baseline_case_for().session.database(owned)
    instant = datetime.fromtimestamp(occurred_at // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = await accept_input(ParsedInput(str(tmp_path / "cancel-baseline.json"), {
        "request_id": "900", "created_at": instant, "name": "取消基准准备",
        "actions": [{"name": "取消", "type": "cancel_task", "params": {
            "target": {"action_instance_id": str(action_id)}}}]}),
        AcceptanceContext(CommandMode.RUN, ResultCatalog(), AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: occurred_at)), new_operation_key(), database)
    assert accepted.plan_id is not None, accepted
    cancel_id = await database.read(lambda owner: owner.connection.execute(
        "SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()[0])
    repository = CancellationRepository()
    async def save(method, command):
        key = new_operation_key()
        receipt = await database.write(lambda owner: method(command, key, owner), key=key)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return receipt.value
    await save(repository.start_cancel_action, StartCancelAction(cancel_id, occurred_at))
    fixed = await save(repository.fix_cancel_targets, FixCancelTargets(cancel_id,
        FixedCancelSet((FixedTarget(action_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), occurred_at))
    item_id, = fixed.item_ids
    if not apply:
        return item_id
    await save(repository.apply_cancel_target, ApplyCancelTarget(item_id, CancelApplyMode.PRE_START, occurred_at))
    return cancel_id


async def advance_existing_recovery():
    """正式残留流程发现并接手已经持久化的软件恢复责任。"""
    from camctl.capture.residual import residual_flow
    case = baseline_case_for()
    if not hasattr(case, "residual"):
        case.residual = residual_flow(case.factory)
    await case.residual(SimpleNamespace(database=case.database,
        clock=SimpleNamespace(utc_micros=case.clock.wall_us)))


def saved_recovery(owned, activity_id):
    """读取正式恢复存储格式，阶段名称从组件枚举派生。"""
    import json
    from camctl.contracts.enums import enum_for
    row = owned.connection.execute(
        "SELECT phase,state_json FROM software_recoveries WHERE activity_id=?", (activity_id,)).fetchone()
    return None if row is None else {
        **json.loads(row[1]), "phase": enum_for("software_recoveries.phase")(row[0]).name.lower()}


class RecoveryCommitFault:
    """在指定恢复阶段的实际 SQLite COMMIT 完成后丢失一次回执。"""

    def __init__(self, connection, expected_phase):
        self.connection = connection
        self.expected_phase = expected_phase
        self.updated = False
        self.injected = False

    def execute(self, statement, parameters=()):
        import sqlite3
        commit = False
        def authorize(operation, table, _column, _database, _source):
            nonlocal commit
            if operation == sqlite3.SQLITE_UPDATE and table == "software_recoveries":
                self.updated = True
            if operation == sqlite3.SQLITE_TRANSACTION and table == "COMMIT" and self.updated:
                commit = True
            return sqlite3.SQLITE_OK
        self.connection.set_authorizer(authorize)
        try:
            cursor = self.connection.execute(statement, parameters)
            if commit and not self.injected:
                phase = self.connection.execute("SELECT phase FROM software_recoveries").fetchone()
                if phase == (self.expected_phase,):
                    self.injected = True
                    raise sqlite3.OperationalError("实际恢复提交后回执丢失")
            return cursor
        finally:
            self.connection.set_authorizer(None)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class BaselineReadBoundary(Exception):
    """真实首个目录返回已保存，测试在后续业务前观察原读取。"""


async def baseline_read_world(database, tmp_path, directory, *, repository=None,
                              boundary=None, horizon=None):
    """以真实分段任务进入C1的目录读取，不制造基准读取拥有者。"""
    from dataclasses import replace
    from decimal import Decimal
    from camctl.devices.tasks import CaptureResultPolicy, VideoResultMode
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    from ..acceptance.test_adb_camera_definitions import _catalog
    from ..capture.test_baseline_start import _prepared

    catalog = _catalog("dji-action6")
    class SegmentCatalog:
        def __getattr__(self, name):
            return getattr(catalog, name)
        def parameter_definition(self, *args):
            definition = catalog.parameter_definition(*args)
            return replace(definition, task_factory=lambda params: replace(
                definition.task_factory(params), video_result_mode=VideoResultMode.SEGMENTED,
                result_policy=CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1,
                product_rules=({"kind": "video", "format_id": "mp4", "min_count": 1,
                    "exact_count": None, "min_size_bytes": 1, "require_pairing": False},)))
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    assert owned.metadata.instance_id == database.metadata.instance_id
    activity = await _prepared(owned, tmp_path, "camera_timelapse", catalog=SegmentCatalog())
    case = baseline_case_for()
    options = {}
    if boundary is not None:
        options["recovery_boundary"] = boundary
    if horizon is not None:
        options["recovery_max_event_id"] = horizon
    runtime = await case.runtime(owned, directory=directory, driver=None,
        devices={"cam-1": {"driver": "dji-action6", "result_check": {
            "max_attempts": 3, "call_timeout_s": Decimal("1.0000000000000001"),
            "retry_interval_s": Decimal("3")}}},
        capture_repository=repository, **options)
    return owned, runtime, activity


async def baseline_handler(runtime):
    from camctl.capture.handlers import capture_handler
    return await capture_handler("camera_timelapse")(1, runtime)
