"""完整目录的分页输入和实际文件访问结果。

ShellFileTools 是候选工具组合。实际固件须核实工具选项、NUL 输出
和远端退出传递后，才能在正式驱动装配中使用。
"""
from dataclasses import dataclass, replace
from decimal import Decimal
import asyncio
import queue
import re
import shlex
import threading
from typing import Any

from camctl.contracts.pages import Page
from camctl.devices.bindings import DeviceBinding
from camctl.devices.file_identity import FileIdentity
from camctl.devices.read_session import ReadSession, SourceFile
from camctl.operations.models import AttemptTicket, ErrorValue
from camctl.operations.process import RawToolOutcome, ToolSpec, ToolStartError
from .transport import shell_argv


@dataclass(frozen=True)
class DirectoryCursor:
    binding: DeviceBinding
    directories: tuple[str, ...]
    directory_index: int
    after_path: str | None

    def __post_init__(self):
        roots = _roots(self.binding, self.directories)
        object.__setattr__(self, "directories", roots)
        index = self.directory_index
        if type(index) is not int or not 0 <= index < len(roots):
            raise ValueError("目录游标必须指向原范围中的目录")
        if self.after_path is not None:
            identity = FileIdentity(self.binding, self.after_path)
            if not identity.path.startswith(roots[index] + "/"):
                raise ValueError("目录游标不属于原目录")


@dataclass(frozen=True)
class DirectoryRequest:
    binding: DeviceBinding
    directories: tuple[str, ...]
    cursor: DirectoryCursor | None
    batch: int
    timeout_s: Decimal

    def __post_init__(self):
        roots = _roots(self.binding, self.directories)
        object.__setattr__(self, "directories", roots)
        if (type(self.batch) is not int or not 1 <= self.batch <= 128
                or not isinstance(self.timeout_s, Decimal)
                or not self.timeout_s.is_finite() or self.timeout_s <= 0):
            raise ValueError("目录批次必须为 1—128，期限必须为有限正秒数")
        if self.cursor is not None and (not isinstance(self.cursor, DirectoryCursor)
                or self.cursor.binding != self.binding or self.cursor.directories != roots):
            raise ValueError("目录游标不属于原绑定及完整范围")


@dataclass(frozen=True)
class DirectoryRead:
    outcome: RawToolOutcome | None
    error: ErrorValue | None
    page: Page[FileIdentity, DirectoryCursor] | None

    def __post_init__(self):
        if (self.error is None) == (self.page is None):
            raise ValueError("目录返回必须明确为可靠页或读取错误")


