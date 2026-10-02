"""真实登记、SQLite 保存与逐项错误读取的精确 JSON 契约。"""

from decimal import Decimal

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.sources import SelectionMode, select_outputs
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import FixSelection, load_selection, load_selection_facts

from .test_sources import _NOW, _fixed_resolution, _prepared_selection, _set_selection_request


@pytest.fixture
def saved_error(tmp_path):
    _, owned, repository, selection_id = _prepared_selection(tmp_path)
    try:
        _set_selection_request(owned.connection, SelectionMode.EXPLICIT_IDS, (9007199254740993,))
        facts = load_selection_facts(owned.connection, 21, requested_output_ids=(9007199254740993,))
        snapshot = select_outputs(
            _fixed_resolution((21,)), facts, SelectionMode.EXPLICIT_IDS,
            requested_output_ids=(9007199254740993,),
        )
        outcome = repository.fix_selection(
            FixSelection(selection_id, snapshot, _NOW), new_operation_key(), owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        yield owned.connection, selection_id
    finally:
        owned.connection.close()


def test_saved_selection_json_preserves_exact_number(saved_error):
    connection, selection_id = saved_error
    connection.execute(
        "UPDATE obtain_items SET error_details_json = ? WHERE selection_id = ?",
        ('{"requested_output_id":9007199254740993.0}', selection_id),
    )
    saved = load_selection(connection, selection_id)
    assert saved.items[0].error_details == {
        "requested_output_id": Decimal("9007199254740993.0"),
    }


@pytest.mark.parametrize("invalid", ['{"id":1,"id":2}', '{"id":NaN}'])
def test_saved_selection_json_rejects_invalid_facts(saved_error, invalid):
    connection, selection_id = saved_error
    # 注入无法由正常保存入口形成的状态库事实，验证读取错误分类。
    connection.execute("PRAGMA ignore_check_constraints = ON")
    try:
        connection.execute(
            "UPDATE obtain_items SET error_details_json = ? WHERE selection_id = ?",
            (invalid, selection_id),
        )
    finally:
        connection.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(ConsistencyError):
        load_selection(connection, selection_id)
