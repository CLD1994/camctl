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
from pathlib import PurePosixPath
import threading
from typing import Any

from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryCursor, DirectoryRead, DirectoryRequest
from camctl.devices.bindings import DeviceBinding
from camctl.devices.file_identity import FileIdentity
from camctl.devices.read_session import ReadSession, SourceFile
from camctl.operations.models import AttemptTicket, ErrorValue
from camctl.operations.process import RawToolOutcome, ToolSpec, ToolStartError
from .transport import shell_argv


METADATA_FRAME_HEADER = b"CAMCTL-METADATA/1\0"
MAX_METADATA_BATCH_SIZE = 128



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


@dataclass(frozen=True)
class FileMetadataItem:
    """一个批量文件元数据请求的身份、实际大小或逐路径错误。"""

    identity: FileIdentity
    value: int | None
    error: ErrorValue | None


@dataclass(frozen=True)
class FileMetadataBatch:
    """一次批量工具调用及其按请求顺序解码的完整记录前缀。"""

    outcome: RawToolOutcome | None
    items: tuple[FileMetadataItem, ...]
    error: ErrorValue | None


def _metadata_paths(paths):
    if (not isinstance(paths, tuple) or not 1 <= len(paths) <= MAX_METADATA_BATCH_SIZE
            or any(not isinstance(path, str) or not path or "\0" in path for path in paths)):
        raise ValueError("批量文件元数据请求必须包含 1 到 128 个非空路径")
    try:
        for path in paths:
            path.encode("utf-8", "strict")
    except UnicodeError as error:
        raise ValueError("文件路径必须是有效 UTF-8") from error
    if len(set(paths)) != len(paths):
        raise ValueError("批量文件元数据请求不能重复文件身份")
    return paths


def _nul_field(data, offset):
    end = data.find(b"\0", offset)
    return None if end < 0 else (data[offset:end], end + 1)


def _metadata_item_error(identity, reason, remote_exit_code, stdout, stderr):
    return ErrorValue("adb_metadata_failed", "device_file_access", {
        "reason": reason,
        "identity": identity.path,
        "remote_exit_code": remote_exit_code,
        "stdout_hex": stdout.hex(),
        "stderr_hex": stderr.hex(),
    })


def _decode_metadata_frame(output, identities):
    """只返回身份顺序完全匹配且四字段均闭合的记录前缀。"""
    items = []
    frame_errors = []
    if not isinstance(output, bytes) or not output.startswith(METADATA_FRAME_HEADER):
        frame_errors.append({"reason": "invalid_header"})
        return tuple(items), tuple(frame_errors), False

    offset = len(METADATA_FRAME_HEADER)
    fields = ("identity", "remote_exit_code", "stdout", "stderr")
    for index, identity in enumerate(identities):
        record = []
        for field in fields:
            value = _nul_field(output, offset)
            if value is None:
                frame_errors.append({"reason": "truncated_record", "index": index,
                    "identity": identity.path, "field": field})
                return tuple(items), tuple(frame_errors), False
            field_value, offset = value
            record.append(field_value)

        actual_identity, status_bytes, stdout, stderr = record
        expected_identity = identity.path.encode("utf-8")
        if actual_identity != expected_identity:
            frame_errors.append({"reason": "identity_mismatch", "index": index,
                "expected_identity": identity.path, "actual_identity_hex": actual_identity.hex()})
            return tuple(items), tuple(frame_errors), False
        if re.fullmatch(rb"(?:0|[1-9][0-9]{0,2})", status_bytes) is None:
            frame_errors.append({"reason": "invalid_remote_exit_code", "index": index,
                "identity": identity.path, "remote_exit_code_hex": status_bytes.hex()})
            return tuple(items), tuple(frame_errors), False
        remote_exit_code = int(status_bytes)
        if remote_exit_code > 255:
            frame_errors.append({"reason": "invalid_remote_exit_code", "index": index,
                "identity": identity.path, "remote_exit_code_hex": status_bytes.hex()})
            return tuple(items), tuple(frame_errors), False

        if remote_exit_code != 0:
            error = _metadata_item_error(identity, "remote_command_failed", remote_exit_code,
                                         stdout, stderr)
            items.append(FileMetadataItem(identity, None, error))
            continue

        if re.fullmatch(rb"(?:0|[1-9][0-9]*)\n", stdout) is None:
            error = _metadata_item_error(identity, "invalid_size", remote_exit_code, stdout, stderr)
            items.append(FileMetadataItem(identity, None, error))
            continue
        try:
            size = int(stdout[:-1])
        except ValueError:
            error = _metadata_item_error(identity, "invalid_size", remote_exit_code, stdout, stderr)
            items.append(FileMetadataItem(identity, None, error))
            continue
        items.append(FileMetadataItem(identity, size, None))

    end = _nul_field(output, offset)
    if end is None:
        frame_errors.append({"reason": "missing_end", "records_completed": len(items)})
        return tuple(items), tuple(frame_errors), False
    end_value, offset = end
    if end_value != b"END":
        frame_errors.append({"reason": "unexpected_entry_after_requested_items",
            "value_hex": end_value.hex()})
        return tuple(items), tuple(frame_errors), False
    if offset != len(output):
        frame_errors.append({"reason": "trailing_data_after_end", "trailing_bytes": len(output) - offset})
        return tuple(items), tuple(frame_errors), False
    return tuple(items), (), True


