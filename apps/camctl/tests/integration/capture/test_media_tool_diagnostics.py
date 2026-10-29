"""公开工具结果经正式媒体消费者、真实文件线程和保存重送交付完整诊断。"""
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.media_flow import load_processing_status, run_recording_media
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.host_files.media import MediaArtifact, MediaProbe, MediaStream
from camctl.host_files.models import FileObservation, FileObservationKind
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context, flush_diagnostics
from camctl.persistence.repositories.capture import CaptureRepository

from .media_retry_fixtures import media_pipeline as pipeline
from .test_osv_input_failure import prepared_input
from ..support.capture_media_cases import (
    ScriptTools, assembled_media, diagnostic_media_ports, diagnostic_probe,
    diagnostic_tool_result, media_case, media_cases,
)

pytestmark = pytest.mark.asyncio


def assert_diagnostics(records, mode):
    if mode in ('stopped_before_start', 'cancelled'):
        assert records == []
        return
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
        if mode in ('signal_failure', 'cancelled_with_errors'):
            assert 'signal denied' in encoded
        if mode in ('output_failure', 'cancelled_with_errors'):
            assert 'output failed' in encoded if mode == 'output_failure' else 'pipe close failed' in encoded
        if mode == 'close_failure':
            assert 'pipe close failed' in encoded


@pytest.mark.parametrize('entry', ['probe', 'repair'])
@pytest.mark.parametrize('mode', ['exit', 'spawn', 'stopped_before_start', 'cancelled', 'cancelled_with_errors'])
async def test_media_diagnostics_reach_public_output_once_with_original_facts(pipeline, monkeypatch, entry, mode):
    from ..bootstrap.test_binding_transactions import _CommitFailure
    tool = await diagnostic_tool_result(mode)
    source = diagnostic_probe(tool)
    successful = MediaProbe(Decimal(200), None, (
        MediaStream(0, 'video', 'h264', 64, 64, Decimal(200), False),))
    artifact = MediaArtifact(source.error, FileObservation(
        FileObservationKind.MISSING, Path(pipeline[1].staging) / 'derived/missing.mp4'),
        tool_diagnostics=tool)
    counts = diagnostic_media_ports(monkeypatch, probe=source if entry == 'probe' else None,
        artifact=artifact, successful_probe=successful)
    tools = ScriptTools(successful)
    flow = await assembled_media(pipeline, None, tools)
    stage = 'save_check_result' if entry == 'probe' else 'save_repair_result'
    actual = getattr(CaptureRepository, stage)
    requests = []
    def save(repository, command, key, current):
        relevant = entry == 'repair' or command.source_probe is not None
        if relevant:
            requests.append((command, key))
            if len(requests) == 1:
                current = replace(current, connection=_CommitFailure(current.connection, True))
        return actual(repository, command, key, current)
    monkeypatch.setattr(CaptureRepository, stage, save)
    records = []
    with error_diagnostics_context(None, records.append):
        with pytest.raises(ConsistencyError):
            async with diagnostic_scope():
                await run_recording_media(flow, 1, 1, pipeline[2])
        assert len(requests) == 1
        async with diagnostic_scope():
            await run_recording_media(flow, 1, 1, pipeline[2])
        await media_case(flow).resume_saves()
        await flush_diagnostics()
    assert requests == [requests[0], requests[0]]
    assert counts == {'probe': 1, 'repair': int(entry == 'repair')}
    assert flow.file_executor.unfinished_files() == ()
    assert_diagnostics(records, mode)


@pytest.mark.parametrize('mode', ['exit', 'spawn', 'stopped_before_start', 'cancelled',
    'signal_failure', 'output_failure', 'close_failure'])
async def test_saved_source_probe_json_roundtrip_preserves_control_and_does_not_repeat_error(
        tmp_path, monkeypatch, mode):
    tool = await diagnostic_tool_result(mode)
    source = diagnostic_probe(tool)
    counts = diagnostic_media_ports(monkeypatch, probe=source)
    owned, runtime, flow, source_id, _original, tools, entry = await prepared_input(tmp_path, False, 'valid')
    records = []
    try:
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                prepared = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
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
                failed = await run_recording_media(flow, 1, 1, source_id, target_extension='osv')
        assert failed.phase.value == 'failed_saved'
        assert counts == {'probe': 1, 'repair': 0}
        assert load_processing_status(owned, 1).repair_state == 6
        assert_diagnostics(records, mode)
    finally:
        owned.connection.close()
