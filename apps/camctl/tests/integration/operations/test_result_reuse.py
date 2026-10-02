"""完整结果事务重送核对原输入，并保持原完整边界的响应。"""

from dataclasses import replace
from decimal import Decimal
import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.history.decoding import decode_event_row
from camctl.operations.attempts import (
    AttemptFinish, AttemptTarget, FinishDisposition, RunFinish, RunOutcome, RunStatus,
)
from camctl.devices.evidence import DeviceObservation
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import operations as operation_commands
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    CommandPlan, TransactionError, commit_operation, event_envelope, row_facts, update_change,
)

from .test_attempts import (
    _NOW, _CONTRACTS, _FILE_ABSENT, _delete_intent, _outcome, _preflight_intent,
    _seed_environment, _validated,
)
from ..persistence.test_transactions import FailingConnection


@pytest.fixture
def environment(tmp_path):
    owned = _seed_environment(tmp_path)
    try:
        yield owned, OperationRepository()
    finally:
        owned.connection.close()


def _finish(ticket, status=AttemptStatus.FAILED, **decisions):
    succeeded = status is AttemptStatus.SUCCEEDED
    outcome = _outcome(
        status=status,
        error=None if succeeded else ErrorValue("transport_timeout", "transport"),
        effect=EffectState.CONFIRMED if succeeded else EffectState.UNKNOWN,
        basis=SettlementBasis.OBSERVED if succeeded else SettlementBasis.ASSUMED,
        evidence_type="operation_returned" if succeeded else "adb_foreground_assumption",
        observations=(_FILE_ABSENT,) if succeeded else (),
    )
    return AttemptFinish(ticket, _validated(ticket, outcome), _NOW, **decisions)


def _saved(repository, owned, finish, key):
    value = repository.finish_attempt(finish, key, owned)
    assert value.kind is DbOutcomeKind.COMPLETED, value.error
    return value.value


def _unchanged_reuse(repository, owned, finish, key):
    before = tuple(owned.connection.iterdump())
    value = repository.finish_attempt(finish, key, owned)
    assert tuple(owned.connection.iterdump()) == before
    return value


@pytest.mark.parametrize("status,decisions,run_status", [
    (AttemptStatus.SUCCEEDED, {}, RunStatus.ACTIVE),
    (AttemptStatus.FAILED, {}, RunStatus.ACTIVE),
    (AttemptStatus.UNKNOWN, {}, RunStatus.ACTIVE),
    (AttemptStatus.FAILED, {"retry_wait": True}, RunStatus.ACTIVE),
    (AttemptStatus.SUCCEEDED, {"run_finish": RunFinish(RunOutcome.SUCCEEDED)}, RunStatus.SUCCEEDED),
    (AttemptStatus.FAILED, {"run_finish": RunFinish(RunOutcome.FAILED, ErrorValue("budget_exhausted", "operation"))}, RunStatus.FAILED),
    (AttemptStatus.UNKNOWN, {"run_finish": RunFinish(RunOutcome.UNCONFIRMED, ErrorValue("budget_exhausted", "operation"))}, RunStatus.UNCONFIRMED),
])
def test_result_redelivery_returns_complete_original_response(environment, status, decisions, run_status):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket, status, **decisions)
    key = new_operation_key()
    first = _saved(repository, owned, finish, key)
    assert first.disposition is FinishDisposition.SAVED
    assert first.attempt_status is status
    assert first.run_status is run_status
    again = _unchanged_reuse(repository, owned, finish, key)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == first


@pytest.mark.parametrize("field,value", [
    ("run_id", 2), ("attempt_id", 2), ("responsibility_key", "delete/2"),
    ("target_id", "2"), ("operation", "query"),
])
def test_new_result_rejects_ticket_not_owned_by_original_responsibility(environment, field, value):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    original = _finish(ticket)
    changed = replace(original, ticket=replace(ticket, **{field: value}))
    outcome = _unchanged_reuse(repository, owned, changed, new_operation_key())
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK


@pytest.mark.parametrize("stage", ["new", "late", "reuse"])
@pytest.mark.parametrize("field,value", [
    ("run_id", 2), ("attempt_id", 2), ("responsibility_key", "delete/2"),
    ("target_id", "2"),
])
def test_result_rejects_foreign_validation_context_without_observations(environment, stage, field, value):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()
    if stage != "new":
        _saved(repository, owned, finish, key)
    foreign = replace(ticket, **{field: value})
    validated = _validated(foreign, finish.outcome.outcome)
    request = replace(finish, outcome=validated)
    outcome = _unchanged_reuse(repository, owned, request, key if stage == "reuse" else new_operation_key())
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError), outcome.error


