"""录像消费者通过正式受理、拍摄工厂和实际数据库线程取得输入。"""

from contextvars import ContextVar
import json
from time import perf_counter
from functools import wraps
from dataclasses import replace

import pytest_asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.bootstrap.flows import cancel_flow
from camctl.bootstrap.capture_assembly import execution_wait_config
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceContract
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.capture import CaptureRepository, FinishRecordingResults
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest, SchedulingRepository, StartActionRequest,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .capture import capture_database, capture_factory, restart_capture_database
from .capture_media_cases import Results, ScriptTools
from ..bootstrap.test_media_assembly import _SessionDriver, _config
from ..capture.media_retry_fixtures import _EVIDENCE
from ..capture.result_consumer_fixtures import ResultCatalog, returned
from ..capture.test_capture_contract import ResultsDouble

NOW = 1_750_000_000_000_000
CONTENT = b'0123456789'
EVIDENCE = _EVIDENCE.with_contract(EvidenceContract(
    'timelapse_sent', 1, 'control', frozenset({'activity_id'}), identity_field='activity_id')).with_contract(
        EvidenceContract('adb_foreground_assumption', 1, 'stop', frozenset()))


_CASES = ContextVar('recording_consumer_cases', default=None)


@pytest_asyncio.fixture(autouse=True)
async def recording_cases(capture_resources):
    cases = []
    token = _CASES.set(cases)
    try:
        yield cases
    finally:
        errors = []
        closing_at = perf_counter()
        for case in reversed(cases):
            try:
                await case.close()
            except BaseException as error:
                errors.append(error)
        try:
            await capture_resources.close()
        except BaseException as error:
            errors.append(error)
        closing_seconds = perf_counter() - closing_at
        for case in cases:
            (case.path.parent / 'recording-consumer-timings.json').write_text(json.dumps({
                'spans': case.timings, 'closed_sessions': len(case.sessions),
                'shared_session_close_seconds': closing_seconds,
                'resource_close_errors': [repr(error) for error in errors],
                'scope_note': 'start/build/write 等嵌套跨度不能相加；session close 等待实际文件责任和数据库 worker。',
            }, ensure_ascii=False, indent=2))
        _CASES.reset(token)
        if errors:
            raise BaseExceptionGroup('录像消费者资源收场失败', errors)


def timed(name):
    def decorate(operation):
        @wraps(operation)
        async def measured(self, *args, **kwargs):
            started = perf_counter()
            phase = 'preparation' if self.preparing else 'target'
            try:
                return await operation(self, *args, **kwargs)
            finally:
                self.timings.append({'operation': name, 'phase': phase,
                    'seconds': perf_counter() - started})
        return measured
    return decorate


class RecordingDriver(_SessionDriver):
    """公开设备端口提供实际启动、停止错误、源字节及摘要。"""

    def __init__(self, *, stop_failures=0):
        super().__init__(CONTENT)
        self.stop_failures = stop_failures

    async def control(self, request):
        self.calls.append(('control', request.operation))
        observation = 'timelapse_sent' if request.operation == 'start_timelapse' else 'start_confirmed'
        return DeviceCallResult.from_outcome(returned('operation', observation, request.ticket.target_id))

    async def stop(self, request):
        if self.stop_failures:
            self.calls.append(('stop', request.operation))
            self.stop_failures -= 1
            return DeviceCallResult.from_outcome(CallOutcome(
                status=AttemptStatus.FAILED, error=ErrorValue('transport_timeout', 'transport'),
                effect=EffectState.UNKNOWN, settlement=Settlement(SettlementBasis.ASSUMED,
                    EvidenceValue('adb_foreground_assumption', 1, {})), observations=()))
        return await super().stop(request)


class BeforeFinish(BaseException):
    """正式消费者已形成完整保存输入，本次存储调用尚未执行。"""


