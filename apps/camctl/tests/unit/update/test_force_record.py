"""强制更新记录的无状态输入分区。"""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

paths = ModuleType('config_paths')
paths.lock_file_paths = lambda db: (Path(str(db) + '.session.lock'), Path(str(db) + '.admission.lock'))
source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/force_record.py'
spec = importlib.util.spec_from_file_location('force_record_under_test', source)
record_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'config_paths': paths}):
    spec.loader.exec_module(record_module)


def force_record(state='preparing'):
    token = 'a' * 32
    layout = {'environment': '/target/venv', 'state_db': '/state/db', 'staging': '/stage',
              'ready': '/ready', 'processing': '/processing'}
    objects = {'environment': '/target/venv', 'configuration': '/config', 'database': '/state/db',
               'wal': '/state/db-wal', 'shm': '/state/db-shm', 'staging': '/stage', 'ready': '/ready'}
    record = {'mode': 'force', 'state': state, 'mutation_intent': state != 'preparing',
              'output': '/target/updates/' + token, 'controller': '/python',
              'layout_path': '/target/maintenance/layout.json', 'configuration_path': '/config',
              'bundle_path': '/bundle', 'processing_identity': {'dev': 1, 'ino': 2, 'uid': 3, 'gid': 4},
              'configuration_source': None, 'layout': layout, 'objects': objects,
              'config_snapshot': {'source': None, 'resolved': '/config', 'present': False, 'sha256': None},
              'lock_paths': ['/state/db.session.lock', '/state/db.admission.lock'],
              'output_identity': {'dev': 1, 'ino': 2, 'uid': 3, 'gid': 4}}
    if state != 'preparing':
        record['manifest'] = {name: None for name in objects}
        record['namespaces'] = {name: {'path': str(Path(path).parent / ('.camctl-restore-' + token + '-' + name)),
                                       'dev': 1, 'ino': 2, 'uid': 3, 'gid': 4, 'missing_parents': []}
                                for name, path in objects.items()}
    return record


@patch.object(Path, 'resolve', lambda path: path)
def test_record_allows_preparing_and_completed_original_absence():
    for state in ('preparing', 'updating', 'committed', 'rolled_back'):
        record = force_record(state)
        assert record_module.classify(record) == state
        record_module.validate_target(record, Path('/target'))


@patch.object(Path, 'resolve', lambda path: path)
def test_mutation_intent_requires_all_backup_and_recovery_materials():
    record = force_record('updating')
    del record['manifest']['database']
    with pytest.raises(ValueError):
        record_module.classify(record)


@patch.object(Path, 'resolve', lambda path: path)
def test_record_rejects_changed_layout_and_unowned_namespace():
    record = force_record('updating')
    record['objects']['database'] = '/other/db'
    with pytest.raises(ValueError):
        record_module.classify(record)
    record = force_record('updating')
    record['namespaces']['database']['path'] = '/unrelated/.camctl-restore-' + 'a' * 32 + '-database'
    with pytest.raises(ValueError):
        record_module.validate_target(record, Path('/target'))
