"""取回选择使用本批直接事实等待来源，不提交已知无资格的写事务。"""

from types import SimpleNamespace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.devices.tasks import CaptureResultPolicy
from camctl.outputs import definitions, obtain_flow
from camctl.outputs.read_discovery import ReadDiscoveryOwner
from camctl.outputs.sources import SelectionSnapshot
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories import segment_selections
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection


pytestmark = pytest.mark.asyncio


def _row(*, status=2, policy=CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value, processing=None):
    return (11, 105, 106, status, policy,
        *(processing if processing is not None else (None, None, None, None)))


@pytest.fixture
def world(monkeypatch):
    owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "a" * 32, 6, "/staging", "/ready", "/processing"))
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    discovery = create_autospec(ReadDiscoveryOwner, instance=True, spec_set=True)
    outputs = create_autospec(OutputsRepository, instance=True, spec_set=True)
    runtime = SimpleNamespace(database=database, discovery=discovery, outputs=outputs,
        pending_segment_selections={})
    database.read.side_effect = lambda callback, **options: callback(owned)
    database.write.side_effect = lambda callback, **options: callback(owned)
    discovery.rows.return_value = Page((_row(),), None)
    saved = segment_selections.SegmentSelectionSaved(0,
        segment_selections.SegmentSelectionDiagnosticRef(OperationKey("a" * 32),
            TransactionRange(3, 10, 10), 11, 106))
    fix = create_autospec(segment_selections.fix_segment_selection,
        return_value=DbOutcome(DbOutcomeKind.COMPLETED, saved))
    monkeypatch.setattr(segment_selections, "fix_segment_selection", fix)
    consume = create_autospec(obtain_flow._consume_selection_diagnostics)
    monkeypatch.setattr(obtain_flow, "_consume_selection_diagnostics", consume)
    return SimpleNamespace(runtime=runtime, database=database, discovery=discovery,
        outputs=outputs, fix=fix, consume=consume)


@pytest.mark.parametrize("status", [1, 2])
@pytest.mark.parametrize("policy", [
    CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
    CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value,
])
async def test_capturing_segmented_source_does_not_enter_selection_write(world, status, policy):
    world.discovery.rows.return_value = Page((_row(status=status, policy=policy),), None)
    for now in (100, 101, 102):
        await obtain_flow._fix_pending_selections(world.runtime, now)
    world.database.read.assert_not_awaited()
    world.database.write.assert_not_awaited()
    assert not world.runtime.pending_segment_selections


@pytest.mark.parametrize("status", [3, 4, 5, 6])
async def test_source_terminal_change_can_fix_in_the_next_round(world, status):
    await obtain_flow._fix_pending_selections(world.runtime, 100)
    world.database.write.assert_not_awaited()
    world.discovery.rows.return_value = Page((_row(status=status),), None)
    await obtain_flow._fix_pending_selections(world.runtime, 101)
    world.fix.assert_called_once()
    request = world.fix.call_args.args[0]
    assert (request.selection_id, request.occurred_at) == (11, 101)
    world.consume.assert_awaited_once()
    world.database.read.assert_not_awaited()


@pytest.mark.parametrize("column,state", [
    ("check_state", "RUNNING"), ("repair_state", "PENDING"), ("repair_state", "RUNNING"),
    ("discard_state", "PENDING"), ("discard_state", "RUNNING"),
])
async def test_unfinished_source_processing_does_not_enter_selection_write(world, column, state):
    values = {name: int(enum_for(f"recording_processing.{name}")[state_name])
        for name, state_name in (("check_state", "COMPLETED"),
            ("repair_state", "NOT_NEEDED"), ("discard_state", "NOT_NEEDED"))}
    values[column] = int(enum_for(f"recording_processing.{column}")[state])
    processing = (21, *(values[name] for name in ("check_state", "repair_state", "discard_state")))
    world.discovery.rows.return_value = Page((_row(status=3, processing=processing),), None)
    await obtain_flow._fix_pending_selections(world.runtime, 100)
    world.database.read.assert_not_awaited()
    world.database.write.assert_not_awaited()


