"""取消资格事实读取后应用同一事件循环中的 START 入口阶段。"""

from __future__ import annotations

import sqlite3
import json
from unittest.mock import create_autospec

import pytest

from camctl.cancellation.models import RecordCancelResult
from camctl.cancellation.ports import SettlementOutcome, TargetSettlementPort
from camctl.cancellation.rules import (
    CancelEligibility,
    DispatchPhase,
    EligibilityFacts,
    LoadedEligibility,
    current_eligibility_facts,
    decide_cancel_eligibility,
)
from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.start_entry import EntryPhase, StartEntryCoordinator
from camctl.operations.models import AttemptTicket
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.service import LogChannel
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database


def _action(*, action_type=1, status=1, cancel_requested=0):
    return {
        "id": 7,
        "type": action_type,
        "status": status,
        "cancel_requested": cancel_requested,
        "driver_id": DRIVER_ID if action_type == 1 else "other-driver",
        "execution_spec_json": {"output_storage": "host"},
    }


def _facts(dispatch, *, terminal=False, cancel_applied=False, stop_supported=True):
    return EligibilityFacts(
        terminal=terminal,
        cancel_applied=cancel_applied,
        dispatch=dispatch,
        stop_supported=stop_supported,
    )


def _loaded(*, action=None, facts=None, entered_dispatch=DispatchPhase.START_PENDING):
    return LoadedEligibility(
        action=_action() if action is None else action,
        facts=_facts(DispatchPhase.NOT_STARTED) if facts is None else facts,
        entered_dispatch=entered_dispatch,
    )


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (EntryPhase.READY, DispatchPhase.NOT_STARTED),
        (EntryPhase.ENTERED, DispatchPhase.START_PENDING),
    ],
)
def test_current_facts_apply_phase_after_database_snapshot(phase, expected):
    loaded = _loaded()

    facts = current_eligibility_facts(loaded, phase)

    assert facts.dispatch is expected


@pytest.mark.parametrize("phase", [EntryPhase.UNKNOWN, EntryPhase.RESERVED])
def test_unresolved_local_start_entry_rejects_cancel_facts(phase):
    with pytest.raises(ConsistencyError):
        current_eligibility_facts(_loaded(), phase)


@pytest.mark.parametrize("phase", list(EntryPhase))
def test_non_hasselblad_facts_ignore_local_start_entry(phase):
    persisted = _facts(DispatchPhase.NOT_STARTED)
    loaded = _loaded(
        action=_action(action_type=2),
        facts=persisted,
        entered_dispatch=None,
    )

    assert current_eligibility_facts(loaded, phase) is persisted


@pytest.mark.parametrize(
    ("status", "cancel_requested", "dispatch", "terminal", "cancel_applied"),
    [
        (6, 0, DispatchPhase.UNVERIFIED, True, False),
        (1, 1, DispatchPhase.UNVERIFIED, False, True),
    ],
)
def test_terminal_or_applied_cancel_facts_are_not_overridden(
        status, cancel_requested, dispatch, terminal, cancel_applied):
    persisted = _facts(
        dispatch, terminal=terminal, cancel_applied=cancel_applied)
    loaded = _loaded(
        action=_action(status=status, cancel_requested=cancel_requested),
        facts=persisted,
        entered_dispatch=DispatchPhase.START_PENDING,
    )

    assert current_eligibility_facts(loaded, EntryPhase.READY) is persisted
    assert current_eligibility_facts(loaded, EntryPhase.ENTERED) is persisted


@pytest.mark.asyncio
async def test_apply_cancel_uses_start_entry_phase_changed_during_fact_read(mocker):
    item_id, target_id = 19, 7
    metadata = DatabaseMetadata(
        "camctl", "c" * 32, 5, "/staging", "/ready", "/processing")
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)

    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        if sql.startswith("SELECT target_action_id, status, cancellation_effect"):
            cursor.fetchone.return_value = (target_id, 1, 0)
        elif sql.startswith("SELECT type FROM actions"):
            cursor.fetchone.return_value = (1,)
        elif sql.startswith("SELECT status, target_action_id FROM cancel_items"):
            cursor.fetchone.return_value = (4, target_id)
        elif sql.startswith("SELECT status, cancellation_effect, outcome, error_code"):
            cursor.fetchone.return_value = (
                4, 0, None, "task_cancel_unsupported", None, target_id)
        else:
            raise AssertionError(f"意外取消资格测试查询: {sql}")
        return cursor

    connection.execute.side_effect = execute
    owned = OwnedConnection(connection, metadata)
    database = inline_business_database(owned)
    start_entries = StartEntryCoordinator()
    permit = start_entries.prepare(metadata.instance_id, target_id)
    ticket = AttemptTicket(1, "control", str(target_id), f"start/{target_id}", 11)
    permit.bind(metadata.instance_id, ticket)
    loaded = _loaded(action=_action(status=2), facts=_facts(
        DispatchPhase.START_PENDING, stop_supported=False))

    def load_inputs(current_connection, action_id, *, motor_permit=None):
        assert current_connection is connection
        assert action_id == target_id
        assert motor_permit is None
        permit.enter(ticket)
        return loaded

    mocker.patch(
        "camctl.cancellation.service.load_eligibility_inputs",
        autospec=True,
        side_effect=load_inputs,
    )
    repository = create_autospec(
        CancellationRepository, instance=True, spec_set=True)
    repository.record_cancel_result.return_value = DbOutcome(
        kind=DbOutcomeKind.COMPLETED)
    settlement = create_autospec(
        TargetSettlementPort, instance=True, spec_set=True)
    settlement.settle.return_value = SettlementOutcome(complete=False)
    runtime = CancellationRuntime(
        database=database,
        repository=repository,
        settlement=settlement,
        occurred_at=lambda: 10,
        start_entries=start_entries,
    )

    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=3, item_ids=(item_id,)), runtime)

    repository.apply_cancel_target.assert_not_called()
    repository.record_cancel_result.assert_called_once()
    request = repository.record_cancel_result.call_args.args[0]
    assert isinstance(request, RecordCancelResult)
    assert request.code == "task_cancel_unsupported"
    assert permit.phase is EntryPhase.ENTERED
    assert decide_cancel_eligibility(
        current_eligibility_facts(loaded, permit.phase)
    ) is CancelEligibility.REJECT_UNSUPPORTED
    assert progress.items[0].status == 4
    settlement.settle.assert_not_awaited()
    records = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
    assert [(record["stage"], record["event"]) for record in records] == [
        ("cancel.finish_member", "started"), ("cancel.finish_member", "committed"),
        ("cancel.finish_member", "returned")]
    assert records[1]["error_code"] == "task_cancel_unsupported"
    assert all(record["action_id"] == 3 and record["target_action_id"] == target_id
               and record["item_id"] == item_id for record in records)
