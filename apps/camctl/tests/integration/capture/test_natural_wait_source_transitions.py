"""RESULTS沿原意图选择来源；后来的取消与STOP不改写原页。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..support.natural_sources import cold_source_runtime, legacy_result_runtime
from .result_consumer_fixtures import returned
from .test_native_completion_context import _osv
from .test_natural_wait_time_eligibility import _world, _request_cancel, _progress_cancel
from .test_natural_wait_retention import _pages, _retained, _other
from .test_result_page_history import _actual, _CURSOR
from .test_record_file_completion_wait import _completed_result

pytestmark = pytest.mark.asyncio


async def _finish_cancel(owned, runtime, wall, confirmed):
    await _progress_cancel(runtime, wall)
    origin, = owned.connection.execute('SELECT id FROM actions WHERE type=6').fetchone()
    assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (origin,)).fetchone() == (3 if confirmed else 4,)


def _stop_driver(outcome):
    driver = create_autospec(StopDriver, instance=True)
    async def stop(request):
        actual = (returned('stop', 'stop_confirmed', request.ticket.target_id)
            if outcome == 'confirmed' else CallOutcome(
                status=AttemptStatus.FAILED if outcome == 'failed' else AttemptStatus.UNKNOWN,
                effect=EffectState.UNKNOWN, error=ErrorValue('device_error', 'device'),
                settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
        return DeviceCallResult.from_outcome(actual)
    driver.stop.side_effect = stop
    return driver


async def _source_world(tmp_path, startup, stop_outcome, **options):
    stop = _stop_driver(stop_outcome)
    options.setdefault('check_config', AttemptConfig(1, Decimal('1'), Decimal(0)))
    world = await _world(tmp_path, startup, stopper=stop, **options)
    return world, stop


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('stop_outcome', ['confirmed', 'failed', 'unknown'])
@pytest.mark.parametrize('bad', [False, True])
async def test_public_cancel_during_natural_read_keeps_original_basis_and_page_time(tmp_path, startup, stop_outcome, bad):
    world, stop = await _source_world(tmp_path, startup, stop_outcome)
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    contexts = []
    async def actual(request, batch):
        contexts.append(request.params['completion_context'])
        await _request_cancel(runtime, aid, wall)
        if bad: wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        raw = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 6
        from camctl.contracts.json_values import parse_exact_json
        capture = owned.connection.execute('SELECT capture_json FROM device_activities WHERE action_id=?', (aid,)).fetchone()[0]
        assert capture is None or parse_exact_json(capture)['status'] != 'completed'
        retained = _retained(owned, aid)
        assert len(retained) == int(not bad)
        if not bad:
            from camctl.contracts.json_values import parse_exact_json
            proof = parse_exact_json(retained[0][2])
            assert proof['basis'] == 2
            assert proof['wait_completed_event_id'] == contexts[0]['wait_completed_event_id']
            assert proof['start_result_event_id'] == contexts[0]['start_result_event_id']
            assert 'stop_result_event_id' not in proof
        assert _pages(owned) == raw and len(contexts) == 1
        assert raw[0][1] == (due-1 if bad else due+100)
        assert stop.stop.await_count == result.list_results.await_count == 1
        state = owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone()
        assert state == ((1, 1) if bad and stop_outcome != 'confirmed' else (3, 2))
        await _finish_cancel(owned, runtime, wall, stop_outcome == 'confirmed' and not bad)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('bad_last', [False, True])
async def test_natural_pages_cross_stop_with_original_context_cursor_and_budget(tmp_path, startup, bad_last):
    world, stop = await _source_world(tmp_path, startup, 'confirmed')
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    contexts = []
    async def actual(request, batch):
        contexts.append(request.params['completion_context'])
        if len(contexts) == 1:
            assert stop.stop.await_count == 0
            await _request_cancel(runtime, aid, wall)
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], next_cursor=_CURSOR))
        assert stop.stop.await_count == 1
        assert request.params['cursor'] == _CURSOR.as_json()
        if bad_last: wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_other()], cursor=_CURSOR, finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 6
        assert len(contexts) == 2 and contexts[0] == contexts[1]
        assert len(_retained(owned, aid)) == (1 if bad_last else 2)
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key=?", (f'results/{aid}',)).fetchone() == (1,)
        from camctl.contracts.json_values import parse_exact_json
        capture = owned.connection.execute('SELECT capture_json FROM device_activities WHERE action_id=?', (aid,)).fetchone()[0]
        assert capture is None or parse_exact_json(capture)['status'] != 'completed'
        await _finish_cancel(owned, runtime, wall, not bad_last)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('valid_wait', [False, True])
async def test_real_stop_source_keeps_independent_wait_even_when_natural_wait_exists(tmp_path, startup, valid_wait):
    from .result_consumer_fixtures import complete_natural_wait, _prepare_through_handler
    world, stop = await _source_world(tmp_path, startup, 'confirmed',
        check_config=AttemptConfig(3, Decimal('1'), Decimal(0)))
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        assert await complete_natural_wait(owned, aid, wall[0]) is not None
        await _request_cancel(runtime, aid, wall)
        await _prepare_through_handler(runtime, aid, 'camera_timelapse', 'stop')
        async def actual(request, batch):
            context = request.params['completion_context']
            assert context['kind'] == 'timelapse_stop'
            assert 'startup_margin_ms' not in context
            wall[0] = due-1
            if not valid_wait:
                return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
            completion = {'method': 'stop_return_and_wait', 'version': 1,
                'activity_id': context['activity_id'], 'stop_result_event_id': context['stop_result_event_id'],
                'required_wait_ms': 30000, 'observed_wait_ns': 30000000000, 'completed': True}
            return _completed_result(request.ticket, context, entries=[_osv()], finalized=True, completion=completion)
        result.list_results.side_effect = actual
        if valid_wait:
            await advance(aid, runtime)
            assert (await runtime.action(aid))['status'] == 6 and len(_retained(owned, aid)) == 1
            from camctl.contracts.json_values import parse_exact_json
            proof = parse_exact_json(_retained(owned, aid)[0][2])
            assert proof['basis'] == 3 and 'wait_completed_event_id' not in proof
        else:
            with pytest.raises(ConsistencyError): await advance(aid, runtime)
            assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (0,)
        assert result.list_results.await_count == stop.stop.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('save_kind', ['page', 'file'])
@pytest.mark.parametrize('committed', [False, True])
@pytest.mark.parametrize('stop_in_call', [False, True])
async def test_unknown_original_save_crosses_stop_and_connection_without_reread(
        tmp_path, capture_resources, monkeypatch, startup, save_kind, committed, stop_in_call):
    from camctl.persistence.models import DbOutcome
    from camctl.persistence.repositories.capture import CaptureRepository
    state = {}
    world, stop = await _source_world(tmp_path, startup, 'confirmed', state=state)
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    contexts = []
    async def actual(request, batch):
        contexts.append(request.params['completion_context'])
        await _request_cancel(runtime, aid, wall)
        if stop_in_call:
            await advance(aid, runtime)
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    name = 'save_result_page' if save_kind == 'page' else 'save_file_completion'
    save = getattr(CaptureRepository, name)
    requests = []
    unknown_count = 1 if save_kind == 'page' else 2
    def uncertain(repository, command, key, connection):
        requests.append((command, key))
        if len(requests) <= unknown_count:
            if committed and len(requests) == 1:
                assert save(repository, command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(repository, command, key, connection)
    monkeypatch.setattr(CaptureRepository, name, uncertain)
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        with pytest.raises(ConsistencyError): await advance(aid, runtime)
        assert len(requests) == unknown_count
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        await capture_resources.restart_database(owned)
        runtime = await state['factory'](owned)
        wall[0] = due-1
        await advance(aid, runtime)
        assert all(item == requests[0] for item in requests)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 6
        assert len(_retained(owned, aid)) == 1
        from camctl.contracts.json_values import parse_exact_json
        proof = parse_exact_json(_retained(owned, aid)[0][2])
        assert proof['basis'] == 2
        assert proof['wait_completed_event_id'] == contexts[0]['wait_completed_event_id']
        assert 'stop_result_event_id' not in proof
        assert result.list_results.await_count == stop.stop.await_count == 1
        assert len(_pages(owned)) == 1 and _pages(owned)[0][1] == due+100
        await _finish_cancel(owned, runtime, wall, True)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('stop_outcome', ['confirmed', 'failed', 'unknown'])
async def test_reliable_natural_page_recovers_original_source_without_process_cache(
        tmp_path, capture_resources, startup, stop_outcome):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.bootstrap.flows import cancel_flow
    from camctl.capture.handlers import capture_handler
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.ports import DriverDeclaration, ResultDriver
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.capture import capture_runtime
    from .result_consumer_fixtures import RESULT_EVIDENCE, consumer_world
    from .test_capture_contract import _NOW
    from .test_natural_wait_time_eligibility import SmallNativeCatalog
    from .test_record_file_completion_wait import _REGISTRY
    from .test_result_page_history import _BINDING

    wall, mono = [_NOW], [5_000_000_000]
    result = create_autospec(ResultDriver, instance=True)
    stop = create_autospec(StopDriver, instance=True)
    evidence = _REGISTRY.with_contract(RESULT_EVIDENCE.contract('timelapse_sent', 1))
    inputs = dict(evidence=evidence, results=DriverResultListing(result, _BINDING, evidence),
        stopper=stop, wall_us=lambda: wall[0], monotonic_ns=lambda: mono[0],
        check_config=AttemptConfig(1, Decimal('1'), Decimal(0)),
        wait_config=lambda action: CaptureWaitConfig(1000, 200, 0, startup))

    async def assemble(owned, *, driver):
        activity_id, = owned.connection.execute('SELECT id FROM device_activities').fetchone()
        outcome = (returned('stop', 'stop_confirmed', activity_id) if stop_outcome == 'confirmed' else CallOutcome(
            status=AttemptStatus.FAILED if stop_outcome == 'failed' else AttemptStatus.UNKNOWN,
            effect=EffectState.UNKNOWN, error=ErrorValue('device_error', 'device'),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
        stop.stop.return_value = DeviceCallResult.from_outcome(outcome)
        inputs['driver'] = driver
        inputs['drivers'] = DriverRegistry((DriverEntry('camctl-adb',
            SimpleNamespace(control=driver.control, stop=stop.stop),
            DriverDeclaration(control_supported=True, stop_supported=True,
                query_supported=False, result_supported=False, read_supported=False,
                digest_supported=False, delete_supported=False,
                adb_foreground_recovery_operations=frozenset({'result'})),
            evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        return await capture_runtime(owned, **inputs)

    owned, runtime, aid, handler = await consumer_world(tmp_path, 'timelapse', start=False,
        catalog=SmallNativeCatalog(startup), runtime_factory=assemble)
    advance = capture_handler(handler)
    due = _NOW + (startup + 1200) * 1000
    await advance(aid, runtime)

    async def cancel():
        instant = datetime.fromtimestamp(wall[0] // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        accepted = await accept_input(ParsedInput('cancel-timelapse.json', {
            'request_id': '2', 'created_at': instant, 'name': '取消延时摄影',
            'actions': [{'name': '取消', 'type': 'cancel_task', 'params': {
                'target': {'action_instance_id': str(aid)}}}]}),
            AcceptanceContext(CommandMode.RUN, SmallNativeCatalog(startup),
                AcceptanceRepository(), clock=SimpleNamespace(utc_micros=lambda: wall[0])),
            new_operation_key(), runtime.database)
        assert accepted.plan_id is not None, accepted
        metadata = runtime.database.metadata
        await cancel_flow(ready=Path(metadata.ready_path), processing=Path(metadata.processing_path),
            start_entries=runtime.start_entries)(SimpleNamespace(database=runtime.database,
                clock=SimpleNamespace(utc_micros=lambda: wall[0])))

    async def actual(request, batch):
        await cancel()
        await advance(aid, runtime)
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual

    class InterruptedPage(CaptureRepository):
        def save_result_page(self, request, key, connection):
            saved = super().save_result_page(request, key, connection)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            return replace(saved, handoff_error=ConsistencyError('原页可靠保存后模拟进程结束'))

    runtime.capture = InterruptedPage()
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        with pytest.raises(ConsistencyError, match='模拟进程结束'): await advance(aid, runtime)
        raw = _pages(owned)
        assert len(raw) == 1 and not _retained(owned, aid)
        from camctl.operations.models import AttemptTicket
        run_id, attempt_no, activity_id, responsibility = owned.connection.execute(
            'SELECT r.id,t.attempt_no,r.activity_id,r.responsibility_key FROM operation_runs r'
            ' JOIN operation_attempts t ON t.run_id=r.id WHERE r.action_id=? AND r.kind=7', (aid,)).fetchone()
        ticket = AttemptTicket(attempt_no, 'result', str(activity_id), responsibility, run_id)
        original_source = CaptureRepository().read_result_source(ticket, owned)
        horizon = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        database = await capture_resources.restart_database(owned)
        wall[0] += 5_000_000
        resumed = await capture_runtime(database, **inputs,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        assert CaptureRepository().read_result_source(ticket, owned) == original_source
        for _ in range(3): await advance(aid, resumed)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (aid,)).fetchone() == (6,)
        assert len(_retained(owned, aid)) == 1 and _pages(owned) == raw
        from camctl.contracts.json_values import parse_exact_json
        proof = parse_exact_json(_retained(owned, aid)[0][2])
        assert proof['basis'] == 2 and 'stop_result_event_id' not in proof
        assert result.list_results.await_count == stop.stop.await_count == 1
        inputs['driver'].control.assert_awaited_once()
        # 可靠末页保留了完整实际返回，恢复仍沿原SUCCEEDED，而非补做设备调用。
        assert owned.connection.execute('SELECT status,error_json FROM operation_attempts WHERE run_id=?', (ticket.run_id,)).fetchone() == (2, None)
        metadata = database.metadata
        await cancel_flow(ready=Path(metadata.ready_path), processing=Path(metadata.processing_path),
            start_entries=resumed.start_entries)(SimpleNamespace(database=database,
                clock=SimpleNamespace(utc_micros=lambda: wall[0])))
        assert owned.connection.execute('SELECT status FROM actions WHERE type=6').fetchone() == (
            3 if stop_outcome == 'confirmed' else 4,)
    finally:
        owned.connection.close()


def _legacy_catalog(startup):
    from .test_natural_wait_time_eligibility import SmallNativeCatalog
    class LegacyCatalog(SmallNativeCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is not None and action_type == 'camera_timelapse':
                factory = definition.task_factory
                return replace(definition, task_factory=lambda params: replace(factory(params), file_completion_wait_s=None))
            return definition
    return LegacyCatalog(startup)


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('version', [1, 2])
async def test_old_direct_file_before_wait_is_retained_without_upgrading_original_source(tmp_path, capture_resources, startup, version):
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.evidence import DeviceObservation
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.natural_sources import (recovered_source_runtime,
        save_historical_source_page, save_historical_direct_files)
    from ..support.result_cases import begin_result_attempt
    from .result_consumer_fixtures import RESULT_EVIDENCE
    state = {}
    world, stop = await _source_world(tmp_path, startup, 'failed',
        catalog=_legacy_catalog(startup), state=state)
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    evidence = RESULT_EVIDENCE if version == 1 else state['options']['evidence']
    async def actual(request, batch):
        assert not request.params.get('completion_context')
        actual = _actual(request.ticket, entries=[_osv()], finalized=True)
        if version == 1:
            actual = replace(actual, observations=(DeviceObservation('result_files_listed', 1,
                {'activity_id': request.ticket.target_id, 'entries': actual.observations[0].data['entries']}),))
        return DeviceCallResult.from_outcome(actual)
    result.list_results.side_effect = actual
    try:
        wall[0] += 100
        ticket = begin_result_attempt(owned, aid, AttemptConfig(1, Decimal('1'), Decimal(0)), wall[0])
        saved = await save_historical_source_page(owned, aid, ticket,
            DriverResultListing(result, DeviceBinding('cam-1', 'camctl-adb'), evidence), evidence, wall[0])
        save_historical_direct_files(owned, aid, saved)
        file_before = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert file_before is not None
        runtime = await recovered_source_runtime(owned, capture_resources, state, evidence=evidence)
        await _request_cancel(runtime, aid, wall)
        await advance(aid, runtime)
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        for _ in range(3): await advance(aid, runtime)
        original = CaptureRepository().read_result_source(ticket, owned)
        assert original.kind.value == 'direct' and original.context is None
        assert original.natural_lower_bound is None
        assert (await runtime.action(aid))['status'] == 6
        assert owned.connection.execute('SELECT wait_completed_event_id FROM device_activities WHERE action_id=?', (aid,)).fetchone()[0] is not None
        assert len(_retained(owned, aid)) == 1
        assert _retained(owned, aid)[0][1:] == file_before
        assert result.list_results.await_count == stop.stop.await_count == 1
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        await _finish_cancel(owned, runtime, wall, False)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('stop_outcome', ['confirmed', 'failed', 'unknown'])
async def test_old_natural_source_keeps_original_direct_guarantee_after_stop(tmp_path, capture_resources, startup, version, stop_outcome):
    from camctl.devices.evidence import DeviceObservation
    from .result_consumer_fixtures import RESULT_EVIDENCE
    state = {}
    world, stop = await _source_world(tmp_path, startup, stop_outcome,
        catalog=_legacy_catalog(startup), state=state)
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    if version == 1:
        runtime = await legacy_result_runtime(owned, capture_resources, state, result)
    async def actual(request, batch):
        assert not request.params.get('completion_context')
        await _request_cancel(runtime, aid, wall)
        await advance(aid, runtime)
        actual = _actual(request.ticket, entries=[_osv()], finalized=True)
        if version == 1:
            actual = replace(actual, observations=(DeviceObservation('result_files_listed', 1,
                {'activity_id': request.ticket.target_id, 'entries': actual.observations[0].data['entries']}),))
        return DeviceCallResult.from_outcome(actual)
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        for _ in range(3): await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 6
        assert len(_retained(owned, aid)) == 1
        from camctl.contracts.json_values import parse_exact_json
        proof = parse_exact_json(_retained(owned, aid)[0][2])
        assert proof['basis'] == 1 and 'stop_result_event_id' not in proof
        assert result.list_results.await_count == stop.stop.await_count == 1
        await _finish_cancel(owned, runtime, wall, stop_outcome == 'confirmed')
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('stop_outcome', ['confirmed', 'failed', 'unknown'])
async def test_intent_before_wait_cannot_gain_natural_source_from_later_wait_or_stop(tmp_path, capture_resources, startup, stop_outcome):
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.natural_sources import recovered_source_runtime, save_historical_source_page
    from ..support.result_cases import begin_result_attempt
    from .result_consumer_fixtures import complete_natural_wait
    state = {}
    world, stop = await _source_world(tmp_path, startup, stop_outcome, state=state)
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    async def actual(request, batch):
        assert not request.params.get('completion_context')
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    try:
        ticket = begin_result_attempt(owned, aid, AttemptConfig(1, Decimal('1'), Decimal(0)), wall[0])
        assert CaptureRepository().read_result_source(ticket, owned).kind.value == 'unconfirmed'
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        assert await complete_natural_wait(owned, aid, wall[0]) is not None
        await _request_cancel(runtime, aid, wall)
        await advance(aid, runtime)
        await save_historical_source_page(owned, aid, ticket,
            state['options']['results'], state['options']['evidence'], wall[0])
        runtime = await recovered_source_runtime(owned, capture_resources, state)
        for _ in range(3): await advance(aid, runtime)
        assert CaptureRepository().read_result_source(ticket, owned).kind.value == 'unconfirmed'
        assert (await runtime.action(aid))['status'] == 6 and not _retained(owned, aid)
        assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (0,)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == ((3, 2) if stop_outcome == 'confirmed' else (1, 1))
        assert result.list_results.await_count == stop.stop.await_count == 1
        await _finish_cancel(owned, runtime, wall, False)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('first_good', [False, True])
async def test_later_independent_stop_round_does_not_reclassify_prior_natural_page(tmp_path, startup, first_good):
    world, stop = await _source_world(tmp_path, startup, 'confirmed',
        check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    contexts = []
    async def actual(request, batch):
        context = request.params['completion_context']
        contexts.append(context)
        if len(contexts) == 1:
            assert context['kind'] == 'timelapse_natural'
            await _request_cancel(runtime, aid, wall)
            if not first_good: wall[0] = due-1
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()],
                error=ErrorValue('device_error', 'device')))
        assert context['kind'] == 'timelapse_stop' and context['prior_completion'] is None
        completion = {'method': 'stop_return_and_wait', 'version': 1,
            'activity_id': context['activity_id'], 'stop_result_event_id': context['stop_result_event_id'],
            'required_wait_ms': 30000, 'observed_wait_ns': 30000000000, 'completed': True}
        return _completed_result(request.ticket, context, entries=[_osv()], finalized=True, completion=completion)
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        prior = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 6 and len(_retained(owned, aid)) == 1
        assert _pages(owned)[:1] == prior[:1]
        from camctl.contracts.json_values import parse_exact_json
        proof = parse_exact_json(_retained(owned, aid)[0][2])
        assert proof['basis'] == (2 if first_good else 3)
        assert proof.get('wait_completed_event_id') == (contexts[0]['wait_completed_event_id'] if first_good else None)
        assert result.list_results.await_count == 2 and stop.stop.await_count == 1
        await _finish_cancel(owned, runtime, wall, True)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('latest', ['empty', 'missing_identity', 'incomplete', 'size_unknown', 'size_mismatch'])
@pytest.mark.parametrize('canceled', [False, True])
async def test_stop_source_retains_known_files_but_does_not_fill_current_collection(tmp_path, startup, latest, canceled):
    from camctl.devices.tasks import CompletionMode, EndControl
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.result_cases import current_result_ticket
    from .test_natural_wait_time_eligibility import SmallNativeCatalog
    class StopCatalog(SmallNativeCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is not None and action_type == 'camera_timelapse' and not canceled:
                factory = definition.task_factory
                return replace(definition, task_factory=lambda params: replace(factory(params),
                    end_control=EndControl.HOST_TIMER, wait_after_send=False,
                    result_wait_margin_s=None, cancel_completion_policy=None,
                    completion_mode=CompletionMode.DEVICE_EVIDENCE))
            return definition
    world, stop = await _source_world(tmp_path, startup, 'confirmed',
        catalog=StopCatalog(startup), check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    calls = []
    later = {'empty': [], 'missing_identity': [{**_other(), 'complete': False, 'size_bytes': None}],
        'incomplete': [{**_osv(), 'complete': False, 'size_bytes': None}],
        'size_unknown': [{**_osv(), 'size_bytes': None}],
        'size_mismatch': [{**_osv(), 'size_bytes': 51}]}[latest]
    async def actual(request, batch):
        context = request.params['completion_context']
        assert context['kind'] == 'timelapse_stop'
        calls.append(request.ticket)
        completion = {'method': 'stop_return_and_wait', 'version': 1,
            'activity_id': context['activity_id'], 'stop_result_event_id': context['stop_result_event_id'],
            'required_wait_ms': 30000, 'observed_wait_ns': 30000000000, 'completed': True}
        return _completed_result(request.ticket, context,
            entries=[_osv()] if len(calls) == 1 else later,
            finalized=len(calls) == 2, error=ErrorValue('device_error', 'device') if len(calls) == 1 else None,
            completion=completion)
    result.list_results.side_effect = actual
    try:
        if canceled: await _request_cancel(runtime, aid, wall)
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        original = owned.connection.execute('SELECT id,size_bytes,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert original is not None
        await advance(aid, runtime)
        ticket = current_result_ticket(owned, aid)
        pages = CaptureRepository().read_result_pages(ticket, None, 32, owned)
        assert [entry.identity for page in pages.items for entry in page.page.entries] == [entry['identity'] for entry in later]
        assert owned.connection.execute('SELECT id,size_bytes,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone() == original
        for _ in range(4): await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        assert len(_retained(owned, aid)) == 1
        assert result.list_results.await_count == 2 and stop.stop.await_count == 1
        if canceled: await _finish_cancel(owned, runtime, wall, True)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('page_time', ['good', 'before_wait', 'after_end'])
async def test_independent_finite_end_guard_checks_every_original_page_time(tmp_path, startup, page_time):
    from camctl.contracts.history_values import HistoryBoundary, TransactionRange
    from camctl.history.decoding import decode_event_row
    from camctl.history.events import load_event_registry
    from camctl.history.reads import CaptureResultReadScope
    from camctl.history.validators import EventContext, EventValidationError, validate_event
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.history import HistoryRepository
    from ..support.result_cases import current_result_ticket
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup, True)
    async def actual(request, batch):
        cursor = request.params.get('cursor')
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_osv()] if cursor is None else [],
            cursor=None if cursor is None else _CURSOR,
            next_cursor=_CURSOR if cursor is None else None, finalized=cursor is not None))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        ticket = current_result_ticket(owned, aid)
        page = CaptureRepository().read_last_result_page(ticket, owned)
        contract = load_event_registry()['events']['DEVICE_OBSERVED']
        cursor = owned.connection.execute('SELECT * FROM history_events WHERE event_type=?'
            " AND json_extract(body_json,'$.reason')=?", (contract['id'], contract['branches']['FINITE_CANCEL_END']['reason']))
        columns = tuple(column[0] for column in cursor.description)
        raw, = cursor.fetchall()
        event = decode_event_row(raw)
        previous = owned.connection.execute('SELECT id,last_event_id FROM history_transactions'
            ' WHERE id<? ORDER BY id DESC LIMIT 1', (event.transaction_id,)).fetchone()
        history = HistoryRepository(Path(owned.connection.execute('PRAGMA database_list').fetchone()[2]))
        restored = history.restore_entity('action', aid, HistoryBoundary(*previous))
        state = {}
        for (table, identity), row in restored.items():
            state.setdefault(table, {})[identity] = row
        source = owned.connection.execute('SELECT * FROM history_events WHERE id<=? ORDER BY id',
            (previous[1],))
        state['history_events'] = {row[0]: dict(zip(columns, row)) for row in source}
        scope = CaptureResultReadScope(aid, int(ticket.target_id), ticket.run_id,
            ticket.attempt_id, page.ref.event_id)
        owned.connection.execute('BEGIN')
        try:
            reader = SqlEventReads(owned.connection, scope)
            class OriginalPages:
                scope = reader.scope
                required = reader.required
                def result_pages(self, ticket, cursor, batch):
                    actual = reader.result_pages(ticket, cursor, batch)
                    return replace(actual, items=tuple(replace(saved,
                        occurred_at=due-1 if page_time == 'before_wait' else due+200)
                        if page_time != 'good' and saved.ref.page_no == 1 else saved
                        for saved in actual.items))
            context = EventContext(TransactionRange(event.transaction_id, event.event_id, event.event_id),
                {('device_activities', int(ticket.target_id)): ('action', aid)}, state,
                current_reads=OriginalPages())
            if page_time != 'good':
                with pytest.raises(EventValidationError): validate_event(event, context)
            else:
                validate_event(event, context)
        finally:
            owned.connection.execute('ROLLBACK')
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('completed', [False, True])
async def test_declared_stop_wait_on_original_natural_page_is_not_silently_skipped(tmp_path, startup, completed):
    world, stop = await _source_world(tmp_path, startup, 'confirmed',
        check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    owned, runtime, aid, advance, wall, mono, result, unused, due = world
    async def actual(request, batch):
        assert request.params['completion_context']['kind'] == 'timelapse_natural'
        await _request_cancel(runtime, aid, wall)
        await advance(aid, runtime)
        event, = owned.connection.execute("SELECT t.result_event_id FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key=?", (f'stop/{aid}',)).fetchone()
        completion = {'method': 'stop_return_and_wait', 'version': 1,
            'activity_id': request.ticket.target_id, 'stop_result_event_id': event,
            'required_wait_ms': 30000, 'observed_wait_ns': 30000000000 if completed else 0,
            'completed': completed}
        return _completed_result(request.ticket, None, completion=completion,
            error=ErrorValue('device_error', 'device'))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        raw = _pages(owned)
        with pytest.raises(ConsistencyError): await advance(aid, runtime)
        assert _pages(owned) == raw and not _retained(owned, aid)
        assert result.list_results.await_count == stop.stop.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_old_v2_retained_metadata_comes_from_original_complete_source(tmp_path, startup, canceled):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, catalog=_legacy_catalog(startup),
        check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()],
                error=ErrorValue('device_error', 'device')))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[{**_osv(), 'original_name': 'bad-new-name.OSV'}], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        original = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        await advance(aid, runtime)
        raw = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        assert len(_retained(owned, aid)) == 1 and _retained(owned, aid)[0][1:] == original
        saved = runtime.capture.read_result_sources(calls[0], None, 1, owned)
        assert saved.items[0][0].original_name is None
        assert _pages(owned) == raw and result.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled, save_mode', [(False, 'reliable'), (True, 'reliable'), (True, 'before_commit'), (True, 'after_commit')])
async def test_legal_unanchored_start_consumes_finite_query_without_completing_activity(tmp_path, capture_resources, monkeypatch, startup, canceled, save_mode):
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.persistence.models import DbOutcome
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.scheduling import SchedulingRepository
    from ..support.natural_sources import unanchored_source_world
    stop = _stop_driver('unknown')
    owned, runtime, aid, advance, wall, result, state = await unanchored_source_world(
        tmp_path, capture_resources, startup, stop)
    class StartIntentSaved(Exception):
        pass
    begin = SchedulingRepository.grant_start
    def interrupted(repository, request, key, current):
        receipt = begin(repository, request, key, current)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=StartIntentSaved())
    async def actual(request, batch):
        assert not request.params.get('completion_context')
        return DeviceCallResult.from_outcome(_actual(request.ticket, finalized=True))
    result.list_results.side_effect = actual
    save = CaptureRepository.save_result_page
    requests = []
    def uncertain(repository, command, key, connection):
        requests.append((command, key))
        if len(requests) == 1 and save_mode != 'reliable':
            if save_mode == 'after_commit':
                assert save(repository, command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(repository, command, key, connection)
    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(SchedulingRepository, 'grant_start', interrupted)
            with pytest.raises(StartIntentSaved): await advance(aid, runtime)
        horizon = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        runtime = await cold_source_runtime(owned, capture_resources, state,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        if canceled: await _request_cancel(runtime, aid, wall)
        monkeypatch.setattr(CaptureRepository, 'save_result_page', uncertain)
        if canceled and save_mode != 'reliable':
            with pytest.raises(ConsistencyError): await advance(aid, runtime)
            await capture_resources.restart_database(owned)
            runtime = await state['factory'](owned)
        await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        assert owned.connection.execute('SELECT sent_at,wait_completed_event_id,activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (None, None, 1, 1)
        assert state['options']['driver'].control.await_count == 0
        assert stop.stop.await_count == result.list_results.await_count == int(canceled)
        assert not _retained(owned, aid)
        assert all(request == requests[0] for request in requests)
        if canceled: await _finish_cancel(owned, runtime, wall, False)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('opaque', [False, True])
async def test_v1_complete_source_remains_original_after_v2_listing(tmp_path, capture_resources, startup, canceled, opaque):
    from camctl.capture.handlers import ResultFilesPort
    from camctl.capture.result_inputs import ListedResult, observed_file
    from camctl.devices.evidence import DeviceObservation
    from .result_consumer_fixtures import RESULT_EVIDENCE
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.ports import ResultDriver
    state = {}
    owned, runtime, aid, advance, wall, mono, unused, stopper, due = await _world(
        tmp_path, startup, canceled, catalog=_legacy_catalog(startup), state=state,
        check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    old = create_autospec(ResultFilesPort, instance=True)
    async def actual_v1(ticket, *, timeout_s):
        outcome = _actual(ticket, entries=[_osv()], error=ErrorValue('device_error', 'device'))
        outcome = replace(outcome, observations=(DeviceObservation('result_files_listed', 1,
            {'activity_id': ticket.target_id, 'entries': outcome.observations[0].data['entries']}),))
        return ListedResult((replace(observed_file(_osv()), evidence={'listing': 'clip-1'} if opaque else dict(_osv())),), outcome)
    old.list_round.side_effect = actual_v1
    runtime = await cold_source_runtime(owned, capture_resources, state,
        evidence=RESULT_EVIDENCE, results=old)
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        original = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert original is not None
        async def actual_v2(request, batch):
            wall[0] = due-1
            return DeviceCallResult.from_outcome(_actual(request.ticket,
                entries=[{**_osv(), 'original_name': 'bad-new-name.OSV'} if opaque else _osv()], finalized=True))
        new = create_autospec(ResultDriver, instance=True)
        new.list_results.side_effect = actual_v2
        runtime = await cold_source_runtime(owned, capture_resources, state,
            results=DriverResultListing(new, DeviceBinding('cam-1', 'camctl-adb'), state['options']['evidence']))
        await advance(aid, runtime)
        raw = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        assert len(_retained(owned, aid)) == 1 and _retained(owned, aid)[0][1:] == original
        saved = runtime.capture.read_result_sources(new.list_results.call_args.args[0].ticket, None, 1, owned)
        assert saved.items[0][0].original_name is None
        assert _pages(owned) == raw
        assert old.list_round.await_count == new.list_results.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('change', ['missing_identity', 'different_metadata'])
async def test_old_v2_missing_original_complete_entry_is_diagnosed_without_latest_fallback(tmp_path, startup, change, monkeypatch):
    import json
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(
        tmp_path, startup, catalog=_legacy_catalog(startup),
        check_config=AttemptConfig(1, Decimal('1'), Decimal(0)))
    async def actual(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        ticket = result.list_results.call_args.args[0].ticket
        file_id, raw_evidence = owned.connection.execute(
            'SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        evidence = json.loads(raw_evidence)
        if change == 'missing_identity': evidence['observation'].pop('identity')
        else: evidence['observation']['original_name'] = 'unknown-origin.OSV'
        owned.connection.execute('UPDATE device_files SET completion_evidence_json=? WHERE id=?',
            (json.dumps(evidence), file_id))
        owned.connection.commit()
        before = tuple(owned.connection.iterdump())
        with pytest.raises(ConsistencyError): runtime.capture.read_result_sources(ticket, None, 1, owned)
        assert tuple(owned.connection.iterdump()) == before
        assert result.list_results.await_count == 1 and len(_retained(owned, aid)) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_closed_v1_natural_round_after_later_stop_keeps_only_current_raw_entries(tmp_path, capture_resources, startup):
    from camctl.devices.evidence import DeviceObservation
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.result_cases import current_result_ticket
    state = {}
    world, stop = await _source_world(tmp_path, startup, 'confirmed',
        catalog=_legacy_catalog(startup), state=state,
        check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    owned, runtime, aid, advance, wall, mono, driver, unused, due = world
    runtime = await legacy_result_runtime(owned, capture_resources, state, driver)
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 2:
            await _request_cancel(runtime, aid, wall)
            await advance(aid, runtime)
        value = _actual(request.ticket, entries=[_osv()] if len(calls) == 1 else [],
            error=ErrorValue('device_error', 'device') if len(calls) == 1 else None)
        value = replace(value, observations=(DeviceObservation('result_files_listed', 1,
            {'activity_id': request.ticket.target_id, 'entries': value.observations[0].data['entries']}),))
        return DeviceCallResult.from_outcome(value)
    driver.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        await advance(aid, runtime)
        ticket = current_result_ticket(owned, aid)
        assert CaptureRepository().read_result_source(ticket, owned).kind.value == 'natural'
        assert owned.connection.execute(
            "SELECT json_extract(result_json,'$.observations[0].data.entries')"
            " FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == ('[]',)
        retained = CaptureRepository().read_result_sources(ticket, None, 1, owned)
        assert len(retained.items) == 1 and retained.items[0][1].complete
        assert (await runtime.action(aid))['status'] == 6 and len(_retained(owned, aid)) == 1
        assert driver.list_results.await_count == 2 and stop.stop.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('consumer', ['photo', 'record'])
@pytest.mark.parametrize('binding_failure', [False, True])
async def test_shared_current_filter_preserves_prior_complete_files_at_failure_boundary(tmp_path, capture_resources, monkeypatch, consumer, binding_failure):
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.natural_sources import result_consumer_world
    from ..support.result_cases import current_result_ticket
    from .test_result_page_history import _entry
    case, owned, runtime, aid, handler, driver = await result_consumer_world(
        tmp_path, consumer, capture_resources)
    calls = []
    entry = {**_entry('a'), 'kind': 'photo' if consumer == 'photo' else 'video'}
    async def actual(request, batch):
        calls.append(request.ticket)
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[entry] if len(calls) == 1 else [],
            finalized=len(calls) == 2,
            error=ErrorValue('device_error', 'device') if len(calls) == 1 or binding_failure else None))
    driver.list_results.side_effect = actual
    class SecondSaved(Exception):
        pass
    from camctl.persistence.repositories.operations import OperationRepository
    save = OperationRepository.finish_attempt
    def boundary(repository, finish, key, current):
        receipt = save(repository, finish, key, current)
        if binding_failure and len(calls) == 2 and finish.ticket == calls[-1]:
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=SecondSaved())
        return receipt
    monkeypatch.setattr(OperationRepository, 'finish_attempt', boundary)
    advance = capture_handler(handler)
    try:
        await advance(aid, runtime)
        original = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert original is not None
        if binding_failure:
            with pytest.raises(SecondSaved): await advance(aid, runtime)
            runtime = await case.reopen(owned, cold=True, assembly_options={'devices': {}})
        else:
            await advance(aid, runtime)
        ticket = current_result_ticket(owned, aid)
        pages = CaptureRepository().read_result_pages(ticket, None, 32, owned)
        assert pages.items and all(not page.page.entries for page in pages.items)
        await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 4
        assert len(_retained(owned, aid)) == 1 and _retained(owned, aid)[0][1:] == original
        assert driver.list_results.await_count == 2
    finally:
        await case.close()
        owned.connection.close()


async def test_recording_media_cache_keeps_original_current_set_instead_of_prior_complete_files(tmp_path, capture_resources):
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.natural_sources import result_consumer_world
    from ..support.result_cases import current_result_ticket
    from .result_consumer_fixtures import ResultCatalog
    from .test_result_page_history import _entry
    class OneRecordCatalog(ResultCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is not None and action_type == 'camera_record':
                factory = definition.task_factory
                return replace(definition, task_factory=lambda params: replace(factory(params),
                    product_rules=({'kind': 'video', 'format_id': None, 'min_count': 1,
                        'exact_count': 1, 'require_pairing': False},)))
            return definition
    case, owned, runtime, aid, handler, driver = await result_consumer_world(
        tmp_path, 'record', capture_resources, catalog=OneRecordCatalog())
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_entry('a' if len(calls) == 1 else 'b')], finalized=len(calls) == 2,
            error=ErrorValue('device_error', 'device') if len(calls) == 1 else None))
    driver.list_results.side_effect = actual
    advance = capture_handler(handler)
    try:
        await advance(aid, runtime)
        await advance(aid, runtime)
        ticket = current_result_ticket(owned, aid)
        pages = CaptureRepository().read_result_pages(ticket, None, 32, owned)
        assert [entry.identity for page in pages.items for entry in page.page.entries] == ['/DCIM/b.mp4']
        assert (await runtime.action(aid))['status'] == 3
        assert owned.connection.execute("SELECT json_extract(f.identity_key,'$[2]') FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=? ORDER BY f.id", (aid,)).fetchall() == [('/DCIM/a.mp4',), ('/DCIM/b.mp4',)]
        assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (2,)
        assert driver.list_results.await_count == 2
    finally:
        await case.close()
        owned.connection.close()
