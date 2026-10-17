"""原生延时的自然结束与提前停止使用各自原始完成依据。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import _listing_round, _register_listing, capture_handler
from camctl.devices.capture_completion import validate_completion_context
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.repositories.history import HistoryRepository

from .result_consumer_fixtures import ResultCatalog, consumer_world
from .test_capture_contract import _NOW
from .test_record_file_completion_wait import (
    _completed_result, _result_port, _stop_context,
)
from .test_result_page_history import _actual, _entry

pytestmark = pytest.mark.asyncio


class NativeWaitCatalog(ResultCatalog):
    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if definition is not None and action_type == 'camera_timelapse':
            factory = definition.task_factory
            return replace(definition, task_factory=lambda params: replace(factory(params),
                result_wait_margin_s=30, file_completion_wait_s=30,
                product_rules=({'kind': 'video', 'format_id': 'osv', 'min_count': 1,
                    'exact_count': None, 'require_pairing': False, 'min_size_bytes': 1},)))
        return definition


def _osv(size=50):
    return {**_entry('a'), 'identity': '/DCIM/a.OSV', 'locator': {'path': '/DCIM/a.OSV'},
        'format_id': 'osv', 'size_bytes': size}


@pytest.mark.parametrize('size,success', [(50, True), (0, False)])
async def test_natural_wait_context_preserves_original_sent_and_replays_nonzero_rule(tmp_path, size, success):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, 'timelapse', catalog=NativeWaitCatalog())
    runtime.check_config = AttemptConfig(3, Decimal('10'), Decimal('0'))
    runtime.wait_config = lambda action: CaptureWaitConfig(
        action['execution_spec_json']['target_duration_ms'],
        action['execution_spec_json']['result_wait_margin_ms'], 0)
    seen = []
    async def read(request, batch):
        context = request.params.get('completion_context')
        assert context is not None and context['kind'] == 'timelapse_natural'
        assert validate_completion_context(context, activity_id=request.ticket.target_id) == context
        assert context['sent_at_us'] == _NOW and context['target_duration_ms'] == 600000
        assert context['result_wait_margin_ms'] == 30000
        assert context['wait_completed_at_us'] >= context['expected_check_at_us']
        assert 'stop_result_event_id' not in context
        assert request.timeout_s == Decimal('10')
        seen.append(context)
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv(size)], finalized=True))
    driver = _result_port(runtime, read)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)['status'] == (3 if success else 4)
        assert len(seen) == 1
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == ((1 if success else 0),)
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_runs WHERE responsibility_key LIKE 'stop/%'").fetchone() == (0,)
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        history = HistoryRepository(path)
        restored = history.restore_entity('action', action_id, history.current_boundary())
        assert restored[('actions', action_id)]['status'] == runtime.action(action_id)['status']
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_native_confirmed_stop_uses_independent_wait_and_original_page_evidence(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, 'cancel', catalog=NativeWaitCatalog())
    runtime.check_config = AttemptConfig(3, Decimal('10'), Decimal('0'))
    context = {**_stop_context(owned, action_id), 'kind': 'timelapse_stop', 'file_completion_wait_ms': 30000}
    completion = {'method': 'stop_return_and_wait', 'version': 1, 'activity_id': context['activity_id'],
        'stop_result_event_id': context['stop_result_event_id'], 'required_wait_ms': 30000,
        'observed_wait_ns': 30_000_000_000, 'completed': True}
    async def read(request, batch):
        assert request.params.get('completion_context') == context
        assert request.timeout_s == Decimal('40')
        return _completed_result(request.ticket, context, entries=[_osv()], finalized=True, completion=completion)
    driver = _result_port(runtime, read)
    try:
        listing = await _listing_round(runtime, action_id)
        registered = tuple(_register_listing(runtime, action_id, listing))
        assert registered[0][0].size_bytes == 50
        evidence, = owned.connection.execute('SELECT completion_evidence_json FROM device_files').fetchone()
        assert 'stop_result_event_id' in evidence and 'result_page_event_id' in evidence
        assert 'wait_completed_event_id' not in evidence
        driver.list_results.assert_awaited_once()
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        history = HistoryRepository(path)
        file_id = registered[0][1]
        restored = history.restore_entity('device_file', file_id, history.current_boundary())
        assert restored[('device_files', file_id)]['size_bytes'] == 50
    finally:
        owned.connection.close()


@pytest.mark.parametrize('change', [{}, {'result_page_event_id': None, 'start_result_event_id': None}, {'activity_id': 999}, {'wait_completed_event_id': 999},
    {'result_page_event_id': 999}, {'size_bytes': 51}, {'observation': {'identity': '/DCIM/wrong.OSV'}}])
async def test_natural_file_completion_retains_and_checks_original_page_sources(tmp_path, change):
    from camctl.capture.files import FileCompletionSave
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.transaction import row_facts
    owned, runtime, action_id, handler = await consumer_world(tmp_path, 'timelapse', catalog=NativeWaitCatalog())
    runtime.wait_config = lambda action: CaptureWaitConfig(600000, 30000)
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    _result_port(runtime, read)
    try:
        await capture_handler(handler)(action_id, runtime)
        file_id, = owned.connection.execute('SELECT id FROM device_files').fetchone()
        evidence = row_facts(owned.connection, 'device_files', file_id)['completion_evidence_json']
        assert evidence['result_page_event_id'] > evidence['wait_completed_event_id']
        if change:
            params = {'file_id': file_id, 'state': 3, 'occurred_at': runtime.wall_us(),
                'basis': 2, 'activity_id': evidence['activity_id'],
                'wait_completed_event_id': evidence['wait_completed_event_id'],
                'result_page_event_id': evidence['result_page_event_id'],
                'start_result_event_id': evidence['start_result_event_id'],
                'observation': evidence['observation'], 'size_bytes': 50, **change}
            receipt = runtime.capture.save_file_completion(FileCompletionSave(**params), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        else:
            path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
            history = HistoryRepository(path)
            restored = history.restore_entity('device_file', file_id, history.current_boundary())
            assert restored[('device_files', file_id)]['completion_evidence_json'] == evidence
    finally:
        owned.connection.close()


async def test_optional_preview_of_original_outside_task_does_not_invalidate_new_original(tmp_path):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, 'timelapse', catalog=NativeWaitCatalog())
    runtime.wait_config = lambda action: CaptureWaitConfig(600000, 30000)
    preview = {**_osv(), 'identity': '/DCIM/old.LRF', 'locator': {'path': '/DCIM/old.LRF'},
        'format_id': 'lrf', 'paired_identity': '/DCIM/old.OSV'}
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv(), preview], finalized=True))
    _result_port(runtime, read)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)['status'] == 3
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (1,)
        assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE source_action_id IS NULL').fetchone() == (1,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('change', [{}, {'wait_completed_event_id': 999}, {'start_result_event_id': None},
    {'observation': {'identity': '/DCIM/wrong.OSV'}}, {'result_page_event_id': 999}])
async def test_natural_file_event_guard_independently_checks_original_sources(tmp_path, change):
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext, EventValidationError, validate_event
    from camctl.persistence.transaction import event_envelope, row_facts, update_change
    owned, runtime, action_id, handler = await consumer_world(tmp_path, 'timelapse', catalog=NativeWaitCatalog())
    runtime.wait_config = lambda action: CaptureWaitConfig(600000, 30000)
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    _result_port(runtime, read)
    try:
        await capture_handler(handler)(action_id, runtime)
        file_id, = owned.connection.execute('SELECT id FROM device_files').fetchone()
        original = row_facts(owned.connection, 'device_files', file_id)['completion_evidence_json']
        state = {'outputs': {}}
        for table in ('actions', 'device_activities', 'operation_runs', 'operation_attempts', 'device_files'):
            state[table] = {identity: row_facts(owned.connection, table, identity)
                for identity, in owned.connection.execute(f'SELECT id FROM {table}')}
        state['device_files'][file_id].update(completion_state=1, completion_evidence_json=None, size_bytes=None)
        columns = ('id', 'transaction_id', 'event_type', 'event_version', 'occurred_at', 'clock_status', 'change_seq', 'body_json')
        state['history_events'] = {}
        for key in ('start_result_event_id', 'wait_completed_event_id', 'result_page_event_id'):
            identity = original[key]
            row = owned.connection.execute('SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?', (identity,)).fetchone()
            state['history_events'][identity] = dict(zip(columns, row))
        event_id, = owned.connection.execute('SELECT MAX(id)+1 FROM history_events').fetchone()
        transaction_id, = owned.connection.execute('SELECT MAX(id)+1 FROM history_transactions').fetchone()
        evidence = {**original, **change}
        if evidence.get('start_result_event_id') is None:
            del evidence['start_result_event_id']
        event = event_envelope(event_id, transaction_id, 17, 3,
            (update_change('device_files', file_id,
                {'completion_state': 1, 'completion_evidence_json': None, 'size_bytes': None},
                {'completion_state': 3, 'completion_evidence_json': evidence, 'size_bytes': 50}),),
            runtime.wall_us(), evidence={'completion_request': dict.fromkeys(('locator', 'original_name', 'media_type'))})
        context = EventContext(TransactionRange(transaction_id, event_id, event_id),
            {('device_files', file_id): ('device_file', file_id)}, state)
        if change:
            with pytest.raises(EventValidationError):
                validate_event(event, context)
        else:
            assert validate_event(event, context).envelope == event
    finally:
        owned.connection.close()


class StartupNativeCatalog(NativeWaitCatalog):
    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if definition is not None and action_type == 'camera_timelapse':
            factory = definition.task_factory
            return replace(definition, task_factory=lambda params: replace(factory(params),
                target_duration_s=300, startup_margin_ms=1000))
        return definition


async def test_new_native_context_and_history_preserve_original_startup_margin(tmp_path):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, 'timelapse', catalog=StartupNativeCatalog())
    runtime.wait_config = lambda action: CaptureWaitConfig(300000, 30000, startup_margin_ms=1000)
    seen = []
    async def read(request, batch):
        context = request.params['completion_context']
        assert context['startup_margin_ms'] == 1000
        assert context['expected_check_at_us'] == _NOW+331000000
        assert context['target_duration_ms'] == 300000
        seen.append(context)
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    _result_port(runtime, read)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)['status'] == 3
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        history = HistoryRepository(path)
        restored = history.restore_entity('action', action_id, history.current_boundary())
        assert restored[('actions', action_id)]['execution_spec_json']['startup_margin_ms'] == 1000
        assert len(seen) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('change', ['none', 'startup', 'early_wait', 'future_page', 'future_time'])
async def test_new_natural_file_guard_uses_original_startup_and_actual_event_times(tmp_path, change):
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext, EventValidationError, validate_event
    from camctl.persistence.transaction import event_envelope, row_facts, update_change
    owned, runtime, action_id, handler = await consumer_world(tmp_path, 'timelapse', catalog=StartupNativeCatalog())
    runtime.wait_config = lambda action: CaptureWaitConfig(300000, 30000, startup_margin_ms=1000)
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    _result_port(runtime, read)
    try:
        await capture_handler(handler)(action_id, runtime)
        file_id, = owned.connection.execute('SELECT id FROM device_files').fetchone()
        original = row_facts(owned.connection, 'device_files', file_id)['completion_evidence_json']
        state = {'outputs': {}}
        for table in ('actions', 'device_activities', 'operation_runs', 'operation_attempts', 'device_files'):
            state[table] = {identity: row_facts(owned.connection, table, identity)
                for identity, in owned.connection.execute(f'SELECT id FROM {table}')}
        state['device_files'][file_id].update(completion_state=1, completion_evidence_json=None, size_bytes=None)
        columns = ('id', 'transaction_id', 'event_type', 'event_version', 'occurred_at', 'clock_status', 'change_seq', 'body_json')
        state['history_events'] = {}
        for key in ('start_result_event_id', 'wait_completed_event_id', 'result_page_event_id'):
            identity = original[key]
            row = owned.connection.execute('SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?', (identity,)).fetchone()
            state['history_events'][identity] = dict(zip(columns, row))
        if change == 'startup':
            state['actions'][action_id]['execution_spec_json']['startup_margin_ms'] = 2000
        elif change == 'early_wait':
            state['history_events'][original['wait_completed_event_id']]['occurred_at'] = _NOW+330000000
        elif change == 'future_page':
            state['history_events'][original['result_page_event_id']]['occurred_at'] = _NOW+330000000
        elif change == 'future_time':
            state['history_events'][original['result_page_event_id']]['occurred_at'] = _NOW+800000000
        event_id, = owned.connection.execute('SELECT MAX(id)+1 FROM history_events').fetchone()
        transaction_id, = owned.connection.execute('SELECT MAX(id)+1 FROM history_transactions').fetchone()
        event = event_envelope(event_id, transaction_id, 17, 3,
            (update_change('device_files', file_id,
                {'completion_state': 1, 'completion_evidence_json': None, 'size_bytes': None},
                {'completion_state': 3, 'completion_evidence_json': original, 'size_bytes': 50}),),
            runtime.wall_us(), evidence={'completion_request': dict.fromkeys(('locator', 'original_name', 'media_type'))})
        context = EventContext(TransactionRange(transaction_id, event_id, event_id),
            {('device_files', file_id): ('device_file', file_id)}, state)
        if change == 'none':
            assert validate_event(event, context).envelope == event
        else:
            with pytest.raises(EventValidationError):
                validate_event(event, context)
    finally:
        owned.connection.close()
