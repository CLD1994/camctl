"""仓储首次提交和原键恢复只消费通过规范构造的命令。"""

from types import SimpleNamespace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.sources import SelectionMode
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs

from .test_selection_reuse import _save, selection_database, family_database
from .test_selection_authority import _request, _snapshot, _NOW


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("shape", ["same_fields", "float_identity", "float_time"])
def test_repository_rejects_unvalidated_command_before_key_lookup(selection_database, saved, shape):
    owned = selection_database
    if saved:
        command, key = _save(owned)
    else:
        _request(owned.connection, SelectionMode.DEFAULT)
        command = outputs.FixSelection(61, _snapshot(owned.connection), _NOW)
        key = new_operation_key()
    unvalidated = SimpleNamespace(selection_id=61.0 if shape == "float_identity" else 61,
        snapshot=command.snapshot, occurred_at=float(_NOW) if shape == "float_time" else _NOW)
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().fix_selection(unvalidated, key, owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, TypeError), result.error
    assert tuple(owned.connection.iterdump()) == before
