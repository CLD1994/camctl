"""自然等待分别核对逐页文件、当前集合和跨轮终态保留。"""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from camctl.capture.models import WaitCompletedSave
from camctl.capture.timelapse import WaitKind, WaitPlan
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.timelapse import ScheduleWait, TimelapseRepository

from ..support.capture import capture_factory
from .result_consumer_fixtures import consumer_world, returned, _prepare_through_handler
from .test_capture_contract import _NOW
from .test_native_completion_context import _osv
from .test_natural_wait_time_eligibility import SmallNativeCatalog, _world, _progress_cancel
from .test_result_page_history import _actual, _CURSOR

pytestmark = pytest.mark.asyncio


def _other():
    return {**_osv(), 'identity': '/DCIM/b.OSV', 'locator': {'path': '/DCIM/b.OSV'}}


def _pages(owned):
    from camctl.history.events import load_event_registry

    contract = load_event_registry()["events"]["RESULT_PAGE"]
    return owned.connection.execute(
        "SELECT id,occurred_at,body_json FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=? ORDER BY id",
        (contract["id"], contract["branches"]["APPEND"]["reason"]),
    ).fetchall()


def _retained(owned, action_id):
    return owned.connection.execute('SELECT o.id,o.device_file_id,f.completion_evidence_json FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=? ORDER BY o.id', (action_id,)).fetchall()


async def _finish_cancel(owned, runtime, wall):
    await _progress_cancel(runtime, wall)
    assert owned.connection.execute('SELECT status FROM actions WHERE type=6').fetchone() == (4,)


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('mixed', [False, True])
@pytest.mark.parametrize('budget', [1, 2])
async def test_qualified_files_survive_bad_latest_page_without_completing_collection(tmp_path, startup, canceled, mixed, budget):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled,
        check_config=AttemptConfig(budget if mixed else budget+1, Decimal('1'), Decimal(0)))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()],
                next_cursor=_CURSOR if mixed else None,
                error=None if mixed else ErrorValue('device_error', 'device')))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_other()],
            cursor=_CURSOR if mixed and len(calls) == 2 else None, finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        wall[0] = due+100
        await advance(aid, runtime)
        original_pages = _pages(owned)
        assert len(original_pages) == 2
        assert owned.connection.execute("SELECT completion_state FROM device_files WHERE json_extract(identity_key,'$[2]')='/DCIM/a.OSV'").fetchone() == (3,)
        original_file = owned.connection.execute("SELECT id,completion_evidence_json FROM device_files WHERE json_extract(identity_key,'$[2]')='/DCIM/a.OSV'").fetchone()
        assert owned.connection.execute("SELECT completion_state FROM device_files WHERE json_extract(identity_key,'$[2]')='/DCIM/b.OSV'").fetchone() == (1,)
        assert (await runtime.action(aid))['status'] == 2
        # 校时恢复只允许沿剩余预算的新尝试，不能重新解释坏页。
        for _ in range(budget+2):
            wall[0] = due+100
            await advance(aid, runtime)
        retained = _retained(owned, aid)
        assert len(retained) == 1 and retained[0][1:] == original_file
        assert _pages(owned)[:2] == original_pages
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, runtime, wall)
        assert owned.connection.execute('SELECT activity_state,occupancy_state,result_set_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1, 4)
        assert len(calls) == (budget+1)
        assert stopper.stop.await_count == int(canceled)
        before = retained
        await advance(aid, runtime)
        assert _retained(owned, aid) == before
        history = HistoryRepository(Path(owned.connection.execute('PRAGMA database_list').fetchone()[2]))
        restored = history.restore_entity('action', aid, history.current_boundary())
        assert restored[('actions', aid)]['status'] == (6 if canceled else 4)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('latest', ['empty', 'missing'])
async def test_known_complete_file_cannot_fill_current_empty_or_missing_collection(tmp_path, startup, canceled, latest):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_osv()] if len(calls) == 1 else ([] if latest == 'empty' else [{**_other(), 'complete': False, 'size_bytes': None}]),
            error=ErrorValue('device_error', 'device') if len(calls) == 1 else None,
            finalized=len(calls) > 1))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        await advance(aid, runtime)
        if latest == 'missing':
            assert (await runtime.action(aid))['status'] == 2
            await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, runtime, wall)
        assert len(_retained(owned, aid)) == 1
        activity = owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone()
        assert activity == (1, 1)
        assert len(calls) == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('observation', ['timelapse_sent', 'photo_taken', 'start_confirmed', None])
