"""Hasselblad 静态目录、配置源选择与运行登记使用同一纯规则。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from camctl.acceptance.schema import RuleError
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.devices.definitions_runtime import current_driver_definitions
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.registration import (
    register_builtin_hasselblad_driver,
)
from camctl.devices.drivers import runtime as drivers_runtime
from camctl.devices import definitions_runtime
from camctl.devices.tasks import CaptureStorage


@pytest.fixture
def isolated_hasselblad_registration(monkeypatch):
    monkeypatch.setattr(definitions_runtime, "_definitions", {})
    monkeypatch.setattr(drivers_runtime, "_entries", {})


def _camera(serial: str, output_storage: str | None = None) -> dict:
    hasselblad = {
        "executable": "/opt/hasselblad/hb_cli",
        "serial": serial,
    }
    if output_storage is not None:
        hasselblad["output_storage"] = output_storage
    return {
        "kind": "camera",
        "driver": DRIVER_ID,
        "hasselblad": hasselblad,
    }


def _config(tmp_path, devices: dict):
    return load_config(
        {
            "paths": {"staging": str(tmp_path / "staging")},
            "devices": devices,
        },
        ConfigDefaults(),
    )


def test_hasselblad_catalog_and_task_binding_resolve_each_device_source(
    tmp_path, isolated_hasselblad_registration
):
    config = _config(tmp_path, {
        "hb-default": _camera("serial-default"),
        "hb-host": _camera("serial-host", "host"),
        "hb-device": _camera("serial-device", "device"),
    })

    register_builtin_hasselblad_driver(config)

    definitions = current_driver_definitions()
    definition = definitions.drivers[DRIVER_ID]
    catalog = build_catalog(config, definitions)
    expected = {
        "hb-default": (CaptureStorage.HOST, True),
        "hb-host": (CaptureStorage.HOST, True),
        "hb-device": (CaptureStorage.DEVICE, False),
    }
    for device_id, (storage, delete_supported) in expected.items():
        assert definition.source_storage_resolver(config.devices[device_id]) is storage
        assert catalog.source_delete_supported(device_id) is delete_supported
        parameter = catalog.parameter_definition(
            device_id, "camera_take_photo", "hasselblad_photo"
        )
        assert parameter is not None and parameter.task_factory is not None
        task = parameter.task_factory({
            "type": "hasselblad_photo",
            "capture_format": "raw",
            "exposure": {"mode": "P", "iso": 100},
            "white_balance": {"mode": "auto"},
            "focus": {"mode": "MF"},
        })
        assert task.output_storage is storage

    described = {
        device["device_id"]: device
        for device in catalog.describe_document()["devices"]
    }
    assert described["hb-host"]["delete_supported"] is True
    assert described["hb-device"]["delete_supported"] is False

    entry = drivers_runtime.current_registry().entry(DRIVER_ID)
    assert entry is not None
    assert entry.declaration.delete_supported is False

    resolver = definition.source_storage_resolver
    previous_entry = entry
    register_builtin_hasselblad_driver(config)
    refreshed_definition = current_driver_definitions().drivers[DRIVER_ID]
    assert refreshed_definition.source_storage_resolver is resolver
    assert drivers_runtime.current_registry().entry(DRIVER_ID) is previous_entry


def test_invalid_hasselblad_source_rejects_registration_and_catalog_atomically(
    tmp_path, isolated_hasselblad_registration
):
    from camctl.devices.drivers.hasselblad.definitions import driver_definition

    valid = _config(tmp_path, {"hb0": _camera("serial-valid", "host")})
    register_builtin_hasselblad_driver(valid)
    definitions_before = current_driver_definitions()
    entry_before = drivers_runtime.current_registry().entry(DRIVER_ID)

    invalid = replace(
        valid,
        devices={"hb0": _camera("serial-invalid", "archive")},
    )
    with pytest.raises(ValueError, match="output_storage"):
        register_builtin_hasselblad_driver(invalid)
    assert current_driver_definitions().drivers == definitions_before.drivers
    assert drivers_runtime.current_registry().entry(DRIVER_ID) is entry_before

    definition_without_actions = replace(
        definitions_before.drivers[DRIVER_ID],
        actions={},
    )
    with pytest.raises(RuleError, match="正式源"):
        build_catalog(
            invalid,
            DriverDefinitions({DRIVER_ID: definition_without_actions}),
        )

    direct_catalog = build_catalog(valid, definitions_before)
    assert direct_catalog.source_delete_supported("hb0") is True
    assert driver_definition().declaration.delete_supported is False
