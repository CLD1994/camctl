"""L4/L6 多进程文件写入、轮换与有序关闭的组合测试。

真实文件与 CLH：追加与轮换分离、保留数量含活动文件、两个独立
进程写同一文件互不损坏；关闭摘要只写一次且不经队列。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from camctl.logging_runtime.clh_adapter import FileChannel, WriteResult
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord

pytestmark = pytest.mark.asyncio


def _record(message: str, level: LogLevel = LogLevel.INFO) -> LogRecord:
    return LogRecord(level=level, message=message)


class TestFileChannel:
    async def test_append_and_rotation_separated(self, tmp_path: Path) -> None:
        channel = FileChannel(
            tmp_path / "camctl.log", max_bytes=64, file_count=3
        )
        result = channel.write_record(_record("m" * 60))
        assert result.appended is True
        # 上一条已越过阈值：下一条触发轮换。
        second = channel.write_record(_record("second-line"))
        assert second.appended is True
        channel.close()
        files = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("camctl.log"))
        assert files, "应产生日志文件"

    async def test_rotation_failure_does_not_hide_append(self, tmp_path: Path) -> None:
        channel = FileChannel(
            tmp_path / "camctl.log", max_bytes=32, file_count=2
        )
        channel.write_record(_record("w" * 40))  # 先超过阈值，使下一条触发轮换
        # 注入轮换失败：追加结果不被掩盖。
        def broken_rollover():
            raise OSError("轮换失败")

        import unittest.mock as mock
        from concurrent_log_handler import ConcurrentRotatingFileHandler

        # 在追踪层之下注入轮换失败：追加结果不被掩盖。
        with mock.patch.object(
            ConcurrentRotatingFileHandler,
            "doRollover",
            side_effect=OSError("轮换失败"),
        ):
            result = channel.write_record(_record("x" * 40))
        assert result.appended is True
        assert result.rotation_error is not None
        assert "轮换失败" in result.rotation_error
        channel.close()

    async def test_retention_keeps_active_plus_backups(self, tmp_path: Path) -> None:
        channel = FileChannel(
            tmp_path / "camctl.log", max_bytes=32, file_count=2
        )
        for index in range(12):
            result = channel.write_record(_record(f"line-{index:03d}"))
            assert result.appended is True
        channel.close()
        log_files = [p for p in tmp_path.iterdir() if p.name.startswith("camctl.log")]
        # 保留总量 2 = 活动文件 + 1 份归档。
        assert len(log_files) <= 2


class TestOrderedClose:
    async def test_close_summary_is_once(self, tmp_path: Path) -> None:
        written: list[str] = []

        def writer(record: LogRecord) -> bool:
            written.append(record.message)
            return True

        channel = LogChannel(
            capacity=8, low_watermark=2, high_watermark=4,
            level=LogLevel.DEBUG, writer=writer,
        )
        # 不启动监听：水位随生产上升制造确定性丢弃。
        for index in range(4):
            channel.slog(LogRecord(level=LogLevel.DEBUG, message=f"d{index}"))
            await channel.alog(LogRecord(level=LogLevel.DEBUG, message=f"a{index}"))
        assert channel.counters.total_dropped() >= 1
        channel.start()
        runtime = LogRuntime(channel=channel)
        first = await close_logging(runtime)
        assert first.dropped_total >= 1
        assert first.summary_written is True
        again = await close_logging(runtime)
        assert again.summary_written is True  # 不重复尝试
        summaries = [m for m in written if "日志关闭摘要" in m]
        assert len(summaries) == 1

    async def test_zero_drops_no_summary(self, tmp_path: Path) -> None:
        written: list[str] = []

        def writer(record: LogRecord) -> bool:
            written.append(record.message)
            return True

        channel = LogChannel(
            capacity=8, low_watermark=8, high_watermark=9,
            level=LogLevel.DEBUG, writer=writer,
        )
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.ERROR, message="important"))
        runtime = LogRuntime(channel=channel)
        result = await close_logging(runtime)
        assert result.dropped_total == 0
        assert result.summary_written is False
        assert all("摘要" not in m for m in written)


class TestTwoProcesses:
    def test_two_processes_share_file(self, tmp_path: Path) -> None:
        script = (
            "from camctl.logging_runtime.clh_adapter import FileChannel\n"
            "from camctl.logging_runtime.models import LogLevel\n"
            "from camctl.logging_runtime.service import LogRecord\n"
            "import sys\n"
            "channel = FileChannel(sys.argv[1], max_bytes=256, file_count=3)\n"
            "for index in range(30):\n"
            "    result = channel.write_record(LogRecord(level=LogLevel.INFO,"
            " message=f'proc-{sys.argv[2]}-line-{index:03d}'))\n"
            "    assert result.appended\n"
            "channel.close()\n"
        )
        target = tmp_path / "camctl.log"
        processes = [
            subprocess.run(
                [sys.executable, "-c", script, str(target), str(tag)],
                capture_output=True, text=True, timeout=60, cwd=str(Path(__file__).parents[3]),
            )
            for tag in (1, 2)
        ]
        for process in processes:
            assert process.returncode == 0, process.stderr
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(tmp_path.iterdir())
            if path.name.startswith("camctl.log")
        )
        # 轮换保留有限：两进程的近期行都可靠落盘。
        assert "proc-1-line-02" in combined
        assert "proc-2-line-02" in combined
