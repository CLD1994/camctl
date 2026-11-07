"""拷贝及交付的数据库等待与文件责任分别保留。"""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.outputs.copy import (
    CompletionPhase, CopyCompletionError, PreparedCopy, PreparedDisposition, PreparedSaveOutcome,
    SegmentOutcome, complete_copy, copy_next_segment,
)
from camctl.outputs.handoff import (
    AdvanceWithdrawal, DeliveryDecision, DeliveryHandoffOutcome, DeliveryPhase,
    PublicationDisposition, PublicationSaveOutcome, WithdrawalChoice, publish_delivery,
)
from camctl.outputs.withdrawal import WithdrawalProgress, WithdrawalSession
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.persistence.repositories.outputs import OutputsRepository
from support.business_database import inline_business_database
from camctl.host_files.tasks import FileTaskExecutor
from camctl.logging_runtime.events import event_logging
from camctl.session.supervision import Supervisor

pytestmark = pytest.mark.asyncio
KEY = OperationKey("a" * 32)
DIGEST = "a" * 64


async def owned_file_task(task):
    return await task.body(None)


def context_for(repository):
    return SimpleNamespace(database=inline_business_database(OwnedConnection(Mock(spec=sqlite3.Connection),
        DatabaseMetadata("camctl", "a" * 32, 3, "/staging", "/ready", "/processing"))),
        repository=repository, segment_size=10, key=KEY, occurred_at=1000, save_business=None,
        executor=SimpleNamespace(run_owned_async_file_task=owned_file_task, unfinished_files=lambda: ()))


async def test_completed_segment_uses_database_facts_before_file_owner():
    repository = Mock(spec=OutputsRepository)
    repository.load_copy_state.return_value = SimpleNamespace(
        source_size=9, committed_bytes=9, target=SimpleNamespace(file_id=3))
    result = await copy_next_segment(2, context_for(repository))
    assert result.plan.outcome is SegmentOutcome.ALL_COMMITTED
    assert result.saved is None


async def test_saved_verification_prepares_copy_using_original_key():
    repository = Mock(spec=OutputsRepository)
    repository.load_copy_state.return_value = SimpleNamespace(source_size=9,
        committed_bytes=9, target=SimpleNamespace(file_id=3), target_sha256=DIGEST,
        verification_state=int(enum_for("file_copies.verification_state").MATCHED))
    prepared = PreparedCopy(2, 3, 9, DIGEST, True)
    repository.save_prepared.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        PreparedSaveOutcome(PreparedDisposition.SAVED, prepared))
    result = await complete_copy(2, context_for(repository))
    assert result.phase is CompletionPhase.PREPARED
    assert result.prepared == prepared
    request, key, _owned = repository.save_prepared.call_args.args
    assert (request.copy_id, request.size_bytes, request.target_sha256, key) == (2, 9, DIGEST, KEY)


@pytest.mark.parametrize("state", ["MATCHED", "SOURCE_CHECKSUM_UNAVAILABLE"])
async def test_saved_verification_result_redelivery_has_no_new_file_stage(state):
    repository = Mock(spec=OutputsRepository)
    repository.load_copy_state.return_value = SimpleNamespace(source_size=9,
        committed_bytes=9, target=SimpleNamespace(file_id=3), target_sha256=DIGEST,
        verification_state=int(getattr(enum_for("file_copies.verification_state"), state)))
    prepared = PreparedCopy(2, 3, 9, DIGEST, True)
    repository.save_prepared.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN),
        DbOutcome(DbOutcomeKind.COMPLETED, PreparedSaveOutcome(PreparedDisposition.SAVED, prepared))]
    context = context_for(repository)
    context.executor = FileTaskExecutor(Supervisor())
    records = []

    class Channel:
        def slog(self, record):
            records.append(json.loads(record.message))

    with event_logging(Channel()):
        with pytest.raises(CopyCompletionError) as caught:
            await complete_copy(2, context)
        assert caught.value.stage == "prepared_save_unknown"
        assert (await complete_copy(2, context)).phase is CompletionPhase.PREPARED
    assert records == []
    assert [call.args[1] for call in repository.save_prepared.call_args_list] == [KEY, KEY]


