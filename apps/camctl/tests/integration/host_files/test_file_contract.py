"""F7 文件消费者及失败边界的组合集成测试。

真实文件与各文件边界组合：对写入、同步、移动、删除注入错误或
中断，断言每个边界返回的阶段事实与文件系统的实际观察一致；停
止在块间生效并保留已确认字节。三类消费者按各自规则核对：普通
交付的终局未知失败不自动重投，报告恢复按实际位置分类，日志副
本失败停止后续步骤并保留分类。文件身份的占用登记与积压测量只
反映未结束任务，收场即释放。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import camctl.host_files.handoff as handoff
import camctl.host_files.io as file_io
from camctl.devices.read_session import SourceFile, open_read
from camctl.host_files.handoff import (
    HandoffDirectories,
    HandoffIdentity,
    PublishResult,
    PublishStage,
    ReadyName,
    WithdrawStage,
    publish_file,
    withdraw_file,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.segments import SegmentFailure, SegmentSpec, transfer_segment
from camctl.host_files.tasks import FileTask, FileTaskExecutor, FileTaskId
from camctl.logging_runtime.copies import (
    CopyOutcome,
    CopyRequest,
    MarkerStore,
    deliver_failure_copy,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord
from camctl.operations.models import AttemptTicket
from camctl.outputs.handoff import (
    DeliveryFacts,
    DeliveryHandoffOutcome,
    DeliveryLocations,
    LocationObservation,
    decide_handoff,
)
from camctl.reporting.models import FrozenReport
from camctl.reporting.publication import (
    ReportDirectories,
    ReportFileDecision,
    observe_report_locations,
    recover_report_files,
    report_file_name,
)
from camctl.session.supervision import ResponsibilityOwner, Supervisor

pytestmark = pytest.mark.asyncio

_TICKET = AttemptTicket(
    attempt_id=1, operation="read", target_id="9",
    responsibility_key="copy/9", run_id=1,
)

_NO_DATA_TIMEOUT_S = Decimal("30")


class _OffsetStream:
    """真实读取流替身：遵守 read(limit) 顺序协议，可选中途失败。"""

    def __init__(self, payload: bytes, fail_on_read: int | None = None) -> None:
        self._payload = payload
        self._offset = 0
        self._fail_on = fail_on_read
        self.read_calls = 0

    def read(self, limit: int) -> bytes:
        self.read_calls += 1
        if self._fail_on is not None and self.read_calls >= self._fail_on:
            raise OSError("injected source read failure")
        chunk = self._payload[self._offset:self._offset + limit]
        self._offset += len(chunk)
        return chunk

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class _TargetFile:
    """真实顺序写入端：os 层无缓冲，显式同步并记录同步次数。"""

    _BINARY = getattr(os, "O_BINARY", 0)

    def __init__(self, path: Path) -> None:
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | self._BINARY, 0o600)
        self.sync_calls = 0

    def write(self, data) -> int:
        return os.write(self._fd, data)

    def sync(self) -> None:
        os.fsync(self._fd)
        self.sync_calls += 1

    def close(self) -> None:
        os.close(self._fd)


class _StoppingTarget:
    """转发到真实写入端，并在第 N 次写入后触发停止。"""

    def __init__(self, target: _TargetFile, stop: threading.Event,
                 after_writes: int) -> None:
        self._target = target
        self._stop = stop
        self._after = after_writes
        self.writes = 0

    def write(self, data) -> int:
        count = self._target.write(data)
        self.writes += 1
        if self.writes >= self._after:
            self._stop.set()
        return count

    def sync(self) -> None:
        self._target.sync()

    def close(self) -> None:
        self._target.close()


class _CopyChannelDouble:
    """副本通道替身：真实写入目标文件，受追加语义约束。"""

    def __init__(self) -> None:
        self.copied_into: Path | None = None

    def copy_with_write(self, record, destination: Path):
        destination.write_bytes(b"log-copy-bytes")
        self.copied_into = destination
        return _AppendResult(appended=True), None


class _AppendResult:
    def __init__(self, appended: bool) -> None:
        self.appended = appended
        self.append_error = None if appended else "append failed"


def _handoff_dirs(tmp_path: Path, content: bytes | None = b"payload"):
    staging = tmp_path / "staging" / "deliveries"
    ready = tmp_path / "ready"
    processing = tmp_path / "processing"
    staging.mkdir(parents=True)
    ready.mkdir()
    processing.mkdir()
    source = None
    if content is not None:
        source = staging / "7.bin"
        source.write_bytes(content)
    return staging.parent, ready, processing, source


def _staging_ref(staging: Path) -> tuple[FileRef, BoundDirectories]:
    ref = FileRef(
        file_id=7, purpose=FilePurpose.DELIVERY_COPY,
        relative_path="deliveries/7.bin", root=staging,
    )
    return ref, BoundDirectories(staging=staging)


async def _publish_by_move(source: Path, directories, target: ReadyName):
    shutil.move(str(source), str(directories.ready / target.name))
    return PublishResult(
        stage=PublishStage.MOVED, directory=DirectorySyncStage.SYNCED,
        source_removed=True, error=None,
    )


class TestBoundaryEffects:
    async def test_write_failure_keeps_confirmed_bytes(
            self, tmp_path: Path) -> None:
        """写入边界：源读取失败时已确认字节与目标文件观察一致。"""
        payload = bytes(index % 251 for index in range(8000))
        stream = _OffsetStream(payload, fail_on_read=2)
        session = await open_read(
            SourceFile("9", {}, 8000), 0, _TICKET, stream=stream,
            no_data_timeout_s=_NO_DATA_TIMEOUT_S,
        )
        target_path = tmp_path / "target.bin"
        target_path.touch()
        target = _TargetFile(target_path)
        result = transfer_segment(
            SegmentSpec("copy/9", 0, "copy.part", 0, 8000, 4000,
                        threading.Event()),
            session, target,
        )
        # 记录的效果：读取失败，已确认范围与写入范围已知。
        assert result.error is SegmentFailure.READ_FAILED
        assert result.processed_end == 4000
        assert result.write_extent_known is True
        # 观察的效果：目标文件恰好包含已确认字节。
        observed = target_path.read_bytes()
        assert len(observed) == result.processed_end
        assert observed == payload[:4000]

    async def test_sync_failure_preserves_completed_stage_facts(
            self, tmp_path: Path, monkeypatch) -> None:
        """同步边界：截断或同步失败不吞掉已完成阶段的实际效果。"""
        staging = tmp_path / "staging" / "deliveries"
        staging.mkdir(parents=True)
        target_path = staging / "7.bin"
        target_path.write_bytes(b"0123456789abcdefghij")
        ref = FileRef(
            file_id=7, purpose=FilePurpose.DELIVERY_COPY,
            relative_path="deliveries/7.bin", root=staging.parent,
        )
        roots = BoundDirectories(staging=staging.parent)

        # 截断失败：不能继续，文件保持观察到的原状。
        def failing_truncate(fd: int, length: int) -> None:
            raise OSError("injected truncate failure")

        monkeypatch.setattr(file_io, "_ftruncate", failing_truncate)
        blocked = file_io.prepare_target(ref, roots, 10)
        assert blocked.truncated is False
        assert blocked.can_continue is False
        assert blocked.error is not None
        assert len(target_path.read_bytes()) == 20
        monkeypatch.undo()

        # 截断成功：文件观察立即反映目标长度。
        prepared = file_io.prepare_target(ref, roots, 10)
        assert prepared.truncated is True
        assert prepared.can_continue is True
        assert prepared.error is None
        assert target_path.read_bytes() == b"0123456789"

        # 文件同步失败：目录同步不尝试，文件事实保留。
        def failing_fsync(fd: int) -> None:
            raise OSError("injected fsync failure")

        monkeypatch.setattr(file_io, "_fsync", failing_fsync)
        failed = file_io.sync_target(ref, roots)
        assert failed.file_synced is False
        assert failed.directory is DirectorySyncStage.NOT_ATTEMPTED
        assert failed.error is not None
        assert target_path.read_bytes() == b"0123456789"

        # 文件同步成功、目录同步失败：两个阶段分别表达。
        def failing_directory(path: Path):
            raise OSError("injected directory sync failure")

        monkeypatch.setattr(file_io, "_fsync", lambda fd: None)
        monkeypatch.setattr(file_io, "_DIRECTORY_SYNC_SUPPORTED", True)
        monkeypatch.setattr(file_io, "_sync_directory", failing_directory)
        synced = file_io.sync_target(ref, roots)
        assert synced.file_synced is True
        assert synced.directory is DirectorySyncStage.FAILED
        assert synced.error is not None
        assert target_path.read_bytes() == b"0123456789"

    async def test_publish_stages_match_observed_locations(
            self, tmp_path: Path, monkeypatch) -> None:
        """移动边界：发布的三种阶段与两目录实际观察一致。"""
        staging, ready, _, source = _handoff_dirs(tmp_path, b"payload")
        ref, roots = _staging_ref(staging)

        # 成功：文件互换位置，目录同步得到确认（或平台不支持）。
        result = await publish_file(
            ref, roots, HandoffDirectories(staging=staging, ready=ready),
            ReadyName("7.bin"),
        )
        assert result.stage is PublishStage.MOVED
        assert result.directory in (
            DirectorySyncStage.SYNCED, DirectorySyncStage.UNSUPPORTED)
        assert (ready / "7.bin").read_bytes() == b"payload"
        assert not source.exists()

        # 移动成功但目录同步失败：已移动事实保留，文件实际在 ready。
        # 模拟支持目录同步的平台注入同步失败。
        staging2, ready2, _, source2 = _handoff_dirs(tmp_path / "case2", b"second")
        ref2, roots2 = _staging_ref(staging2)

        def failing_directory(path: Path):
            raise OSError("injected directory sync failure")

        monkeypatch.setattr(handoff, "_DIRECTORY_SYNC_SUPPORTED", True)
        monkeypatch.setattr(handoff, "_sync_directory", failing_directory)
        result2 = await publish_file(
            ref2, roots2, HandoffDirectories(staging=staging2, ready=ready2),
            ReadyName("7.bin"),
        )
        assert result2.stage is PublishStage.MOVED
        assert result2.directory is DirectorySyncStage.FAILED
        assert (ready2 / "7.bin").read_bytes() == b"second"
        assert not source2.exists()
        monkeypatch.undo()

        # 同名目标不覆盖：未移动，两侧内容保持。
        staging3, ready3, _, source3 = _handoff_dirs(tmp_path / "case3", b"third")
        (ready3 / "7.bin").write_bytes(b"occupied")
        ref3, roots3 = _staging_ref(staging3)
        result3 = await publish_file(
            ref3, roots3, HandoffDirectories(staging=staging3, ready=ready3),
            ReadyName("7.bin"),
        )
        assert result3.stage is PublishStage.NOT_MOVED
        assert (ready3 / "7.bin").read_bytes() == b"occupied"
        assert source3.read_bytes() == b"third"

    async def test_withdraw_stages_match_observed_state(
            self, tmp_path: Path) -> None:
        """删除边界：撤回结果与 ready/processing 的实际观察一致。"""
        _, ready, processing, _ = _handoff_dirs(tmp_path, None)
        (ready / "7.bin").write_bytes(b"ready-copy")
        withdrawn = await withdraw_file(HandoffIdentity(ready, "7.bin"))
        assert withdrawn.stage is WithdrawStage.WITHDRAWN
        assert not (ready / "7.bin").exists()

        # 已领取进入 processing 的对象不属于 camctl，保持不动。
        (processing / "7.bin").write_bytes(b"claimed")
        absent = await withdraw_file(HandoffIdentity(ready, "7.bin"))
        assert absent.stage is WithdrawStage.NOT_PRESENT
        assert (processing / "7.bin").read_bytes() == b"claimed"


class TestCancellationBoundary:
    async def test_stop_between_chunks_confirms_written_bytes_only(
            self, tmp_path: Path) -> None:
        """停止在块间生效：保留已确认字节，不再推进新写入。"""
        payload = bytes(index % 251 for index in range(8000))
        session = await open_read(
            SourceFile("9", {}, 8000), 0, _TICKET,
            stream=_OffsetStream(payload),
            no_data_timeout_s=_NO_DATA_TIMEOUT_S,
        )
        target_path = tmp_path / "target.bin"
        target_path.touch()
        stop = threading.Event()
        target = _StoppingTarget(_TargetFile(target_path), stop, after_writes=1)
        result = transfer_segment(
            SegmentSpec("copy/9", 0, "copy.part", 0, 8000, 2000, stop),
            session, target,
        )
        assert result.error is SegmentFailure.STOPPED
        assert result.processed_end == 2000
        assert target_path.read_bytes() == payload[:2000]

        # 停止保持时再次进入：已确认末尾不再推进，本次无新写入。
        again = transfer_segment(
            SegmentSpec("copy/9", 0, "copy.part", 2000, 8000, 2000, stop),
            session, target,
        )
        assert again.error is SegmentFailure.STOPPED
        assert again.processed_end == 2000
        assert target_path.read_bytes() == payload[:2000]


class TestConsumerFileRules:
    async def test_unconfirmed_delivery_is_final_and_not_republished(
            self, tmp_path: Path) -> None:
        """普通交付：三处均无副本时终局失败，不安排自动重投。"""
        _, ready, _, _ = _handoff_dirs(tmp_path, None)
        facts = DeliveryFacts(
            delivery_id=7, status=3, prepared_size=7,
            prepared_sha256="a" * 64,
        )
        absent = DeliveryLocations(
            staging=LocationObservation(observed=True, present=False),
            ready=LocationObservation(observed=True, present=False),
            processing=LocationObservation(observed=True, present=False),
        )
        decision = decide_handoff(facts, absent)
        assert decision.outcome is DeliveryHandoffOutcome.UNCONFIRMED_FINAL
        assert decision.failure is not None
        assert decision.failure.code == "delivery_handoff_unconfirmed"
        assert decision.failure.details["delivery_id"] == "7"
        assert decision.republishes == 0

        # 对照：ready 中存在与准备记录一致的完整副本时补存本地事实。
        content = b"payload"
        (ready / "7.bin").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        matched = DeliveryFacts(
            delivery_id=7, status=3, prepared_size=len(content),
            prepared_sha256=digest,
        )
        present = DeliveryLocations(
            staging=LocationObservation(observed=True, present=False),
            ready=LocationObservation(
                observed=True, present=True, length=len(content),
                sha256=digest),
            processing=LocationObservation(observed=True, present=False),
        )
        confirmed = decide_handoff(matched, present)
        assert confirmed.outcome is DeliveryHandoffOutcome.DELIVERED_LOCALLY

    async def test_report_recovery_classifies_by_actual_locations(
            self, tmp_path: Path) -> None:
        """报告恢复按三处目录的实际观察分类，观察失败不当不存在。"""
        report = FrozenReport(
            report_id=3, boundary=None, from_wm=0, to_wm=10,
            format_version=1,
        )
        staging = tmp_path / "staging"
        ready = tmp_path / "ready"
        processing = tmp_path / "processing"
        for directory in (staging, ready, processing):
            directory.mkdir()

        # 三处均可靠为空：沿原身份重建。
        empty = ReportDirectories(staging=staging, ready=ready, processing=processing)
        assert recover_report_files(
            report, observe_report_locations(empty)
        ) is ReportFileDecision.NOT_PRESENT

        # ready 中已有该报告文件：保留不动。
        existing = ready / report_file_name(3, "b" * 64)
        existing.write_bytes(b"report")
        assert recover_report_files(
            report, observe_report_locations(empty)
        ) is ReportFileDecision.PRESENT_ELSEWHERE
        existing.unlink()

        # ready 绑定目录缺失是观察错误：分类为不可靠，绝不当不存在。
        broken = ReportDirectories(
            staging=staging, ready=tmp_path / "absent-ready",
            processing=processing,
        )
        assert recover_report_files(
            report, observe_report_locations(broken)
        ) is ReportFileDecision.UNRELIABLE

    async def test_log_copy_failure_stops_and_classifies(
            self, tmp_path: Path) -> None:
        """日志副本：发布失败停止后续步骤并保留分类，不依赖状态库。"""
        staging_root = tmp_path / "staging"
        ready_dir = tmp_path / "ready"
        logs_dir = staging_root / "logs"
        logs_dir.mkdir(parents=True)
        ready_dir.mkdir()
        trigger = LogRecord(level=LogLevel.ERROR, message="报告失败")

        def request_for(case: str) -> tuple[CopyRequest, _CopyChannelDouble]:
            marker = MarkerStore(logs_dir / case)
            marker.directory.mkdir(parents=True, exist_ok=True)
            channel = _CopyChannelDouble()
            return CopyRequest(
                trigger=trigger, staging_root=staging_root,
                ready_dir=ready_dir, channel=channel, marker=marker,
            ), channel

        request, channel = request_for("ok")
        published = await deliver_failure_copy(
            request, publish=_publish_by_move)
        assert published.outcome is CopyOutcome.PUBLISHED
        assert channel.copied_into is not None
        assert not channel.copied_into.exists()  # 已发布离开准备位置

        # 发布未移动：副本失败，本次准备位置的副本被清理。
        request, channel = request_for("not-moved")

        async def not_moved(source, directories, target):
            return PublishResult(
                stage=PublishStage.NOT_MOVED,
                directory=DirectorySyncStage.NOT_ATTEMPTED,
                source_removed=False, error="injected target occupied")

        failed = await deliver_failure_copy(request, publish=not_moved)
        assert failed.outcome is CopyOutcome.COPY_FAILED
        assert failed.error is not None
        assert not channel.copied_into.exists()

        # 移动结果未知：按未知保留，不清理也不宣称发布完成。
        request, channel = request_for("unknown")

        async def unknown_move(source, directories, target):
            return PublishResult(
                stage=PublishStage.UNKNOWN,
                directory=DirectorySyncStage.NOT_ATTEMPTED,
                source_removed=False, error="injected move unknown")

        result = await deliver_failure_copy(request, publish=unknown_move)
        assert result.outcome is CopyOutcome.COPY_UNKNOWN


class TestOwnershipAndBacklogMeasurement:
    async def test_file_ownership_registered_and_backlog_released(
            self) -> None:
        """未结束任务登记文件身份与修改资格，收场后不保留积压。"""
        executor = FileTaskExecutor(Supervisor())
        release = threading.Event()
        started = threading.Event()
        task_id = FileTaskId("contract-1")
        owner = create_autospec(ResponsibilityOwner, instance=True)

        def body(stop):
            started.set()
            release.wait(10)
            return "done"

        task = FileTask(task_id, 11, "segment", "copy", body)
        execution = asyncio.ensure_future(executor.run_file_task(task, owner))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, started.wait, 10)
        assert executor.unfinished_files() == (11,)
        assert executor.lease_of(task_id) is not None
        release.set()
        result = await execution
        assert result.value == "done"
        # 收场即释放：积压测量只反映未结束任务，不累计历史。
        assert executor.unfinished_files() == ()
        assert executor.lease_of(task_id) is None
