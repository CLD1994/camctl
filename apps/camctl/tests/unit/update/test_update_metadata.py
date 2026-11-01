"""更新包模式的纯输入分类。"""
import importlib.util
from pathlib import Path

import pytest

source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/bundle_support.py'
spec = importlib.util.spec_from_file_location('update_bundle_support_under_test', source)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
config_source = source.parents[1] / 'config_paths.py'
config_spec = importlib.util.spec_from_file_location('update_config_paths_under_test', config_source)
config_paths = importlib.util.module_from_spec(config_spec)
config_spec.loader.exec_module(config_paths)


@pytest.mark.parametrize('release', [{}, {'update': {}}, {'update': {'force': False}}])
def test_missing_or_false_force_is_ordinary(release):
    assert support.update_metadata(release) == {'force': False}


def test_force_preserves_complete_configuration():
    config = '[paths]\nstaging = "/work"\n'
    assert support.update_metadata({'update': {'force': True, 'config_toml': config}}) == {
        'force': True, 'config_toml': config}


@pytest.mark.parametrize('release', [None, {'update': None}, {'update': {'force': 1}},
                                     {'update': {'force': True}},
                                     {'update': {'force': True, 'config_toml': None}},
                                     {'update': {'config_toml': ''}},
                                     {'update': {'force': False, 'config_toml': ''}}])
def test_invalid_mode_combinations_are_rejected(release):
    with pytest.raises(ValueError):
        support.update_metadata(release)


def test_default_configuration_path_uses_supplied_home():
    assert config_paths.default_config_path('/user/home') == Path('/user/home/.camctl/config.toml')


def test_lock_paths_derive_from_database_path():
    assert config_paths.lock_file_paths('/data/state.db') == (
        Path('/data/state.db.session.lock'), Path('/data/state.db.admission.lock'))
