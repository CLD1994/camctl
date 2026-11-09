"""媒体工具的原始返回和正常停止事实独立于业务文案保留。"""
import asyncio
import base64
import errno
import shlex
import sys
from pathlib import Path

import pytest

from camctl.host_files import media
from camctl.operations.process import ToolStartError
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId
from camctl.session.supervision import Supervisor

pytestmark = pytest.mark.asyncio

# 单帧 MPEG-4 测试视频；工具替身读取实际登记的输入文件。
_VIDEO = base64.b64decode(
    "AAAAHGZ0eXBpc29tAAACAGlzb21pc28ybXA0MQAAAAhmcmVlAAAAGW1kYXQAAAGzABAHAAABthYFGCPbfgAAA0Ftb292AAAA"
    "bG12aGQAAAAAAAAAAAAAAAAAAAPoAAAD6AABAAABAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAA"
    "AEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAACa3RyYWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAEAAAAAAAAD"
    "6AAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAEAAAABAAAAAAACRlZHRzAAAA"
    "HGVsc3QAAAAAAAAAAQAAA+gAAAAAAAEAAAAAAeNtZGlhAAAAIG1kaGQAAAAAAAAAAAAAAAAAAEAAAABAAFXEAAAAAAAtaGRs"
    "cgAAAAAAAAAAdmlkZQAAAAAAAAAAAAAAAFZpZGVvSGFuZGxlcgAAAAGObWluZgAAABR2bWhkAAAAAQAAAAAAAAAAAAAAJGRp"
    "bmYAAAAcZHJlZgAAAAAAAAABAAAADHVybCAAAAABAAABTnN0YmwAAADqc3RzZAAAAAAAAAABAAAA2m1wNHYAAAAAAAAAAQAA"
    "AAAAAAAAAAAAAAAAAAAAEAAQAEgAAABIAAAAAAAAAAETTGF2YzYwLjMxLjEwMiBtcGVnNAAAAAAAAAAAAAAAAAAY//8AAABg"
    "ZXNkcwAAAAADgICATwABAASAgIBBIBEAAAAAAw1AAAAAiAWAgIAvAAABsAEAAAG1iRMAAAEAAAABIADEjYgADQCEAhRjAAAB"
    "skxhdmM2MC4zMS4xMDIGgICAAQIAAAAQcGFzcAAAAAEAAAABAAAAFGJ0cnQAAAAAAAMNQAAAAIgAAAAYc3R0cwAAAAAAAAAB"
    "AAAAAQAAQAAAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAEAAAABAAAAFHN0c3oAAAAAAAAAEQAAAAEAAAAUc3RjbwAAAAAAAAAB"
    "AAAALAAAAGJ1ZHRhAAAAWm1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAALWlsc3QAAAAlqXRv"
    "bwAAAB1kYXRhAAAAAQAAAABMYXZmNjAuMTYuMTAw"
)


class ProbeOwner:
    async def take_over(self, task):
        raise AssertionError("本用例等待实际结果，不应发生取消接手")


def _tool(tmp_path: Path, body: str) -> str:
    helper = tmp_path / "probe.py"
    helper.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = tmp_path / "probe.cmd"
        launcher.write_text(f'@"{sys.executable}" "{helper}" %*\n', encoding="utf-8")
    else:
        launcher = tmp_path / "probe.sh"
        launcher.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return str(launcher)


async def _public_probe(tmp_path: Path, tool: str, *, stopped=False):
    staging = tmp_path / "staging"
    directory = staging / "recording-inputs"
    directory.mkdir(parents=True)
    source = directory / "1.mp4"
    source.write_bytes(_VIDEO)
    supervisor = Supervisor()

    class StopBeforeExecution(FileTaskExecutor):
        async def run_async_file_task(self, task, owner):
            # 任务在同一事件循环回合登记，停止请求先于执行体调度。
            asyncio.get_running_loop().call_soon(self.request_stop, task.task_id)
            return await super().run_async_file_task(task, owner)

    executor = StopBeforeExecution(supervisor) if stopped else FileTaskExecutor(supervisor)
    result = await media.probe_media(
        FileRef(1, FilePurpose.RECORDING_INPUT, "recording-inputs/1.mp4", staging),
        BoundDirectories(staging=staging), media.ProbeRequest(ffprobe=tool),
        executor=executor, task_id=FileTaskId("probe-diagnostics"), owner=ProbeOwner())
    assert result.ran and result.error is None
    assert executor.unfinished_files() == ()
    assert not (await supervisor.drain_required()).failed
    assert source.read_bytes() == _VIDEO
    return result.value


async def test_failed_probe_keeps_original_tool_result(tmp_path):
    tool = _tool(tmp_path, r"""
import pathlib, sys
assert pathlib.Path(sys.argv[-1]).read_bytes().startswith(b"\x00\x00\x00\x1cftyp")
sys.stdout.buffer.write(b"private media stdout")
sys.stderr.buffer.write(b"RAW-MEDIA-ERROR\xff")
sys.exit(7)
""")
    probe = await _public_probe(tmp_path, tool)
    assert probe.error is not None and probe.error.startswith("tool_failed")
    outcome = probe.tool_diagnostics.outcome
    assert outcome.exit.exit_code == 7
    assert outcome.error is None
    assert outcome.output == b"private media stdout"
    assert outcome.stderr == b"RAW-MEDIA-ERROR\xff"
    assert probe.tool_diagnostics.exception is None


async def test_probe_start_failure_keeps_original_exception_graph(tmp_path):
    missing = str(tmp_path / "missing-ffprobe")
    probe = await _public_probe(tmp_path, missing)
    assert probe.error is not None and probe.error.startswith("tool_unavailable")
    error = probe.tool_diagnostics.exception
    assert isinstance(error, ToolStartError)
    assert isinstance(error.__cause__, OSError)
    assert error.__cause__.errno == errno.ENOENT
    assert probe.tool_diagnostics.outcome is None


async def test_probe_stopped_before_start_has_explicit_control_fact(tmp_path):
    marker = tmp_path / "tool-started"
    tool = _tool(tmp_path, f"from pathlib import Path; Path({str(marker)!r}).touch()")
    probe = await _public_probe(tmp_path, tool, stopped=True)
    assert probe.error is not None and probe.error.startswith("tool_cancelled")
    assert probe.tool_diagnostics.stopped_before_start is True
    assert probe.tool_diagnostics.outcome is None
    assert probe.tool_diagnostics.exception is None
    assert not marker.exists()
