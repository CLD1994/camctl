"""已登记维护部署的实际日志位置。"""
import json
import os
from pathlib import Path
import sys
import tempfile

from camctl.update_engine.layout import environment_locator, read_reference
from camctl.config_paths import log_registration_id


def register(log_path, configuration_source, *, environment=None):
    environment = Path(sys.prefix if environment is None else environment).resolve()
    layout_path = read_reference(environment_locator(environment))
    if layout_path is None:
        return False
    if not Path(log_path).is_absolute():
        raise ValueError('日志登记路径必须为绝对路径')
    if configuration_source is not None and not Path(configuration_source).is_absolute():
        raise ValueError('日志登记配置来源必须为绝对路径')
    layout = json.loads(layout_path.read_text(encoding='utf-8'))
    if not isinstance(layout, dict) or layout.get('environment') != str(environment):
        raise ValueError('日志登记的维护布局与当前环境不一致')
    path = Path(log_path).expanduser().resolve()
    source = None if configuration_source is None else str(Path(configuration_source).resolve())
    if not path.is_absolute() or (source is not None and not Path(source).is_absolute()):
        raise ValueError('日志登记路径必须为绝对路径')
    registry = layout_path.parent / 'logs'
    try:
        registry.mkdir(mode=0o700)
        parent_fd = os.open(registry.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except FileExistsError:
        if not registry.is_dir() or registry.is_symlink():
            raise ValueError('日志登记目录无效')
    directory_fd = os.open(registry, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
        name = log_registration_id(path) + '.json'
        fd, temp = tempfile.mkstemp(prefix='.log-location-', dir=registry)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'path': str(path), 'config_source': source}, stream, ensure_ascii=True, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, registry / name)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory_fd)
    return True
