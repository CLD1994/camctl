"""派生产物正式事件必须复核其承载文件的当前来源。"""

from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history import validators
from camctl.history.validators import EventValidationError
from camctl.outputs.sources import SelectionMode
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories import outputs
from camctl.persistence.transaction import commit_operation

from .test_selection_derived_sequence import _scenario, _file, _output
from .test_selection_guard import selection_database, family_database
from .test_selection_event_sequence import _Sequence


@pytest.mark.parametrize("case,table,column,file_id", [
    ("preview_only", "device_files", "source_action_id", 599),
    ("repair_only", "intermediate_files", "owner_action_id", 899),
])
def test_foreign_file_cannot_be_registered_as_derivative(
    selection_database, monkeypatch, case, table, column, file_id,
):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    _, _, registration, context = _scenario(selection_database, SelectionMode.DEFAULT, case)
    connection = selection_database.connection
    connection.execute(f"UPDATE {table} SET {column}=12 WHERE id=?", (file_id,))
    connection.commit()
    context.state_rows[table][file_id][column] = 12
    before = tuple(connection.iterdump())

    receipt = commit_operation(_Sequence((registration,), context), new_operation_key(), selection_database)

    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, EventValidationError), receipt.error
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("kind,case", [(2, "repair_only"), (3, "preview_only")])
@pytest.mark.parametrize("cleaned", [False, True])
def test_existing_derivative_keeps_its_role_after_cleanup(selection_database, monkeypatch, kind, case, cleaned):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    _, _, registration, context = _scenario(selection_database, SelectionMode.DEFAULT, case)
    connection = selection_database.connection
    _, file_id = _file(connection, 998, kind, 100)
    _output(connection, 998, kind, file_id)
    if cleaned:
        connection.execute("UPDATE outputs SET availability=3,cleanup_status=4 WHERE id=998")
        if kind == 3:
            connection.execute("UPDATE device_files SET presence_state=3 WHERE id=?", (file_id,))
    connection.commit()
    # 重新读取同一原片的完整集合；新事件的关联使用独立身份。
    reads = outputs._CatalogReads(connection)
    reads.required("outputs", 998)
    assert reads.related(705) == (998,)
    for table, rows in reads.state_rows.items():
        context.state_rows.setdefault(table, {}).update(rows)
    origin = registration.rows[1]
    context.owners.pop(("output_origins", origin.row_id))
    context.owners["output_origins", 1001] = ("output", 999)
    registration = replace(registration, rows=(registration.rows[0], replace(origin, row_id=1001)))
    before = tuple(connection.iterdump())

    receipt = commit_operation(_Sequence((registration,), context), new_operation_key(), selection_database)

    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, EventValidationError), receipt.error
    assert tuple(connection.iterdump()) == before
