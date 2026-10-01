"""编码器的局部行为；投影、字段结构与 Schema 均在稳定边界隔离。"""
from __future__ import annotations

import json
from decimal import Decimal, localcontext
from unittest.mock import create_autospec

import pytest

from camctl.contracts.public_projection import ProjectionStructure, PublicProjectionError
from camctl.reporting import encoding
from camctl.reporting.encoding import ReportDocument, encode_number, encode_report, iter_report_chunks


def _document(ids=(1,)):
    return ReportDocument("7", 0, 5, tuple(("plan", identity, ("action", (5,))) for identity in ids))


def _action(identity="5"):
    return {"status": "pending", "type": "camera_take_photo", "action_instance_id": identity,
            "name": "shoot", "device_id": "cam-1", "scheduled_at": "2025-01-15 09:00:00",
            "policy": {"max_delay_ms": 1000},
            "input_params": {"type": "single_shot", "quality": Decimal("1.50")},
            "effective_params": {"type": "single_shot", "quality": Decimal("1.50")}}


def _plan(identity="1"):
    return {"status": "running", "request_id": "42", "plan_instance_id": identity,
            "name": "morning", "created_at": "2025-01-15 08:00:00", "actions": [_action()]}


def _output(identity):
    return {"output_id": identity, "source_action_instance_id": "5", "kind": "original",
            "availability": "available", "cleanup": {"status": "not_requested"},
            "checksum": {"status": "not_obtained"},
            "media": {"check_status": "not_performed", "duration": {"status": "unknown"}}}


def _delivery(identity):
    return {"delivery_id": identity, "output_id": identity, "source_action_instance_id": "5",
            "file_name": identity + ".jpg", "display_name": "picture.jpg", "status": "pending"}


def _obtain():
    return {"action_instance_id": "6", "name": "obtain", "type": "obtain_action_outputs",
            "scheduled_at": "2025-01-15 10:00:00", "status": "running",
            "input_params": {"source": {"action_name": "shoot"}, "purpose": "manual"},
            "result": {"failures": []}, "deliveries": [_delivery("10"), _delivery("2")]}


def _diagnostic(identity):
    return {"diagnostic_id": identity, "file_name": "invalid.json", "errors": [
        {"code": "invalid_request_id", "stage": "admission",
         "details": {"field": "request_id", "reason": "required"}}]}


@pytest.fixture(autouse=True)
def fragments(monkeypatch):
    values = {("plan", 1): _plan()}
    structures = {
        "report": ProjectionStructure("report_id", (("plans", "plan"), ("plan_file_diagnostics", "diagnostic"))),
        "plan": ProjectionStructure("plan_instance_id", (("actions", "action"),)),
        "action": ProjectionStructure("action_instance_id", (("outputs", "output"), ("deliveries", "delivery"))),
        "output": ProjectionStructure("output_id", ()),
        "delivery": ProjectionStructure("delivery_id", ()),
        "diagnostic": ProjectionStructure("diagnostic_id", ()),
    }
    def project(facts):
        try:
            return values[(facts.entity, facts.root_id)]
        except KeyError as error:
            raise PublicProjectionError("missing frozen entity") from error
    monkeypatch.setattr(encoding, "project_public", create_autospec(encoding.project_public, side_effect=project))
    monkeypatch.setattr(encoding, "projection_structure", create_autospec(encoding.projection_structure, side_effect=structures.__getitem__))
    monkeypatch.setattr(encoding, "validate_document", create_autospec(encoding.validate_document, return_value=None))
    return values


def test_same_facts_same_bytes(fragments):
    first = encode_report(_document(), {})
    fragments[("plan", 1)] = dict(reversed(list(fragments[("plan", 1)].items())))
    assert encode_report(_document(), {}) == first
    assert first.endswith(b"}\n") and not first.endswith(b"\n\n")


