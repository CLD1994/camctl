"""B2 本地配置加载与冻结的单元测试。

期望独立来自配置专题的默认值、容量表示与组合规则；省略沿用默认、
显式非法值拒绝，不混用。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.bootstrap.config import (
    ConfigDefaults,
    ConfigError,
    load_config,
    parse_capacity,
)


class TestCapacityParsing:
    def test_exact_decimal_conversion(self) -> None:
        assert parse_capacity("1.5 KiB") == 1536
        assert parse_capacity("128 MiB") == 134217728
        assert parse_capacity("1.5 GiB") == 1610612736
        assert parse_capacity("1.5KB") == 1500
        assert parse_capacity("  2  MB  ") == 2_000_000
        assert parse_capacity("512B") == 512

    def test_same_capacity_written_differently_is_equal(self) -> None:
        assert parse_capacity("1 KiB") == parse_capacity("1024B") == 1024

    @pytest.mark.parametrize("bad", ["128", "128 M", "128 mib", "-1 MiB", "1.5 K", "1..5 MiB", ".5 MiB", "5. MiB", "1e3 B", "128XB"])
    def test_invalid_capacity_syntax_rejected(self, bad: str) -> None:
        with pytest.raises(ConfigError):
            parse_capacity(bad)

    def test_non_string_capacity_rejected(self) -> None:
        for bad in (128, 1.5, True, None):
            with pytest.raises(ConfigError):
                parse_capacity(bad)

    def test_fractional_byte_rejected(self) -> None:
        with pytest.raises(ConfigError):
            parse_capacity("0.1 B")


class TestDefaultsAndOverride:
    def test_no_document_uses_full_defaults(self) -> None:
        cfg = load_config(None, ConfigDefaults())
        assert cfg.copy.segment_size_bytes == 134217728
        assert cfg.log.max_size_bytes == 10 * 1024 * 1024
        assert cfg.log.level == "WARNING"
        assert cfg.log.info_sample_probability == Decimal("0.1")
        assert cfg.database.queue_capacity == 64
        assert cfg.database.busy_timeout_ms == 9000
        assert cfg.history.event_batch_size == 256
        assert str(cfg.clock.min_plausible_date) == "2025-01-01"
        assert cfg.clock.recovery_wait_cap_s == Decimal(60)
        assert cfg.devices == {}

    def test_partial_override_keeps_other_defaults(self) -> None:
        cfg = load_config({"log": {"level": "INFO"}}, ConfigDefaults())
        assert cfg.log.level == "INFO"
        assert cfg.log.file_count == 3
        assert cfg.copy.segment_size_bytes == 134217728

    def test_custom_defaults_are_honored(self) -> None:
        cfg = load_config(None, ConfigDefaults(log_file="/var/log/x.log", staging="/data/staging"))
        assert cfg.paths.log_file == "/var/log/x.log"
        assert cfg.paths.staging == "/data/staging"


class TestCombinationAndTypes:
    def test_partial_override_checks_combination(self) -> None:
        # 只覆盖一个日志水位，造成 L≥H：整组拒绝。
        with pytest.raises(ConfigError, match="组合"):
            load_config({"log": {"queue_low_watermark": 800}}, ConfigDefaults())

    def test_watermark_equal_capacity_rejected(self) -> None:
        with pytest.raises(ConfigError, match="组合"):
            load_config({"log": {"queue_high_watermark": 1000}}, ConfigDefaults())

    def test_bool_count_rejected(self) -> None:
        with pytest.raises(ConfigError, match="log.file_count"):
            load_config({"log": {"file_count": True}}, ConfigDefaults())

    def test_zero_capacity_rejected(self) -> None:
        with pytest.raises(ConfigError, match="queue_capacity"):
            load_config({"database": {"queue_capacity": 0}}, ConfigDefaults())

    def test_non_finite_seconds_rejected(self) -> None:
        with pytest.raises(ConfigError, match="有限"):
            load_config({"clock": {"lower_bound_tolerance_s": Decimal("NaN")}}, ConfigDefaults())
        with pytest.raises(ConfigError, match="有限"):
            load_config({"clock": {"recheck_delay_s": Decimal("Infinity")}}, ConfigDefaults())

    def test_zero_tolerance_rejected_but_zero_recovery_cap_allowed(self) -> None:
        with pytest.raises(ConfigError):
            load_config({"clock": {"lower_bound_tolerance_s": 0}}, ConfigDefaults())
        cfg = load_config({"clock": {"recovery_wait_cap_s": 0}}, ConfigDefaults())
        assert cfg.clock.recovery_wait_cap_s == 0

    def test_decimal_seconds_accepted_exactly(self) -> None:
        cfg = load_config({"clock": {"recheck_delay_s": Decimal("2.5")}}, ConfigDefaults())
        assert cfg.clock.recheck_delay_s == Decimal("2.5")

    def test_probability_bounds(self) -> None:
        cfg = load_config({"log": {"info_sample_probability": 1}}, ConfigDefaults())
        assert cfg.log.info_sample_probability == 1
        with pytest.raises(ConfigError):
            load_config({"log": {"info_sample_probability": Decimal("1.5")}}, ConfigDefaults())
        with pytest.raises(ConfigError):
            load_config({"log": {"info_sample_probability": True}}, ConfigDefaults())

    def test_unknown_field_rejected_at_every_level(self) -> None:
        with pytest.raises(ConfigError, match="未知字段"):
            load_config({"database": {"queue_capacityx": 5}}, ConfigDefaults())
        with pytest.raises(ConfigError, match="未知字段"):
            load_config({"unknown_section": {}}, ConfigDefaults())

    def test_invalid_date_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_config({"clock": {"min_plausible_date": "2025-02-30"}}, ConfigDefaults())
        with pytest.raises(ConfigError):
            load_config({"clock": {"min_plausible_date": "2025-1-1"}}, ConfigDefaults())

    def test_non_table_section_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_config({"database": 5}, ConfigDefaults())


class TestSnapshotImmutability:
    def test_snapshot_and_devices_are_read_only(self) -> None:
        cfg = load_config(
            {"devices": {"cam-1": {"kind": "camera", "driver": "adb", "recording": {"start_timeout_s": 12}}}},
            ConfigDefaults(),
        )
        with pytest.raises(Exception):
            cfg.log.level = "DEBUG"  # type: ignore[misc]
        with pytest.raises(Exception):
            cfg.devices["cam-2"] = {}  # type: ignore[index]
        with pytest.raises(Exception):
            cfg.devices["cam-1"]["driver"] = "other"  # type: ignore[index]

    def test_device_requires_kind_and_driver(self) -> None:
        with pytest.raises(ConfigError, match="kind"):
            load_config({"devices": {"cam-1": {"driver": "adb"}}}, ConfigDefaults())
        with pytest.raises(ConfigError, match="driver"):
            load_config({"devices": {"cam-1": {"kind": "camera"}}}, ConfigDefaults())
