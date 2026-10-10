"""D1 同源静态能力与参数规则的单元测试。

期望独立来自能力说明格式与参数规则契约：默认值应用不改原输入，
显式 null 与非法值不被默认覆盖；定义缺失或无效必须失败；同一份
定义供导出与受理使用。
"""

from __future__ import annotations

from decimal import Decimal
from dataclasses import replace
from unittest.mock import create_autospec

import pytest
from video_estimate_helpers import (
    catalog_for_capabilities,
    record_capability,
    record_catalog,
)

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
                    "camera_take_photo": (ActionCapability(
                        action_type="camera_take_photo",
                        parameter_type="single_shot",
                        name="单张拍摄",
                        description="拍摄一张照片。",
                        preview_supported=False,
                        schema=_SINGLE_SHOT,
                        defaults={"quality": 5},
                    ),)
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
        definition = catalog.parameter_definition("cam-1", "camera_take_photo", "single_shot")
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
        schema = action["parameter_types"][0]["schema"]
        assert schema["properties"]["type"] == _SINGLE_SHOT["properties"]["type"]
        assert schema["properties"]["quality"]["default"] == 5
        assert "default" not in _SINGLE_SHOT["properties"]["quality"]

    def test_only_deployed_and_declared_capabilities(self) -> None:
        # 目录只导出已部署驱动且设备实际声明的能力（kind=camera）。
        config = _config({"cam-1": {"kind": "camera", "driver": "camctl-adb"}})
        catalog = build_catalog(config, _definitions())
        types = {a["type"] for a in catalog.describe_document()["devices"][0]["actions"]}
        assert types == {"camera_take_photo"}


def test_framework_support_does_not_depend_on_device_configuration():
    definitions = _definitions()
    empty = build_catalog(_config(), definitions)
    configured = build_catalog(_config({"cam-1":{"kind":"camera","driver":"camctl-adb"}}), definitions)
    assert empty.action_types() == configured.action_types()


def test_parameter_types_are_selected_from_same_catalog():
    capability = _definitions().drivers["camctl-adb"].actions["camera_take_photo"][0]
    alternate = ActionCapability(
        action_type="camera_take_photo", parameter_type="alternate", name="其他任务",
        description="同一能力的另一参数类型。", preview_supported=True,
        schema={**_SINGLE_SHOT, "properties":{**_SINGLE_SHOT["properties"], "type":{"const":"alternate"}}}, defaults={"quality":7},
    )
    definitions = DriverDefinitions(drivers={"camctl-adb":DriverDefinition(
        driver_id="camctl-adb", actions={"camera_take_photo":(capability, alternate)},
    )})
    catalog = build_catalog(_config({"cam-1":{"kind":"camera","driver":"camctl-adb"}}), definitions)
    selected = catalog.parameter_definition("cam-1", "camera_take_photo", "alternate")
    assert selected.defaults == {"quality":7}
    assert selected.preview_supported is True
    assert catalog.parameter_definition("cam-1", "camera_take_photo", "unknown") is None
    assert catalog.device_supports("cam-1", "camera_record") is False
    assert len(catalog.describe_document()["devices"][0]["actions"][0]["parameter_types"]) == 2


def test_empty_declared_capability_is_definition_error():
    definitions = DriverDefinitions({"camctl-adb":DriverDefinition("camctl-adb", {"camera_take_photo":()})})
    with pytest.raises(ValueError):
        build_catalog(_config({"cam-1":{"kind":"camera","driver":"camctl-adb"}}), definitions)


def test_nested_default_annotations_follow_effective_object_default():
    from dataclasses import replace
    capability = _definitions().drivers["camctl-adb"].actions["camera_take_photo"][0]
    schema = {**_SINGLE_SHOT, "properties":{**_SINGLE_SHOT["properties"],
        "settings":{"type":"object", "properties":{"quality":{"type":"integer","default":1}}},
        "payload":{"const":{"default":17}},
    }, "$defs":{"unused":{"type":"integer","default":3}}}
    cap = replace(capability, schema=schema, defaults={"settings":{"quality":2}})
    definitions = DriverDefinitions({"camctl-adb":DriverDefinition("camctl-adb", {"camera_take_photo":(cap,)})})
    catalog = build_catalog(_config({"cam-1":{"kind":"camera","driver":"camctl-adb"}}),definitions)
    selected = catalog.parameter_definition("cam-1","camera_take_photo","single_shot")
    assert selected.schema["properties"]["settings"]["default"] == {"quality":2}
    assert "default" not in selected.schema["properties"]["settings"]["properties"]["quality"]
    assert "default" not in selected.schema["$defs"]["unused"]
    assert selected.schema["properties"]["payload"]["const"] == {"default":17}
    assert apply_defaults({"type":"single_shot"}, selected.defaults)["settings"] == {"quality":2}
    assert apply_defaults({"type":"single_shot","settings":{}}, selected.defaults)["settings"] == {}