async def test_wait_repository_uses_same_sent_semantics_as_completion_context(tmp_path, startup, observation):
    owned, runtime, aid, handler = await consumer_world(tmp_path, 'timelapse', catalog=SmallNativeCatalog(startup), start=False)
    activity_id = owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?', (aid,)).fetchone()[0]
    actual = returned('operation', observation or 'timelapse_sent', activity_id)
    if observation is None:
        actual = replace(actual, observations=())
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(actual)
    try:
        await _prepare_through_handler(runtime, aid, handler, 'start')
        due = _NOW+(startup+1200)*1000
        commands = [
            (runtime.timelapse.schedule_wait, ScheduleWait(aid, WaitPlan(WaitKind.WAIT_THEN_CHECK, due), 200, 0, _NOW)),
            (runtime.timelapse.reconfigure_wait, ScheduleWait(aid, WaitPlan(WaitKind.WAIT_THEN_CHECK, due+1000), 200, 1, _NOW)),
            (runtime.timelapse.complete_wait, WaitCompletedSave(aid, due+1000))]
        for save, command in commands:
            before = tuple(owned.connection.iterdump())
            key = new_operation_key()
            receipt = save(command, key, owned)
            assert receipt.kind is (DbOutcomeKind.COMPLETED if observation == 'timelapse_sent' else DbOutcomeKind.ROLLED_BACK)
            if observation != 'timelapse_sent':
                assert tuple(owned.connection.iterdump()) == before
            else:
                assert save(command, key, owned).kind is DbOutcomeKind.COMPLETED
        if observation == 'timelapse_sent':
            sent_at, expected, completed = owned.connection.execute(
                'SELECT sent_at,expected_check_at,wait_completed_event_id FROM device_activities WHERE action_id=?',
                (aid,)).fetchone()
            assert (sent_at, expected) == (_NOW, due+1000) and completed is not None
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('original_qualified', [False, True])
async def test_optional_preview_never_uses_bad_original_page_for_pairing(tmp_path, startup, canceled, original_qualified):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, check_config=AttemptConfig(1, Decimal('1'), Decimal(0)))
    calls = []
    preview = {**_osv(), 'identity': '/DCIM/a.LRF', 'locator': {'path': '/DCIM/a.LRF'},
        'format_id': 'lrf', 'paired_identity': '/DCIM/a.OSV'}
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv() if original_qualified else preview], next_cursor=_CURSOR))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[preview if original_qualified else _osv()], cursor=_CURSOR, finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        wall[0] = due+100
        await advance(aid, runtime)
        await advance(aid, runtime)
        retained = _retained(owned, aid)
        assert len(retained) == int(original_qualified)
        assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (int(original_qualified),)
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, runtime, wall)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        assert len(calls) == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('committed', [False, True])
async def test_qualified_file_unknown_keeps_original_source_and_key_across_runtime_restart(tmp_path, monkeypatch, capture_resources, startup, canceled, committed):
    from camctl.persistence.models import DbOutcome
    state = {}
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, state=state,
        check_config=AttemptConfig(1, Decimal('1'), Decimal(0)))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], next_cursor=_CURSOR))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_other()], cursor=_CURSOR, finalized=True))
    result.list_results.side_effect = actual
    from camctl.persistence.repositories.capture import CaptureRepository
    save = CaptureRepository.save_file_completion
    requests = []
    def uncertain(repository, command, key, connection):
        requests.append((command, key))
        if len(requests) <= 2:
            if committed and len(requests) == 1:
                assert save(repository, command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(repository, command, key, connection)
    monkeypatch.setattr(CaptureRepository, 'save_file_completion', uncertain)
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        wall[0] = due+100
        with pytest.raises(ConsistencyError): await advance(aid, runtime)
        assert len(calls) == 2 and len(requests) == 2
        pages = _pages(owned)
        # 新运行时接管未决原文件申请；时间映射为新会话，申请仍保留原键。
        await capture_resources.restart_database(owned)
        wall[0] = due-1
        runtime = await state['factory'](owned)
        await advance(aid, runtime)
        assert requests[0] == requests[1] == requests[2]
        assert len(calls) == 2 and _pages(owned) == pages
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert len(_retained(owned, aid)) == 1
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, runtime, wall)
        assert len(calls) == 2 and stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_bad_nonfinal_page_stops_remaining_pages_but_retains_good_previous_page(tmp_path, startup, canceled):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, check_config=AttemptConfig(1, Decimal('1'), Decimal(0)))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        assert len(calls) <= 2, '不合格页之后不得继续物理目录分页'
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], next_cursor=_CURSOR))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_other()], cursor=_CURSOR,
            next_cursor=replace(_CURSOR, after_path='/DCIM/b.mp4')))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        pages = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert len(_retained(owned, aid)) == 1
        assert _pages(owned) == pages and len(calls) == 2
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, runtime, wall)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
    finally:
        owned.connection.close()