class RecordingCase:
    def __init__(self, directory, *, consumer='record', tools=None, stop_failures=0):
        self.config = _config(directory, repair_margin_s='10')
        self.path = Path(self.config.paths.state_db)
        assert initialize_state(self.config, self.path).outcome is InitOutcome.CREATED
        self.owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
        self.consumer, self.tools = consumer, tools
        self.closed = False
        self.preparing = True
        self.timings = []
        _CASES.get().append(self)
        self.handler = 'camera_record' if consumer == 'record' else 'camera_timelapse'
        self.driver = RecordingDriver(stop_failures=stop_failures)
        self.results = Results(ResultsDouble({}))
        self.wall, self.mono = [NOW], [5_000_000_000]
        self.sessions = []
        self.requests = (tools.configure(Path(directory) / 'media-tools')
            if tools is not None else (None, None))
        self.registry = DriverRegistry((DriverEntry('camctl-adb', self.driver,
            DriverDeclaration(control_supported=True, stop_supported=True, query_supported=False,
                result_supported=False, read_supported=tools is not None,
                digest_supported=tools is not None, delete_supported=False),
            EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))

    @timed('build')
    async def build(self):
        self.database = await capture_database(self.owned)
        factory = await capture_factory(self.database, driver=self.driver, evidence=EVIDENCE,
            devices=self.config.devices, drivers=self.registry, results=self.results,
            staging=Path(self.config.paths.staging), wait_config=execution_wait_config,
            stop_config=AttemptConfig(3, Decimal('10'), Decimal('1')),
            wall_us=lambda: self.wall[0], monotonic_ns=lambda: self.mono[0],
            probe_request=self.requests[0], repair_request=self.requests[1])
        self.runtime = await factory(self.database)
        self.sessions.append(self.database)
        return self

    @timed('accept')
    async def accept(self, document):
        accepted = await accept_input(ParsedInput('recording-consumer.json', document),
            AcceptanceContext(CommandMode.RUN, ResultCatalog(), AcceptanceRepository(),
                clock=SimpleNamespace(utc_micros=lambda: self.wall[0])),
            new_operation_key(), self.database)
        assert accepted.plan_id is not None, accepted
        return accepted

    @timed('write')
    async def write(self, method, request, key=None):
        key = new_operation_key() if key is None else key
        return await self.database.write(
            lambda owned: method(request, key, owned), key=key, description='录像消费者公开保存')

    @timed('start')
    async def start(self, *, started_at=NOW):
        self.wall[0] = started_at
        await self.build()
        instant = datetime.fromtimestamp(started_at // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        await self.accept({'request_id': '1', 'created_at': instant, 'name': '录像消费者',
            'actions': [{'name': '拍摄', 'type': self.handler, 'device_id': 'cam-1',
                'scheduled_at': instant, 'params': {'type': 'video' if self.consumer == 'record' else 'timelapse'},
                'policy': {'max_delay_ms': 5000}}]})
        for method, request in (
            (SchedulingRepository().observe_window, ObserveWindowRequest(1, started_at, started_at)),
            (SchedulingRepository().start_action, StartActionRequest(1, started_at, started_at)),
        ):
            saved = await self.write(method, request)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        await self.advance()
        assert self.row('SELECT status,execution_started FROM actions WHERE id=1') == (2, 1)
        assert self.row('SELECT started_at FROM device_activities WHERE id=1')[0] == (
            started_at if self.consumer == 'record' else None)
        self.preparing = False
        return self

    @timed('advance')
    async def advance(self):
        return await capture_handler(self.handler)(1, self.runtime)

    def files(self, *entries):
        self.results.delegate = ResultsDouble({1: entries})

    @timed('restart')
    async def restart(self, *, wall=None):
        if wall is not None:
            self.wall[0] = wall
        previous = self.database
        await restart_capture_database(self.owned)
        self.owned.connection.close()
        self.owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
        await self.build()
        assert self.database is not previous
        return self

    @timed('cancel')
    async def cancel(self):
        instant = datetime.fromtimestamp(self.wall[0] // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        await self.accept({'request_id': '2', 'created_at': instant, 'name': '取消原拍摄',
            'actions': [{'name': '取消', 'type': 'cancel_task',
                'params': {'target': {'action_instance_id': '1'}}}]})
        await cancel_flow(ready=Path(self.config.paths.ready), processing=Path(self.config.paths.processing))(
            SimpleNamespace(database=self.database, clock=SimpleNamespace(utc_micros=lambda: self.wall[0])))
        assert self.row('SELECT status,cancel_requested FROM actions WHERE id=1') == (2, 1)

    @timed('finish_input')
    async def finish_input(self):
        """在公开保存协作边界截获 C1 实际形成的原命令，不访问待保存集合。"""
        requests = []
        def before(method):
            def save(_repository, request, key, _owned):
                requests.append((method, request, key))
                return DbOutcome(DbOutcomeKind.NOT_EXECUTED, control=BeforeFinish(method))
            return save
        with patch.object(CaptureRepository, 'finish_capture', before('finish_capture')), \
             patch.object(CaptureRepository, 'finish_recording_results', before('finish_recording_results')), \
             patch.object(CaptureRepository, 'finish_canceled_capture', before('finish_canceled_capture')):
            try:
                await self.advance()
            except BeforeFinish:
                pass
            else:
                raise AssertionError('处理器必须形成真实完成保存输入')
        assert len(requests) == 1
        method, request, key = requests[0]
        assert self.row('SELECT status FROM actions WHERE id=1') == (2,)
        assert self.row('SELECT COUNT(*) FROM outputs') == (0,)
        return method, request, key

    def row(self, sql, *parameters):
        row = self.owned.connection.execute(sql, parameters).fetchone()
        assert row is not None, sql
        return row

    @property
    def stop_calls(self):
        return [operation for kind, operation in self.driver.calls if kind == 'stop']

    @timed('close')
    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.tools is not None:
            await self.tools.close()
        self.owned.connection.close()


def capture_request(request):
    """公开组合命令中的拍摄输入；保持原核实责任不变。"""
    return request.capture if isinstance(request, FinishRecordingResults) else request


def with_capture(request, capture):
    return replace(request, capture=capture) if isinstance(request, FinishRecordingResults) else capture


async def finish_case(directory, *, consumer='record', complete_files=True, canceled=False, second_file=False):
    from ..capture.test_capture_contract import _entry
    case = await RecordingCase(directory, consumer=consumer).start()
    case.preparing = True
    if complete_files:
        entries = (_entry('complete-file', size=4096),)
        case.files(*(entries + (_entry('second-file', size=1024),) if second_file else entries))
        case.wall[0] = NOW + (60_000_000 if consumer == 'record' else 700_000_000)
        case.mono[0] = 65_000_000_000 if consumer == 'record' else 705_000_000_000
        await case.finish_input()
        # 新 worker 与新工厂消费已经保存的原页及文件；前次尚未执行终态保存。
        await case.restart()
    if canceled:
        await case.cancel()
    case.preparing = False
    return case


async def reconciliation_case(directory, *, started_at_offset_s, repair=False):
    from camctl.host_files.media import MediaProbe, MediaStream
    from ..capture.test_capture_contract import _entry
    seconds = Decimal('200')
    tools = ScriptTools(MediaProbe(seconds, None, (
        MediaStream(0, 'video', 'h264', 64, 64, seconds, False),))) if repair else None
    case = await RecordingCase(directory, tools=tools).start(
        started_at=NOW - started_at_offset_s * 1_000_000)
    case.preparing = True
    case.files(_entry('clip-1', size=len(CONTENT)))
    await case.restart(wall=NOW)
    case.preparing = False
    return case
