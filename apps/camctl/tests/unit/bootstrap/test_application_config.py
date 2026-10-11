"""配置适配器向各命令提供同一份已展开路径。

HOME 假值与示例字面量按平台生成：目录绑定校验要求绝对路径，
Windows 上 POSIX 风格假值不成立，Linux 上盘符路径同样不成立。
三个目录字段只校验可接受，值保持展开后的操作系统路径形式。
"""
from io import BytesIO
from pathlib import Path
import os

import pytest

from camctl.bootstrap.application import ConfigAdapter

if os.name == "nt":
    _EXAMPLE_HOME = "C:/home/example"
    _SPACE_HOME = "C:/home/space name"
    _OTHER_ROOT = "C:/srv"
else:
    _EXAMPLE_HOME = "/home/example"
    _SPACE_HOME = "/home/space name"
    _OTHER_ROOT = "/srv"


def _expanded(home: str, *parts: str) -> str:
    """与生产 `_expand` 相同的字符串形式（`str(Path(...))`）。"""
    return str(Path(home, *parts))


@pytest.mark.parametrize("selected", [None, Path("/selected/missing.toml")])
def test_missing_config_expands_all_default_paths(mocker, selected):
    mocker.patch.object(Path, "open", autospec=True, side_effect=FileNotFoundError)
    config = ConfigAdapter(Path(_EXAMPLE_HOME)).load(selected)
    assert config.paths.state_db == _expanded(_EXAMPLE_HOME, ".camctl", "state.db")
    assert config.paths.log_file == _expanded(_EXAMPLE_HOME, ".camctl", "camctl.log")
    assert config.paths.staging == _expanded(_EXAMPLE_HOME, ".camctl", "staging")
    assert config.paths.ready == _expanded(_EXAMPLE_HOME, ".camctl", "ready")
    assert config.paths.processing == _expanded(_EXAMPLE_HOME, ".camctl", "processing")


def test_partial_override_keeps_expanded_defaults(mocker):
    document = f'[paths]\nready = "{_OTHER_ROOT}/ready"\n'.encode()
    mocker.patch.object(Path, "open", autospec=True,
                      side_effect=lambda *args, **kwargs: BytesIO(document))
    config = ConfigAdapter(Path(_EXAMPLE_HOME)).load(Path("/selected/config.toml"))
    assert config.paths.ready == _expanded(_OTHER_ROOT, "ready")
    assert config.paths.state_db == _expanded(_EXAMPLE_HOME, ".camctl", "state.db")
    assert config.paths.processing == _expanded(_EXAMPLE_HOME, ".camctl", "processing")


def test_home_prefix_expands_and_other_literals_stay_unchanged(mocker):
    document = (
        f'[paths]\nready = "$HOME/custom"\n'
        f'processing = "{_OTHER_ROOT}/$HOME/data"\n'
    ).encode()
    mocker.patch.object(Path, "open", autospec=True,
                      side_effect=lambda *args, **kwargs: BytesIO(document))
    config = ConfigAdapter(Path(_SPACE_HOME)).load(None)
    assert config.paths.ready == _expanded(_SPACE_HOME, "custom")
    assert config.paths.processing == _expanded(_OTHER_ROOT, "$HOME", "data")


@pytest.mark.parametrize("field_name", ["state_db", "log_file", "staging", "ready", "processing"])
@pytest.mark.parametrize("separators", ["//", "///"])
def test_home_prefix_with_repeated_separators_preserves_home(mocker, field_name, separators):
    document = f'[paths]\n{field_name} = "$HOME{separators}custom/{field_name}"\n'.encode()
    mocker.patch.object(Path, "open", autospec=True,
                      side_effect=lambda *args, **kwargs: BytesIO(document))
    config = ConfigAdapter(Path(_EXAMPLE_HOME)).load(None)
    assert getattr(config.paths, field_name) == _expanded(_EXAMPLE_HOME, "custom", field_name)


def test_missing_explicit_file_does_not_use_default_file(mocker):
    def read(path, *args, **kwargs):
        if path == Path("/selected/missing.toml"):
            raise FileNotFoundError
        return BytesIO(f'[paths]\nready = "{_OTHER_ROOT}/other-ready"\n'.encode())
    mocker.patch.object(Path, "open", autospec=True, side_effect=read)
    config = ConfigAdapter(Path(_EXAMPLE_HOME)).load(Path("/selected/missing.toml"))
    assert config.paths.ready == _expanded(_EXAMPLE_HOME, ".camctl", "ready")
