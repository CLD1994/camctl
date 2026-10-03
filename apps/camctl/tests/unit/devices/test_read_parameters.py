"""读取参数错误在源调用前拒绝，不污染已有会话。"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.devices import read_session
from camctl.devices.read_session import ReadSession, ReadSessionError, SourceFile, SourceStream, open_read
from camctl.operations.models import AttemptTicket


TICKET = AttemptTicket(attempt_id=1, operation="read", target_id="9", responsibility_key="copy/9", run_id=1)


@pytest.mark.parametrize("size", [True, False, 1.0, Decimal(1), "1", None, -1])
def test_source_size_requires_nonnegative_integer(size):
    with pytest.raises(ValueError):
        SourceFile("9", {}, size)


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [False, True])
@pytest.mark.parametrize("offset", [True, 1.0, Decimal(1), "1", None, -1, 3])
async def test_all_session_entries_reject_invalid_offsets_without_touching_source(factory, offset):
    stream = create_autospec(SourceStream, instance=True)
    with pytest.raises(ReadSessionError):
        if factory:
            await open_read(SourceFile("9", {}, 2), offset, TICKET,
                            stream=stream, no_data_timeout_s=Decimal(1))
        else:
            ReadSession(SourceFile("9", {}, 2), offset, stream, Decimal(1))
    stream.read.assert_not_called()
    stream.close.assert_not_called()
    stream.cancel.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [False, True])
@pytest.mark.parametrize("timeout", [Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity"), 1, 1.0, True, None])
async def test_all_session_entries_reject_invalid_timeouts(factory, timeout):
    stream = create_autospec(SourceStream, instance=True)
    with pytest.raises(ReadSessionError):
        if factory:
            await open_read(SourceFile("9", {}, 2), 0, TICKET,
                            stream=stream, no_data_timeout_s=timeout)
        else:
            ReadSession(SourceFile("9", {}, 2), 0, stream, timeout)
    stream.read.assert_not_called()
    stream.close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [True, 1.0, Decimal(1), "1", None, 0, -1])
async def test_invalid_read_limit_leaves_session_available(limit):
    stream = create_autospec(SourceStream, instance=True)
    stream.read.return_value = b"xy"
    session = ReadSession(SourceFile("9", {}, 2), 0, stream, Decimal(1))
    with pytest.raises(ReadSessionError):
        session.read_chunk(limit)
    stream.read.assert_not_called()
    stream.close.assert_not_called()
    assert session.position() == 0
    assert session.read_chunk(2).data == b"xy"
    assert (await session.wait_stopped()).bytes_read == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout,elapsed", [(Decimal("1e-1000"), 0.0), (Decimal("0.10000000000000001"), 0.1)])
async def test_timeout_preserves_decimal_threshold(monkeypatch, timeout, elapsed):
    ticks = iter([0.0, elapsed, elapsed])
    monkeypatch.setattr(read_session, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    stream = create_autospec(SourceStream, instance=True)
    stream.read.side_effect = [b"", b"x"]
    session = ReadSession(SourceFile("9", {}, 1), 0, stream, timeout)
    chunk = session.read_chunk(1)
    assert chunk.data == b"x"
    assert chunk.eof is True
    assert (await session.wait_stopped()).error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 2])
async def test_offset_at_source_end_closes_without_reading(size):
    stream = create_autospec(SourceStream, instance=True)
    session = await open_read(SourceFile("9", {}, size), size, TICKET,
                              stream=stream, no_data_timeout_s=Decimal("1e1000"))
    assert session.read_chunk(1).eof is True
    assert (await session.wait_stopped()).bytes_read == 0
    stream.read.assert_not_called()
    stream.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_timeout_expires_at_exact_threshold(monkeypatch):
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(read_session, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    stream = create_autospec(SourceStream, instance=True)
    stream.read.return_value = b""
    session = ReadSession(SourceFile("9", {}, 1), 0, stream, Decimal(1))
    assert session.read_chunk(1).error == "no_data"
    assert (await session.wait_stopped()).error == "no_data"
