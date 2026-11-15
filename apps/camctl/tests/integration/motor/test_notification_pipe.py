"""真实管道与子进程证明容量错误有界返回，通知 fd 不继续继承。"""
import asyncio
import errno
import os
import sys
from decimal import Decimal

import pytest

from camctl.contracts.values import ObjectId
from camctl.motor.notification import (
    WriteKind, encode_motor_notification, open_notification_writer,
)
from camctl.operations.process import ToolSpec, execute_tool

from .pipe_support import close_if_same, run_pipe_case

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 通知管道")


def test_real_pipe_normalized_lines_and_eof():
    run_pipe_case(_real_pipe_normalized_lines_and_eof)


def _real_pipe_normalized_lines_and_eof():
    read_fd, write_fd = os.pipe()
    writer = open_notification_writer(write_fd)
    try:
        assert writer.available
        assert not os.get_inheritable(write_fd)
        assert not os.get_blocking(write_fd)
        for position in [-2147483648, 0, 2147483647]:
            result = writer.send(encode_motor_notification(ObjectId(12), position))
            assert result.kind is WriteKind.WRITTEN
        writer.close()
        actual = os.read(read_fd, 4096)
        assert actual == (
            b'{"type":"motor_control","action_instance_id":"12","params":{"position":-2147483648}}\n'
            b'{"type":"motor_control","action_instance_id":"12","params":{"position":0}}\n'
            b'{"type":"motor_control","action_instance_id":"12","params":{"position":2147483647}}\n')
        assert os.read(read_fd, 1) == b""
    finally:
        writer.close()
        os.close(read_fd)


def test_full_pipe_returns_without_waiting_or_retry():
    run_pipe_case(_full_pipe_returns_without_waiting_or_retry)


def _full_pipe_returns_without_waiting_or_retry():
    read_fd, write_fd = os.pipe()
    writer = open_notification_writer(write_fd)
    try:
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        result = writer.send(encode_motor_notification(ObjectId(1), 1))
        assert result.kind is WriteKind.FAILED
        assert result.reason == "would_block"
        assert result.written_bytes == 0
        assert result.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
    finally:
        writer.close()
        os.close(read_fd)


def test_closed_reader_is_explicit_write_failure():
    read_fd, write_fd = os.pipe()
    writer = open_notification_writer(write_fd)
    os.close(read_fd)
    try:
        result = writer.send(encode_motor_notification(ObjectId(1), 1))
        assert result.kind is WriteKind.FAILED
        assert result.reason == "broken_pipe"
        assert result.errno == errno.EPIPE
    finally:
        writer.close()


def test_read_end_is_rejected_and_closed():
    read_fd, write_fd = os.pipe()
    try:
        writer = open_notification_writer(read_fd)
        assert not writer.available
        assert writer.send(b"x\n").reason == "invalid_descriptor"
        with pytest.raises(OSError) as error:
            os.fstat(read_fd)
        assert error.value.errno == errno.EBADF
    finally:
        os.close(write_fd)


def test_regular_file_is_not_a_notification_channel(tmp_path):
    fd = os.open(tmp_path / "file", os.O_CREAT | os.O_WRONLY, 0o600)
    identity = os.fstat(fd)
    try:
        writer = open_notification_writer(fd)
        assert not writer.available
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        close_if_same(fd, identity)


def test_invalid_descriptor_keeps_explicit_unavailable_reason():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    writer = open_notification_writer(write_fd)
    result = writer.send(b"x\n")
    assert result.kind is WriteKind.UNAVAILABLE
    assert result.reason == "invalid_descriptor"
    assert result.errno == errno.EBADF


def test_cli_early_configuration_failure_closes_notification_fd(tmp_path):
    from io import StringIO
    from camctl.cli import main

    config = tmp_path / "invalid.toml"
    config.write_text("[paths\n")
    read_fd, write_fd = os.pipe()
    identity = os.fstat(write_fd)
    out, err = StringIO(), StringIO()
    try:
        result = main(["run", "--host-notification-fd", str(write_fd),
                       "--config", str(config)], out, err)
        assert result == 1
        assert out.getvalue() == ""
        assert err.getvalue()
        os.set_blocking(read_fd, False)
        assert os.read(read_fd, 1) == b""
    finally:
        try:
            close_if_same(write_fd, identity)
        finally:
            os.close(read_fd)


def test_managed_tool_does_not_inherit_notification_writer():
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    writer = open_notification_writer(write_fd)
    # 子进程按 inode 验证管道身份，避免内部启动设施复用相同整数造成误判。
    identity = os.fstat(write_fd)
    script = f'''
import os
try:
    s = os.fstat({write_fd})
except OSError:
    raise SystemExit(0)
raise SystemExit(7 if (s.st_dev, s.st_ino) == ({identity.st_dev}, {identity.st_ino}) else 0)
'''
    try:
        class NoStop:
            async def requested(self):
                await asyncio.Future()

        result = asyncio.run(execute_tool(ToolSpec(
            argv=(sys.executable, "-c", script), timeout_s=Decimal(5),
            terminate_grace_s=Decimal(1)), stop=NoStop()))
        assert result.exit.exit_code == 0
    finally:
        writer.close()
        os.close(read_fd)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="通过 proc 核验实际报告进程 fd")
@pytest.mark.asyncio
async def test_report_process_does_not_inherit_notification_writer(tmp_path):
    import multiprocessing
    from camctl.reporting.maintenance import MaintenanceLimits
    from camctl.reporting.supervisor import WorkerSupervisor
    from pathlib import Path

    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    writer = open_notification_writer(write_fd)
    inode = os.fstat(write_fd).st_ino
    previous = {process.pid for process in multiprocessing.active_children()}
    supervisor = WorkerSupervisor(limits=MaintenanceLimits(5,300,5),lock_path=str(tmp_path/'report.lock'))
    processes = []
    try:
        startup = await supervisor.start()
        assert startup.ready, startup.detail
        processes = [process for process in multiprocessing.active_children() if process.pid not in previous]
        assert len(processes) == 1
        process, = processes
        assert process.is_alive()
        targets = []
        for fd in Path(f"/proc/{process.pid}/fd").iterdir():
            try:
                targets.append(os.readlink(fd))
            except FileNotFoundError:
                continue
        assert f"pipe:[{inode}]" not in targets
    finally:
        try:
            processes.extend(process for process in multiprocessing.active_children()
                             if process.pid not in previous and process not in processes)
            await supervisor.stop()
        finally:
            try:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(10)
                    if process.is_alive():
                        process.kill()
                        process.join(10)
                    assert not process.is_alive(), "报告进程未能退出"
            finally:
                try:
                    writer.close()
                finally:
                    os.close(read_fd)
