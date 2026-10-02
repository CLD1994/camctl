"""启动授予核对原组、责任与配置；后来状态不改变原票据。"""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, OperationKind, RunFinish, RunOutcome
from camctl.operations.models import AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import GrantOutcome, SchedulingRepository
from camctl.persistence.runtime import OwnedConnection

from .test_resources import _CONTRACTS, _NOW, _grant_request, _seed_activity, _seed_environment, _seed_plan, _seed_record_action


CONFIG = AttemptConfig(3, Decimal("10"), Decimal("0.5"))
NEW_CONFIG = AttemptConfig(4, Decimal("12"), Decimal("0.75"))


@pytest.fixture
def environment(tmp_path):
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    for identity in (1, 2):
        _seed_record_action(connection, identity, 1, input_index=identity - 1)
        _seed_activity(connection, identity)
    connection.commit()
    try:
        yield owned, SchedulingRepository()
    finally:
        connection.close()


def _request(**fields):
    return _grant_request(1, config=CONFIG, **fields)


def _completed(outcome):
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value


def _original(environment, **fields):
    owned, repository = environment
    request = _request(**fields)
    key = new_operation_key()
    result = _completed(repository.grant_start(request, key, owned))
    assert result.outcome is GrantOutcome.GRANTED
    assert result.ticket == AttemptTicket(1, "control", "1", "start/1", 1)
    assert result.activity_id == 1
    return request, key, result


def _readonly(environment, request, key):
    owned, repository = environment
    before = tuple(owned.connection.iterdump())
    result = repository.grant_start(request, key, owned)
    assert tuple(owned.connection.iterdump()) == before
    return result


@pytest.mark.parametrize("field,value", [
    ("action_id", 2), ("device_id", "cam-2"), ("occurred_at", _NOW + 1),
    ("config", AttemptConfig(4, Decimal("10"), Decimal("0.5"))),
    ("config", AttemptConfig(3, Decimal("11"), Decimal("0.5"))),
    ("config", AttemptConfig(3, Decimal("10"), Decimal("0.6"))),
    ("config", AttemptConfig(3, Decimal("10"), None)),
])
def test_grant_original_key_rejects_different_saved_input(environment, field, value):
    request, key, _ = _original(environment)
    result = _readonly(environment, replace(request, **{field: value}), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ValueError), result.error


def test_new_grant_rejects_device_not_owned_by_action(environment):
    result = _readonly(environment, _request(device_id="cam-2"), new_operation_key())
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ValueError), result.error


@pytest.mark.parametrize("old_key", [False, True])
@pytest.mark.parametrize("field,value", [("action_id", True), ("action_id", 1.0),
                                        ("occurred_at", False), ("occurred_at", 0.0)])
def test_grant_identity_types_are_checked_before_new_or_saved_decision(environment, old_key, field, value):
    request, key, _ = _original(environment, occurred_at=0)
    result = _readonly(environment, replace(request, **{field: value}), key if old_key else new_operation_key())
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ValueError), result.error


def test_grant_reuses_exact_original_ticket_with_equivalent_numeric_config(environment):
    request, key, first = _original(environment)
    equivalent = AttemptConfig(3, Decimal("10.00"), Decimal("0.50"))
    assert _completed(_readonly(environment, replace(request, config=equivalent), key)) == first


def test_grant_does_not_reuse_ordinary_start_intent_without_activity_dispatch(environment):
    owned, _ = environment
    intent = AttemptIntent("control", 1, OperationKind.START, AttemptTarget(activity_id=1), None, CONFIG, occurred_at=_NOW)
    key = new_operation_key()
    _completed(OperationRepository().begin_attempt(intent, key, owned))
    result = _readonly(environment, _request(), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ValueError), result.error


def _finish_for_retry(owned, ticket, *, run_finish=None):
    outcome = validate_outcome(ticket, CallOutcome(
        status=AttemptStatus.FAILED, error=ErrorValue("window_ended", "dispatch"),
        effect=EffectState.NO_EFFECT,
        settlement=Settlement(SettlementBasis.NOT_DISPATCHED, EvidenceValue("dispatch_prevented", 1, {})),
        observations=(),
    ), _CONTRACTS)
    _completed(OperationRepository().finish_attempt(
        AttemptFinish(ticket, outcome, _NOW, retry_wait=run_finish is None, run_finish=run_finish), new_operation_key(), owned))
    # 夹具提供已可靠核实未派发的活动状态；活动观察生产者另作组合验收。
    owned.connection.execute("UPDATE device_activities SET dispatch_state = 1 WHERE id = 1")


