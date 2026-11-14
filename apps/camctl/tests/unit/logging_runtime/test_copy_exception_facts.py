"""副本交付保留原异常及可靠写入事实。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.logging_runtime.clh_adapter import WriteResult
from camctl.logging_runtime.copies import (CopyRequest, MarkerCheck, MarkerPresence,
    MarkerSave, SaveOutcome, deliver_failure_copy)
from camctl.logging_runtime.models import LogFileStage, LogLevel
from camctl.logging_runtime.service import LogRecord

pytestmark = pytest.mark.asyncio


class MemoryMarker:
    directory = Path("/unused/logs")
    ownership = SimpleNamespace(new_token=lambda: "probe")

    def check(self):
        return MarkerCheck(MarkerPresence.ABSENT)

    def create(self, *args):
        return MarkerSave(SaveOutcome.SAVED)


async def run_failed_copy(original):
    stages = []

    class Channel:
        def copy_with_write(self, *args):
            raise original

    async def runner(stage, operation, **kwargs):
        stages.append(stage)
        assert stage in {LogFileStage.MARKER_CHECK, LogFileStage.MARKER_CREATE, LogFileStage.COPY_WRITE}
        return operation()

    async def publish(*args):
        pytest.fail("原写入和副本位置未确认，不能发布")

    request = CopyRequest(LogRecord(LogLevel.ERROR, "original report"),
                          Path("/unused"), Path("/unused/ready"), Channel(), MemoryMarker())
    with pytest.raises(type(original)) as raised:
        await deliver_failure_copy(request, operation_runner=runner, publish=publish)
    assert raised.value is original
    assert stages == [LogFileStage.MARKER_CHECK, LogFileStage.MARKER_CREATE, LogFileStage.COPY_WRITE]


async def test_unreadable_write_result_keeps_original_control_and_skips_unconfirmed_cleanup():
    class OriginalControl(asyncio.CancelledError):
        @property
        def write_result(self):
            raise RuntimeError("write facts getter failed")

    original = OriginalControl("original copy control")
    await run_failed_copy(original)
    receipt = original.copy_receipt
    assert receipt.file_stage is LogFileStage.COPY_WRITE
    assert receipt.write_result is None
    assert receipt.cleanup_attempted is False


async def test_unwritable_copy_receipt_keeps_original_control():
    class OriginalControl(asyncio.CancelledError):
        @property
        def copy_receipt(self):
            raise RuntimeError("copy facts getter failed")

        @copy_receipt.setter
        def copy_receipt(self, value):
            raise asyncio.CancelledError("copy facts setter failed")

        def add_note(self, note):
            raise RuntimeError("copy facts note failed")

    original = OriginalControl("original copy control")
    await run_failed_copy(original)


async def test_unreadable_group_write_facts_still_find_reliable_leaf_without_unknown_cleanup():
    class UnreadableLeaf(asyncio.CancelledError):
        @property
        def write_result(self):
            raise RuntimeError("leaf write facts getter failed")

    class UnreadableGroup(BaseExceptionGroup):
        @property
        def write_result(self):
            raise RuntimeError("group write facts getter failed")

    reliable = asyncio.CancelledError("actual writer control")
    # 原日志完成事实明确，副本句柄关闭事实仍未知，不能清理。
    fact = reliable.write_result = WriteResult(appended=True, rotated=False, durable=True)
    original = UnreadableGroup("copy controls", [UnreadableLeaf("unreadable"), reliable])
    await run_failed_copy(original)
    assert original.copy_receipt.write_result is fact
    assert original.copy_receipt.cleanup_attempted is False
