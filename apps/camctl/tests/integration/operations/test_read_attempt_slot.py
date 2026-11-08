"""新读取意图消费当前相机机会及轮次，原意图恢复保留首次结果。"""

from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventValidationError
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionError, commit_operation
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.outputs.batches import BatchOutcome, BatchRequest
from camctl.persistence.repositories.file_batches import FileBatchRepository, register_file_batch_guards

from ..outputs.test_grant_reuse import granted, read_request, read_targets, local_read, _grant_read_access
from ..support.output_storage_cases import QueryProbe
from ..support.output_planning import EventSequence, capture_operation


@pytest.fixture
def ready_read(granted):
    """提供已持有机会的当前事实，不替代机会授予事务的验收。"""
    owned, command, _, prepared = granted
    _grant_read_access(owned, command, prepared)
    return granted


def _intent(command, prepared):
    config = AttemptConfig(3, "10", "0") if command.config is not None else AttemptConfig(1)
    return AttemptIntent("read", command.action_id, OperationKind.READ_FILE,
        AttemptTarget(copy_id=prepared.copy_id), None, config, command.occurred_at + 1, copy_round=1)


@pytest.mark.parametrize("slot", ["held", "absent", "foreign"])
def test_new_read_attempt_requires_its_own_device_slot(ready_read, slot):
    owned, command, _, prepared = ready_read
    if slot != "held":
        owned.connection.execute("UPDATE file_copies SET slot_device_id=? WHERE id=?",
                                 (None if slot == "absent" else "another-camera", prepared.copy_id))
        owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
    if slot == "foreign":
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result
        assert isinstance(result.error, ConsistencyError), result.error
    elif slot == "absent" and command.source_device_file_id is not None:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.disposition is BeginDisposition.REJECTED
        assert result.value.reason == "read_slot_not_held"
    else:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.disposition is BeginDisposition.GRANTED
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?",
                                        (prepared.run_id,)).fetchone() == (1,)
        return
    assert tuple(owned.connection.iterdump()) == before


def test_read_attempt_cannot_use_a_different_copy_round(ready_read):
    owned, command, _, prepared = ready_read
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(replace(_intent(command, prepared), copy_round=2),
                                                 new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, TransactionError), result.error
    assert tuple(owned.connection.iterdump()) == before


