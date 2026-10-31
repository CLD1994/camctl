"""目录帧、原范围游标与特殊路径按独立预期验证。"""
from decimal import Decimal
import shlex

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.file_identity import FileIdentity
from camctl.devices.drivers.adb_cameras.filesystem import (
    AdbFilesystem, DirectoryCursor, DirectoryRequest, ShellFileTools, decode_directory,
)
from camctl.devices.drivers.adb_cameras.transport import shell_argv
from camctl.operations.process import LocalExit, RawToolOutcome

BINDING = DeviceBinding("cam-1", "dji-action6")
ROOTS = ("/DCIM", "/SD/DCIM")


def _request(*, cursor=None, batch=2):
    return DirectoryRequest(BINDING, ROOTS, cursor, batch, Decimal("3"))


def _raw(data, *, code=0, error=None, failure=None):
    return RawToolOutcome(LocalExit(exit_code=code), data, error, None,
                          output_failure=failure)


@pytest.mark.parametrize("raw", [
    _raw(b"CAMCTL-DIRECTORY/1\0/DCIM/old.mp4\0END\0", code=1),
    _raw(b"CAMCTL-DIRECTORY/1\0/DCIM/old.mp4\0", error="timeout"),
    _raw(b"CAMCTL-DIRECTORY/1\0END\0", error="output_failed", failure="output_limit_exceeded"),
    _raw(b""),
    _raw(b"CAMCTL-DIRECTORY/1\0/DCIM/a"),
])
def test_directory_read_failure_is_not_empty(raw):
    result = decode_directory(_request(), raw)
    assert result.error is not None
    assert result.page is None
    assert result.outcome is raw


def test_empty_directory_can_advance_to_next_directory():
    result = decode_directory(_request(), _raw(b"CAMCTL-DIRECTORY/1\0END\0"))
    assert result.error is None
    assert result.page.items == ()
    assert result.page.next_cursor == DirectoryCursor(BINDING, ROOTS, 1, None)


def test_final_nonempty_page_has_no_next_cursor():
    cursor = DirectoryCursor(BINDING, ROOTS, 1, None)
    result = decode_directory(_request(cursor=cursor),
        _raw(b"CAMCTL-DIRECTORY/1\0/SD/DCIM/new/movie.mp4\0END\0"))
    assert [entry.path for entry in result.page.items] == ["/SD/DCIM/new/movie.mp4"]
    assert result.page.next_cursor is None


def test_nul_frames_preserve_spaces_quotes_and_newlines():
    paths = ["/DCIM/a 'quote'.mp4", "/DCIM/b\nnewline.DNG"]
    result = decode_directory(_request(),
        _raw(b"CAMCTL-DIRECTORY/1\0" + b"\0".join(p.encode() for p in paths) + b"\0MORE\0"))
    assert [entry.path for entry in result.page.items] == paths
    assert result.page.next_cursor.after_path == paths[-1]


@pytest.mark.parametrize("paths, marker", [
    (["/DCIM/b", "/DCIM/a"], "END"),
    (["/DCIM/a", "/DCIM/a"], "END"),
    (["/DCIM/a", "/DCIM/b", "/DCIM/c"], "MORE"),
    (["/OTHER/a"], "END"),
    ([], "MORE"),
])
def test_invalid_page_cannot_be_a_success(paths, marker):
    data = b"CAMCTL-DIRECTORY/1\0" + b"".join(p.encode() + b"\0" for p in paths) + marker.encode() + b"\0"
    result = decode_directory(_request(), _raw(data))
    assert result.error is not None and result.page is None


@pytest.mark.parametrize("path", ["/DCIM/a", "/DCIM/0"])
def test_repeated_or_backward_page_is_rejected(path):
    cursor = DirectoryCursor(BINDING, ROOTS, 0, "/DCIM/a")
    result = decode_directory(_request(cursor=cursor),
        _raw(b"CAMCTL-DIRECTORY/1\0" + path.encode() + b"\0END\0"))
    assert result.error is not None and result.page is None


def test_cursor_from_other_binding_is_rejected():
    other = DeviceBinding("cam-2", "dji-action6")
    with pytest.raises(ValueError):
        _request(cursor=DirectoryCursor(other, ROOTS, 0, None))


