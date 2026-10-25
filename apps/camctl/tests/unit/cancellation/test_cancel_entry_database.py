"""取消目标固定时只把当前入口阶段交给完整事务。"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.cancellation.models import (
    CancelTargetsDisposition,
    CancelTargetsSaved,
)
from camctl.cancellation.rules import DispatchPhase, EligibilityFacts, LoadedEligibility
from camctl.cancellation.targets import TargetResolution
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.start_entry import EntryBlock, EntryPhase, StartEntryCoordinator
from camctl.operations.models import AttemptTicket
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture_facts import StartFacts
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database


_ACTION_ID = 7
_PREVIEW_ID = 9
_CANCEL_ACTION_ID = 3


def _world(mocker, *, enter_during_read):
    metadata = DatabaseMetadata(
        "camctl", "d" * 32, 5, "/staging", "/ready", "/processing")
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    owned = OwnedConnection(connection, metadata)
    database = inline_business_database(owned)

    entries = StartEntryCoordinator()
    permit = entries.prepare(metadata.instance_id, _ACTION_ID)
    ticket = AttemptTicket(
        1, "control", str(_ACTION_ID), f"start/{_ACTION_ID}", 11)
    permit.bind(metadata.instance_id, ticket)

    action = {
        "id": _ACTION_ID,
        "type": 1,
        "status": 2,
        "cancel_requested": 0,
        "execution_started": 1,
        "driver_id": DRIVER_ID,
        "execution_spec_json": {"output_storage": "host"},
    }
    loaded = LoadedEligibility(
        action=action,
        facts=EligibilityFacts(
            terminal=False,
            cancel_applied=False,
            dispatch=DispatchPhase.START_PENDING,
            stop_supported=False,
        ),
        entered_dispatch=DispatchPhase.START_PENDING,
    )

    def load_inputs(current_connection, action_id, *, motor_permit=None):
        assert current_connection is connection
        assert action_id == _ACTION_ID
        assert motor_permit is None
        if enter_during_read:
            permit.enter(ticket)
        return loaded

    mocker.patch(
        "camctl.cancellation.rules.load_eligibility_inputs",
        autospec=True,
        side_effect=load_inputs,
    )
    mocker.patch(
        "camctl.cancellation.targets.resolve_cancel_target",
        autospec=True,
        return_value=TargetResolution(action_ids=(_ACTION_ID,)),
    )
    mocker.patch(
        "camctl.persistence.repositories.cancellation.sqlite_auto_candidates",
        autospec=True,
        return_value=((_ACTION_ID, _PREVIEW_ID),),
    )
    mocker.patch(
        "camctl.persistence.repositories.capture_facts.load_start_facts",
        autospec=True,
        return_value=StartFacts(
            {"id": _ACTION_ID}, {"id": ticket.run_id,
             "action_id": _ACTION_ID, "activity_id": _ACTION_ID},
            ticket.attempt_id, False, 2),
    )

    repository = create_autospec(
        CancellationRepository, instance=True, spec_set=True)
    saves = []

    def save(request, key, current_owned, *, reserved_entries=()):
        assert current_owned is owned
        if request.entry_blocks:
            assert permit.phase is EntryPhase.RESERVED
            assert entries.pending_cancels[_CANCEL_ACTION_ID] == (request, key)
            assert reserved_entries == (
                (request.entry_blocks[0], EntryPhase.RESERVED),)
        saves.append((request, key, reserved_entries))
        item_ids = tuple(target.action_id for target in request.targets.targets)
        return DbOutcome(
            kind=DbOutcomeKind.COMPLETED,
            value=CancelTargetsSaved(CancelTargetsDisposition.SAVED, item_ids),
        )

    repository.fix_cancel_targets.side_effect = save
    return metadata, database, entries, permit, ticket, repository, saves


@pytest.mark.asyncio
async def test_entered_during_read_does_not_link_preview_or_create_entry_block(mocker):
    _metadata, database, entries, permit, _ticket, repository, saves = _world(
        mocker, enter_during_read=True)

    item_ids = await _resolve_and_fix(database, repository, entries)

    request, _key, reserved_entries = saves[0]
    assert item_ids == (_ACTION_ID,)
    assert tuple(target.action_id for target in request.targets.targets) == (_ACTION_ID,)
    assert request.entry_blocks == ()
    assert reserved_entries == ()
    assert permit.phase is EntryPhase.ENTERED
    assert (_CANCEL_ACTION_ID not in entries.pending_cancels)
    repository.fix_cancel_targets.assert_called_once()


@pytest.mark.asyncio
async def test_ready_entry_is_reserved_as_tuple_and_blocked_after_commit(mocker):
    metadata, database, entries, permit, ticket, repository, saves = _world(
        mocker, enter_during_read=False)

    item_ids = await _resolve_and_fix(database, repository, entries)

    request, _key, reserved_entries = saves[0]
    block = EntryBlock(metadata.instance_id, _ACTION_ID, ticket)
    assert item_ids == (_ACTION_ID, _PREVIEW_ID)
    assert tuple(target.action_id for target in request.targets.targets) == (
        _ACTION_ID, _PREVIEW_ID)
    assert request.entry_blocks == (block,)
    assert reserved_entries == ((block, EntryPhase.RESERVED),)
    assert isinstance(reserved_entries, tuple)
    assert _CANCEL_ACTION_ID not in entries.pending_cancels
    assert permit.phase is EntryPhase.BLOCKED
    repository.fix_cancel_targets.assert_called_once()


async def _resolve_and_fix(database, repository, entries):
    from camctl.bootstrap.flows import _resolve_and_fix

    spec_json = json.dumps({"params": {"target": {
        "action_instance_id": str(_ACTION_ID)}}})
    return await _resolve_and_fix(
        database,
        repository,
        _CANCEL_ACTION_ID,
        spec_json,
        lambda: 10,
        start_entries=entries,
    )
