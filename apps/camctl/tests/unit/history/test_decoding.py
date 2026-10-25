"""历史存储行解码；登记边界使用受接口约束的内存替身。"""
from copy import deepcopy
from decimal import Decimal
import json
import re
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


def _json_with_integer_literals(body, *members):
    """把指定 JSON 成员的数字字面量换成精确小数或指数形式。"""
    text = json.dumps(body)
    for member, original, replacement in members:
        pattern = re.compile(
            rf'("{re.escape(member)}":\s*){re.escape(str(original))}(?=[,}}])')
        text, count = pattern.subn(lambda match: f'{match.group(1)}{replacement}', text, count=1)
        assert count == 1
    return text


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


def _preparation_request():
    return {"action_id": 8, "trusted_wall_now": 0, "prepare_lead_s": 10, "occurred_at": 1}


def _preparation_failure_request():
    return {"action_id": 8, "reason": "device_failed", "occurred_at": 1}


def _preparation_body(member, request):
    # 此处只替换稳定的登记端口，两个真实结构入口仍调用生产成员校验。
    events.load_event_registry()["events"]["PLAN_ACCEPTED"]["branches"]["CREATE"]["evidence"] = [member]
    body = _body()
    body["evidence"] = {member: request}
    return body


@pytest.mark.parametrize("member,payload", [
    ("preparation_request", 7),
    ("preparation_request", []),
    ("preparation_request", {}),
    ("preparation_request", {**_preparation_request(), "extra": 1}),
    ("preparation_request", {key: value for key, value in _preparation_request().items() if key != "trusted_wall_now"}),
    *[("preparation_request", {**_preparation_request(), "action_id": value})
      for value in (True, 0, "8", 9223372036854775808)],
    *[("preparation_request", {**_preparation_request(), "trusted_wall_now": value})
      for value in (True, "0", 0.0)],
    *[("preparation_request", {**_preparation_request(), "occurred_at": value})
      for value in (True, "1", 1.0)],
    *[("preparation_request", {**_preparation_request(), "prepare_lead_s": value})
      for value in (True, -1, "10", None)],
    ("preparation_failure_request", 7),
    ("preparation_failure_request", []),
    ("preparation_failure_request", {}),
    ("preparation_failure_request", {**_preparation_failure_request(), "extra": 1}),
    *[("preparation_failure_request", {**_preparation_failure_request(), "action_id": value})
      for value in (True, 0, "8", 9223372036854775808)],
    *[("preparation_failure_request", {**_preparation_failure_request(), "occurred_at": value})
      for value in (True, "1", 1.0)],
    *[("preparation_failure_request", {**_preparation_failure_request(), "reason": value})
      for value in (True, None, [], "start_unknown")],
])
@pytest.mark.parametrize("entry", ["structure", "decode"])
def test_preparation_evidence_shape_is_checked_without_business_guards(member, payload, entry):
    body = _preparation_body(member, payload)
    if entry == "decode":
        with pytest.raises(ConsistencyError):
            decode_event_row(_row(body))
    else:
        event = decode_event_row(_row())
        object.__setattr__(event, "evidence", body["evidence"])
        with pytest.raises(validators.EventValidationError):
            validators.validate_event_structure(event)


@pytest.mark.parametrize("lead", [True, -1, 0.0, Decimal("NaN"), Decimal("Infinity")])
def test_preparation_lead_must_be_a_finite_nonnegative_exact_number(lead):
    _preparation_body("preparation_request", {})
    event = decode_event_row(_row())
    object.__setattr__(event, "evidence", {
        "preparation_request": {**_preparation_request(), "prepare_lead_s": lead},
    })
    with pytest.raises(validators.EventValidationError):
        validators.validate_event_structure(event)


@pytest.mark.parametrize("literal", ["0", "10", "0.00000000000000001"])
def test_preparation_evidence_preserves_exact_lead(literal):
    body = _preparation_body("preparation_request", _preparation_request())
    text = json.dumps(body).replace('"prepare_lead_s": 10', '"prepare_lead_s": ' + literal)
    event = decode_event_row(_row(body_json=text))
    assert event.evidence["preparation_request"]["prepare_lead_s"] == Decimal(literal)