def test_root_fields_precede_entity_collections_in_fixed_order():
    payload = json.loads(encode_report(_document(), {}))
    assert list(payload) == ["from_wm", "report_id", "to_wm", "plans"]
    assert list(payload["plans"][0]) == ["created_at", "name", "plan_instance_id", "request_id", "status", "actions"]


def test_equal_exact_values_have_identical_report_bytes(fragments):
    first = encode_report(_document(), {})
    fragments[("plan", 1)]["actions"][0]["effective_params"]["quality"] = Decimal("1.5")
    assert encode_report(_document(), {}) == first


@pytest.mark.parametrize("value,want", [
    (0, b"0"), (Decimal("-0.00"), b"0"), (1, b"1"), (Decimal("1.000"), b"1"),
    (Decimal("1.2500"), b"1.25"), (Decimal("1.0000000000000001"), b"1.0000000000000001"),
    (Decimal("0.00000099999999999999"), b"9.9999999999999e-7"),
    (Decimal("1e-6"), b"0.000001"), (Decimal("0.00000012"), b"1.2e-7"),
    (Decimal("-0.00000012"), b"-1.2e-7"), (Decimal("1e20"), b"100000000000000000000"),
    (Decimal("999999999999999999999"), b"999999999999999999999"),
    (Decimal("1e21"), b"1e21"), (-10**21, b"-1e21"),
    (9007199254740993, b"9007199254740993"), (10**5000, b"1e5000"),
], ids=lambda value: "large-integer" if isinstance(value, int) and value.bit_length() > 1000 else None)
def test_report_numbers_follow_value_partitions(value, want):
    with localcontext() as context:
        context.prec = 1
        assert encode_number(value) == want


