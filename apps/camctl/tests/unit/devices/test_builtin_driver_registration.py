"""内置驱动注册从一个部署入口协调静态定义与运行端口。"""

from io import StringIO

import pytest

from camctl.devices.drivers import builtin


def test_builtin_registration_invokes_adb_and_hasselblad(monkeypatch):
    from camctl.devices.drivers.adb_cameras import registration as adb_registration
    from camctl.devices.drivers.hasselblad import registration as hasselblad_registration

    calls = []
    monkeypatch.setattr(
        adb_registration,
        "register_builtin_camera_drivers",
        lambda config: calls.append(("adb", config)),
    )
    monkeypatch.setattr(
        hasselblad_registration,
        "register_builtin_hasselblad_driver",
        lambda config: calls.append(("hasselblad", config)),
    )
    config = object()

    builtin.register_builtin_drivers(config)

    assert calls == [("adb", config), ("hasselblad", config)]


@pytest.fixture(autouse=True)
def isolate_driver_registries():
    from camctl.devices.definitions_runtime import reset_driver_definitions
    from camctl.devices.drivers.runtime import reset_drivers

    reset_driver_definitions()
    reset_drivers()
    yield
    reset_driver_definitions()
    reset_drivers()


def _config(staging, *, serial="64010904"):
    from camctl.bootstrap.config import ConfigDefaults, load_config

    return load_config(
        {
            "paths": {"staging": str(staging)},
            "devices": {
                "hb0": {
                    "kind": "camera",
                    "driver": "hasselblad_cfv100c",
                    "hasselblad": {
                        "executable": "/home/cld/hb_cli/hb_cli",
                        "serial": serial,
                    },
                }
            },
        },
        ConfigDefaults(),
    )


def test_hasselblad_definition_and_entry_come_from_same_registration():
    from camctl.devices.definitions_runtime import current_driver_definitions
    from camctl.devices.drivers.hasselblad.configuration import CameraConfig, read_camera_config
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
    from camctl.devices.drivers.hasselblad.driver import EVIDENCE, HasselbladDriver
    from camctl.devices.drivers.hasselblad.registration import register_builtin_hasselblad_driver
    from camctl.devices.drivers.registry import DriverStatus
    from camctl.devices.drivers.runtime import current_registry

    config = _config("/srv/staging")

    register_builtin_hasselblad_driver(config)

    entry = current_registry().entry(DRIVER_ID)
    assert current_driver_definitions().drivers[DRIVER_ID] == driver_definition()
    assert isinstance(entry.driver, HasselbladDriver)
    assert entry.driver.driver_id == DRIVER_ID
    assert entry.evidence is EVIDENCE
    assert entry.status is DriverStatus.DEVICE_VERIFICATION_PENDING
    assert entry.driver.devices is config.devices
    assert entry.driver.roots.staging.as_posix() == "/srv/staging"
    assert read_camera_config(entry.driver.devices["hb0"]) == CameraConfig(
        "/home/cld/hb_cli/hb_cli", "64010904", 10
    )


def test_repeated_same_driver_registration_refreshes_config_without_replacing_entry():
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    from camctl.devices.drivers.hasselblad.registration import register_builtin_hasselblad_driver
    from camctl.devices.drivers.runtime import current_registry

    first = _config("/srv/first")
    second = _config("/srv/second", serial="64010905")
    register_builtin_hasselblad_driver(first)
    previous = current_registry().entry(DRIVER_ID)

    register_builtin_hasselblad_driver(second)

    current = current_registry().entry(DRIVER_ID)
    assert current is previous
    assert current.driver.devices is second.devices
    assert current.driver.roots.staging.as_posix() == "/srv/second"


def test_conflicting_hasselblad_entry_is_rejected_before_definition_registration():
    from camctl.acceptance.schema import RuleError
    from camctl.devices.definitions_runtime import current_driver_definitions
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    from camctl.devices.drivers.hasselblad.driver import EVIDENCE
    from camctl.devices.drivers.hasselblad.registration import register_builtin_hasselblad_driver
    from camctl.devices.ports import DriverDeclaration
    from camctl.devices.drivers.registry import DriverEntry, DriverStatus
    from camctl.devices.drivers.runtime import register_drivers

    declaration = DriverDeclaration(False, False, False, False, False, False, False)
    register_drivers(DriverEntry(
        DRIVER_ID, object(), declaration, EVIDENCE, DriverStatus.DEVICE_VERIFICATION_PENDING
    ))

    with pytest.raises(RuleError, match="身份与既有登记冲突"):
        register_builtin_hasselblad_driver(_config("/srv/staging"))

    assert current_driver_definitions().drivers == {}


def test_conflicting_hasselblad_definition_is_rejected_without_runtime_registration():
    from camctl.acceptance.schema import RuleError
    from camctl.devices.catalog import DriverDefinition
    from camctl.devices.definitions_runtime import (
        current_driver_definitions,
        register_driver_definitions,
    )
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    from camctl.devices.drivers.hasselblad.registration import register_builtin_hasselblad_driver
    from camctl.devices.drivers.runtime import current_registry

    conflicting = DriverDefinition(DRIVER_ID, {})
    register_driver_definitions(conflicting)

    with pytest.raises(RuleError, match="身份与既有登记冲突"):
        register_builtin_hasselblad_driver(_config("/srv/staging"))

    assert current_driver_definitions().drivers[DRIVER_ID] is conflicting
    assert current_registry().entry(DRIVER_ID) is None


def test_default_catalog_uses_unified_builtin_registration(monkeypatch):
    from camctl.bootstrap import lifecycle
    from camctl.devices import catalog

    config = object()
    monkeypatch.setattr(
        builtin,
        "register_builtin_drivers",
        lambda actual: calls.append(actual),
    )
    calls = []
    monkeypatch.setattr(catalog, "build_catalog", lambda actual, definitions: (actual, definitions))
    monkeypatch.setattr(catalog, "default_driver_definitions", lambda: "definitions")

    result = lifecycle._default_catalog(config)

    assert calls == [config]
    assert result == (config, "definitions")


def test_cli_describe_uses_unified_builtin_registration(monkeypatch):
    from camctl import cli
    from camctl.bootstrap import application
    from camctl.devices import catalog

    config = object()
    calls = []
    monkeypatch.setattr(builtin, "register_builtin_drivers", calls.append)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: cls("/unused")))
    monkeypatch.setattr(application.ConfigAdapter, "load", lambda *args: config)
    monkeypatch.setattr(catalog, "build_catalog", lambda *args: "catalog")
    monkeypatch.setattr(catalog, "default_driver_definitions", lambda: "definitions")
    monkeypatch.setattr(application, "describe", lambda *args: {"devices": []})
    monkeypatch.setattr(cli, "encode_describe_document", lambda document: b"{}")

    out, err = StringIO(), StringIO()
    result = cli._run_describe(cli.Command(cli.CommandKind.DESCRIBE, None, None), out, err)

    assert result == 0
    assert calls == [config]
    assert out.getvalue() == "{}"
    assert err.getvalue() == ""