async def test_held_original_save_precedes_waiting_for_new_source_facts(world):
    request = segment_selections.FixSegmentSelection(11, 90)
    key = OperationKey("b" * 32)
    world.runtime.pending_segment_selections[11] = obtain_flow.PendingSegmentSelection(request, key)

    async def discover(*args, **options):
        world.fix.assert_called_once()
        assert world.fix.call_args.args[:2] == (request, key)
        return Page((_row(),), None)

    world.discovery.rows.side_effect = discover
    await obtain_flow._fix_pending_selections(world.runtime, 100)
    world.database.write.assert_awaited_once()
    assert not world.runtime.pending_segment_selections


async def test_already_saved_receipt_is_consumed_before_empty_discovery(world):
    request = segment_selections.FixSegmentSelection(11, 90)
    saved = world.fix.return_value.value
    world.runtime.pending_segment_selections[11] = obtain_flow.PendingSegmentSelection(
        request, OperationKey("b" * 32), saved=saved)
    world.discovery.rows.return_value = Page((), None)
    await obtain_flow._fix_pending_selections(world.runtime, 100)
    world.consume.assert_awaited_once()
    world.database.write.assert_not_awaited()
    assert not world.runtime.pending_segment_selections


@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_original_save_error_stops_before_new_discovery(world, kind):
    request = segment_selections.FixSegmentSelection(11, 90)
    pending = obtain_flow.PendingSegmentSelection(request, OperationKey("b" * 32))
    world.runtime.pending_segment_selections[11] = pending
    world.fix.return_value = DbOutcome(kind, error=ConsistencyError("原选择未可靠保存"))
    with pytest.raises(ConsistencyError):
        await obtain_flow._fix_pending_selections(world.runtime, 100)
    world.discovery.rows.assert_not_awaited()
    if kind is DbOutcomeKind.UNKNOWN:
        assert world.runtime.pending_segment_selections[11] is pending


@pytest.mark.parametrize("status", [2, 3])
async def test_nonsegmented_source_keeps_ordinary_selection_path(world, monkeypatch, status):
    world.discovery.rows.return_value = Page((_row(status=status, policy=None),), None)
    action = dict(id=106, execution_spec_json="{}", input_fields_json='{"params":{}}', resolved_source_plan_id=20)
    monkeypatch.setattr(obtain_flow, "_action_facts", create_autospec(
        obtain_flow._action_facts, return_value=action))
    monkeypatch.setattr(definitions, "read_selection_request", create_autospec(
        definitions.read_selection_request, return_value=(definitions.SelectionMode.DEFAULT, ())))
    facts = object()
    monkeypatch.setattr(obtain_flow, "load_selection_facts", create_autospec(
        obtain_flow.load_selection_facts, return_value=facts))
    snapshot = SelectionSnapshot(status == 3)
    monkeypatch.setattr(obtain_flow, "select_outputs", create_autospec(
        obtain_flow.select_outputs, return_value=snapshot))
    world.outputs.fix_selection.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    await obtain_flow._fix_pending_selections(world.runtime, 100)
    world.fix.assert_not_called()
    obtain_flow.select_outputs.assert_called_once()
    if snapshot.is_fixed:
        world.outputs.fix_selection.assert_called_once()
        request = world.outputs.fix_selection.call_args.args[0]
        assert request.selection_id == 11 and request.snapshot is snapshot
    else:
        world.outputs.fix_selection.assert_not_called()


async def test_invalid_processing_state_remains_an_error(world):
    world.discovery.rows.return_value = Page((_row(status=3, processing=(21, 99, 2, 1)),), None)
    with pytest.raises(ConsistencyError):
        await obtain_flow._fix_pending_selections(world.runtime, 100)
    world.database.write.assert_not_awaited()


async def test_empty_discovery_does_not_create_new_selection(world):
    world.discovery.rows.return_value = Page((), None)
    await obtain_flow._fix_pending_selections(world.runtime, 100)
    world.database.read.assert_not_awaited()
    world.database.write.assert_not_awaited()
