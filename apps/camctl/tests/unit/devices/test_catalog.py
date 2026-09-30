"""D1 同源静态能力与参数规则的单元测试。

期望独立来自能力说明格式与参数规则契约：默认值应用不改原输入，
显式 null 与非法值不被默认覆盖；定义缺失或无效必须失败；同一份
定义供导出与受理使用。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.devices.catalog import (
    ActionCapability,
    DriverDefinition,
    DriverDefinitions,
    apply_defaults,
    build_catalog,
)

_SINGLE_SHOT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "type": {"const": "single_shot"},
        "quality": {"type": "integer", "minimum": 1, "maximum": 10},
        "interval_s": {"type": "number", "multipleOf": Decimal("0.5")},
    },
    "required": ["type"],
    "additionalProperties": False,
}


def _definitions() -> DriverDefinitions:
    return DriverDefinitions(
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
                        schema=_SINGLE_SHOT,
                        defaults={"quality": 5},
                    )
                },
            )
        }
    )


def _config(devices: dict | None = None):
    return load_config(
        {"devices": devices} if devices is not None else {},
        ConfigDefaults(),
    )


class TestApplyDefaults:
    def test_defaults_preserve_raw_input(self) -> None:
        raw = {"type": "single_shot"}
        effective = apply_defaults(raw, defaults={"quality": 5})
        assert raw == {"type": "single_shot"}
        assert effective == {"type": "single_shot", "quality": 5}

    def test_explicit_null_not_overridden_by_default(self) -> None:
        raw = {"type": "single_shot", "quality": None}
        effective = apply_defaults(raw, defaults={"quality": 5})
        assert effective["quality"] is None

    def test_explicit_value_wins(self) -> None:
        effective = apply_defaults(
            {"type": "single_shot", "quality": 9}, defaults={"quality": 5}
        )
        assert effective["quality"] == 9

    def test_extra_keys_pass_through(self) -> None:
        effective = apply_defaults(
            {"type": "single_shot", "interval_s": Decimal("1.5")}, defaults={"quality": 5}
        )
        assert effective["interval_s"] == Decimal("1.5")


class TestBuildCatalog:
    def test_catalog_matches_declared_device(self) -> None:
        config = _config({"cam-1": {"kind": "camera", "driver": "camctl-adb"}})
        catalog = build_catalog(config, _definitions())
        assert catalog.device_exists("cam-1")
        assert catalog.driver_id("cam-1") == "camctl-adb"
        assert "camera_take_photo" in catalog.action_types()
        definition = catalog.parameter_definition("cam-1", "camera_take_photo")
        assert definition is not None
        assert definition.defaults == {"quality": 5}

    def test_undeclared_device_absent(self) -> None:
        config = _config()
        catalog = build_catalog(config, _definitions())
        assert not catalog.device_exists("cam-1")

    def test_missing_driver_definition_fails(self) -> None:
        config = _config({"cam-1": {"kind": "camera", "driver": "unknown-driver"}})
        with pytest.raises(ValueError, match="unknown-driver"):
            build_catalog(config, _definitions())

    def test_empty_catalog_is_legal(self) -> None:
        catalog = build_catalog(_config(), DriverDefinitions(drivers={}))
        assert catalog.action_types()
        document = catalog.describe_document()
        assert document == {"devices": []}


class TestDescribeDocument:
    def test_document_contains_declared_capabilities(self) -> None:
        config = _config({"cam-1": {"kind": "camera", "driver": "camctl-adb"}})
        catalog = build_catalog(config, _definitions())
        document = catalog.describe_document()
        assert document["devices"][0]["device_id"] == "cam-1"
        assert document["devices"][0]["driver_id"] == "camctl-adb"
        action = document["devices"][0]["actions"][0]
        assert action["type"] == "camera_take_photo"
        assert action["parameter_types"][0]["type"] == "single_shot"
        assert action["parameter_types"][0]["schema"] == _SINGLE_SHOT

    def test_only_deployed_and_declared_capabilities(self) -> None:
        # 目录只导出已部署驱动且设备实际声明的能力（kind=camera）。
        config = _config({"cam-1": {"kind": "camera", "driver": "camctl-adb"}})
        catalog = build_catalog(config, _definitions())
        types = {a["type"] for a in catalog.describe_document()["devices"][0]["actions"]}
        assert types == {"camera_take_photo"}
