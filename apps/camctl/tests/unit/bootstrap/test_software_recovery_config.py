"""部署使能必须使用所属驱动的已验证资料，有限 STOP 可采用精确毫秒值。"""
from decimal import Decimal

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config


def device(driver="dji-action6", **fields):
    return load_config({"devices": {"cam-1": {"kind": "camera", "driver": driver,
        "adb": {"serial": "software-test-camera"}, **fields}}}, ConfigDefaults()).devices["cam-1"]


def test_unverified_deployment_is_disabled_and_exact_stop_timeout_is_legal():
    config = device(recording={"stop_timeout_s": Decimal("0.001"), "max_stop_attempts": 1})
    assert "software_recovery" not in config
    assert device(software_recovery={})["software_recovery"]["verified_fingerprint"] == ""
    assert config["recording"]["stop_timeout_s"] == Decimal("0.001")
    assert config["recording"]["max_stop_attempts"] == 1


@pytest.mark.parametrize("driver,fingerprint", [
    ("dji-action6", "action6-verinfo-json-sha256/v1:" + "a" * 64),
    ("dji-osmo360-ii", "android-build-fingerprint/v1:software-test-firmware"),
])
def test_verified_source_token_survives_config_loading(driver, fingerprint):
    assert device(driver, software_recovery={"verified_fingerprint": fingerprint})["software_recovery"]["verified_fingerprint"] == fingerprint


@pytest.mark.parametrize("driver,fingerprint", [
    ("dji-action6", "software-test-firmware"),
    ("dji-action6", "android-build-fingerprint/v1:software-test-firmware"),
    ("dji-osmo360-ii", "action6-verinfo-json-sha256/v1:" + "a" * 64),
])
def test_bare_or_other_driver_firmware_is_rejected(driver, fingerprint):
    with pytest.raises(ConfigError, match="verified_fingerprint"):
        device(driver, software_recovery={"verified_fingerprint": fingerprint})
