"""副本原异常携带事实的读取失败不替换原异常，也不授权未知清理。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime.copies import (
    CopyChannel, CopyOutcome, CopyReceipt, CopyRequest, FailureLogService, MarkerCheck, MarkerPresence,
    MarkerSave, RunFileOwnership, SaveOutcome,
)
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogRecord

pytestmark = pytest.mark.asyncio


class MemoryMarker:
    directory = Path("/unused/logs")
    ownership = SimpleNamespace(new_token=lambda: "probe")

    def check(self):
        return MarkerCheck(MarkerPresence.ABSENT)

    def create(self, *args):
        return MarkerSave(SaveOutcome.SAVED)


async def test_unreadable_group_copy_receipt_still_finds_reliable_leaf():
    class UnreadableLeaf(asyncio.CancelledError):
        @property
        def copy_receipt(self):
            raise RuntimeError("leaf copy facts getter failed")

    class UnreadableGroup(BaseExceptionGroup):
        @property
        def copy_receipt(self):
            raise RuntimeError("group copy facts getter failed")

    reliable = asyncio.CancelledError("actual copy control")
    fact = reliable.copy_receipt = CopyReceipt(CopyOutcome.COPY_UNKNOWN, error="known copy fact")
    original = UnreadableGroup("copy controls", [UnreadableLeaf("unreadable"), reliable])
    messages = await run_failed_session_delivery(original)
    assert len(messages) == 1
    assert fact.error in messages[0]


async def test_unreadable_copy_receipt_without_reliable_fact_remains_unknown():
    class OriginalControl(asyncio.CancelledError):
        @property
        def copy_receipt(self):
            raise RuntimeError("copy facts getter failed")

    original = OriginalControl("original copy control")
    messages = await run_failed_session_delivery(original)
    assert len(messages) == 1
    assert "outcome=unknown" in messages[0]
    assert "original copy control" in messages[0]


async def test_session_consumer_keeps_original_control_and_delivers_unknown_copy_diagnostic():
    class OriginalControl(asyncio.CancelledError):
        @property
        def copy_receipt(self):
            raise asyncio.CancelledError("secondary getter control")

    original = OriginalControl("original copy control")
    messages = await run_failed_session_delivery(original)
    assert len(messages) == 1
    assert "original report" in messages[0]
    assert "original copy control" in messages[0]
    assert "outcome=unknown" in messages[0]


async def run_failed_session_delivery(original):
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.session.service import run_session
    from camctl.session.supervision import Supervisor
    from integration.session.test_custom_delivery_durability import session_world

    context, database, session, admission = session_world()
    messages = []

    async def diagnose(message):
        messages.append(message)

    async def fail_copy(request):
        raise original

    async def report(_context):
        raise ValueError("original report")

    marker = MemoryMarker()
    copy_channel = create_autospec(CopyChannel, instance=True, spec_set=True)
    owner = LoggingOwner(logs_dir=marker.directory, ownership=RunFileOwnership(),
                         supervisor=Supervisor(), owner_settled=True, diagnose=diagnose)
    context.logging_owner = owner
    context.failure_log = FailureLogService(marker, deliver=fail_copy)
    context.copy_request_factory = lambda error: CopyRequest(
        LogRecord(LogLevel.ERROR, "original report"), Path("/unused"),
        Path("/unused/ready"), copy_channel, marker)
    context.on_log_diagnostic = diagnose
    context.on_stderr_diagnostic = diagnose
    context.flows = {"report": report}

    with pytest.raises(type(original)) as raised:
        await run_session(context, None)

    assert raised.value is original
    database.close.assert_awaited_once()
    session.close.assert_called_once()
    admission.close.assert_called()
    copy_channel.copy_with_write.assert_not_called()
    return messages
