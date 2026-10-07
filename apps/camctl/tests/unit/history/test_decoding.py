"""历史存储行解码；登记边界使用受接口约束的内存替身。"""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.contracts.history_values import TransactionRange
from camctl.history import events, validators
from camctl.history.decoding import decode_event_row


@pytest.fixture(autouse=True)
def registration(monkeypatch):
    registry = {"format_version": 1, "tables": {"plans": {
        "immutable": ["request_id", "name", "created_at"], "mutable": ["status"],
        "write_once": [], "derived": ["id", "created_event_id", "last_event_id", "change_count"],
        "owner": {"entity": "plan", "id": "id", "via": []}, "state_columns": ["status"],
    }, "device_activities": {
        "immutable": ["task_locator_json"], "mutable": ["sent_at", "started_at"],
        "write_once": [], "derived": ["id", "created_event_id", "last_event_id", "change_count"],
        "owner": {"entity": "action", "id": "id", "via": []}, "state_columns": [],
    }}, "events": {"PLAN_ACCEPTED": {"id": 1, "version": 1, "branches": {"CREATE": {
        "reason": 1, "rows": [{"table": "plans", "op": "create", "after": {"status": [1]}}],
        "guards": [], "evidence": [],
    }}}, "DEVICE_OBSERVED": {"id": 13, "version": 1, "branches": {"OBSERVE": {
        "reason": 2, "rows": [{"table": "device_activities", "op": "update",
            "columns": ["sent_at"], "required": []}],
        "guards": [], "evidence": ["observation"],
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
    # 精确小数经 OBSERVE 分支登记的 observation 依据成员往返。
    body = {"reason": 2, "evidence": {}, "rows": [{
        "table": "device_activities", "id": 11,
        "before": {"exists": True, "values": {"sent_at": None}},
        "after": {"exists": True, "values": {"sent_at": 5}},
    }]}
    text = json.dumps(body).replace('"evidence": {}',
                                    '"evidence": {"observation": {"value": 0.10000000000000001}}')
    event = decode_event_row(_row(body_json=text, event_type=13))
    assert event.evidence["observation"]["value"] == Decimal("0.10000000000000001")


def test_undeclared_evidence_member_is_rejected_on_decode():
    """读取与保存共用依据成员白名单：未登记成员不能解释为合法事实。"""
    text = json.dumps(_body()).replace(
        '"evidence": {}', '"evidence": {"observation": {"value": 1}}')
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body_json=text))


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


def _transition_registration(order):
    _update_registration(None)
    registry = events.load_event_registry()
    branch = registry["events"]["PLAN_ACCEPTED"]["branches"]["CREATE"]
    branch["rows"][0].update(before={}, after={}, transitions={key: ["start"] for key in order})
    registry["state_models"] = {f"plans.{key}": {"edges": [{
        "id": "start", "from": 1, "to": 2, "by": ["PLAN_ACCEPTED.CREATE"],
    }]} for key in ("status", "note")}
    validators.load_enum_registry()["enums"]["plans.note"] = {
        "members": {"PENDING": 1, "RUNNING": 2, "ENDED": 3}}


@pytest.mark.parametrize("order", [("status", "note"), ("note", "status")])
def test_every_changed_state_column_must_have_a_legal_transition(order):
    _transition_registration(order)
    body = _update_body({"status": 1, "note": 3}, {"status": 2, "note": 2})
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


def test_multiple_legal_transitions_are_accepted():
    _transition_registration(("status", "note"))
    body = _update_body({"status": 1, "note": 1}, {"status": 2, "note": 2})
    event = decode_event_row(_row(body))
    assert event.rows[0].after.values == {"status": 2, "note": 2}


def test_unchanged_state_column_does_not_require_a_transition():
    _transition_registration(("status", "note"))
    event = decode_event_row(_row(_update_body({"note": 1}, {"note": 2})))
    assert event.rows[0].after.values == {"note": 2}


def test_save_validation_also_rejects_later_illegal_transition(monkeypatch):
    from camctl.history.events import EventEnvelope, RowChange, RowImage
    _transition_registration(("status", "note"))
    event = EventEnvelope(7, 3, 1, 1, 0, 1, None, 1, {}, (
        RowChange("plans", 1, RowImage(True, {"status": 1, "note": 3}),
                  RowImage(True, {"status": 2, "note": 2})),))
    context = validators.EventContext(TransactionRange(3, 7, 7), {("plans", 1): ("plan", 1)}, {})
    monkeypatch.setattr(validators, "_history_objects", lambda: {"plan": {"id": 4}})
    with pytest.raises(validators.EventValidationError):
        validators.validate_event(event, context)


def test_required_column_must_change_even_if_another_column_changes():
    _update_registration(None, required=("note",))
    body = _update_body({"status": 1, "note": "same"}, {"status": 2, "note": "same"})
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


@pytest.mark.parametrize("before,after", [
    ("same", "same"), (1, 1.0),
    ({"a": 1, "b": [True, None]}, {"b": [True, None], "a": 1}),
])
def test_update_rejects_columns_with_equal_exact_json_values(before, after):
    _update_registration(None)
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(_update_body({"note": before}, {"note": after})))


def test_update_cannot_include_unchanged_fields_beside_changed_fields():
    _update_registration(None)
    body = _update_body({"status": 1, "note": "same"}, {"status": 2, "note": "same"})
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


def test_required_column_can_change_to_null_when_branch_allows_it():
    _update_registration(None, required=("note",))
    event = decode_event_row(_row(_update_body({"note": "old"}, {"note": None})))
    assert event.rows[0].after.values == {"note": None}


def test_boolean_and_numeric_values_are_distinct_changes():
    _update_registration(None)
    event = decode_event_row(_row(_update_body({"note": False}, {"note": 0})))
    assert event.rows[0].before.values["note"] is False
    assert event.rows[0].after.values["note"] == 0


@pytest.mark.parametrize("state", [2, None])
def test_save_checks_unchanged_branch_conditions_in_reliable_context(monkeypatch, state):
    from camctl.history.events import EventEnvelope, RowChange, RowImage
    _update_registration(None)
    events.load_event_registry()["events"]["PLAN_ACCEPTED"]["branches"]["CREATE"]["rows"][0]["after"] = {"status": [1]}
    event = EventEnvelope(7, 3, 1, 1, 0, 1, None, 1, {}, (
        RowChange("plans", 1, RowImage(True, {"note": "old"}), RowImage(True, {"note": "new"})),))
    values = {"note": "old"}
    if state is not None:
        values["status"] = state
    context = validators.EventContext(TransactionRange(3, 7, 7), {("plans", 1): ("plan", 1)},
                                      {"plans": {1: values}})
    monkeypatch.setattr(validators, "_history_objects", lambda: {"plan": {"id": 4}})
    with pytest.raises(validators.EventValidationError):
        validators.validate_event(event, context)


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
