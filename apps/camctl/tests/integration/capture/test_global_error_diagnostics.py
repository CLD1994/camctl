"""实际拍摄组合事务和媒体链保留业务结果，同时输出公共错误诊断。"""
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import CaptureRuntime, _stop_call, capture_handler
from camctl.capture.media import CheckExecutionPhase
from camctl.capture.media_flow import run_recording_media
from camctl.host_files.media import MediaProbe
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.operations.models import ErrorValue
from .test_recording_start_runtime import _result, _world, StartDriver
from .test_call_result_save import _diagnosed_stop, ReturnedCall, _CALL_EVIDENCE
from .test_input_copy import _CONTENT, pipeline  # noqa: F401
from .test_media_flow import ProbeTools, ReadDriverDouble, _Digest
from .test_capture_contract import _NOW, ResultsDouble
from support.business_database import inline_business_database
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import MediaFlow, DriverReadSessions
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.timelapse import TimelapseRepository
from camctl.scheduling.rules import LaunchWindow

pytestmark = pytest.mark.asyncio


def _configured(owned, driver, *, maximum=3):
    return CaptureRuntime(database=inline_business_database(owned),
        scheduling=SchedulingRepository(), operations=OperationRepository(),
        capture=CaptureRepository(), timelapse=TimelapseRepository(), driver=driver,
        results=ResultsDouble({}), evidence=_CALL_EVIDENCE,
        wall_us=lambda: _NOW, monotonic_ns=lambda: 7_000_000_000,
        window_of=lambda action: LaunchWindow(action['scheduled_at'],
            action['scheduled_at'] + action['max_delay_ms'] * 1000),
        wait_config=lambda _: CaptureWaitConfig(600_000, 0),
        start_config=AttemptConfig(maximum, Decimal('0.75'), Decimal(1)))


def _flow(pipeline, driver, tools):
    database = inline_business_database(pipeline[0])
    return MediaFlow(database=database, roots=pipeline[1],
        sessions=DriverReadSessions(database, driver, ticket=None), tools=tools,
        policy=MediaPolicy(Decimal(2)), occurred_at=lambda: 1_750_000_100_000_000,
        digest_supported=True, digest=_Digest(_CONTENT))


async def test_start_call_failure_and_final_exhaustion_share_one_full_diagnostic(tmp_path):
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(no_effect=True,
            error=ErrorValue('transport_timeout', 'transport', {'timeout_s': Decimal('0.75')}))), maximum=1)
        records = []
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                await capture_handler('camera_record')(12, runtime)
        document, = [json.loads(record) for record in records]
        assert document['error']['code'] == 'transport_timeout'
        assert document['run_error']['code'] == 'start_attempts_exhausted'
        assert document['ticket']['responsibility_key'] == 'start/12'
        assert owned.connection.execute('SELECT status FROM actions WHERE id=12').fetchone() == (4,)
        assert owned.connection.execute('SELECT status FROM operation_attempts').fetchone() == (3,)
    finally:
        owned.connection.close()


async def test_confirmed_stop_keeps_actual_call_error_and_tool_stderr(tmp_path):
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        await capture_handler('camera_record')(12, runtime)
        runtime.stop_config = AttemptConfig(1, Decimal('0.125'), Decimal(1))
        actual = _diagnosed_stop(runtime)
        call = actual.diagnostics.calls[0]
        actual = replace(actual, diagnostics=replace(actual.diagnostics,
            calls=(replace(call, stderr=replace(call.stderr, data=b'stop stderr\xff')),)))
        runtime.stopper = ReturnedCall(actual)
        records = []
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                result = await _stop_call(runtime, await runtime.action(12))
                assert result.phase == 'confirmed'
                assert not records
        document, = [json.loads(record) for record in records]
        assert document['error']['code'] == 'transport_timeout'
        call, = runtime.stopper.calls
        stored = owned.connection.execute('SELECT status,error_json FROM operation_attempts WHERE run_id=?',
            (call.ticket.run_id,)).fetchone()
        assert stored[0] == 3 and json.loads(stored[1])['code'] == 'transport_timeout'
        assert document['outcome']['diagnostics']['calls'][0]['stderr']['hex'] == b'stop stderr\xff'.hex()
        assert len(runtime.stopper.calls) == 1
    finally:
        owned.connection.close()


async def test_media_check_failure_is_logged_after_real_read_and_reliable_save(pipeline):
    owned = pipeline[0]
    tools = ProbeTools(MediaProbe(None, 'probe_failed: media unavailable'))
    records = []
    with error_diagnostics_context(None, records.append):
        async with diagnostic_scope():
            step = await run_recording_media(_flow(pipeline, ReadDriverDouble(_CONTENT), tools), 1, 1, 11)
            assert not records
    assert step.phase is CheckExecutionPhase.CHECK_TERMINAL
    document, = [json.loads(record) for record in records]
    assert document['stage'] == 'capture.media_check'
    assert document['context'] == {'action_id': 1, 'processing_id': 1}
    state, media_json = owned.connection.execute('SELECT check_state,media_json FROM recording_processing WHERE id=1').fetchone()
    assert state == 4
    assert json.loads(media_json)['error']['code'] == document['error']['code']
