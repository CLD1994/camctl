"""取回固定定义只保存选择方式，并拒绝不可解释的持久值。"""
from decimal import Decimal

import pytest

from camctl.outputs.definitions import build_obtain_spec, validate_obtain_spec
from camctl.outputs import definitions


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


@pytest.mark.parametrize("mode", [1, 2])
def test_nonexplicit_selection_uses_fixed_definition(mode):
    params = {} if mode == 1 else {"filter": "preview"}
    assert definitions.read_selection_request({"selection_mode": mode}, {"params": params}) == (mode, ())


def test_explicit_selection_reads_original_ids_in_order():
    assert definitions.read_selection_request({"selection_mode": 3},
        {"params": {"output_ids": ["9007199254740993", "1"]}}) == (3, (9007199254740993, 1))


@pytest.mark.parametrize("raw", [None, [], {}, {"params": None}, {"params": []},
    {"params": {}}, {"params": {"output_ids": []}}, {"params": {"output_ids": "1"}},
    {"params": {"output_ids": [True]}}, {"params": {"output_ids": [1]}},
    {"params": {"output_ids": ["01"]}}, {"params": {"output_ids": ["0"]}},
    {"params": {"output_ids": ["9223372036854775808"]}},
    {"params": {"output_ids": ["1", "1"]}}])
def test_explicit_selection_requires_reliable_original_list(raw):
    with pytest.raises(ValueError):
        definitions.read_selection_request({"selection_mode": 3}, raw)


@pytest.mark.parametrize("mode,params", [
    (1, {"output_ids": ["701"]}), (2, {"output_ids": ["701"]}),
    (1, {"output_ids": []}), (2, {"output_ids": None}),
    (1, {"filter": "preview"}), (2, {}), (2, {"filter": "default"}),
    (1, {"filter": None}), (1, {"filter": "unknown"}),
    (3, {"output_ids": ["701"], "filter": "default"}),
    (3, {"output_ids": ["701"], "filter": "preview"}),
    (3, {"output_ids": ["701"], "filter": None}),
])
def test_saved_selection_mode_must_agree_with_original_selector(mode, params):
    with pytest.raises(ValueError):
        definitions.read_selection_request({"selection_mode": mode}, {"params": params})


@pytest.mark.parametrize("mode", [1, 2])
@pytest.mark.parametrize("raw", [None, [], {}, {"params": None}])
def test_nonexplicit_mode_still_requires_original_parameter_object(mode, raw):
    with pytest.raises(ValueError):
        definitions.read_selection_request({"selection_mode": mode}, raw)


def test_explicit_default_filter_retains_default_mode():
    assert definitions.read_selection_request({"selection_mode": 1},
        {"params": {"filter": "default"}}) == (1, ())