@pytest.mark.parametrize("reason", ["device_failed", "baseline_read_failed"])
def test_preparation_failure_evidence_accepts_exact_failure_reasons(reason):
    request = {**_preparation_failure_request(), "reason": reason}
    event = decode_event_row(_row(_preparation_body("preparation_failure_request", request)))
    assert event.evidence["preparation_failure_request"] == request


def test_boolean_is_not_a_numeric_column_enum():
    body = _body()
    body["rows"][0]["after"]["values"]["status"] = True
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body))


def _host_event_registration():
    registry = events.load_event_registry()
    registry["tables"]["intermediate_files"] = {
        "immutable": ["owner_action_id", "owner_delivery_id", "purpose", "relative_path"],
        "mutable": ["retention_state", "cleanup_state", "size_bytes", "sha256", "last_error_json"],
        "write_once": [],
        "derived": ["id", "created_event_id", "last_event_id", "change_count"],
        "owner": {"entity": "intermediate_file", "id": "id", "via": []},
        "state_columns": [],
    }
    registry["events"]["INTERMEDIATE_FILE_CHANGED"] = {
        "id": 26,
        "version": 1,
        "branches": {
            "CREATE": {
                "reason": 1,
                "rows": [{"table": "intermediate_files", "op": "create",
                          "after": {"purpose": [2, 5, 6]}}],
                "guards": [],
                "evidence": ["host_photo_request"],
            },
            "LIFECYCLE": {
                "reason": 2,
                "rows": [{"table": "intermediate_files", "op": "update",
                          "columns": ["size_bytes", "sha256", "retention_state"],
                          "required": []}],
                "guards": [],
                "evidence": [],
            },
            "HOST_PHOTO_COMPLETE": {
                "reason": 5,
                "rows": [{"table": "intermediate_files", "op": "update",
                          "columns": ["size_bytes", "sha256"],
                          "required": ["size_bytes", "sha256"]}],
                "guards": [],
                "evidence": ["host_photo_request"],
            },
        },
    }


def _host_file_row(identity, purpose, relative_path):
    return {
        "table": "intermediate_files",
        "id": identity,
        "before": {"exists": False, "values": {}},
        "after": {"exists": True, "values": {
            "owner_action_id": 7,
            "owner_delivery_id": None,
            "purpose": purpose,
            "relative_path": relative_path,
            "retention_state": 1,
            "cleanup_state": 1,
            "size_bytes": None,
            "sha256": None,
            "last_error_json": None,
        }},
    }


def _host_prepare_body(evidence=None):
    return {
        "reason": 1,
        "evidence": ({"host_photo_request": {"action_id": 7, "occurred_at": 0}}
                     if evidence is None else evidence),
        "rows": [
            _host_file_row(31, 5, "capture-originals/31.3fr"),
            _host_file_row(32, 6, "capture-previews/32.jpg"),
        ],
    }


def test_history_decode_accepts_complete_host_photo_prepare_event():
    _host_event_registration()
    assert events.reason_for_branch("INTERMEDIATE_FILE_CHANGED", "HOST_PHOTO_COMPLETE") == 5
    event = decode_event_row(_row(_host_prepare_body(), event_type=26))
    assert event.evidence["host_photo_request"] == {"action_id": 7, "occurred_at": 0}


def test_history_decode_accepts_mathematical_integer_prepare_request_and_rows():
    _host_event_registration()
    body = _host_prepare_body()
    text = _json_with_integer_literals(
        body,
        ("action_id", 7, "7.0"),
        ("occurred_at", 0, "0e0"),
        ("id", 31, "31.0"),
        ("id", 32, "32e0"),
        ("owner_action_id", 7, "7.0"),
        ("owner_action_id", 7, "7.0"),
        ("purpose", 5, "5.0"),
        ("purpose", 6, "6e0"),
        ("retention_state", 1, "1.0"),
        ("retention_state", 1, "1.0"),
        ("cleanup_state", 1, "1e0"),
        ("cleanup_state", 1, "1e0"),
    )

    event = decode_event_row(_row(body_json=text, event_type=26))

    assert event.evidence["host_photo_request"]["action_id"] == Decimal("7.0")
    assert type(event.rows[0].row_id) is int and event.rows[0].row_id == 31
    assert event.rows[0].after.values["owner_action_id"] == Decimal("7.0")
    assert event.rows[0].after.values["purpose"] == Decimal("5.0")
    assert event.rows[0].after.values["retention_state"] == Decimal("1.0")


