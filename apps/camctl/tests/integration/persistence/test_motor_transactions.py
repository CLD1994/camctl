"""真实受理、专属发送事实与权威历史事务的共同提交。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode, PlanDisposition, ProcessInput
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.values import new_operation_key, to_utc_micros
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_runtime import _create_valid_database

NOW = to_utc_micros("2026-10-08 09:00:00")


def _body(*, count=1, position=100):
    return {
        "request_id": "101",
        "created_at": "2026-10-08 08:00:00",
        "name": "定位",
        "actions": [
            {
                "name": f"定位{i}",
                "type": "motor_control",
                "scheduled_at": "2026-10-08 09:00:00",
                "params": {"position": position},
                "policy": {"max_delay_ms": 1000},
            }
            for i in range(count)
        ],
    }


@pytest.fixture
def owned(tmp_path):
    _create_valid_database(tmp_path / "state.db")
    value = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    register_acceptance_guards()
    yield value
    value.connection.close()


def _admit(owned, body=None):
    catalog = build_catalog(load_config({}, ConfigDefaults()), DriverDefinitions({}))
    outcome = AcceptanceRepository().process_input(
        ProcessInput(
            ParsedInput("motor.json", body or _body()), catalog, CommandMode.RUN, NOW
        ),
        new_operation_key(),
        owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.plan_disposition is PlanDisposition.REGISTERED
    return outcome


def test_empty_device_configuration_accepts_motor(owned):
    _admit(owned)
    row = owned.connection.execute(
        "SELECT type,status,execution_spec_json,max_delay_ms,device_id,driver_id,effective_params_json FROM actions"
    ).fetchone()
    assert row == (8, 1, "{}", 1000, None, None, None)


@pytest.fixture
def prepared(owned):
    from camctl.motor.models import PrepareSendRequest
    from camctl.persistence.repositories.motor import (
        MotorRepository,
        register_motor_guards,
    )

    register_motor_guards()
    _admit(owned)
    repository = MotorRepository()
    key = new_operation_key()
    request = PrepareSendRequest(1, NOW, NOW)
    outcome = repository.prepare_send(request, key, owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.outcome.name == "GRANTED"
    assert outcome.value.permit is not None
    return owned, repository, request, key, outcome.value.permit


def test_prepare_commits_unique_intent_with_action_start(prepared):
    owned, repository, request, key, permit = prepared
    facts = repository.read_facts(1, owned)
    assert facts.action["status"] == 2
    assert facts.action["execution_started"] == 1
    assert facts.notification.outcome.name == "PENDING"
    assert facts.notification.intent_operation_key == str(key)
    assert owned.connection.execute(
        "SELECT count(*) FROM device_activities"
    ).fetchone() == (0,)
    again = repository.prepare_send(request, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED
    assert again.value.outcome.name == "ALREADY"
    assert again.value.permit is None
    assert owned.connection.execute(
        "SELECT count(*) FROM motor_notifications"
    ).fetchone() == (1,)
    other = repository.prepare_send(request, new_operation_key(), owned)
    assert other.value.outcome.name == "ALREADY" and other.value.permit is None


def test_same_prepare_key_rejects_changed_input(prepared):
    owned, repository, request, key, permit = prepared
    outcome = repository.prepare_send(
        replace(request, trusted_wall_now=NOW + 1), key, owned
    )
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK


@pytest.mark.parametrize(
    "kind,status,send_outcome,fields",
    [
        ("WRITTEN", 3, "WRITTEN", {"written_bytes": 78}),
        (
            "FAILED",
            4,
            "FAILED",
            {"written_bytes": 0, "errno": 32, "reason": "broken_pipe"},
        ),
        ("UNCONFIRMED", 4, "UNCONFIRMED", {}),
        ("EXPIRED", 5, "NOT_SENT", {}),
    ],
)
def test_finish_commits_result_terminal_plan_and_reports(
    prepared, kind, status, send_outcome, fields
):
    from camctl.motor.models import FinishSendRequest, MotorFinalKind
    from camctl.scheduling.rules import ExpirationReason

    owned, repository, request, key, permit = prepared
    kwargs = dict(
        action_id=1,
        occurred_at=NOW + 2_000_000,
        kind=MotorFinalKind[kind],
        permit=None if kind == "UNCONFIRMED" else permit,
        **fields,
    )
    if kind == "EXPIRED":
        kwargs["expiration_reason"] = ExpirationReason.WINDOW_EXHAUSTED
    finish = FinishSendRequest(**kwargs)
    outcome = repository.finish_send(finish, new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    facts = repository.read_facts(1, owned)
    assert facts.action["status"] == status
    assert facts.notification.outcome.name == send_outcome
    assert owned.connection.execute("SELECT status FROM plans").fetchone() == (3,)
    assert (
        owned.connection.execute(
            "SELECT count(*) FROM report_entity_changes WHERE entity_type=1 AND entity_id=1"
        ).fetchone()[0]
        >= 3
    )


def test_terminal_finish_reuses_original_key_and_rejects_changed_result(prepared):
    from camctl.motor.models import FinishSendRequest, MotorFinalKind

    owned, repository, _, _, permit = prepared
    request = FinishSendRequest(
        1, NOW + 1, MotorFinalKind.WRITTEN, permit, written_bytes=78
    )
    key = new_operation_key()
    first = repository.finish_send(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    second = repository.finish_send(request, key, owned)
    assert second.kind is DbOutcomeKind.COMPLETED, second.error
    assert second.value.reused is True
    third = repository.finish_send(replace(request, written_bytes=79), key, owned)
    assert third.kind is DbOutcomeKind.ROLLED_BACK


def test_success_requires_local_permission(prepared):
    from camctl.motor.models import FinishSendRequest, MotorFinalKind

    owned, repository, _, _, _ = prepared
    request = FinishSendRequest(1, NOW + 1, MotorFinalKind.WRITTEN, written_bytes=78)
    outcome = repository.finish_send(request, new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert repository.read_facts(1, owned).action["status"] == 2


def test_unknown_recovery_finishes_without_new_permission(prepared):
    from camctl.motor.models import FinishSendRequest, MotorFinalKind

    owned, repository, _, _, _ = prepared
    outcome = repository.finish_send(
        FinishSendRequest(1, NOW + 1, MotorFinalKind.UNCONFIRMED),
        new_operation_key(),
        owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    facts = repository.read_facts(1, owned)
    assert facts.action["error_code"] == 29
    assert facts.action["error_details_json"] == {}
    assert facts.notification.written_bytes is None and facts.notification.errno is None


def test_sql_rejects_unknown_outcome_and_inconsistent_completion(prepared):
    owned, repository, _, _, _ = prepared
    for sql in [
        "UPDATE motor_notifications SET outcome=99",
        "UPDATE motor_notifications SET written_bytes=1",
        "UPDATE motor_notifications SET intent_at=NULL",
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(sql)
        owned.connection.rollback()


def test_read_rejects_cross_record_state_inconsistency(prepared):
    from camctl.contracts.values import ConsistencyError

    owned, repository, _, _, _ = prepared
    owned.connection.execute("UPDATE actions SET status=3")
    with pytest.raises(ConsistencyError):
        repository.read_facts(1, owned)


def test_two_motor_results_complete_parent_only_after_last_action(owned):
    from camctl.motor.models import (
        FinishSendRequest,
        MotorFinalKind,
        PrepareSendRequest,
    )
    from camctl.persistence.repositories.motor import (
        MotorRepository,
        register_motor_guards,
    )

    register_motor_guards()
    _admit(owned, _body(count=2))
    repository = MotorRepository()
    for action_id, expected_plan in ((1, 2), (2, 3)):
        prepared = repository.prepare_send(
            PrepareSendRequest(action_id, NOW, NOW), new_operation_key(), owned
        )
        assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
        result = repository.finish_send(
            FinishSendRequest(
                action_id,
                NOW + 1,
                MotorFinalKind.WRITTEN,
                prepared.value.permit,
                written_bytes=78,
            ),
            new_operation_key(),
            owned,
        )
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert owned.connection.execute("SELECT status FROM plans").fetchone() == (
            expected_plan,
        )


@pytest.mark.parametrize(
    "when,reason", [(NOW - 1, "too_early"), (NOW + 1_000_001, "window_ended")]
)
def test_time_rejection_never_creates_intent(owned, when, reason):
    from camctl.motor.models import PrepareSendRequest, PrepareOutcome
    from camctl.persistence.repositories.motor import (
        MotorRepository,
        register_motor_guards,
    )

    register_motor_guards()
    _admit(owned)
    outcome = MotorRepository().prepare_send(
        PrepareSendRequest(1, when, NOW), new_operation_key(), owned
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert (
        outcome.value.outcome is PrepareOutcome.REJECTED
        and outcome.value.reason == reason
    )
    assert owned.connection.execute(
        "SELECT count(*) FROM motor_notifications"
    ).fetchone() == (0,)


def test_restoring_old_boundary_preserves_intent_and_public_results(prepared):
    from camctl.motor.models import FinishSendRequest, MotorFinalKind
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.contracts.public_projection import ProjectionInput, project_public

    owned, repository, _, _, permit = prepared
    path = owned.connection.execute("PRAGMA database_list").fetchone()[2]
    history = HistoryRepository(path)
    old_boundary = history.current_boundary()
    result = repository.finish_send(
        FinishSendRequest(1, NOW + 1, MotorFinalKind.WRITTEN, permit, written_bytes=78),
        new_operation_key(),
        owned,
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    current_boundary = history.current_boundary()
    before = history.restore_entity("action", 1, old_boundary, event_batch_size=1)
    after = history.restore_entity("action", 1, current_boundary, event_batch_size=1)
    assert before[("motor_notifications", 1)]["outcome"] == 1
    assert before[("actions", 1)]["status"] == 2
    assert after[("motor_notifications", 1)]["outcome"] == 3
    assert after[("actions", 1)]["status"] == 3
    public = project_public(
        ProjectionInput("action", 1, {"actions": {1: after[("actions", 1)]}})
    )
    assert public["type"] == "motor_control" and public["status"] == "succeeded"
    assert public["input_params"] == {"position": 100} and public["policy"] == {
        "max_delay_ms": 1000
    }
    assert (
        not {
            "device_execution",
            "effective_params",
            "result_params",
            "outputs",
            "deliveries",
            "device_id",
        }
        & public.keys()
    )


@pytest.mark.parametrize("change", ["missing_key", "wrong_action", "missing_notice"])
def test_read_verifies_original_intent_transaction(prepared, change):
    import json
    from camctl.contracts.values import ConsistencyError

    owned, repository, _, key, _ = prepared
    if change == "missing_key":
        owned.connection.execute(
            "UPDATE history_transactions SET operation_key=? WHERE operation_key=?",
            ("a" * 32, str(key)),
        )
    else:
        row = owned.connection.execute(
            "SELECT e.id,e.body_json FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id WHERE t.operation_key=?",
            (str(key),),
        ).fetchone()
        body = json.loads(row[1])
        if change == "wrong_action":
            body["evidence"]["request"]["action_id"] = 2
        else:
            body["rows"] = [
                row for row in body["rows"] if row["table"] != "motor_notifications"
            ]
        owned.connection.execute(
            "UPDATE history_events SET body_json=? WHERE id=?",
            (json.dumps(body), row[0]),
        )
    with pytest.raises(ConsistencyError):
        repository.read_facts(1, owned)


def test_pending_intent_cannot_carry_separate_cancel_marker(prepared):
    from camctl.contracts.values import ConsistencyError

    owned, repository, _, _, _ = prepared
    owned.connection.execute("UPDATE actions SET cancel_requested=1")
    with pytest.raises(ConsistencyError):
        repository.read_facts(1, owned)


def test_read_rejects_terminal_result_missing_its_notification_history(prepared):
    import json
    from camctl.contracts.values import ConsistencyError
    from camctl.motor.models import FinishSendRequest, MotorFinalKind

    owned, repository, _, _, permit = prepared
    result = repository.finish_send(
        FinishSendRequest(1, NOW + 1, MotorFinalKind.WRITTEN, permit, written_bytes=78),
        new_operation_key(),
        owned,
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    row = owned.connection.execute(
        "SELECT id,body_json FROM history_events WHERE event_type=34 AND json_extract(body_json,'$.reason')=2"
    ).fetchone()
    body = json.loads(row[1])
    body["rows"] = [
        change for change in body["rows"] if change["table"] != "motor_notifications"
    ]
    owned.connection.execute(
        "UPDATE history_events SET body_json=? WHERE id=?", (json.dumps(body), row[0])
    )
    with pytest.raises(ConsistencyError):
        repository.read_facts(1, owned)


def test_reuse_rejects_terminal_result_missing_its_notification_history(prepared):
    import json
    from camctl.motor.models import FinishSendRequest, MotorFinalKind

    owned, repository, _, _, permit = prepared
    key = new_operation_key()
    request = FinishSendRequest(
        1, NOW + 1, MotorFinalKind.WRITTEN, permit, written_bytes=78
    )
    result = repository.finish_send(request, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    row = owned.connection.execute(
        "SELECT id,body_json FROM history_events WHERE event_type=34 AND json_extract(body_json,'$.reason')=2"
    ).fetchone()
    body = json.loads(row[1])
    body["rows"] = [
        change for change in body["rows"] if change["table"] != "motor_notifications"
    ]
    owned.connection.execute(
        "UPDATE history_events SET body_json=? WHERE id=?", (json.dumps(body), row[0])
    )
    reused = repository.finish_send(request, key, owned)
    assert reused.kind is DbOutcomeKind.ROLLED_BACK


@pytest.mark.parametrize("request_kind", ["prepare", "finish", "observe"])
def test_verify_absent_operation_is_read_only_not_committed(
    prepared, monkeypatch, request_kind
):
    from camctl.motor.models import (
        PrepareSendRequest,
        FinishSendRequest,
        MotorFinalKind,
    )
    from camctl.persistence.repositories.motor import _MotorCommand
    from camctl.persistence.repositories.scheduling import ObserveWindowRequest

    owned, repository, request, _, permit = prepared
    requests = {
        "prepare": request,
        "finish": FinishSendRequest(
            1, NOW + 1, MotorFinalKind.WRITTEN, permit, written_bytes=78
        ),
        "observe": ObserveWindowRequest(1, NOW, NOW),
    }

    def forbidden_plan(*args):
        raise AssertionError("核实不得为缺失原键创建新事务")

    monkeypatch.setattr(_MotorCommand, "plan", forbidden_plan)
    counts = owned.connection.execute(
        "SELECT count(*) FROM history_transactions"
    ).fetchone()
    result = repository.verify_operation(
        requests[request_kind], new_operation_key(), owned
    )
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert (
        owned.connection.execute("SELECT count(*) FROM history_transactions").fetchone()
        == counts
    )
    assert owned.connection.in_transaction is False


def test_verify_prepare_preserves_original_receipt_without_permission(prepared):
    owned, repository, request, key, _ = prepared
    result = repository.verify_operation(request, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome.name == "ALREADY" and result.value.permit is None
    assert result.boundary is not None


def test_verify_finish_returns_original_result_read_only(prepared):
    from camctl.motor.models import FinishSendRequest, MotorFinalKind

    owned, repository, _, _, permit = prepared
    request = FinishSendRequest(
        1, NOW + 1, MotorFinalKind.WRITTEN, permit, written_bytes=78
    )
    key = new_operation_key()
    finished = repository.finish_send(request, key, owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    result = repository.verify_operation(request, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.status == 3 and result.value.reused is True
    assert result.boundary == finished.boundary


@pytest.mark.parametrize(
    "changed",
    [{"trusted_wall_now": NOW + 1}, {"action_id": 2}, {"occurred_at": NOW + 1}],
)
def test_verify_prepare_changed_input_is_unknown(prepared, changed):
    owned, repository, request, key, _ = prepared
    result = repository.verify_operation(replace(request, **changed), key, owned)
    assert result.kind is DbOutcomeKind.UNKNOWN and result.error is not None
    assert owned.connection.in_transaction is False


def test_verify_observe_checks_original_wall_time(owned):
    from camctl.persistence.repositories.motor import (
        MotorRepository,
        register_motor_guards,
    )
    from camctl.persistence.repositories.scheduling import (
        ObserveWindowRequest,
        register_window_guard,
    )

    register_motor_guards()
    register_window_guard()
    _admit(owned)
    repository = MotorRepository()
    key = new_operation_key()
    request = ObserveWindowRequest(1, NOW, NOW + 1)
    first = repository.observe_window(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    result = repository.verify_operation(request, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.observed_at == NOW
    wrong = repository.verify_operation(
        replace(request, trusted_wall_now=NOW + 1), key, owned
    )
    assert wrong.kind is DbOutcomeKind.UNKNOWN


def test_verify_read_failure_cannot_be_classified_not_committed(prepared, monkeypatch):
    import camctl.persistence.repositories.motor as module

    owned, repository, request, key, _ = prepared

    def unavailable(*args):
        raise sqlite3.OperationalError("verify failed")

    monkeypatch.setattr(module, "saved_transaction_events", unavailable)
    result = repository.verify_operation(request, key, owned)
    assert result.kind is DbOutcomeKind.UNKNOWN
    assert isinstance(result.error, sqlite3.OperationalError)
    assert owned.connection.in_transaction is False
