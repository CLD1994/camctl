"""分段结果保留实际写入下界、失败阶段和独立源结束事实。"""

import threading
from dataclasses import replace
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.devices.read_session import ReadChunk, ReadEnd, ReadSession
from camctl.host_files.segments import SegmentError, SegmentSpec, WritableFile, transfer_segment


def _spec(stop=None):
    return SegmentSpec("copy/9", 0, "copy.part", 10, 16, 6, stop or threading.Event())


def _source(chunks, end=None, close_failure=None):
    source = create_autospec(ReadSession, instance=True)
    source.position.return_value = 10
    source.read_chunk.side_effect = chunks
    source.poll_stopped.return_value = end
    source.poll_stopped.side_effect = close_failure
    return source


def _target():
    target = create_autospec(WritableFile, instance=True)
    target.write.side_effect = len
    return target


@pytest.mark.parametrize("field", ["round_index", "range_start", "range_end", "chunk_size"])
@pytest.mark.parametrize("invalid", [True, 1.0, Decimal(1), "1", None])
def test_segment_numeric_fields_require_integers(field, invalid):
    # 范围足够容纳 1，确保失败依据是类型而不是恰好越界。
    spec = SegmentSpec("copy/9", 0, "copy.part", 0, 3, 2, threading.Event())
    with pytest.raises(SegmentError):
        replace(spec, **{field: invalid})


def test_short_writes_deliver_entire_chunk_before_next_read():
    source = _source([ReadChunk(b"abcdef", eof=True)], ReadEnd(True, 6, None))
    target = _target()
    written = bytearray()

    def write(data):
        written.extend(data[:2])
        return len(data[:2])

    target.write.side_effect = write
    result = transfer_segment(_spec(), source, target)
    assert written == b"abcdef"
    assert (result.processed_end, result.synced, result.error) == (16, True, None)
    assert result.write_extent_known is True


def test_zero_write_is_failure_without_false_progress():
    source = _source([ReadChunk(b"abcdef")])
    target = _target()
    target.write.side_effect = [2, 0, AssertionError("不得重复无进展调用")]
    result = transfer_segment(_spec(), source, target)
    assert result.error == "write_failed"
    assert result.processed_end == 12
    assert result.write_extent_known is True
    assert result.synced is False
    target.sync.assert_not_called()


@pytest.mark.parametrize("count", [-1, 7, None, True, 1.5])
def test_invalid_write_count_does_not_claim_known_effect(count):
    source = _source([ReadChunk(b"abcdef")])
    target = _target()
    target.write.side_effect = [count]
    result = transfer_segment(_spec(), source, target)
    assert result.error == "write_failed"
    assert result.processed_end == 10
    assert result.write_extent_known is False
    assert result.failure is not None
    target.sync.assert_not_called()


def test_write_exception_retains_confirmed_prefix_and_unknown_effect():
    source = _source([ReadChunk(b"abcdef", eof=True)], ReadEnd(True, 6, None))
    target = _target()
    failure = OSError("disk full")
    target.write.side_effect = [2, failure]
    result = transfer_segment(_spec(), source, target)
    assert result.error == "write_failed"
    assert result.failure is failure
    assert (result.processed_end, result.write_extent_known) == (12, False)
    assert result.source_ended is True
    assert result.source_end == ReadEnd(True, 6, None)
    target.sync.assert_not_called()


@pytest.mark.parametrize("close_fails", [False, True])
def test_source_failure_returns_previous_writes_and_actual_stop(close_fails):
    read_failure, close_failure = OSError("read failed"), OSError("close failed")
    failure = ExceptionGroup("源错误", [read_failure, close_failure]) if close_fails else read_failure
    source = _source(
        [ReadChunk(b"ab"), failure],
        None if close_fails else ReadEnd(True, 2, "failed"),
        close_failure if close_fails else None,
    )
    target = _target()
    result = transfer_segment(_spec(), source, target)
    assert result.error == "read_failed"
    assert result.failure is failure
    assert (result.processed_end, result.write_extent_known) == (12, True)
    assert result.source_ended is (not close_fails)
    assert result.source_close_error is (close_failure if close_fails else None)
    target.sync.assert_not_called()


@pytest.mark.parametrize("stop_at", ["read", "short_write"])
def test_stop_between_calls_prevents_new_write(stop_at):
    stop = threading.Event()
    source = _source([ReadChunk(b"abcdef", eof=True)], ReadEnd(True, 6, None))
    target = _target()

    def read(limit):
        stop.set()
        return ReadChunk(b"abcdef", eof=True)

    def write(data):
        stop.set()
        return 2

    if stop_at == "read":
        source.read_chunk.side_effect = read
    else:
        target.write.side_effect = write
    result = transfer_segment(_spec(stop), source, target)
    assert (result.error, result.synced) == ("stopped", False)
    assert result.processed_end == (10 if stop_at == "read" else 12)
    assert result.source_ended is True
    assert target.write.call_count == (0 if stop_at == "read" else 1)
    target.sync.assert_not_called()


def test_write_failure_keeps_source_closed_fact():
    end = ReadEnd(True, 6, None)
    source = _source([ReadChunk(b"abcdef", eof=True)], end)
    target = _target()
    target.write.side_effect = OSError("write failed")
    result = transfer_segment(_spec(), source, target)
    assert result.source_ended is True
    assert result.source_end is end


def test_sync_failure_keeps_original_exception():
    source = _source([ReadChunk(b"abcdef", eof=True)], ReadEnd(True, 6, None))
    target = _target()
    failure = OSError("sync failed")
    target.sync.side_effect = failure
    result = transfer_segment(_spec(), source, target)
    assert result.error == "sync_failed"
    assert result.failure is failure
    assert (result.processed_end, result.synced) == (16, False)


def test_stop_before_read_keeps_pending_close_distinct_from_failure():
    stop = threading.Event()
    stop.set()
    source = _source([])
    target = _target()
    result = transfer_segment(_spec(stop), source, target)
    assert result.source_end is None
    assert result.source_close_error is None
    assert result.source_ended is False
    source.read_chunk.assert_not_called()
    target.write.assert_not_called()
    target.sync.assert_not_called()
