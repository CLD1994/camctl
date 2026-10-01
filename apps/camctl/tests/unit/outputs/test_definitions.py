"""取回固定定义只保存选择方式，并拒绝不可解释的持久值。"""
from decimal import Decimal

import pytest

from camctl.outputs.definitions import build_obtain_spec, validate_obtain_spec


@pytest.mark.parametrize("params,expected", [
    ({"source":{"current_plan":True}}, 1),
    ({"source":{"current_plan":True},"filter":"default"}, 1),
    ({"source":{"action_name":"shoot"},"filter":"preview"}, 2),
    ({"source":{"action_instance_id":"500"},"output_ids":["900"]}, 3),
])
def test_selection_meaning_is_fixed_without_copying_source_or_ids(params, expected):
    assert build_obtain_spec(params) == {"selection_mode":expected}


@pytest.mark.parametrize("spec", [None, [], "{}", {}, {"selection_mode":True},
    {"selection_mode":0}, {"selection_mode":4}, {"selection_mode":"1"},
    {"selection_mode":Decimal("1.5")}, {"selection_mode":1,"source":{}}])
def test_reader_rejects_wrong_shape_or_unregistered_selection(spec):
    with pytest.raises(ValueError):
        validate_obtain_spec(spec)


def test_reader_accepts_mathematical_json_integer():
    assert validate_obtain_spec({"selection_mode":Decimal("2.0")}) == {"selection_mode":2}
