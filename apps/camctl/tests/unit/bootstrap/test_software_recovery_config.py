"""用户只选择是否允许恢复，固件核验由驱动维护。"""
from decimal import Decimal

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config


def device(driver="dji-action6", **fields):
    return load_config({"devices": {"cam-1": {"kind": "camera", "driver": driver,
        "adb": {"serial": "software-test-camera"}, **fields}}}, ConfigDefaults()).devices["cam-1"]


def test_unverified_deployment_is_disabled_and_exact_stop_timeout_is_legal():
    config = device(recording={"stop_timeout_s": Decimal("0.001"), "max_stop_attempts": 1})
    assert "software_recovery" not in config
    assert device(software_recovery={})["software_recovery"]["enabled"] is False
    assert config["recording"]["stop_timeout_s"] == Decimal("0.001")
    assert config["recording"]["max_stop_attempts"] == 1


@pytest.mark.parametrize("driver", ["dji-action6", "dji-osmo360-ii"])
@pytest.mark.parametrize("enabled", [False, True])
def test_enabled_is_the_only_user_recovery_choice(driver, enabled):
    assert device(driver, software_recovery={"enabled": enabled})["software_recovery"]["enabled"] is enabled


@pytest.mark.parametrize("enabled", [None, 0, 1, "true", "false", []])
def test_recovery_enabled_requires_an_actual_boolean(enabled):
    with pytest.raises(ConfigError, match="enabled"):
        device(software_recovery={"enabled": enabled})


def test_legacy_fingerprint_gives_a_migration_instruction():
    with pytest.raises(ConfigError, match="enabled"):
        device(software_recovery={"verified_fingerprint": "action6-verinfo-json-sha256/v1:" + "a" * 64})
