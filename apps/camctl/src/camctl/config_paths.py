"""普通入口与环境外维护入口共用的配置来源路径。"""
from pathlib import Path


def default_config_path(home):
    return Path(home) / '.camctl' / 'config.toml'


def lock_file_paths(database_path):
    path = Path(database_path)
    return (path.with_name(path.name + '.session.lock'),
            path.with_name(path.name + '.admission.lock'))


def rotated_log_names(path):
    path = Path(path)
    prefix = path.name + '.'
    result = []
    for candidate in path.parent.iterdir():
        suffix = candidate.name[len(prefix):] if candidate.name.startswith(prefix) else ''
        if suffix.isascii() and suffix.isdecimal() and int(suffix) > 0 and suffix == str(int(suffix)):
            result.append((int(suffix), candidate))
    return sorted(result)


def log_registration_id(path):
    import hashlib
    import os
    return hashlib.sha256(os.fsencode(Path(path))).hexdigest()
