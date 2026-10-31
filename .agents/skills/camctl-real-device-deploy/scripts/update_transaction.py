"""停机更新的持久备份、锁和可重复恢复。"""
import fcntl
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat


class JournalState(Enum):
    PREPARING = 'preparing'
    UPDATING = 'updating'
    COMMITTED = 'committed'
    ROLLED_BACK = 'rolled_back'


def valid_file_metadata(value):
    return (isinstance(value, dict) and set(value) == {'uid', 'gid', 'mode', 'xattrs'}
            and all(type(value[key]) is int and value[key] >= 0 for key in ('uid', 'gid', 'mode'))
            and isinstance(value['xattrs'], dict)
            and all(isinstance(key, str) and isinstance(data, str)
                    and len(data) % 2 == 0 and re.fullmatch('[0-9a-f]*', data)
                    for key, data in value['xattrs'].items()))


def classify_record(record):
    """拒绝未知或不完整记录，不能把它当作尚未修改。"""
    if not isinstance(record, dict) or type(record.get('mutation_intent')) is not bool:
        raise ValueError('更新记录缺少合法修改意图')
    try:
        state = JournalState(record['state'])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError('更新记录状态无效') from error
    intent = record['mutation_intent']
    if ((state is JournalState.PREPARING and intent)
            or (state in (JournalState.UPDATING, JournalState.COMMITTED) and not intent)):
        raise ValueError('更新记录状态与修改意图冲突')
    for key in ('env', 'env_backup', 'state_db', 'db_backup', 'output', 'controller'):
        value = record.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('更新记录路径无效: ' + key)
    locks = record.get('lock_paths')
    if (not isinstance(locks, list) or len(locks) != 2
            or any(not isinstance(path, str) or not Path(path).is_absolute() for path in locks)
            or len(set(locks)) != 2):
        raise ValueError('更新记录锁路径无效')
    original = record.get('original')
    if (not isinstance(original, dict) or not isinstance(original.get('config'), str)
            or not Path(original['config']).is_absolute()):
        raise ValueError('更新记录原配置无效')
    if (not isinstance(original.get('config_sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', original['config_sha256'])
            or not isinstance(original.get('deployment'), dict)
            or original['deployment'].get('state_db') != record['state_db']
            or not isinstance(original['deployment'].get('metadata'), dict)):
        raise ValueError('更新记录原部署身份无效')
    if (original.get('state_db') != record['state_db']
            or original.get('lock_paths') != locks):
        raise ValueError('更新记录数据库或锁身份不一致')
    if intent:
        if (not isinstance(record.get('env_manifest'), dict) or not record['env_manifest']
                or not isinstance(record.get('db_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', record['db_sha256'])
                or not valid_file_metadata(record.get('db_meta'))
                or not isinstance(record['env_manifest'].get(''), dict)
                or record['env_manifest'].get('', {}).get('kind') != 'dir'):
            raise ValueError('更新记录恢复材料不完整')
    client = record.get('client')
    if 'client' in record:
        if (not isinstance(client, dict) or type(client.get('present')) is not bool
                or not isinstance(client.get('path'), str)
                or not Path(client['path']).is_absolute()
                or not isinstance(client.get('configured_path'), str)
                or not Path(client['configured_path']).is_absolute()):
            raise ValueError('更新记录能力文件材料无效')
        if client['present'] and (not isinstance(client.get('backup'), str)
                                  or not Path(client['backup']).is_absolute()
                                  or not isinstance(client.get('sha256'), str)
                                  or not re.fullmatch('[0-9a-f]{64}', client['sha256'])
                                  or not valid_file_metadata(client.get('meta'))):
            raise ValueError('更新记录能力文件备份不完整')
    return state


def validate_record_target(record, target):
    output = Path(record['output'])
    updates = target / 'updates'
    if (Path(record['env']) != target / 'venv' or output.parent != updates
            or Path(record['env_backup']) != output / 'venv.backup'
            or Path(record['db_backup']) != output / 'state.backup.db'):
        raise ValueError('更新记录与目标目录不一致')
    client = record.get('client')
    if client is not None and client['present'] and Path(client['backup']) != output / 'client-capabilities.backup':
        raise ValueError('能力文件备份与更新记录目录不一致')


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


def file_metadata(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('需要普通文件: ' + str(path))
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode),
            'xattrs': {key: os.getxattr(path, key).hex() for key in os.listxattr(path)}}


def assert_file(path, sha256, metadata):
    if digest(path) != sha256 or file_metadata(path) != metadata:
        raise ValueError('文件内容或元信息无法核实: ' + str(path))


def verify_database_snapshot(path, expected, verify_integrity):
    """读取独立数据库并核对保存在更新记录中的原实例身份。"""
    keys = ('application_id', 'instance_id', 'format_version', 'staging_path',
            'ready_path', 'processing_path')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        verify_integrity(connection)
        row = connection.execute(
            'SELECT application_id, instance_id, format_version, staging_path, '
            'ready_path, processing_path FROM database_metadata WHERE id = 1').fetchone()
        if row is None or dict(zip(keys, row)) != expected:
            raise ValueError('数据库备份身份或目录绑定不一致')
    finally:
        connection.close()


def validate_paths(target, original, client_data, *, before_lock=False):
    """在任何安装前隔离保留对象、能力目标与更新控制文件。"""
    env_path = target / 'venv'
    env = env_path.resolve()
    updates = (target / 'updates').resolve()
    config = Path(original['config']).resolve()
    db = Path(original['state_db']).resolve()
    locks = [Path(path).resolve() for path in original['lock_paths']]
    locks.append((target / 'updates/update.lock').resolve())
    protected = [config, db, *locks]
    raw_protected = [Path(original['config']), Path(original['state_db']),
                     *(Path(path) for path in original['lock_paths']), target / 'updates/update.lock']
    for path, raw in zip(protected, raw_protected):
        if path == env or path.is_relative_to(env):
            raise ValueError('保留对象位于更新中的虚拟环境: ' + str(path))
        if raw == env_path or raw.is_relative_to(env_path):
            raise ValueError('保留对象路径位于更新中的虚拟环境: ' + str(raw))
    candidate = ((client_data / 'device-capabilities.json').resolve()
                 if client_data is not None else None)
    raw_candidate = client_data / 'device-capabilities.json' if client_data is not None else None
    excluded = [env, updates, env.with_name(env.name + '.camctl-restore'),
                env.with_name(env.name + '.camctl-displaced')]
    if candidate is not None and any(candidate == path or candidate.is_relative_to(path) for path in excluded):
        raise ValueError('能力文件位于更新环境或控制材料内')
    raw_excluded = [env_path, target / 'updates',
                    env_path.with_name(env_path.name + '.camctl-restore'),
                    env_path.with_name(env_path.name + '.camctl-displaced')]
    if raw_candidate is not None and any(raw_candidate == path or raw_candidate.is_relative_to(path)
                                         for path in raw_excluded):
        raise ValueError('能力文件路径位于更新环境或控制材料内')
    reserved = [*protected, Path(str(db) + '-wal'), Path(str(db) + '-shm'),
                db.with_name(db.name + '.camctl-restore')]
    if candidate is not None and candidate in reserved:
        raise ValueError('能力文件与保留对象路径冲突')

    def identity(path):
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino
    protected_ids = [identity(path) for path in protected]
    required_ids = [protected_ids[0], protected_ids[1], protected_ids[4]]
    if not before_lock:
        required_ids.extend(protected_ids[2:4])
    existing_ids = [value for value in protected_ids if value is not None]
    if any(value is None for value in required_ids) or len(set(existing_ids)) != len(existing_ids):
        raise ValueError('保留对象不存在或共享文件身份')
    candidate_id = identity(candidate) if candidate is not None else None
    for path in reserved:
        other = identity(path)
        if candidate_id is not None and candidate_id == other:
            raise ValueError('能力文件与保留对象为同一文件')
    # 硬链接可使目录外文件仍与 venv 内对象共享内容。
    outside_ids = {value for value in [candidate_id, *protected_ids]
                   if value is not None}
    for directory, names, files in os.walk(env, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in (*names, *files):
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                info = path.stat()
            if (info.st_dev, info.st_ino) in outside_ids:
                raise ValueError('保留对象与虚拟环境内文件为同一对象')
    if candidate_id is not None:
        for directory, names, files in os.walk(updates, followlinks=False,
                                                onerror=lambda error: (_ for _ in ()).throw(error)):
            for name in (*names, *files):
                path = Path(directory) / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    info = path.stat()
                if (info.st_dev, info.st_ino) == candidate_id:
                    raise ValueError('能力文件与更新控制材料为同一对象')


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


def recover(record, verify_integrity):
    """使用保留的完整备份幂等恢复；无修改意图时不覆盖原环境。"""
    state = classify_record(record)
    if state is JournalState.PREPARING:
        return
    if state is not JournalState.UPDATING:
        raise ValueError('终态更新记录不能再次恢复')
    original = record['original']
    if digest(Path(original['config'])) != original['config_sha256']:
        raise ValueError('原配置与更新记录不一致')
    env = Path(record['env'])
    env_backup = Path(record['env_backup'])
    db = Path(record['state_db'])
    db_backup = Path(record['db_backup'])
    expected_env = record['env_manifest']
    if manifest(env_backup) != expected_env:
        raise ValueError('更新备份无法核实')
    assert_file(db_backup, record['db_sha256'], record['db_meta'])
    metadata = original['deployment']['metadata']
    verify_database_snapshot(db_backup, metadata, verify_integrity)
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
    assert_file(db, record['db_sha256'], record['db_meta'])
    verify_database_snapshot(db, metadata, verify_integrity)
    client = record.get('client')
    if client:
        path = Path(client['path'])
        if Path(client['configured_path']).resolve() != path:
            raise ValueError('能力文件路径与更新记录不一致')
        if client['present']:
            backup = Path(client['backup'])
            assert_file(backup, client['sha256'], client['meta'])
            replace_file(backup, path)
            assert_file(path, client['sha256'], client['meta'])
        elif path.exists():
            path.unlink()
            sync_dir(path.parent)
        if not client['present'] and path.exists():
            raise ValueError('原本不存在的能力文件仍然存在')
