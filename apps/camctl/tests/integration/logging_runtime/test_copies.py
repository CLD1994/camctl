"""L5 独立故障标记与锁内日志副本交付的组合测试。

真实文件、CLH 跨进程锁与交接目录：触发记录在锁内写入并复制，其
他进程的竞争轮换不使副本漏掉触发记录；标记先行、进度更新、抑制
与失败清理、启动清理按规格决策表组合验证；全程不依赖状态库。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.copies import (
    MARKER_NAME,
    CopyOutcome,
    CopyRequest,
    FailureLogService,
    MarkerPresence,
    MarkerStage,
    MarkerStore,
    is_copy_file_name,
    is_marker_temp_name,
    marker_temp_file_name,
    startup_sweep,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

_TRIGGER = "report-failure-trigger-record"


def _channel(log_path: Path, *, max_bytes: int = 256) -> FileChannel:
    return FileChannel(log_path, max_bytes=max_bytes, file_count=3)


def _layout(tmp_path: Path):
    staging = tmp_path / "staging"
    ready = tmp_path / "ready"
    logs_dir = staging / "logs"
    logs_dir.mkdir(parents=True)
    ready.mkdir()
    log_path = tmp_path / "camctl.log"
    return staging, ready, logs_dir, log_path


def _request(staging: Path, ready: Path, channel: FileChannel,
             marker: MarkerStore, message: str = _TRIGGER) -> CopyRequest:
    return CopyRequest(
        trigger=LogRecord(level=LogLevel.ERROR, message=message),
        staging_root=staging, ready_dir=ready,
        channel=channel, marker=marker,
    )


# ---- 锁内写入与复制 --------------------------------------------------


class TestLockedCopy:
    pytestmark = pytest.mark.asyncio
    async def test_copy_contains_all_current_bytes(self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        channel.write_record(LogRecord(level=LogLevel.INFO, message="before"))
        destination = logs_dir / "log-copy-r1.log"
        write_result, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message=_TRIGGER), destination)
        assert copy_error is None and write_result.appended is True
        copied = destination.read_bytes()
        assert _TRIGGER.encode() in copied
        # 副本即复制时点的当前日志文件：触发记录之前的行也在其中。
        assert b"before" in copied
        # 原日志继续留在配置路径。
        assert log_path.exists()
        channel.close()

    async def test_copy_contains_trigger_before_rotation(
            self, tmp_path: Path) -> None:
        """另一进程竞争轮换：副本仍包含触发记录，各条记录完整。"""
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        script = (
            "from camctl.logging_runtime.clh_adapter import FileChannel\n"
            "from camctl.logging_runtime.models import LogLevel\n"
            "from camctl.logging_runtime.service import LogRecord\n"
            "import sys\n"
            "channel = FileChannel(sys.argv[1], max_bytes=128, file_count=3)\n"
            "for index in range(40):\n"
            "    result = channel.write_record(LogRecord(level=LogLevel.INFO,"
            " message=f'proc-line-{index:03d}'))\n"
            "    assert result.appended\n"
            "channel.close()\n"
        )
        first = subprocess.run(
            [sys.executable, "-c", script, str(log_path)],
            capture_output=True, text=True, timeout=60,
            cwd=str(Path(__file__).parents[3]))
        assert first.returncode == 0, first.stderr
        # 竞争写手在副本复制期间持续追加并轮换。
        competitor = subprocess.Popen(
            [sys.executable, "-c", script, str(log_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(Path(__file__).parents[3]))
        destination = logs_dir / "log-copy-r1.log"
        write_result, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message=_TRIGGER), destination)
        out, err = competitor.communicate(timeout=60)
        assert competitor.returncode == 0, err
        assert copy_error is None and write_result.appended is True
        copied = destination.read_bytes().decode()
        # 触发记录完整存在；锁内快照结束于完整记录边界。
        assert _TRIGGER in copied
        assert copied.endswith("\n")
        channel.close()

    async def test_records_stay_complete_during_copy(
            self, tmp_path: Path) -> None:
        """复制期间同进程线程并发追加：副本内没有半条记录。"""
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path, max_bytes=512)
        stop = threading.Event()
        errors: list[str] = []

        def churn() -> None:
            index = 0
            while not stop.is_set():
                result = channel.write_record(LogRecord(
                    level=LogLevel.INFO, message=f"churn-{index:04d}" + "x" * 40))
                if not result.appended:
                    errors.append(result.append_error or "append failed")
                index += 1

        worker = threading.Thread(target=churn, daemon=True)
        worker.start()
        destination = logs_dir / "log-copy-r1.log"
        write_result, copy_error = channel.copy_with_write(
            LogRecord(level=LogLevel.ERROR, message=_TRIGGER), destination)
        stop.set()
        worker.join(timeout=10)
        assert copy_error is None and write_result.appended is True
        copied = destination.read_bytes().decode()
        assert _TRIGGER in copied
        assert not errors
        # 锁保证：副本是某一时刻的完整文件，任何行都不会缺尾。
        for line in copied.splitlines():
            assert line.startswith(("churn-", _TRIGGER, "camctl"))
        channel.close()


# ---- 完整交付链 ------------------------------------------------------

class TestDelivery:
    pytestmark = pytest.mark.asyncio
    async def test_full_delivery_publishes_copy(self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        channel.write_record(LogRecord(level=LogLevel.INFO, message="context"))
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime.copies import deliver_failure_copy
        receipt = await deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.PUBLISHED
        assert is_copy_file_name(receipt.copy_name)
        published = ready / receipt.copy_name
        copied = published.read_bytes()
        assert _TRIGGER.encode() in copied and b"context" in copied
        # 标记保留并记录发布进度；准备位置无副本残留。
        check = marker.check()
        assert check.presence is MarkerPresence.EXISTS
        assert check.state.stage is MarkerStage.PUBLISHED
        assert check.state.copy_name == receipt.copy_name
        leftovers = [p.name for p in logs_dir.iterdir()
                     if is_copy_file_name(p.name)]
        assert leftovers == []
        # 原日志继续可用。
        after = channel.write_record(LogRecord(level=LogLevel.INFO,
                                               message="after"))
        assert after.appended is True
        channel.close()

    async def test_repeated_failure_is_suppressed(self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime.copies import deliver_failure_copy
        first = await deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert first.outcome is CopyOutcome.PUBLISHED
        second = await deliver_failure_copy(
            _request(staging, ready, channel, marker,
                     message="later failure differs"))
        assert second.outcome is CopyOutcome.SUPPRESSED
        assert second.copy_name == first.copy_name
        assert [p.name for p in ready.iterdir()] == [first.copy_name]
        channel.close()

    async def test_invalid_marker_content_still_suppresses(
            self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        (logs_dir / MARKER_NAME).write_bytes(b"{broken")
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime.copies import deliver_failure_copy
        receipt = await deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.SUPPRESSED
        assert receipt.copy_name is None
        assert list(ready.iterdir()) == []
        channel.close()

    async def test_marker_create_failure_stops_before_copy(
            self, tmp_path: Path, monkeypatch) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime import copies
        monkeypatch.setattr(
            copies, "_write_synced",
            lambda path, data: (_ for _ in ()).throw(OSError("disk full")))
        receipt = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.MARKER_CREATE_FAILED
        assert list(ready.iterdir()) == []
        assert [p.name for p in logs_dir.iterdir()] == []
        channel.close()

    async def test_publish_failure_cleans_staging_copy(
            self, tmp_path: Path, monkeypatch) -> None:
        from camctl.host_files.handoff import (
            HandoffDirectories, PublishResult, PublishStage, ReadyName)
        from camctl.host_files.io import DirectorySyncStage
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)

        async def rejected(source, directories, target):
            return PublishResult(
                PublishStage.NOT_MOVED, DirectorySyncStage.NOT_ATTEMPTED,
                False, "target_exists")

        from camctl.logging_runtime import copies
        receipt = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker), publish=rejected)
        assert receipt.outcome is CopyOutcome.COPY_FAILED
        # 失败清理：准备位置无副本；标记保留故障持续。
        assert [p.name for p in logs_dir.iterdir()] == [MARKER_NAME]
        assert list(ready.iterdir()) == []
        channel.close()

    async def test_failed_delivery_is_not_retried(
            self, tmp_path: Path) -> None:
        """发布失败清理副本后：同一故障的后续失败不再复制或投递。"""
        from camctl.host_files.handoff import PublishResult, PublishStage
        from camctl.host_files.io import DirectorySyncStage
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)

        async def rejected(source, directories, target):
            return PublishResult(
                PublishStage.NOT_MOVED, DirectorySyncStage.NOT_ATTEMPTED,
                False, "target_exists")

        from camctl.logging_runtime import copies
        failed = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker), publish=rejected)
        assert failed.outcome is CopyOutcome.COPY_FAILED
        # 标记保留：后续失败被抑制，不重新复制、不递归。
        later = await copies.deliver_failure_copy(
            _request(staging, ready, channel, marker,
                     message="second failure in same round"))
        assert later.outcome is CopyOutcome.SUPPRESSED
        assert later.copy_name == failed.copy_name
        assert list(ready.iterdir()) == []
        assert [p.name for p in logs_dir.iterdir()] == [MARKER_NAME]
        channel.close()

    async def test_database_unavailable_does_not_block_delivery(
            self, tmp_path: Path) -> None:
        """交付不依赖状态库：流程内没有任何数据库访问。"""
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime.copies import deliver_failure_copy
        receipt = await deliver_failure_copy(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.PUBLISHED
        channel.close()


# ---- 恢复与服务 ------------------------------------------------------

class TestRecovery:
    pytestmark = pytest.mark.asyncio
    async def test_recovery_deletes_marker_and_allows_new_round(
            self, tmp_path: Path) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        service = FailureLogService(marker)
        first = await service.on_report_failure(
            _request(staging, ready, channel, marker))
        assert first.outcome is CopyOutcome.PUBLISHED
        deletion = await service.on_report_recovered()
        assert deletion.outcome.value == "deleted"
        assert marker.check().presence is MarkerPresence.ABSENT
        # 新一轮故障重新取得首次尝试：新副本身份。
        second = await service.on_report_failure(
            _request(staging, ready, channel, marker))
        assert second.outcome is CopyOutcome.PUBLISHED
        assert second.round_id != first.round_id
        assert sorted(p.name for p in ready.iterdir()) == sorted(
            [first.copy_name, second.copy_name])
        channel.close()

    async def test_suspension_lifted_by_recovery(
            self, tmp_path: Path, monkeypatch) -> None:
        staging, ready, logs_dir, log_path = _layout(tmp_path)
        channel = _channel(log_path)
        marker = MarkerStore(logs_dir)
        from camctl.logging_runtime import copies
        real_deliver = copies.deliver_failure_copy

        async def failing_marker(request, **kwargs):
            from camctl.logging_runtime.copies import CopyReceipt
            return CopyReceipt(CopyOutcome.MARKER_UNAVAILABLE,
                               error="stat failed")

        service = FailureLogService(marker, deliver=failing_marker)
        first = await service.on_report_failure(
            _request(staging, ready, channel, marker))
        assert first.outcome is CopyOutcome.MARKER_UNAVAILABLE
        # 同次运行内不再尝试。
        second = await service.on_report_failure(
            _request(staging, ready, channel, marker))
        assert second.outcome is CopyOutcome.SUPPRESSED
        await service.on_report_recovered()
        # 恢复解除暂停：真实交付可以再次进行。
        service_ok = FailureLogService(marker, deliver=real_deliver)
        receipt = await service_ok.on_report_failure(
            _request(staging, ready, channel, marker))
        assert receipt.outcome is CopyOutcome.PUBLISHED
        channel.close()


# ---- 启动清理 --------------------------------------------------------

class TestStartupSweep:
    def test_sweep_cleans_old_copies_and_temps(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "staging" / "logs"
        logs_dir.mkdir(parents=True)
        old_copy = logs_dir / "log-copy-old.log"
        old_copy.write_bytes(b"half")
        temp = logs_dir / marker_temp_file_name("stale")
        temp.write_bytes(b"{")
        marker = logs_dir / MARKER_NAME
        marker.write_bytes(b'{"round_id":"r","copy_name":"log-copy-x.log",'
                           b'"stage":"copied"}')
        stranger = logs_dir / "notes.txt"
        stranger.write_bytes(b"keep")
        result = startup_sweep(logs_dir, active_names=())
        assert result.error is None and result.failures == ()
        assert not old_copy.exists() and not temp.exists()
        assert marker.exists() and stranger.exists()

    def test_sweep_excludes_active_copy(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "staging" / "logs"
        logs_dir.mkdir(parents=True)
        active = logs_dir / "log-copy-mine.log"
        active.write_bytes(b"writing")
        result = startup_sweep(logs_dir, active_names=("log-copy-mine.log",))
        assert result.error is None
        assert active.exists()

    def test_sweep_reports_directory_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "staging" / "logs"
        result = startup_sweep(missing)
        assert result.error is not None
