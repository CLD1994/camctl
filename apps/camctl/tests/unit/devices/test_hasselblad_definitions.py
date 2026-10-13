"""Hasselblad 静态能力须同时驱动说明导出和首次受理。"""

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.devices.catalog import DriverDefinitions, build_catalog
def test_hasselblad_photo_definition_is_exported_with_its_fixed_task():
    from jsonschema import Draft202012Validator

    from camctl.devices.drivers.hasselblad.definitions import (
        DRIVER_ID,
        PARAMETER_TYPE,
        capability,
        driver_definition,
    )
    from camctl.devices.tasks import CaptureStorage

    definition = driver_definition()
    config = load_config(
        {
            "devices": {
                "hb0": {
                    "kind": "camera",
                    "driver": DRIVER_ID,
                    "hasselblad": {
                        "executable": "/home/cld/hb_cli/hb_cli",
                        "serial": "64010904",
                    },
                }
            }
        },
        ConfigDefaults(),
    )
    catalog = build_catalog(config, DriverDefinitions({DRIVER_ID: definition}))
    description = catalog.document()["devices"][0]["actions"][0]["parameter_types"][0]
    parameter = catalog.parameter_definition("hb0", "camera_take_photo", PARAMETER_TYPE)

    expected_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"type": {"const": "hasselblad_photo"}},
        "required": ["type"],
        "additionalProperties": False,
        "default": {},
    }
    assert definition.driver_id == "hasselblad_cfv100c"
    assert description["type"] == "hasselblad_photo"
    assert description["preview_supported"] is True
    assert description["schema"] == expected_schema
    assert all(word in description["description"] for word in ("单张", "RAW", "曝光", "白平衡", "对焦"))
    assert parameter is not None
    assert parameter.defaults == {}
    validator = Draft202012Validator(description["schema"])
    assert validator.is_valid({"type": "hasselblad_photo"})
    assert not validator.is_valid({})
    assert not validator.is_valid({"type": "hasselblad_photo", "iso": 800})
    assert "default" not in capability().schema

    task = parameter.task_factory({"type": "hasselblad_photo"})
    assert task.action_type == "camera_take_photo"
    assert task.output_storage is CaptureStorage.HOST
    assert task.start_call_timeout_s.as_tuple() == (0, (1, 8, 0), 0)
