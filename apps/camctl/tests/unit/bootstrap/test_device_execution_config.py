"""设备执行配置的正式字段范围；通过纯配置入口验证。"""

from decimal import Decimal

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config


def _load(section, field, value):
    return load_config({"devices": {"camera-1": {
        "kind": "camera", "driver": "adb", section: {field: value},
    }}}, ConfigDefaults()).devices["camera-1"][section][field]


@pytest.mark.parametrize("value,expected", [(0, Decimal(0)), (2, Decimal(2)),
    (Decimal("0.125"), Decimal("0.125")), ("0.0125", Decimal("0.0125"))])
def test_explicit_prepare_lead_preserves_finite_nonnegative_seconds(value, expected):
    assert _load("connection", "prepare_lead_s", value) == expected


@pytest.mark.parametrize("value", [True, None, -1, "NaN", "Infinity", "later", 1.5])
def test_prepare_lead_rejects_invalid_explicit_override(value):
    with pytest.raises(ConfigError, match="prepare_lead_s"):
        _load("connection", "prepare_lead_s", value)


def test_omitted_prepare_lead_is_left_to_the_driver_default():
    device = load_config({"devices": {"camera-1": {"kind": "camera", "driver": "adb"}}},
                         ConfigDefaults()).devices["camera-1"]
    assert "connection" not in device


@pytest.mark.parametrize("section,field", [
    ("recording", "start_timeout_s"), ("recording", "stop_timeout_s"),
    ("result_check", "call_timeout_s"), ("copy", "read_idle_timeout_s"),
])
def test_positive_seconds_preserve_exact_value(section, field):
    assert _load(section, field, "0.0125") == Decimal("0.0125")


@pytest.mark.parametrize("section,field", [
    ("recording", "start_timeout_s"), ("recording", "stop_timeout_s"),
    ("result_check", "call_timeout_s"), ("copy", "read_idle_timeout_s"),
])
@pytest.mark.parametrize("bad", [0, -1, True, None, "NaN", "Infinity", "later", 1.25])
def test_positive_seconds_reject_invalid_values(section, field, bad):
    with pytest.raises(ConfigError, match=field):
        _load(section, field, bad)


@pytest.mark.parametrize("section,field,minimum", [
    ("recording", "max_start_attempts", 1), ("recording", "max_stop_attempts", 1),
    ("result_check", "max_attempts", 1), ("copy", "max_read_attempts", 1),
    ("copy", "max_recopies", 0), ("capture", "extra_wait_ms", 0),
])
def test_integer_field_accepts_its_lower_boundary(section, field, minimum):
    assert _load(section, field, minimum) == minimum


@pytest.mark.parametrize("section,field,minimum", [
    ("recording", "max_start_attempts", 1), ("recording", "max_stop_attempts", 1),
    ("result_check", "max_attempts", 1), ("copy", "max_read_attempts", 1),
    ("copy", "max_recopies", 0), ("capture", "extra_wait_ms", 0),
])
@pytest.mark.parametrize("bad", [True, None, "3", Decimal("3"), 1.25])
def test_integer_field_rejects_non_integer_values(section, field, minimum, bad):
    with pytest.raises(ConfigError, match=field):
        _load(section, field, bad)


@pytest.mark.parametrize("section,field,minimum", [
    ("recording", "max_start_attempts", 1), ("recording", "max_stop_attempts", 1),
    ("result_check", "max_attempts", 1), ("copy", "max_read_attempts", 1),
    ("copy", "max_recopies", 0), ("capture", "extra_wait_ms", 0),
])
def test_integer_field_rejects_value_below_lower_boundary(section, field, minimum):
    with pytest.raises(ConfigError, match=field):
        _load(section, field, minimum - 1)


@pytest.mark.parametrize("section", [
    "recording", "result_check", "copy", "capture", "query", "residual_stop", "cleanup",
])
def test_execution_section_rejects_unknown_field(section):
    with pytest.raises(ConfigError, match="unexpected"):
        _load(section, "unexpected", 1)


@pytest.mark.parametrize("section", [
    "recording", "result_check", "copy", "capture", "query", "residual_stop", "cleanup",
])
def test_execution_section_requires_table_even_when_explicitly_null(section):
    with pytest.raises(ConfigError, match=section):
        load_config({"devices": {"camera-1": {
            "kind": "camera", "driver": "adb", section: None,
        }}}, ConfigDefaults())


def test_obtain_defaults_are_materialized_for_each_declared_device():
    config = load_config({"devices": {
        "camera-1": {"kind": "camera", "driver": "adb"},
        "camera-2": {"kind": "camera", "driver": "adb", "obtain": {}},
    }}, ConfigDefaults())

    assert config.devices["camera-1"]["obtain"] == {
        "source_check_timeout_s": Decimal("10"),
        "source_check_retry_interval_s": Decimal("3"),
    }
    assert config.devices["camera-2"]["obtain"] == config.devices["camera-1"]["obtain"]


def test_obtain_device_defaults_come_from_config_defaults():
    config = load_config({"devices": {"camera-1": {
        "kind": "camera", "driver": "adb",
    }}}, ConfigDefaults(
        source_check_timeout_s="6.25",
        source_check_retry_interval_s="0",
    ))

    assert config.devices["camera-1"]["obtain"] == {
        "source_check_timeout_s": Decimal("6.25"),
        "source_check_retry_interval_s": Decimal("0"),
    }


@pytest.mark.parametrize("field,raw,expected", [
    ("source_check_timeout_s", "0.125", Decimal("0.125")),
    ("source_check_timeout_s", Decimal("2.5"), Decimal("2.5")),
    ("source_check_timeout_s", 10, Decimal("10")),
    ("source_check_retry_interval_s", "0", Decimal("0")),
    ("source_check_retry_interval_s", Decimal("2.5"), Decimal("2.5")),
    ("source_check_retry_interval_s", 3, Decimal("3")),
])
def test_obtain_device_seconds_preserve_exact_legal_values(field, raw, expected):
    assert _load("obtain", field, raw) == expected


@pytest.mark.parametrize("bad", [0, -1, True, None, "NaN", "Infinity", "not-seconds", 1.25])
def test_obtain_device_timeout_rejects_zero_and_invalid_seconds(bad):
    with pytest.raises(ConfigError, match="source_check_timeout_s"):
        _load("obtain", "source_check_timeout_s", bad)


@pytest.mark.parametrize("bad", [-1, True, None, "NaN", "Infinity", "not-seconds", 1.25])
def test_obtain_device_retry_interval_rejects_negative_and_invalid_seconds(bad):
    with pytest.raises(ConfigError, match="source_check_retry_interval_s"):
        _load("obtain", "source_check_retry_interval_s", bad)


@pytest.mark.parametrize("field", ["max_source_check_attempts", "unknown"])
def test_obtain_device_rejects_unknown_fields_including_global_attempt_limit(field):
    with pytest.raises(ConfigError, match="未知字段"):
        _load("obtain", field, 3)


def test_obtain_device_section_must_be_a_table():
    with pytest.raises(ConfigError, match="obtain"):
        load_config({"devices": {"camera-1": {
            "kind": "camera", "driver": "adb", "obtain": None,
        }}}, ConfigDefaults())
