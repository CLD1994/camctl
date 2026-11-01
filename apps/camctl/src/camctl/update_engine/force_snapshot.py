"""强制重建的原始对象快照，保留跨对象硬链接及不存在事实。"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_file(path):
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_paths(paths, processing, protected):
    """核对待重建对象、processing 与维护材料之间的路径和文件身份。"""
    roots = {name: Path(value) for name, value in paths.items()}
    processing = Path(processing)
    protected = [Path(path) for path in protected]
    if any(not path.is_absolute() for path in (*roots.values(), processing, *protected)):
        raise ValueError('强制更新路径必须为绝对路径')

    def overlaps(a, b):
        return a == b or a.is_relative_to(b) or b.is_relative_to(a)

    normalized = {name: (Path(os.path.abspath(path)), path.resolve()) for name, path in roots.items()}
    excluded = [(Path(os.path.abspath(path)), path.resolve()) for path in (processing, *protected)]
    values = list(normalized.items())
    for index, (name, pair) in enumerate(values):
        for other_name, other in values[index + 1:]:
            if any(overlaps(a, b) for a, b in zip(pair, other)):
                raise ValueError('强制更新对象路径重叠: ' + name + ' / ' + other_name)
        if any(any(overlaps(a, b) for a, b in zip(pair, other)) for other in excluded):
            raise ValueError('强制更新对象与保留路径重叠: ' + name)
    info = processing.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('processing 不是目录')

    def identities(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return set()
        found = {(info.st_dev, info.st_ino)}
        if stat.S_ISDIR(info.st_mode):
            for directory, names, files in os.walk(path, followlinks=False,
                    onerror=lambda error: (_ for _ in ()).throw(error)):
                for name in (*names, *files):
                    child = (Path(directory) / name).lstat()
                    found.add((child.st_dev, child.st_ino))
        return found

    preserved = set()
    for path in (processing, *protected):
        preserved.update(identities(path.resolve()))
    for name, path in roots.items():
        if identities(path.resolve()) & preserved:
            raise ValueError('强制更新对象与保留路径共享文件身份: ' + name)
    return {'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid, 'gid': info.st_gid}


def inventory(paths, *, sync=False):
    """不跟随链接遍历全部对象，以统一身份记录跨对象硬链接。"""
    entries = {}
    links = {}

    def visit(path, key):
        try:
            info = path.lstat()
        except FileNotFoundError:
            entries[key] = None
            return
        record = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
                  'mtime_ns': info.st_mtime_ns,
                  'xattrs': {name: os.getxattr(path, name, follow_symlinks=False).hex()
                             for name in os.listxattr(path, follow_symlinks=False)}}
        if stat.S_ISDIR(info.st_mode):
            record['kind'] = 'dir'
            entries[key] = record
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child, key + '/' + child.name)
            if sync:
                _sync_directory(path)
        elif stat.S_ISREG(info.st_mode):
            record.update(kind='file', size=info.st_size, sha256=_digest(path),
                          hardlink=links.setdefault((info.st_dev, info.st_ino), key))
            entries[key] = record
            if sync:
                _sync_file(path)
        elif stat.S_ISLNK(info.st_mode):
            record.update(kind='symlink', target=os.readlink(path))
            entries[key] = record
        else:
            raise ValueError('无法备份的对象类型: ' + str(path))

    for name, path in sorted(paths.items()):
        visit(Path(path), name)
    return entries


def _copy_object(source, destination, copied):
    info = source.lstat()
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir(mode=0o700)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            _copy_object(child, destination / child.name, copied)
    elif stat.S_ISREG(info.st_mode):
        identity = (info.st_dev, info.st_ino)
        if identity in copied:
            os.link(copied[identity], destination)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
            copied[identity] = destination
    elif stat.S_ISLNK(info.st_mode):
        os.symlink(os.readlink(source), destination)
    else:
        raise ValueError('无法备份的对象类型: ' + str(source))
    os.chown(destination, info.st_uid, info.st_gid, follow_symlinks=False)
    shutil.copystat(source, destination, follow_symlinks=False)


def backup(paths, destination):
    """完整复制并同步全部原始对象，核对源与备份后返回冻结清单。"""
    destination = Path(destination)
    original = inventory(paths)
    destination.mkdir(mode=0o700)
    _sync_directory(destination.parent)
    copied = {}
    for name, source in sorted(paths.items()):
        if original[name] is not None:
            _copy_object(Path(source), destination / name, copied)
    backup_paths = {name: destination / name for name in paths}
    restored = inventory(backup_paths, sync=True)
    _sync_directory(destination)
    if original != restored or original != inventory(paths):
        raise ValueError('原始对象与完整备份不一致')
    return original


def prepare_namespaces(paths, token):
    """在修改意图前创建可核实的同文件系统恢复命名空间。"""
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('事务身份无效')
    namespaces = {}
    for name, value in sorted(paths.items()):
        if not re.fullmatch('[a-z][a-z0-9_-]*', name):
            raise ValueError('快照对象名称无效')
        path = Path(value)
        if not path.is_absolute():
            raise ValueError('快照对象路径必须为绝对路径')
        parent = path.parent
        missing = []
        while True:
            try:
                info = parent.lstat()
            except FileNotFoundError:
                missing.append(str(parent))
                if parent == parent.parent:
                    raise ValueError('找不到可用的恢复父目录')
                parent = parent.parent
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('恢复父路径不是目录: ' + str(parent))
            break
        namespace = parent / ('.camctl-restore-' + token + '-' + name)
        namespace.mkdir(mode=0o700)
        _sync_directory(parent)
        info = namespace.lstat()
        _sync_directory(namespace)
        namespaces[name] = {'path': str(namespace), 'dev': info.st_dev, 'ino': info.st_ino,
                            'uid': info.st_uid, 'gid': info.st_gid, 'missing_parents': missing}
    return namespaces


def _remove_owned(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _namespace(item):
    namespace = Path(item['path'])
    info = namespace.lstat()
    if (not stat.S_ISDIR(info.st_mode) or
            (info.st_dev, info.st_ino, info.st_uid, info.st_gid) !=
            (item['dev'], item['ino'], item['uid'], item['gid'])):
        raise ValueError('恢复命名空间身份不一致: ' + str(namespace))
    return namespace


def displace(paths, namespaces):
    """在已有修改意图下将旧对象移入事务命名空间。"""
    if set(paths) != set(namespaces):
        raise ValueError('恢复命名空间与对象不一致')
    for name, value in sorted(paths.items()):
        target = Path(value)
        namespace = _namespace(namespaces[name])
        try:
            target.lstat()
        except FileNotFoundError:
            continue
        displaced = Path(tempfile.mkdtemp(prefix='displaced-', dir=namespace))
        _sync_directory(namespace)
        os.replace(target, displaced / 'object')
        _sync_directory(target.parent)
        _sync_directory(displaced)


def restore(paths, backup_directory, expected, namespaces):
    """从完整备份重新准备并发布所有原对象，允许恢复再次中断。"""
    if set(paths) != set(namespaces):
        raise ValueError('恢复命名空间与对象不一致')
    backup_directory = Path(backup_directory)
    backup_paths = {name: backup_directory / name for name in paths}
    if inventory(backup_paths) != expected:
        raise ValueError('原始对象备份无法核实')
    stages = {}
    copied = {}
    for name, target in sorted(paths.items()):
        item = namespaces[name]
        namespace = _namespace(item)
        stage = namespace / 'stage'
        _remove_owned(stage)
        _sync_directory(namespace)
        if expected[name] is not None:
            _copy_object(backup_paths[name], stage, copied)
            stages[name] = stage
    if inventory({name: stages.get(name, Path(namespaces[name]['path']) / 'stage') for name in paths},
                 sync=True) != expected:
        raise ValueError('准备的恢复对象无法核实')
    for name, value in sorted(paths.items()):
        target = Path(value)
        namespace = Path(namespaces[name]['path'])
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        else:
            displaced = Path(tempfile.mkdtemp(prefix='displaced-', dir=namespace))
            _sync_directory(namespace)
            os.replace(target, displaced / 'object')
            _sync_directory(target.parent)
            _sync_directory(displaced)
        if expected[name] is not None:
            os.replace(stages[name], target)
            _sync_directory(target.parent)
            _sync_directory(namespace)
    if inventory(paths, sync=True) != expected:
        raise ValueError('恢复后的原始对象无法核实')
    missing = {Path(path) for item in namespaces.values() for path in item['missing_parents']}
    for path in sorted(missing, key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except FileNotFoundError:
            continue
        _sync_directory(path.parent)
