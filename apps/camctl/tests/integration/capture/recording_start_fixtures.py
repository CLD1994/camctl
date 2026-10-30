"""录像 START 经公开受理、驱动登记和真实会话工厂建立前态。"""

from dataclasses import replace
from decimal import Decimal

import pytest_asyncio

from camctl.devices.ports import DeviceCallResult
from camctl.devices.tasks import StartReturn
from camctl.operations.attempts import AttemptConfig

from .result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog, consumer_world
from .test_capture_contract import ResultsDouble, _NOW


class RecordStartCatalog(ResultCatalog):
    def __init__(self, duration_ms=60000, startup=None, *, sent=False, task_factory=None):
        self.duration_ms, self.startup = duration_ms, startup
        self.sent, self.task_factory = sent, task_factory

    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if definition is None or action_type != 'camera_record':
            return definition
        factory = self.task_factory or (lambda params: replace(
            definition.task_factory(params),
            target_duration_s=Decimal(self.duration_ms) / 1000,
            startup_margin_ms=self.startup,
            start_return_meaning=StartReturn.SENT if self.sent else None))
        return replace(definition, task_factory=factory)


class ScriptedStop:
    """脚本由用例提交，运行时始终调用同一登记端口。"""
    def __init__(self, monotonic):
        self.monotonic, self.delegate = monotonic, None
        self.calls, self.times = [], []

    async def stop(self, request):
        self.calls.append(request)
        self.times.append(self.monotonic())
        if self.delegate is not None:
            return await self.delegate.stop(request)
        return DeviceCallResult((), {'code': 'transport_timeout'})


class RecordStartCase:
    def __init__(self, session):
        self.session = session
        self.wall, self.mono = [_NOW], [7_000_000_000]
        self.stopper = ScriptedStop(lambda: self.mono[0])
        self.options = {}

    async def world(self, tmp_path, *, duration_ms=60000, startup=None, sent=False,
                    task_factory=None, now_us=_NOW, max_delay_ms=60000):
        async def unstarted(owned, *, driver):
            return None
        self.owned, _, self.aid, self.handler = await consumer_world(
            tmp_path, 'record', start=False, runtime_factory=unstarted,
            catalog=RecordStartCatalog(duration_ms, startup, sent=sent, task_factory=task_factory),
            now_us=now_us, max_delay_ms=max_delay_ms)
        return self.owned

    async def runtime(self, owned, driver, *, wall=None, mono=None, maximum=3,
                      timeout=Decimal('0.75'), query=None, stop_config=None, query_config=None,
                      assembly_options=None):
        from .test_recording_start_runtime import _START_EVIDENCE
        from camctl.devices.evidence import EvidenceContract
        self.wall = [_NOW] if wall is None else wall
        self.mono = [7_000_000_000] if mono is None else mono
        evidence = _START_EVIDENCE
        for name in ("stop_returned", "stop_confirmed", "results_returned", "result_files_listed"):
            evidence = evidence.with_contract(RESULT_EVIDENCE.contract(name, 1))
        evidence = evidence.with_contract(EvidenceContract(
            'recording_sent', 1, 'control', frozenset({'activity_id'}), identity_field='activity_id'))
        from .test_baseline_prepare import Directory
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryRead
        self.options = dict(driver=driver, evidence=evidence, stopper=self.stopper,
            directory=Directory([lambda request: DirectoryRead(None, None, Page((), None))]),
            results=ResultsDouble({}), query=query,
            wall_us=lambda: self.wall[0], monotonic_ns=lambda: self.mono[0],
            start_config=AttemptConfig(maximum, timeout, Decimal('1')),
            query_config=query_config or AttemptConfig(2, Decimal('0.5'), Decimal('1')))
        if stop_config is not None:
            self.options['stop_config'] = stop_config
        self.options.update({} if assembly_options is None else assembly_options)
        self.factory = await self.session.factory(owned, **self.options)
        return await self.factory(owned)

    async def reopen(self, owned, driver, **options):
        await self.session.restart_database(owned)
        return await self.runtime(owned, driver, **options)

    async def assert_stop_deadline(self, runtime, deadline_ns):
        from camctl.capture.handlers import capture_handler
        before = len(self.stopper.calls)
        self.mono[0] = deadline_ns - 1
        await capture_handler(self.handler)(self.aid, runtime)
        assert len(self.stopper.calls) == before
        self.mono[0] = deadline_ns
        await capture_handler(self.handler)(self.aid, runtime)
        assert len(self.stopper.calls) == before + 1
        assert self.stopper.times[-1] == deadline_ns


@pytest_asyncio.fixture
async def start_case(capture_resources):
    yield RecordStartCase(capture_resources)


class StartSaveFault:
    """在仓储使用的实际 worker 连接上注入一次或持续的写入故障。"""
    def __init__(self, connection, mode, *, target_table='device_activities', target_insert=False):
        self.connection, self.mode = connection, mode
        self.target_table, self.target_insert = target_table, target_insert
        self.armed = self.enabled = True
        self.result_keys, self.rolled_back_images, self.reopened_images = [], [], []
        self.hits = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        import sqlite3
        committed = rollback = False
        def authorize(operation, table, _column, _database, _source):
            nonlocal committed, rollback
            rollback |= operation == sqlite3.SQLITE_TRANSACTION and table == 'ROLLBACK'
            if not self.enabled or (not self.armed and self.mode != 'persistent'):
                return sqlite3.SQLITE_OK
            write_kind = sqlite3.SQLITE_INSERT if self.target_insert else sqlite3.SQLITE_UPDATE
            projection = operation == write_kind and table == self.target_table
            commit = operation == sqlite3.SQLITE_TRANSACTION and table == 'COMMIT'
            if ((self.mode == 'projection' and projection)
                    or (self.mode in ('commit_before', 'commit_after', 'persistent') and commit)):
                self.hits += 1
                self.armed = False
                if self.mode == 'commit_after':
                    committed = True
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.connection.set_authorizer(authorize)
        try:
            result = self.connection.execute(statement, parameters)
        finally:
            self.connection.set_authorizer(None)
        if committed:
            raise sqlite3.OperationalError('原START结果实际COMMIT后的回执故障')
        if rollback:
            self.rolled_back_images.append(tuple(self.connection.iterdump()))
        return result


def observe_start_save(monkeypatch, mode):
    from camctl.persistence.repositories.capture import CaptureRepository
    original = CaptureRepository.finish_start_result
    class Observed:
        requests = None
        fault = None
    observed = Observed()
    observed.requests = []
    def save(repository, finish, observation, key, owned, **options):
        observed.requests.append((finish, observation, key, options))
        if observed.fault is None:
            observed.fault = StartSaveFault(owned.connection, mode)
        elif observed.fault.connection is not owned.connection:
            # UNKNOWN 由实际执行器关闭原连接；同一 worker 重建连接后尚未重送结果，
            # 在此观察真实重开后的投影与历史完整图像。
            if mode == 'commit_before' and not observed.fault.reopened_images:
                observed.fault.reopened_images.append(tuple(owned.connection.iterdump()))
            observed.fault.connection = owned.connection
        observed.fault.result_keys.append(str(key))
        return original(repository, finish, observation, key,
            replace(owned, connection=observed.fault), **options)
    monkeypatch.setattr(CaptureRepository, 'finish_start_result', save)
    return observed
