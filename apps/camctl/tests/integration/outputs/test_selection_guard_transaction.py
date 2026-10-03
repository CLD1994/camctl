"""正式选择守卫与真实提交、历史解码和回放共同保持保存的完整选择。"""

from contextlib import closing
from dataclasses import replace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.decoding import decode_event_row
from camctl.history.reads import ReadCoverage
from camctl.history.replay import EntityImage, apply_forward, apply_reverse
from camctl.history import validators
from camctl.history.validators import EventValidationError, validate_event
from camctl.outputs.sources import SelectionMode
from camctl.persistence.repositories import outputs
from camctl.persistence.transaction import commit_operation, row_facts

from .test_selection_guard import _proposal, _request, _snapshot, _NOW, selection_database, family_database
from .test_selection_event_sequence import _Sequence
from ..operations.test_result_reuse import _FaultConnection


class _SelectionProposal:
    def __init__(self, command, key, mutation):
        self.command, self.key, self.mutation = command, key, mutation

    def plan(self, scope):
        plan = outputs._FixSelectionCommand(self.command, self.key).plan(scope)
        event, = plan.events
        if self.mutation == "omit":
            event = replace(event, rows=event.rows[:-1])
        elif self.mutation == "swap_ids":
            selection, first, second, *rest = event.rows
            event = replace(event, rows=(selection, replace(first, row_id=second.row_id),
                replace(second, row_id=first.row_id), *rest))
        elif self.mutation == "reverse_rows":
            event = replace(event, rows=tuple(reversed(event.rows)))
        elif self.mutation == "append":
            extra = replace(event.rows[1], row_id=99)
            event = replace(event, rows=(*event.rows, extra))
            plan = replace(plan, owners={**plan.owners, ("obtain_items", 99): ("action", 30)})
        elif self.mutation == "sparse_ids":
            event, owners = _sparse_event(event, plan.owners)
            plan = replace(plan, owners=owners)
        else:
            raise AssertionError(self.mutation)
        return replace(plan, events=(event,))


def _sparse_event(event, owners):
    selection, *items = event.rows
    items = tuple(replace(row, row_id=11 + 7 * index) for index, row in enumerate(items))
    owners = {**owners, **{("obtain_items", row.row_id): ("action", 30) for row in items}}
    return replace(event, rows=(*reversed(items), selection)), owners


@pytest.mark.parametrize("mode", list(SelectionMode))
@pytest.mark.parametrize("mutation", ["omit", "append", "swap_ids", "reverse_rows", "sparse_ids"])
def test_transaction_preserves_complete_saved_order(selection_database, mode, mutation):
    owned = selection_database
    ids = (705, 703, 999, 704) if mode is SelectionMode.EXPLICIT_IDS else ()
    _request(owned.connection, mode, ids)
    command = outputs.FixSelection(61, _snapshot(owned.connection, mode, ids), _NOW)
    before = tuple(owned.connection.iterdump())
    key = new_operation_key()
    receipt = commit_operation(_SelectionProposal(command, key, mutation), key, owned)
    if mutation not in ("reverse_rows", "sparse_ids"):
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert tuple(owned.connection.iterdump()) == before
    else:
        assert receipt.kind == "completed", receipt.error
        with closing(owned.connection.execute(
            "SELECT output_id, requested_output_id FROM obtain_items WHERE selection_id=61 ORDER BY id"
        )) as cursor:
            rows = cursor.fetchall()
        assert rows == {
            SelectionMode.DEFAULT: [(703, None), (705, None)],
            SelectionMode.PREVIEW: [(703, None), (None, None)],
            SelectionMode.EXPLICIT_IDS: [(705, 705), (703, 703), (None, 999), (None, 704)],
        }[mode]
        saved = outputs.load_selection(owned.connection, 61)
        assert [(item.output_id, item.requested_output_id) for item in saved.items] == rows
        if mutation == "sparse_ids":
            with closing(owned.connection.execute("SELECT id FROM obtain_items ORDER BY id")) as cursor:
                assert cursor.fetchall() == ([(11,), (18,), (25,), (32,)]
                    if mode is SelectionMode.EXPLICIT_IDS else [(11,), (18,)])


