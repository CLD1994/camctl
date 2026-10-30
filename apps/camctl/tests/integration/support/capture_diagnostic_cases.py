"""拍摄诊断通过正式工厂、真实保存和公共媒体入口取得原结果。"""

from dataclasses import replace
from decimal import Decimal

import pytest_asyncio

from camctl.devices.evidence import DeviceObservation, EvidenceContract
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.tool_diagnostics import ToolCallOutput, ToolDiagnostics, ToolStream, ToolStreamState

from .call_cases import CallCase, CallPort
from .capture import capture_assembly


@pytest_asyncio.fixture
async def diagnostic_case(capture_resources):
    case = CallCase()
    try:
        yield case
    finally:
        await case.close()


def actual_call(operation, ticket, *, confirmed=False, no_effect=False, error=None,
                raw=b'camera error\xff\n'):

    returned = {'control': 'operation_returned', 'result': 'results_returned'}.get(operation, operation+'_returned')
    observation = {'control': 'start_confirmed', 'stop': 'stop_confirmed', 'query': 'activity_status'}.get(operation)
    return CallOutcome(status=AttemptStatus.FAILED if error is not None else AttemptStatus.SUCCEEDED,
        error=error, effect=EffectState.CONFIRMED if confirmed else EffectState.NO_EFFECT if no_effect else EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue(returned, 1, {})),
        observations=(DeviceObservation(observation, 1, {'activity_id': ticket.target_id}),) if confirmed else (),
        diagnostics=ToolDiagnostics(2 if operation in ('control', 'stop') else 1, (ToolCallOutput(1, operation,
            ToolStream(ToolStreamState.COMPLETE, raw), ToolStream(ToolStreamState.COMPLETE, raw)),)))


async def call_world(case, directory, *, control=None, stop=None, query=None, maximum=3, diagnostics_check=None, **options):
    from ..capture.test_call_result_save import _CALL_EVIDENCE
    from ..capture.test_capture_contract import _NOW
    control = control or (lambda request: actual_call('control', request.ticket, confirmed=True))
    stop = stop or (lambda request: actual_call('stop', request.ticket, confirmed=True))
    query = query or (lambda request: actual_call('query', request.ticket))
    evidence = _CALL_EVIDENCE.with_contract(EvidenceContract('dispatch_prevented', 1, 'control', frozenset()))
    class CheckedPort(CallPort):
        def result(self, request):
            response = super().result(request)
            return replace(response, diagnostics_check=diagnostics_check)
    await case.world(directory, evidence=evidence, control=CheckedPort(control),
        stop=CallPort(stop), query=CallPort(query), now=_NOW)
    case.options.update(start_config=AttemptConfig(maximum, Decimal('.75'), Decimal(1)), **options)
    case.factory = capture_assembly(**case.options)
    return await case.view()


async def started_call(case, directory, **options):
    import pytest
    from camctl.capture.handlers import capture_handler
    from .call_cases import SavedCallBoundary
    runtime = await call_world(case, directory, **options)
    case.boundary = 'control'
    with pytest.raises(SavedCallBoundary):
        await capture_handler('camera_record')(case.action_id, runtime)
    case.boundary = None
    return await case.view()


