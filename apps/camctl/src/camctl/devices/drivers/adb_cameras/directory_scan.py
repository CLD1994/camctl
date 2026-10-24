"""当前结果尝试独占的完整目录清单和连续分页。"""
import asyncio
from dataclasses import replace
import tempfile
import time

from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryCursor, DirectoryRead, DirectoryRequest
from camctl.devices.file_identity import FileIdentity
from camctl.logging_runtime.performance import record_performance
from camctl.operations.process import OUTPUT_LIMIT_BYTES, ToolSpec

from .filesystem import _error, _returned
from .transport import shell_argv


_HEADER = b"CAMCTL-DIRECTORY/1"
_READ_SIZE = 65536


async def _file_work(function, *args):
    """取消等待后仍取得已提交文件操作的实际结果。"""
    actual = asyncio.create_task(asyncio.to_thread(function, *args))
    interrupted = None
    while True:
        try:
            result = await asyncio.shield(actual)
            break
        except asyncio.CancelledError as error:
            if actual.cancelled():
                raise
            interrupted = interrupted or error
        except BaseException as error:
            if interrupted is not None:
                raise interrupted from error
            raise
    if interrupted is not None:
        raise interrupted
    return result


class _DirectoryFrame:
    """流式核实 NUL 字段，只保留当前字段和前一路径。"""

    def __init__(self, binding, root):
        self.binding, self.root = binding, root
        self.field = bytearray()
        self.started = self.ended = False
        self.previous = None

    def receive(self, data):
        offset = 0
        while offset < len(data):
            if self.ended:
                raise ValueError("目录结束标记后仍有数据")
            end = data.find(b"\0", offset)
            limit = len(data) if end < 0 else end
            if len(self.field) + limit - offset > OUTPUT_LIMIT_BYTES:
                raise ValueError("目录字段超过工具输出界限")
            self.field.extend(data[offset:limit])
            if end < 0:
                return
            value = bytes(self.field)
            self.field.clear()
            self._field(value)
            offset = end + 1

    def _field(self, value):
        if not self.started:
            if value != _HEADER:
                raise ValueError("目录响应版本帧无效")
            self.started = True
        elif value == b"END":
            self.ended = True
        else:
            identity = FileIdentity(self.binding, value.decode("utf-8", "strict"))
            if not identity.path.startswith(self.root + "/"):
                raise ValueError("目录路径不属于原固定目录")
            if self.previous is not None and identity.path <= self.previous:
                raise ValueError("目录路径重复或顺序不前进")
            self.previous = identity.path

    def finish(self):
        if not self.started or not self.ended or self.field:
            raise ValueError("目录响应缺少完整结束帧")


class _DirectoryCache:
    def __init__(self, file, binding):
        self.file, self.binding = file, binding
        self.buffer = b""
        self.pending = None
        self.initialized = False

    def receive(self, data):
        view = memoryview(data)
        while view:
            count = self.file.write(view)
            if type(count) is not int or not 0 < count <= len(view):
                raise OSError("目录缓存写入没有可靠进度")
            view = view[count:]

    def _field(self):
        while True:
            end = self.buffer.find(b"\0")
            if end >= 0:
                value, self.buffer = self.buffer[:end], self.buffer[end + 1:]
                return value
            data = self.file.read(_READ_SIZE)
            if not data:
                raise OSError("目录缓存提前结束")
            self.buffer += data
            if len(self.buffer) > OUTPUT_LIMIT_BYTES + _READ_SIZE:
                raise OSError("目录缓存字段超过工具输出界限")

    def _next(self):
        if self.pending is not None:
            identity, self.pending = self.pending, None
            return identity
        value = self._field()
        return None if value == b"END" else FileIdentity(self.binding, value.decode("utf-8", "strict"))

    def page(self, request):
        index = 0 if request.cursor is None else request.cursor.directory_index
        if not self.initialized:
            self.file.seek(0)
            if self._field() != _HEADER:
                raise OSError("目录缓存版本帧无效")
            after = None if request.cursor is None else request.cursor.after_path
            if after is not None:
                while (identity := self._next()) is not None:
                    if identity.path > after:
                        self.pending = identity
                        break
                else:
                    return self._page(request, index, (), False)
            self.initialized = True
        entries = []
        for _ in range(request.batch):
            identity = self._next()
            if identity is None:
                return self._page(request, index, tuple(entries), False)
            entries.append(identity)
        self.pending = self._next()
        return self._page(request, index, tuple(entries), self.pending is not None)

    @staticmethod
    def _page(request, index, entries, more):
        cursor = (DirectoryCursor(request.binding, request.directories, index, entries[-1].path)
                  if more else DirectoryCursor(request.binding, request.directories, index + 1, None)
                  if index + 1 < len(request.directories) else None)
        return Page(entries, cursor)

    def close(self):
        self.file.close()
        self.buffer = b""
        self.pending = None