@pytest.mark.parametrize("mode", list(SelectionMode))
@pytest.mark.parametrize("state", ["uncovered", "current_item", "other_selection"])
def test_formal_transaction_requires_reliable_empty_current_selection(selection_database, monkeypatch, mode, state):
    owned = selection_database
    event, context = _proposal(owned, mode)
    if state == "uncovered":
        ranges = dict(context.read_coverage.ranges)
        ranges.pop(("obtain_items", "selection_id"))
        context = replace(context, read_coverage=ReadCoverage(ranges))
    else:
        selection_id = 61 if state == "current_item" else 62
        if state == "other_selection":
            owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
            owned.connection.execute("INSERT INTO action_dependencies (id,action_id,depends_on_action_id)"
                                     " VALUES (42,30,12)")
            owned.connection.execute("INSERT INTO obtain_source_selections (id,dependency_id,status) VALUES (62,42,2)")
        owned.connection.execute("INSERT INTO obtain_items (id,selection_id,output_id,basis,status,source_dependency)"
                                 " VALUES (99,?,?,1,2,0)",
                                 (selection_id, 701 if state == "current_item" else 704))
        owned.connection.commit()
        context.state_rows["obtain_items"][99] = row_facts(owned.connection, "obtain_items", 99)
    before = tuple(owned.connection.iterdump())
    original = validators.NAMED_GUARDS["source_selection"]
    seen = []

    def inspect(proposal, current):
        seen.append((proposal.event_type, proposal.reason))
        original(proposal, current)

    monkeypatch.setitem(validators.NAMED_GUARDS, "source_selection", inspect)
    receipt = commit_operation(_Sequence((event,), context), new_operation_key(), owned)
    assert seen == [(4, 1)], receipt.error
    if state == "other_selection":
        assert receipt.kind == "completed", receipt.error
        assert row_facts(owned.connection, "obtain_items", 99) == context.state_rows["obtain_items"][99]
        assert len(outputs.load_selection(owned.connection, 61).items) == (4 if mode is SelectionMode.EXPLICIT_IDS else 2)
    else:
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("failure", ["INSERT INTO obtain_items", "UPDATE obtain_source_selections SET"])
def test_selection_write_failure_rolls_back_history_and_projection(selection_database, failure):
    owned = selection_database
    _request(owned.connection, SelectionMode.DEFAULT)
    command = outputs.FixSelection(61, _snapshot(owned.connection), _NOW)
    before = tuple(owned.connection.iterdump())
    target = replace(owned, connection=_FaultConnection(owned.connection, failure))
    key = new_operation_key()
    receipt = commit_operation(outputs._FixSelectionCommand(command, key), key, target)
    assert receipt.kind == "rolled_back", receipt.error
    assert isinstance(receipt.error, sqlite3.OperationalError), receipt.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("mode", list(SelectionMode))
@pytest.mark.parametrize("sparse", [False, True])
def test_history_replays_saved_choice_without_current_catalog(selection_database, mode, monkeypatch, sparse):
    owned = selection_database
    event, context = _proposal(owned, mode)
    if sparse:
        event, owners = _sparse_event(event, context.owners)
        context = replace(context, owners=owners)
    validated = validate_event(event, context)
    image = EntityImage(validated.references[0][0], 30, True,
        {("actions", 30): context.state_rows["actions"][30],
         ("obtain_source_selections", 61): context.state_rows["obtain_source_selections"][61]},
        last_event_id=1, change_count=1)
    ids = (705, 703, 999, 704) if mode is SelectionMode.EXPLICIT_IDS else ()
    command = outputs.FixSelection(61, _snapshot(owned.connection, mode, ids), _NOW)
    key = new_operation_key()
    proposal = _SelectionProposal(command, key, "sparse_ids") if sparse else outputs._FixSelectionCommand(command, key)
    receipt = commit_operation(proposal, key, owned)
    assert receipt.kind == "completed", receipt.error
    with closing(owned.connection.execute(
        "SELECT id, transaction_id, event_type, event_version, occurred_at, clock_status, change_seq, body_json"
        " FROM history_events WHERE id=2"
    )) as cursor:
        stored = cursor.fetchone()
    saved = {row.row_id: row_facts(owned.connection, "obtain_items", row.row_id)
             for row in event.rows if row.table == "obtain_items"}
    # 当前目录已不可解释时，历史仍只使用保存的事实。
    owned.connection.execute("DELETE FROM output_origins")
    owned.connection.execute("DELETE FROM outputs")
    owned.connection.commit()
    monkeypatch.setattr(outputs, "select_outputs", create_autospec(
        outputs.select_outputs, side_effect=AssertionError("历史回放不得重新选择")))
    decoded = decode_event_row(stored)
    replayed = apply_forward(image, replace(validated, envelope=decoded))
    for identity, facts in saved.items():
        assert replayed.rows["obtain_items", identity] == {
            key: facts[key] for key in replayed.rows["obtain_items", identity]}
    assert replayed.rows["obtain_source_selections", 61]["status"] == 2
    restored = apply_reverse(replayed, replace(validated, envelope=decoded))
    assert restored.rows == image.rows
