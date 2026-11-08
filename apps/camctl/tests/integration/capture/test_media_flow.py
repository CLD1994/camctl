"""录像媒体链调用方的组件集成测试。

真实资格、续传、分段、完整性、检查与修复事务组合 D4 读取会话绑
定：DriverReadSessions 按设备文件完成事实经驱动替身打开真实
ReadSession；run_recording_media 串联输入取得与检查执行，检查完
成后按已保存决定继续或收束；重入不重复拷贝。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.media import CheckExecutionPhase, MediaPolicy
from camctl.capture.media_flow import (
    DriverReadSessions,
    MediaFlow,
    load_processing_status,
    run_recording_media,
)
from camctl.devices.read_session import ReadSession, SourceFile
from camctl.host_files.media import MediaProbe
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.outputs.copy import SourceDigest, SourceDigestReader

from ..support.capture import capture_database
from .test_input_copy import _CONTENT, pipeline  # noqa: F401  环境夹具
from .media_retry_fixtures import media_pipeline  # noqa: F401
from ..support.capture_media_cases import MediaCase, ScriptTools, media_cases  # noqa: F401

pytestmark = pytest.mark.asyncio


class _Stream:
    """内存字节流：与受管读取通道同形的同步读取。"""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self._position = 0
        self._stopped = False

    def read(self, limit: int) -> bytes:
        if self._stopped or self._position >= len(self._content):
            return b""
        chunk = self._content[self._position:self._position + limit]
        self._position += len(chunk)
        return chunk

    def stop(self) -> None:
        self._stopped = True

    def close(self) -> None:
        return None


class ReadDriverDouble:
    """D4 驱动替身：记录打开请求并返回真实 ReadSession。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.opens: list[tuple[object, int]] = []

    async def open_read(self, source: SourceFile, offset: int, ticket, *, idle_timeout_s):
        self.opens.append((source, offset))
        return ReadSession(
            source, offset, _Stream(self.content[offset:]), idle_timeout_s)


class ProbeTools:
    """工具替身：返回编排的媒体观察。"""

    def __init__(self, observation: MediaProbe) -> None:
        self.observation = observation
        self.calls: list[str] = []

    async def probe(self, input) -> FileTaskResult:
        self.calls.append("probe")
        return FileTaskResult(
            task_id=FileTaskId("probe"), ran=True, value=self.observation)

    async def repair(self, input, output, *, trim_s) -> FileTaskResult:
        self.calls.append("repair")
        return FileTaskResult(task_id=FileTaskId("repair"), ran=False)


class _Digest:
    """源端摘要读取替身：返回与内容一致的摘要。"""

    def __init__(self, content: bytes) -> None:
        import hashlib

        self._digest = hashlib.sha256(content).hexdigest()

    async def read_digest(self) -> SourceDigest:
        return SourceDigest(digest=self._digest, error=None)


async def _flow(pipeline, driver, tools) -> MediaFlow:
    owned, roots = pipeline[0], pipeline[1]
    database = await capture_database(owned)
    return MediaFlow(
        database=database,
        roots=roots,
        sessions=DriverReadSessions(await capture_database(owned), driver, ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=lambda: 1_750_000_100_000_000,
        digest_supported=True,
        digest=_Digest(_CONTENT),
    )


class TestRunRecordingMedia:
    async def test_input_check_chain_completes_without_repair(self, media_pipeline, media_cases):
        from camctl.host_files.media import MediaStream

        owned, roots, source_id = media_pipeline
        tools = ScriptTools(MediaProbe(Decimal("61"), None, (
            MediaStream(0, "video", "h264", 64, 64, Decimal("61"), False),)))
        case = await MediaCase(owned, roots, source_id, tools).start()
        step = await run_recording_media(case.runtime.media, 1, 1, source_id)
        assert step.phase is CheckExecutionPhase.CHECK_COMPLETED, step
        assert step.repair_decision is not None
        row = owned.connection.execute(
            "SELECT check_state, media_json FROM recording_processing WHERE id = 1"
        ).fetchone()
        assert row[0] == 3
        assert '"seconds":61' in row[1]
        assert owned.connection.execute(
            "SELECT verification_state FROM file_copies").fetchone()[0] == 3
        relative = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE purpose = 2"
        ).fetchone()[0]
        assert (roots.staging / relative).read_bytes() == _CONTENT

    async def test_reentry_reuses_copy_and_check_conclusion(self, media_pipeline, media_cases):
        from camctl.host_files.media import MediaStream

        owned, roots, source_id = media_pipeline
        tools = ScriptTools(MediaProbe(Decimal("61"), None, (
            MediaStream(0, "video", "h264", 64, 64, Decimal("61"), False),)))
        case = await MediaCase(owned, roots, source_id, tools).start()
        await run_recording_media(case.runtime.media, 1, 1, source_id)
        resumed = await MediaCase(owned, roots, source_id, tools).start()
        step = await run_recording_media(resumed.runtime.media, 1, 1, source_id)
        assert step.phase in (
            CheckExecutionPhase.ALREADY_FINISHED,
            CheckExecutionPhase.FINISHED_DECISION_SAVED), step
        # 重入不再打开读取会话，也不重复拷贝。
        assert len([call for call in case.driver.calls if call[0] == "read"]) == 1
        assert not [call for call in resumed.driver.calls if call[0] == "read"]
        assert len(tools.probes) == 1
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies").fetchone()[0] == 1

    async def test_long_recording_keeps_repair_pending_until_tool_finishes(self, media_pipeline, media_cases):
        import asyncio
        import sys
        from camctl.capture.media import RepairExecutionPhase
        from camctl.host_files.media import MediaStream

        class PausedRepairTools(ScriptTools):
            def configure(self, directory):
                requests = super().configure(directory)
                repair = self.directory / "repair"
                repair.write_text(
                    "#!" + sys.executable + "\n"
                    "import json,pathlib,sys,time\n"
                    f"root=pathlib.Path({str(self.directory)!r})\n"
                    "path=pathlib.Path(sys.argv[-1])\n"
                    "with (root/'calls.jsonl').open('a') as log:\n"
                    "    log.write(json.dumps(['repair',int(path.stem),str(path)])+'\\n')\n"
                    "(root/'repair-started').touch()\n"
                    "while not (root/'release-repair').exists(): time.sleep(.005)\n"
                    f"path.write_bytes({self.content!r})\n"
                )
                repair.chmod(0o755)
                return requests

        owned, roots, source_id = media_pipeline
        tools = PausedRepairTools(MediaProbe(Decimal("200"), None, (
            MediaStream(0, "video", "h264", 64, 64, Decimal("200"), False),)))
        case = await MediaCase(owned, roots, source_id, tools).start()
        task = asyncio.create_task(run_recording_media(case.runtime.media, 1, 1, source_id))

        async def wait_for_repair():
            while not (tools.directory / "repair-started").exists():
                if task.done():
                    await task
                    raise AssertionError("修复工具尚未启动，媒体流程已结束")
                await asyncio.sleep(.005)

        try:
            await asyncio.wait_for(wait_for_repair(), 5)
            assert not task.done()
            decision = owned.connection.execute(
                "SELECT repair_state, repair_basis_json FROM recording_processing WHERE id=1").fetchone()
            assert decision[0] == 4
            assert tools.calls == ["probe", "repair"]
            (tools.directory / "release-repair").touch()
            step = await asyncio.wait_for(task, 5)
            assert step.phase is RepairExecutionPhase.SUCCEEDED, step
            assert (roots.staging / step.output_file.relative_path).read_bytes() == tools.content
        finally:
            (tools.directory / "release-repair").touch()
            await asyncio.gather(task, return_exceptions=True)

    async def test_status_loader_reads_saved_facts(self, pipeline):
        owned = pipeline[0]
        owned.connection.execute(
            "UPDATE recording_processing SET check_state = 3, media_json = ?"
            " WHERE id = 1",
            ('{"check_status": "completed",'
             ' "duration": {"status": "known", "seconds": 90}}',))
        owned.connection.commit()
        status = load_processing_status(owned, 1)
        assert status.check_state == 3
        assert status.check_duration_s == Decimal("90")
        assert status.target_duration_ms == 60000


