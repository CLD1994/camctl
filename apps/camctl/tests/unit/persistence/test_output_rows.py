"""已保存产物选择的 JSON 值适配契约。"""

from decimal import Decimal

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.outputs import _decoded


def test_saved_selection_json_preserves_exact_number():
    assert _decoded('{"requested_output_id":9007199254740993.0}') == {
        "requested_output_id": Decimal("9007199254740993.0")}


@pytest.mark.parametrize("invalid", ['{"id":1,"id":2}', '{"id":NaN}'])
def test_saved_selection_json_rejects_invalid_facts(invalid):
    with pytest.raises(ConsistencyError):
        _decoded(invalid)
