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
from camctl.outputs.withdrawal import PendingWithdrawal, _save
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
        repository=repository, segment_size=10, key=KEY, occurred_at=1000,
        executor=SimpleNamespace(run_owned_async_file_task=owned_file_task))


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
    repository.load_delivery_state.return_value = SimpleNamespace(target=SimpleNamespace(file_id=3))
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


def pending_withdrawal():
    return PendingWithdrawal(1, 3, "delivery.bin",
        AdvanceWithdrawal(2, WithdrawalChoice.WITHDRAWN, 1000), KEY)


async def test_cancel_during_withdrawal_save_still_removes_confirmed_pending():
    repository = Mock(spec=OutputsRepository)
    repository.advance_withdrawal.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    context = context_for(repository)
    pending = pending_withdrawal()
    context.pending = {2: pending}
    started, release = asyncio.Event(), asyncio.Event()
    original_write = context.database.write

    async def paused_write(callback, *, key, description):
        started.set()
        await release.wait()
        return await original_write(callback, key=key, description=description)

    context.database.write = paused_write
    task = asyncio.create_task(_save(pending, context))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.pending == {}
    assert repository.advance_withdrawal.call_args.args[:2] == (pending.request, KEY)


@pytest.mark.parametrize("kind", (DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN))
async def test_unconfirmed_withdrawal_keeps_original_save_responsibility(kind):
    repository = Mock(spec=OutputsRepository)
    repository.advance_withdrawal.return_value = DbOutcome(kind, error=OSError("database"))
    context = context_for(repository)
    pending = pending_withdrawal()
    context.pending = {2: pending}
    with pytest.raises(ConsistencyError):
        await _save(pending, context)
    assert context.pending == {2: pending}
