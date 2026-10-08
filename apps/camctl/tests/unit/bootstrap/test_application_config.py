"""配置适配器向各命令提供同一份已展开路径。"""
from io import BytesIO
from pathlib import Path

import pytest

from camctl.bootstrap.application import ConfigAdapter


@pytest.mark.parametrize("selected", [None, Path("/selected/missing.toml")])
def test_missing_config_expands_all_default_paths(mocker, selected):
    mocker.patch.object(Path, "open", autospec=True, side_effect=FileNotFoundError)
    config = ConfigAdapter(Path("/home/example")).load(selected)
    assert config.paths.state_db == "/home/example/.camctl/state.db"
    assert config.paths.log_file == "/home/example/.camctl/camctl.log"
    assert config.paths.staging == "/home/example/.camctl/staging"
    assert config.paths.ready == "/home/example/.camctl/ready"
    assert config.paths.processing == "/home/example/.camctl/processing"


def test_partial_override_keeps_expanded_defaults(mocker):
    mocker.patch.object(Path, "open", autospec=True,
                      side_effect=lambda *args, **kwargs: BytesIO(b'[paths]\nready = "/srv/ready"\n'))
    config = ConfigAdapter(Path("/home/example")).load(Path("/selected/config.toml"))
    assert config.paths.ready == "/srv/ready"
    assert config.paths.state_db == "/home/example/.camctl/state.db"
    assert config.paths.processing == "/home/example/.camctl/processing"


def test_home_prefix_expands_and_other_literals_stay_unchanged(mocker):
    document = b'[paths]\nready = "$HOME/custom"\nprocessing = "/srv/$HOME/data"\n'
    mocker.patch.object(Path, "open", autospec=True,
                      side_effect=lambda *args, **kwargs: BytesIO(document))
    config = ConfigAdapter(Path("/home/space name")).load(None)
    assert config.paths.ready == "/home/space name/custom"
    assert config.paths.processing == "/srv/$HOME/data"


@pytest.mark.parametrize("field_name", ["state_db", "log_file", "staging", "ready", "processing"])
@pytest.mark.parametrize("separators", ["//", "///"])
def test_home_prefix_with_repeated_separators_preserves_home(mocker, field_name, separators):
    document = f'[paths]\n{field_name} = "$HOME{separators}custom/{field_name}"\n'.encode()
    mocker.patch.object(Path, "open", autospec=True,
                      side_effect=lambda *args, **kwargs: BytesIO(document))
    config = ConfigAdapter(Path("/home/example")).load(None)
    assert getattr(config.paths, field_name) == f"/home/example/custom/{field_name}"


def test_missing_explicit_file_does_not_use_default_file(mocker):
    def read(path, *args, **kwargs):
        if path == Path("/selected/missing.toml"):
            raise FileNotFoundError
        return BytesIO(b'[paths]\nready = "/other/ready"\n')
    mocker.patch.object(Path, "open", autospec=True, side_effect=read)
    config = ConfigAdapter(Path("/home/example")).load(Path("/selected/missing.toml"))
    assert config.paths.ready == "/home/example/.camctl/ready"
