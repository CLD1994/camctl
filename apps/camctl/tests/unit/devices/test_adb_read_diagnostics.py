"""ADB 流读取保留原调用诊断，不启动进程或工作线程。"""
from concurrent.futures import Future
from decimal import Decimal
from types import SimpleNamespace

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.filesystem import (
    AdbFilesystem, ShellFileTools, _AdbSourceStream,
)
from camctl.operations.process import (
    OUTPUT_LIMIT_BYTES, LocalExit, RawToolOutcome, SignalFailure, SignalStage,
    ToolStartError,
)

pytestmark = pytest.mark.asyncio
_PATH = "/DCIM/a 'quoted'\npreview.LRF"


def _stream(raw=None, *, error=None, size=3252796, offset=65539):
    filesystem = AdbFilesystem(DeviceBinding("osmo", "dji-osmo360-ii"),
        "serial-1", SimpleNamespace(), tools=ShellFileTools())
    stream = _AdbSourceStream(filesystem, _PATH, offset, size)
    stream.future = Future()
    if error is not None:
        stream.future.set_exception(error)
    elif raw is not None:
        stream.future.set_result(raw)
    return stream


@pytest.mark.parametrize("exit_value, transport_error, output_failure", [
    (LocalExit(exit_code=0), None, None),
    (LocalExit(exit_code=7), None, None),
    (LocalExit(signal=15), "output_failed", "stderr: output_limit_exceeded"),
])
async def test_early_end_preserves_distinct_raw_outcomes_without_copying_media(
        exit_value, transport_error, output_failure):
    raw = RawToolOutcome(exit_value, b"private media bytes", transport_error,
        Decimal("3.125"), (SignalFailure(SignalStage.TERMINATE, "signal denied"),),
        output_failure, stderr=b"raw\x00\xff\n")
    stream = _stream(raw)

    with pytest.raises(OSError) as caught:
        stream.read(65536)

    details = getattr(caught.value, "diagnostic_details", None)
    assert details is not None
    assert details["reason"] == "unexpected_end"
    assert (details["path"], details["offset"], details["expected_size"], details["remaining"]) == (
        _PATH, 65539, 3252796, 3187257)
    assert details["transport_error"] == transport_error
    assert details["output_failure"] == output_failure
    assert details["local_exit_code"] == exit_value.exit_code
    assert details["local_signal"] == exit_value.signal
    assert details["used_grace_s"] == "3.125"
    assert details["signal_failures"] == [{"stage": "terminate", "message": "signal denied"}]
    assert details["stderr_hex"] == "72617700ff0a"
    assert details["output_complete"] is (output_failure is None)
    assert details["output_limit_bytes"] == OUTPUT_LIMIT_BYTES
    assert "private media bytes" not in repr(details)


@pytest.mark.parametrize("method, reason", [("read", "read_call_failed"), ("close", "close_call_failed")])
async def test_original_tool_start_error_is_chained_with_read_context(method, reason):
    original = ToolStartError("adb missing: \u8bca\u65ad")
    stream = _stream(error=original)

    with pytest.raises(OSError) as caught:
        stream.read(1) if method == "read" else stream.close()

    assert caught.value is not original
    assert caught.value.__cause__ is original
    details = caught.value.diagnostic_details
    assert details["reason"] == reason
    assert details["path"] == _PATH
    assert details["expected_size"] == 3252796
    assert details["remaining"] == 3187257
    assert details["stderr_hex"] is None
    assert details["output_complete"] is None


@pytest.mark.parametrize("raw", [
    RawToolOutcome(LocalExit(exit_code=8), None, None, None, stderr=b"denied\xff"),
    RawToolOutcome(LocalExit(exit_code=0), None, "output_failed", None,
                   output_failure="stdout: broken pipe", stderr=b"read\xff"),
    RawToolOutcome(None, None, None, None, stderr=b"no exit\xff"),
])
async def test_close_failure_preserves_the_actual_outcome(raw):
    stream = _stream(raw, size=65539)

    with pytest.raises(OSError) as caught:
        stream.close()

    details = getattr(caught.value, "diagnostic_details", None)
    assert details is not None
    assert details["reason"] == "call_failed"
    assert details["stderr_hex"] == raw.stderr.hex()
    assert details["transport_error"] == raw.error
    assert details["output_failure"] == raw.output_failure
    assert details["local_exit_code"] == (None if raw.exit is None else raw.exit.exit_code)
    assert stream.closed is True


async def test_read_after_close_keeps_the_original_call_context():
    raw = RawToolOutcome(LocalExit(exit_code=0), None, None, None, stderr=b"dd\xff")
    stream = _stream(raw, size=65539)
    stream.close()

    with pytest.raises(OSError) as caught:
        stream.read(1)

    details = getattr(caught.value, "diagnostic_details", None)
    assert details is not None
    assert details["reason"] == "read_after_close"
    assert details["stderr_hex"] == "6464ff"
    assert details["local_exit_code"] == 0


async def test_stderr_at_the_existing_capture_limit_is_not_truncated_again():
    stderr = b"\xff" * OUTPUT_LIMIT_BYTES
    raw = RawToolOutcome(LocalExit(exit_code=1), b"", None, None, stderr=stderr)
    stream = _stream(raw)

    with pytest.raises(OSError) as caught:
        stream.read(1)

    details = getattr(caught.value, "diagnostic_details", None)
    assert details is not None
    assert bytes.fromhex(details["stderr_hex"]) == stderr
    assert details["output_complete"] is True


@pytest.mark.parametrize("stdout, stderr", [(None, b"error"), (b"", None), (None, None)])
async def test_missing_output_capture_is_not_reported_as_complete(stdout, stderr):
    raw = RawToolOutcome(LocalExit(exit_code=0), stdout, None, None, stderr=stderr)
    stream = _stream(raw)

    with pytest.raises(OSError) as caught:
        stream.read(1)

    assert caught.value.diagnostic_details["output_complete"] is False


async def test_unfinished_read_and_intentional_cancel_keep_their_original_semantics():
    stream = _stream()
    assert stream.read(1) == b""
    stream.cancel()
    assert stream.read(1) == b""
    assert not stream.future.done()


async def test_close_of_short_read_does_not_change_existing_cancel_qualification():
    raw = RawToolOutcome(LocalExit(exit_code=9), None, None, None, stderr=b"remote denied")
    stream = _stream(raw)
    stream.close()
    assert stream.closed and stream.cancelled.is_set()
