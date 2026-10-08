"""设备执行配置的正式字段范围；通过纯配置入口验证。"""

from decimal import Decimal

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config


def _load(section, field, value):
    return load_config({"devices": {"camera-1": {
        "kind": "camera", "driver": "adb", section: {field: value},
    }}}, ConfigDefaults()).devices["camera-1"][section][field]


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