class AdbDirectoryScan:
    """每个固定目录执行一次完整调用，随后只接受原清单的连续游标。"""

    def __init__(self, filesystem, request: DirectoryRequest):
        self.filesystem = filesystem
        self.binding, self.directories = request.binding, request.directories
        self.expected_cursor = request.cursor
        self.cache = None
        self.closed = self.finished = self.busy = False

    async def read_directory(self, request: DirectoryRequest, *, stop) -> DirectoryRead:
        if (not isinstance(request, DirectoryRequest) or request.binding != self.binding
                or request.directories != self.directories or request.cursor != self.expected_cursor):
            raise ValueError("目录读取必须沿原绑定、完整范围和连续游标推进")
        if self.closed or self.finished or self.busy:
            raise ValueError("目录读取上下文已经结束或仍有未完成读取")
        self.busy = True
        raw = None
        needs_call = self.cache is None
        called = False
        elapsed_ns = None
        try:
            index = 0 if request.cursor is None else request.cursor.directory_index
            if needs_call:
                # 工厂返回的文件先登记给拥有者，再启动任何可取消等待。
                def create_cache():
                    self.cache = _DirectoryCache(tempfile.TemporaryFile(mode="w+b"), self.binding)
                await _file_work(create_cache)
                frame = _DirectoryFrame(self.binding, request.directories[index])

                async def receive(data):
                    frame.receive(data)
                    await _file_work(self.cache.receive, data)

                fs = self.filesystem
                spec = ToolSpec(shell_argv(fs.serial, fs.tools.directory_scan_script(
                    request.directories[index])), request.timeout_s, fs.terminate_grace_s)
                started = time.monotonic_ns()
                called = True
                try:
                    raw = await fs.transport.run_stream(spec, stop, receive)
                finally:
                    elapsed_ns = time.monotonic_ns() - started
                    record_performance("adb_directory", elapsed_ns,
                                       device_id=self.binding.device_id,
                                       directory=request.directories[index])
                if not _returned(raw):
                    return self._failure(raw, "call_failed", called, elapsed_ns)
                frame.finish()
            page = await _file_work(self.cache.page, request)
            self.expected_cursor = page.next_cursor
            self.finished = page.next_cursor is None
            if page.next_cursor is None or page.next_cursor.directory_index != index:
                self.cache.close()
                self.cache = None
            return DirectoryRead(raw, None, page, call_performed=called, elapsed_ns=elapsed_ns)
        except (OSError, ValueError) as error:
            return self._failure(raw, str(error), called, elapsed_ns)
        except BaseException as error:
            self.closed = True
            try:
                self._close_cache()
            except OSError as close_error:
                raise error from close_error
            raise
        finally:
            self.busy = False

    def _failure(self, raw, reason, called, elapsed_ns):
        self.closed = True
        error = _error(raw, "directory", reason)
        try:
            self._close_cache()
        except OSError as close_error:
            error = replace(error, details={**error.details,
                "cache_close_error": f"{type(close_error).__name__}: {close_error}"})
        return DirectoryRead(raw, error, None,
                             call_performed=called, elapsed_ns=elapsed_ns)

    def _close_cache(self):
        if self.cache is not None:
            self.cache.close()
            self.cache = None

    def close(self):
        """读取实际结束后关闭清单；重复关闭不重复操作文件。"""
        if self.busy:
            raise ValueError("目录实际读取结束前不能关闭缓存")
        self._close_cache()
        self.closed = True
