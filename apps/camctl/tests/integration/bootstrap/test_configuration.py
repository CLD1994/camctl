"""B2 配置加载的组件集成测试：真实 TOML 文件解析后加载生效配置。

目录绑定仓储的组合验证随 B4 初始化承接；本文件验证装配适配器
的真实文件读取路径（tomllib 以 Decimal 读取小数）与配置加载的组合。
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.bootstrap.application import ConfigAdapter
from camctl.bootstrap.config import ConfigError



class TestRealTomlLoading:
    def test_real_file_overrides_selected_fields(self, tmp_path: Path) -> None:
        document = tmp_path / "camctl.toml"
        document.write_text(
            "\n".join(
                [
                    '[database]',
                    'queue_capacity = 8',
                    '',
                    '[log]',
                    'level = "INFO"',
                    'info_sample_probability = 0.1234567890123456789',
                    '',
                    '[copy]',
                    'segment_size = "1.5 GiB"',
                ]
            ),
            encoding="utf-8",
        )
        cfg = ConfigAdapter(tmp_path).load(document)
        assert cfg.database.queue_capacity == 8
        assert cfg.database.busy_timeout_ms == 9000
        assert cfg.log.level == "INFO"
        assert isinstance(cfg.log.info_sample_probability, Decimal)
        assert cfg.log.info_sample_probability == Decimal("0.1234567890123456789")
        assert cfg.copy.segment_size_bytes == 1610612736

    def test_empty_file_uses_defaults(self, tmp_path: Path) -> None:
        document = tmp_path / "camctl.toml"
        document.write_text("# 只有注释\n", encoding="utf-8")
        cfg = ConfigAdapter(tmp_path).load(document)
        assert cfg.database.queue_capacity == 64
        assert cfg.copy.segment_size_bytes == 134217728

    def test_invalid_file_value_is_config_error(self, tmp_path: Path) -> None:
        document = tmp_path / "camctl.toml"
        document.write_text('[log]\nfile_count = 0\n', encoding="utf-8")
        with pytest.raises(ConfigError):
            ConfigAdapter(tmp_path).load(document)

    def test_unreadable_file_is_not_silent_default(self, tmp_path: Path) -> None:
        # 无法可靠读取且不能确认为不存在：适配器必须报错而非缺省。
        target = tmp_path / "camctl.toml"
        target.write_bytes(b"\xff\xfe broken")
        with pytest.raises(UnicodeDecodeError):
            ConfigAdapter(tmp_path).load(target)

    def test_directory_is_read_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as raised:
            ConfigAdapter(tmp_path).load(tmp_path)
        assert isinstance(raised.value.__cause__, IsADirectoryError)

    def test_invalid_toml_is_config_error(self, tmp_path: Path) -> None:
        document = tmp_path / "camctl.toml"
        document.write_text("[log\n", encoding="utf-8")
        with pytest.raises(ConfigError) as raised:
            ConfigAdapter(tmp_path).load(document)
        assert isinstance(raised.value.__cause__, tomllib.TOMLDecodeError)
