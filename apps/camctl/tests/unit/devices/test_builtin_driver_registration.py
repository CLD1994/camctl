"""内置驱动注册从一个部署入口协调静态定义与运行端口。"""

from io import StringIO

import pytest

from camctl.devices.drivers import builtin
from device_driver_fixtures import (
    synthetic_driver_declaration,
    synthetic_source_storage_resolver,
)


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
    assert current_driver_definitions().drivers[DRIVER_ID].declaration == entry.declaration
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


def test_video_result_modes_bind_real_action6_and_osmo_factories_per_device():
    from camctl.bootstrap.config import ConfigDefaults, load_config
    from camctl.devices.catalog import build_catalog
    from camctl.devices.definitions_runtime import current_driver_definitions
    from camctl.devices.drivers.adb_cameras.registration import register_builtin_camera_drivers

    config = load_config({"devices": {
        "action6-single": {"kind": "camera", "driver": "dji-action6"},
        "action6-segmented": {"kind": "camera", "driver": "dji-action6",
                               "video": {"result_mode": "segmented"}},
        "osmo-single": {"kind": "camera", "driver": "dji-osmo360-ii"},
        "osmo-segmented": {"kind": "camera", "driver": "dji-osmo360-ii",
                            "video": {"result_mode": "segmented"}},
    }}, ConfigDefaults())
    register_builtin_camera_drivers(config)
    register_builtin_camera_drivers(config)
    catalog = build_catalog(config, current_driver_definitions())

    def build(device_id, action_type, parameter_type, params):
        parameter = catalog.parameter_definition(device_id, action_type, parameter_type)
        assert parameter is not None and callable(parameter.task_factory)
        return parameter.task_factory(params)

    def freeze(action_type, task):
        from camctl.capture.models import build_capture_spec
        return build_capture_spec(action_type, task)

    ordinary_params = {"type": "action6_record", "duration_s": 12}
    single = build("action6-single", "camera_record", "action6_record", ordinary_params)
    segmented = build("action6-segmented", "camera_record", "action6_record", ordinary_params)
    assert single.video_result_mode.value == "single_file"
    assert single.product_rules[0]["exact_count"] == 1
    assert freeze("camera_record", single)["video_result_mode"] == "single_file"
    assert segmented.video_result_mode.value == "segmented"
    assert segmented.product_rules[0]["exact_count"] is None
    assert segmented.product_rules[0]["min_size_bytes"] == 1
    assert freeze("camera_record", segmented)["video_result_mode"] == "segmented"
    assert segmented.result_policy.value == "action6_video_segments_v1"
    assert freeze("camera_record", segmented)["result_policy"] == \
        "action6_video_segments_v1"

    timelapse_params = {"type": "action6_timelapse", "duration_s": 300,
                        "outputs": "video_raw"}
    native_single = build("action6-single", "camera_timelapse", "action6_timelapse",
                          timelapse_params)
    native_segmented = build("action6-segmented", "camera_timelapse", "action6_timelapse",
                             timelapse_params)
    assert native_single.video_result_mode.value == "single_file"
    assert native_single.product_rules[0]["exact_count"] is None
    assert [rule["kind"] for rule in native_segmented.product_rules] == ["video", "photo"]
    assert native_segmented.video_result_mode.value == "segmented"
    assert native_segmented.result_policy.value == "action6_video_segments_v1"
    assert freeze("camera_timelapse", native_single)["video_result_mode"] == "single_file"
    assert freeze("camera_timelapse", native_segmented)["video_result_mode"] == "segmented"
    native_segmented_spec = freeze("camera_timelapse", native_segmented)
    assert native_segmented_spec["result_policy"] == "action6_video_segments_v1"
    assert [rule["kind"] for rule in native_segmented_spec["product_rules"]] == ["video", "photo"]

    osmo_record_single = build("osmo-single", "camera_record", "osmo360ii_record",
                               {"type": "osmo360ii_record", "duration_s": 12})
    osmo_record_segmented = build("osmo-segmented", "camera_record", "osmo360ii_record",
                                  {"type": "osmo360ii_record", "duration_s": 12})
    osmo_native_single = build("osmo-single", "camera_timelapse", "osmo360ii_timelapse",
                               {"type": "osmo360ii_timelapse", "duration_s": 300})
    osmo_native_segmented = build("osmo-segmented", "camera_timelapse", "osmo360ii_timelapse",
                                  {"type": "osmo360ii_timelapse", "duration_s": 300})
    assert osmo_record_single.result_policy is None
    assert osmo_native_single.result_policy is None
    assert osmo_record_segmented.result_policy.value == "osmo360ii_record_segments_v1"
    assert osmo_native_segmented.result_policy.value == "osmo_empty_tail_classification_v1"
    assert "result_policy" not in freeze("camera_record", osmo_record_single)
    assert "result_policy" not in freeze("camera_timelapse", osmo_native_single)
    assert freeze("camera_timelapse", osmo_native_single)["video_result_mode"] == "single_file"
    assert freeze("camera_record", osmo_record_segmented)["result_policy"] == \
        "osmo360ii_record_segments_v1"
    assert freeze("camera_timelapse", osmo_native_segmented)["result_policy"] == \
        "osmo_empty_tail_classification_v1"

    descriptions = catalog.describe_document()["devices"]
    by_device = {device["device_id"]: device for device in descriptions}
    action6_record_description = by_device["action6-single"]["actions"][0]["parameter_types"][0]["description"]
    action6_segmented_description = by_device["action6-segmented"]["actions"][0]["parameter_types"][0]["description"]
    assert "单文件视频结果模式" in action6_record_description
    assert "分段视频结果模式" in action6_segmented_description


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

    conflicting = DriverDefinition(
        DRIVER_ID,
        {},
        synthetic_driver_declaration(),
        source_storage_resolver=synthetic_source_storage_resolver,
)
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
