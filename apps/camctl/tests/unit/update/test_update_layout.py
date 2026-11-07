"""维护布局定位与初始化输入在内存中核对。"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/layout.py'
spec = importlib.util.spec_from_file_location('update_layout_under_test', source)
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)

record = {'environment': '/deployment/venv', 'state_db': '/state/db', 'staging': '/staging',
          'ready': '/ready', 'processing': '/processing'}
reference = '/deployment/maintenance/layout.json'


def check(files, state_db='/state/db', staging='/staging'):
    def read(path, **kwargs):
        del kwargs
        if str(path) not in files:
            raise FileNotFoundError(str(path))
        return files[str(path)]
    with patch.object(Path, 'resolve', lambda path: path), patch.object(Path, 'read_text', read):
        return layout.check_initialization('/deployment/venv', state_db, staging, '/ready', '/processing')


def test_unregistered_and_matching_layouts_are_allowed():
    assert check({}) is None
    files = {'/deployment/venv/.camctl-maintenance-layout': json.dumps(reference),
             '/state/db.camctl-maintenance-layout': json.dumps(reference), reference: json.dumps(record)}
    assert check(files) is None


def test_changed_database_is_detected_from_environment_locator():
    files = {'/deployment/venv/.camctl-maintenance-layout': json.dumps(reference),
             reference: json.dumps(record)}
    with pytest.raises(ValueError):
        check(files, state_db='/new/database')


def test_existing_database_locator_detects_changed_directory():
    files = {'/state/db.camctl-maintenance-layout': json.dumps(reference), reference: json.dumps(record)}
    with pytest.raises(ValueError):
        check(files, staging='/other')


def test_different_locators_and_unreadable_record_are_rejected():
    files = {'/deployment/venv/.camctl-maintenance-layout': json.dumps(reference),
             '/state/db.camctl-maintenance-layout': json.dumps('/other/layout')}
    with pytest.raises(ValueError):
        check(files)
    del files['/state/db.camctl-maintenance-layout']
    with pytest.raises(FileNotFoundError):
        check(files)
