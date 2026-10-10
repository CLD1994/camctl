"""目录帧、原范围游标与特殊路径按独立预期验证。"""
from decimal import Decimal
import shlex

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.filesystem import (
    DirectoryCursor, DirectoryRequest, decode_directory,
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
