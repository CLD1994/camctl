"""真实前序产物事件改变当前选择依据，后序事件不能提前授权选择。"""

from dataclasses import replace
import json
import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.events import business_columns
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from camctl.outputs.sources import SelectionMode
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.transactions import CommandPlan, event_envelope, row_change, row_facts, update_change

from ..support.output_source_cases import observed_event_guards

from .test_selection_guard import _proposal, _NOW, selection_database, family_database
from .test_qualification import _seed_device_file, _seed_output
from ..support.output_planning import storage_fault as _FaultConnection


from ..support.output_planning import EventSequence as _Sequence, observe_ranges




def _scenarios(owned, mode, change, *, storage):
    connection = owned.connection
    if change == "new_output":
        _seed_device_file(connection, 599, 11)
        connection.commit()
    initial, current = _proposal(owned, mode, storage=storage)
    current.state_rows["plans"] = {1: row_facts(connection, "plans", 1)}
    if change == "new_output":
        # 明确查询未来可能登记的原片关系；查询结果当前确实为空。
        current = observe_ranges(current, connection,
            ("output_origins", "output_id", 999),
            ("output_origins", "original_output_id", 999))
        _seed_output(connection, 999, 11, 599)
        connection.commit()
        facts = row_facts(connection, "outputs", 999)
        change_row = row_change("outputs", 999, {column: facts[column] for column in business_columns("outputs")})
        reason, identity = 1, 999
    else:
        before = row_facts(connection, "outputs", 703)
        connection.execute("UPDATE outputs SET availability=4, error_json=? WHERE id=703",
                           (json.dumps({"reason": "source_missing"}),))
        connection.commit()
        after = row_facts(connection, "outputs", 703)
        change_row = update_change("outputs", 703,
            {column: before[column] for column in ("availability", "error_json")},
            {column: after[column] for column in ("availability", "error_json")})
        reason, identity = 4, 703
    changed, changed_context = _proposal(owned, mode, storage=storage)
    current.owners.update(changed_context.owners)
    if change == "new_output":
        connection.execute("DELETE FROM outputs WHERE id=999")
    else:
        connection.execute("UPDATE outputs SET availability=1, error_json=NULL WHERE id=703")
    connection.commit()
    current.owners["outputs", identity] = ("output", identity)
    return initial, changed, event_envelope(2, 2, 20, reason, (change_row,), _NOW), current


@pytest.mark.parametrize("mode", list(SelectionMode))
@pytest.mark.parametrize("change", ["new_output", "availability"])
@pytest.mark.parametrize("change_first", [False, True])
@pytest.mark.parametrize("select_changed", [False, True])
def test_selection_uses_only_changes_that_precede_it(observed_event_guards,
    selection_database, monkeypatch, mode, change, change_first, select_changed,
):
    storage, callbacks = observed_event_guards
    owned = selection_database
    initial, changed, observation, context = _scenarios(owned, mode, change, storage=storage)
    selection = changed if select_changed else initial
    events = (observation, selection) if change_first else (selection, observation)
    before = tuple(owned.connection.iterdump())
    seen = []

    def inspect(event, current):
        present = current.state_rows["outputs"].get(999)
        seen.append((present is not None, current.state_rows["outputs"][703]["availability"]))

    callbacks["source_selection"] = inspect
    receipt = storage.commit_operation(_Sequence(events, context), new_operation_key(), owned)
    assert seen == [(change_first and change == "new_output", 4 if change_first and change == "availability" else 1)], receipt.error
    if change_first != select_changed:
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert tuple(owned.connection.iterdump()) == before
    else:
        assert receipt.kind == "completed", receipt.error
        saved = outputs.load_selection(owned.connection, 61).items
        if change == "new_output":
            if mode is SelectionMode.EXPLICIT_IDS:
                target = next(item for item in saved if item.requested_output_id == 999)
                assert (target.output_id, target.error_code) == ((999, None) if change_first else (None, 1))
            elif mode is SelectionMode.DEFAULT:
                assert tuple(item.output_id for item in saved) == ((703, 705, 999) if change_first else (703, 705))
            else:
                assert tuple(item.original_output_id for item in saved) == ((701, 705, 999) if change_first else (701, 705))
        else:
            target = next(item for item in saved if item.output_id == 703)
            assert target.error_code == (3 if change_first else None)


@pytest.mark.parametrize("mode", list(SelectionMode))
def test_prior_fixed_selection_prevents_second_first_fix(observed_event_guards, selection_database, monkeypatch, mode):
    storage, callbacks = observed_event_guards
    event, context = _proposal(selection_database, mode, storage=storage)
    second = replace(event, rows=(event.rows[0], *(replace(row, row_id=row.row_id + 100) for row in event.rows[1:])))
    for row in second.rows[1:]:
        context.owners[row.table, row.row_id] = ("action", 30)
    seen = []

    def inspect(event, current):
        seen.append((current.state_rows["obtain_source_selections"][61]["status"],
                     tuple(current.complete_rows("obtain_items", "selection_id", 61))))

    callbacks["source_selection"] = inspect
    before = tuple(selection_database.connection.iterdump())
    receipt = storage.commit_operation(_Sequence((event, second), context), new_operation_key(), selection_database)
    assert receipt.kind == "rolled_back", receipt.error
    assert isinstance(receipt.error, EventValidationError), receipt.error
    assert seen == [(1, ()), (2, tuple(row.row_id for row in event.rows[1:]))]
    assert tuple(selection_database.connection.iterdump()) == before


@pytest.mark.parametrize("mode", list(SelectionMode))
@pytest.mark.parametrize("change", ["new_output", "availability"])
def test_selection_write_failure_also_rolls_back_prior_output_event(observed_event_guards, selection_database, monkeypatch, mode, change):
    storage, callbacks = observed_event_guards
    _, selection, observation, context = _scenarios(selection_database, mode, change, storage=storage)
    before = tuple(selection_database.connection.iterdump())
    target = replace(selection_database, connection=_FaultConnection(
        selection_database.connection, "INSERT INTO obtain_items"))
    receipt = storage.commit_operation(_Sequence((observation, selection), context), new_operation_key(), target)
    assert receipt.kind == "rolled_back", receipt.error
    assert type(receipt.error) is sqlite3.DatabaseError, receipt.error
    assert tuple(selection_database.connection.iterdump()) == before
