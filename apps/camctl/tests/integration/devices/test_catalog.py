"""D1 同源能力目录的组件集成测试。

同一份驱动定义驱动 describe 导出与受理校验：导出文档通过公共
capabilities Schema，受理用同一 Schema 与默认值校验参数。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.acceptance.rules import validate_new_body
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.cli import encode_describe_document
from camctl.contracts.schemas import SchemaValidationError
from camctl.devices.catalog import (
    ActionCapability,
    DriverDefinition,
    DriverDefinitions,
    build_catalog,
)

_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "type": {"const": "single_shot"},
        "quality": {"type": "integer", "minimum": 1, "maximum": 10},
        "interval_s": {"type": "number", "multipleOf": Decimal("0.5")},
    },
    "required": ["type", "quality"],
    "additionalProperties": False,
}

_DEFINITIONS = DriverDefinitions(
    drivers={
        "camctl-adb": DriverDefinition(
            driver_id="camctl-adb",
            actions={
                "camera_take_photo": (ActionCapability(
                    action_type="camera_take_photo",
                    parameter_type="single_shot",
                    name="单张拍摄",
                    description="拍摄一张照片。",
                    preview_supported=False,
                    schema=_SCHEMA,
                    defaults={"interval_s": Decimal("1.0")},
                ),)
            },
        )
    }
)


def _catalog(devices: dict | None = None):
    config = load_config(
        {"devices": devices or {"cam-1": {"kind": "camera", "driver": "camctl-adb"}}},
        ConfigDefaults(),
    )
    return build_catalog(config, _DEFINITIONS)


def _plan(params: dict) -> dict:
    return {
        "request_id": "42",
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": params,
                "policy": {"max_delay_ms": 1000},
            }
        ],
    }


class TestSameSourceRules:
    def test_describe_document_passes_public_schema(self) -> None:
        catalog = _catalog()
        payload = encode_describe_document(catalog.describe_document())
        assert payload.endswith(b"\n")

    def test_acceptance_uses_same_schema_and_defaults(self) -> None:
        catalog = _catalog()
        decision = validate_new_body(
            _plan({"type": "single_shot", "quality": 8}), catalog
        )
        assert not decision.is_whole_rejection
        action = decision.actions[0]
        assert action.ok
        # 同源默认值补齐（interval_s 省略 → 1.0），原输入保持。
        assert action.effective_params["interval_s"] == Decimal("1.0")

    def test_acceptance_rejects_what_schema_rejects(self) -> None:
        catalog = _catalog()
        decision = validate_new_body(
            _plan({"type": "single_shot", "quality": 99}), catalog
        )
        assert not decision.is_whole_rejection
        assert not decision.actions[0].ok

    def test_exact_multiple_from_same_schema(self) -> None:
        catalog = _catalog()
        ok = validate_new_body(
            _plan({"type": "single_shot", "quality": 1, "interval_s": Decimal("1.5")}),
            catalog,
        )
        assert ok.actions[0].ok
        bad = validate_new_body(
            _plan({"type": "single_shot", "quality": 1, "interval_s": Decimal("1.3")}),
            catalog,
        )
        assert not bad.actions[0].ok

    def test_document_rejected_by_schema_when_malformed(self) -> None:
        catalog = _catalog()
        document = catalog.describe_document()
        document["devices"][0]["actions"][0]["parameter_types"][0].pop("schema")
        with pytest.raises(SchemaValidationError):
            encode_describe_document(document)


@pytest.mark.parametrize("device_id, supported", [("cam-1", True), ("cam-2", False)])
@pytest.mark.parametrize("extra_device", [False, True])
def test_device_support_keeps_local_failure_scope(device_id, supported, extra_device):
    devices = {"cam-1":{"kind":"camera", "driver":"camctl-adb"},
        "cam-2":{"kind":"camera", "driver":"other"}}
    if extra_device:
        devices["cam-3"] = {"kind":"camera", "driver":"camctl-adb"}
    definitions = DriverDefinitions({**_DEFINITIONS.drivers,
        "other":DriverDefinition("other", {})})
    catalog = build_catalog(load_config({"devices":devices}, ConfigDefaults()), definitions)
    body = _plan({"type":"single_shot", "quality":8})
    body["actions"][0]["device_id"] = device_id
    decision = validate_new_body(body, catalog)
    assert not decision.is_whole_rejection
    assert decision.actions[0].ok is supported
    encode_describe_document(catalog.describe_document())


def test_alternate_parameter_type_has_same_exported_and_effective_defaults():
    from dataclasses import replace
    capability = _DEFINITIONS.drivers["camctl-adb"].actions["camera_take_photo"][0]
    alternate = replace(capability, parameter_type="alternate", preview_supported=True,
        schema={**_SCHEMA,"properties":{**_SCHEMA["properties"],"type":{"const":"alternate"}}},
        defaults={"interval_s":Decimal("2.0")})
    definitions = DriverDefinitions({"camctl-adb":DriverDefinition("camctl-adb",
        {"camera_take_photo":(capability, alternate)})})
    catalog = build_catalog(load_config({"devices":{"cam-1":{"kind":"camera","driver":"camctl-adb"}}}, ConfigDefaults()), definitions)
    document = catalog.describe_document()
    encode_describe_document(document)
    exported = document["devices"][0]["actions"][0]["parameter_types"][1]
    assert exported["type"] == "alternate"
    assert exported["schema"]["properties"]["interval_s"]["default"] == Decimal("2.0")
    decision = validate_new_body(_plan({"type":"alternate","quality":8}), catalog)
    assert decision.actions[0].ok
    assert decision.actions[0].effective_params == {"type":"alternate","quality":8,"interval_s":Decimal("2.0")}
    assert decision.actions[0].parameter_definition.preview_supported is True


@pytest.mark.parametrize("changes", [
    {"$schema":None}, {"$schema":"https://json-schema.org/draft-07/schema"},
    {"type":"invalid-type"}, {"type":"array"}, {"required":[]},
    {"properties":{"type":{"const":"other"}}},
    {"properties":{"type":{"const":"single_shot"},"extra":{"$ref":"missing.schema.json"}}},
    {"properties":{"type":{"const":"single_shot"},"extra":{"$ref":"plan.schema.json#/$defs/source"}}},
])
def test_invalid_parameter_definition_fails_describe_and_acceptance(changes):
    from dataclasses import replace
    from camctl.acceptance.schema import RuleError
    capability = _DEFINITIONS.drivers["camctl-adb"].actions["camera_take_photo"][0]
    bad = replace(capability, schema={**_SCHEMA, **changes})
    definitions = DriverDefinitions({"camctl-adb":DriverDefinition("camctl-adb", {"camera_take_photo":(bad,)})})
    config = load_config({"devices":{"cam-1":{"kind":"camera","driver":"camctl-adb"}}}, ConfigDefaults())
    catalog = build_catalog(config, definitions)
    with pytest.raises(RuleError):
        catalog.describe_document()
    with pytest.raises(RuleError):
        validate_new_body(_plan({"type":"single_shot","quality":8}), catalog)


def test_valid_parameter_local_reference_is_self_contained():
    from dataclasses import replace
    capability = _DEFINITIONS.drivers["camctl-adb"].actions["camera_take_photo"][0]
    schema = {**_SCHEMA, "$defs":{"quality":{"type":"integer","minimum":1,"maximum":10}},
        "properties":{**_SCHEMA["properties"],"quality":{"$ref":"#/$defs/quality"}}}
    cap = replace(capability, schema=schema)
    defs = DriverDefinitions({"camctl-adb":DriverDefinition("camctl-adb", {"camera_take_photo":(cap,)})})
    config = load_config({"devices":{"cam-1":{"kind":"camera","driver":"camctl-adb"}}},ConfigDefaults())
    catalog = build_catalog(config, defs)
    assert validate_new_body(_plan({"type":"single_shot","quality":8}),catalog).actions[0].ok
    encode_describe_document(catalog.describe_document())