@pytest.mark.parametrize("stage", ["new", "late", "reuse"])
def test_result_rejects_observation_validated_for_another_granted_target(environment, stage):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket, AttemptStatus.SUCCEEDED)
    key = new_operation_key()
    if stage != "new":
        _saved(repository, owned, finish, key)
    owned.connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
        " status, restriction_state, outcome, final_event_id, error_code, error_details_json)"
        " VALUES (2, 1, 2, NULL, 1, 1, NULL, NULL, NULL, NULL)"
    )
    grant = repository.begin_attempt(_delete_intent(target=AttemptTarget(cleanup_item_id=2)), new_operation_key(), owned)
    assert grant.kind is DbOutcomeKind.COMPLETED, grant.error
    foreign = grant.value.ticket
    assert foreign.target_id == "2" and foreign.run_id != ticket.run_id
    call = replace(finish.outcome.outcome, observations=(
        DeviceObservation("file_absent", 1, {"cleanup_item_id": "2"}),
    ))
    request = replace(finish, outcome=_validated(foreign, call))
    outcome = _unchanged_reuse(repository, owned, request, key if stage == "reuse" else new_operation_key())
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError), outcome.error


@pytest.mark.parametrize("stage", ["new", "late", "reuse"])
def test_result_accepts_equivalent_proof_revalidated_for_same_ticket(environment, stage):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()
    if stage != "new":
        _saved(repository, owned, finish, key)
    equal_ticket = replace(ticket)
    validated = _validated(equal_ticket, finish.outcome.outcome)
    request = replace(finish, ticket=equal_ticket, outcome=validated)
    outcome = (repository.finish_attempt(request, key, owned) if stage == "new" else
               _unchanged_reuse(repository, owned, request, key if stage == "reuse" else new_operation_key()))
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    expected = FinishDisposition.ALREADY_ENDED if stage == "late" else FinishDisposition.SAVED
    assert outcome.value.disposition is expected
    assert outcome.value.attempt_status is AttemptStatus.FAILED


def test_final_result_redelivery_checks_unchanged_null_error(environment):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket, AttemptStatus.SUCCEEDED, run_finish=RunFinish(RunOutcome.SUCCEEDED))
    key = new_operation_key()
    first = _saved(repository, owned, finish, key)
    cursor = owned.connection.execute(
        "SELECT body_json FROM history_events WHERE id = ("
        " SELECT last_event_id FROM history_transactions WHERE operation_key = ?)", (str(key),)
    )
    try:
        body = json.loads(cursor.fetchone()[0])
    finally:
        cursor.close()
    assert body["reason"] == 3
    assert "error_json" not in body["rows"][0]["after"]["values"]
    again = _unchanged_reuse(repository, owned, replace(finish, outcome=_validated(ticket, finish.outcome.outcome)), key)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == first


