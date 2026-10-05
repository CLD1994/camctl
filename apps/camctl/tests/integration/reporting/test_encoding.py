"""受理事实、冻结范围、真实公开投影、字段登记与确定字节的组合。

小型数据库在冻结后不再写入，测试显式读取其中的入选行；此处不
验证历史恢复或分页，不把当前投影读取作为固定 H 的生产实现。
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.rules import extract_request_identity
from camctl.acceptance.service import CommandMode, PlanDisposition, ProcessInput
from camctl.contracts import public_projection
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, register_acceptance_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts
from camctl.reporting.encoding import ReportDocument, encode_report, iter_report_chunks
from camctl.reporting.policy import ReportingRepository, register_report_guards

from ..acceptance.test_acceptance import Catalog, _NOW
from ..persistence.test_runtime import _create_valid_database
from unit.reporting.test_encoding import _action, _diagnostic, _obtain, _output, _plan

register_acceptance_guards()
register_report_guards()

_EXPECTED = (
    '{"from_wm":0,"report_id":"1","to_wm":2,"plans":[{"created_at":"2026-01-15 08:00:00",'
    '"name":"plan","plan_instance_id":"1","request_id":"42","status":"pending","actions":['
    '{"action_instance_id":"1","device_id":"cam-1","effective_params":{"quality":1.5,"type":"single_shot"},'
    '"input_params":{"quality":1.5,"type":"single_shot"},"name":"shoot","policy":{"max_delay_ms":1000},'
    '"scheduled_at":"2026-01-15 09:00:00","status":"pending","type":"camera_take_photo"}]}]}\n'
).encode("utf-8")


class QualityCatalog(Catalog):
    def parameter_definition(self, device_id, action_type, parameter_type):
        if device_id == "cam-1" and action_type == "camera_take_photo" and parameter_type == "single_shot":
            return ParameterDefinition(schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object", "properties": {
                    "type": {"const": "single_shot"}, "quality": {"type": "number"}},
                "required": ["type", "quality"], "additionalProperties": False,
            }, defaults={})
        return None


@pytest.fixture
def accepted_report(tmp_path):
    path = tmp_path / "state.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        body = parse_exact_json('''{
            "request_id":"42","created_at":"2026-01-15 08:00:00","name":"plan",
            "actions":[{"name":"shoot","type":"camera_take_photo","device_id":"cam-1",
                        "scheduled_at":"2026-01-15 09:00:00","policy":{"max_delay_ms":1000},
                        "params":{"type":"single_shot","quality":1.50}}]
        }''')
        accepted = AcceptanceRepository().process_input(
            ProcessInput(ParsedInput("original.json", body), QualityCatalog(), CommandMode.RUN, _NOW),
            new_operation_key(), owned,
        )
        assert accepted.kind is DbOutcomeKind.COMPLETED
        assert accepted.value.plan_disposition is PlanDisposition.REGISTERED
        frozen = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=_NOW)
        assert frozen.kind is DbOutcomeKind.COMPLETED
        report = frozen.value.report
        facts = {}
        for table in ("plans", "actions"):
            row = row_facts(owned.connection, table, 1)
            assert row is not None
            facts[table] = {1: row}
        document = ReportDocument(str(report.report_id), report.from_wm, report.to_wm,
                                  (("plan", 1, {"action": {1: {}}}),))
        yield document, facts
    finally:
        owned.connection.close()


def test_real_acceptance_projection_encoding_matches_independent_bytes(accepted_report):
    document, facts = accepted_report
    for capacity in (1, 7, 64, 8192):
        chunks = tuple(iter_report_chunks(document, facts, buffer_size=capacity))
        assert all(0 < len(chunk) <= capacity for chunk in chunks)
        encoded = b"".join(chunks)
        assert encoded == _EXPECTED
        assert sha256(encoded).hexdigest() == sha256(_EXPECTED).hexdigest()
        decoded = parse_exact_json(encoded.decode("utf-8"))
        validate_document("protocol/status-report.schema.json", decoded)
        assert decoded["plans"][0]["actions"][0]["effective_params"]["quality"] == Decimal("1.5")


def test_shared_registration_field_reordering_preserves_bytes(accepted_report, monkeypatch):
    document, facts = accepted_report
    original_read = public_projection.resource_bytes
    registration = json.loads(original_read("registry/report-dependencies.json"))
    for definition in registration["projections"].values():
        definition["fields"] = dict(reversed(list(definition["fields"].items())))
    altered = json.dumps(registration, ensure_ascii=False).encode("utf-8")
    def read(name):
        return altered if name == "registry/report-dependencies.json" else original_read(name)
    monkeypatch.setattr(public_projection, "resource_bytes", read)
    public_projection._dependencies.cache_clear()
    try:
        assert encode_report(document, facts) == _EXPECTED
        assert public_projection.projection_structure("report").entity_fields == (
            ("plans", "plan"), ("plan_file_diagnostics", "diagnostic"))
        assert public_projection.projection_structure("action").entity_fields == (
            ("outputs", "output"), ("deliveries", "delivery"))
    finally:
        public_projection._dependencies.cache_clear()


def test_successful_unit_projection_examples_match_real_schema():
    assert _diagnostic("2")["errors"] == [extract_request_identity({}).error]
    plan = _plan()
    camera = _action()
    camera["status"] = "succeeded"
    camera["outputs"] = [_output("10"), _output("2")]
    plan["actions"] = [camera, _obtain()]
    validate_document("protocol/status-report.schema.json", {
        "report_id": "7", "from_wm": 0, "to_wm": 5,
        "plans": [plan], "plan_file_diagnostics": [_diagnostic("2")],
    })
    ordinary = deepcopy(plan)
    ordinary["actions"] = [_action()]
    ordinary["actions"][0]["effective_params"].update({
        "value": '中文😀/"\\\b\f\n\r\t\x00\x1f',
        "outputs": [{"output_id": "10"}, {"output_id": "2"}],
        "empty_array": [], "empty_object": {}, "null": None,
    })
    validate_document("protocol/status-report.schema.json", {
        "report_id": "7", "from_wm": 0, "to_wm": 5, "plans": [ordinary],
    })
