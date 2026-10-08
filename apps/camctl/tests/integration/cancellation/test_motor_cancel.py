"""电机四种取消寻址共享资格，并原子结束本地未发送意图。"""

from dataclasses import replace
from pathlib import Path

import pytest

from camctl.cancellation.models import (
    ApplyCancelTarget,
    CancelApplyMode,
    CancelTarget,
    FixCancelTargets,
    ResolvedTargets,
    StartCancelAction,
    TargetFacts,
)
from camctl.cancellation.rules import (
    CancelEligibility,
    decide_cancel_eligibility,
    load_eligibility_facts,
)
from camctl.cancellation.targets import prepare_cancel_set, resolve_cancel_target
from camctl.contracts.values import new_operation_key
from camctl.motor.models import PrepareSendRequest, SendOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import (
    CancellationRepository,
    SqliteCancelLookup,
    register_cancellation_guards,
)
from camctl.persistence.repositories.motor import MotorRepository, register_motor_guards
from ..persistence.test_motor_transactions import NOW, _admit, _body, owned


@pytest.fixture
def motor_cancel(owned):
    from camctl.persistence.repositories.outputs import register_outputs_guards

    register_outputs_guards()
    register_cancellation_guards()
    register_motor_guards()
    body = _body()
    body["actions"][0]["group"] = "g"
    _admit(owned, body)
    cancel = {
        "request_id": "102",
        "created_at": "2026-10-08 08:00:00",
        "name": "取消",
        "actions": [
            {
                "name": "取消",
                "type": "cancel_task",
                "params": {"target": {"action_instance_id": "1"}},
            }
        ],
    }
    _admit(owned, cancel)
    motor = MotorRepository()
    prepared = motor.prepare_send(
        PrepareSendRequest(1, NOW, NOW), new_operation_key(), owned
    )
    assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
    return owned, motor, prepared.value.permit


@pytest.mark.parametrize(
    "target",
    [
        CancelTarget(request_id="101"),
        CancelTarget(plan_instance_id=1),
        CancelTarget(action_instance_id=1),
        CancelTarget(plan_instance_id=1, group="g"),
    ],
)
@pytest.mark.parametrize("local", [False, True])
def test_four_entries_share_motor_permission(motor_cancel, target, local):
    owned, motor, permit = motor_cancel
    ids = resolve_cancel_target(target, SqliteCancelLookup(owned.connection)).action_ids
    assert ids == (1,)
    facts = load_eligibility_facts(
        owned.connection, 1, motor_permits={1: permit} if local else {}
    )
    expected = (
        CancelEligibility.ALLOW_PRE_START
        if local
        else CancelEligibility.REJECT_UNSUPPORTED
    )
    assert decide_cancel_eligibility(facts) is expected


