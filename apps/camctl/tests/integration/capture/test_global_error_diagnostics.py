"""实际拍摄组合事务和媒体链保留业务结果，同时输出公共错误诊断。"""

import json
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.media import CheckExecutionPhase
from camctl.capture.media_flow import run_recording_media
from camctl.host_files.media import MediaProbe
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.operations.models import ErrorValue

from .test_input_copy import pipeline  # noqa: F401
from ..support.capture_diagnostic_cases import diagnostic_case, actual_call, call_world, started_call
from ..support.call_cases import SavedCallBoundary
from ..support.capture_media_cases import media_cases, MediaCase, ScriptTools

pytestmark = pytest.mark.asyncio


async def test_start_call_failure_and_final_exhaustion_share_one_full_diagnostic(tmp_path, diagnostic_case):
    case = diagnostic_case
    runtime = await call_world(case, tmp_path, maximum=1, control=lambda request:
        actual_call('control', request.ticket, no_effect=True,
            error=ErrorValue('transport_timeout', 'transport', {'timeout_s': Decimal('.75')})))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            await capture_handler('camera_record')(case.action_id, runtime)
    document, = [json.loads(record) for record in records]
    assert document['error']['code'] == 'transport_timeout'
    assert document['run_error']['code'] == 'start_attempts_exhausted'
    assert document['ticket']['responsibility_key'] == f'start/{case.action_id}'
    assert case.owned.connection.execute('SELECT status FROM actions WHERE id=?', (case.action_id,)).fetchone() == (4,)
    assert case.owned.connection.execute('SELECT status FROM operation_attempts').fetchone() == (3,)


async def test_confirmed_stop_keeps_actual_call_error_and_tool_stderr(tmp_path, diagnostic_case):
    case = diagnostic_case
    runtime = await started_call(case, tmp_path, stop=lambda request:
        actual_call('stop', request.ticket, confirmed=True, raw=b'stop stderr\xff',
            error=ErrorValue('transport_timeout', 'transport', {'timeout_s': Decimal('.125')})))
    case.clock.wall += 60_000_000
    case.clock.monotonic += 60_000_000_000
    case.boundary = 'stop'
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            with pytest.raises(SavedCallBoundary):
                await capture_handler('camera_record')(case.action_id, runtime)
            assert not records
    document, = [json.loads(record) for record in records]
    assert document['error']['code'] == 'transport_timeout'
    request, = case.stop.calls
    stored = case.owned.connection.execute('SELECT status,error_json FROM operation_attempts WHERE run_id=?',
        (request.ticket.run_id,)).fetchone()
    assert stored[0] == 3 and json.loads(stored[1])['code'] == 'transport_timeout'
    assert case.owned.connection.execute('SELECT activity_state FROM device_activities WHERE id=?',
        (case.activity_id,)).fetchone() == (3,)
    assert document['outcome']['diagnostics']['calls'][0]['stderr']['hex'] == b'stop stderr\xff'.hex()
    assert len(case.stop.calls) == 1


async def test_media_check_failure_is_logged_after_real_read_and_reliable_save(pipeline, media_cases):
    owned, roots = pipeline[:2]
    tools = ScriptTools(MediaProbe(None, 'probe_failed: media unavailable'))
    case = await MediaCase(owned, roots, 11, tools).start()
    try:
        records = []
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                step = await run_recording_media(case.runtime.media, 1, 1, 11)
                assert not records
        assert step.phase is CheckExecutionPhase.CHECK_TERMINAL
        document, = [json.loads(record) for record in records]
        assert document['stage'] == 'capture.media_check'
        assert document['context'] == {'action_id': 1, 'processing_id': 1}
        state, media_json = owned.connection.execute('SELECT check_state,media_json FROM recording_processing WHERE id=1').fetchone()
        assert state == 4
        assert json.loads(media_json)['error']['code'] == document['error']['code']
        assert tools.calls == ['probe']
        assert owned.connection.execute('SELECT status,result_json IS NOT NULL FROM operation_attempts WHERE run_id IN (SELECT id FROM operation_runs WHERE kind=3)').fetchall() == [(2, 1)]
    finally:
        owned.connection.close()