@pytest.mark.parametrize("body,occurred_at", [
    (_host_prepare_body(evidence={"host_photo_request": 0}), 0),
    (_host_prepare_body(evidence={}), 0),
    (_host_prepare_body(evidence={"host_photo_request": {"action_id": 8, "occurred_at": 0}}), 0),
    (_host_prepare_body(), 1),
])
def test_history_decode_rejects_invalid_or_missing_host_prepare_request(body, occurred_at):
    _host_event_registration()
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body, event_type=26, occurred_at=occurred_at))


def test_history_decode_does_not_accept_boolean_as_prepare_action_identity():
    _host_event_registration()
    body = _host_prepare_body()
    body["evidence"]["host_photo_request"]["action_id"] = 1
    body["rows"][0]["after"]["values"]["owner_action_id"] = True
    body["rows"][1]["after"]["values"]["owner_action_id"] = 1
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body, event_type=26))


def test_history_decode_keeps_legacy_file_create_without_photo_request():
    _host_event_registration()
    body = {
        "reason": 1,
        "evidence": {},
        "rows": [_host_file_row(40, 2, "recording-inputs/40.mp4")],
    }
    event = decode_event_row(_row(body, event_type=26))
    assert "host_photo_request" not in event.evidence


def test_history_decode_keeps_file_promotion_lifecycle_without_byte_request():
    _host_event_registration()
    body = {
        "reason": 2,
        "evidence": {},
        "rows": [{
            "table": "intermediate_files",
            "id": 31,
            "before": {"exists": True, "values": {"retention_state": 1}},
            "after": {"exists": True, "values": {"retention_state": 3}},
        }],
    }
    event = decode_event_row(_row(body, event_type=26))
    assert event.rows[0].after.values == {"retention_state": 3}


def _host_complete_body(original_sha256="a" * 64):
    return {
        "reason": 5,
        "evidence": {"host_photo_request": {
            "action_id": 7,
            "original_file_id": 31,
            "original_size_bytes": 4096,
            "original_sha256": original_sha256,
            "preview_file_id": 32,
            "preview_size_bytes": 128,
            "preview_sha256": "b" * 64,
            "occurred_at": 0,
        }},
        "rows": [
            {"table": "intermediate_files", "id": 31,
             "before": {"exists": True, "values": {"size_bytes": None, "sha256": None}},
             "after": {"exists": True, "values": {"size_bytes": 4096, "sha256": "a" * 64}}},
            {"table": "intermediate_files", "id": 32,
             "before": {"exists": True, "values": {"size_bytes": None, "sha256": None}},
             "after": {"exists": True, "values": {"size_bytes": 128, "sha256": "b" * 64}}},
        ],
    }


def test_history_decode_accepts_and_cross_checks_host_complete_bytes():
    _host_event_registration()
    event = decode_event_row(_row(_host_complete_body(), event_type=26))
    assert len(event.rows) == 2


def test_history_decode_accepts_mathematical_integer_complete_request_and_rows():
    _host_event_registration()
    body = _host_complete_body()
    text = _json_with_integer_literals(
        body,
        ("action_id", 7, "7.0"),
        ("original_file_id", 31, "31.0"),
        ("original_size_bytes", 4096, "4096e0"),
        ("preview_file_id", 32, "32e0"),
        ("preview_size_bytes", 128, "128.0"),
        ("occurred_at", 0, "-1.0"),
        ("id", 31, "31.0"),
        ("id", 32, "32e0"),
        ("size_bytes", 4096, "4096e0"),
        ("size_bytes", 128, "128.0"),
    )

    event = decode_event_row(_row(body_json=text, event_type=26, occurred_at=-1))

    assert event.evidence["host_photo_request"]["original_size_bytes"] == Decimal("4096")
    assert type(event.rows[0].row_id) is int and event.rows[0].row_id == 31
    assert event.rows[0].after.values["size_bytes"] == Decimal("4096")


