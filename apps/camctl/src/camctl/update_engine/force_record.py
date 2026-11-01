"""强制重建事务的持久记录与恢复定位约束。"""
from pathlib import Path
import re

from config_paths import lock_file_paths

_REQUIRED_OBJECTS = frozenset(('environment', 'configuration', 'database', 'wal', 'shm', 'staging', 'ready'))


def _absolute(value):
    return isinstance(value, str) and '\0' not in value and Path(value).is_absolute()


def _valid_manifest(manifest, roots):
    if not isinstance(manifest, dict) or not set(roots).issubset(manifest):
        return False
    for key, entry in manifest.items():
        if not isinstance(key, str):
            return False
        parts = key.split('/')
        if parts[0] not in roots or any(part in ('', '.', '..') for part in parts):
            return False
        if len(parts) > 1:
            parent = manifest.get('/'.join(parts[:-1]))
            if not isinstance(parent, dict) or parent.get('kind') != 'dir':
                return False
        if entry is None:
            if len(parts) != 1:
                return False
            continue
        if not isinstance(entry, dict):
            return False
        common = {'kind', 'mode', 'uid', 'gid', 'mtime_ns', 'xattrs'}
        kind = entry.get('kind')
        required = common | ({'size', 'sha256', 'hardlink'} if kind == 'file'
                             else {'target'} if kind == 'symlink' else set())
        if (kind not in ('file', 'dir', 'symlink') or set(entry) != required or
                any(type(entry[name]) is not int or entry[name] < 0 for name in ('mode', 'uid', 'gid')) or
                type(entry['mtime_ns']) is not int or
                not isinstance(entry['xattrs'], dict) or
                any(not isinstance(name, str) or not isinstance(value, str) or
                    len(value) % 2 or not re.fullmatch('[0-9a-f]*', value)
                    for name, value in entry['xattrs'].items())):
            return False
        if kind == 'symlink' and (not isinstance(entry['target'], str) or '\0' in entry['target']):
            return False
        if kind == 'file':
            link = entry['hardlink']
            if (type(entry['size']) is not int or entry['size'] < 0 or
                    not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256']) or
                    not isinstance(link, str) or link not in manifest):
                return False
            first = manifest[link]
            if (not isinstance(first, dict) or first.get('kind') != 'file' or first.get('hardlink') != link or
                    {k: v for k, v in first.items() if k != 'hardlink'} !=
                    {k: v for k, v in entry.items() if k != 'hardlink'}):
                return False
    return True


def classify(record):
    if not isinstance(record, dict) or record.get('mode') != 'force' or type(record.get('mutation_intent')) is not bool:
        raise ValueError('强制更新记录或修改意图无效')
    state = record.get('state')
    if state not in ('preparing', 'updating', 'committed', 'rolled_back'):
        raise ValueError('强制更新状态无效')
    intent = record['mutation_intent']
    if (state == 'preparing' and intent) or (state in ('updating', 'committed') and not intent):
        raise ValueError('强制更新状态与修改意图冲突')
    for key in ('output', 'controller', 'layout_path', 'configuration_path'):
        if not _absolute(record.get(key)):
            raise ValueError('强制更新路径无效: ' + key)
    source = record.get('configuration_source')
    if source is not None and not _absolute(source):
        raise ValueError('强制更新配置来源无效')
    layout = record.get('layout')
    fields = {'environment', 'state_db', 'staging', 'ready', 'processing'}
    if (not isinstance(layout, dict) or set(layout) != fields or
            any(not _absolute(layout[name]) for name in fields)):
        raise ValueError('强制更新布局无效')
    expected_locks = list(map(str, lock_file_paths(layout['state_db'])))
    if record.get('lock_paths') != expected_locks:
        raise ValueError('强制更新锁与布局不一致')
    objects = record.get('objects')
    if not isinstance(objects, dict) or set(objects) not in (_REQUIRED_OBJECTS, _REQUIRED_OBJECTS | {'client'}):
        raise ValueError('强制更新对象集合无效')
    expected = {'environment': layout['environment'], 'configuration': record['configuration_path'],
                'database': layout['state_db'], 'wal': layout['state_db'] + '-wal',
                'shm': layout['state_db'] + '-shm', 'staging': str(Path(layout['staging']).resolve()),
                'ready': str(Path(layout['ready']).resolve())}
    if any(objects.get(name) != value for name, value in expected.items()):
        raise ValueError('强制更新对象与布局不一致')
    if any(not _absolute(value) for value in objects.values()):
        raise ValueError('强制更新对象路径无效')
    identity = record.get('output_identity')
    if (not isinstance(identity, dict) or set(identity) != {'dev', 'ino', 'uid', 'gid'} or
            any(type(value) is not int or value < 0 for value in identity.values())):
        raise ValueError('强制更新输出身份无效')
    if intent:
        manifest = record.get('manifest')
        namespaces = record.get('namespaces')
        if not _valid_manifest(manifest, objects):
            raise ValueError('强制更新备份清单不完整')
        if not isinstance(namespaces, dict) or set(namespaces) != set(objects):
            raise ValueError('强制更新恢复位置不完整')
        for name, item in namespaces.items():
            if (not isinstance(item, dict) or set(item) != {'path', 'dev', 'ino', 'uid', 'gid', 'missing_parents'} or
                    not isinstance(item['path'], str) or not Path(item['path']).is_absolute() or
                    any(type(item[key]) is not int or item[key] < 0 for key in ('dev', 'ino', 'uid', 'gid')) or
                    not isinstance(item['missing_parents'], list) or
                    any(not isinstance(path, str) or not Path(path).is_absolute() for path in item['missing_parents'])):
                raise ValueError('强制更新恢复位置无效: ' + name)
    return state


def validate_target(record, target):
    target = Path(target)
    output = Path(record['output'])
    token = output.name
    if (output.parent != target / 'updates' or not re.fullmatch('[0-9a-f]{32}', token) or
            Path(record['layout_path']) != target / 'maintenance/layout.json'):
        raise ValueError('强制更新记录与部署不一致')
    for name, item in record.get('namespaces', {}).items():
        namespace = Path(item['path'])
        object_parent = Path(record['objects'][name]).parent
        if (namespace.name != '.camctl-restore-' + token + '-' + name or
                namespace.parent not in (object_parent, *object_parent.parents)):
            raise ValueError('恢复位置与强制更新对象不一致: ' + name)
        expected_missing = []
        cursor = object_parent
        while cursor != namespace.parent:
            expected_missing.append(str(cursor))
            cursor = cursor.parent
        if item['missing_parents'] != expected_missing:
            raise ValueError('恢复父目录记录与对象不一致: ' + name)
