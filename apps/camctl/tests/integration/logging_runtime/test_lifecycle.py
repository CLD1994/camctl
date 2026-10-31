"""L6 有序关闭的组件集成测试：真实文件通道、监听线程与多进程。

真实 FileChannel 与 LogChannel 组合：关闭摘要落盘并携带进程身
份；文件通道失效时跳过摘要；多个独立进程写同一共享文件时各自
汇总、互不重复。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import unittest.mock as mock
from pathlib import Path

import pytest
from concurrent_log_handler import ConcurrentRotatingFileHandler

from camctl.logging_runtime.admission import AdmissionKind
from camctl.logging_runtime.clh_adapter import ChannelWriteError, FileChannel
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel, LogWriteResult
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
@pytest.mark.parametrize("appended", [False, True])
async def test_summary_keeps_write_fact_when_channel_becomes_unsafe(appended):
    def writer(record):
        if record.level is LogLevel.WARNING:
            runtime.path_unsafe = True
            channel.disable_channel(ChannelError("摘要写入后的文件关闭失败"))
            return LogWriteResult(appended=appended)
        return LogWriteResult(appended=True)

    channel = _channel_with(writer)
    runtime = LogRuntime(channel, level=LogLevel.DEBUG)
    _make_drops(channel)
    channel.start()
    result = await close_logging(runtime)

    assert runtime.path_unsafe is True
    assert result.listener_stopped is True
    assert channel.listener_alive is False
    assert channel.summary_written is appended
    assert result.summary_written is appended
    assert runtime.summary_written is appended
    assert (await close_logging(runtime)).summary_written is appended


@pytest.mark.asyncio
class TestRealFileClose:
    @pytest.mark.parametrize("path_unsafe", [False, True])
    @pytest.mark.parametrize("later_control", [False, True])
    @pytest.mark.parametrize("blocked_stage", ["stop", "file", "wait"])
    async def test_parent_cancel_waits_for_complete_log_close(
        self, tmp_path: Path, monkeypatch, path_unsafe: bool, later_control: bool,
        blocked_stage: str,
    ) -> None:
        file_channel = _file_channel(tmp_path)
        channel = _channel_with(lambda record: file_channel.write_record(record).appended)
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.ERROR, message="关闭前记录"))
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        parent_errors: list[asyncio.CancelledError] = []
        close_error = (asyncio.CancelledError("later file close control")
                       if later_control else OSError("later file close error"))
        real_stop = channel.stop_listener
        real_close = channel.async_close
        real_wait = channel.await_closed
        real_shield = asyncio.shield

        def block_stage(stage):
            if blocked_stage == stage:
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("test did not release log close")

        def stop_once(*args, **kwargs):
            calls.append("stop")
            block_stage("stop")
            return real_stop(*args, **kwargs)

        def close_file():
            calls.append("file")
            block_stage("file")
            file_channel.close()
            raise close_error

        def close_channel():
            calls.append("channel")
            real_close()

        async def wait_channel():
            calls.append("wait")
            if blocked_stage == "wait":
                entered.set()
                assert await asyncio.to_thread(release.wait, 10)
            await real_wait()

        async def observe_parent_cancel(task):
            try:
                return await real_shield(task)
            except asyncio.CancelledError as error:
                parent_errors.append(error)
                raise

        channel.stop_listener = stop_once
        channel.async_close = close_channel
        channel.await_closed = wait_channel
        monkeypatch.setattr(asyncio, "shield", observe_parent_cancel)
        runtime = LogRuntime(channel=channel, file_closer=close_file,
                             path_unsafe=path_unsafe)
        closing = asyncio.create_task(close_logging(runtime))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            closing.cancel("first close wait cancel")
            await asyncio.sleep(0)
            closing.cancel("later close wait cancel")
            await asyncio.sleep(0)
            pending_before_release = not closing.done()
            calls_before_release = list(calls)
            release.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await closing
            assert pending_before_release is True
            expected_calls = {"stop": ["stop"], "file": ["stop", "file"],
                              "wait": ["stop", "file", "channel", "wait"]}
            assert calls_before_release == expected_calls[blocked_stage]
            assert parent_errors
            if blocked_stage == "wait" and later_control:
                assert raised.value is close_error
                assert "first close wait cancel" in "\n".join(raised.value.__notes__)
            else:
                assert raised.value is parent_errors[0]
                assert raised.value.args == ("first close wait cancel",)
                assert str(close_error) in "\n".join(raised.value.__notes__)
            assert calls == ["stop", "file", "channel", "wait"]
            assert runtime.close_attempts == 1
            assert runtime.listener_stopped is True
            assert channel.listener_alive is False
            assert file_channel.closed is True
            with pytest.raises(asyncio.CancelledError) as repeated:
                await close_logging(runtime)
            assert repeated.value is raised.value
            assert calls == ["stop", "file", "channel", "wait"]
        finally:
            release.set()
            if not closing.done():
                await asyncio.gather(closing, return_exceptions=True)
            await asyncio.to_thread(real_stop)
            file_channel.close()
            real_close()
            await real_wait()

    @pytest.mark.parametrize("path_unsafe", [False, True])
    @pytest.mark.parametrize("control_error", [False, True])
    async def test_listener_stop_error_does_not_skip_queue_close(
        self, tmp_path: Path, path_unsafe: bool, control_error: bool,
    ) -> None:
        file_channel = _file_channel(tmp_path)
        channel = _channel_with(lambda record: file_channel.write_record(record).appended)
        channel.start()
        calls: list[str] = []
        failure = (asyncio.CancelledError("listener stop control") if control_error
                   else OSError("listener stop error"))
        real_stop = channel.stop_listener
        real_close = channel.async_close
        real_wait = channel.await_closed

        def fail_after_real_stop(*args, **kwargs):
            calls.append("stop")
            real_stop(*args, **kwargs)
            raise failure

        def close_file():
            calls.append("file")
            file_channel.close()

        def close_channel():
            calls.append("channel")
            real_close()

        async def wait_channel():
            calls.append("wait")
            await real_wait()

        channel.stop_listener = fail_after_real_stop
        channel.async_close = close_channel
        channel.await_closed = wait_channel
        runtime = LogRuntime(channel=channel, file_closer=close_file,
                             path_unsafe=path_unsafe)
        try:
            if control_error:
                with pytest.raises(asyncio.CancelledError) as raised:
                    await close_logging(runtime)
                assert raised.value is failure
            else:
                result = await close_logging(runtime)
                assert str(failure) in result.close_error
            assert runtime.listener_stopped is False
            assert calls == (["stop", "channel", "wait"] if path_unsafe else
                             ["stop", "file", "channel", "wait"])
            assert channel.listener_alive is False
        finally:
            file_channel.close()
            real_close()
            await real_wait()

    async def test_unsafe_close_waits_for_active_listener_write(self) -> None:
        import asyncio

        started = threading.Event()
        release = threading.Event()
        closed: list[bool] = []

        def writer(record: LogRecord) -> bool:
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test did not release listener write")
            return True

        channel = _channel_with(writer)
        channel.start()
        await channel.alog(LogRecord(level=LogLevel.ERROR, message="active write"))
        assert await asyncio.to_thread(started.wait, 5)
        runtime = LogRuntime(
            channel=channel,
            path_unsafe=True,
            file_closer=lambda: closed.append(True),
        )
        closing = asyncio.create_task(close_logging(runtime))
        waited_while_active = False
        try:
            done, _ = await asyncio.wait((closing,), timeout=0.25)
            waited_while_active = not done
        finally:
            release.set()
        result = await asyncio.wait_for(closing, timeout=5)

        assert waited_while_active is True
        assert result.listener_stopped is True
        assert channel.listener_alive is False
        assert closed == [True]

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
                # 重要交付用 WARNING：按接纳规则始终保留，不经水位区
                # 的 INFO 采样，注入结果只取决于追加失败本身。
                admission = await channel.deliver_important(
                    LogRecord(level=LogLevel.WARNING, message="写入失败记录")
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