def test_shell_command_preserves_one_remote_script_argument():
    script = "printf '%s' " + shlex.quote("a 'quote'\nline; literal")
    argv = shell_argv("serial-1", script)
    assert argv[:3] == ("adb", "-s", "serial-1")
    assert argv[3:5] == ("shell", "-T")
    assert shlex.split(argv[5]) == ["sh", "-c", script]


@pytest.mark.parametrize("serial", [None, "", "-invalid", "a\0b"])
def test_serial_must_be_explicit(serial):
    with pytest.raises(ValueError):
        shell_argv(serial, "true")


def _metadata_record(path, remote_exit, stdout=b"", stderr=b""):
    return (path.encode("utf-8") + b"\0" + str(remote_exit).encode("ascii") + b"\0"
            + stdout + b"\0" + stderr + b"\0")


def _metadata_frame(*records, end=True, trailing=b""):
    return (b"CAMCTL-METADATA/1\0" + b"".join(records)
            + (b"END\0" if end else b"") + trailing)


def _metadata_raw(data, *, code=0, error=None, failure=None, stderr=b""):
    return RawToolOutcome(LocalExit(exit_code=code), data, error, None,
                          output_failure=failure, stderr=stderr)


class _MetadataTransport:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    async def run(self, spec, stop):
        self.calls.append((spec, stop))
        return self.raw


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout", [b"", b"73", b"00\n", b"73\r\n", b"diagnostic\xff\n73\n"])
async def test_single_metadata_failure_retains_exact_response_and_file(stdout):
    path = "/DCIM/a 'quoted'\nclip.MP4"
    raw = _metadata_raw(stdout, stderr=b"remote diagnostic\xff\n")
    transport = _MetadataTransport(raw)
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await filesystem.metadata(FileIdentity(BINDING, path), timeout_s=Decimal("3.125"), stop=object())

    assert result.value is None and result.outcome is raw
    assert result.error.code == "adb_metadata_failed"
    details = result.error.details
    assert details["identity"] == path
    assert details["stdout_hex"] == stdout.hex()
    assert details["stdout_text"] == stdout.decode("utf-8", "backslashreplace")
    assert details["stderr_hex"] == raw.stderr.hex()
    assert details["local_exit_code"] == 0
    assert details["timeout_s"] == "3.125"
    assert details["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_family_read_returns_only_complete_exact_directory_and_stem():
    paths = ("/DCIM/a 'quoted'\nclip.LRF", "/DCIM/a 'quoted'\nclip.OSV")
    raw = _raw(b"CAMCTL-FAMILY/1\0" + b"".join(p.encode() + b"\0" for p in paths) + b"END\0")
    transport = _MetadataTransport(raw)
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await filesystem.read_family(FileIdentity(BINDING, paths[0]), timeout_s=Decimal("3"), stop=object())

    assert result.error is None
    assert tuple(item.path for item in result.value) == paths
    assert len(transport.calls) == 1
    script = shlex.split(transport.calls[0][0].argv[5])[2]
    assert shlex.quote("/DCIM/a 'quoted'\nclip") in script
    assert "-maxdepth 1" in script


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    b"", b"CAMCTL-FAMILY/1\0/OTHER/clip.OSV\0END\0",
    b"CAMCTL-FAMILY/1\0/DCIM/other.OSV\0END\0",
    b"CAMCTL-FAMILY/1\0/DCIM/clip.OSV\0/DCIM/clip.OSV\0END\0",
    b"CAMCTL-FAMILY/1\0/DCIM/clip.OSV\0", b"CAMCTL-FAMILY/1\0END\0extra",
])
async def test_family_read_failure_cannot_prove_missing_pair(data):
    filesystem = AdbFilesystem(BINDING, "serial-1", _MetadataTransport(_raw(data)), tools=ShellFileTools())

    result = await filesystem.read_family(FileIdentity(BINDING, "/DCIM/clip.LRF"), timeout_s=Decimal("3"), stop=object())

    assert result.value is None and result.error.code == "adb_directory_failed"
    assert result.error.details["stdout_hex"] == data.hex()


async def _metadata_many(filesystem, identities, *, timeout_s=Decimal("9"), stop=None):
    method = getattr(filesystem, "metadata_many", None)
    assert callable(method), "AdbFilesystem must expose metadata_many"
    return await method(identities, timeout_s=timeout_s, stop=stop)


