"""派生产物正式事件必须复核其承载文件的当前来源。"""

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history import validators
from camctl.history.validators import EventValidationError
from camctl.outputs.sources import SelectionMode
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.transaction import commit_operation

from .test_selection_derived_sequence import _scenario
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
