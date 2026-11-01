"""普通入口与环境外维护入口共用的配置来源路径。"""
from pathlib import Path


def default_config_path(home):
    return Path(home) / '.camctl' / 'config.toml'


def lock_file_paths(database_path):
    path = Path(database_path)
    return (path.with_name(path.name + '.session.lock'),
            path.with_name(path.name + '.admission.lock'))