@pytest.mark.asyncio
async def test_metadata_many_reads_ordered_identities_in_one_call_with_original_timeout():
    first = "/DCIM/a 'quoted'\nname.MP4"
    second = "/DCIM/zero.JPG"
    identities = (FileIdentity(BINDING, first), FileIdentity(BINDING, second))
    transport = _MetadataTransport(_metadata_raw(_metadata_frame(
        _metadata_record(first, 0, b"73\n"), _metadata_record(second, 0, b"0\n"))))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())
    stop = object()

    result = await _metadata_many(filesystem, identities, timeout_s=Decimal("4.125"), stop=stop)

    assert result.error is None
    assert tuple(item.identity for item in result.items) == identities
    assert tuple(item.value for item in result.items) == (73, 0)
    assert all(item.error is None for item in result.items)
    assert len(transport.calls) == 1
    assert transport.calls[0][0].timeout_s == Decimal("4.125")
    assert transport.calls[0][1] is stop


@pytest.mark.asyncio
async def test_metadata_many_preserves_each_complete_item_and_all_item_errors():
    paths = ("/DCIM/first.MP4", "/DCIM/missing.JPG", "/DCIM/zero.JPG", "/DCIM/bad.DNG")
    identities = tuple(FileIdentity(BINDING, path) for path in paths)
    transport = _MetadataTransport(_metadata_raw(_metadata_frame(
        _metadata_record(paths[0], 0, b"120\n"),
        _metadata_record(paths[1], 1, b"", b"missing"),
        _metadata_record(paths[2], 0, b"0\n"),
        _metadata_record(paths[3], 0, b"17"))))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await _metadata_many(filesystem, identities)

    assert result.error.code == "adb_metadata_failed"
    assert tuple(item.identity for item in result.items) == identities
    assert tuple(item.value for item in result.items) == (120, None, 0, None)
    assert result.items[0].error is None and result.items[2].error is None
    assert result.items[1].error.details["identity"] == paths[1]
    assert result.items[1].error.details["remote_exit_code"] == 1
    assert result.items[1].error.details["stdout_hex"] == ""
    assert result.items[1].error.details["stderr_hex"] == b"missing".hex()
    assert result.items[3].error.details["identity"] == paths[3]
    assert result.items[3].error.details["remote_exit_code"] == 0
    assert result.items[3].error.details["stdout_hex"] == b"17".hex()
    assert result.items[3].error.details["stderr_hex"] == ""
    assert [error["identity"] for error in result.error.details["item_errors"]] == [paths[1], paths[3]]
    assert len(transport.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout", [b"", b"00\n", b"+1\n", b"1"])
async def test_metadata_many_requires_exact_size_stdout_for_remote_success(stdout):
    path = "/DCIM/bad-size.MP4"
    identity = FileIdentity(BINDING, path)
    transport = _MetadataTransport(_metadata_raw(_metadata_frame(
        _metadata_record(path, 0, stdout, b"raw diagnostic\xff"))))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await _metadata_many(filesystem, (identity,))

    assert result.error.code == "adb_metadata_failed"
    assert result.items[0].value is None
    assert result.items[0].error.details["stdout_hex"] == stdout.hex()
    assert result.items[0].error.details["stderr_hex"] == b"raw diagnostic\xff".hex()
    assert result.error.details["item_errors"] == [dict(result.items[0].error.details)]
    assert result.error.details["stdout_hex"] == transport.raw.output.hex()
    assert result.error.details["stdout_text"] == transport.raw.output.decode("utf-8", "backslashreplace")
    assert result.error.details["identities"] == [path]
    assert result.error.details["command"] == list(transport.calls[0][0].argv)
    assert result.error.details["timeout_s"] == "9"
    assert result.error.details["elapsed_ms"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("identities", [
    (),
    [FileIdentity(BINDING, "/DCIM/a.MP4")],
    (FileIdentity(BINDING, "/DCIM/a.MP4"), FileIdentity(BINDING, "/DCIM/a.MP4")),
    tuple(FileIdentity(BINDING, f"/DCIM/{index}.MP4") for index in range(129)),
    (FileIdentity(DeviceBinding("other", "dji-action6"), "/DCIM/a.MP4"),),
])
async def test_metadata_many_rejects_invalid_request_before_dispatch(identities):
    transport = _MetadataTransport(_metadata_raw(b""))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    with pytest.raises(ValueError):
        await _metadata_many(filesystem, identities)

    assert transport.calls == []


@pytest.mark.asyncio
async def test_metadata_many_rejects_successful_empty_stdout_as_a_bad_frame():
    identity = FileIdentity(BINDING, "/DCIM/a.MP4")
    transport = _MetadataTransport(_metadata_raw(b""))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await _metadata_many(filesystem, (identity,))

    assert result.error.code == "adb_metadata_failed"
    assert result.items == ()


@pytest.mark.asyncio
async def test_metadata_many_keeps_only_complete_prefix_when_frame_is_truncated():
    first, second = "/DCIM/a.MP4", "/DCIM/b.MP4"
    identities = (FileIdentity(BINDING, first), FileIdentity(BINDING, second))
    prefix = _metadata_record(first, 0, b"12\n")
    partial = second.encode() + b"\0" + b"0\0" + b"4"
    transport = _MetadataTransport(_metadata_raw(b"CAMCTL-METADATA/1\0" + prefix + partial))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await _metadata_many(filesystem, identities)

    assert result.error.code == "adb_metadata_failed"
    assert [(item.identity.path, item.value) for item in result.items] == [(first, 12)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [b"", b"00", b"256", b"+1"])
async def test_metadata_many_rejects_noncanonical_or_out_of_range_remote_exit_code(status):
    path = "/DCIM/a.MP4"
    identity = FileIdentity(BINDING, path)
    frame = (b"CAMCTL-METADATA/1\0" + path.encode() + b"\0" + status
             + b"\0" + b"12\n\0" + b"\0" + b"END\0")
    transport = _MetadataTransport(_metadata_raw(frame))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await _metadata_many(filesystem, (identity,))

    assert result.error.code == "adb_metadata_failed"
    assert result.items == ()
    assert result.error.details["frame_errors"][0]["reason"] == "invalid_remote_exit_code"
    assert result.error.details["frame_errors"][0]["stdout_hex"] == b"12\n".hex()
    assert result.error.details["frame_errors"][0]["stderr_hex"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    lambda first, second: _metadata_frame(_metadata_record(second, 0, b"2\n"),
                                           _metadata_record(first, 0, b"1\n")),
    lambda first, second: _metadata_frame(_metadata_record(first, 0, b"1\n"),
                                           _metadata_record(first, 0, b"1\n")),
    lambda first, second: _metadata_frame(_metadata_record(first, 0, b"1\n"),
                                           _metadata_record(second, 0, b"2\n"), end=False),
    lambda first, second: _metadata_frame(_metadata_record(first, 0, b"1\n"),
                                           _metadata_record(second, 0, b"2\n"), trailing=b"extra\0"),
])
async def test_metadata_many_rejects_wrong_identity_duplicate_missing_end_or_trailing_data(frame):
    first, second = "/DCIM/a.MP4", "/DCIM/b.MP4"
    identities = (FileIdentity(BINDING, first), FileIdentity(BINDING, second))
    transport = _MetadataTransport(_metadata_raw(frame(first, second)))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await _metadata_many(filesystem, identities)

    assert result.error.code == "adb_metadata_failed"
    if b"END\0" not in frame(first, second):
        assert [item.value for item in result.items] == [1, 2]
        assert result.error.details["frame_errors"][0]["reason"] == "missing_end"


@pytest.mark.asyncio
async def test_metadata_many_tool_error_keeps_frame_and_all_decoded_item_errors():
    first, second = "/DCIM/a.MP4", "/DCIM/b.MP4"
    identities = (FileIdentity(BINDING, first), FileIdentity(BINDING, second))
    data = (b"CAMCTL-METADATA/1\0" + _metadata_record(first, 1, b"", b"gone")
            + second.encode() + b"\0" + b"0\0" + b"7\n")
    transport = _MetadataTransport(_metadata_raw(data, error="timeout"))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await _metadata_many(filesystem, identities)

    assert result.error.details["reason"] == "call_failed"
    assert result.error.details["transport_error"] == "timeout"
    assert result.error.details["frame_errors"]
    assert result.error.details["item_errors"] == [dict(result.items[0].error.details)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["transport", "exit", "output"])
async def test_metadata_many_keeps_complete_prefix_but_tool_failure_still_fails(cause):
    path = "/DCIM/a.MP4"
    identities = (FileIdentity(BINDING, path),)
    kwargs = {"code": 0}
    if cause == "transport":
        kwargs["error"] = "timeout"
    elif cause == "exit":
        kwargs["code"] = 9
    else:
        kwargs["failure"] = "output_limit_exceeded"
    transport = _MetadataTransport(_metadata_raw(
        _metadata_frame(_metadata_record(path, 0, b"12\n")), **kwargs))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())

    result = await _metadata_many(filesystem, identities)

    assert result.error.code == "adb_metadata_failed"
    assert [(item.identity.path, item.value) for item in result.items] == [(path, 12)]
    if cause == "output":
        assert result.error.details["frame_errors"][0]["reason"] == "output_incomplete"


