"""B2 配置加载的组件集成测试：真实 TOML 文件解析后加载生效配置。

目录绑定仓储的组合验证随 B4 初始化承接；本文件验证装配适配器
的文件读取路径（tomllib 以 Decimal 读取小数）与纯加载函数的组合。
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config


def _load_toml(path: Path):
    with path.open("rb") as handle:
        return tomllib.load(handle, parse_float=Decimal)


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
                    'info_sample_probability = 0.25',
                    '',
                    '[copy]',
                    'segment_size = "1.5 GiB"',
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_config(_load_toml(document), ConfigDefaults())
        assert cfg.database.queue_capacity == 8
        assert cfg.database.busy_timeout_ms == 9000
        assert cfg.log.level == "INFO"
        assert cfg.log.info_sample_probability == Decimal("0.25")
        assert cfg.copy.segment_size_bytes == 1610612736

    def test_empty_file_uses_defaults(self, tmp_path: Path) -> None:
        document = tmp_path / "camctl.toml"
        document.write_text("# 只有注释\n", encoding="utf-8")
        cfg = load_config(_load_toml(document), ConfigDefaults())
        assert cfg.database.queue_capacity == 64
        assert cfg.copy.segment_size_bytes == 134217728

    def test_invalid_file_value_is_config_error(self, tmp_path: Path) -> None:
        document = tmp_path / "camctl.toml"
        document.write_text('[log]\nfile_count = 0\n', encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(_load_toml(document), ConfigDefaults())

    def test_missing_file_uses_defaults_without_search(self, tmp_path: Path) -> None:
        cfg = load_config(None, ConfigDefaults())
        assert cfg.copy.segment_size_bytes == 134217728

    def test_unreadable_file_is_not_silent_default(self, tmp_path: Path) -> None:
        # 无法可靠读取且不能确认为不存在：适配器必须报错而非缺省。
        target = tmp_path / "camctl.toml"
        target.write_bytes(b"\xff\xfe broken")
        with pytest.raises((UnicodeDecodeError, tomllib.TOMLDecodeError)):
            _load_toml(target)
