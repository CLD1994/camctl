"""普通入口与环境外维护入口共用的配置来源路径。"""
from pathlib import Path


def default_config_path(home):
    return Path(home) / '.camctl' / 'config.toml'