def _metadata_v2_record(path, stdout, stderr, status):
    return path.encode() + b"\0" + stdout + b"\0" + stderr + b"\0" + status + b"\0"


@pytest.mark.asyncio
async def test_metadata_v2_preserves_zero_size_and_exact_per_path_error_bytes():
    first, second = "/DCIM/zero.MP4", "/DCIM/a 'quote'\nerror.MP4"
    identities = tuple(FileIdentity(BINDING, path) for path in (first, second))
    frame = (b"CAMCTL-METADATA/2\0"
             + _metadata_v2_record(first, b"0\n", b"", b"0")
             + _metadata_v2_record(second, b"partial\n\n", b"diagnostic\xff\n\n", b"7") + b"END\0")
    transport = _MetadataTransport(_metadata_raw(frame))
    filesystem = AdbFilesystem(BINDING, "serial-1", transport, tools=ShellFileTools())
    result = await filesystem.metadata_many(identities, timeout_s=Decimal("3"), stop=object())
    assert result.items[0].value == 0 and result.items[0].error is None
    assert result.items[1].value is None
    details = result.items[1].error.details
    assert details["remote_exit_code"] == 7
    assert details["stdout_hex"] == b"partial\n\n".hex()
    assert details["stderr_hex"] == b"diagnostic\xff\n\n".hex()
    assert result.error.details["item_errors"] == [dict(details)]


