"""强制更新快照边界的内存替身。"""
import importlib.util
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/force_snapshot.py'
spec = importlib.util.spec_from_file_location('force_snapshot_under_test', source)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def info(mode, inode, size=0):
    return SimpleNamespace(st_mode=mode, st_dev=4, st_ino=inode, st_uid=10, st_gid=20,
                           st_mtime_ns=100, st_size=size)


def test_inventory_preserves_missing_symlink_and_cross_root_hardlink():
    objects = {'config': Path('/original/config'), 'database': Path('/original/db'),
               'staging': Path('/original/stage'), 'ready': Path('/original/ready')}
    stats = {'/original/config': info(stat.S_IFLNK | 0o777, 2),
             '/original/db': info(stat.S_IFREG | 0o600, 3, 7),
             '/original/stage': info(stat.S_IFDIR | 0o700, 4),
             '/original/stage/copy': info(stat.S_IFREG | 0o600, 3, 7)}
    def lstat(path):
        try:
            return stats[str(path)]
        except KeyError:
            raise FileNotFoundError(str(path))
    with patch.object(Path, 'lstat', lstat), \
            patch.object(Path, 'iterdir', lambda path: [path / 'copy']), \
            patch.object(snapshot.os, 'listxattr', return_value=[]), \
            patch.object(snapshot.os, 'readlink', return_value='../actual'), \
            patch.object(snapshot, '_digest', return_value='a' * 64):
        result = snapshot.inventory(objects)
    assert result['ready'] is None
    assert result['config']['target'] == '../actual'
    assert result['database']['size'] == 7
    assert result['database']['hardlink'] == result['staging/copy']['hardlink'] == 'database'


def test_path_validation_does_not_traverse_processing():
    root = info(stat.S_IFDIR | 0o700, 7)
    def lstat(path):
        if str(path) == '/processing':
            return root
        raise FileNotFoundError(str(path))
    with patch.object(Path, 'resolve', lambda path: path), patch.object(Path, 'stat', return_value=root), \
            patch.object(Path, 'lstat', lstat), \
            patch.object(snapshot.os, 'walk', side_effect=AssertionError('不得遍历 processing')):
        identity = snapshot.validate_paths({'database': '/db'}, '/processing', ['/control'])
    assert identity == {'dev': 4, 'ino': 7, 'uid': 10, 'gid': 20}


def test_path_validation_rejects_processing_overlap_before_effects():
    with patch.object(Path, 'resolve', lambda path: path), patch.object(Path, 'stat') as stat_file:
        with pytest.raises(ValueError):
            snapshot.validate_paths({'staging': '/processing/old'}, '/processing', ['/control'])
    stat_file.assert_not_called()


def test_backup_requires_copy_and_source_to_match_before_return():
    paths = {'database': Path('/db'), 'wal': Path('/db-wal')}
    expected = {'database': {'kind': 'file'}, 'wal': None}
    with patch.object(snapshot, 'inventory', side_effect=[expected, expected, expected]), \
            patch.object(Path, 'mkdir'), patch.object(snapshot, '_sync_directory'), \
            patch.object(snapshot, '_copy_object') as copy:
        assert snapshot.backup(paths, Path('/backup')) == expected
    copy.assert_called_once()
    assert copy.call_args.args[:2] == (Path('/db'), Path('/backup/database'))


def test_backup_rejects_source_change_after_copy():
    paths = {'database': Path('/db')}
    original = {'database': {'kind': 'file', 'sha256': 'old'}}
    changed = {'database': {'kind': 'file', 'sha256': 'new'}}
    with patch.object(snapshot, 'inventory', side_effect=[original, original, changed]), \
            patch.object(Path, 'mkdir'), patch.object(snapshot, '_sync_directory'), \
            patch.object(snapshot, '_copy_object'):
        with pytest.raises(ValueError):
            snapshot.backup(paths, Path('/backup'))


def test_restore_prepares_copy_before_replacing_original():
    paths = {'database': Path('/db')}
    expected = {'database': {'kind': 'file', 'sha256': 'old'}}
    namespaces = {'database': {'path': '/namespace', 'dev': 4, 'ino': 8, 'uid': 10, 'gid': 20,
                               'missing_parents': []}}
    ns_info = info(stat.S_IFDIR | 0o700, 8)
    events = Mock()
    def lstat(path):
        return ns_info if str(path) == '/namespace' else info(stat.S_IFREG | 0o600, 9)
    with patch.object(snapshot, 'inventory', side_effect=[expected, expected, expected]), \
            patch.object(Path, 'lstat', lstat), patch.object(snapshot, '_remove_owned'), \
            patch.object(snapshot, '_sync_directory'), \
            patch.object(snapshot, '_copy_object', side_effect=lambda *args: events.copy()), \
            patch.object(snapshot.tempfile, 'mkdtemp', return_value='/namespace/displaced'), \
            patch.object(snapshot.os, 'replace', side_effect=lambda *args: events.replace(*args)):
        snapshot.restore(paths, Path('/backup'), expected, namespaces)
    assert events.mock_calls == [
        ('copy', (), {}),
        ('replace', (Path('/db'), Path('/namespace/displaced/object')), {}),
        ('replace', (Path('/namespace/stage'), Path('/db')), {}),
    ]


def test_restore_never_replaces_when_backup_does_not_match():
    expected = {'database': {'kind': 'file', 'sha256': 'old'}}
    with patch.object(snapshot, 'inventory', return_value={'database': None}), \
            patch.object(snapshot.os, 'replace') as replace:
        with pytest.raises(ValueError):
            snapshot.restore({'database': Path('/db')}, Path('/backup'), expected,
                             {'database': {'path': '/namespace'}})
    replace.assert_not_called()
