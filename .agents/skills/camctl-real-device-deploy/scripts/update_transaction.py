"""停机更新的持久备份、锁和可重复恢复。"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_file(path):
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def atomic_json(path, value):
    temp = path.with_name(path.name + '.new')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def manifest(root, *, sync=False):
    """记录全部文件、目录、链接及元信息，不跟随符号链接。"""
    result = {}
    hardlinks = {}
    def walk(path, name):
        info = path.lstat()
        entry = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
                 'mtime_ns': info.st_mtime_ns,
                 'xattrs': {key: os.getxattr(path, key, follow_symlinks=False).hex()
                            for key in os.listxattr(path, follow_symlinks=False)}}
        if stat.S_ISDIR(info.st_mode):
            entry['kind'] = 'dir'
            result[name] = entry
            for child in sorted(path.iterdir()):
                walk(child, (name + '/' if name else '') + child.name)
            if sync:
                sync_dir(path)
        elif stat.S_ISREG(info.st_mode):
            key = (info.st_dev, info.st_ino)
            first = hardlinks.setdefault(key, name)
            entry.update(kind='file', sha256=digest(path), hardlink=first)
            result[name] = entry
            if sync:
                sync_file(path)
        elif stat.S_ISLNK(info.st_mode):
            entry.update(kind='symlink', target=os.readlink(path))
            result[name] = entry
        else:
            raise ValueError('环境包含无法安全备份的文件: ' + str(path))
    walk(root, '')
    return result


def copy_environment(source, destination):
    """保留环境内部硬链接，但不与原环境共享可写 inode。"""
    copied = {}
    def copy_file(src, dst):
        info = os.stat(src, follow_symlinks=False)
        key = (info.st_dev, info.st_ino)
        if key in copied:
            os.link(copied[key], dst)
        else:
            shutil.copy2(src, dst)
            copied[key] = dst
        return dst
    shutil.copytree(source, destination, symlinks=True, copy_function=copy_file)
    def metadata(src, dst):
        info = src.lstat()
        if stat.S_ISDIR(info.st_mode):
            for child in src.iterdir():
                metadata(child, dst / child.name)
        os.chown(dst, info.st_uid, info.st_gid, follow_symlinks=False)
        shutil.copystat(src, dst, follow_symlinks=False)
    metadata(source, destination)


class Locks:
    """只关闭描述符，不主动解锁共享给更新写入者的 flock。"""
    def __init__(self):
        self.fds = []

    def acquire(self, path):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.fds.append(fd)

    def database(self, paths):
        for path in paths:
            self.acquire(Path(path))

    def close(self):
        errors = []
        while self.fds:
            try:
                os.close(self.fds.pop())
            except OSError as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup('关闭更新锁失败', errors)


def backup_database(source, destination):
    src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            if str(dst.execute('PRAGMA journal_mode').fetchone()[0]).lower() == 'wal':
                busy, _, _ = dst.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if busy:
                    raise ValueError('数据库备份的 WAL 无法完成检查点')
        finally:
            dst.close()
    finally:
        src.close()
    info = source.stat()
    os.chown(destination, info.st_uid, info.st_gid)
    shutil.copystat(source, destination)
    sync_file(destination)
    sync_dir(destination.parent)


def backup_environment(source, destination):
    before = manifest(source)
    copy_environment(source, destination)
    copied = manifest(destination, sync=True)
    sync_dir(destination.parent)
    if before != copied:
        raise ValueError('Python 环境备份与原环境不一致')
    return copied


def replace_file(source, destination):
    temp = destination.with_name(destination.name + '.camctl-restore')
    shutil.copy2(source, temp)
    info = source.stat()
    os.chown(temp, info.st_uid, info.st_gid)
    shutil.copystat(source, temp)
    sync_file(temp)
    os.replace(temp, destination)
    sync_dir(destination.parent)


def make_directories(path):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        sync_dir(directory.parent)


def recover(record):
    """使用保留的完整备份幂等恢复；无修改意图时不覆盖原环境。"""
    if not record.get('mutation_intent'):
        return
    original = record['original']
    if digest(Path(original['config'])) != original['config_sha256']:
        raise ValueError('原配置与更新记录不一致')
    env = Path(record['env'])
    env_backup = Path(record['env_backup'])
    db = Path(record['state_db'])
    db_backup = Path(record['db_backup'])
    expected_env = record['env_manifest']
    if manifest(env_backup) != expected_env or digest(db_backup) != record['db_sha256']:
        raise ValueError('更新备份无法核实')
    staging = env.with_name(env.name + '.camctl-restore')
    displaced = env.with_name(env.name + '.camctl-displaced')
    if staging.exists():
        shutil.rmtree(staging)
        sync_dir(staging.parent)
    copy_environment(env_backup, staging)
    if manifest(staging, sync=True) != expected_env:
        raise ValueError('恢复的 Python 环境无法核实')
    sync_dir(staging.parent)
    if displaced.exists():
        shutil.rmtree(displaced)
        sync_dir(displaced.parent)
    if env.exists():
        os.replace(env, displaced)
        sync_dir(env.parent)
    os.replace(staging, env)
    sync_dir(env.parent)
    if manifest(env) != expected_env:
        raise ValueError('原路径的 Python 环境无法核实')
    for suffix in ('-wal', '-shm'):
        sidecar = Path(str(db) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    sync_dir(db.parent)
    replace_file(db_backup, db)
    if digest(db) != record['db_sha256']:
        raise ValueError('恢复的数据库无法核实')
    client = record.get('client')
    if client:
        path = Path(client['path'])
        if Path(client['configured_path']).resolve() != path:
            raise ValueError('能力文件路径与更新记录不一致')
        if client['present']:
            backup = Path(client['backup'])
            if digest(backup) != client['sha256']:
                raise ValueError('能力文件备份无法核实')
            replace_file(backup, path)
            if digest(path) != client['sha256']:
                raise ValueError('恢复的能力文件无法核实')
        elif path.exists():
            path.unlink()
            sync_dir(path.parent)
