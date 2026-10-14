"""配置装载严格校验 Hasselblad 设备子表及相机身份唯一性。"""

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config


def _device(serial, *, product_id=10):
    return {
        "kind": "camera",
        "driver": "hasselblad_cfv100c",
        "hasselblad": {
            "executable": "/home/cld/hb_cli/hb_cli",
            "serial": serial,
            "product_id": product_id,
        },
    }


def test_config_snapshot_retains_a_purely_parsed_hasselblad_camera_config():
    from camctl.devices.drivers.hasselblad.configuration import CameraConfig, read_camera_config

    config = load_config({"devices": {"hb0": _device("64010904", product_id=12)}}, ConfigDefaults())
    assert read_camera_config(config.devices["hb0"]) == CameraConfig(
        "/home/cld/hb_cli/hb_cli", "64010904", 12
    )


def test_duplicate_hasselblad_serial_cannot_bind_two_logical_devices():
    with pytest.raises(ConfigError, match="serial"):
        load_config(
            {"devices": {"hb0": _device("64010904"), "hb1": _device("64010904")}},
            ConfigDefaults(),
        )


def test_distinct_hasselblad_serials_remain_separate_logical_devices():
    config = load_config(
        {"devices": {"hb0": _device("64010904"), "hb1": _device("64010905")}},
        ConfigDefaults(),
    )
    assert tuple(config.devices) == ("hb0", "hb1")


@pytest.mark.parametrize("storage", ["host", "device"])
def test_output_storage_is_only_accepted_in_the_hasselblad_subtable(storage):
    device = _device("SERIAL-1")
    device["output_storage"] = storage
    with pytest.raises(ConfigError):
        load_config({"devices": {"hb0": device}}, ConfigDefaults())


@pytest.mark.parametrize("storage", [None, True, 1, "", "camera"])
def test_load_config_rejects_invalid_hasselblad_output_storage(storage):
    device = _device("SERIAL-1")
    device["hasselblad"]["output_storage"] = storage
    with pytest.raises(ConfigError):
        load_config({"devices": {"hb0": device}}, ConfigDefaults())