async def test_known_handoff_result_is_saved_before_publication_returns(monkeypatch):
    repository = Mock(spec=OutputsRepository)
    repository.load_delivery_state.return_value = SimpleNamespace(action_id=1, file_name="delivery.bin",
        target=SimpleNamespace(file_id=3, relative_path="target/3"),
        facts=SimpleNamespace(status=int(enum_for("deliveries.status").PUBLISHING), withdrawal_requested=False))
    repository.save_publication.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        PublicationSaveOutcome(PublicationDisposition.SAVED))
    context = context_for(repository)
    context.directories = SimpleNamespace(staging=Path("/staging"))
    context.publication_conditions_met = True
    decision = DeliveryDecision(DeliveryHandoffOutcome.DELIVERED_LOCALLY)
    monkeypatch.setattr("camctl.outputs.handoff._decide_again", lambda *_args: decision)

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr("camctl.outputs.handoff.asyncio.to_thread", inline)
    result = await publish_delivery(2, context)
    assert result.phase is DeliveryPhase.PUBLISHED
    assert repository.save_publication.call_args.args[0].delivery_id == 2



def withdrawal_test_session(monkeypatch, repository):
    monkeypatch.setattr("camctl.outputs.withdrawal.OutputsRepository", lambda: repository)
    class Cursor:
        def fetchall(self): return [(1, "delivery.bin", 5, 1, None, 3, 2)]
        def close(self): pass
    class Database:
        metadata = SimpleNamespace(instance_id="db", ready_path="/ready", processing_path="/processing")
        async def read(self, callback, **kwargs):
            return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda *args: Cursor())))
        async def write(self, callback, **kwargs):
            return callback(None)
    executor = SimpleNamespace(unfinished_files=lambda: (), run_owned_async_file_task=owned_file_task)
    return WithdrawalSession(ready=Path("/ready"), processing=Path("/processing"), executor=executor), Database()


async def test_cancel_during_withdrawal_save_still_delivers_saved_request(monkeypatch):
    repository = Mock(spec=OutputsRepository)
    repository.advance_withdrawal.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    session, database = withdrawal_test_session(monkeypatch, repository)
    started, release = asyncio.Event(), asyncio.Event()
    original_write = database.write

    async def paused_write(callback, **kwargs):
        started.set()
        await release.wait()
        return await original_write(callback, **kwargs)

    database.write = paused_write
    task = asyncio.create_task(session.withdraw_delivery(2, database=database, occurred_at=lambda: 1000))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await session.resume_pending_saves(1, database=database) is WithdrawalProgress.STEP_FINISHED
    assert repository.advance_withdrawal.call_count == 1
    request, _key, _owned = repository.advance_withdrawal.call_args.args
    assert (request.delivery_id, request.choice, request.occurred_at) == (2, WithdrawalChoice.REQUESTED, 1000)


@pytest.mark.parametrize("kind", (DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN))
async def test_unconfirmed_withdrawal_reuses_original_save_responsibility(monkeypatch, kind):
    repository = Mock(spec=OutputsRepository)
    original_error = OSError("database")
    repository.advance_withdrawal.side_effect = [DbOutcome(kind, error=original_error), DbOutcome(DbOutcomeKind.COMPLETED)]
    session, database = withdrawal_test_session(monkeypatch, repository)
    with pytest.raises(ConsistencyError) as caught:
        await session.withdraw_delivery(2, database=database, occurred_at=lambda: 1000)
    assert caught.value.__cause__ is original_error
    assert await session.resume_pending_saves(1, database=database) is WithdrawalProgress.STEP_FINISHED
    first, second = repository.advance_withdrawal.call_args_list
    assert first.args[:2] == second.args[:2]
    request = first.args[0]
    assert (request.delivery_id, request.choice, request.occurred_at) == (2, WithdrawalChoice.REQUESTED, 1000)
