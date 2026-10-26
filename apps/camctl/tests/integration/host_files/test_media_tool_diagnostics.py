"""媒体工具的原始返回和正常停止事实独立于业务文案保留。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.host_files import media
from camctl.operations.process import LocalExit, RawToolOutcome, ToolStartError

pytestmark = pytest.mark.asyncio


async def test_failed_probe_keeps_original_tool_result(monkeypatch):
    raw = RawToolOutcome(LocalExit(exit_code=7), b'private media stdout', None, None,
        stderr=b'RAW-MEDIA-ERROR\xff')
    monkeypatch.setattr(media, 'resolve_file', lambda *_: SimpleNamespace(path='/staging/input.mp4'))
    monkeypatch.setattr(media, '_execute_tool', AsyncMock(return_value=raw))
    probe = await media._probe(None, None, media.ProbeRequest(), SimpleNamespace(stop_requested=False))
    assert probe.error is not None
    assert probe.tool_diagnostics.outcome is raw
    assert probe.tool_diagnostics.exception is None


async def test_probe_start_failure_keeps_original_exception_graph(monkeypatch):
    error = ToolStartError('tool launch failed')
    error.__cause__ = OSError(2, 'ffprobe missing')
    monkeypatch.setattr(media, 'resolve_file', lambda *_: SimpleNamespace(path='/staging/input.mp4'))
    monkeypatch.setattr(media, '_execute_tool', AsyncMock(side_effect=error))
    probe = await media._probe(None, None, media.ProbeRequest(), SimpleNamespace(stop_requested=False))
    assert probe.tool_diagnostics.exception is error
    assert probe.tool_diagnostics.exception.__cause__.errno == 2


async def test_probe_stopped_before_start_has_explicit_control_fact(monkeypatch):
    monkeypatch.setattr(media, 'resolve_file', lambda *_: SimpleNamespace(path='/staging/input.mp4'))
    executor = AsyncMock(side_effect=AssertionError('must not run'))
    monkeypatch.setattr(media, '_execute_tool', executor)
    probe = await media._probe(None, None, media.ProbeRequest(), SimpleNamespace(stop_requested=True))
    assert probe.tool_diagnostics.stopped_before_start is True
    assert probe.tool_diagnostics.outcome is None
    executor.assert_not_awaited()
