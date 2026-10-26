"""媒体 producer 与原保存消费者组合，工具和数据库边界均使用内存替身。"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.capture.media import check_observation_from_probe, _save_repair_failure
from camctl.capture.media_flow import CaptureProcessingSaves, load_processing_status, run_recording_media
from camctl.contracts.json_values import parse_exact_json
from camctl.capture.processing import CheckResultSave
from camctl.host_files import media
from camctl.host_files.models import FileObservation, FileObservationKind
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.operations.process import LocalExit, RawToolOutcome, ToolStartError, SignalFailure, SignalStage
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from .test_input_copy import pipeline  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('entry', ['probe', 'repair'])
@pytest.mark.parametrize('mode', ['exit', 'spawn', 'stopped_before_start', 'cancelled', 'cancelled_with_errors'])
async def test_media_diagnostics_reach_public_output_once_with_original_facts(monkeypatch, entry, mode):
    raw = RawToolOutcome(LocalExit(exit_code=7), b'PRIVATE-MEDIA-STDOUT',
        'cancelled' if mode.startswith('cancelled') else None, None,
        (SignalFailure(SignalStage.TERMINATE, 'signal denied'),) if mode == 'cancelled_with_errors' else (),
        output_failure='pipe close failed' if mode == 'cancelled_with_errors' else None,
        stderr=b'RAW-MEDIA-ERROR\xff')
    error = ToolStartError('tool launch failed')
    error.__cause__ = OSError(2, 'media tool missing')
    tool = AsyncMock(side_effect=error) if mode == 'spawn' else AsyncMock(return_value=raw)
    monkeypatch.setattr(media, '_execute_tool', tool)
    monkeypatch.setattr(media, 'resolve_file', lambda *_: SimpleNamespace(path='/staging/media.mp4'))
    monkeypatch.setattr(media, 'observe_file', lambda *_: FileObservation(
        FileObservationKind.VALID_OBJECT, Path('/staging/media.mp4'), is_file=True, size_bytes=1))
    async def inline(function, *args):
        return function(*args)
    monkeypatch.setattr(media.asyncio, 'to_thread', inline)
    control = SimpleNamespace(stop_requested=mode == 'stopped_before_start',
        stop_event=SimpleNamespace(is_set=lambda: False))
    actual = (await media._probe(None, None, media.ProbeRequest(), control) if entry == 'probe'
        else await media._repair(None, None, None, media.RepairRequest(), control))
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = (21,)
    database.write.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN), DbOutcome(DbOutcomeKind.COMPLETED)]
    saves = CaptureProcessingSaves(database)
    records = []
    with error_diagnostics_context(None, records.append):
        for _ in range(2):
            async with diagnostic_scope():
                if entry == 'probe':
                    await saves.save_check_result(CheckResultSave(3, check_observation_from_probe(actual),
                        100, source_probe=actual))
                else:
                    await _save_repair_failure(SimpleNamespace(
                        processing=SimpleNamespace(processing_id=3), occurred_at=100, saves=saves), None, actual)
    if mode in ('stopped_before_start', 'cancelled'):
        assert records == []
    else:
        document, = [json.loads(record) for record in records]
        encoded = json.dumps(document, ensure_ascii=False)
        if mode == 'spawn':
            assert 'ToolStartError' in encoded
            assert '"errno": 2' in encoded
            assert 'media tool missing' in encoded
        else:
            assert b'RAW-MEDIA-ERROR\xff'.hex() in encoded
            assert 'RAW-MEDIA-ERROR' in encoded
            assert 'exit_code' in encoded
            assert 'PRIVATE-MEDIA-STDOUT' not in encoded
            assert b'PRIVATE-MEDIA-STDOUT'.hex() not in encoded
            if mode == 'cancelled_with_errors':
                assert 'signal denied' in encoded and 'pipe close failed' in encoded
    if mode == 'stopped_before_start':
        tool.assert_not_awaited()
    assert not saves.pending
    assert database.write.await_count == 2


@pytest.mark.parametrize('mode', ['exit', 'spawn', 'stopped_before_start', 'cancelled',
    'signal_failure', 'output_failure', 'close_failure'])
async def test_saved_source_probe_json_roundtrip_preserves_control_and_does_not_repeat_error(
        pipeline, monkeypatch, mode):
    from .test_global_error_diagnostics import _flow
    from .test_media_flow import ProbeTools, ReadDriverDouble
    from .test_input_copy import _CONTENT
    raw = RawToolOutcome(LocalExit(exit_code=7), b'PRIVATE-MEDIA-STDOUT',
        'cancelled' if mode not in ('exit', 'spawn') else None, None,
        (SignalFailure(SignalStage.TERMINATE, 'signal denied'),) if mode == 'signal_failure' else (),
        output_failure={'output_failure': 'output failed', 'close_failure': 'pipe close failed'}.get(mode),
        stderr=b'RAW-MEDIA-ERROR\xff')
    error = ToolStartError('tool launch failed')
    error.__cause__ = OSError(2, 'media tool missing')
    monkeypatch.setattr(media, '_execute_tool',
        AsyncMock(side_effect=error) if mode == 'spawn' else AsyncMock(return_value=raw))
    monkeypatch.setattr(media, 'resolve_file', lambda *_: SimpleNamespace(path='/staging/input.osv'))
    source = await media._probe(None, None, media.ProbeRequest(),
        SimpleNamespace(stop_requested=mode == 'stopped_before_start'))
    owned = pipeline[0]
    owned.connection.execute("UPDATE device_files SET original_name='input.osv' WHERE id=11")
    owned.connection.execute("UPDATE recording_processing SET check_decision=2,"
        "check_basis_json=?,repair_state=3,repair_basis_json=? WHERE id=1", (
        '{"reason":3,"target_duration_ms":60000,"control_elapsed_ns":75000000000}',
        '{"reason":2,"target_duration_ms":60000,"threshold_s":62,"control_elapsed_ns":75000000000}'))
    owned.connection.commit()
    tools = ProbeTools(source)
    flow = _flow(pipeline, ReadDriverDouble(_CONTENT), tools)
    records = []
    try:
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                prepared = await run_recording_media(flow, 1, 1, 11, target_extension='osv')
            assert prepared.phase.value == 'prepared'
            status = load_processing_status(owned, 1)
            assert status.source_probe.tool_diagnostics.control_only is (
                mode in ('stopped_before_start', 'cancelled'))
            assert status.source_probe.tool_diagnostics.outcome is None
            assert status.source_probe.tool_diagnostics.exception is None
            documents = [parse_exact_json(row[0]) for row in owned.connection.execute(
                'SELECT body_json FROM history_events WHERE event_type=19')]
            saved, = [body['evidence']['source_probe'] for body in documents
                if 'source_probe' in body['evidence']]
            assert set(saved) == {'duration_s', 'error', 'format_name', 'streams', 'tool_control'}
            async with diagnostic_scope():
                failed = await run_recording_media(flow, 1, 1, 11, target_extension='osv')
        assert failed.phase.value == 'failed_saved'
        assert tools.calls == ['probe']
        assert load_processing_status(owned, 1).repair_state == 6
        if mode in ('stopped_before_start', 'cancelled'):
            assert records == []
        else:
            document, = [json.loads(record) for record in records]
            encoded = json.dumps(document)
            if mode == 'spawn':
                assert 'ToolStartError' in encoded and 'media tool missing' in encoded
            else:
                assert b'RAW-MEDIA-ERROR\xff'.hex() in encoded
                assert 'PRIVATE-MEDIA-STDOUT' not in encoded
                if mode == 'signal_failure':
                    assert 'signal denied' in encoded
                if mode == 'output_failure':
                    assert 'output failed' in encoded
                if mode == 'close_failure':
                    assert 'pipe close failed' in encoded
    finally:
        owned.connection.close()