class TestVideoEstimateMetadata:
    @pytest.fixture(autouse=True)
    def isolate_schema_validation(self, monkeypatch):
        from camctl.devices import catalog as module

        monkeypatch.setattr(module, "validate_parameter_schema",
                            create_autospec(module.validate_parameter_schema))

    def test_describe_exports_decimal_estimate(self):
        document = record_catalog({
            "bitrate_mbps": Decimal("130.125"),
            "duration": {"method": "direct", "seconds": {
                "source": "constant", "value": 60,
            }},
        }).describe_document()
        parameter = document["devices"][0]["actions"][0]["parameter_types"][0]
        assert parameter["video_size_estimate"] == {
            "bitrate_mbps": Decimal("130.125"),
            "duration": {"method": "direct", "seconds": {
                "source": "constant", "value": 60,
            }},
        }
        assert "video_size_estimate" not in parameter["schema"]["properties"]

    def test_undeclared_estimate_is_omitted(self):
        parameter = record_catalog(None).describe_document()[
            "devices"][0]["actions"][0]["parameter_types"][0]
        assert "video_size_estimate" not in parameter

    def test_exported_nested_estimate_does_not_modify_source(self):
        estimate = {
            "bitrate_mbps": {"by": "/mode", "values": {"high": 130}},
            "duration": {"method": "direct", "seconds": {
                "source": "constant", "value": 60,
            }},
        }
        catalog = record_catalog(estimate)
        document = catalog.describe_document()
        exported = document["devices"][0]["actions"][0]["parameter_types"][0]
        exported["video_size_estimate"]["bitrate_mbps"]["values"]["high"] = 1
        exported["video_size_estimate"]["duration"]["seconds"]["value"] = 1
        assert estimate["bitrate_mbps"]["values"]["high"] == 130
        assert estimate["duration"]["seconds"]["value"] == 60
        fresh = catalog.describe_document()["devices"][0]["actions"][0]["parameter_types"][0]
        assert fresh["video_size_estimate"] == estimate

    def test_execution_definition_does_not_include_estimate(self):
        capability = replace(record_capability({
            "bitrate_mbps": 130, "duration": {"method": "direct", "seconds": {
                "source": "constant", "value": 60,
            }},
        }), defaults={"quality": 5})
        capability.schema["properties"]["quality"] = {"type": "integer"}
        catalog = catalog_for_capabilities(capability)
        selected = catalog.parameter_definition("cam-1", "camera_record", "estimate_record")
        assert selected is not None
        assert set(vars(selected)) == {"schema", "defaults", "preview_supported", "task_factory"}
        assert selected.defaults == {"quality": 5}
        assert apply_defaults({"type": "estimate_record"}, selected.defaults) == {
            "type": "estimate_record", "quality": 5,
        }
        assert selected.task_factory is capability.task_factory
        assert "video_size_estimate" not in selected.schema

    @pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
    @pytest.mark.parametrize("task_factory", [None, "not_callable"])
    def test_estimate_does_not_make_candidate_executable(self, action_type, task_factory):
        candidate = replace(record_capability({
            "bitrate_mbps": 130, "duration": {"method": "direct", "seconds": {
                "source": "constant", "value": 60,
            }},
        }), action_type=action_type, task_factory=task_factory)
        catalog = catalog_for_capabilities(candidate)
        assert catalog.describe_document() == {"devices": [{
            "device_id": "cam-1", "driver_id": "estimate_demo", "actions": [],
        }]}
        assert not catalog.device_supports("cam-1", action_type)
        assert catalog.parameter_definition("cam-1", action_type, "estimate_record") is None

    def test_legacy_positional_definition_omits_estimate(self):
        original = record_capability(None)
        legacy = ActionCapability(
            original.action_type, original.parameter_type, original.name,
            original.description, original.preview_supported, original.schema,
            original.defaults, original.task_factory,
        )
        parameter = catalog_for_capabilities(legacy).describe_document()[
            "devices"][0]["actions"][0]["parameter_types"][0]
        assert "video_size_estimate" not in parameter
