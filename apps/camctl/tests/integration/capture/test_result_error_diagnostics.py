"""拍摄结果经正式消费者与真实保存交付公共诊断。"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.capture.processing import (
    CheckPhase, CheckResultSave, MediaObservation, ProcessingError, RepairOutcome, RepairResultSave,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.enums import enum_for
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context, flush_diagnostics
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.operations.tool_diagnostics import ToolDiagnostics
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository

pytestmark = pytest.mark.asyncio

from ..support.capture_diagnostic_cases import diagnostic_case, call_world, started_call, actual_call
from ..support.call_cases import SavedCallBoundary
from ..support.result_cases import result_case
from .test_input_copy import pipeline
from .media_retry_fixtures import media_pipeline
from ..support.capture_media_cases import media_cases
from .baseline_database import database
from ..support.baseline_cases import baseline_case


def _documents(records):
    return [json.loads(record) for record in records]


@pytest.mark.parametrize('operation', ['control', 'stop', 'query', 'result'])
async def test_call_error_is_logged_before_failed_business_save_and_replay_logs_once(
        tmp_path, diagnostic_case, result_case, monkeypatch, operation):
    from camctl.capture.handlers import capture_handler
    from camctl.operations.attempts import QueryPurpose
    error = ErrorValue('device_error', 'device', {'operation': operation})
    records = []
    owned = None
    try:
        if operation == 'result':
            from dataclasses import replace
            from .result_consumer_fixtures import RESULT_EVIDENCE, result_driver
            from ..bootstrap.test_binding_transactions import _CommitFailure
            case = result_case
            owned, runtime, aid, handler = await case.world(tmp_path, 'photo', evidence_contracts=(
                replace(RESULT_EVIDENCE.contract('results_returned', 1), max_diagnostic_calls=1),))
            driver = result_driver(case, actual_call('result', SimpleNamespace(target_id='1'), error=error))
            enabled, saves = [True], []
            actual = OperationRepository.finish_attempt
            def finish(repository, request, key, current):
                if request.ticket.operation == 'result':
                    saves.append((request, key))
                    if enabled[0]:
                        current = replace(current, connection=_CommitFailure(current.connection, False))
                return actual(repository, request, key, current)
            monkeypatch.setattr(OperationRepository, 'finish_attempt', finish)
            with error_diagnostics_context(None, records.append):
                async with diagnostic_scope():
                    with pytest.raises(ConsistencyError):
                        await capture_handler(handler)(aid, runtime)
                enabled[0] = False
                async with diagnostic_scope():
                    await capture_handler(handler)(aid, runtime)
            assert driver.list_results.await_count == 1
            ticket = saves[0][0].ticket
            assert len(saves) >= 2 and all(value == saves[0] for value in saves)
        else:
            case = diagnostic_case
            if operation == 'control':
                runtime = await call_world(case, tmp_path, control=lambda request: actual_call(operation, request.ticket, error=error))
                port = case.control
            elif operation == 'stop':
                runtime = await started_call(case, tmp_path, stop=lambda request: actual_call(operation, request.ticket, error=error))
                case.clock.wall += 60_000_000
                case.clock.monotonic += 60_000_000_000
                port = case.stop
            else:
                from .test_call_result_save import _query_consumer
                runtime = await _query_consumer(case, tmp_path, QueryPurpose.ACTIVITY_OBSERVATION)
                case.query.outcome = lambda request: actual_call(operation, request.ticket, error=error)
                from dataclasses import replace
                from ..support.capture import capture_assembly
                case.options['evidence'] = case.options['evidence'].with_contract(replace(
                    case.options['evidence'].contract('query_returned', 1), max_diagnostic_calls=1))
                case.factory = capture_assembly(**case.options)
                runtime = await case.view()
                port = case.query
            case.boundary = operation
            case.fault.arm(port, 'persistent')
            with error_diagnostics_context(None, records.append):
                async with diagnostic_scope():
                    with pytest.raises(ConsistencyError):
                        await capture_handler('camera_record')(case.action_id, runtime)
                request, = port.calls
                ticket = request.ticket
                assert case.owned.connection.execute('SELECT status,result_event_id FROM operation_attempts WHERE run_id=?',
                    (ticket.run_id,)).fetchone() == (1, None)
                case.fault.enabled = False
                async with diagnostic_scope():
                    with pytest.raises(SavedCallBoundary):
                        await capture_handler('camera_record')(case.action_id, await case.restart())
            assert len(port.calls) == 1
            saves = [(finish, key) for finish, key in case.saves if finish.ticket == ticket]
            assert len(saves) >= 2 and all(value == saves[0] for value in saves)
        owned = case.owned if operation != 'result' else owned
        finish, key = saves[0]
        rows = owned.connection.execute(
            'SELECT a.id,a.status,a.error_json,a.result_json,a.result_event_id,e.occurred_at,t.operation_key'
            ' FROM operation_attempts a JOIN history_events e ON e.id=a.result_event_id'
            ' JOIN history_transactions t ON t.id=e.transaction_id WHERE a.run_id=? AND a.attempt_no=?',
            (ticket.run_id, ticket.attempt_id)).fetchall()
        (attempt_id, status, error_json, result_json, event_id, occurred_at, transaction_key), = rows
        expected_error = {'code': 'device_error', 'stage': 'device', 'details': {'operation': operation}}
        assert status == 3 and json.loads(error_json) == expected_error
        assert occurred_at == finish.occurred_at and transaction_key == str(key)
        assert owned.connection.execute('SELECT COUNT(*) FROM history_transactions WHERE operation_key=?',
            (str(key),)).fetchone() == (1,)
        events = owned.connection.execute(
            "SELECT e.id FROM history_events e, json_each(e.body_json, '$.rows') r"
            " WHERE json_extract(r.value, '$.table')='operation_attempts'"
            " AND json_extract(r.value, '$.id')=?"
            " AND json_extract(r.value, '$.after.values.result_event_id') IS NOT NULL", (attempt_id,)).fetchall()
        assert events == [(event_id,)]
        saved_call, = json.loads(result_json)['diagnostics']['calls']
        assert saved_call['step_name'] == operation
        assert saved_call['stderr']['hex'] == b'camera error\xff\n'.hex()
        document, = _documents(records)
        assert document['level'] == 'ERROR' and document['error'] == expected_error
        assert document['ticket']['run_id'] == ticket.run_id
        assert document['outcome']['diagnostics']['calls'][0]['stderr']['hex'] == b'camera error\xff\n'.hex()
    finally:
        if operation == 'result' and owned is not None:
            from .task_cleanup import close_observer
            close_observer(owned)



@pytest.mark.parametrize('wrapper', ['ordinary', 'results', 'start_close', 'binding'])
async def test_failed_capture_is_logged_when_combined_database_save_completes(
        tmp_path, result_case, monkeypatch, wrapper):
    from camctl.capture.handlers import capture_handler
    records = []
    if wrapper == 'start_close':
        from dataclasses import replace
        from ..bootstrap.test_binding_transactions import _CommitFailure
        from ..support.close_cases import start_case
        requests = []
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                world = await start_case(tmp_path, 'lowered')
            try:
                actual = CaptureRepository.close_start
                def close_start(repository, finish, action, key, owned):
                    requests.append((finish, action, key))
                    if len(requests) == 1:
                        owned = replace(owned, connection=_CommitFailure(owned.connection, True))
                    return actual(repository, finish, action, key, owned)
                monkeypatch.setattr(CaptureRepository, 'close_start', close_start)
                async with diagnostic_scope():
                    with pytest.raises(ConsistencyError):
                        await world.advance()
                async with diagnostic_scope():
                    await world.advance()
                aid, owned = world.action_id, world.owned
                assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (aid,)).fetchone() == (4,)
                assert requests == [requests[0], requests[0]]
                assert len(world.driver.calls) == 1
                assert owned.connection.execute('SELECT result_event_id IS NOT NULL FROM operation_attempts').fetchone() == (1,)
                assert owned.connection.execute('SELECT COUNT(*) FROM history_transactions WHERE operation_key=?',
                    (str(requests[0][2]),)).fetchone() == (1,)
            finally:
                await world.close()
        call, document = _documents(records)
        assert call['error']['code'] == 'device_busy'
        assert call['ticket']['run_id'] == world.driver.calls[0].ticket.run_id
        assert document['error']['code'] == 'start_attempts_exhausted'
        assert document['context']['action_id'] == aid
        return
    else:
        case = result_case
        owned, runtime, aid, handler = await case.world(tmp_path, 'photo' if wrapper == 'ordinary' else 'record')
        try:
            if wrapper == 'binding':
                runtime = await case.reopen(owned, cold=True, assembly_options={'devices': {}})
            with error_diagnostics_context(None, records.append):
                async with diagnostic_scope():
                    await capture_handler(handler)(aid, runtime)
            assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (aid,)).fetchone() == (4,)
        finally:
            owned.connection.close()
    document, = _documents(records)
    assert document['context']['action_id'] == aid
    assert document['error']['code'] == {'binding': 'device_binding_unavailable',
        'start_close': 'start_attempts_exhausted'}.get(wrapper, 'capture_failed')


@pytest.mark.parametrize('kind', ['check', 'repair', 'issues'])
async def test_media_error_is_logged_when_original_request_is_retried(pipeline, capture_resources, monkeypatch, kind):
    from dataclasses import replace
    from .test_capture_contract import _NOW
    from ..support.capture_diagnostic_cases import media_saves
    from ..bootstrap.test_binding_transactions import _CommitFailure
    error = ProcessingError('probe_unconfirmed', 'probe', {'reason': 'duration unavailable'})
    command = (RepairResultSave(1, RepairOutcome.FAILED, _NOW, error=error) if kind == 'repair'
        else CheckResultSave(1, MediaObservation(CheckPhase.COMPLETED, duration_s=1, issues=(error,))
             if kind == 'issues' else MediaObservation(CheckPhase.UNCONFIRMED, error=error), _NOW))
    saves = await media_saves(pipeline, capture_resources, repair=kind == 'repair')
    method = 'save_repair_result' if kind == 'repair' else 'save_check_result'
    actual = getattr(CaptureRepository, method)
    requests = []
    def save(repository, request, key, owned):
        requests.append((request, key))
        if len(requests) == 1:
            owned = replace(owned, connection=_CommitFailure(owned.connection, True))
        return actual(repository, request, key, owned)
    monkeypatch.setattr(CaptureRepository, method, save)
    records = []
    try:
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                first = await getattr(saves, method)(command)
            assert first.disposition.value == 'unknown'
            async with diagnostic_scope():
                second = await getattr(saves, method)(command)
            assert second.disposition.value == 'saved'
        document, = _documents(records)
        assert document['error']['code'] == 'probe_unconfirmed'
        assert document['context']['processing_id'] == 1
        assert document['context']['action_id'] == 1
        assert requests == [requests[0], requests[0]]
        assert pipeline[0].connection.execute('SELECT COUNT(*) FROM history_transactions WHERE operation_key=?',
            (str(requests[0][1]),)).fetchone() == (1,)
    finally:
        pipeline[0].connection.close()


async def test_preparation_original_failed_finish_logs_even_after_database_success(tmp_path, diagnostic_case):
    from ..support.capture_diagnostic_cases import preparation_flow
    case = diagnostic_case
    preparation, flow, context = await preparation_flow(case, tmp_path)
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await flow(context)
            await flow.settle()
    document, = _documents(records)
    assert document['error']['details']['operation'] == 'prepare'
    assert document['context']['action_id'] == case.action_id
    assert len(preparation.calls) == 1 and case.control.calls == []
    assert case.owned.connection.execute('SELECT t.status,t.result_event_id IS NOT NULL FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id WHERE r.kind=?', (int(enum_for('operation_runs.kind').PREPARE_CAPTURE),)).fetchone() == (3, 1)


async def test_normal_not_dispatched_control_does_not_log_error(tmp_path, diagnostic_case):
    from camctl.capture.handlers import capture_handler
    case = diagnostic_case
    outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        error=ErrorValue('canceled', 'dispatch', {}), settlement=Settlement(
            SettlementBasis.NOT_DISPATCHED, EvidenceValue('dispatch_prevented', 1)))
    runtime = await call_world(case, tmp_path, control=outcome)
    case.boundary = 'control'
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(SavedCallBoundary):
                await capture_handler('camera_record')(case.action_id, runtime)
    assert records == []
    assert len(case.control.calls) == 1
    assert case.owned.connection.execute('SELECT status,result_event_id IS NOT NULL FROM operation_attempts').fetchone() == (3, 1)


async def test_baseline_freezes_full_actual_error_before_return_model_discards_tool_outcome(
        database, tmp_path, baseline_case):
    from camctl.devices.directory import DirectoryRead
    from camctl.operations.process import RawToolOutcome
    from .test_baseline_read_owner import ObserveRead
    from ..support.baseline_cases import BaselineReadBoundary, baseline_read_world, baseline_handler
    actual = RawToolOutcome(None, b'file metadata', 'timeout', None, stderr=b'directory\xff')
    class Directory:
        calls = 0
        async def read_directory(self, request, *, stop):
            self.calls += 1
            return DirectoryRead(actual, ErrorValue('device_error', 'directory', {'path': '/DCIM'}), None)
    directory, repository, records = Directory(), ObserveRead(), []
    owned, runtime, activity = await baseline_read_world(database, tmp_path, directory, repository=repository)
    try:
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                with pytest.raises(BaselineReadBoundary):
                    await baseline_handler(runtime)
        document, = _documents(records)
        assert document['details']['tool_outcome']['stderr']['hex'] == b'directory\xff'.hex()
        assert document['details']['stdout_hex'] == b'file metadata'.hex()
        assert document['context']['read_no'] == 1
        request, _key = repository.returns[0]
        assert request.returned.outcome is None and directory.calls == 1
        assert owned.connection.execute('SELECT result_event_id IS NOT NULL FROM baseline_read_calls').fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_failed_result_page_and_final_attempt_share_one_diagnostic(tmp_path, result_case):
    from camctl.capture.handlers import capture_handler
    from ..support.result_cases import saved_result_pages
    case = result_case
    from dataclasses import replace
    from .result_consumer_fixtures import RESULT_EVIDENCE
    from decimal import Decimal
    from camctl.operations.attempts import AttemptConfig
    owned, runtime, aid, handler = await case.world(tmp_path, 'photo',
        assembly_options={'check_config': AttemptConfig(3, Decimal(3), Decimal(2))}, evidence_contracts=(
        replace(RESULT_EVIDENCE.contract('results_returned', 1), max_diagnostic_calls=1),))
    error = ErrorValue('device_error', 'results', {'reason': 'directory unavailable'})
    from camctl.capture.result_inputs import ResultPage
    outcome = actual_call('result', SimpleNamespace(target_id='1'), error=error)
    case.results.delegate = SimpleNamespace(list_page=AsyncMock(return_value=ResultPage((), None, None, False, None, outcome, False)))
    records = []
    try:
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                await capture_handler(handler)(aid, runtime)
        document, = _documents(records)
        assert document['context']['page_no'] == 1
        assert document['error']['details'] == {'reason': 'directory unavailable'}
        assert case.results.delegate.list_page.await_count == 1
        assert len(saved_result_pages(owned, aid)) == 1
        assert owned.connection.execute("SELECT result_event_id IS NOT NULL FROM operation_attempts WHERE run_id=(SELECT id FROM operation_runs WHERE action_id=? AND kind=7)", (aid,)).fetchone() == (1,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('kind', ['exception', 'tool', 'invalid', 'probe'])
async def test_handled_segment_probe_errors_keep_actual_diagnostic_when_save_is_unknown(
        tmp_path, capture_resources, monkeypatch, kind):
    from dataclasses import replace
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository
    from ..support.bootstrap_read_cases import inspection_case
    from ..support.capture_diagnostic_cases import segment_probe_port
    from ..bootstrap.test_binding_transactions import _CommitFailure
    calls = segment_probe_port(monkeypatch, kind)
    from ..support.segment_inspection_cases import inspection_source
    source = await inspection_source(tmp_path)
    world = await inspection_case(source, capture_resources)
    from camctl.outputs.qualification import OperationConfig
    from decimal import Decimal
    world.request = replace(world.request, budget=OperationConfig(3, Decimal(3), Decimal(0)))
    actual, requests = SegmentInspectionRepository.save_result, []
    def save(repository, request, key, current):
        requests.append((request, key))
        if len(requests) == 1:
            current = replace(current, connection=_CommitFailure(current.connection, True))
        return actual(repository, request, key, current)
    monkeypatch.setattr(SegmentInspectionRepository, 'save_result', save)
    records = []
    try:
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                result = await world.inspect()
            assert result.reason == 'result_save_unknown'
            async with diagnostic_scope():
                saved = await world.inspect()
        documents = _documents(records)
        assert len(documents) == 2
        probe, = [doc for doc in documents if doc['stage'] == 'capture.segment_probe']
        storage, = [doc for doc in documents if doc['stage'] == 'capture.segment_save']
        assert probe['context']['action_id'] == world.ref.action_id
        assert probe['context']['inspection_id'] == result.inspection_id
        assert storage['context']['inspection_id'] == result.inspection_id
        assert probe['level'] == storage['level'] == 'ERROR'
        assert storage['error']['cause']['type'] == 'sqlite3.OperationalError'
        assert storage['error']['cause']['message'] == 'injected commit response failure'
        assert len(calls) == 1 and len(world.reader.requests) == 1
        world.held_ticket()
        assert requests == [requests[0], requests[0]]
        request, key = requests[0]
        assert saved.reason != 'result_save_unknown'
        row, = world.owned.connection.execute(
            'SELECT s.result_json,s.result_event_id,e.occurred_at,t.operation_key'
            ' FROM segment_inspections s JOIN history_events e ON e.id=s.result_event_id'
            ' JOIN history_transactions t ON t.id=e.transaction_id WHERE s.id=?', (result.inspection_id,)).fetchall()
        saved_json, event_id, occurred_at, transaction_key = row
        assert json.loads(saved_json) == request.result_json
        assert occurred_at == request.occurred_at and transaction_key == str(key)
        assert world.owned.connection.execute(
            'SELECT COUNT(*) FROM history_transactions WHERE operation_key=?', (str(key),)).fetchone() == (1,)
        assert world.owned.connection.execute(
            "SELECT e.id FROM history_events e, json_each(e.body_json, '$.rows') r"
            " WHERE json_extract(r.value, '$.table')='segment_inspections'"
            " AND json_extract(r.value, '$.id')=?"
            " AND json_extract(r.value, '$.after.values.result_event_id') IS NOT NULL",
            (result.inspection_id,)).fetchall() == [(event_id,)]
        data = json.loads(saved_json)
        if kind == 'exception':
            assert probe['error']['type'] == 'builtins.ValueError'
            assert probe['error']['message'] == 'invalid probe response'
            assert probe['error']['cause']['type'] == 'builtins.RuntimeError'
            assert probe['error']['cause']['message'] == 'original tool failure'
            assert data['tool'] == {'ran': False, 'error': 'invalid probe response'}
        elif kind == 'tool':
            assert probe['error'] == 'OSError: tool failed'
            assert data['tool'] == {'ran': True, 'error': 'OSError: tool failed'}
        elif kind == 'invalid':
            assert probe['error'] == {'reason': 'probe_result_unavailable', 'probe': None}
            assert data['tool'] == {'ran': True, 'error': 'probe returned no MediaProbe'}
        else:
            assert probe['error']['reason'] == 'probe_error'
            assert probe['error']['probe']['error'] == 'invalid_structure: wrong streams'
            assert probe['error']['probe']['duration_s'] is None
            assert data['tool'] == {'ran': True, 'error': None}
            assert data['probe']['error'] == 'invalid_structure: wrong streams'
    finally:
        await world.close()
        world.owned.connection.close()


@pytest.mark.parametrize('later_failed', [False, True])
async def test_tolerated_action6_setting_error_is_logged_with_the_outer_result(
        tmp_path, capture_resources, monkeypatch, later_failed):
    from datetime import datetime, timezone
    from decimal import Decimal
    from dataclasses import replace
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.ports import ParameterDefinition
    from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
    from camctl.capture.handlers import capture_handler
    from camctl.devices.drivers.adb_cameras.action6_record import action6_record_contract
    from camctl.devices.tasks import CaptureTask
    from camctl.operations.attempts import AttemptConfig
    from camctl.operations.process import LocalExit, RawToolOutcome
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.scheduling import SchedulingRepository, ObserveWindowRequest, StartActionRequest
    from ..bootstrap.test_recording_stop import _RecordCatalog
    from ..support.capture import capture_assembly
    from ..support.database import create_test_database
    from .test_capture_contract import _NOW, ResultsDouble
    from unit.devices.test_adb_camera_driver import Action6ContractTransport, _action6_contract_driver, _params
    from camctl.devices.drivers.adb_cameras.commands import CameraModel

    class Catalog(_RecordCatalog):
        def driver_id(self, device_id):
            return 'dji-action6'
        def parameter_definition(self, device_id, action_type, parameter_type):
            if parameter_type != 'action6_record':
                return None
            return ParameterDefinition(schema={'$schema': 'https://json-schema.org/draft/2020-12/schema', 'type': 'object', 'properties': {key: {'const': value} for key, value in _params(CameraModel.ACTION6).items()}, 'required': list(_params(CameraModel.ACTION6)), 'additionalProperties': False}, defaults={}, task_factory=lambda params:
                CaptureTask('camera_record', target_duration_s=Decimal(10), stop_supported=True))

    path = tmp_path / 'state.db'
    create_test_database(path)
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    database = await capture_resources.database(owned)
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    accepted = await accept_input(ParsedInput('action6.json', {'request_id': '1', 'created_at': instant,
        'name': '录像', 'actions': [{'name': '录像', 'type': 'camera_record', 'device_id': 'cam-1',
        'scheduled_at': instant, 'params': _params(CameraModel.ACTION6), 'policy': {'max_delay_ms': 60000}}]}),
        AcceptanceContext(CommandMode.RUN, Catalog(), AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: _NOW)), new_operation_key(), database)
    assert accepted.plan_id is not None, accepted
    scheduling = SchedulingRepository()
    for save, request in ((scheduling.observe_window, ObserveWindowRequest(1, _NOW, _NOW)),
                          (scheduling.start_action, StartActionRequest(1, _NOW, _NOW))):
        receipt = save(request, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    bitrate = ('simulate_device', '-s', 'bitrate', '2')
    transport = Action6ContractTransport(aperture_payload='e3', overrides={
        bitrate: RawToolOutcome(LocalExit(exit_code=9), b'bitrate failed', None, None, stderr=b'bitrate stderr')
    } if later_failed else None)
    driver = _action6_contract_driver(transport)
    factory = capture_assembly(driver=driver, stopper=driver, results=ResultsDouble({}), evidence=action6_record_contract().evidence,
        driver_id='dji-action6', staging=tmp_path / 'staging', wall_us=lambda: _NOW,
        monotonic_ns=lambda: 0, start_config=AttemptConfig(3, Decimal(20), Decimal(1)))
    actual = CaptureRepository.finish_start_result
    def finish(repository, *args, **kwargs):
        receipt = actual(repository, *args, **kwargs)
        return replace(receipt, handoff_error=SavedCallBoundary()) if receipt.kind is DbOutcomeKind.COMPLETED else receipt
    monkeypatch.setattr(CaptureRepository, 'finish_start_result', finish)
    records = []
    try:
        runtime = await factory(database, 'cam-1')
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                with pytest.raises(SavedCallBoundary):
                    await capture_handler('camera_record')(1, runtime)
        document, = _documents(records)
        outcome = document.get('outcome') or document['details']['outcome']
        calls = outcome['settlement']['evidence']['data']['calls']

        def resolve_call(call):
            if '$ref' not in call:
                return call
            # 同一调用事实已在诊断中出现时，序列化器用文档内引用保留它。
            pointer = call['$ref']
            assert pointer.startswith('#/')
            value = document
            for part in pointer[2:].split('/'):
                part = part.replace('~1', '/').replace('~0', '~')
                value = value[int(part)] if isinstance(value, list) else value[part]
            return value

        assert outcome['status'] == ('failed' if later_failed else 'succeeded')
        assert any(call['error'] is not None and call['error']['details'].get('response_payload') == 'e3'
            for call in map(resolve_call, calls))
        if later_failed:
            assert document['error']['details']['reason'] == 'exit_nonzero'
        assert transport.aperture_seen
        assert owned.connection.execute('SELECT result_event_id IS NOT NULL FROM operation_attempts').fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_capture_completion_preserves_all_database_errors_for_fatal_diagnostic(
        tmp_path, capture_resources, result_case, monkeypatch):
    from camctl.capture.handlers import capture_handler
    from ..support.capture_diagnostic_cases import ActualStorageFault, diagnostic_database
    from ..support.database import database_path
    case = result_case
    owned, runtime, aid, handler = await case.world(tmp_path, 'photo')
    secondary = RuntimeError('observer failed')
    fault = ActualStorageFault(secondary=secondary)
    actual = CaptureRepository.finish_capture
    def save(repository, request, key, current):
        fault.armed = True
        return actual(repository, request, key, current)
    monkeypatch.setattr(CaptureRepository, 'finish_capture', save)
    await capture_resources.close()
    try:
        async with diagnostic_database(database_path(owned), fault) as database:
            runtime = await case.factory(database, 'cam-1')
            with pytest.raises(ConsistencyError) as raised:
                await capture_handler(handler)(aid, runtime)
            assert isinstance(raised.value.__cause__, BaseExceptionGroup)
            assert raised.value.__cause__.exceptions == (fault.primary, secondary)
            assert fault.commits == fault.failed_closes == 1
            assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (aid,)).fetchone() == (2,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('operation', ['probe', 'repair'])
@pytest.mark.parametrize('ran', [True, False])
async def test_uncompleted_media_tool_logs_actual_failure_and_keeps_cancel_control(
        media_pipeline, media_cases, monkeypatch, operation, ran):
    pipeline = media_pipeline
    from decimal import Decimal
    from camctl.bootstrap import capture_assembly
    from camctl.capture.media_flow import run_recording_media
    from camctl.host_files.media import MediaProbe, MediaStream
    from camctl.host_files.tasks import FileTaskResult
    from ..support.capture_media_cases import ScriptTools, assembled_media, diagnostic_media_ports
    successful = MediaProbe(Decimal(200), None, (
        MediaStream(0, 'video', 'h264', 64, 64, Decimal(200), False),))
    diagnostic_media_ports(monkeypatch, successful_probe=successful)
    calls = []
    async def uncompleted(*args, task_id, **kwargs):
        calls.append(task_id)
        return FileTaskResult(task_id, ran, error='file_conflict: resource occupied' if ran else None)
    monkeypatch.setattr(capture_assembly, 'probe_media' if operation == 'probe' else 'repair_media', uncompleted)
    flow = await assembled_media(pipeline, None, ScriptTools(successful))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            step = await run_recording_media(flow, 1, 1, pipeline[2])
    assert step.error is not None
    assert step.phase.value == ('probe_not_completed' if operation == 'probe' else 'tool_not_completed')
    assert len(calls) == 1
    assert flow.file_executor.unfinished_files() == ()
    if ran:
        document, = _documents(records)
        assert document['error']['message'] == 'file_conflict: resource occupied'
    else:
        assert not records


async def test_handled_segment_save_error_keeps_secondary_errors_in_public_diagnostic(
        tmp_path, capture_resources, monkeypatch):
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository
    from ..support.bootstrap_read_cases import inspection_case
    from ..support.capture_diagnostic_cases import ActualStorageFault, diagnostic_database
    from ..support.database import database_path
    secondary = OSError('database notification failed')
    fault = ActualStorageFault(secondary=secondary)
    from ..support.segment_inspection_cases import inspection_source
    source = await inspection_source(tmp_path)
    world = await inspection_case(source, capture_resources)
    actual = SegmentInspectionRepository.save_result
    def save(repository, request, key, current):
        fault.armed = True
        return actual(repository, request, key, current)
    monkeypatch.setattr(SegmentInspectionRepository, 'save_result', save)
    await capture_resources.close()
    records = []
    try:
        async with diagnostic_database(database_path(world.owned), fault) as database:
            world.database = database
            await world.rebuild()
            with error_diagnostics_context(None, records.append):
                async with diagnostic_scope():
                    result = await world.inspect()
            assert result.reason == 'result_save_unknown'
            document, = _documents(records)
            assert document['context']['inspection_id'] == result.inspection_id
            assert 'database notification failed' in json.dumps(document)
            assert 'database failed' in json.dumps(document)
            assert fault.commits == fault.failed_closes == 1
            assert len(world.reader.requests) == 1
            world.held_ticket()
    finally:
        await world.close()
        world.owned.connection.close()


async def test_invalid_call_diagnostic_preserves_original_outcome_before_fatal_handling(tmp_path, diagnostic_case):
    from camctl.capture.handlers import capture_handler
    from camctl.logging_runtime.diagnostics import record_error
    case = diagnostic_case
    def rejected(actual):
        raise ValueError('diagnostic rejected')
    runtime = await call_world(case, tmp_path, diagnostics_check=rejected, control=lambda request:
        actual_call('control', request.ticket, error=ErrorValue('device_error', 'device', {'operation': 'control'})))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(ConsistencyError) as raised:
                await capture_handler('camera_record')(case.action_id, runtime)
            record_error(raised.value, stage='fatal')
    document, = _documents(records)
    assert document['details']['outcome']['diagnostics']['calls'][0]['stderr']['hex'] == b'camera error\xff\n'.hex()
    request, = case.control.calls
    assert document['context']['ticket']['run_id'] == request.ticket.run_id
    assert case.owned.connection.execute('SELECT status,result_event_id FROM operation_attempts').fetchone() == (1, None)


async def test_preparation_validation_error_keeps_original_tool_bytes_and_exception(tmp_path, diagnostic_case):
    from camctl.logging_runtime.diagnostics import record_error
    from ..support.capture_diagnostic_cases import preparation_flow
    def rejected(actual):
        raise ValueError('preparation diagnostic rejected')
    case = diagnostic_case
    preparation, flow, context = await preparation_flow(case, tmp_path, diagnostics_check=rejected)
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(ValueError) as raised:
                await flow(context)
                await flow.settle()
            record_error(raised.value, stage='fatal')
        await flush_diagnostics()
    document, = _documents(records)
    assert document['error']['message'] == 'preparation diagnostic rejected'
    assert document['details']['outcome']['diagnostics']['calls'][0]['stderr']['hex'] == b'camera error\xff\n'.hex()
    assert len(preparation.calls) == 1 and case.control.calls == []
    assert case.owned.connection.execute('SELECT status,result_event_id FROM operation_attempts').fetchone() == (1, None)


@pytest.mark.parametrize('code,stage,details,expected', [
    ('canceled', 'dispatch', {}, False),
    ('window_ended', 'dispatch', {}, False),
    ('start_preparation_timeout', 'dispatch', {}, True),
    ('transport_cancelled', 'transport', {}, False),
    ('transport_cancelled', 'transport', {'signal_failures': [{'stage': 'terminate', 'message': 'denied'}]}, True),
    ('transport_cancelled', 'transport', {'output_failure': 'pipe close failed'}, True),
])
async def test_call_control_classification_keeps_actual_timeout_and_cleanup_errors(
        tmp_path, diagnostic_case, code, stage, details, expected):
    from camctl.capture.handlers import capture_handler
    case = diagnostic_case
    actual = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        error=ErrorValue(code, stage, details), settlement=Settlement(
            SettlementBasis.NOT_DISPATCHED, EvidenceValue('dispatch_prevented', 1)))
    runtime = await call_world(case, tmp_path, control=actual)
    case.boundary = 'control'
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(SavedCallBoundary):
                await capture_handler('camera_record')(case.action_id, runtime)
    assert bool(records) is expected
    assert len(case.control.calls) == 1
    assert case.owned.connection.execute('SELECT status,result_event_id IS NOT NULL FROM operation_attempts').fetchone() == (3, 1)


async def test_result_page_call_error_and_validation_error_have_independent_records(tmp_path, result_case):
    from dataclasses import replace
    from camctl.capture.handlers import capture_handler
    from camctl.operations.tool_diagnostics import DiagnosticsValidationError
    from camctl.logging_runtime.diagnostics import record_error
    from .result_consumer_fixtures import RESULT_EVIDENCE, result_driver
    case = result_case
    from decimal import Decimal
    from camctl.operations.attempts import AttemptConfig
    owned, runtime, aid, handler = await case.world(tmp_path, 'photo',
        assembly_options={'check_config': AttemptConfig(3, Decimal(1), Decimal(2))}, evidence_contracts=(
        replace(RESULT_EVIDENCE.contract('results_returned', 1), max_diagnostic_calls=1),))
    outcome = replace(actual_call('result', SimpleNamespace(target_id='1'),
        error=ErrorValue('device_error', 'result', {'reason': 'device-call-error'})),
        diagnostics=ToolDiagnostics(2, ()))
    from camctl.capture.result_inputs import ResultPage
    case.results.delegate = SimpleNamespace(list_page=AsyncMock(return_value=ResultPage((), None, None, False, None, outcome, False)))
    records = []
    try:
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                with pytest.raises(DiagnosticsValidationError) as raised:
                    await capture_handler(handler)(aid, runtime)
                record_error(raised.value, stage='outer')
            async with diagnostic_scope():
                record_error(raised.value, stage='outer_replay')
        documents = _documents(records)
        assert len(documents) == 2
        assert documents[0]['error']['details']['reason'] == 'device-call-error'
        assert documents[1]['error']['type'].endswith('DiagnosticsValidationError')
        assert documents[1]['error']['message']
        assert case.results.delegate.list_page.await_count == 1
        assert owned.connection.execute("SELECT result_event_id FROM operation_attempts WHERE run_id=(SELECT id FROM operation_runs WHERE action_id=? AND kind=7)", (aid,)).fetchone() == (None,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('stage', ['observation', 'checksum', 'synchronization'])
async def test_media_stopping_retains_actual_file_error(pipeline, capture_resources, stage):
    from pathlib import Path
    from camctl.host_files.io import HashResult, SyncResult, DirectorySyncStage
    from camctl.host_files.media import MediaArtifact, MediaToolResult
    from camctl.host_files.models import FileObservation, FileObservationKind
    failure = OSError(5, 'media file observation failed')
    failure.__cause__ = ValueError('original file fact')
    artifact = MediaArtifact('tool_cancelled' if stage == 'observation' else None,
        FileObservation(FileObservationKind.ERROR if stage == 'observation' else FileObservationKind.VALID_OBJECT,
            Path('/staging/output.mp4'), error=failure if stage == 'observation' else None,
            is_file=None if stage == 'observation' else True, size_bytes=1),
        checksum=HashResult(None, 1, 'checksum close failed') if stage == 'checksum' else None,
        synchronization=SyncResult(False, DirectorySyncStage.NOT_ATTEMPTED, 'file sync failed')
            if stage == 'synchronization' else None,
        postprocessing_stopped=stage == 'checksum',
        tool_diagnostics=MediaToolResult(stopped_before_start=True) if stage == 'observation' else None)
    from ..support.capture_diagnostic_cases import media_saves
    from .test_capture_contract import _NOW
    saves = await media_saves(pipeline, capture_resources, repair=True)
    records = []
    try:
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                receipt = await saves.save_repair_result(RepairResultSave(
                    1, RepairOutcome.FAILED, _NOW, error=ProcessingError('tool_failed', 'repair', {}),
                    artifact=artifact))
        assert receipt.disposition.value == 'saved'
        document, = _documents(records)
        encoded = json.dumps(document)
        assert {'observation': 'original file fact', 'checksum': 'checksum close failed',
            'synchronization': 'file sync failed'}[stage] in encoded
        if stage == 'observation':
            assert document['details']['artifact']['observation']['error']['errno'] == 5
        assert pipeline[0].connection.execute('SELECT repair_state FROM recording_processing WHERE id=1').fetchone() == (6,)
    finally:
        pipeline[0].connection.close()