@pytest.mark.parametrize("value", [True, False, 1.0, None, "1", Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_numbers_reject_non_exact_or_non_finite_values(value):
    with pytest.raises(ValueError):
        encode_number(value)


@pytest.mark.parametrize("ids,want", [
    ((10, 2, 100), ["2", "10", "100"]),
    ((9007199254740993, 9007199254740992), ["9007199254740992", "9007199254740993"]),
])
def test_entity_order_uses_integer_ids(fragments, ids, want):
    for identity in ids:
        fragments[("plan", identity)] = _plan(str(identity))
    payload = json.loads(encode_report(_document(ids), {}))
    assert [plan["plan_instance_id"] for plan in payload["plans"]] == want


@pytest.mark.parametrize("identity", [True, 0, -1, 1.0, "1", 2**63])
def test_root_entity_identity_is_strict(identity):
    with pytest.raises(ValueError):
        encode_report(_document((identity,)), {})


def test_duplicate_root_entity_identity_is_rejected():
    with pytest.raises(ValueError):
        encode_report(_document((1, 1)), {})


def test_child_entities_are_sorted_by_integer_identity(fragments):
    fragments[("plan", 1)]["actions"] = [_action("10"), _action("2"), _action("100")]
    payload = json.loads(encode_report(_document(), {}))
    assert [action["action_instance_id"] for action in payload["plans"][0]["actions"]] == ["2", "10", "100"]


def test_duplicate_child_entity_identity_is_rejected(fragments):
    fragments[("plan", 1)]["actions"] = [_action(), _action()]
    with pytest.raises(ValueError):
        encode_report(_document(), {})


def test_action_self_fields_precede_declared_child_collections(fragments):
    action = fragments[("plan", 1)]["actions"][0]
    action["status"] = "succeeded"
    action["outputs"] = [_output("10"), _output("2")]
    fragments[("plan", 1)]["actions"].append(_obtain())
    payload = json.loads(encode_report(_document(), {}))
    camera, obtain = payload["plans"][0]["actions"]
    assert list(camera)[-1] == "outputs"
    assert list(obtain)[-1] == "deliveries"
    assert [item["output_id"] for item in camera["outputs"]] == ["2", "10"]
    assert [item["delivery_id"] for item in obtain["deliveries"]] == ["2", "10"]


def test_diagnostics_follow_plans_and_use_integer_identity_order(fragments):
    for identity in (10, 2, 100):
        fragments[("diagnostic", identity)] = _diagnostic(str(identity))
    document = ReportDocument("7", 0, 5, _document().plans,
                              tuple(("diagnostic", identity, ("", ())) for identity in (10, 2, 100)))
    payload = json.loads(encode_report(document, {}))
    assert list(payload)[-2:] == ["plans", "plan_file_diagnostics"]
    assert [item["diagnostic_id"] for item in payload["plan_file_diagnostics"]] == ["2", "10", "100"]


def test_duplicate_diagnostic_identity_is_rejected(fragments):
    fragments[("diagnostic", 2)] = _diagnostic("2")
    document = ReportDocument("7", 0, 5, diagnostics=(("diagnostic", 2, ("", ())),) * 2)
    with pytest.raises(ValueError):
        encode_report(document, {})


def test_parameter_arrays_are_preserved_when_names_match_entity_collections(fragments):
    action = fragments[("plan", 1)]["actions"][0]
    action["effective_params"] = {"type": "single_shot", "outputs": [{"output_id": "10"}, {"output_id": "2"}],
                                  "empty_array": [], "empty_object": {}, "null": None}
    payload = json.loads(encode_report(_document(), {}))
    params = payload["plans"][0]["actions"][0]["effective_params"]
    assert list(params) == ["empty_array", "empty_object", "null", "outputs", "type"]
    assert params["outputs"] == [{"output_id": "10"}, {"output_id": "2"}]
    assert params["empty_array"] == [] and params["empty_object"] == {} and params["null"] is None


def test_empty_entity_collections_are_omitted(fragments):
    fragments[("plan", 1)]["actions"] = []
    assert "actions" not in json.loads(encode_report(_document(), {}))["plans"][0]


@pytest.mark.parametrize("capacity", [1, 3, 7, 64, 8192])
def test_streaming_buffer_has_a_fixed_capacity(capacity):
    parts = list(iter_report_chunks(ReportDocument("7", 0, 5), {}, buffer_size=capacity))
    assert all(0 < len(part) <= capacity for part in parts)
    assert b"".join(parts) == b'{"from_wm":0,"report_id":"7","to_wm":5}\n'


@pytest.mark.parametrize("capacity", [True, False, 0, -1, 1.0, "1"])
def test_invalid_buffer_capacity_is_rejected(capacity):
    with pytest.raises(ValueError):
        list(iter_report_chunks(_document(), {}, buffer_size=capacity))


def test_streaming_starts_before_a_later_entity_is_read():
    stream = iter_report_chunks(_document((1, 2)), {}, buffer_size=1)
    assert next(stream) == b"{"
    written = bytearray(b"{")
    with pytest.raises(PublicProjectionError):
        for part in stream:
            written.extend(part)
    assert b'"plan_instance_id":"1"' in written
    assert not written.endswith(b"}\n")


def test_string_escaping_and_utf8_splits_preserve_exact_bytes(fragments):
    fragments[("plan", 1)]["actions"][0]["effective_params"] = {"type": "single_shot", "value": '中文😀/"\\\b\f\n\r\t\x00\x1f'}
    parts = list(iter_report_chunks(_document(), {}, buffer_size=3))
    combined = b"".join(parts)
    assert '中文😀/'.encode("utf-8") + b'\\"\\\\\\b\\f\\n\\r\\t\\u0000\\u001f' in combined
    assert not combined.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("value", [1.5, Decimal("NaN"), chr(0xD800)])
def test_invalid_json_fields_abort_generation(fragments, value):
    fragments[("plan", 1)]["actions"][0]["effective_params"] = {"type": "single_shot", "value": value}
    with pytest.raises(ValueError):
        encode_report(_document(), {})
