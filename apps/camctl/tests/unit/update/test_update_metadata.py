"""更新包模式的纯输入分类。"""
import importlib.util
from pathlib import Path

import pytest

source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/bundle_support.py'
spec = importlib.util.spec_from_file_location('update_bundle_support_under_test', source)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


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
