"""原始结果来源、完成元信息和终态保留通过正式入口分别核对。"""
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import ResultFilesPort, capture_handler
from camctl.capture.models import ResultSetPhase, ResultSetSave
from camctl.capture.results import FileKind
from camctl.capture.result_inputs import ListedResult, observed_file
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptConfig, AttemptFinish, RunFinish, RunOutcome
from camctl.operations.models import ErrorValue
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository

from ..support.result_cases import result_case, result_case_for, begin_result_attempt
from ..support.natural_sources import save_historical_source_page, save_historical_direct_files
from .result_consumer_fixtures import ResultCatalog, complete_natural_wait
from .result_source_fixtures import save_v1_round, current_v1_entries
from .test_capture_contract import ResultsDouble, _entry, _PAGE_EVIDENCE, _NOW
from .test_result_device_completion import device_pages, _COMPLETION
from .test_timelapse_host_timer import HostCatalog
from .test_result_page_history import _actual

pytestmark = pytest.mark.asyncio


async def test_direct_record_file_does_not_consume_unrelated_stop_history(tmp_path):
    case = result_case_for()
    owned, runtime, aid, handler = await case.world(tmp_path, 'record')
    try:
        stop_id, = owned.connection.execute(
            "SELECT t.result_event_id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.responsibility_key=?", (f'stop/{aid}',)).fetchone()
        owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=?",
            ('{"reason":1,"evidence":{},"rows":[]}', stop_id))
        owned.connection.commit()
        activity_id, = owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?', (aid,)).fetchone()
        case.results.delegate = ResultsDouble({activity_id: (_entry('clip'),)})
        await capture_handler(handler)(aid, runtime)
        assert owned.connection.execute(
            "SELECT json_extract(identity_key,'$[2]'), completion_state, json_extract(completion_evidence_json,'$.basis') FROM device_files").fetchone() == ('clip', 3, 1)
        case.driver.control.assert_awaited_once()
        case.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def _original_direct_history(tmp_path):
    case = result_case_for()
    owned, runtime, aid, handler = await case.world(tmp_path, 'timelapse')
    activity_id, = owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?', (aid,)).fetchone()
    ticket = begin_result_attempt(owned, aid, runtime.check_config, case.clock.wall)
    port = ResultsDouble({activity_id: (_entry('clip'),)})
    page = await save_historical_source_page(owned, aid, ticket, port, _PAGE_EVIDENCE, case.clock.wall)
    save_historical_direct_files(owned, aid, page)
    wait_id = await complete_natural_wait(runtime, aid)
    assert CaptureRepository().read_result_source(ticket, owned).kind.value == 'direct'
    observation = {'first_page_event_id': page.ref.event_id,
        'last_page_event_id': page.ref.event_id, 'file_count': 1}
    proof = {'method': 'time_and_outputs', 'wait_completed_event_id': wait_id,
        'observation': observation}
    command = ResultSetSave(aid, case.clock.wall, ResultSetPhase.COMPLETE,
        contract='result_files_listed/v2', observation=observation,
        capture={'status': 'completed'}, evidence=proof)
    finish = AttemptFinish(ticket, validate_outcome(ticket, page.page.outcome, _PAGE_EVIDENCE),
        case.clock.wall, run_finish=RunFinish(RunOutcome.SUCCEEDED))
    return owned, runtime, aid, page, wait_id, command, finish


