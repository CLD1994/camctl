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
from camctl.contracts.values import ConsistencyError
from camctl.devices.read_session import ReadSession, SourceFile
from camctl.host_files.media import MediaProbe
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.outputs.copy import SourceDigest, SourceDigestReader

from .test_input_copy import _CONTENT, pipeline  # noqa: F401  环境夹具

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

    async def open_read(self, source: SourceFile, offset: int, ticket):
        self.opens.append((source, offset))
        return ReadSession(
            source, offset, _Stream(self.content[offset:]), Decimal("10"))


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


def _flow(pipeline, driver, tools) -> MediaFlow:
    owned, roots = pipeline[0], pipeline[1]
    return MediaFlow(
        owned=owned,
        roots=roots,
        sessions=DriverReadSessions(owned, driver, ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=lambda: 1_750_000_100_000_000,
        digest_supported=True,
        digest=_Digest(_CONTENT),
    )


class TestDriverReadSessions:
    async def test_opens_session_by_completed_file_facts(self, pipeline):
        owned = pipeline[0]
        driver = ReadDriverDouble(_CONTENT)
        sessions = DriverReadSessions(owned, driver, ticket=None)
        session = await sessions.open_session(11, 4)
        assert session.position() == 4
        source, offset = driver.opens[0]
        assert offset == 4
        assert source.size_bytes == len(_CONTENT)
        assert source.file_id  # 身份键承载稳定身份

    async def test_rejects_incomplete_source(self, pipeline):
        owned = pipeline[0]
        owned.connection.execute(
            "UPDATE device_files SET completion_state = 1, size_bytes = NULL"
            " WHERE id = 11")
        owned.connection.commit()
        sessions = DriverReadSessions(owned, ReadDriverDouble(_CONTENT), None)
        with pytest.raises(ConsistencyError):
            await sessions.open_session(11, 0)


class TestRunRecordingMedia:
    async def test_input_check_chain_completes_without_repair(self, pipeline):
        owned, roots = pipeline[0], pipeline[1]
        driver = ReadDriverDouble(_CONTENT)
        tools = ProbeTools(MediaProbe(duration_s=Decimal("61"), error=None))
        step = await run_recording_media(
            _flow(pipeline, driver, tools), 1, 1, 11)
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

    async def test_reentry_reuses_copy_and_check_conclusion(self, pipeline):
        owned = pipeline[0]
        driver = ReadDriverDouble(_CONTENT)
        tools = ProbeTools(MediaProbe(duration_s=Decimal("61"), error=None))
        flow = _flow(pipeline, driver, tools)
        await run_recording_media(flow, 1, 1, 11)
        step = await run_recording_media(
            _flow(pipeline, ReadDriverDouble(_CONTENT), tools), 1, 1, 11)
        assert step.phase in (
            CheckExecutionPhase.ALREADY_FINISHED,
            CheckExecutionPhase.FINISHED_DECISION_SAVED), step
        # 重入不再打开读取会话，也不重复拷贝。
        assert driver.opens == [(driver.opens[0][0], 0)]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies").fetchone()[0] == 1

    async def test_short_duration_requires_repair_flow(self, pipeline):
        owned = pipeline[0]
        tools = ProbeTools(MediaProbe(duration_s=Decimal("200"), error=None))
        step = await run_recording_media(
            _flow(pipeline, ReadDriverDouble(_CONTENT), tools), 1, 1, 11)
        # 多录 200s 超过目标加余量：修复决定保存并登记输出后进入执行，
        # 工具未运行保留运行阶段，下一次推进续跑。
        from camctl.capture.media import RepairExecutionPhase

        assert step.phase is RepairExecutionPhase.TOOL_NOT_COMPLETED, step
        decision = owned.connection.execute(
            "SELECT repair_state, repair_basis_json FROM recording_processing"
            " WHERE id = 1").fetchone()
        assert decision[0] == 4
        assert tools.calls == ["probe", "repair"]

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

    async def open_read(self, source: SourceFile, offset: int, ticket):
        self.opens += 1
        return ReadSession(source, offset, _FailingStream(), Decimal("10"))


class TestCopyRetryInterval:
    async def test_segment_failure_waits_interval_before_next_read(
            self, pipeline):
        from camctl.capture.input_copy import InputPhase

        owned, roots = pipeline[0], pipeline[1]
        driver = _FlakyReadDriver()
        tools = ProbeTools(MediaProbe(duration_s=Decimal("61"), error=None))
        clock = _Clock()
        flow = MediaFlow(
            owned=owned,
            roots=roots,
            sessions=DriverReadSessions(owned, driver, ticket=None),
            tools=tools,
            policy=MediaPolicy(repair_margin_s=Decimal("2")),
            occurred_at=lambda: 1_750_000_100_000_000,
            digest_supported=True,
            digest=_Digest(_CONTENT),
            retry_interval_s=Decimal("3"),
            monotonic_ns=clock,
        )
        step = await run_recording_media(flow, 1, 1, 11)
        # 段传输通信失败：保存失败事实并建立读取重试等待。
        assert step.phase is InputPhase.SEGMENT_FAILED, step
        assert driver.opens == 1
        clock.advance_s(Decimal("1"))
        waited = await run_recording_media(flow, 1, 1, 11)
        # 间隔未到：不开新读取会话，不触设备。
        assert waited.phase is InputPhase.RETRY_WAITING, waited
        assert driver.opens == 1
        clock.advance_s(Decimal("2"))
        retried = await run_recording_media(flow, 1, 1, 11)
        # 到时：恢复续传，再次打开读取会话。
        assert retried.phase is InputPhase.SEGMENT_FAILED, retried
        assert driver.opens == 2