def test_local_intent_cancel_has_no_persisted_unknown_gap(motor_cancel):
    owned, motor, permit = motor_cancel
    repository = CancellationRepository(motor_permits={1: permit})
    start = repository.start_cancel_action(
        StartCancelAction(2, NOW + 1), new_operation_key(), owned
    )
    assert start.kind is DbOutcomeKind.COMPLETED, start.error
    fixed = prepare_cancel_set(
        2, ResolvedTargets(direct=(TargetFacts(1, False, True),))
    )
    saved = repository.fix_cancel_targets(
        FixCancelTargets(2, fixed, NOW + 2), new_operation_key(), owned
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    command = ApplyCancelTarget(
        saved.value.item_ids[0], CancelApplyMode.PRE_START, NOW + 3
    )
    key = new_operation_key()
    result = repository.apply_cancel_target(command, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    facts = motor.read_facts(1, owned)
    assert (
        facts.action["status"],
        facts.action["cancel_requested"],
        facts.notification.outcome,
    ) == (6, 1, SendOutcome.NOT_SENT)
    assert facts.notification.written_bytes is None
    again = CancellationRepository().apply_cancel_target(command, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert owned.connection.execute(
        "SELECT status FROM plans WHERE id=1"
    ).fetchone() == (3,)

    from unittest.mock import Mock
    from camctl.contracts.clock import ClockPort
    from camctl.motor.notification import NotificationWriter
    from camctl.motor.rules import MotorDecision
    from camctl.motor.service import MotorRuntime, advance_motor

    writer = Mock(spec=NotificationWriter)
    clock = Mock(spec=ClockPort)
    policy = Mock()
    permits = {1: permit}
    decision = advance_motor(
        1, MotorRuntime(owned, motor, writer, clock, policy, permits)
    )
    assert decision is MotorDecision.KEEP_TERMINAL and permits == {}
    writer.send.assert_not_called()
    clock.utc_micros.assert_not_called()
    policy.assert_not_called()


def _fixed_motor_item(motor_cancel, repository):
    owned, _, _ = motor_cancel
    started = repository.start_cancel_action(
        StartCancelAction(2, NOW + 1), new_operation_key(), owned
    )
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = prepare_cancel_set(
        2, ResolvedTargets(direct=(TargetFacts(1, False, True),))
    )
    saved = repository.fix_cancel_targets(
        FixCancelTargets(2, fixed, NOW + 2), new_operation_key(), owned
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    return saved.value.item_ids[0]


@pytest.mark.parametrize("stage", ["apply", "record"])
def test_verify_motor_cancel_operation_preserves_original_result(motor_cancel, stage):
    from camctl.cancellation.models import RecordCancelResult

    owned, _, permit = motor_cancel
    repository = CancellationRepository(motor_permits={1: permit})
    item_id = _fixed_motor_item(motor_cancel, repository)
    key = new_operation_key()
    if stage == "apply":
        request = ApplyCancelTarget(item_id, CancelApplyMode.PRE_START, NOW + 3)
        result = repository.apply_cancel_target(request, key, owned)
        changed = replace(request, mode=CancelApplyMode.TERMINAL)
    else:
        request = RecordCancelResult(
            item_id,
            NOW + 3,
            code="task_cancel_unsupported",
            details={"action_instance_id": "1"},
        )
        result = repository.record_cancel_result(request, key, owned)
        changed = replace(request, details={"action_instance_id": "2"})
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    verified = repository.verify_motor_operation(request, key, owned)
    assert verified.kind is DbOutcomeKind.COMPLETED, verified.error
    assert verified.value.disposition.name == "ALREADY"
    mismatch = repository.verify_motor_operation(changed, key, owned)
    assert mismatch.kind is DbOutcomeKind.UNKNOWN
    absent = repository.verify_motor_operation(request, new_operation_key(), owned)
    assert absent.kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.in_transaction is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["apply", "record"])
async def test_cancel_unknown_reopens_verifies_and_stops_before_settlement(
    motor_cancel, monkeypatch, stage
):
    import sqlite3
    from unittest.mock import AsyncMock, Mock
    from camctl.cancellation.ports import TargetSettlementPort
    from camctl.cancellation.service import (
        ApplyCancel,
        CancellationRuntime,
        apply_cancel,
    )
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    owned, _, permit = motor_cancel
    permits = {1: permit} if stage == "apply" else {}
    repository = CancellationRepository(motor_permits=permits)
    item_id = _fixed_motor_item(motor_cancel, repository)
    path = owned.connection.execute("PRAGMA database_list").fetchone()[2]
    method = "apply_cancel_target" if stage == "apply" else "record_cancel_result"
    original = getattr(repository, method)
    requests = []

    def unknown(request, key, connection):
        saved = original(request, key, connection)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        requests.append((request, key))
        return DbOutcome(
            DbOutcomeKind.UNKNOWN,
            error=sqlite3.OperationalError("commit result unknown"),
        )

    monkeypatch.setattr(repository, method, unknown)
    verified = []

    def verify(request, key, connection):
        verified.append((request, key))
        return CancellationRepository(motor_permits=permits).verify_motor_operation(
            request, key, connection
        )

    monkeypatch.setattr(repository, "verify_motor_operation", verify)
    opened = []

    def reopen():
        fresh = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
        opened.append(fresh)
        return fresh

    settlement = Mock(spec=TargetSettlementPort)
    settlement.settle = AsyncMock()
    runtime = CancellationRuntime(
        owned,
        repository,
        settlement,
        lambda: NOW + 3,
        motor_permits=permits,
        open_connection=reopen,
    )
    with pytest.raises(ConsistencyError) as stopped:
        await apply_cancel(ApplyCancel(2, (item_id,)), runtime)
    assert requests == verified and len(verified) == 1, str(stopped.value)
    assert len(opened) == 1
    settlement.settle.assert_not_called()
    for closed in (owned, *opened):
        with pytest.raises(sqlite3.ProgrammingError):
            closed.connection.execute("SELECT 1")