@pytest.mark.asyncio
@pytest.mark.parametrize("tail, field, completed", [
    (b"/DCIM/b", "identity", 1),
    (b"/DCIM/b\0" + b"1\n", "stdout", 1),
    (b"/DCIM/b\0" + b"1\n\0err", "stderr", 1),
    (b"/DCIM/b\0" + b"1\n\0err\0", "remote_exit_code", 1),
    (b"/DCIM/b\0" + b"1\n\0err\0" + b"0", "remote_exit_code", 1),
    (_metadata_v2_record("/DCIM/b", b"1\n", b"", b"0"), None, 2),
])
async def test_metadata_v2_requires_complete_status_and_end_before_batch_success(tail, field, completed):
    identities = (FileIdentity(BINDING, "/DCIM/a"), FileIdentity(BINDING, "/DCIM/b"))
    frame = b"CAMCTL-METADATA/2\0" + _metadata_v2_record("/DCIM/a", b"2\n", b"", b"0") + tail
    filesystem = AdbFilesystem(BINDING, "serial-1", _MetadataTransport(_metadata_raw(frame)), tools=ShellFileTools())
    result = await filesystem.metadata_many(identities, timeout_s=Decimal("3"), stop=object())
    assert result.error is not None and len(result.items) == completed
    error = result.error.details["frame_errors"][0]
    assert error["reason"] == ("missing_end" if field is None else "truncated_record")
    if field is not None:
        assert error["field"] == field


def test_metadata_script_v2_uses_one_cat_and_one_error_cache_per_path():
    script = ShellFileTools().metadata_many_script(("/DCIM/a 'quote'\nline",))
    lexer = shlex.shlex(script, posix=True, punctuation_chars=";")
    lexer.whitespace_split = True
    tokens = list(lexer)
    assert "CAMCTL-METADATA/2" in tokens
    assert tokens.count("cat") == 1
    assert "$tmp.err" not in script
    assert "/DCIM/a 'quote'\nline" in tokens