def _metadata_batch_error(raw, *, frame_errors=(), item_errors=()):
    if not _returned(raw):
        primary = _error(raw, "metadata", "call_failed")
    elif frame_errors:
        primary = _error(raw, "metadata", "invalid_response_frame")
    else:
        primary = _error(raw, "metadata", "item_failures")
    details = dict(primary.details)
    if frame_errors:
        details["frame_errors"] = list(frame_errors)
    if item_errors:
        details["item_errors"] = [dict(item.details) for item in item_errors]
    return ErrorValue(primary.code, primary.stage, details)


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
        return '\n'.join((
            'tmp=$(mktemp) || exit $?',
            'trap \'rm -f -- "$tmp" "$tmp.sorted"\' 0',
            f'find {shlex.quote(root)} -type f -print0 > "$tmp" || exit $?',
            f'after={shlex.quote(after or "")}',
            # 将游标一起排序，即使原文件已不存在，也能确定严格晚于游标的记录。
            'seen_after=1',
            'if [ -n "$after" ]; then',
            '    seen_after=0',
            '    printf \'%s\\000\' "$after" >> "$tmp" || exit $?',
            'fi',
            'LC_ALL=C sort -z -- "$tmp" > "$tmp.sorted" || exit $?',
            # 空记录是私有结束标记；读取失败或意外 EOF 不能证明目录已读完。
            'printf \'\\000\' >> "$tmp.sorted" || exit $?',
            "printf 'CAMCTL-DIRECTORY/1\\000' || exit $?",
            'count=0',
            'while :; do',
            "    IFS= read -r -d '' path || exit $?",
            '    if [ -z "$path" ]; then',
            '        [ "$seen_after" -eq 1 ] || exit 1',
            "        printf 'END\\000' || exit $?",
            '        break',
            '    fi',
            '    if [ "$path" = "$after" ]; then',
            '        seen_after=1',
            '        continue',
            '    fi',
            '    [ "$seen_after" -eq 1 ] || continue',
            f'    if [ "$count" -ge {batch} ]; then',
            "        printf 'MORE\\000' || exit $?",
            '        break',
            '    fi',
            '    printf \'%s\\000\' "$path" || exit $?',
            '    count=$((count + 1))',
            'done < "$tmp.sorted"',
        ))

    def file_presence_script(self, path, *, delete=False):
        """单次精确删除及完整目录观察；输出大小不随目录条目数增长。"""
        # find 同时列出非文件，不能因类型过滤漏掉同名目录或失效链接。
        # 私有空记录携带 find 本身的退出码，不采用管道末段推断扫描成功。
        parent = str(PurePosixPath(path).parent)
        scan_root = parent if parent == "/" else parent + "/"
        return "\n".join((
            f'target={shlex.quote(path)}',
            f'parent={shlex.quote(parent)}',
            f'scan_root={shlex.quote(scan_root)}',
            'rm_status=-',
            *(('rm -- "$target"', 'rm_status=$?') if delete else ()),
            '(',
            '    scan_status=1',
            '    if [ -d "$parent" ] && [ -r "$parent" ] && [ -x "$parent" ]; then',
            '        find "$scan_root" -print0',
            '        scan_status=$?',
            '    fi',
            "    printf '\\000%s\\000' \"$scan_status\"",
            ') | (',
            '    state=0',
            '    count=0',
            '    roots=0',
            '    invalid=0',
            '    while :; do',
            "        IFS= read -r -d '' entry || exit 1",
            '        [ -n "$entry" ] || break',
            '        if [ "$entry" = "$parent" ] || [ "$entry" = "$scan_root" ]; then',
            '            roots=$((roots + 1))',
            '            continue',
            '        fi',
            '        case "$entry" in',
            '            "$scan_root"*) relative=${entry#"$scan_root"};;',
            '            *) invalid=1; continue;;',
            '        esac',
            '        case "/$relative/" in *"/../"*|*"/./"*|*"//"*) invalid=1; continue;; esac',
            '        [ "$entry" = "$target" ] || continue',
            '        count=$((count + 1))',
            '        if [ "$count" -gt 1 ]; then',
            '            state=duplicate',
            '        elif [ -f "$target" ]; then',
            '            state=1',
            '        else',
            '            state=type',
            '        fi',
            '    done',
            "    IFS= read -r -d '' scan_status || exit 1",
            '    case "$scan_status" in \"\"|*[!0-9]*) exit 1;; esac',
            # 私有结束状态后必须是实际 EOF，残余字节不构成完整扫描。
            "    IFS= read -r -d '' extra && exit 1",
            '    [ -z "$extra" ] || exit 1',
            '    if [ "$scan_status" -ne 0 ] && [ "$state" = 0 ]; then state=unknown; fi',
            '    if [ "$roots" -gt 1 ] || [ "$invalid" -eq 1 ] ||',
            '        { [ "$scan_status" -eq 0 ] && [ "$roots" -ne 1 ]; }; then state=invalid; fi',
            "    printf 'CAMCTL-FILE/1\\000%s\\000%s\\000%s\\000%s\\000%s\\000END\\000' "
                + ("delete" if delete else "query") + ' "$target" "$state" "$rm_status" "$scan_status" || exit $?',
            '    [ "$scan_status" -eq 0 ] && [ "$state" != type ] && [ "$state" != duplicate ] && [ "$state" != invalid ]',
            ')',
        ))

    def metadata_script(self, path):
        path = shlex.quote(path)
        return f'test -f {path} && LC_ALL=C stat -c %s -- {path}'

    def metadata_many_script(self, paths: tuple[str, ...]) -> str:
        paths = _metadata_paths(paths)
        header = METADATA_FRAME_HEADER[:-1].decode("ascii")
        return "\n".join((
            'tmp=$(mktemp) || exit $?',
            'trap \'rm -f -- "$tmp" "$tmp.err"\' 0',
            f"printf '%s\\000' {shlex.quote(header)} || exit $?",
            'for path in ' + shlex.join(paths) + '; do',
            '    : > "$tmp" || exit $?',
            '    : > "$tmp.err" || exit $?',
            '    { test -f "$path" && LC_ALL=C stat -c %s -- "$path"; } > "$tmp" 2> "$tmp.err"',
            '    status=$?',
            '    printf \'%s\\000%s\\000\' "$path" "$status" || exit $?',
            '    cat "$tmp" || exit $?',
            "    printf '\\000' || exit $?",
            '    cat "$tmp.err" || exit $?',
            "    printf '\\000' || exit $?",
            'done',
            "printf 'END\\000' || exit $?",
        ))

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

    async def metadata_many(
        self, identities: tuple[FileIdentity, ...], *, timeout_s, stop,
    ) -> FileMetadataBatch:
        if not isinstance(identities, tuple) or not 1 <= len(identities) <= MAX_METADATA_BATCH_SIZE:
            raise ValueError("批量文件元数据请求必须包含 1 到 128 个身份")
        paths = tuple(self._identity(identity) for identity in identities)
        _metadata_paths(paths)
        try:
            raw = await self._run(self.tools.metadata_many_script(paths), timeout_s, stop)
        except ToolStartError as error:
            return FileMetadataBatch(None, (), _error(None, "metadata", str(error)))

        items, frame_errors, frame_complete = _decode_metadata_frame(raw.output, identities)
        if raw.output_failure is not None:
            frame_errors = (*frame_errors, {"reason": "output_incomplete",
                "output_failure": raw.output_failure})
            frame_complete = False
        item_errors = tuple(item.error for item in items if item.error is not None)
        error = None
        if not _returned(raw) or not frame_complete or frame_errors or item_errors:
            error = _metadata_batch_error(raw, frame_errors=frame_errors, item_errors=item_errors)
        return FileMetadataBatch(raw, items, error)

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
                or ticket.operation != "read"):
            raise ValueError("读取必须使用原源文件及匹配的已提交尝试")
        # READ 票据目标是副本行号；源身份是原设备、驱动与稳定路径。
        # 二者的关联由保存 copy/source 的装配和仓储核对。
        identity = FileIdentity.from_source(source.file_id, source.locator)
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