@pytest.mark.parametrize("selected", ["first", "second"])
def test_grant_old_key_uses_original_attempt_config_after_later_retry(environment, selected):
    owned, repository = environment
    request, first_key, first = _original(environment)
    _finish_for_retry(owned, first.ticket)
    second_request = replace(request, config=NEW_CONFIG, occurred_at=_NOW + 1)
    second_key = new_operation_key()
    second = _completed(repository.grant_start(second_request, second_key, owned))
    assert second.outcome is GrantOutcome.GRANTED
    assert second.ticket == AttemptTicket(2, "control", "1", "start/1", 1)
    original_request, key, original = ((request, first_key, first) if selected == "first"
                                     else (second_request, second_key, second))
    assert _completed(_readonly(environment, original_request, key)) == original
    foreign = NEW_CONFIG if selected == "first" else CONFIG
    rejected = _readonly(environment, replace(original_request, config=foreign), key)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK


@pytest.mark.parametrize("mutation", ["attempt_missing", "run_identity", "intent_reference", "attempt_configuration",
                                      "run_configuration", "activity_omitted", "extra_attempt"])
def test_grant_reuse_rejects_unreliable_original_or_current_association(environment, mutation):
    owned, _ = environment
    connection = owned.connection
    request, key, _ = _original(environment)
    if mutation == "attempt_missing":
        connection.execute("DELETE FROM operation_attempts WHERE id = 1")
    elif mutation == "run_identity":
        connection.execute("UPDATE operation_runs SET kind = 2 WHERE id = 1")
    else:
        with closing(connection.execute("SELECT id, body_json FROM history_events WHERE transaction_id ="
                " (SELECT id FROM history_transactions WHERE operation_key = ?)", (str(key),))) as cursor:
            event_id, raw = cursor.fetchone()
        body = json.loads(raw)
        attempt = next(row for row in body["rows"] if row["table"] == "operation_attempts")
        run = next(row for row in body["rows"] if row["table"] == "operation_runs")
        if mutation == "intent_reference":
            attempt["after"]["values"]["intent_event_id"] = event_id + 1
        elif mutation == "attempt_configuration":
            attempt["after"]["values"]["timeout_s_json"] = 11
            request = replace(request, config=AttemptConfig(3, Decimal("11"), Decimal("0.5")))
        elif mutation == "run_configuration":
            run["after"]["values"]["max_attempts_used"] = 4
        elif mutation == "activity_omitted":
            body["rows"] = [row for row in body["rows"] if row["table"] != "device_activities"]
        else:
            extra = json.loads(json.dumps(attempt))
            extra["id"] = 2
            extra["after"]["values"]["attempt_no"] = 2
            body["rows"].append(extra)
        connection.execute("UPDATE history_events SET body_json = ? WHERE id = ?", (json.dumps(body), event_id))
    rejected = _readonly(environment, request, key)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, ValueError), rejected.error


def test_grant_old_key_returns_original_ticket_after_run_finished(environment):
    owned, _ = environment
    request, key, first = _original(environment)
    _finish_for_retry(owned, first.ticket, run_finish=RunFinish(RunOutcome.FAILED, ErrorValue("budget_exhausted", "operation")))
    assert _completed(_readonly(environment, request, key)) == first


def test_grant_keeps_attempt_ordinal_when_physical_row_id_is_different(environment):
    owned, repository = environment
    intent = AttemptIntent("control", 2, OperationKind.STOP, AttemptTarget(activity_id=2), None, CONFIG, occurred_at=_NOW)
    _completed(OperationRepository().begin_attempt(intent, new_operation_key(), owned))
    key = new_operation_key()
    first = _completed(repository.grant_start(_request(), key, owned))
    assert first.ticket == AttemptTicket(1, "control", "1", "start/1", 2)
    with closing(owned.connection.execute("SELECT id FROM operation_attempts WHERE run_id = 2")) as cursor:
        assert cursor.fetchone()[0] == 2
    assert _completed(_readonly(environment, _request(), key)) == first


def test_grant_uses_original_activity_identity_when_it_differs_from_action(environment):
    owned, repository = environment
    owned.connection.execute("UPDATE device_activities SET id = 42 WHERE action_id = 1")
    key = new_operation_key()
    first = _completed(repository.grant_start(_request(), key, owned))
    assert first.ticket == AttemptTicket(1, "control", "42", "start/1", 1)
    assert first.activity_id == 42
    assert _completed(_readonly(environment, _request(), key)) == first


