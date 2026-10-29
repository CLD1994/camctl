"""自然等待的正式输入、真实会话重开与公开存储申请见证。"""

from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest_asyncio

from camctl.capture.handlers import capture_handler
from camctl.capture.models import WaitCompletedSave
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.operations.attempts import OperationKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.timelapse import ScheduleWait, TimelapseRepository

from .result_consumer_fixtures import ResultCatalog
from .test_capture_contract import ResultsDouble, _NOW, _entry
from ..support.result_cases import result_case


class TimingCatalog(ResultCatalog):
    def __init__(self, duration_ms=1000, margin_ms=200, startup=None):
        self.duration_ms, self.margin_ms, self.startup = duration_ms, margin_ms, startup

    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if action_type != "camera_timelapse" or definition is None:
            return definition
        return replace(definition, task_factory=lambda params: replace(
            definition.task_factory(params), target_duration_s=Decimal(self.duration_ms) / 1000,
            result_wait_margin_s=Decimal(self.margin_ms) / 1000,
            startup_margin_ms=self.startup))


class WaitBoundary(Exception):
    """实际等待保存可靠，后续结果列举尚未执行。"""


class WaitCase:
    def __init__(self, case, monkeypatch):
        self.case = case
        self.hooks = {}
        self.requests = []
        self.reliable_schedule = None
        self.narrow = False
        self.originals = {}
        self.call_start = 0
        for name in ("schedule_wait", "reconfigure_wait", "complete_wait", "verify_wait"):
            original = getattr(TimelapseRepository, name)
            self.originals[name] = original
            def observe(repository, command, key, owned, _name=name, _save=original, **options):
                hook = self.hooks.get(_name)
                saved = (hook(command, key, owned, **options) if hook is not None
                         else _save(repository, command, key, owned, **options))
                self.requests.append((_name, command, key, saved))
                if saved.kind is DbOutcomeKind.COMPLETED:
                    if isinstance(command, ScheduleWait) and (_name != "verify_wait" or saved.value is not None):
                        self.reliable_schedule = command
                    if self.narrow and isinstance(command, WaitCompletedSave) and saved.value is not None:
                        return replace(saved, handoff_error=WaitBoundary())
                return saved
            monkeypatch.setattr(TimelapseRepository, name, observe)
        begin = OperationRepository.begin_attempt
        def begin_result(repository, command, key, owned):
            saved = begin(repository, command, key, owned)
            if (self.narrow and command.kind is OperationKind.CHECK_CAPTURE_RESULTS
                    and saved.kind is DbOutcomeKind.COMPLETED):
                return replace(saved, handoff_error=WaitBoundary())
            return saved
        monkeypatch.setattr(OperationRepository, "begin_attempt", begin_result)

    def save(self, name):
        return lambda command, key, owned, **options: self.originals[name](
            TimelapseRepository(), command, key, owned, **options)

    def reject_projection(self, name):
        save = self.save(name)
        def rejected(command, key, owned, **options):
            denied = []
            def authorize(operation, table, _column, _database, _source):
                if operation == sqlite3.SQLITE_UPDATE and table == "device_activities":
                    denied.append(table)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            owned.connection.set_authorizer(authorize)
            try:
                result = save(command, key, owned, **options)
            finally:
                owned.connection.set_authorizer(None)
            assert denied and result.kind is DbOutcomeKind.ROLLED_BACK, result.error
            return result
        return rejected

    @property
    def deadline(self):
        return None if self.reliable_schedule is None else self.reliable_schedule.plan.monotonic_deadline_ns

    @property
    def calls(self):
        return [call.args[0].operation for call in self.case.driver.control.await_args_list[self.call_start:]]

    @property
    def results(self):
        return self.case.results.delegate

    async def world(self, tmp_path, *, startup=None, duration_ms=1000, margin_ms=200,
                    wall=None, mono=None, extra=None, advance=False, max_delay_ms=5000, now_us=_NOW):
        self.wall = [_NOW] if wall is None else wall
        self.mono = [5_000_000_000] if mono is None else mono
        self.extra = [0] if extra is None else extra
        self.duration_ms, self.margin_ms, self.startup = duration_ms, margin_ms, startup
        self.devices = {"cam-1": {"driver": "camctl-adb", "capture": {"extra_wait_ms": self.extra[0]}}}
        self.owned, self.runtime, self.aid, self.handler = await self.case.world(
            tmp_path, "timelapse", catalog=TimingCatalog(duration_ms, margin_ms, startup), start=False,
            max_delay_ms=max_delay_ms, now_us=now_us,
            assembly_options={"wall_us": lambda: self.wall[0], "monotonic_ns": lambda: self.mono[0],
                "devices": self.devices, "check_config": None,
                "wait_config": lambda action: CaptureWaitConfig(
                    duration_ms, margin_ms, self.extra[0], startup_margin_ms=startup or 0)})
        self.case.results.delegate = ResultsDouble({self.aid: (_entry("sequence-1"),)})
        if advance:
            await self.drive()
        return self.owned, self.runtime, self.wall, self.mono, self.extra

    async def reopen(self, *, wall=None, mono=None, extra=None):
        if wall is not None:
            self.wall = wall
        if mono is not None:
            self.mono = mono
        if extra is not None:
            self.extra = extra
        self.devices["cam-1"]["capture"]["extra_wait_ms"] = self.extra[0]
        self.call_start = len(self.case.driver.control.await_args_list)
        self.runtime = await self.case.reopen(self.owned, cold=True)
        return self.runtime

    async def drive(self, *, narrow=False):
        # 这是装配模块的显式配置输入，不代表 CLI 会在运行中重新装载配置。
        self.devices["cam-1"]["capture"]["extra_wait_ms"] = self.extra[0]
        self.narrow = narrow
        try:
            try:
                await capture_handler(self.handler)(self.aid, self.runtime)
            except WaitBoundary:
                if not narrow:
                    raise
        finally:
            self.narrow = False
        return self.owned.connection.execute(
            "SELECT wait_completed_event_id FROM device_activities WHERE action_id=?",
            (self.aid,)).fetchone()[0]



@pytest_asyncio.fixture
async def wait_case(result_case, monkeypatch):
    yield WaitCase(result_case, monkeypatch)