@pytest.mark.parametrize("change", [
    "status", "effect", "error", "settlement", "observations", "call_info",
    "retry_wait", "run_finish", "occurred_at",
])
def test_result_redelivery_rejects_different_input(environment, change):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()
    _saved(repository, owned, finish, key)
    outcome = finish.outcome.outcome
    if change == "status":
        changed = replace(outcome, status=AttemptStatus.UNKNOWN)
    elif change == "effect":
        changed = replace(outcome, effect=EffectState.NO_EFFECT)
    elif change == "error":
        changed = replace(outcome, error=ErrorValue("transport_timeout", "transport", {"different": True}))
    elif change == "settlement":
        changed = replace(outcome, settlement=replace(outcome.settlement, evidence=EvidenceValue("operation_returned", 1)))
    elif change == "observations":
        changed = replace(outcome, observations=(_FILE_ABSENT,))
    elif change == "call_info":
        changed = replace(outcome, call_info=CallInfo(local_exit_code=7))
    else:
        changed = None
    if changed is not None:
        different = replace(finish, outcome=_validated(ticket, changed))
    elif change == "retry_wait":
        different = replace(finish, retry_wait=True)
    elif change == "run_finish":
        different = replace(finish, run_finish=RunFinish(RunOutcome.CANCELED))
    else:
        different = replace(finish, occurred_at=_NOW + 1)
    result = _unchanged_reuse(repository, owned, different, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


@pytest.mark.parametrize("field,value", [
    ("run_id", 2), ("attempt_id", 2), ("responsibility_key", "delete/2"),
    ("target_id", "2"), ("operation", "query"),
])
def test_result_redelivery_rejects_different_ticket(environment, field, value):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()
    _saved(repository, owned, finish, key)
    changed = replace(finish, ticket=replace(ticket, **{field: value}))
    result = _unchanged_reuse(repository, owned, changed, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


class _CancelRun:
    def __init__(self, run_id):
        self.run_id = run_id

    def plan(self, scope):
        facts = row_facts(scope.connection, "operation_runs", self.run_id)
        allocation = scope.allocate(1)
        row = update_change("operation_runs", self.run_id,
                            {"status": facts["status"], "retry_wait_required": facts["retry_wait_required"]},
                            {"status": 5, "retry_wait_required": 0})
        return CommandPlan(
            (event_envelope(allocation.first_event_id, allocation.txn_id, 10, 3, (row,), _NOW),),
            {("operation_runs", self.run_id): ("action", 1)},
            {"operation_runs": {self.run_id: facts}},
        )


@pytest.mark.parametrize("cancel_before", [True, False])
def test_result_only_redelivery_returns_status_at_original_end_boundary(environment, cancel_before):
    owned, repository = environment
    ticket = repository.begin_attempt(_preflight_intent(), new_operation_key(), owned).value.ticket
    finish = AttemptFinish(ticket, validate_outcome(ticket, CallOutcome(
        status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("query_returned", 1)),
    ), _CONTRACTS), _NOW)
    if cancel_before:
        receipt = commit_operation(_CancelRun(ticket.run_id), new_operation_key(), owned)
        assert receipt.kind == "completed", receipt.error
    key = new_operation_key()
    first = _saved(repository, owned, finish, key)
    assert first.run_status is (RunStatus.CANCELED if cancel_before else RunStatus.ACTIVE)
    if not cancel_before:
        receipt = commit_operation(_CancelRun(ticket.run_id), new_operation_key(), owned)
        assert receipt.kind == "completed", receipt.error
    again = _unchanged_reuse(repository, owned, finish, key)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == first


@pytest.mark.parametrize("change", ["status", "error"])
def test_final_result_redelivery_rejects_different_run_finish(environment, change):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket, run_finish=RunFinish(RunOutcome.FAILED, ErrorValue("budget_exhausted", "operation")))
    key = new_operation_key()
    _saved(repository, owned, finish, key)
    decision = (RunFinish(RunOutcome.UNCONFIRMED, finish.run_finish.error) if change == "status"
                else RunFinish(RunOutcome.FAILED, ErrorValue("budget_exhausted", "operation", {"different": True})))
    outcome = _unchanged_reuse(repository, owned, replace(finish, run_finish=decision), key)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError), outcome.error


def test_result_rejects_operation_key_used_for_intent(environment):
    owned, repository = environment
    key = new_operation_key()
    ticket = repository.begin_attempt(_delete_intent(), key, owned).value.ticket
    outcome = _unchanged_reuse(repository, owned, _finish(ticket), key)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError), outcome.error


def test_late_result_rejects_different_target(environment):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    _saved(repository, owned, finish, new_operation_key())
    changed = replace(finish, ticket=replace(ticket, target_id="2"))
    outcome = _unchanged_reuse(repository, owned, changed, new_operation_key())
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError), outcome.error


@pytest.mark.parametrize("original,repeated,equal", [
    (Decimal("0.10000000000000001"), Decimal("0.100000000000000010"), True),
    (False, 0, False),
])
def test_result_redelivery_compares_exact_error_details(environment, original, repeated, equal):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    call = replace(finish.outcome.outcome, error=ErrorValue("transport_timeout", "transport", {"value": original}))
    finish = replace(finish, outcome=_validated(ticket, call))
    key = new_operation_key()
    first = _saved(repository, owned, finish, key)
    repeated_call = replace(call, error=ErrorValue("transport_timeout", "transport", {"value": repeated}))
    outcome = _unchanged_reuse(repository, owned, replace(finish, outcome=_validated(ticket, repeated_call)), key)
    if equal:
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value == first
    else:
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(outcome.error, TransactionError), outcome.error