def _roots(binding, directories):
    if not isinstance(binding, DeviceBinding):
        raise ValueError("目录请求必须保留原设备绑定")
    roots = tuple(directories)
    if not roots:
        raise ValueError("完整目录范围不能为空")
    for root in roots:
        FileIdentity(binding, root)
    roots = tuple(sorted(roots, key=lambda root: root + "/"))
    if len(set(roots)) != len(roots) or any(
            a.startswith(b + "/") or b.startswith(a + "/")
            for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ValueError("完整目录范围不能重复或相互包含")
    return roots


def _error(raw, operation, reason):
    details: dict[str, Any] = {"reason": reason}
    if raw is not None:
        details.update(transport_error=raw.error, output_failure=raw.output_failure)
        if raw.exit is not None:
            details.update(local_exit_code=raw.exit.exit_code, local_signal=raw.exit.signal)
        if raw.stderr is not None:
            details["stderr"] = raw.stderr.decode("utf-8", "replace")
    return ErrorValue(f"adb_{operation}_failed", "device_file_access", details)


def _returned(raw):
    return (raw.error is None and raw.output_failure is None and raw.exit is not None
            and raw.exit.exit_code == 0 and isinstance(raw.output, bytes))


def decode_directory(request, raw):
    if not _returned(raw):
        return DirectoryRead(raw, _error(raw, "directory", "call_failed"), None)
    try:
        parts = raw.output.split(b"\0")
        if (len(parts) < 3 or parts[0] != b"CAMCTL-DIRECTORY/1"
                or parts[-1] != b"" or parts[-2] not in (b"END", b"MORE")):
            raise ValueError("目录响应缺少完整版本帧")
        entries = tuple(FileIdentity(request.binding, path.decode("utf-8", "strict"))
                        for path in parts[1:-2])
        index = 0 if request.cursor is None else request.cursor.directory_index
        after = None if request.cursor is None else request.cursor.after_path
        if (len(entries) > request.batch
                or any(not item.path.startswith(request.directories[index] + "/") for item in entries)
                or any(a.sort_key >= b.sort_key for a, b in zip(entries, entries[1:]))
                or (entries and after is not None and entries[0].path <= after)):
            raise ValueError("目录页超限、越界、重复或顺序不前进")
        if parts[-2] == b"MORE":
            if not entries:
                raise ValueError("同一目录的后续游标必须有实际进度")
            cursor = DirectoryCursor(request.binding, request.directories, index, entries[-1].path)
        else:
            cursor = (DirectoryCursor(request.binding, request.directories, index + 1, None)
                      if index + 1 < len(request.directories) else None)
        return DirectoryRead(raw, None, Page(entries, cursor))
    except (ValueError, UnicodeError) as error:
        return DirectoryRead(raw, _error(raw, "directory", str(error)), None)


class ShellFileTools:
    """候选 Linux 工具组合，真实选项和退出契约由设备验证确认。"""

    def directory_script(self, root, after, batch):
        # 各命令分别检查退出；不能从管道最后一段的成功推断 find 成功。
        select = ('BEGIN { RS="\\0"; ORS="\\0"; after=ENVIRON["CAMCTL_AFTER"]; '
                  'batch=ENVIRON["CAMCTL_BATCH"]+0 } '
                  '{ if (length($0) && (!length(after) || $0 > after)) { '
                  'if (n < batch) { print $0; n++ } else { more=1; exit } } } '
                  'END { print (more ? "MORE" : "END") }')
        return '\n'.join((
            'tmp=$(mktemp) || exit $?',
            'trap \'rm -f -- "$tmp" "$tmp.sorted"\' 0',
            f'find {shlex.quote(root)} -type f -print0 > "$tmp" || exit $?',
            'LC_ALL=C sort -z -- "$tmp" > "$tmp.sorted" || exit $?',
            "printf 'CAMCTL-DIRECTORY/1\\000'",
            f'CAMCTL_AFTER={shlex.quote(after or "")} CAMCTL_BATCH={batch} LC_ALL=C '
            f'awk {shlex.quote(select)} "$tmp.sorted"',
        ))

    def metadata_script(self, path):
        path = shlex.quote(path)
        return f'test -f {path} && LC_ALL=C stat -c %s -- {path}'

    def digest_script(self, path):
        return f'LC_ALL=C sha256sum < {shlex.quote(path)}'

    def delete_script(self, path):
        return f'rm -- {shlex.quote(path)}'

    def read_script(self, path, offset):
        # 只略过完整块，首块的字节偏移由读取端去除；不要求 dd 的扩展选项。
        return f'dd if={shlex.quote(path)} bs=65536 skip={offset // 65536}'


@dataclass(frozen=True)
class FileAccessRead:
    outcome: RawToolOutcome | None
    value: int | str | bool | None
    error: ErrorValue | None


class AdbFilesystem:
    def __init__(self, binding, serial, transport, *, tools, terminate_grace_s=Decimal("1")):
        if not isinstance(binding, DeviceBinding):
            raise ValueError("文件适配需要原设备绑定")
        shell_argv(serial, "true")
        self.binding, self.serial, self.transport = binding, serial, transport
        self.tools, self.terminate_grace_s = tools, terminate_grace_s

    async def read_directory(self, request, *, stop):
        if not isinstance(request, DirectoryRequest) or request.binding != self.binding:
            raise ValueError("目录请求不属于原驱动绑定")
        index = 0 if request.cursor is None else request.cursor.directory_index
        after = None if request.cursor is None else request.cursor.after_path
        script = self.tools.directory_script(request.directories[index], after, request.batch)
        try:
            raw = await self._run(script, request.timeout_s, stop)
        except ToolStartError as error:
            return DirectoryRead(None, _error(None, "directory", str(error)), None)
        return decode_directory(request, raw)

    def _identity(self, identity):
        if not isinstance(identity, FileIdentity) or identity.binding != self.binding:
            raise ValueError("文件请求不属于原设备绑定")
        return identity.path

    async def _run(self, script, timeout_s, stop):
        spec = ToolSpec(shell_argv(self.serial, script), timeout_s, self.terminate_grace_s)
        return await self.transport.run(spec, stop)

    async def _file_call(self, identity, operation, script_for, decode, timeout_s, stop):
        path = self._identity(identity)
        try:
            raw = await self._run(script_for(path), timeout_s, stop)
        except ToolStartError as error:
            return FileAccessRead(None, None, _error(None, operation, str(error)))
        if not _returned(raw):
            return FileAccessRead(raw, None, _error(raw, operation, "call_failed"))
        try:
            return FileAccessRead(raw, decode(raw.output), None)
        except ValueError as error:
            return FileAccessRead(raw, None, _error(raw, operation, str(error)))

    async def metadata(self, identity, *, timeout_s, stop):
        def decode(data):
            if re.fullmatch(rb"(?:0|[1-9][0-9]*)\n", data) is None:
                raise ValueError("文件长度响应无效")
            return int(data)
        return await self._file_call(identity, "metadata", self.tools.metadata_script, decode, timeout_s, stop)

    async def exists(self, identity, *, timeout_s, stop):
        result = await self.metadata(identity, timeout_s=timeout_s, stop=stop)
        # 长度零仍证明存在；stat 的失败不能证明不存在。
        return replace(result, value=True) if result.error is None else result

    async def source_digest(self, identity, *, timeout_s, stop):
        def decode(data):
            if re.fullmatch(rb"[0-9a-f]{64}  -\n", data) is None:
                raise ValueError("源摘要响应无效")
            return data[:64].decode("ascii")
        return await self._file_call(identity, "digest", self.tools.digest_script, decode, timeout_s, stop)

    async def delete_file(self, identity, *, timeout_s, stop):
        def decode(data):
            if data != b"":
                raise ValueError("指定文件删除返回了未登记的输出")
            return True
        return await self._file_call(identity, "delete", self.tools.delete_script, decode, timeout_s, stop)

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        if (not isinstance(source, SourceFile) or not isinstance(ticket, AttemptTicket)
                or ticket.operation != "read" or ticket.target_id != source.file_id):
            raise ValueError("读取必须使用原源文件及匹配的已提交尝试")
        identity = FileIdentity.from_json(dict(source.locator))
        path = self._identity(identity)
        if type(offset) is not int or not 0 <= offset <= source.size_bytes:
            raise ValueError("读取偏移不在原固定长度范围内")
        # 先校验会话输入，再启动唯一受管流，避免无效输入产生进程。
        stream = _AdbSourceStream(self, path, offset, source.size_bytes)
        session = ReadSession(source, offset, stream, idle_timeout_s)
        stream.start()
        return session


class _ReadStop:
    def __init__(self):
        self.event = asyncio.Event()

    async def requested(self):
        await self.event.wait()


class _AdbSourceStream:
    """一个读取尝试持有一个受管进程，文件字节经两块有界队列交付。"""

    def __init__(self, filesystem, path, offset, size):
        self.fs, self.path, self.offset = filesystem, path, offset
        self.remaining = size - offset
        self.skip = offset % 65536
        self.queue = queue.Queue(maxsize=2)
        self.buffer = b""
        self.stop = _ReadStop()
        self.cancelled = threading.Event()
        self.closed = False
        self.loop = asyncio.get_running_loop()
        self.future = None

    def start(self):
        self.future = asyncio.run_coroutine_threadsafe(self._run(), self.loop)

    async def _run(self):
        spec = ToolSpec(shell_argv(self.fs.serial, self.fs.tools.read_script(self.path, self.offset)),
                        None, self.fs.terminate_grace_s)
        return await self.fs.transport.run_stream(spec, self.stop, self._receive)

    async def _receive(self, data):
        skipped = min(self.skip, len(data))
        self.skip -= skipped
        data = data[skipped:][:self.remaining]
        self.remaining -= len(data)
        if not data:
            return
        while not self.cancelled.is_set():
            try:
                self.queue.put_nowait(data)
                return
            except queue.Full:
                await asyncio.sleep(0.001)

    def read(self, limit):
        if self.closed:
            raise OSError("源读取会话已经关闭")
        if self.cancelled.is_set():
            return b""
        if not self.buffer:
            try:
                self.buffer = self.queue.get_nowait()
            except queue.Empty:
                if self.future.done():
                    raw = self.future.result()
                    raise OSError(f"源读取在固定长度前结束: {_error(raw, 'read', 'unexpected_end').details}")
                return b""
        data, self.buffer = self.buffer[:limit], self.buffer[limit:]
        return data

    def cancel(self):
        self.cancelled.set()
        self.loop.call_soon_threadsafe(self.stop.event.set)

    def close(self):
        if self.closed:
            return
        if self.remaining or self.buffer or not self.queue.empty():
            self.cancel()
        raw = self.future.result()
        self.closed = True
        if (raw.exit is None or raw.output_failure is not None
                or (raw.error is not None and not (self.cancelled.is_set() and raw.error == "cancelled"))
                or (not self.cancelled.is_set() and raw.exit.exit_code != 0)):
            raise OSError(f"源读取实际收场失败: {_error(raw, 'read', 'call_failed').details}")
