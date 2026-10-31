"""取消发起者收场的数据库提交与原申请接手。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.cancellation.models import FinishCancelAction, StopWaitCancelItems
from camctl.cancellation.service import (
    CancellationRuntime,
    OriginSettle,
    settle_origin_cancel,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.service import LogChannel
from support.business_database import inline_business_database


@pytest.mark.asyncio
async def test_unknown_stop_wait_reuses_request_and_key_until_completion():
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = (2, 1, 2)
    connection.execute.return_value = cursor
    metadata = DatabaseMetadata(
        "camctl", "a" * 32, 5, "/staging", "/ready", "/processing")
    owned = OwnedConnection(connection, metadata)

    class Repository:
        def __init__(self):
            self.calls = []
            self.outcomes = [
                DbOutcome(kind=DbOutcomeKind.UNKNOWN),
                DbOutcome(kind=DbOutcomeKind.COMPLETED),
                DbOutcome(kind=DbOutcomeKind.COMPLETED),
            ]

        def stop_wait_cancel_items(self, request, key, owned):
            self.calls.append((request, key))
            return self.outcomes.pop(0)

        def finish_origin_canceled(self, request, key, owned):
            self.calls.append((request, key))
            return self.outcomes.pop(0)

    repository = Repository()
    pending = {}
    runtime = CancellationRuntime(
        database=inline_business_database(owned), repository=repository,
        settlement=None, occurred_at=lambda: 10,
        pending_origin_settlements=pending,
    )
    command = OriginSettle(origin_action_id=7)

    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        first = await settle_origin_cancel(command, runtime)
        identity = metadata.instance_id, command.origin_action_id
        original_request, original_key = pending[identity]
        second = await settle_origin_cancel(command, runtime)

    assert first.kind is DbOutcomeKind.UNKNOWN
    assert isinstance(original_request, StopWaitCancelItems)
    assert second.kind is DbOutcomeKind.COMPLETED
    assert repository.calls[0] == (original_request, original_key)
    assert repository.calls[1] == (original_request, original_key)
    assert isinstance(repository.calls[2][0], FinishCancelAction)
    assert repository.calls[2][1] != original_key
    assert identity not in pending
    records = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
    stop = [record for record in records if record["stage"] == "cancel.origin_stop_waiting"]
    assert [record["event"] for record in stop] == ["started", "returned", "committed"]
    assert stop[-1]["operation_key"] == original_key
    assert all(record["level"] == "INFO" and record["action_id"] == 7
               and record["database_id"] == metadata.instance_id for record in records)


@pytest.mark.asyncio
async def test_cancellation_waits_for_origin_pending_application():
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = (2, 1, 2)
    connection.execute.return_value = cursor
    metadata = DatabaseMetadata(
        "camctl", "b" * 32, 5, "/staging", "/ready", "/processing")
    owned = OwnedConnection(connection, metadata)

    class Repository:
        def __init__(self):
            self.calls = []

        def stop_wait_cancel_items(self, request, key, owned):
            self.calls.append((request, key))
            return DbOutcome(kind=DbOutcomeKind.COMPLETED)

    repository = Repository()
    runtime = CancellationRuntime(
        database=inline_business_database(owned), repository=repository,
        settlement=None, occurred_at=lambda: 10,
    )
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    async def delayed_write(callback, *, key, description):
        write_started.set()
        await release_write.wait()
        return callback(owned)

    runtime.database.write = delayed_write
    command = OriginSettle(origin_action_id=8)
    task = asyncio.create_task(settle_origin_cancel(command, runtime))
    await write_started.wait()
    task.cancel()
    release_write.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert repository.calls[0][0] == StopWaitCancelItems(8, 10)
    assert (metadata.instance_id, 8) not in runtime.pending_origin_settlements
