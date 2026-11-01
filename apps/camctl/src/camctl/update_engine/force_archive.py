"""强制更新提交后的独立归档任务。"""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile

import force_record
import force_snapshot
import update_transaction as transaction


def _identity(record):
    output = Path(record['output'])
    if transaction.directory_identity(output) != record['output_identity']:
        raise ValueError('归档事务输出身份已变化')
    force_record.validate_target(record, output.parent.parent)
    return output


def _copy_sync(source, destination):
    try:
        src = source.open('rb')
    except FileNotFoundError:
        return False
    with src, destination.open('wb') as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    return True


def prepare(record):
    """冻结归档材料并持久登记任务，不改变回滚备份。"""
    output = _identity(record)
    if force_record.classify(record) != 'updating':
        raise ValueError('归档准备要求完整备份和未提交事务')
    job_path = output / 'archive-job.json'
    try:
        existing = json.loads(job_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing['record'] != record:
            raise ValueError('归档任务与事务不一致')
        return
    frozen = output / 'archive-input'
    frozen.mkdir(mode=0o700, exist_ok=True)
    transaction.sync_dir(output)
    inputs = {}
    for name in ('update.log', 'receipt.json'):
        if _copy_sync(output / name, frozen / name):
            inputs[name] = transaction.digest(frozen / name)
        else:
            inputs[name] = None
    transaction.sync_dir(frozen)
    job = {'version': 1, 'record': record, 'inputs': inputs, 'state': 'prepared', 'sha256': None}
    transaction.atomic_json(job_path, job)


class _CheckedReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.source.read(size)
        self.digest.update(data)
        return data


def _add_manifest(tar, backup, manifest):
    for name, entry in sorted(manifest.items()):
        if name.split('/')[0] in ('staging', 'ready') or entry is None:
            continue
        info = tarfile.TarInfo('objects/' + name)
        info.mode, info.uid, info.gid = entry['mode'], entry['uid'], entry['gid']
        info.mtime = entry['mtime_ns'] / 1_000_000_000
        info.pax_headers = {'SCHILY.xattr.' + k: bytes.fromhex(v).decode('latin1')
                            for k, v in entry['xattrs'].items()}
        if entry['kind'] == 'dir':
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        elif entry['kind'] == 'symlink':
            info.type, info.linkname = tarfile.SYMTYPE, entry['target']
            tar.addfile(info)
        elif entry['kind'] == 'file':
            info.size = entry['size']
            with (backup / name).open('rb') as source:
                checked = _CheckedReader(source)
                tar.addfile(info, checked)
                if checked.digest.hexdigest() != entry['sha256'] or source.read(1):
                    raise ValueError('归档备份内容无法核实: ' + name)
        else:
            raise ValueError('归档备份类型无效: ' + name)


def _add_bytes(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o600, 0
    tar.addfile(info, io.BytesIO(data))


def _add_file(tar, name, path, expected):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = path.stat().st_size, 0o600, 0
    with path.open('rb') as source:
        checked = _CheckedReader(source)
        tar.addfile(info, checked)
        if checked.digest.hexdigest() != expected or source.read(1):
            raise ValueError('冻结材料内容无法核实: ' + str(path))


def _build(output, job, destination):
    record = job['record']
    with destination.open('wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as tar:
                _add_bytes(tar, 'record.json', json.dumps(record, sort_keys=True, ensure_ascii=True).encode())
                _add_bytes(tar, 'inputs.json', json.dumps(job['inputs'], sort_keys=True).encode())
                _add_manifest(tar, output / 'backup', record['manifest'])
                for item in record['logs']:
                    for entry in item['files']:
                        if entry['present']:
                            name = entry['copy']
                            if Path(name).name != name:
                                raise ValueError('冻结日志名称无效')
                            _add_file(tar, 'old-logs/' + name, output / 'old-logs' / name, entry['sha256'])
                for name in ('update.log', 'receipt.json'):
                    if job['inputs'][name] is not None:
                        _add_file(tar, 'update/' + name, output / 'archive-input' / name, job['inputs'][name])
        raw.flush()
        os.fsync(raw.fileno())


def _publish(output, processing, name, digest):
    archive = output / 'archive.tar.gz'
    preparation = processing / ('.camctl-force-' + output.name)
    preparation.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(preparation.lstat().st_mode):
        raise ValueError('归档准备位置不是目录')
    transaction.sync_dir(processing)
    complete = preparation / 'archive.tar.gz'
    temp = preparation / '.archive.tmp'
    try:
        info = complete.lstat()
    except FileNotFoundError:
        temp.unlink(missing_ok=True)
        transaction.sync_dir(preparation)
        with temp.open('xb') as dst, archive.open('rb') as src:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if transaction.digest(temp) != digest:
            raise ValueError('归档发布副本摘要不一致')
        os.replace(temp, complete)
        transaction.sync_dir(preparation)
    else:
        if not stat.S_ISREG(info.st_mode) or transaction.digest(complete) != digest:
            raise ValueError('已有归档准备文件内容不一致')
        transaction.sync_file(complete)
        transaction.sync_dir(preparation)
    try:
        os.link(complete, processing / name, follow_symlinks=False)
    except FileExistsError:
        target = processing / name
        if not stat.S_ISREG(target.lstat().st_mode) or transaction.digest(target) != digest:
            raise ValueError('归档目标已存在且内容不同')
    transaction.sync_dir(processing)
    transaction.sync_dir(preparation)


def _cleanup(record, output):
    backup = output / 'backup'
    force_snapshot._remove_owned(backup)
    transaction.sync_dir(output)
    for item in record['namespaces'].values():
        parent = Path(item['path']).parent
        try:
            namespace = force_snapshot._namespace(item)
        except FileNotFoundError:
            transaction.sync_dir(parent)
            continue
        force_snapshot._remove_owned(namespace)
        transaction.sync_dir(namespace.parent)
    for value in record.get('temporary_paths', []):
        temporary = Path(value)
        if temporary.parent != output or temporary.name != 'prepare-venv':
            raise ValueError('归档临时清理路径无效')
        force_snapshot._remove_owned(temporary)
        transaction.sync_dir(output)
    processing = Path(record['layout']['processing'])
    if transaction.directory_identity(processing) != record['processing_identity']:
        raise ValueError('归档交付目录身份已变化')
    preparation = processing / ('.camctl-force-' + output.name)
    try:
        info = preparation.lstat()
    except FileNotFoundError:
        transaction.sync_dir(processing)
    else:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('归档准备位置不是目录')
        (preparation / 'archive.tar.gz').unlink(missing_ok=True)
        (preparation / '.archive.tmp').unlink(missing_ok=True)
        transaction.sync_dir(preparation)
        preparation.rmdir()
        transaction.sync_dir(processing)


def finalize(record):
    """消费已可靠提交事务的归档任务，保留失败时的恢复责任。"""
    if force_record.classify(record) != 'committed':
        raise ValueError('归档仅接受已提交事务')
    output = _identity(record)
    path = output / 'archive-job.json'
    job = json.loads(path.read_text(encoding='utf-8'))
    transaction.sync_file(path)
    transaction.sync_dir(output)
    frozen = job['record']
    if (job.get('version') != 1 or
            {k: v for k, v in record.items() if k != 'state'} !=
            {k: v for k, v in frozen.items() if k != 'state'}):
        raise ValueError('归档任务与已提交事务不一致')
    if job['state'] not in ('prepared', 'compressed', 'complete', 'cleaned'):
        raise ValueError('归档状态无效')
    if job['state'] == 'cleaned':
        return
    if job['state'] == 'prepared':
        temp = output / '.archive.tar.gz.tmp'
        _build(output, job, temp)
        os.replace(temp, output / 'archive.tar.gz')
        transaction.sync_dir(output)
        job['sha256'] = transaction.digest(output / 'archive.tar.gz')
        job['state'] = 'compressed'
        transaction.atomic_json(path, job)
    if job['state'] == 'compressed':
        digest = job['sha256']
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('归档摘要无效')
        if transaction.digest(output / 'archive.tar.gz') != digest:
            raise ValueError('归档材料摘要不一致')
        processing = Path(record['layout']['processing'])
        if transaction.directory_identity(processing) != record['processing_identity']:
            raise ValueError('归档交付目录身份已变化')
        _publish(output, processing, 'camctl-force-diagnostic-' + output.name + '.tar.gz', digest)
        job['state'] = 'complete'
        transaction.atomic_json(path, job)
    _cleanup(record, output)
    job['state'] = 'cleaned'
    transaction.atomic_json(path, job)
