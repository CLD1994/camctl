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