class _Clock:
    """受控单调钟：按秒推进读数。"""

    def __init__(self, ns: int = 5_000_000_000) -> None:
        self.ns = ns

    def __call__(self) -> int:
        return self.ns

    def advance_s(self, seconds: Decimal) -> None:
        self.ns += int(Decimal(seconds) * 1_000_000_000)


class _FailingStream:
    """读取流替身：读取即抛通信中断。"""

    def read(self, limit: int) -> bytes:
        raise RuntimeError("传输中断")

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FlakyReadDriver:
    """读取驱动替身：读取会话的真实打开计数；流读取即失败。"""

    def __init__(self) -> None:
        self.opens = 0

    async def open_read(self, source: SourceFile, offset: int, ticket, *, idle_timeout_s):
        self.opens += 1
        return ReadSession(source, offset, _FailingStream(), idle_timeout_s)


class TestCopyRetryInterval:
    async def test_segment_failure_waits_interval_before_next_read(
            self, media_pipeline):
        from types import SimpleNamespace
        from camctl.bootstrap.capture_assembly import session_capture_assembly
        from camctl.capture.input_copy import InputPhase
        from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
        from camctl.devices.ports import DriverDeclaration
        from camctl.host_files.tasks import FileTaskExecutor
        from camctl.session.supervision import Supervisor
        from ..bootstrap.test_media_assembly import _SessionDriver, _config
        from .media_retry_fixtures import _EVIDENCE

        owned, roots, source_id = media_pipeline
        driver = _FlakyReadDriver()
        clock = _Clock()
        base = _SessionDriver(_CONTENT)
        ports = SimpleNamespace(control=base.control, stop=base.stop,
            open_read=driver.open_read, digest=base.digest)
        declaration = DriverDeclaration(control_supported=True, stop_supported=True,
            query_supported=False, result_supported=False, read_supported=True,
            digest_supported=True, delete_supported=False)
        registry = DriverRegistry((DriverEntry("camctl-adb", ports, declaration,
            _EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        devices = {key: dict(value) for key, value in _config(roots.staging.parent).devices.items()}
        devices["cam-1"]["copy"] = {"retry_interval_s": Decimal("3")}
        executor = FileTaskExecutor(Supervisor())
        factory = session_capture_assembly(devices=devices, drivers=registry,
            staging=roots.staging, wait_config=lambda _: None,
            wall_us=lambda: 1_750_000_100_000_000, monotonic_ns=clock,
            file_executor=executor)
        runtime = await factory(await capture_database(owned), "cam-1")
        assert runtime is not None and runtime.media is not None
        try:
            step = await run_recording_media(runtime.media, 1, 1, source_id)
            assert step.phase is InputPhase.SEGMENT_FAILED, step
            assert driver.opens == 1
            clock.advance_s(Decimal("1"))
            waited = await run_recording_media(runtime.media, 1, 1, source_id)
            assert waited.phase is InputPhase.RETRY_WAITING, waited
            assert driver.opens == 1
            clock.advance_s(Decimal("2"))
            retried = await run_recording_media(runtime.media, 1, 1, source_id)
            assert retried.phase is InputPhase.SEGMENT_FAILED, retried
            assert driver.opens == 2
        finally:
            assert not (await executor.drain_required()).failed