async def test_original_direct_query_cannot_use_a_later_natural_wait(tmp_path):
    owned, runtime, aid, page, wait_id, command, finish = await _original_direct_history(tmp_path)
    try:
        intent, = owned.connection.execute('SELECT intent_event_id FROM operation_attempts WHERE run_id=?',
            (finish.ticket.run_id,)).fetchone()
        assert intent < wait_id
        assert owned.connection.execute('SELECT completion_state FROM device_files').fetchone() == (3,)
        before = tuple(owned.connection.iterdump())
        receipt = CaptureRepository().finish_result_check(finish, command, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
        assert owned.connection.execute(
            'SELECT activity_state,occupancy_state,result_set_state,completion_basis FROM device_activities').fetchone() == (1, 1, 1, 1)
        assert (await runtime.action(aid))['status'] == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('boundary', ['repository', 'event_guard'])
async def test_natural_result_boundary_rejects_original_intent_before_wait(tmp_path, boundary):
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext, EventValidationError, validate_event
    from camctl.persistence.transaction import event_envelope, row_facts, update_change
    owned, runtime, aid, page, wait_id, command, finish = await _original_direct_history(tmp_path)
    try:
        before = tuple(owned.connection.iterdump())
        if boundary == 'repository':
            receipt = CaptureRepository().finish_result_check(finish, command, new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        else:
            state = {table: {row[0]: row_facts(owned.connection, table, row[0])
                for row in owned.connection.execute(f'SELECT id FROM {table}')}
                for table in ('actions', 'device_activities', 'operation_runs', 'operation_attempts')}
            cursor = owned.connection.execute('SELECT * FROM history_events')
            columns = [column[0] for column in cursor.description]
            state['history_events'] = {row[0]: dict(zip(columns, row)) for row in cursor}
            event_id = owned.connection.execute('SELECT MAX(id)+1 FROM history_events').fetchone()[0]
            txn_id = owned.connection.execute('SELECT MAX(id)+1 FROM history_transactions').fetchone()[0]
            event = event_envelope(event_id, txn_id, 16, 1, (update_change('device_activities', aid,
                {'result_set_state': 1, 'result_check_json': None, 'completion_basis': 1,
                 'completion_evidence_json': None, 'capture_json': None},
                {'result_set_state': 3, 'result_check_json': {'contract': command.contract,
                    'outcome': 1, 'observation': command.observation}, 'completion_basis': 3,
                 'completion_evidence_json': command.evidence, 'capture_json': {'status': 'completed'}}),), runtime.wall_us())
            change_seq = owned.connection.execute('SELECT MAX(change_seq)+1 FROM history_events').fetchone()[0]
            event = replace(event, change_seq=change_seq)
            context = EventContext(TransactionRange(txn_id, event_id, event_id),
                {('device_activities', aid): ('action', aid)}, state)
            with pytest.raises(EventValidationError, match="自然集合的原结果来源无效"):
                validate_event(event, context)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


async def _host_world(tmp_path, *, catalog=None):
    case = result_case_for()
    owned, runtime, aid, handler = await case.world(tmp_path, 'timelapse', start=False,
        independent_activity=True, catalog=catalog or HostCatalog(),
        evidence_contracts=(_COMPLETION,))
    await capture_handler(handler)(aid, runtime)
    return case, owned, runtime, aid, handler


@pytest.mark.parametrize('reopen', [False, True])
async def test_known_current_format_finishes_with_original_completion_metadata(tmp_path, monkeypatch, reopen):
    case, owned, runtime, aid, handler = await _host_world(tmp_path)
    class ProcessStopped(Exception):
        pass
    try:
        advance = capture_handler(handler)
        device_pages(runtime, completion_page=1, case='unknown_format')
        case.clock.monotonic = 15_000_000_000
        await advance(aid, runtime)
        files = owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files ORDER BY id').fetchall()
        assert len(files) == 2
        later = device_pages(runtime, completion_page=None)
        case.clock.monotonic += 3_000_000_000
        if reopen:
            def pause(*args, **kwargs):
                raise ProcessStopped()
            with monkeypatch.context() as fault:
                fault.setattr(CaptureRepository, 'finish_capture', pause)
                with pytest.raises(ConsistencyError) as stopped:
                    await advance(aid, runtime)
                assert isinstance(stopped.value.__cause__, ProcessStopped)
            assert owned.connection.execute('SELECT result_set_state FROM device_activities').fetchone() == (3,)
            runtime = await case.reopen(owned, cold=True)
        await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 3
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files ORDER BY id').fetchall() == files
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (2,)
        assert later.list_results.await_count == 2
        case.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


class MinimumHostCatalog(HostCatalog):
    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if definition is None or action_type != 'camera_timelapse':
            return definition
        factory = definition.task_factory
        return replace(definition, task_factory=lambda params: replace(factory(params), product_rules=(
            {'kind': 'video', 'format_id': 'mp4', 'min_count': 1, 'exact_count': None,
             'require_pairing': False, 'min_size_bytes': 51},)))


async def test_current_minimum_size_rejection_does_not_fall_back_to_unknown_retained_format(tmp_path):
    case, owned, runtime, aid, handler = await _host_world(tmp_path, catalog=MinimumHostCatalog())
    try:
        advance = capture_handler(handler)
        device_pages(runtime, completion_page=1, case='unknown_format')
        case.clock.monotonic = 15_000_000_000
        await advance(aid, runtime)
        original = owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files ORDER BY id').fetchall()
        later = device_pages(runtime, completion_page=None)
        case.clock.monotonic += 3_000_000_000
        await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 4
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files ORDER BY id').fetchall() == original
        assert later.list_results.await_count == 2
        case.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def _v1_world(tmp_path, consumer, *, catalog=None, start=True):
    case = result_case_for()
    config = AttemptConfig(2, Decimal('1'), Decimal(0))
    owned, runtime, aid, handler = await case.world(tmp_path, consumer, catalog=catalog,
        start=start, result_page_contract=False, assembly_options={'check_config': config})
    if consumer == 'timelapse':
        await complete_natural_wait(runtime, aid)
    return case, owned, runtime, aid, handler, config


@pytest.mark.parametrize('consumer', ['photo', 'record', 'timelapse'])
@pytest.mark.parametrize('latest_present', [False, True])
async def test_v1_saved_current_set_and_terminal_retention_have_separate_inputs(tmp_path, consumer, latest_present):
    case, owned, runtime, aid, handler, config = await _v1_world(tmp_path, consumer)
    port = ResultsDouble({aid: (_entry('auxiliary', kind=FileKind.OTHER),)})
    case.results.delegate = port
    try:
        runtime, first = await save_v1_round(owned, runtime, aid, port, config, case.clock.wall)
        original = owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall()
        latest = (_entry('current', kind=FileKind.PHOTO if consumer == 'photo' else FileKind.VIDEO),) if latest_present else ()
        port.files_by_action[aid] = latest
        runtime, second = await save_v1_round(owned, runtime, aid, port, config, case.clock.wall)
        current = current_v1_entries(owned, second.ticket)
        assert [(entry.identity, entry.kind, entry.format_id) for entry in current] == [
            (entry.identity, entry.kind, entry.format_id) for entry in latest]
        await capture_handler(handler)(aid, runtime)
        assert (await runtime.action(aid))['status'] == 4
        assert port.calls == [aid, aid]
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files WHERE id=?', (original[0][0],)).fetchall() == original
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (1 + int(latest_present),)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities').fetchone() == (
            (3, 2) if consumer == 'record' else (2, 1) if consumer == 'photo' else (1, 1))
    finally:
        owned.connection.close()


@pytest.mark.parametrize('consumer', ['photo', 'record'])
async def test_v1_current_completion_must_match_original_fixed_size(tmp_path, consumer):
    case, owned, runtime, aid, handler, config = await _v1_world(tmp_path, consumer)
    kind = FileKind.PHOTO if consumer == 'photo' else FileKind.VIDEO
    port = ResultsDouble({aid: (_entry('current', kind=kind),)})
    try:
        runtime, first = await save_v1_round(owned, runtime, aid, port, config, case.clock.wall)
        original = owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall()
        port.files_by_action[aid] = (replace(_entry('current', kind=kind), size_bytes=99),)
        runtime, second = await save_v1_round(owned, runtime, aid, port, config, case.clock.wall)
        current = current_v1_entries(owned, second.ticket)
        assert [(entry.identity, entry.complete, entry.size_bytes) for entry in current] == [('current', True, 99)]
        runtime = await case.reopen(owned, cold=True)
        assert current_v1_entries(owned, second.ticket) == current
        retained = CaptureRepository().read_result_sources(second.ticket, None, 128, owned)
        assert [(file.file_id, file.complete, file.size_bytes) for _, file, _ in retained.items] == [('current', True, 4096)]
        await capture_handler(handler)(aid, runtime)
        assert (await runtime.action(aid))['status'] == 4
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall() == original
    finally:
        owned.connection.close()


async def test_record_cancel_binding_failure_keeps_files_without_outputs(tmp_path):
    from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
    case, owned, runtime, aid, handler, config = await _v1_world(tmp_path, 'record', start=False)
    try:
        await capture_handler(handler)(aid, runtime)
        port = ResultsDouble({aid: (_entry('current'),)})
        case.results.delegate = port
        runtime, first = await save_v1_round(owned, runtime, aid, port, config, case.clock.wall)
        original = owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall()
        _apply_public_cancel(owned, aid, case.clock.wall)
        runtime = await case.reopen(owned, cold=True, assembly_options={'devices': {}})
        await capture_handler(handler)(aid, runtime)
        assert (await runtime.action(aid))['status'] == 6
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall() == original
        assert port.calls == [aid]
        case.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()


_KIND_BOUNDARIES = [
    ('record', 'live', 'opaque', 'known', 0, 1),
    ('record', 'live', 'parsed', 'known', 0, 1),
    ('record', 'cache', 'opaque', 'known', 50, 51),
    ('record', 'cache', 'parsed', 'known', 50, 51),
    ('record', 'saved', 'opaque', 'known', 50, 51),
    ('record', 'live', 'opaque', 'known', 50, 1),
    ('record', 'live', 'opaque', 'unknown', 50, 1),
    ('record', 'cache', 'opaque', 'unknown', 50, 1),
    ('record', 'saved', 'opaque', 'unknown', 50, 1),
    ('record', 'live', 'parsed', 'explicit_other', 50, 1),
    ('photo', 'live', 'opaque', 'known', 50, 1),
    ('photo', 'live', 'parsed', 'known', 50, 1),
    ('photo', 'saved', 'opaque', 'known', 50, 1),
    ('photo', 'live', 'opaque', 'unknown', 50, 1),
    ('timelapse', 'saved', 'opaque', 'known', 0, 1),
    ('timelapse', 'saved', 'opaque', 'known', 50, 51),
    ('timelapse', 'saved', 'opaque', 'known', 50, 1),
    ('timelapse', 'saved', 'opaque', 'unknown', 50, 1),
]


@pytest.mark.parametrize('consumer,view,proof,current_kind,size,minimum', _KIND_BOUNDARIES,
    ids=['-'.join(map(str, case)) for case in _KIND_BOUNDARIES])
async def test_v1_terminal_uses_known_typed_kind_and_retains_unknown_current(
        tmp_path, consumer, view, proof, current_kind, size, minimum):
    """原结果和首次完成事实经公开仓储保存，终态由正式处理器消费。"""
    from unittest.mock import create_autospec

    class Catalog(ResultCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is None or action_type == 'camera_take_photo':
                return definition
            factory = definition.task_factory
            return replace(definition, task_factory=lambda params: replace(factory(params), product_rules=(
                {'kind': 'video', 'format_id': 'osv', 'min_count': 1, 'exact_count': None,
                 'require_pairing': False, 'min_size_bytes': minimum},)))

    case, owned, runtime, aid, handler, config = await _v1_world(tmp_path, consumer, catalog=Catalog())
    kind = FileKind.PHOTO if consumer == 'photo' else FileKind.VIDEO
    format_id = 'jpeg' if consumer == 'photo' else 'osv'
    retained_kind = FileKind.OTHER if current_kind == 'known' else kind
    retained_format = None if current_kind == 'known' else format_id
    port = create_autospec(ResultFilesPort, instance=True)
    case.results.delegate = port
    calls = []

    async def actual(ticket, *, timeout_s):
        calls.append(ticket)
        raw = {'identity': 'current', 'locator': {'path': '/DCIM/current'},
               'complete': True, 'size_bytes': size,
               'kind': retained_kind.value if len(calls) == 1 else kind.value,
               'format_id': retained_format if len(calls) == 1 else format_id}
        if len(calls) == 2 and current_kind != 'known':
            raw['format_id'] = None
            if current_kind == 'unknown':
                del raw['kind']
            else:
                raw['kind'] = 'other'
        outcome = _actual(ticket, entries=[raw], error=ErrorValue('device_error', 'device'))
        outcome = replace(outcome, observations=(DeviceObservation('result_files_listed', 1,
            {'activity_id': ticket.target_id, 'entries': [raw]}),))
        entry = observed_file(raw)
        if proof == 'opaque':
            entry = replace(entry, evidence={'listing': 'current'})
        return ListedResult((entry,), outcome)

    port.list_round.side_effect = actual
    try:
        for _ in range(2):
            runtime, saved = await save_v1_round(owned, runtime, aid, port, config, case.clock.wall)
            if len(calls) == 1:
                original = owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall()
        # 同一会话重入和新 worker 均消费原可靠结果，不构造内部缓存对象。
        if view == 'saved':
            runtime = await case.reopen(owned, cold=True)
        await capture_handler(handler)(aid, runtime)
        if view == 'cache':
            before = tuple(owned.connection.iterdump())
            await capture_handler(handler)(aid, runtime)
            assert tuple(owned.connection.iterdump()) == before
        current = current_v1_entries(owned, saved.ticket)
        expected_current_kind = kind if current_kind == 'known' else FileKind.OTHER
        assert [(entry.kind, entry.format_id) for entry in current] == [
            (expected_current_kind, format_id if current_kind == 'known' else None)]
        retained = CaptureRepository().read_result_sources(saved.ticket, None, 128, owned)
        assert [(entry.kind, entry.format_id) for entry, _, _ in retained.items] == [
            (retained_kind, retained_format)]
        expected_outputs = int(consumer == 'photo' or size >= minimum)
        assert (await runtime.action(aid))['status'] == 4
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (expected_outputs,)
        assert owned.connection.execute('SELECT id,completion_evidence_json,size_bytes FROM device_files').fetchall() == original
        assert len(calls) == port.list_round.await_count == 2
        case.driver.control.assert_awaited_once()
        if consumer == 'record':
            case.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()