class _SetRunConfig:
    def __init__(self, run_id, maximum):
        self.run_id = run_id
        self.maximum = maximum

    def plan(self, scope):
        facts = row_facts(scope.connection, "operation_runs", self.run_id)
        allocation = scope.allocate(1)
        row = update_change("operation_runs", self.run_id,
                            {"max_attempts_used": facts["max_attempts_used"]}, {"max_attempts_used": self.maximum})
        return CommandPlan(
            (event_envelope(allocation.first_event_id, allocation.txn_id, 10, 2, (row,), _NOW),),
            {("operation_runs", self.run_id): ("action", 1)},
            {"operation_runs": {self.run_id: facts}},
        )


@pytest.mark.parametrize("invalid", ["middle", "last", "count", "value", "body"])
def test_result_redelivery_rejects_inconsistent_later_recovery_facts(environment, invalid):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()
    _saved(repository, owned, finish, key)
    middle = commit_operation(_SetRunConfig(ticket.run_id, 3), new_operation_key(), owned)
    assert middle.kind == "completed", middle.error
    end = commit_operation(_CancelRun(ticket.run_id), new_operation_key(), owned)
    assert end.kind == "completed", end.error
    connection = owned.connection
    if invalid == "middle":
        connection.execute("DELETE FROM entity_event_links WHERE event_id = ?", (middle.boundary.last_event_id,))
    elif invalid == "last":
        connection.execute("DELETE FROM entity_event_links WHERE event_id = ?", (end.boundary.last_event_id,))
    elif invalid == "count":
        connection.execute("UPDATE entity_event_links SET change_count = 99 WHERE event_id = ?", (end.boundary.last_event_id,))
    elif invalid == "value":
        connection.execute("UPDATE operation_runs SET status = 3 WHERE id = ?", (ticket.run_id,))
    else:
        connection.execute("UPDATE history_events SET body_json = '{}' WHERE id = ?", (middle.boundary.last_event_id,))
    outcome = _unchanged_reuse(repository, owned, finish, key)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError), outcome.error


class _FaultConnection(FailingConnection):
    @property
    def in_transaction(self):
        return self._connection.in_transaction


def test_result_recovery_query_error_rolls_back_without_new_facts(environment):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()
    _saved(repository, owned, finish, key)
    later = commit_operation(_CancelRun(ticket.run_id), new_operation_key(), owned)
    assert later.kind == "completed", later.error
    before = tuple(owned.connection.iterdump())
    fault = _FaultConnection(owned.connection, "SELECT l.change_count")
    outcome = repository.finish_attempt(finish, key, replace(owned, connection=fault))
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, sqlite3.OperationalError), outcome.error
    assert tuple(owned.connection.iterdump()) == before
    assert owned.connection.in_transaction is False


def test_result_rolled_back_then_same_key_can_save_original_input(environment):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()
    before = tuple(owned.connection.iterdump())
    fault = _FaultConnection(owned.connection, "INSERT INTO entity_event_links")
    outcome = repository.finish_attempt(finish, key, replace(owned, connection=fault))
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before
    first = _saved(repository, owned, finish, key)
    again = _unchanged_reuse(repository, owned, finish, key)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == first


def test_result_commit_receipt_lost_then_same_key_returns_original_result(environment):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()

    class LostReceipt(_FaultConnection):
        def execute(self, sql, parameters=()):
            result = self._connection.execute(sql, parameters)
            if sql == "COMMIT":
                raise sqlite3.OperationalError("提交已完成，调用方未收到完成通知")
            return result

    lost = LostReceipt(owned.connection, "unused")
    unknown = repository.finish_attempt(finish, key, replace(owned, connection=lost))
    assert unknown.kind is DbOutcomeKind.UNKNOWN, unknown.error
    again = _unchanged_reuse(repository, owned, finish, key)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value.disposition is FinishDisposition.SAVED
    assert again.value.attempt_status is AttemptStatus.FAILED
    assert again.value.run_status is RunStatus.ACTIVE


