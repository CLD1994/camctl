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
                "camera_take_photo": ActionCapability(
                    action_type="camera_take_photo",
                    parameter_type="single_shot",
                    name="单张拍摄",
                    description="拍摄一张照片。",
                    preview_supported=False,
                    schema=_SCHEMA,
                    defaults={"interval_s": Decimal("1.0")},
                )
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
