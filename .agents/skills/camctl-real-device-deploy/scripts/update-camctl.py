#!/usr/bin/env python3.11
"""停止 host 和全部 CLI 后，离线更新原部署中的 camctl。"""
from __future__ import annotations

import sys

# 每次启动或重新执行时，先阻止导入在包内写入缓存。
sys.dont_write_bytecode = True

import argparse
from email.parser import BytesParser
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import traceback
import uuid
import zipfile

from bundle_support import current_target, verify_checksums, wheel_target_manifest
from deploy_support import absolute_path
import update_transaction as transaction


def freeze_exception(error, stage):
    """冻结异常；旧发行物不可导入时保留标准异常链和编码器故障。"""
    try:
        from camctl.operations.error_diagnostics import exception_diagnostic
        diagnostic = exception_diagnostic(error)
    except Exception as encoder_error:
        def convert(item):
            if item is None:
                return None
            result = {'type': f'{item.exc_type.__module__}.{item.exc_type.__qualname__}',
                      'message': str(item),
                      'traceback': [{'filename': frame.filename, 'lineno': frame.lineno,
                                     'function': frame.name} for frame in item.stack],
                      'cause': convert(item.__cause__), 'context': convert(item.__context__),
                      'suppress_context': item.__suppress_context__}
            if item.exceptions is not None:
                result['exceptions'] = [convert(child) for child in item.exceptions]
            if item.__notes__ is not None:
                result['notes'] = list(item.__notes__)
            return result
        diagnostic = convert(traceback.TracebackException.from_exception(error, lookup_lines=False))
        diagnostic['encoder_error'] = convert(traceback.TracebackException.from_exception(
            encoder_error, lookup_lines=False))
    diagnostic['stage'] = stage
    return diagnostic


def deliver_diagnostics(ops, receipt, diagnostics):
    if not diagnostics:
        return
    try:
        ops.record_diagnostics(diagnostics)
    except Exception as error:
        failure = freeze_exception(error, 'diagnostic_log')
        receipt['log_error'] = failure
        try:
            print(json.dumps({'diagnostics': diagnostics, 'log_error': failure},
                             ensure_ascii=False, default=repr), file=sys.stderr)
            sys.stderr.flush()
        except Exception as stderr_error:
            receipt['stderr_error'] = freeze_exception(stderr_error, 'diagnostic_stderr')


def perform_update(ops):
    """冻结错误事实，先释放锁，再交付诊断和最终收据。"""
    receipt = {'status': 'checking', 'steps': [], 'environment_may_have_changed': False,
               'diagnostics': []}
    locked = True
    intent = False
    committed = False
    failures = []
    stage = 'preflight'
    try:
        receipt['original'] = ops.preflight()
        stage = 'save_original'
        ops.save(receipt)
        stage = 'lock'
        ops.lock(receipt['original'])
        locked = True
        stage = 'check_original'
        ops.check_original(receipt['original'])
        stage = 'save_original_check'
        ops.save(receipt)
        stage = 'backup'
        ops.backup(receipt)
        stage = 'save_install_intent'
        ops.mark_intent()
        intent = True
        receipt['environment_may_have_changed'] = True
        stage = 'install'
        ops.install(receipt)
        stage = 'migrate'
        ops.migrate(receipt)
        stage = 'check_new'
        ops.check_new(receipt)
        stage = 'sync_new'
        ops.sync_new()
        stage = 'commit'
        ops.commit()
        committed = True
        receipt['status'] = 'updated_stopped'
    except Exception as error:
        receipt.update(status='failed', error=f'{type(error).__name__}: {error}')
        failures.append((error, stage))
        if intent and not committed and stage != 'commit':
            try:
                ops.rollback()
                receipt['rollback'] = 'verified'
            except Exception as rollback_error:
                receipt['rollback'] = 'failed'
                failures.append((rollback_error, 'rollback'))
        elif stage == 'commit':
            receipt['rollback'] = 'commit_unknown'
    finally:
        if locked:
            try:
                ops.unlock()
            except Exception as error:
                receipt['unlock_error'] = f'{type(error).__name__}: {error}'
                if not committed:
                    receipt['status'] = 'failed'
                failures.append((error, 'unlock'))

    receipt['diagnostics'].extend(freeze_exception(error, label) for error, label in failures)

    deliver_diagnostics(ops, receipt, receipt['diagnostics'])
    try:
        ops.save(receipt)
    except Exception as error:
        receipt['receipt_error'] = f'{type(error).__name__}: {error}'
        if not committed:
            receipt['status'] = 'failed'
        diagnostic = freeze_exception(error, 'save_final_receipt')
        receipt['diagnostics'].append(diagnostic)
        deliver_diagnostics(ops, receipt, [diagnostic])
    return receipt


