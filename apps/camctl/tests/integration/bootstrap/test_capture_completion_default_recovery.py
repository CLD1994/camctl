"""默认五入口以真实会话先核原拍摄终态，再筛选新的业务候选。"""

from dataclasses import replace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..capture.test_capture_completion_save_recovery import completion_method, completion_world
from ..support.bootstrap_recovery import DefaultRecoveryWorld, StorageGate, TransactionFault
from .test_start_close_default_recovery import _rows

pytestmark = pytest.mark.asyncio
_ENTRANCES = ("normal", "residual", "restricted", "normal-cancel", "restricted-cancel")
_CONSUMERS = ("photo", "timelapse", "canceled_timelapse", "binding")


async def _exercise(tmp_path, monkeypatch, capture_resources, consumer, entrance, *,
                    after_commit=True, confirmation_unknown=None):
    source = await completion_world(tmp_path, monkeypatch, consumer)
    world = DefaultRecoveryWorld(source, capture_resources, monkeypatch, binding=consumer == "binding")
    world.wall[0] += 5_000_000
    runtime = await world.producer()
    method = completion_method(consumer)
    original = getattr(CaptureRepository, method)
    inputs, receipts, faults = [], [], []
    gate = None if confirmation_unknown is None else StorageGate(
        original_key=True, unknown=confirmation_unknown)

    def save(repository, request, key, owned):
        inputs.append((request, key))
        if len(inputs) == 1:
            fault = TransactionFault(owned.connection, after_commit=after_commit)
            faults.append(fault)
            receipt = original(repository, request, key, replace(owned, connection=fault))
        elif gate is not None:
            receipt = gate.submit(lambda *args: original(repository, *args), request, key, owned)
        else:
            receipt = original(repository, request, key, owned)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(CaptureRepository, method, save)
    try:
        with world.observer() as owned:
            before_attempts, before_files = _rows(owned, "operation_attempts"), _rows(owned, "device_files")
            with pytest.raises(ConsistencyError):
                await capture_handler(source.handler)(source.action_id, runtime)
            assert len(inputs) == len(receipts) == len(faults) == 1
            assert receipts[0].kind is DbOutcomeKind.UNKNOWN and faults[0].commit_calls == 1
            request, key = inputs[0]
            assert request.occurred_at == source.formed_at + 5_000_000
            assert request.action_id == source.action_id
            assert len(request.drafts) == 1 and request.drafts[0].file.device_file_id == source.file_id
        await world.reconnect()
        with world.observer() as owned:
            first_events = saved_transaction_events(owned.connection, key)
            assert (first_events is not None) is after_commit
            before = tuple(owned.connection.iterdump())

            def check():
                assert inputs == [(request, key)] * 2 and inputs[1][0] is request
                assert receipts[1].kind is DbOutcomeKind.COMPLETED, receipts[1].error
                events = saved_transaction_events(owned.connection, key)
                assert events and {event["occurred_at"] for event in events} == {request.occurred_at}
                if first_events is not None:
                    assert events == first_events
                assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
                    (str(key),)).fetchone() == (1,)
                status = 6 if consumer in ("binding", "canceled_timelapse") else 4
                assert owned.connection.execute("SELECT status FROM actions WHERE id=?",
                    (source.action_id,)).fetchone() == (status,)
                assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
                assert _rows(owned, "operation_attempts") == before_attempts
                assert _rows(owned, "device_files") == before_files
                assert saved_transaction_events(owned.connection, source.key) == source.close_events

            await world.replay(entrance, check, ready=lambda: len(receipts) == 2 and
                receipts[-1].kind is DbOutcomeKind.COMPLETED, failure=gate is not None)
            if gate is not None:
                assert inputs == [(request, key)] * 2 and inputs[1][0] is request
                assert gate.hits == 1
                assert receipts[1].kind is (
                    DbOutcomeKind.UNKNOWN if confirmation_unknown else DbOutcomeKind.ROLLED_BACK)
                assert tuple(owned.connection.iterdump()) == before
            else:
                check()
    finally:
        world.close()


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("entrance", _ENTRANCES)
async def test_default_entry_confirms_full_capture_before_candidates(
        tmp_path, monkeypatch, capture_resources, consumer, after_commit, entrance):
    await _exercise(tmp_path, monkeypatch, capture_resources, consumer, entrance, after_commit=after_commit)


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("confirmation_unknown", [False, True])
@pytest.mark.parametrize("entrance", ["normal", "restricted-cancel"])
async def test_default_entry_keeps_capture_when_original_key_cannot_be_read(
        tmp_path, monkeypatch, capture_resources, consumer, confirmation_unknown, entrance):
    await _exercise(tmp_path, monkeypatch, capture_resources, consumer, entrance,
        confirmation_unknown=confirmation_unknown)