def _legacy_start_observation(owned, action_id, observation):
    """按历史存储格式准备旧等待的原返回，供当前仓储消费者核对。

    旧归档保留已提交的等待及原键，但其 START 返回仅含另种观察。
    投影与原结果事件共同准备同一完整输入，随后只验证历史消费。
    """
    attempt_id, event_id, raw, activity_id = owned.connection.execute(
        'SELECT t.id,t.result_event_id,t.result_json,r.activity_id FROM operation_runs r'
        ' JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key=?',
        (f'start/{action_id}',)).fetchone()
    result = json.loads(raw)
    assert result['observations'] == [{'type': 'timelapse_sent', 'version': 1,
        'data': {'activity_id': str(activity_id)}}]
    result['observations'] = [{'type': observation, 'version': 1,
        'data': {'activity_id': str(activity_id)}}]
    body = json.loads(owned.connection.execute(
        'SELECT body_json FROM history_events WHERE id=?', (event_id,)).fetchone()[0])
    row, = [row for row in body['rows']
            if row['table'] == 'operation_attempts' and row['id'] == attempt_id]
    assert row['after']['values']['result_json'] == json.loads(raw)
    row['after']['values']['result_json'] = result
    owned.connection.execute('UPDATE operation_attempts SET result_json=? WHERE id=?',
        (json.dumps(result, ensure_ascii=False), attempt_id))
    owned.connection.execute('UPDATE history_events SET body_json=? WHERE id=?',
        (json.dumps(body, ensure_ascii=False), event_id))
    owned.connection.commit()


