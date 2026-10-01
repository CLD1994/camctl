"""历史存储行解码；登记边界使用受接口约束的内存替身。"""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.history import events, validators
from camctl.history.decoding import decode_event_row


@pytest.fixture(autouse=True)
def registration(monkeypatch):
    registry = {"format_version": 1, "tables": {"plans": {
        "immutable": ["request_id", "name", "created_at"], "mutable": ["status"],
        "write_once": [], "derived": ["id", "created_event_id", "last_event_id", "change_count"],
        "owner": {"entity": "plan", "id": "id", "via": []}, "state_columns": ["status"],
    }}, "events": {"PLAN_ACCEPTED": {"id": 1, "version": 1, "branches": {"CREATE": {
        "reason": 1, "rows": [{"table": "plans", "op": "create", "after": {"status": [1]}}],
        "guards": [],
    }}}}, "state_models": {}}
    read = create_autospec(events.load_event_registry, return_value=registry)
    monkeypatch.setattr(events, "load_event_registry", read)
    monkeypatch.setattr(validators, "load_event_registry", read)
    monkeypatch.setattr(validators, "load_enum_registry", create_autospec(
        validators.load_enum_registry, return_value={"enums": {
            "plans.status": {"members": {"PENDING": 1, "RUNNING": 2}},
            "history_events.clock_status": {"members": {"UNCHECKED": 1, "TRUSTED": 2, "INVALID": 3}},
        }, "json_enums": {}}))


def _body():
    return {"reason": 1, "evidence": {}, "rows": [{
        "table": "plans", "id": 1, "before": {"exists": False, "values": {}},
        "after": {"exists": True, "values": {
            "request_id": 42, "name": "plan", "created_at": 0, "status": 1}},
    }]}


def _row(body=None, **columns):
    values = {"event_id": 7, "transaction_id": 3, "event_type": 1, "event_version": 1,
              "occurred_at": 0, "clock_status": 1, "change_seq": 5,
              "body_json": json.dumps(_body() if body is None else body)}
    values.update(columns)
    return tuple(values.values())


def test_legal_stored_event_decodes_exact_facts():
    event = decode_event_row(_row())
    assert (event.event_id, event.transaction_id, event.reason, event.change_seq) == (7, 3, 1, 5)
    assert event.rows[0].after.values == {"request_id": 42, "name": "plan", "created_at": 0, "status": 1}


def test_absent_creation_values_are_valid_for_nonexistent_before():
    body = _body()
    del body["rows"][0]["before"]["values"]
    event = decode_event_row(_row(body))
    assert event.rows[0].before.exists is False
    assert event.rows[0].before.values == {}


def test_integer_json_identity_is_normalized_without_rounding():
    event = decode_event_row(_row(body_json=json.dumps(_body()).replace('"id": 1', '"id": 1.0')))
    assert type(event.rows[0].row_id) is int
    assert event.rows[0].row_id == 1


def test_business_decimal_is_not_converted_to_float():
    text = json.dumps(_body()).replace('"evidence": {}', '"evidence": {"observation": {"value": 0.10000000000000001}}')
    event = decode_event_row(_row(body_json=text))
    assert event.evidence["observation"]["value"] == Decimal("0.10000000000000001")


def test_boolean_is_not_a_numeric_column_enum():
    body = _body()
    body["rows"][0]["after"]["values"]["status"] = True
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


def _update_registration(registration, *, required=()):
    registry = events.load_event_registry()
    registry["tables"]["plans"]["mutable"].append("note")
    registry["events"]["PLAN_ACCEPTED"]["branches"]["CREATE"]["rows"] = [{
        "table": "plans", "op": "update", "columns": ["status", "note"],
        "required": list(required), "before": {"status": [1]}, "after": {"status": [2]},
    }]


def _update_body(before, after):
    body = _body()
    body["rows"][0]["before"] = {"exists": True, "values": before}
    body["rows"][0]["after"] = {"exists": True, "values": after}
    return body


def test_update_can_use_a_subset_of_allowed_columns(registration):
    _update_registration(registration)
    event = decode_event_row(_row(_update_body({"status": 1}, {"status": 2})))
    assert event.rows[0].after.values == {"status": 2}


def test_update_before_enum_cannot_be_boolean(registration):
    _update_registration(registration)
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(_update_body({"status": True, "note": "old"},
                                          {"status": 2, "note": "new"})))


def test_update_subset_cannot_omit_required_column(registration):
    _update_registration(registration, required=("status",))
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(_update_body({"note": "old"}, {"note": "new"})))


def test_integral_json_number_is_a_valid_column_enum():
    text = json.dumps(_body()).replace('"status": 1', '"status": 1.0')
    event = decode_event_row(_row(body_json=text))
    assert event.rows[0].after.values["status"] == 1


@pytest.mark.parametrize("field,bad", [
    ("event_id", True), ("event_id", 0), ("event_id", 1.5), ("event_id", "7"),
    ("event_id", 2**63), ("transaction_id", -1), ("event_version", 2),
    ("event_type", 999), ("clock_status", True), ("clock_status", 999),
    ("occurred_at", 1.5), ("change_seq", True), ("change_seq", 0),
])
def test_invalid_stored_header_is_consistency_error(field, bad):
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(**{field: bad}))


@pytest.mark.parametrize("missing", ["reason", "evidence", "rows"])
def test_missing_required_body_member_is_not_defaulted(missing):
    body = _body()
    del body[missing]
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


@pytest.mark.parametrize("field,bad", [("reason", True), ("reason", 999), ("evidence", None),
                                      ("evidence", []), ("rows", []), ("rows", {})])
def test_invalid_body_member_is_consistency_error(field, bad):
    body = _body()
    body[field] = bad
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


@pytest.mark.parametrize("field,bad", [("id", True), ("id", 0), ("id", 1.5), ("id", "1"),
                                      ("table", "history_events")])
def test_invalid_business_row_identity_or_table_is_rejected(field, bad):
    body = _body()
    body["rows"][0][field] = bad
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


@pytest.mark.parametrize("side,field,bad", [("before", "exists", 0), ("before", "exists", "false"),
                                           ("before", "values", {"status": 1}),
                                           ("after", "exists", False), ("after", "values", [])])
def test_invalid_row_existence_or_values_are_rejected(side, field, bad):
    body = _body()
    body["rows"][0][side][field] = bad
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


def test_existing_row_requires_values():
    body = _body()
    del body["rows"][0]["after"]["values"]
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


def test_duplicate_business_row_is_rejected():
    body = _body()
    body["rows"].append(deepcopy(body["rows"][0]))
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


@pytest.mark.parametrize("text", ['{"reason":1,"reason":2}', '{"reason":NaN}', '{bad'])
def test_invalid_exact_json_is_consistency_error(text):
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body_json=text))
