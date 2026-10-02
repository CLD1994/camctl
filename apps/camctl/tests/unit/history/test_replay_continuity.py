"""正逆应用核实已验证事件与恢复镜像之间的精确值连续性。"""

from copy import deepcopy
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.history import replay
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.replay import EntityImage, ReplayError, apply_forward, apply_reverse
from camctl.history.validators import ValidatedEvent

from unit.history.test_replay import _create_plan_event, _plan_image


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(replay, "load_enum_registry", create_autospec(
        replay.load_enum_registry, return_value={"history_objects": {
            "plan": {"id": 4, "table": "plans", "report_target": True, "snapshot": True},
            "device_file": {"id": 9, "table": "device_files", "report_target": False, "snapshot": True},
        }},
    ))


def _json_change(before, after):
    row = RowChange("device_files", 1, RowImage(True, {"last_error_json": before}),
                    RowImage(True, {"last_error_json": after}))
    return ValidatedEvent(
        EventEnvelope(2, 2, 17, 1, 1, 2, None, 3, {}, (row,)),
        "DEVICE_FILE_CHANGED", "COMPLETE", ((9, 1),), {("device_files", 1): (9, 1)},
    )


def _file_image(values):
    return EntityImage(9, 1, True, {("device_files", 1): values}, 2, 2)


@pytest.mark.parametrize("current,expected", [
    ({"value": False}, {"value": 0}), ({"value": True}, {"value": 1}),
    ({"details": {"value": False}}, {"details": {"value": 0}}),
    ({"value": [True, None]}, {"value": [1, None]}),
])
def test_forward_rejects_boolean_and_numeric_old_value_mismatch(current, expected):
    image = _file_image({"last_error_json": current})
    with pytest.raises(ReplayError):
        apply_forward(image, _json_change(expected, {"value": "new"}))
    assert image.rows[("device_files", 1)]["last_error_json"] is current


@pytest.mark.parametrize("current,expected", [
    ({"value": False}, {"value": 0}), ({"value": True}, {"value": 1}),
    ({"details": {"value": False}}, {"details": {"value": 0}}),
    ({"value": [True, None]}, {"value": [1, None]}),
])
def test_reverse_rejects_boolean_and_numeric_new_value_mismatch(current, expected):
    image = _file_image({"last_error_json": current})
    with pytest.raises(ReplayError):
        apply_reverse(image, _json_change({"value": "old"}, expected))
    assert image.rows[("device_files", 1)]["last_error_json"] is current


def test_forward_rejects_missing_old_column():
    image = _file_image({})
    with pytest.raises(ReplayError):
        apply_forward(image, _json_change(None, {"value": 1}))
    assert image.rows[("device_files", 1)] == {}


def test_reverse_rejects_missing_new_column():
    image = _file_image({})
    with pytest.raises(ReplayError):
        apply_reverse(image, _json_change({"value": 1}, None))
    assert image.rows[("device_files", 1)] == {}


@pytest.mark.parametrize("current,expected", [
    (None, None), ({"value": Decimal("0.10000000000000001")}, {"value": Decimal("0.100000000000000010")}),
    ({"b": [True, None], "a": 1}, {"a": Decimal("1.0"), "b": [True, None]}),
])
def test_forward_accepts_equal_exact_old_values(current, expected):
    result = apply_forward(_file_image({"last_error_json": current}),
                           _json_change(expected, {"value": "new"}))
    assert result.rows[("device_files", 1)]["last_error_json"] == {"value": "new"}


@pytest.mark.parametrize("current,expected", [
    (None, None), ({"value": Decimal("0.10000000000000001")}, {"value": Decimal("0.100000000000000010")}),
    ({"b": [True, None], "a": 1}, {"a": Decimal("1.0"), "b": [True, None]}),
])
def test_reverse_accepts_equal_exact_new_values(current, expected):
    result = apply_reverse(_file_image({"last_error_json": current}),
                           _json_change({"value": "old"}, expected))
    assert result.rows[("device_files", 1)]["last_error_json"] == {"value": "old"}


@pytest.mark.parametrize("current,expected", [
    ({"items": [1, 2]}, {"items": [2, 1]}), ({}, {"value": None}),
    ({"value": Decimal("0.10000000000000001")}, {"value": Decimal("0.1")}),
])
def test_forward_rejects_distinct_exact_old_values(current, expected):
    with pytest.raises(ReplayError):
        apply_forward(_file_image({"last_error_json": current}), _json_change(expected, None))


@pytest.mark.parametrize("current,expected", [
    ({"items": [1, 2]}, {"items": [2, 1]}), ({}, {"value": None}),
    ({"value": Decimal("0.10000000000000001")}, {"value": Decimal("0.1")}),
])
def test_reverse_rejects_distinct_exact_new_values(current, expected):
    with pytest.raises(ReplayError):
        apply_reverse(_file_image({"last_error_json": current}), _json_change(None, expected))


@pytest.mark.parametrize("change", ["different", "missing", "extra", "boolean"])
def test_reverse_creation_requires_complete_matching_business_row(change):
    image = _plan_image(status=1, count=1, last=101)
    values = image.rows[("plans", 1)]
    if change == "different":
        values["name"] = "other"
    elif change == "missing":
        del values["name"]
    elif change == "extra":
        values["unexpected"] = 42
    else:
        values["status"] = True
    before = deepcopy(image.rows)
    with pytest.raises(ReplayError):
        apply_reverse(image, _create_plan_event(101))
    assert image.rows == before