async def unfinished_start(case, directory, monkeypatch):
    import pytest
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.repositories.scheduling import SchedulingRepository
    from camctl.persistence.models import DbOutcomeKind
    runtime = await call_world(case, directory)
    case.before_intent_horizon = case.owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    actual = SchedulingRepository.grant_start
    captured = []

    class IntentSaved(Exception):
        pass

    def grant(repository, request, key, owned):
        receipt = actual(repository, request, key, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED and receipt.value.ticket is not None, receipt.error
        captured.append(receipt.value.ticket)
        return replace(receipt, handoff_error=IntentSaved())

    with monkeypatch.context() as boundary:
        boundary.setattr(SchedulingRepository, 'grant_start', grant)
        with pytest.raises(IntentSaved):
            await capture_handler('camera_record')(case.action_id, runtime)
    ticket, = captured
    assert case.control.calls == []
    case.ticket = ticket
    case.intent_horizon = case.owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    return ticket


async def recovery_runtime(case, records, *, boundary, horizon, evidence=True):
    from types import SimpleNamespace
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.ports import DriverDeclaration
    evidence_registry = case.options['evidence'].with_contract(EvidenceContract(
        'adb_foreground_recovery', 1, 'control', frozenset()))
    ports = SimpleNamespace(control=case.control.control, stop=case.stop.stop, query_state=case.query.query_state)
    declaration = DriverDeclaration(control_supported=True, stop_supported=True, query_supported=True,
        result_supported=False, read_supported=False, digest_supported=False, delete_supported=False,
        adb_foreground_recovery_operations=frozenset({'control'}) if evidence else frozenset())
    case.options.update(drivers=DriverRegistry((DriverEntry('camctl-adb', ports, declaration,
        evidence_registry, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),)),
        evidence=evidence_registry, recovery_boundary=boundary,
        recovery_max_event_id=None if horizon is None else lambda: horizon,
        on_recovery_diagnostic=records.append)
    return await case.restart(cold=True)


async def media_saves(pipeline, resources, *, repair=False):
    """C9公开ProcessingSaves适配器组合真实worker和五个处理仓储事务。"""
    from camctl.capture.media_flow import CaptureProcessingSaves
    from camctl.capture.processing import (CheckResultSave, CheckPhase, MediaObservation,
        RepairDecisionSave, RepairDecisionChoice, RepairBasis, RepairReason)
    from ..capture.test_capture_contract import _NOW
    database = await resources.database(pipeline[0])
    saves = CaptureProcessingSaves(database)
    if repair:
        for save, command in (
            (saves.save_check_result, CheckResultSave(1, MediaObservation(CheckPhase.COMPLETED,
                duration_s=Decimal(75)), _NOW)),
            (saves.save_repair_decision, RepairDecisionSave(1, RepairDecisionChoice.PENDING,
                RepairBasis(RepairReason.THRESHOLD_REACHED, 60000, threshold_s=Decimal(62),
                    actual_duration_s=Decimal(75)), _NOW)),
        ):
            receipt = await save(command)
            assert receipt.disposition.value == 'saved', receipt
    return saves


async def preparation_flow(case, directory, *, diagnostics_check=None):
    from types import SimpleNamespace
    from camctl.bootstrap.flows import capture_flow
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.ports import DeviceCallResult, DriverDeclaration
    await call_world(case, directory)
    class Preparation:
        default_prepare_lead_s = Decimal(0)
        calls = []
        async def prepare_capture(self, request):
            self.calls.append(request)
            return DeviceCallResult.from_outcome(actual_call('control', request.ticket,
                error=ErrorValue('device_error', 'device', {'operation': 'prepare'})),
                diagnostics_check=diagnostics_check)
        def prepared_capture_matches(self, request):
            return False
    preparation = Preparation()
    ports = SimpleNamespace(control=case.control.control, stop=case.stop.stop,
        default_prepare_lead_s=Decimal(0), prepare_capture=preparation.prepare_capture,
        prepared_capture_matches=preparation.prepared_capture_matches)
    declaration = DriverDeclaration(control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=False, digest_supported=False, delete_supported=False,
        capture_preparation_supported=True)
    case.options.update(drivers=DriverRegistry((DriverEntry('camctl-adb', ports, declaration,
        case.options['evidence'], DriverStatus.SOFTWARE_CONTRACT_VERIFIED),)))
    case.factory = capture_assembly(**case.options)
    flow = capture_flow(case.factory)
    context = SimpleNamespace(database=case.database, clock=SimpleNamespace(utc_micros=case.clock.wall_us))
    return preparation, flow, context


def segment_probe_port(monkeypatch, kind):
    """MediaTools正式文件端口实际执行受管任务，交付四类原结果。"""
    import asyncio
    from pathlib import Path
    from camctl.bootstrap import capture_assembly as assembly
    from camctl.host_files.media import MediaProbe
    from camctl.host_files.tasks import FileTask, AsyncFileTask
    from ..capture.test_input_copy import _CONTENT
    calls = []
    async def probe(input_file, roots, request, *, executor, task_id, owner):
        assert not request.count_frames
        calls.append(input_file.file_id)
        def execute(_stop):
            assert (Path(input_file.root) / input_file.relative_path).read_bytes() == _CONTENT
            if kind == 'tool':
                raise OSError('tool failed')
            return None if kind == 'invalid' else MediaProbe(None, 'invalid_structure: wrong streams')
        if kind == 'exception':
            async def failure(control):
                await asyncio.to_thread(execute, None)
                try:
                    raise RuntimeError('original tool failure')
                except RuntimeError as cause:
                    raise ValueError('invalid probe response') from cause
            await executor.run_owned_async_file_task(AsyncFileTask(task_id, (input_file.file_id,),
                'segment_probe', '分段检查实际工具失败', failure))
        return await executor.run_file_task(FileTask(task_id, input_file.file_id,
            'segment_probe', '分段检查实际返回', execute), owner)
    monkeypatch.setattr(assembly, 'probe_media', probe)
    return calls


class ActualStorageFault:
    """真实COMMIT失败及失效连接close错误经DbExecutor原通道交付。"""
    def __init__(self, *, secondary=None, after=False):
        import sqlite3
        self.primary = sqlite3.OperationalError('database failed')
        self.secondary, self.after = secondary, after
        self.armed = self.closing = False
        self.commits = self.failed_closes = 0

    def connection(self, actual):
        fault = self
        class Connection:
            def execute(self, statement, parameters=()):
                if fault.armed and statement == 'COMMIT':
                    fault.armed, fault.closing = False, True
                    fault.commits += 1
                    if fault.after:
                        actual.execute(statement, parameters)
                    raise fault.primary
                return actual.execute(statement, parameters)
            def close(self):
                actual.close()
                if fault.closing:
                    fault.closing = False
                    if fault.secondary is not None:
                        fault.failed_closes += 1
                        raise fault.secondary
            def __getattr__(self, name):
                return getattr(actual, name)
        return Connection()


from contextlib import asynccontextmanager


@asynccontextmanager
async def diagnostic_database(path, fault):
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    def connect():
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        return replace(owned, connection=fault.connection(owned.connection))
    database = await BusinessDatabase.open(connect)
    try:
        yield database
    finally:
        fault.armed = False
        await database.close()