@pytest.mark.parametrize("ended", [False, True])
def test_new_grant_rejects_unreliable_existing_run_before_qualification(environment, ended):
    owned, _ = environment
    _, _, first = _original(environment)
    _finish_for_retry(owned, first.ticket, run_finish=(RunFinish(RunOutcome.FAILED,
        ErrorValue("budget_exhausted", "operation")) if ended else None))
    owned.connection.execute("UPDATE operation_runs SET kind = 2 WHERE id = 1")
    result = _readonly(environment, _request(), new_operation_key())
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ValueError), result.error


def _fault_connection(connection, prefix, error, *, committed=False):
    double = create_autospec(sqlite3.Connection, instance=True)
    double.in_transaction = connection.in_transaction

    def execute(sql, parameters=()):
        try:
            if sql.startswith(prefix):
                if committed:
                    connection.execute(sql, parameters)
                raise error
            return connection.execute(sql, parameters)
        finally:
            double.in_transaction = connection.in_transaction

    double.execute.side_effect = execute
    return double


@pytest.mark.parametrize("prefix", ["SELECT * FROM operation_attempts", "SELECT * FROM operation_runs",
                                  "SELECT * FROM device_activities", "SELECT * FROM actions",
                                  "SELECT last_event_id", "SELECT l.change_count"])
def test_grant_original_reads_preserve_sql_failure_and_database(environment, prefix):
    owned, repository = environment
    request, key, first = _original(environment)
    _finish_for_retry(owned, first.ticket)
    error = sqlite3.OperationalError("原授予读取失败")
    proxy = _fault_connection(owned.connection, prefix, error)
    before = tuple(owned.connection.iterdump())
    result = repository.grant_start(request, key, OwnedConnection(proxy, owned.metadata))
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert result.error is error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("prefix", ["INSERT INTO operation_attempts", "INSERT INTO operation_runs",
                                  "UPDATE device_activities", "INSERT INTO entity_event_links"])
def test_grant_write_failure_rolls_back_all_joint_facts(environment, prefix):
    owned, repository = environment
    error = sqlite3.OperationalError("授予共同保存失败")
    proxy = _fault_connection(owned.connection, prefix, error)
    before = tuple(owned.connection.iterdump())
    result = repository.grant_start(_request(), new_operation_key(), OwnedConnection(proxy, owned.metadata))
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert result.error is error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("committed", [False, True])
def test_grant_unknown_commit_is_resolved_by_original_key(environment, committed):
    owned, repository = environment
    key = new_operation_key()
    error = sqlite3.OperationalError("授予提交收据不可用")
    proxy = _fault_connection(owned.connection, "COMMIT", error, committed=committed)
    before = tuple(owned.connection.iterdump())
    result = repository.grant_start(_request(), key, OwnedConnection(proxy, owned.metadata))
    assert result.kind is DbOutcomeKind.UNKNOWN
    assert result.error is error
    if owned.connection.in_transaction:
        owned.connection.rollback()
    if not committed:
        assert tuple(owned.connection.iterdump()) == before
    first = _completed(repository.grant_start(_request(), key, owned))
    assert first.ticket == AttemptTicket(1, "control", "1", "start/1", 1)
    _finish_for_retry(owned, first.ticket)
    _completed(repository.grant_start(replace(_request(), config=NEW_CONFIG), new_operation_key(), owned))
    assert _completed(_readonly(environment, _request(), key)) == first


def test_grant_rejects_original_key_with_multiple_complete_event_members(environment):
    owned, _ = environment
    request, key, first = _original(environment)
    _finish_for_retry(owned, first.ticket)
    with closing(owned.connection.execute("SELECT id, last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1")) as cursor:
        later, last = cursor.fetchone()
    with closing(owned.connection.execute("SELECT id FROM history_transactions WHERE operation_key = ?", (str(key),))) as cursor:
        original = cursor.fetchone()[0]
    owned.connection.execute("UPDATE history_events SET transaction_id = ? WHERE transaction_id = ?", (original, later))
    owned.connection.execute("DELETE FROM history_transactions WHERE id = ?", (later,))
    owned.connection.execute("UPDATE history_transactions SET last_event_id = ? WHERE id = ?", (last, original))
    result = _readonly(environment, request, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ValueError), result.error