def test_read_attempt_requires_the_record_of_its_source(ready_read):
    owned, command, _, prepared = ready_read
    table, identity = (("device_files", command.source_device_file_id) if command.source_device_file_id is not None
                       else ("intermediate_files", command.source_intermediate_file_id))
    owned.connection.execute(f"DELETE FROM {table} WHERE id=?", (identity,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
@pytest.mark.parametrize("binding", ["missing_observer", "different_device", "different_driver"])
def test_read_attempt_requires_consistent_original_device_binding(ready_read, binding):
    owned, command, _, prepared = ready_read
    with sqlite3.connect(":memory:") as baseline:
        owned.connection.backup(baseline)
        accepted = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        assert accepted.value.disposition is BeginDisposition.GRANTED
        baseline.backup(owned.connection)
    column = "source_action_id" if binding == "different_device" else "observer_action_id"
    owned.connection.execute(f"UPDATE device_files SET {column}=? WHERE id=?",
                             (999 if binding == "missing_observer" else 12, command.source_device_file_id))
    if binding != "missing_observer":
        column = "device_id" if binding == "different_device" else "driver_id"
        owned.connection.execute(f"UPDATE actions SET {column}=? WHERE id=12", ("different",))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error
    assert ("读取源观察者与可靠来源的原设备绑定不一致" if binding != "missing_observer"
            else "缺失") in str(result.error)
    assert tuple(owned.connection.iterdump()) == before
    assert not owned.connection.in_transaction


@pytest.mark.parametrize("state", ["released", "next_round"])
def test_original_read_intent_does_not_reapply_current_slot_or_round(ready_read, state):
    owned, command, _, prepared = ready_read
    intent, key = _intent(command, prepared), new_operation_key()
    repository = OperationRepository()
    first = repository.begin_attempt(intent, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.disposition is BeginDisposition.GRANTED
    if state == "released":
        owned.connection.execute("UPDATE file_copies SET slot_device_id=NULL WHERE id=?", (prepared.copy_id,))
    else:
        owned.connection.execute("UPDATE file_copies SET round=2, recopies_used=1 WHERE id=?", (prepared.copy_id,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    repeated = repository.begin_attempt(intent, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value == first.value
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
def test_later_slot_grant_cannot_authorize_earlier_read_attempt(granted):
    owned, command, _, prepared = granted
    request = SlotRequest(prepared.copy_id, command.occurred_at)
    if command.item_id is not None:
        register_file_batch_guards()
        batch = FileBatchRepository().acquire(
            BatchRequest(command.action_id, "cam-1", command.occurred_at), new_operation_key(), owned)
        assert batch.kind is DbOutcomeKind.COMPLETED and batch.value is BatchOutcome.GRANTED, batch

    def acquire_and_begin(target):
        slot = OutputsRepository().grant_read_slot(request, new_operation_key(), target)
        assert slot.kind is DbOutcomeKind.COMPLETED, slot.error
        assert slot.value.outcome is SlotOutcome.GRANTED
        begun = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), target)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        assert begun.value.disposition is BeginDisposition.GRANTED
        return begun

    proposal = capture_operation(owned, acquire_and_begin)
    slot_event, intent_event = proposal.events
    assert (slot_event.event_type, intent_event.event_type) == (22, 11)
    with sqlite3.connect(":memory:") as baseline:
        owned.connection.backup(baseline)
        legal = commit_operation(EventSequence((slot_event, intent_event), proposal), new_operation_key(), owned)
        assert legal.kind == "completed", legal.error
        baseline.backup(owned.connection)

    event_id = slot_event.event_id
    rows = tuple(replace(row, after=replace(row.after,
        values={**row.after.values, "intent_event_id": event_id}))
        if row.table == "operation_attempts" else row for row in intent_event.rows)
    early_intent = replace(intent_event, rows=rows)
    before = tuple(owned.connection.iterdump())
    refused = commit_operation(EventSequence((early_intent, slot_event), proposal), new_operation_key(), owned)
    assert refused.kind == "rolled_back", refused.error
    assert isinstance(refused.error, EventValidationError), refused.error
    assert "设备读取意图要求当前拷贝已取得原来源设备的读取机会" in str(refused.error)
    assert tuple(owned.connection.iterdump()) == before
    assert not owned.connection.in_transaction


def _incomplete_read_proposal(owned, intent, key, mutation):
    proposal = capture_operation(owned,
        lambda target: OperationRepository().begin_attempt(intent, key, target))
    state = deepcopy(proposal.state_rows)
    copy = state["file_copies"][intent.target.copy_id]
    if mutation == "slot":
        copy["slot_device_id"] = "another-camera"
    elif mutation == "round":
        copy.update(round=2, recopies_used=1)
    elif mutation == "binding":
        source = state["device_files"][copy["source_device_file_id"]]
        state["actions"].pop(source["observer_action_id"])
    else:
        table, identity = (("device_files", copy["source_device_file_id"])
                           if copy["source_device_file_id"] is not None
                           else ("intermediate_files", copy["source_intermediate_file_id"]))
        state[table].pop(identity)
    return replace(proposal, state_rows=state)


@pytest.mark.parametrize("mutation", ["slot", "round", "source"])
def test_real_intent_transaction_requires_complete_current_read_facts(ready_read, mutation):
    owned, command, _, prepared = ready_read
    with sqlite3.connect(":memory:") as baseline:
        owned.connection.backup(baseline)
        legal = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
        assert legal.kind is DbOutcomeKind.COMPLETED, legal.error
        assert legal.value.disposition is BeginDisposition.GRANTED
        baseline.backup(owned.connection)
    key = new_operation_key()
    proposal = _incomplete_read_proposal(owned, _intent(command, prepared), key, mutation)
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(EventSequence(proposal.events, proposal), key, owned)
    assert receipt.kind == "rolled_back", receipt
    assert isinstance(receipt.error, EventValidationError), receipt.error
    expected = {"slot": "读取机会" if command.source_device_file_id else "主机源拷贝不能持有相机读取机会",
                "round": "读取意图的轮次与当前拷贝不符", "source": "固定关联缺少"}
    assert expected[mutation] in str(receipt.error)
    assert tuple(owned.connection.iterdump()) == before
    assert not owned.connection.in_transaction


@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
def test_real_intent_transaction_requires_current_device_binding(ready_read):
    owned, command, _, prepared = ready_read
    owned.connection.execute("UPDATE device_files SET observer_action_id=12 WHERE id=?",
                             (command.source_device_file_id,))
    owned.connection.commit()
    with sqlite3.connect(":memory:") as baseline:
        owned.connection.backup(baseline)
        legal = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
        assert legal.kind is DbOutcomeKind.COMPLETED, legal.error
        assert legal.value.disposition is BeginDisposition.GRANTED
        baseline.backup(owned.connection)
    key = new_operation_key()
    proposal = _incomplete_read_proposal(owned, _intent(command, prepared), key, "binding")
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(EventSequence(proposal.events, proposal), key, owned)
    assert receipt.kind == "rolled_back", receipt.error
    assert isinstance(receipt.error, EventValidationError), receipt.error
    assert "固定关联缺少 actions#12" in str(receipt.error)
    assert tuple(owned.connection.iterdump()) == before
    assert not owned.connection.in_transaction


@pytest.mark.parametrize("failure", ["execute", "fetch"])
def test_read_source_query_failure_is_preserved_without_attempt(ready_read, failure):
    owned, command, _, prepared = ready_read
    table = "device_files" if command.source_device_file_id is not None else "intermediate_files"
    identity = command.source_device_file_id if command.source_device_file_id is not None else command.source_intermediate_file_id
    columns = {row[1] for row in owned.connection.execute(f"PRAGMA table_info({table})")}
    probe = QueryProbe(owned.connection,
        lambda actual, reads, parameters: actual == columns and (table, 'id') in reads and parameters == (identity,),
        failure=failure)
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(),
                                                 replace(owned, connection=probe))
    assert probe.matches == 1
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert result.error is probe.error
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    assert tuple(owned.connection.iterdump()) == before
    assert not owned.connection.in_transaction