def test_result_rejects_operation_key_used_for_read_configuration(environment, monkeypatch):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    body = {"reason": 4, "evidence": {"attempt_id": 1}, "rows": [{
        "table": "operation_attempts", "id": 1,
        "before": {"exists": True, "values": {"max_attempts_used": 2}},
        "after": {"exists": True, "values": {"max_attempts_used": 3}},
    }]}
    # 原键协作者提供的替身事实仍须满足真实存储解码契约。
    decode_event_row((2, 2, 12, 1, _NOW, 2, None, json.dumps(body)))
    saved = {"event_id": 2, "occurred_at": _NOW, "transaction": TransactionRange(2, 2, 2),
             "type": 12, "reason": 4, "body": body}
    monkeypatch.setattr(operation_commands, "_saved_transaction_events", create_autospec(
        operation_commands._saved_transaction_events, return_value=[saved]))
    outcome = _unchanged_reuse(repository, owned, _finish(ticket), new_operation_key())
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, TransactionError), outcome.error


def test_existing_row_cannot_be_restored_before_its_creation(environment):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    before = tuple(owned.connection.iterdump())
    owned.connection.execute("BEGIN")
    try:
        with pytest.raises(ConsistencyError):
            read_row_values_at_boundary(owned.connection, owner=("action", 1), table="operation_runs",
                row_id=ticket.run_id, columns=frozenset({"status"}), current_values={"status": 2},
                boundary=HistoryBoundary(1, 1), current_boundary=HistoryBoundary(2, 2))
    finally:
        owned.connection.execute("ROLLBACK")
    assert tuple(owned.connection.iterdump()) == before


def test_result_recovery_pages_many_changes_in_one_complete_transaction(environment):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket)
    key = new_operation_key()
    first = _saved(repository, owned, finish, key)

    class ManyConfigurations:
        def plan(self, scope):
            facts = row_facts(scope.connection, "operation_runs", ticket.run_id)
            allocation = scope.allocate(129)
            initial = facts["max_attempts_used"]
            events = tuple(event_envelope(allocation.first_event_id + index, allocation.txn_id, 10, 2,
                (update_change("operation_runs", ticket.run_id,
                               {"max_attempts_used": initial + index}, {"max_attempts_used": initial + index + 1}),),
                _NOW) for index in range(129))
            return CommandPlan(events, {("operation_runs", ticket.run_id): ("action", 1)},
                               {"operation_runs": {ticket.run_id: facts}})

    receipt = commit_operation(ManyConfigurations(), new_operation_key(), owned)
    assert receipt.kind == "completed", receipt.error
    canceled = commit_operation(_CancelRun(ticket.run_id), new_operation_key(), owned)
    assert canceled.kind == "completed", canceled.error
    sizes = []

    class ObservedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            sizes.append(len(rows))
            return rows

        def close(self):
            self.cursor.close()

    class ObservedConnection(_FaultConnection):
        def execute(self, sql, parameters=()):
            cursor = self._connection.execute(sql, parameters)
            return ObservedCursor(cursor) if sql.startswith("SELECT l.change_count") else cursor

    before = tuple(owned.connection.iterdump())
    observed = ObservedConnection(owned.connection, "unused")
    again = repository.finish_attempt(finish, key, replace(owned, connection=observed))
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == first
    assert len(sizes) >= 2 and sum(sizes) == 130
    assert all(0 < size <= 128 for size in sizes)
    assert tuple(owned.connection.iterdump()) == before


def test_retry_result_redelivery_preserves_original_active_status_after_success(environment):
    owned, repository = environment
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    finish = _finish(ticket, retry_wait=True)
    key = new_operation_key()
    first = _saved(repository, owned, finish, key)
    second = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    _saved(repository, owned, _finish(second, AttemptStatus.SUCCEEDED, run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key())
    again = _unchanged_reuse(repository, owned, finish, key)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == first
    assert again.value.run_status is RunStatus.ACTIVE


def test_result_redelivery_uses_physical_row_id_instead_of_attempt_number(environment):
    owned, repository = environment
    repository.begin_attempt(_preflight_intent(), new_operation_key(), owned)
    ticket = repository.begin_attempt(_delete_intent(), new_operation_key(), owned).value.ticket
    assert ticket.attempt_id == 1
    assert owned.connection.execute("SELECT id FROM operation_attempts WHERE run_id = ?", (ticket.run_id,)).fetchone()[0] == 2
    finish = _finish(ticket)
    key = new_operation_key()
    first = _saved(repository, owned, finish, key)
    again = _unchanged_reuse(repository, owned, finish, key)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value == first
