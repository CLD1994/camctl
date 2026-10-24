"""单次完整目录调用、连续缓存页和实际收场的单元契约。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
import io
import shlex

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRequest
from camctl.devices.drivers.adb_cameras import directory_scan
from camctl.devices.drivers.adb_cameras.filesystem import AdbFilesystem, ShellFileTools
from camctl.operations.process import LocalExit, OUTPUT_LIMIT_BYTES, RawToolOutcome, ToolCallCancelled


BINDING = DeviceBinding("cam-1", "dji-action6")
ROOTS = ("/DCIM", "/SD/DCIM")


def _request(*, cursor=None, directories=ROOTS, batch=2):
    return DirectoryRequest(BINDING, directories, cursor, batch, Decimal("3"))


def _frame(*paths):
    return b"CAMCTL-DIRECTORY/1\0" + b"".join(path.encode() + b"\0" for path in paths) + b"END\0"


def _raw(*, code=0, error=None, failure=None):
    return RawToolOutcome(LocalExit(exit_code=code), b"", error, None, output_failure=failure)


class _StreamTransport:
    """遵守 run_stream 的排空及实际退出语义；不启动真实进程。"""

    def __init__(self, responses, *, outcome=None, exit_gate=None, chunk_size=65536):
        self.responses = iter(responses)
        self.outcome = outcome or _raw()
        self.exit_gate = exit_gate
        self.chunk_size = chunk_size
        self.calls = []
        self.output_ended = asyncio.Event()
        self.exited = False

    async def run_stream(self, spec, stop, stdout_sink):
        self.calls.append((spec, stop))
        sink_error = None
        data = next(self.responses)
        for offset in range(0, len(data), self.chunk_size):
            if sink_error is None:
                try:
                    await stdout_sink(data[offset:offset + self.chunk_size])
                except Exception as error:
                    sink_error = error
        self.output_ended.set()
        interrupted = None
        if self.exit_gate is not None:
            while not self.exit_gate.is_set():
                try:
                    await self.exit_gate.wait()
                except asyncio.CancelledError as error:
                    interrupted = error
        self.exited = True
        if interrupted is not None:
            raise ToolCallCancelled(_raw(error="cancelled")) from interrupted
        if sink_error is not None:
            return _raw(error="output_failed", failure=f"stdout_sink_failed: {sink_error}")
        return self.outcome


@pytest.fixture
def memory_files(monkeypatch):
    files = []
    active = False

    class MemoryFile(io.BytesIO):
        close_calls = 0

        def close(self):
            self.close_calls += 1
            return super().close()

        def write(self, data):
            assert active, "缓存写入必须交给文件执行边界"
            return super().write(data)

        def read(self, size=-1):
            assert active and 0 < size <= 65536
            return super().read(size)

        def seek(self, *args):
            assert active, "缓存定位必须交给文件执行边界"
            return super().seek(*args)

    def create(*, mode):
        assert active and mode == "w+b"
        file = MemoryFile()
        files.append(file)
        return file

    async def inline(function, *args):
        nonlocal active
        active = True
        try:
            return function(*args)
        finally:
            active = False

    monkeypatch.setattr(directory_scan.tempfile, "TemporaryFile", create)
    monkeypatch.setattr(directory_scan.asyncio, "to_thread", inline)
    return files


def _scan(transport, request=None):
    fs = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())
    return fs.directory_scan(request or _request())


@pytest.mark.asyncio
async def test_one_complete_call_supplies_many_pages_and_each_directory_advances(memory_files, monkeypatch):
    timings = []
    monkeypatch.setattr(directory_scan, "record_performance", lambda *args, **fields: timings.append((args, fields)))
    paths = ("/DCIM/a 'quote'.MP4", "/DCIM/b\nnewline.MP4", "/DCIM/c.MP4", "/DCIM/d.MP4", "/DCIM/e.MP4")
    transport = _StreamTransport([_frame(*paths), _frame("/SD/DCIM/z.MP4")], chunk_size=7)
    scan = _scan(transport)
    request = _request()
    assert not memory_files and not transport.calls
    pages = []
    while True:
        page = await scan.read_directory(request, stop=object())
        pages.append(page)
        assert page.error is None
        if page.page.next_cursor is None:
            break
        request = replace(request, cursor=page.page.next_cursor)
    assert [item.path for result in pages for item in result.page.items] == [*paths, "/SD/DCIM/z.MP4"]
    assert [result.call_performed for result in pages] == [True, False, False, True]
    assert all(result.elapsed_ns is None for result in pages if not result.call_performed)
    assert all(type(result.elapsed_ns) is int and result.elapsed_ns >= 0 for result in pages if result.call_performed)
    assert len(transport.calls) == 2
    assert len(timings) == 2
    assert [fields["directory"] for _, fields in timings] == list(ROOTS)
    assert all(file.closed for file in memory_files)
    scan.close()
    scan.close()
    assert all(file.close_calls == 1 for file in memory_files)
    with pytest.raises(ValueError):
        await scan.read_directory(_request(), stop=object())


@pytest.mark.asyncio
async def test_resumed_cursor_uses_complete_scan_and_only_returns_strictly_later_paths(memory_files):
    cursor = DirectoryCursor(BINDING, ROOTS, 0, "/DCIM/b.MP4")
    request = _request(cursor=cursor)
    transport = _StreamTransport([_frame("/DCIM/a.MP4", "/DCIM/c.MP4", "/DCIM/d.MP4", "/DCIM/e.MP4")])
    scan = _scan(transport, request)
    result = await scan.read_directory(request, stop=object())
    assert [item.path for item in result.page.items] == ["/DCIM/c.MP4", "/DCIM/d.MP4"]
    assert result.page.next_cursor.after_path == "/DCIM/d.MP4"
    script = shlex.split(transport.calls[0][0].argv[5])[2]
    assert "after=" not in script
    scan.close()
    assert memory_files[0].closed


@pytest.mark.asyncio
async def test_empty_directory_advances_without_claiming_the_entire_scope_is_empty(memory_files):
    transport = _StreamTransport([_frame(), _frame("/SD/DCIM/a.MP4")])
    scan = _scan(transport)
    first = await scan.read_directory(_request(), stop=object())
    assert first.page.items == ()
    assert first.page.next_cursor == DirectoryCursor(BINDING, ROOTS, 1, None)
    second = await scan.read_directory(_request(cursor=first.page.next_cursor), stop=object())
    assert [item.path for item in second.page.items] == ["/SD/DCIM/a.MP4"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["repeat", "skip", "binding", "range"])
async def test_cache_rejects_noncontinuous_cursor_and_changed_scope_before_dispatch(memory_files, change):
    transport = _StreamTransport([_frame("/DCIM/a", "/DCIM/b", "/DCIM/c")])
    scan = _scan(transport)
    first = await scan.read_directory(_request(), stop=object())
    request = _request(cursor=first.page.next_cursor)
    if change == "repeat":
        request = _request()
    elif change == "skip":
        request = _request(cursor=DirectoryCursor(BINDING, ROOTS, 1, None))
    elif change == "binding":
        request = DirectoryRequest(DeviceBinding("other", "dji-action6"), ROOTS, None, 2, Decimal("3"))
    else:
        request = _request(directories=("/DCIM",))
    with pytest.raises(ValueError):
        await scan.read_directory(request, stop=object())
    assert len(transport.calls) == 1
    scan.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    b"", b"CAMCTL-DIRECTORY/1\0/DCIM/a", _frame("/DCIM/b", "/DCIM/a"),
    _frame("/DCIM/a", "/DCIM/a"), _frame("/OTHER/a"), _frame("/DCIM/../a"),
    _frame() + b"extra", b"CAMCTL-DIRECTORY/1\0\xff\0END\0",
    b"CAMCTL-DIRECTORY/1\0" + b"/DCIM/" + b"a" * OUTPUT_LIMIT_BYTES,
])
async def test_invalid_complete_or_truncated_stream_never_delivers_a_page(memory_files, data):
    transport = _StreamTransport([data])
    scan = _scan(transport)
    result = await scan.read_directory(_request(), stop=object())
    assert result.page is None and result.error is not None
    assert transport.exited and memory_files[0].closed
    with pytest.raises(ValueError):
        await scan.read_directory(_request(), stop=object())


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [_raw(code=9), _raw(error="timeout"), _raw(failure="stderr: output_limit_exceeded")])
async def test_valid_frame_does_not_override_actual_call_failure(memory_files, outcome):
    transport = _StreamTransport([_frame("/DCIM/a")], outcome=outcome)
    result = await _scan(transport).read_directory(_request(), stop=object())
    assert result.page is None and result.error.details["reason"] == "call_failed"
    assert memory_files[0].closed


@pytest.mark.asyncio
async def test_cache_creation_error_does_not_report_an_adb_call(memory_files, monkeypatch):
    def fail(*, mode):
        raise OSError("cache creation failed")
    monkeypatch.setattr(directory_scan.tempfile, "TemporaryFile", fail)
    transport = _StreamTransport([_frame()])
    result = await _scan(transport).read_directory(_request(), stop=object())
    assert result.page is None and result.error is not None
    assert not result.call_performed and result.elapsed_ns is None
    assert not transport.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "seek"])
async def test_cache_io_failure_waits_for_call_exit_and_never_delivers_a_page(memory_files, monkeypatch, operation):
    create = directory_scan.tempfile.TemporaryFile
    def fail(*args):
        raise OSError("cache I/O failed")
    def broken(*, mode):
        file = create(mode=mode)
        monkeypatch.setattr(file, operation, fail)
        return file
    monkeypatch.setattr(directory_scan.tempfile, "TemporaryFile", broken)
    transport = _StreamTransport([_frame("/DCIM/a")])
    result = await _scan(transport).read_directory(_request(), stop=object())
    assert result.page is None and result.error is not None
    assert transport.exited and memory_files[0].closed


@pytest.mark.asyncio
async def test_cache_close_error_preserves_original_tool_error_and_allows_close_retry(memory_files, monkeypatch):
    create = directory_scan.tempfile.TemporaryFile
    def broken(*, mode):
        file = create(mode=mode)
        close = file.close
        def fail_once():
            monkeypatch.setattr(file, "close", close)
            raise OSError("cache close failed")
        monkeypatch.setattr(file, "close", fail_once)
        return file
    monkeypatch.setattr(directory_scan.tempfile, "TemporaryFile", broken)
    transport = _StreamTransport([_frame("/DCIM/a")], outcome=_raw(error="timeout"))
    scan = _scan(transport)
    result = await scan.read_directory(_request(), stop=object())
    assert result.page is None
    assert result.outcome.error == "timeout"
    assert result.error.details["transport_error"] == "timeout"
    assert "cache close failed" in result.error.details["cache_close_error"]
    scan.close()
    scan.close()
    assert transport.exited and memory_files[0].closed


@pytest.mark.asyncio
async def test_first_page_waits_for_actual_process_exit(memory_files):
    gate = asyncio.Event()
    transport = _StreamTransport([_frame("/DCIM/a")], exit_gate=gate)
    scan = _scan(transport)
    task = asyncio.create_task(scan.read_directory(_request(), stop=object()))
    await transport.output_ended.wait()
    assert not task.done() and not memory_files[0].closed
    gate.set()
    result = await task
    assert transport.exited and result.page.items[0].path == "/DCIM/a"
    scan.close()


@pytest.mark.asyncio
async def test_cancelled_stream_waits_for_actual_exit_before_closing_cache(memory_files):
    gate = asyncio.Event()
    transport = _StreamTransport([_frame("/DCIM/a")], exit_gate=gate)
    scan = _scan(transport)
    task = asyncio.create_task(scan.read_directory(_request(), stop=object()))
    await transport.output_ended.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not memory_files[0].closed
    gate.set()
    with pytest.raises(ToolCallCancelled):
        await task
    assert transport.exited and memory_files[0].closed
    scan.close()


@pytest.mark.asyncio
async def test_cancelled_stream_retains_actual_outcome_when_cache_close_also_fails(memory_files, monkeypatch):
    gate = asyncio.Event()
    transport = _StreamTransport([_frame("/DCIM/a")], exit_gate=gate)
    scan = _scan(transport)
    task = asyncio.create_task(scan.read_directory(_request(), stop=object()))
    await transport.output_ended.wait()
    close = memory_files[0].close
    def fail_close():
        raise OSError("cache close failed")
    monkeypatch.setattr(memory_files[0], "close", fail_close)
    task.cancel()
    gate.set()
    with pytest.raises(ToolCallCancelled) as caught:
        await task
    assert transport.exited and caught.value.outcome.error == "cancelled"
    assert isinstance(caught.value.__cause__, OSError)
    monkeypatch.setattr(memory_files[0], "close", close)
    scan.close()
    assert memory_files[0].closed


@pytest.mark.asyncio
async def test_cancelled_file_waiter_retains_cache_until_actual_operation_returns(memory_files, monkeypatch):
    transport = _StreamTransport([_frame("/DCIM/a", "/DCIM/b", "/DCIM/c")])
    scan = _scan(transport)
    first = await scan.read_directory(_request(), stop=object())
    entered, release = asyncio.Event(), asyncio.Event()
    inline = directory_scan.asyncio.to_thread

    async def held(function, *args):
        entered.set()
        await release.wait()
        return await inline(function, *args)

    monkeypatch.setattr(directory_scan.asyncio, "to_thread", held)
    task = asyncio.create_task(scan.read_directory(_request(cursor=first.page.next_cursor), stop=object()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not memory_files[0].closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert memory_files[0].closed
    scan.close()


@pytest.mark.asyncio
async def test_directory_larger_than_capture_limit_is_paged_with_bounded_reads(memory_files):
    paths = tuple(f"/DCIM/{index:05d}-" + "x" * 100 for index in range(10000))
    data = _frame(*paths)
    assert len(data) > OUTPUT_LIMIT_BYTES
    transport = _StreamTransport([data])
    request = _request(directories=("/DCIM",), batch=128)
    scan = _scan(transport, request)
    count = 0
    while True:
        result = await scan.read_directory(request, stop=object())
        assert result.error is None and len(result.page.items) <= 128
        count += len(result.page.items)
        if result.page.next_cursor is None:
            break
        request = replace(request, cursor=result.page.next_cursor)
    assert count == 10000 and len(transport.calls) == 1
    assert memory_files[0].closed