async def _cold_runtime(owned, state, resources, *, options=None):
    await resources.restart_database(owned)
    state['factory'] = await capture_factory(
        owned, **(state['options'] if options is None else options))
    return await state['factory'](owned)


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('branch', ['schedule', 'reconfigure', 'complete'])
@pytest.mark.parametrize('observation', ['photo_taken', 'start_confirmed'])
async def test_legacy_saved_wait_key_does_not_bypass_reliable_sent_validation(tmp_path, startup, branch, observation):
    owned, runtime, aid, handler = await consumer_world(tmp_path, 'timelapse', catalog=SmallNativeCatalog(startup), start=False)
    actual = returned('operation', 'timelapse_sent', aid)
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(actual)
    try:
        await _prepare_through_handler(runtime, aid, handler, 'start')
        due = _NOW+(startup+1200)*1000
        initial = ScheduleWait(aid, WaitPlan(WaitKind.WAIT_THEN_CHECK, due), 200, 0, _NOW)
        initial_key = new_operation_key()
        assert runtime.timelapse.schedule_wait(initial, initial_key, owned).kind is DbOutcomeKind.COMPLETED
        if branch == 'schedule': save, command, key = runtime.timelapse.schedule_wait, initial, initial_key
        elif branch == 'reconfigure':
            save = runtime.timelapse.reconfigure_wait
            command, key = ScheduleWait(aid, WaitPlan(WaitKind.WAIT_THEN_CHECK, due+1000), 200, 1, _NOW), new_operation_key()
            assert save(command, key, owned).kind is DbOutcomeKind.COMPLETED
        else:
            save = runtime.timelapse.complete_wait
            command, key = WaitCompletedSave(aid, due), new_operation_key()
            assert save(command, key, owned).kind is DbOutcomeKind.COMPLETED
        _legacy_start_observation(owned, aid, observation)
        before = tuple(owned.connection.iterdump())
        assert save(command, key, owned).kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
        verify = runtime.timelapse.verify_wait(command, key, owned, reconfigure=branch == 'reconfigure')
        assert verify.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_legacy_v1_closed_bad_result_retains_original_complete_file(tmp_path, capture_resources, startup, canceled):
    from camctl.devices.evidence import DeviceObservation
    from .result_consumer_fixtures import RESULT_EVIDENCE
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.devices.bindings import DeviceBinding
    class LegacyCatalog(SmallNativeCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is not None and action_type == 'camera_timelapse':
                factory = definition.task_factory
                return replace(definition, task_factory=lambda params: replace(factory(params), file_completion_wait_s=None))
            return definition
    state = {}
    owned, runtime, aid, advance, wall, mono, driver, stopper, due = await _world(
        tmp_path, startup, canceled, catalog=LegacyCatalog(startup), state=state,
        check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    legacy_options = {**state['options'], 'evidence': RESULT_EVIDENCE,
        'results': DriverResultListing(driver, DeviceBinding('cam-1', 'camctl-adb'), RESULT_EVIDENCE)}
    runtime = await _cold_runtime(owned, state, capture_resources, options=legacy_options)
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 2: wall[0] = due-1
        original = _actual(request.ticket, entries=[_osv() if len(calls) == 1 else _other()],
            error=ErrorValue('device_error', 'device') if len(calls) == 1 else None)
        observation = DeviceObservation('result_files_listed', 1,
            {'activity_id': request.ticket.target_id, 'entries': original.observations[0].data['entries']})
        return DeviceCallResult.from_outcome(replace(original, observations=(observation,)))
    driver.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        original_file = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert original_file is not None
        await advance(aid, runtime)
        raw = owned.connection.execute('SELECT result_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no', (calls[0].run_id,)).fetchall()
        runtime = await _cold_runtime(owned, state, capture_resources, options=legacy_options)
        wall[0] = due+100
        await advance(aid, runtime)
        retained = _retained(owned, aid)
        assert len(retained) == 1 and retained[0][1:] == original_file
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, runtime, wall)
        assert owned.connection.execute('SELECT result_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no', (calls[0].run_id,)).fetchall() == raw
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        assert len(calls) == 2 and stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_previously_held_good_page_after_bad_page_keeps_independent_file_fact(tmp_path, capture_resources, startup, canceled):
    from camctl.capture.result_pages import ResultPageSave
    from camctl.operations.validation import validate_outcome
    from ..support.result_cases import begin_result_attempt
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.evidence import EvidenceContract
    from camctl.devices.ports import DriverDeclaration
    from types import SimpleNamespace
    state = {}
    config = AttemptConfig(1, Decimal('1'), Decimal(0))
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, state=state, check_config=config)
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        repository = CaptureRepository()
        wait = TimelapseRepository().complete_wait(
            WaitCompletedSave(aid, wall[0]), new_operation_key(), owned)
        assert wait.kind is DbOutcomeKind.COMPLETED, wait.error
        ticket = begin_result_attempt(owned, aid, config, wall[0])
        first = _actual(ticket, entries=[_other()], next_cursor=_CURSOR)
        second = _actual(ticket, entries=[_osv()], cursor=_CURSOR, finalized=True)
        for number, actual, cursor, instant in ((1, first, None, due-1), (2, second, _CURSOR, due+100)):
            receipt = repository.save_result_page(ResultPageSave(ticket, number, cursor,
                validate_outcome(ticket, actual, state['options']['evidence']), instant), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        raw = _pages(owned)
        original = repository.read_last_result_page(ticket, owned)
        assert owned.connection.execute('SELECT status,result_event_id FROM operation_attempts'
            ' WHERE run_id=?', (ticket.run_id,)).fetchone() == (1, None)
        horizon = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        evidence = state['options']['evidence'].with_contract(
            EvidenceContract('adb_foreground_recovery', 1, 'result', frozenset()))
        declaration = DriverDeclaration(control_supported=True, stop_supported=True,
            query_supported=False, result_supported=False, read_supported=False,
            digest_supported=False, delete_supported=False,
            adb_foreground_recovery_operations=frozenset({'result'}))
        ports = SimpleNamespace(control=state['options']['driver'].control, stop=stopper.stop)
        options = {**state['options'], 'evidence': evidence,
            'drivers': DriverRegistry((DriverEntry('camctl-adb', ports, declaration, evidence,
                DriverStatus.SOFTWARE_CONTRACT_VERIFIED),)),
            'results': DriverResultListing(result, DeviceBinding('cam-1', 'camctl-adb'), evidence),
            'recovery_boundary': RecoveryBoundary.HOST_LOCAL_SETTLED,
            'recovery_max_event_id': lambda: horizon}
        runtime = await _cold_runtime(owned, state, capture_resources, options=options)
        await advance(aid, runtime)
        await advance(aid, runtime)
        assert len(_retained(owned, aid)) == 1
        assert (await runtime.action(aid))['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, runtime, wall)
        assert _pages(owned) == raw
        assert repository.read_last_result_page(ticket, owned) == original
        status, result_json, occurred_at = owned.connection.execute(
            'SELECT t.status,t.result_json,e.occurred_at FROM operation_attempts t'
            ' JOIN history_events e ON e.id=t.result_event_id WHERE t.run_id=?',
            (ticket.run_id,)).fetchone()
        assert status == 2 and occurred_at == due+100
        assert json.loads(result_json)['observations'] == [
            {'type': item.type, 'version': item.version, 'data': dict(item.data)}
            for item in second.observations]
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        result.list_results.assert_not_awaited()
    finally:
        owned.connection.close()


async def test_retained_sources_keep_bounded_pages_and_original_completion_metadata(tmp_path, monkeypatch):
    from camctl.persistence.repositories.capture import CaptureRepository
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, 1000, check_config=AttemptConfig(2, Decimal('1'), Decimal(0)))
    calls, reads = [], []
    read = CaptureRepository.read_result_sources
    def bounded(repository, ticket, cursor, batch, connection):
        reads.append((ticket, cursor, batch))
        assert batch == 32
        return read(repository, ticket, cursor, batch, connection)
    monkeypatch.setattr(CaptureRepository, 'read_result_sources', bounded)
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv(), _other()], error=ErrorValue('device_error', 'device')))
        wall[0] = due-1
        # 同一原片的坏新元数据不能覆盖可靠完成来源的名称和类型。
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[{**_osv(), 'original_name': 'bad-new-name.OSV'}], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += 2200*1000000
        await advance(aid, runtime)
        await advance(aid, runtime)
        wall[0] = due+100
        await advance(aid, runtime)
        assert (await runtime.action(aid))['status'] == 4 and len(_retained(owned, aid)) == 2
        first = read(CaptureRepository(), calls[0], None, 1, owned)
        second = read(CaptureRepository(), calls[0], first.next_cursor, 1, owned)
        assert first.next_cursor is not None and second.next_cursor is None
        assert len(first.items) == len(second.items) == 1
        assert first.items[0][0].identity != second.items[0][0].identity
        assert first.items[0][0].original_name is None
        assert reads and len(calls) == 2
    finally:
        owned.connection.close()
