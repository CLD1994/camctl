"""Hasselblad 静态能力须同时驱动说明导出和首次受理。"""

from decimal import Decimal

import pytest

from camctl.acceptance.schema import BodySchemaError, validate_precise
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.devices.drivers.hasselblad.definitions import (
    DRIVER_ID,
    PARAMETER_TYPE,
    capability,
    driver_definition,
)
from camctl.devices.drivers.hasselblad.parameters import (
    CAPTURE_FORMATS,
    CaptureFormat,
    PHOTO_COMMANDS,
    photo_schema,
)
from camctl.devices.tasks import CaptureStorage


PHOTO_PARAMS = {
    "type": "hasselblad_photo",
    "capture_format": "raw",
    "exposure": {"mode": "M", "iso": 800, "aperture": Decimal("8"), "shutter_s": "1/125"},
    "white_balance": {"mode": "manual", "temperature_k": 5500},
    "focus": {"mode": "MF", "relative_move": 5},
}


def test_same_static_driver_binds_each_device_to_its_own_storage():
    from camctl.capture.models import build_capture_spec

    definition = driver_definition()
    config = load_config({"devices": {
        device_id: {
            "kind": "camera", "driver": DRIVER_ID,
            "hasselblad": {"executable": "/no/such/hb_cli", "serial": device_id,
                           "output_storage": storage},
        }
        for device_id, storage in (("hb-host", "host"), ("hb-device", "device"))
    }}, ConfigDefaults())
    catalog = build_catalog(config, DriverDefinitions({DRIVER_ID: definition}))
    host = catalog.parameter_definition("hb-host", "camera_take_photo", PARAMETER_TYPE)
    device = catalog.parameter_definition("hb-device", "camera_take_photo", PARAMETER_TYPE)
    for parameter, storage in ((device, "device"), (host, "host"), (device, "device")):
        assert build_capture_spec("camera_take_photo", parameter.task_factory(PHOTO_PARAMS)) == {
            "output_storage": storage, "start_call_timeout_s": 180,
        }
    assert definition.actions["camera_take_photo"][0].task_factory(PHOTO_PARAMS).output_storage.value == "host"
    assert host.schema == device.schema


def test_hasselblad_photo_definition_is_exported_with_its_fixed_task():
    from camctl.acceptance.schema import BodySchemaError

    definition = driver_definition()
    config = load_config(
        {
            "devices": {
                "hb0": {
                    "kind": "camera",
                    "driver": DRIVER_ID,
                    "hasselblad": {
                        "executable": "/no/such/hb_cli",
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

    assert definition.driver_id == "hasselblad_cfv100c"
    assert description["type"] == "hasselblad_photo"
    assert description["name"] == "Hasselblad 单张拍照"
    assert description["preview_supported"] is True
    assert description["schema"] == {**photo_schema(), "default": {}}
    assert description["schema"]["properties"]["capture_format"]["oneOf"] == [
        {"const": item.command_value, "title": item.title}
        for item in CAPTURE_FORMATS.values()
    ]
    assert all(word in description["description"] for word in ("曝光", "白平衡", "对焦", "开发验证临时选项"))
    assert (
        f"`{PHOTO_COMMANDS.DRIVE_MODE_FIELD.value}` 设置为 "
        f"`{PHOTO_COMMANDS.DRIVE_MODE_SINGLE.value}`"
    ) in description["description"]
    assert (
        f"`{PHOTO_COMMANDS.WHITE_BALANCE_FIELD.value}` 设置，色调固定为 "
        f"`{PHOTO_COMMANDS.WHITE_BALANCE_TINT.value}`"
    ) in description["description"]
    assert all(word in description["description"] for word in ("启动前", "启动后", "不支持取消"))
    assert parameter is not None
    assert parameter.defaults == {}
    assert "default" not in capability().schema
    assert parameter.schema["properties"]["type"]["const"] == PARAMETER_TYPE
    validate_precise(parameter.schema, PHOTO_PARAMS)
    with pytest.raises(BodySchemaError):
        validate_precise(parameter.schema, {"type": PARAMETER_TYPE})

    task = parameter.task_factory(PHOTO_PARAMS)
    assert task.action_type == "camera_take_photo"
    assert task.stop_supported is None
    assert task.output_storage is CaptureStorage.HOST
    assert str(task.start_call_timeout_s) == "180"
    with pytest.raises(BodySchemaError):
        parameter.task_factory({"type": PARAMETER_TYPE})


def test_capture_format_description_is_available_without_device_or_tool():
    raw = CAPTURE_FORMATS[CaptureFormat.RAW]
    jpeg = CAPTURE_FORMATS[CaptureFormat.JPEG]

    assert raw.command_value == "raw"
    assert (raw.original_extension, raw.original_media_type) == (".3fr", "image/x-hasselblad-3fr")
    assert (raw.preview_extension, raw.preview_media_type) == (".jpg", "image/jpeg")
    assert jpeg.command_value == "jpeg"
    assert (jpeg.original_extension, jpeg.original_media_type) == (".jpg", "image/jpeg")
    assert (jpeg.preview_extension, jpeg.preview_media_type) == (".jpg", "image/jpeg")