def test_history_decode_rejects_host_complete_branch_without_request():
    _host_event_registration()
    body = _host_complete_body()
    body["evidence"] = {}
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body, event_type=26))


def test_history_decode_keeps_generic_two_file_byte_lifecycle_without_photo_request():
    _host_event_registration()
    body = {
        "reason": 2,
        "evidence": {},
        "rows": [
            {"table": "intermediate_files", "id": 41,
             "before": {"exists": True, "values": {"size_bytes": None, "sha256": None}},
             "after": {"exists": True, "values": {"size_bytes": 4096, "sha256": "a" * 64}}},
            {"table": "intermediate_files", "id": 42,
             "before": {"exists": True, "values": {"size_bytes": None, "sha256": None}},
             "after": {"exists": True, "values": {"size_bytes": 128, "sha256": "b" * 64}}},
        ],
    }
    event = decode_event_row(_row(body, event_type=26))
    assert len(event.rows) == 2


def test_history_decode_rejects_host_complete_hash_that_contradicts_rows():
    _host_event_registration()
    body = _host_complete_body(original_sha256="c" * 64)
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body, event_type=26))


def test_history_decode_does_not_accept_boolean_as_complete_byte_size():
    _host_event_registration()
    body = _host_complete_body()
    body["evidence"]["host_photo_request"]["original_size_bytes"] = 1
    body["rows"][0]["after"]["values"]["size_bytes"] = True
    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body, event_type=26))


@pytest.mark.parametrize(
    "member,original,replacement",
    [
        ("action_id", 7, "true"),
        ("action_id", 7, "7.5"),
        ("action_id", 7, "9223372036854775808.0"),
        ("action_id", 7, "0.0"),
        ("occurred_at", 0, "false"),
        ("occurred_at", 0, "0.5"),
        ("occurred_at", 0, '"0"'),
    ],
)
def test_history_decode_rejects_invalid_prepare_integer_json_values(member, original, replacement):
    _host_event_registration()
    body = _host_prepare_body()
    text = _json_with_integer_literals(body, (member, original, replacement))

    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body_json=text, event_type=26))


@pytest.mark.parametrize(
    "member,original,replacement",
    [
        ("original_file_id", 31, "true"),
        ("original_file_id", 31, "31.5"),
        ("original_file_id", 31, "9223372036854775808.0"),
        ("original_size_bytes", 4096, "0.0"),
        ("original_size_bytes", 4096, "4096.5"),
        ("original_size_bytes", 4096, "9223372036854775808e0"),
    ],
)
def test_history_decode_rejects_invalid_complete_integer_json_values(member, original, replacement):
    _host_event_registration()
    body = _host_complete_body()
    text = _json_with_integer_literals(body, (member, original, replacement))

    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body_json=text, event_type=26))


@pytest.mark.parametrize(
    "member,original,replacement",
    [
        ("owner_action_id", 7, "7.5"),
        ("owner_action_id", 7, "9223372036854775808.0"),
        ("purpose", 5, "5.5"),
        ("retention_state", 1, "true"),
        ("retention_state", 1, "9223372036854775808.0"),
        ("cleanup_state", 1, "1.5"),
    ],
)
def test_history_decode_rejects_noninteger_prepare_row_values(member, original, replacement):
    _host_event_registration()
    body = _host_prepare_body()
    text = _json_with_integer_literals(body, (member, original, replacement))

    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body_json=text, event_type=26))


@pytest.mark.parametrize("literal", ["4096.5", "9223372036854775808e0"])
def test_history_decode_rejects_noninteger_or_oversized_complete_row_size(literal):
    _host_event_registration()
    body = _host_complete_body()
    text = _json_with_integer_literals(body, ("size_bytes", 4096, literal))

    with pytest.raises(ConsistencyError):
        decode_event_row(_row(body_json=text, event_type=26))


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
