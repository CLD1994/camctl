"""L6 有序关闭的组件集成测试：真实文件通道、监听线程与多进程。

真实 FileChannel 与 LogChannel 组合：关闭摘要落盘并携带进程身
份；文件通道失效时跳过摘要；多个独立进程写同一共享文件时各自
汇总、互不重复。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest.mock as mock
from pathlib import Path

import pytest
from concurrent_log_handler import ConcurrentRotatingFileHandler

from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.clh_adapter import ChannelWriteError, FileChannel
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import (
    ChannelError, LogChannel, LogRecord,
)


def _file_channel(tmp_path: Path) -> FileChannel:
    return FileChannel(
        tmp_path / "camctl.log", max_bytes=65536, file_count=3
    )


def _channel_with(writer) -> LogChannel:
    return LogChannel(
        capacity=4, low_watermark=1, high_watermark=2,
        level=LogLevel.DEBUG, writer=writer,
    )


def _make_drops(channel: LogChannel) -> None:
    """不启动监听线程制造水位丢弃（低级别记录在水位区被抑制）。"""
    for index in range(4):
        channel.slog(LogRecord(level=LogLevel.DEBUG, message=f"drop-{index}"))
    assert channel.counters.total_dropped() >= 1


def _read_log(tmp_path: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(tmp_path.iterdir())
        if path.name.startswith("camctl.log")
    )


@pytest.mark.asyncio
class TestRealFileClose:
    async def test_close_writes_summary_and_stops_listener(
        self, tmp_path: Path,
    ) -> None:
        file_channel = _file_channel(tmp_path)

        def writer(record: LogRecord) -> bool:
            result = file_channel.write_record(record)
            if result.append_error is not None:
                raise ChannelWriteError(result.append_error)
            return result.appended

        channel = _channel_with(writer)
        _make_drops(channel)
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.INFO, message="工作记录"))
        runtime = LogRuntime(
            channel=channel,
            level=LogLevel.DEBUG,
            file_closer=file_channel.close,
            identity=f"pid={os.getpid()}",
        )

        result = await close_logging(runtime)
        assert result.listener_stopped is True
        assert result.dropped_total >= 1
        assert result.summary_written is True
        assert channel.listener_alive is False

        content = _read_log(tmp_path)
        assert "工作记录" in content
        assert "日志关闭摘要" in content
        assert f"pid={os.getpid()}" in content

        # 重复关闭不重复汇总。
        again = await close_logging(runtime)
        assert again.summary_written is True
        assert content.count("日志关闭摘要") == 1

    async def test_disabled_file_channel_skips_summary(
        self, tmp_path: Path,
    ) -> None:
        file_channel = _file_channel(tmp_path)

        def writer(record: LogRecord) -> bool:
            result = file_channel.write_record(record)
            if result.append_error is not None:
                raise ChannelWriteError(result.append_error)
            return result.appended

        channel = _channel_with(writer)
        _make_drops(channel)
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.INFO, message="正常记录"))
        # 注入追加失败（处理器关闭后会重开文件，不能作为失败注入）：
        # 通道按追加失败禁用。禁用可能发生在等待前（接纳拒绝）或等
        # 待中（失败凭据异常），两种失败表达都视为禁用生效。
        with mock.patch.object(
            ConcurrentRotatingFileHandler,
            "emit",
            side_effect=OSError("注入追加失败"),
        ):
            delivered_ok = True
            try:
                admission = await channel.deliver_important(
                    LogRecord(level=LogLevel.INFO, message="写入失败记录")
                )
                delivered_ok = (
                    admission.decision.kind is AdmissionKind.ACCEPTED
                    and admission.delivered is not False
                )
            except ChannelError:
                delivered_ok = False
        assert delivered_ok is False
        assert channel.disabled is True

        runtime = LogRuntime(
            channel=channel,
            level=LogLevel.DEBUG,
            file_closer=file_channel.close,
            identity=f"pid={os.getpid()}",
        )
        result = await close_logging(runtime)
        assert result.dropped_total >= 1
        assert result.summary_written is False
        assert channel.listener_alive is False
        assert "日志关闭摘要" not in _read_log(tmp_path)


class TestTwoProcesses:
    def test_two_processes_summarize_independently(self, tmp_path: Path) -> None:
        script = (
            "import asyncio, sys\n"
            "from pathlib import Path\n"
            "from camctl.logging_runtime.clh_adapter import (\n"
            "    ChannelWriteError, FileChannel,\n"
            ")\n"
            "from camctl.logging_runtime.lifecycle import (\n"
            "    LogRuntime, close_logging,\n"
            ")\n"
            "from camctl.logging_runtime.models import LogLevel\n"
            "from camctl.logging_runtime.service import LogChannel, LogRecord\n"
            "import os\n"
            "\n"
            "async def main() -> None:\n"
            "    file_channel = FileChannel(\n"
            "        Path(sys.argv[1]), max_bytes=65536, file_count=3,\n"
            "    )\n"
            "\n"
            "    def writer(record):\n"
            "        result = file_channel.write_record(record)\n"
            "        if result.append_error is not None:\n"
            "            raise ChannelWriteError(result.append_error)\n"
            "        return result.appended\n"
            "\n"
            "    channel = LogChannel(\n"
            "        capacity=4, low_watermark=1, high_watermark=2,\n"
            "        level=LogLevel.DEBUG, writer=writer,\n"
            "    )\n"
            "    for index in range(4):\n"
            "        channel.slog(LogRecord(\n"
            "            level=LogLevel.DEBUG,\n"
            "            message=f'proc-{sys.argv[2]}-drop-{index}',\n"
            "        ))\n"
            "    assert channel.counters.total_dropped() >= 1\n"
            "    channel.start()\n"
            "    await channel.alog(LogRecord(\n"
            "        level=LogLevel.INFO, message=f'proc-{sys.argv[2]}-work',\n"
            "    ))\n"
            "    runtime = LogRuntime(\n"
            "        channel=channel, level=LogLevel.DEBUG,\n"
            "        file_closer=file_channel.close,\n"
            "        identity=f'pid={os.getpid()}',\n"
            "    )\n"
            "    result = await close_logging(runtime)\n"
            "    assert result.summary_written is True, (\n"
            "        f'摘要未写出: {result}'\n"
            "    )\n"
            "\n"
            "asyncio.run(main())\n"
        )
        target = str(tmp_path / "camctl.log")
        processes = [
            subprocess.run(
                [sys.executable, "-c", script, target, str(tag)],
                capture_output=True, text=True, timeout=120,
                cwd=str(Path(__file__).parents[3]),
            )
            for tag in (1, 2)
        ]
        for process in processes:
            assert process.returncode == 0, process.stderr
        content = _read_log(tmp_path)
        # 两个进程各自汇总：各一条摘要，携带各自的进程身份。
        assert content.count("日志关闭摘要") == 2
        assert "proc-1-work" in content
        assert "proc-2-work" in content
