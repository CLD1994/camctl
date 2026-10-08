"""单次非阻塞管道写入；结果只描述本地传输事实。"""
from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from camctl.contracts.values import ObjectId, format_object_id
from camctl.motor.models import position_value


class WriteKind(Enum):
    UNAVAILABLE = "unavailable"
    WRITTEN = "written"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True)
class NotificationWriteResult:
    kind: WriteKind
    written_bytes: int = 0
    reason: str | None = None
    errno: int | None = None


def encode_motor_notification(action_id: ObjectId, position: int) -> bytes:
    """返回规范整数编码、以 LF 结束的一条独立通知。"""
    message = {"type": "motor_control",
               "action_instance_id": format_object_id(action_id),
               "params": {"position": position_value(position)}}
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


class NotificationWriter:
    """由单一 run 流程串行调用，持有一个已配置的管道写端。

    管道容量不足不等待。每次 send 只调用一次 write，恢复不调用
    本发送器重试旧动作。初始化由 open_notification_writer 完成。
    """

    def __init__(self, fd: int | None, *, pipe_buf: int = 0,
                 write: Callable = os.write, close: Callable = os.close,
                 unavailable_reason: str = "not_connected",
                 error_number: int | None = None):
        self._fd = fd
        self._pipe_buf = pipe_buf
        self._write = write
        self._close = close
        self._unavailable_reason = unavailable_reason
        self._error_number = error_number

    @property
    def available(self) -> bool:
        return self._fd is not None

    @property
    def unavailability(self) -> NotificationWriteResult:
        if self.available:
            raise ValueError("有效通知管道没有不可用结论")
        return NotificationWriteResult(
            WriteKind.UNAVAILABLE, reason=self._unavailable_reason,
            errno=self._error_number)

    def send(self, message: bytes) -> NotificationWriteResult:
        if self._fd is None:
            return self.unavailability
        if len(message) > self._pipe_buf:
            return NotificationWriteResult(
                WriteKind.FAILED, reason="message_too_large")
        try:
            written = self._write(self._fd, message)
        except OSError as error:
            reason = ("would_block" if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK)
                      else "broken_pipe" if error.errno == errno.EPIPE
                      else "os_error")
            return NotificationWriteResult(
                WriteKind.FAILED, reason=reason, errno=error.errno)
        if written == len(message):
            return NotificationWriteResult(WriteKind.WRITTEN, written_bytes=written)
        self._unavailable_reason = "disabled_after_partial_write"
        self.close()
        return NotificationWriteResult(
            WriteKind.PARTIAL, written_bytes=written, reason="short_write")

    def close(self) -> None:
        """先撤销所有权，再关闭；失败不能使后续消息复用描述符。"""
        fd, self._fd = self._fd, None
        if fd is not None:
            self._close(fd)


def open_notification_writer(fd: int | None) -> NotificationWriter:
    """接管命令行传来的描述符，配置失败返回可诊断的不可用通道。

    标准流不是本协议的资源，不修改或关闭。合法通知 fd 自本函数
    接管起不再继承给 exec 子进程；报告监督方使用 spawn，受管工具
    使用 close_fds，不把此写端继续传递。
    """
    if fd is None:
        return NotificationWriter(None)
    if isinstance(fd, bool) or not isinstance(fd, int) or not 3 <= fd <= 2147483647:
        return NotificationWriter(None, unavailable_reason="invalid_descriptor")
    try:
        import fcntl

        os.set_inheritable(fd, False)
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "通知描述符必须是管道")
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if flags & os.O_ACCMODE == os.O_RDONLY:
            raise OSError(errno.EBADF, "通知描述符必须可写")
        pipe_buf = os.fpathconf(fd, "PC_PIPE_BUF")
        if pipe_buf <= 0:
            raise OSError(errno.EINVAL, "管道原子写入上限无效")
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except (ImportError, OSError) as error:
        number = error.errno if isinstance(error, OSError) else None
        try:
            os.close(fd)
        except OSError as close_error:
            # EBADF 表示原描述符已经失效；其他关闭错误不能吞掉。
            if close_error.errno != errno.EBADF:
                raise
        return NotificationWriter(None, unavailable_reason="invalid_descriptor",
                                  error_number=number)
    return NotificationWriter(fd, pipe_buf=pipe_buf)