class UpdateOperations:
    def __init__(self, bundle, target, client_data, output, *, config_path=None):
        self.bundle, self.target, self.client_data, self.output = bundle, target, client_data, output
        self.python = target / 'venv/bin/python'
        self.config_path = config_path if config_path is not None else target / 'config.toml'
        self.environment = {k: v for k, v in os.environ.items() if k not in
                            ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
        self.update_locks = transaction.Locks()
        self.db_locks = transaction.Locks()
        self.pending = target / 'updates/pending.json'

    def record_diagnostics(self, diagnostics):
        with (self.output / 'update.log').open('a', encoding='utf-8') as log:
            for diagnostic in diagnostics:
                log.write(json.dumps(diagnostic, ensure_ascii=False, default=repr) + '\n')

    def save(self, receipt):
        (self.output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')

    def preflight(self):
        from camctl.bootstrap.application import ConfigAdapter
        from camctl.session.locks import lock_file_paths
        verify_checksums(self.bundle)
        arch = current_target()
        release = json.loads((self.bundle / 'RELEASE.json').read_text(encoding='utf-8'))
        expected_targets = wheel_target_manifest(self.bundle)
        if (release.get('wheel_targets') != expected_targets
                or any(not target['files'] for target in expected_targets.values())):
            raise ValueError('发行记录与离线依赖文件不一致')
        wheelhouse = self.bundle / 'camctl/wheelhouse' / arch
        if not wheelhouse.is_dir() or not list(wheelhouse.glob('*.whl')):
            raise ValueError('目标平台的离线依赖缺失: ' + str(wheelhouse))
        wheels = list((self.bundle / 'camctl').glob('camctl-*.whl'))
        if len(wheels) != 1 or not (self.bundle / 'camctl/requirements.txt').is_file():
            raise ValueError('camctl wheel 或锁定依赖文件缺失')
        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()
            metadata = [name for name in names if name.endswith('.dist-info/METADATA')]
            if len(metadata) != 1 or 'camctl/deployment.py' not in names:
                raise ValueError('camctl wheel 缺少发行元信息或只读检查入口')
            wheel_info = BytesParser().parsebytes(archive.read(metadata[0]))
            if (wheel_info['Name'] != 'camctl'
                    or wheel_info['Version'] != release['components']['camctl']):
                raise ValueError('camctl wheel 版本与发行记录不一致')
        if not self.config_path.is_file():
            raise ValueError('原配置不存在: ' + str(self.config_path))
        config = ConfigAdapter(Path.home()).load(self.config_path)
        state_db = Path(config.paths.state_db).resolve()
        if not state_db.is_file():
            raise ValueError('原状态库不存在或不是普通文件: ' + str(state_db))
        self.config, self.state_db, self.wheelhouse, self.wheel = config, state_db, wheelhouse, wheels[0]
        self.expected_version = release['components']['camctl']
        return {'state_db': str(state_db), 'config': str(self.config_path),
                'lock_paths': list(map(str, lock_file_paths(state_db))),
                'config_sha256': hashlib.sha256(self.config_path.read_bytes()).hexdigest(),
                'old_version': importlib.metadata.version('camctl'), 'new_version': self.expected_version,
                'release_source_sha256': release['source_sha256'],
                'bundle_manifest_sha256': hashlib.sha256((self.bundle / 'SHA256SUMS').read_bytes()).hexdigest()}

    def lock(self, original):
        self.db_locks.database(original['lock_paths'])

    def unlock(self):
        errors = []
        for locks in (self.db_locks, self.update_locks):
            try:
                locks.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup('释放更新锁失败', errors)

    def check_original(self, original):
        from camctl.persistence.runtime import (CURRENT_FORMAT_VERSION, _FORMAT_VERSIONS, DbConfig,
                                                 DbOpenMode, open_existing, verify_directory_binding)
        owned = open_existing(self.state_db, DbOpenMode.EXISTING_RO,
                              DbConfig(busy_timeout_ms=self.config.database.busy_timeout_ms))
        try:
            verify_directory_binding(owned.metadata, self.config.paths.staging, self.config.paths.ready,
                                     self.config.paths.processing)
            if not int(_FORMAT_VERSIONS.V9) <= owned.metadata.format_version <= CURRENT_FORMAT_VERSION:
                raise ValueError('原状态库格式超出更新维护范围')
            original['deployment'] = {'state_db': str(self.state_db), 'metadata': asdict(owned.metadata)}
        finally:
            owned.connection.close()

    def backup(self, receipt):
        original = receipt['original']
        env_backup = self.output / 'venv.backup'
        db_backup = self.output / 'state.backup.db'
        self.record = {'state': 'preparing', 'mutation_intent': False,
                       'env': str(self.target / 'venv'), 'env_backup': str(env_backup),
                       'state_db': str(self.state_db), 'db_backup': str(db_backup),
                       'lock_paths': original['lock_paths'],
                       'original': original, 'controller': sys.executable,
                       'output': str(self.output)}
        transaction.atomic_json(self.pending, self.record)
        self.record['env_manifest'] = transaction.backup_environment(self.target / 'venv', env_backup)
        transaction.backup_database(self.state_db, db_backup)
        self.record['db_sha256'] = transaction.digest(db_backup)
        probe = (self.target / 'venv').with_name('venv.' + self.output.name + '.probe')
        transaction.copy_environment(env_backup, probe)
        if transaction.manifest(probe, sync=True) != self.record['env_manifest']:
            raise ValueError('无法保留恢复环境的元信息')
        shutil.rmtree(probe)
        transaction.sync_dir(probe.parent)
        probe_db = self.state_db.with_name(self.state_db.name + '.' + self.output.name + '.probe')
        transaction.replace_file(db_backup, probe_db)
        probe_db.unlink()
        transaction.sync_dir(probe_db.parent)
        if self.client_data is not None:
            configured_path = self.client_data / 'device-capabilities.json'
            if configured_path.is_symlink() and not configured_path.exists():
                raise ValueError('能力文件符号链接目标不存在')
            path = configured_path.resolve()
            item = {'path': str(path), 'configured_path': str(configured_path), 'present': path.is_file()}
            if path.exists() and not item['present']:
                raise ValueError('原能力文件不是普通文件: ' + str(path))
            if item['present']:
                backup = self.output / 'client-capabilities.backup'
                shutil.copy2(path, backup)
                info = path.stat()
                os.chown(backup, info.st_uid, info.st_gid)
                shutil.copystat(path, backup)
                transaction.sync_file(backup)
                transaction.sync_dir(backup.parent)
                item.update(backup=str(backup), sha256=transaction.digest(backup))
                probe_client = path.with_name(path.name + '.' + self.output.name + '.probe')
                transaction.replace_file(backup, probe_client)
                probe_client.unlink()
                transaction.sync_dir(probe_client.parent)
            self.record['client'] = item

    def mark_intent(self):
        self.record.update(state='updating', mutation_intent=True)
        transaction.atomic_json(self.pending, self.record)

    def rollback(self):
        # 重取资格才能证明继承锁的写入者已全部结束。
        self.db_locks.close()
        self.update_locks.close()
        self.update_locks.acquire(self.target / 'updates/update.lock')
        self.db_locks.database(self.record['lock_paths'])
        current = json.loads(self.pending.read_text(encoding='utf-8'))
        if current.get('output') != str(self.output) or current.get('state') != 'updating':
            raise ValueError('更新记录已经变化，不能按旧记录回滚')
        transaction.recover(self.record)
        self.record['state'] = 'rolled_back'
        transaction.atomic_json(self.pending, self.record)

    def commit(self):
        self.record['state'] = 'committed'
        transaction.atomic_json(self.pending, self.record)

    def migrate(self, receipt):
        copy = self.output / 'state.migrate.db'
        shutil.copy2(self.record['db_backup'], copy)
        info = Path(self.record['db_backup']).stat()
        os.chown(copy, info.st_uid, info.st_gid)
        shutil.copystat(self.record['db_backup'], copy)
        output = self.run(receipt, '迁移数据库副本', [self.python, '-m', 'camctl.deployment',
                          '--config', self.config_path, '--migrate-copy', copy])
        metadata = json.loads(output)
        original = receipt['original']['deployment']['metadata']
        if {k: v for k, v in metadata.items() if k != 'format_version'} != {
                k: v for k, v in original.items() if k != 'format_version'}:
            raise ValueError('迁移后数据库身份或目录绑定不一致')
        connection = sqlite3.connect(copy)
        try:
            busy, _, _ = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if busy:
                raise ValueError('数据库副本 WAL 无法完成检查点')
        finally:
            connection.close()
        transaction.sync_file(copy)
        for suffix in ('-wal', '-shm'):
            sidecar = Path(str(self.state_db) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        transaction.sync_dir(self.state_db.parent)
        transaction.replace_file(copy, self.state_db)
        receipt['new_deployment'] = {'state_db': str(self.state_db), 'metadata': metadata}

    def sync_new(self):
        transaction.manifest(self.target / 'venv', sync=True)
        transaction.sync_dir(self.target)
        transaction.sync_file(self.state_db)
        transaction.sync_dir(self.state_db.parent)
        if self.client_data is not None:
            path = self.client_data / 'device-capabilities.json'
            transaction.sync_file(path)
            transaction.sync_dir(path.resolve().parent)
            transaction.sync_dir(path.parent)

    def run(self, receipt, label, argv, output=None):
        command = list(map(str, argv))
        lock_fds = (*self.update_locks.fds, *self.db_locks.fds)
        worker = [sys.executable, str(self.bundle / 'update-worker.py')]
        for fd in lock_fds:
            worker.extend(('--lock-fd', str(fd)))
        worker.extend(('--', *command))
        capture = self.output / ('command-' + str(len(receipt['steps'])) + '.stdout')
        with (self.output / 'update.log').open('ab') as log, capture.open('wb') as stdout:
            log.write(('\n$ ' + shlex.join(command) + '\n').encode()); log.flush()
            result = subprocess.run(worker, stdout=stdout, stderr=log, env=self.environment,
                                    pass_fds=lock_fds)
        output_bytes = capture.read_bytes()
        with (self.output / 'update.log').open('ab') as log:
            log.write(output_bytes)
        receipt['steps'].append({'label': label, 'argv': command, 'returncode': result.returncode})
        self.save(receipt)
        if result.returncode:
            raise RuntimeError(label + '失败，详见 update.log')
        if output is not None:
            output.write_bytes(output_bytes)
        return output_bytes.decode('utf-8')

    def install(self, receipt):
        common = ['uv', 'pip', 'install', '--python', self.python, '--offline', '--no-index',
                  '--find-links', self.wheelhouse]
        self.run(receipt, '离线安装锁定依赖', [*common, '--require-hashes', '-r', self.bundle / 'camctl/requirements.txt'])
        self.run(receipt, '离线安装 camctl', [*common, '--no-deps', '--reinstall-package', 'camctl', self.wheel])
        self.run(receipt, '检查依赖', ['uv', 'pip', 'check', '--python', self.python])

    def check_new(self, receipt):
        original = receipt['original']
        version = self.run(receipt, '检查发行版本', [self.target / 'venv/bin/camctl', '--version'])
        if version != self.expected_version + '\n':
            raise ValueError('发行版本不符: ' + repr(version))
        text = self.run(receipt, '只读检查原部署', [self.python, '-m', 'camctl.deployment', '--config', self.config_path])
        if json.loads(text) != receipt['new_deployment']:
            raise ValueError('状态库身份、格式或路径绑定与更新前不一致')
        if hashlib.sha256(self.config_path.read_bytes()).hexdigest() != original['config_sha256']:
            raise ValueError('原配置内容已变化')
        capability = self.output / 'device-capabilities.json'
        self.run(receipt, '导出原配置能力说明', [self.target / 'venv/bin/camctl', 'describe', '--config', self.config_path], output=capability)
        if self.client_data is not None:
            transaction.make_directories(self.client_data)
            shutil.copyfile(capability, self.client_data / 'device-capabilities.json')
            receipt['client_capabilities'] = str(self.client_data / 'device-capabilities.json')
        receipt['capabilities'] = str(capability)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--client-data', type=Path)
    parser.add_argument('--config', type=absolute_path, help='原配置文件的绝对路径；默认 <target>/config.toml')
    args = parser.parse_args()
    if not args.target.is_absolute() or not args.target.is_dir():
        parser.error('--target 必须是已存在的绝对目录')
    if args.client_data is not None and not args.client_data.is_absolute():
        parser.error('--client-data 必须是绝对路径')
    args.target = Path(os.path.abspath(args.target))
    if (sys.version_info[:2] != (3, 11) or sys.implementation.name != 'cpython'
            or sys.prefix != sys.base_prefix):
        parser.error('更新入口必须由虚拟环境外的 CPython 3.11 启动')
    updates = args.target / 'updates'
    updates.mkdir(parents=True, exist_ok=True)
    transaction.sync_dir(args.target)
    output = args.target / 'updates' / uuid.uuid4().hex
    output.mkdir(parents=True)
    (output / 'update.log').touch()
    operations = UpdateOperations(Path(__file__).resolve().parent, args.target, args.client_data, output,
                                  config_path=args.config)
    stage = 'update_lock'
    try:
        operations.update_locks.acquire(updates / 'update.lock')
        if operations.pending.exists():
            stage = 'recover_pending'
            record = json.loads(operations.pending.read_text(encoding='utf-8'))
            if record['state'] in ('committed', 'rolled_back'):
                transaction.sync_file(operations.pending)
                transaction.sync_dir(operations.pending.parent)
            if record['state'] not in ('committed', 'rolled_back'):
                operations.db_locks.database(record['lock_paths'])
                recovery_error = None
                try:
                    transaction.recover(record)
                    record['state'] = 'rolled_back'
                    transaction.atomic_json(operations.pending, record)
                except Exception as error:
                    recovery_error = error
                try:
                    operations.db_locks.close()
                except Exception as close_error:
                    if recovery_error is not None:
                        raise ExceptionGroup('恢复与释放数据库锁均失败',
                                             [recovery_error, close_error])
                    raise
                if recovery_error is not None:
                    raise recovery_error
            transaction.atomic_json(Path(record['output']) / 'journal.json', record)
        stage = 'environment'
        python = args.target / 'venv/bin/python'
        if not python.is_file() or shutil.which('uv') is None:
            raise ValueError('原 Python 环境或 uv 缺失')
        sys.path.insert(0, str(args.target / 'venv/lib/python3.11/site-packages'))
        receipt = perform_update(operations)
    except Exception as error:
        errors = [(error, stage)]
        try:
            operations.unlock()
        except Exception as unlock_error:
            errors.append((unlock_error, 'unlock'))
        receipt = {'status': 'failed', 'diagnostics': [freeze_exception(item, label)
                                                        for item, label in errors]}
        deliver_diagnostics(operations, receipt, receipt['diagnostics'])
        try:
            operations.save(receipt)
        except Exception as save_error:
            failure = freeze_exception(save_error, 'save_final_receipt')
            deliver_diagnostics(operations, receipt, [failure])
        return 1
    print('更新收据和日志目录：' + str(output))
    if receipt['status'] != 'updated_stopped':
        if receipt.get('rollback') == 'verified':
            consequence = '原环境和数据库已恢复'
        elif receipt.get('rollback') == 'commit_unknown':
            consequence = '完成记录结果尚未确认，请重新调用更新入口处理'
        else:
            consequence = ('Python 环境可能已改变' if receipt['environment_may_have_changed']
                           else '尚未开始修改 Python 环境')
        reason = receipt.get('error') or receipt.get('unlock_error') or receipt.get('receipt_error')
        print('更新失败：' + str(reason) +
              '；' + consequence + '，请查看收据。', file=sys.stderr)
        return 1
    print('更新完成，host 保持停止；请按原入口恢复。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
