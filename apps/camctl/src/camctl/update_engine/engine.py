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
import io
import importlib.metadata
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import stat
import subprocess
import traceback
import uuid
import zipfile

from bundle_support import WHEEL_TARGETS, current_target, update_metadata, verify_checksums, wheel_target_manifest
from archive_support import UpdateArchive
from maintenance_integrity import verify_maintenance_integrity
from error_diagnostics import exception_diagnostic
from diagnostic_fallback import freeze


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute() or "\0" in value:
        raise argparse.ArgumentTypeError('路径必须是不含空字符的绝对路径')
    return path

import update_transaction as transaction
from config_paths import log_registration_id, rotated_log_names


def freeze_registered_logs(target, destination):
    registry = target / 'maintenance/logs'
    destination.mkdir(mode=0o700)
    transaction.sync_dir(destination.parent)
    try:
        entries = sorted(registry.iterdir())
    except FileNotFoundError:
        entries = []
    manifest = []
    for entry in entries:
        info = entry.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('日志位置登记不是普通文件: ' + str(entry))
        item = json.loads(entry.read_text(encoding='utf-8'))
        if (not isinstance(item, dict) or set(item) != {'path', 'config_source'} or
                not isinstance(item['path'], str) or not Path(item['path']).is_absolute() or
                entry.name != log_registration_id(item['path']) + '.json' or
                (item['config_source'] is not None and
                 (not isinstance(item['config_source'], str) or not Path(item['config_source']).is_absolute()))):
            raise ValueError('日志位置登记无效: ' + str(entry))
        path = Path(item['path'])
        try:
            rotations = [name for _, name in rotated_log_names(path)]
        except FileNotFoundError:
            rotations = []
        files = []
        for index, source in enumerate([path, *rotations]):
            try:
                source_info = source.lstat()
            except FileNotFoundError:
                files.append({'path': str(source), 'present': False})
                continue
            if not stat.S_ISREG(source_info.st_mode):
                raise ValueError('日志来源不是普通文件: ' + str(source))
            copy = destination / (entry.stem + '-' + str(index))
            shutil.copy2(source, copy)
            transaction.sync_file(copy)
            files.append({'path': str(source), 'present': True, 'copy': copy.name,
                          'sha256': transaction.digest(copy)})
        manifest.append({'registration': item, 'files': files})
    transaction.sync_dir(destination)
    return manifest


def freeze_exception(error, stage):
    return freeze(error, stage, exception_diagnostic)


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


def perform_update(ops, original=None):
    """冻结错误事实，先释放锁，再交付诊断和最终收据。"""
    receipt = {'status': 'checking', 'steps': [], 'environment_may_have_changed': False,
               'diagnostics': []}
    locked = True
    intent = False
    committed = False
    failures = []
    stage = 'preflight'
    try:
        receipt['original'] = ops.preflight() if original is None else original
        stage = 'save_original'
        ops.save(receipt)
        stage = 'lock'
        ops.lock(receipt['original'])
        locked = True
        stage = 'check_original'
        ops.check_paths(receipt['original'], recovery=True)
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
        stage = 'check_final'
        ops.check_final(receipt)
        stage = 'sync_new'
        ops.sync_new()
        stage = 'commit'
        ops.commit()
        committed = True
        receipt['status'] = 'updated_stopped'
    except Exception as error:
        receipt.update(status='failed', error=f'{type(error).__name__}: {error}')
        failures.append((error, stage))
        if stage in ('commit', 'save_install_intent'):
            try:
                actual = ops.confirm_pending()
                if stage == 'commit' and actual == 'committed':
                    committed = True
                    receipt['status'] = 'updated_stopped'
                elif actual == 'updating':
                    intent = True
                elif stage == 'save_install_intent' and actual == 'preparing':
                    intent = False
                else:
                    raise ValueError('持久更新记录与操作阶段不一致')
            except Exception as confirm_error:
                receipt['rollback'] = 'commit_unknown'
                failures.append((confirm_error, 'confirm_pending'))
                intent = False
        if intent and not committed:
            try:
                warning = ops.rollback()
                receipt['rollback'] = 'verified'
                if isinstance(warning, BaseException):
                    failures.append((warning, 'save_rollback'))
            except Exception as rollback_error:
                receipt['rollback'] = 'failed'
                failures.append((rollback_error, 'rollback'))
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
        self.bundle_input, self.target, self.client_data, self.output = bundle, target, client_data, output
        self.archive_input = bundle is not None and not bundle.is_dir()
        self.bundle = output / 'package' if self.archive_input else bundle
        self.python = target / 'venv/bin/python'
        self.config_path = config_path
        self.config_source = config_path
        self.environment = {k: v for k, v in os.environ.items() if k not in
                            ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
        self.update_locks = transaction.Locks()
        self.db_locks = transaction.Locks()
        self.pending = target / 'updates/pending.json'
        self.engine_path = output / 'engine.pyz'

    def record_diagnostics(self, diagnostics):
        with (self.output / 'update.log').open('a', encoding='utf-8') as log:
            for diagnostic in diagnostics:
                log.write(json.dumps(diagnostic, ensure_ascii=False, default=repr) + '\n')

    def save(self, receipt):
        (self.output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')

    def save_recovery_failure(self, receipt):
        path = self.output / ('recovery-' + uuid.uuid4().hex + '.json')
        with path.open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write('\n')

    def preflight_package(self):
        arch = current_target()
        if self.archive_input:
            with UpdateArchive(self.bundle_input) as bundle:
                bundle.verify()
                release = json.loads(bundle.read('RELEASE.json'))
                expected_targets = bundle.wheel_targets(WHEEL_TARGETS)
                wheel_name = bundle.wheel_name()
                if 'camctl/requirements.txt' not in bundle.members:
                    raise ValueError('更新 ZIP 缺少锁定依赖文件')
                wheel_source = io.BytesIO(bundle.read(wheel_name))
                manifest_bytes = bundle.read('SHA256SUMS')
            wheel = self.bundle.joinpath(*Path(wheel_name).parts)
        else:
            verify_checksums(self.bundle)
            release = json.loads((self.bundle / 'RELEASE.json').read_text(encoding='utf-8'))
            expected_targets = wheel_target_manifest(self.bundle)
            wheels = list((self.bundle / 'camctl').glob('camctl-*.whl'))
            if len(wheels) != 1 or not (self.bundle / 'camctl/requirements.txt').is_file():
                raise ValueError('camctl wheel 或锁定依赖文件缺失')
            wheel_source = wheel = wheels[0]
            manifest_bytes = (self.bundle / 'SHA256SUMS').read_bytes()
        self.update = update_metadata(release)
        if (release.get('wheel_targets') != expected_targets
                or any(not target['files'] for target in expected_targets.values())
                or not expected_targets[arch]['files']):
            raise ValueError('发行记录与离线依赖文件不一致')
        wheelhouse = self.bundle / 'camctl/wheelhouse' / arch
        with zipfile.ZipFile(wheel_source) as archive:
            names = archive.namelist()
            metadata = [name for name in names if name.endswith('.dist-info/METADATA')]
            integrity_name = 'camctl/persistence/maintenance_integrity.py'
            if (len(metadata) != 1 or 'camctl/deployment.py' not in names
                    or integrity_name not in names or 'camctl/update_engine.pyz' not in names):
                raise ValueError('camctl wheel 缺少发行元信息或维护入口')
            engine_bytes = Path(sys.argv[0]).read_bytes()
            if archive.read('camctl/update_engine.pyz') != engine_bytes:
                raise ValueError('运行中的维护引擎与 wheel 不一致')
            with zipfile.ZipFile(io.BytesIO(engine_bytes)) as engine_archive:
                engine_integrity = engine_archive.read('maintenance_integrity.py')
            if archive.read(integrity_name) != engine_integrity:
                raise ValueError('部署检查入口与 wheel 内的维护检查不一致')
            wheel_info = BytesParser().parsebytes(archive.read(metadata[0]))
            if (wheel_info['Name'] != 'camctl'
                    or wheel_info['Version'] != release['components']['camctl']):
                raise ValueError('camctl wheel 版本与发行记录不一致')
        self.wheelhouse, self.wheel = wheelhouse, wheel
        self.expected_version = release['components']['camctl']
        return {'new_version': self.expected_version, 'release_source_sha256': release['source_sha256'],
                'bundle_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest()}

    def preflight(self):
        package = self.preflight_package()
        if self.update['force']:
            raise ValueError('强制更新事务尚未接入')
        config = self.probe('config')
        self.config_path = Path(config['config'])
        state_db = Path(config['state_db'])
        if not state_db.is_file():
            raise ValueError('原状态库不存在或不是普通文件: ' + str(state_db))
        self.config, self.state_db = config, state_db
        return {'state_db': str(state_db), 'config': str(self.config_path),
                'config_snapshot': transaction.snapshot_config(self.config_path, self.config_source),
                'lock_paths': config['lock_paths'],
                'old_version': config['old_version'], **package}

    def prepare_package(self, original):
        if not self.archive_input:
            return
        with UpdateArchive(self.bundle_input) as bundle:
            bundle.verify()
            if hashlib.sha256(bundle.read('SHA256SUMS')).hexdigest() != original['bundle_manifest_sha256']:
                raise ValueError('更新 ZIP 在准备过程中发生变化')
            bundle.extract(self.bundle)
        verify_checksums(self.bundle)
        if hashlib.sha256((self.bundle / 'SHA256SUMS').read_bytes()).hexdigest() != original['bundle_manifest_sha256']:
            raise ValueError('事务目录内的更新包无法核实')

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

    def probe(self, mode):
        # 新引擎不导入原环境；在原解释器中独立读取旧配置和运行时代码。
        with zipfile.ZipFile(Path(sys.argv[0])) as archive:
            source = archive.read('probe.py').decode('utf-8')
        result = subprocess.run([str(self.python), '-B', '-I', '-c', source, str(self.config_source) if self.config_source is not None else '', mode],
                                capture_output=True, env=self.environment, check=False)
        if result.returncode:
            raise RuntimeError('原环境只读检查失败: ' + result.stderr.decode('utf-8', errors='replace'))
        return json.loads(result.stdout)

    def check_original(self, original):
        result = self.probe('inspect')
        if (result['config'] != original['config'] or result['state_db'] != original['state_db'] or result['lock_paths'] != original['lock_paths']
                or result['old_version'] != original['old_version']):
            raise ValueError('原环境检查身份发生变化')
        transaction.verify_config(original)
        original['deployment'] = result['deployment']

    def check_paths(self, original, *, before_lock=False, recovery=False, extra_control_files=()):
        transaction.verify_config(original)
        temporary = None
        if recovery and hasattr(self, 'record'):
            metadata = self.record['original']['deployment']['metadata']
            bindings = [metadata['staging_path'], metadata['ready_path'], metadata['processing_path']]
            temporary = self.record['temporary']
        else:
            bindings = [self.config['paths'][name] for name in ('staging', 'ready', 'processing')]
        return transaction.validate_paths(self.target, original, self.client_data, self.output, bindings,
                                          before_lock=before_lock, recovery=recovery, temporary=temporary,
                                          extra_control_files=extra_control_files,
                                          extra_input_paths=(self.bundle_input,) if self.bundle_input else ())

    def backup(self, receipt):
        original = receipt['original']
        env_backup = self.output / 'venv.backup'
        db_backup = self.output / 'state.backup.db'
        client_path = ((self.client_data / 'device-capabilities.json').resolve()
                       if self.client_data is not None else None)
        temporary = transaction.temporary_paths(self.target, self.output, self.state_db, client_path)
        self.record = {'state': 'preparing', 'mutation_intent': False,
                       'env': str(self.target / 'venv'), 'env_backup': str(env_backup),
                       'state_db': str(self.state_db), 'db_backup': str(db_backup),
                       'lock_paths': original['lock_paths'],
                       'original': original, 'controller': sys.executable,
                       'engine': {'path': str(self.engine_path), 'sha256': transaction.digest(self.engine_path)},
                       'output': str(self.output), 'output_identity': transaction.directory_identity(self.output),
                       'temporary': temporary,
                       'temporary_owners': {name: [] for name in ('namespace', 'staging', 'db_temp', 'client_temp')}}
        if self.client_data is not None:
            configured_path = self.client_data / 'device-capabilities.json'
            if configured_path.is_symlink() and not configured_path.exists():
                raise ValueError('能力文件符号链接目标不存在')
            path = configured_path.resolve()
            item = {'path': str(path), 'configured_path': str(configured_path), 'present': path.is_file()}
            if path.exists() and not item['present']:
                raise ValueError('原能力文件不是普通文件: ' + str(path))
            self.record['client'] = item
        transaction.atomic_json(self.pending, self.record)
        namespace = Path(temporary['env_namespace'])
        transaction.owned_namespace(namespace, self.record, self.register_owner)
        self.record['env_manifest'] = transaction.backup_environment(self.target / 'venv', env_backup)
        self.record['db_meta'] = transaction.file_metadata(self.state_db)
        transaction.backup_database(self.state_db, db_backup)
        self.record['db_sha256'] = transaction.digest(db_backup)
        transaction.assert_file(db_backup, self.record['db_sha256'], self.record['db_meta'])
        transaction.verify_database_snapshot(db_backup, original['deployment']['metadata'],
                                             verify_maintenance_integrity)
        transaction.assert_file(db_backup, self.record['db_sha256'], self.record['db_meta'])
        probe = namespace / 'staging'
        self.register_owner('staging', namespace)
        transaction.copy_environment(env_backup, probe)
        if transaction.manifest(probe, sync=True) != self.record['env_manifest']:
            raise ValueError('无法保留恢复环境的元信息')
        shutil.rmtree(probe)
        transaction.sync_dir(probe.parent)
        probe_db = Path(temporary['db_probe'])
        transaction.replace_file(db_backup, probe_db, Path(temporary['db_temp']),
                                 self.record, 'db_temp', self.register_owner)
        transaction.assert_file(probe_db, self.record['db_sha256'], self.record['db_meta'])
        probe_db.unlink()
        transaction.sync_dir(probe_db.parent)
        if self.client_data is not None:
            item = self.record['client']
            path = Path(item['path'])
            if item['present']:
                backup = self.output / 'client-capabilities.backup'
                shutil.copy2(path, backup)
                info = path.stat()
                item['meta'] = transaction.file_metadata(path)
                os.chown(backup, info.st_uid, info.st_gid)
                shutil.copystat(path, backup)
                transaction.sync_file(backup)
                transaction.sync_dir(backup.parent)
                item.update(backup=str(backup), sha256=transaction.digest(backup))
                transaction.assert_file(backup, item['sha256'], item['meta'])
                probe_client = Path(temporary['client_probe'])
                transaction.replace_file(backup, probe_client, Path(temporary['client_temp']),
                                         self.record, 'client_temp', self.register_owner)
                transaction.assert_file(probe_client, item['sha256'], item['meta'])
                probe_client.unlink()
                transaction.sync_dir(probe_client.parent)

    def mark_intent(self):
        self.record.update(state='updating', mutation_intent=True)
        transaction.atomic_json(self.pending, self.record)

    def rollback(self):
        # 重取资格才能证明继承锁的写入者已全部结束。
        self.db_locks.close()
        self.update_locks.close()
        self.update_locks.acquire_directory(self.target)
        self.db_locks.database(self.record['lock_paths'])
        current, state = self.read_pending()
        if state is not transaction.JournalState.UPDATING:
            raise ValueError('更新记录已经变化，不能按旧记录回滚')
        transaction.validate_active_record(current)
        metadata = current['original']['deployment']['metadata']
        bindings = [metadata['staging_path'], metadata['ready_path'], metadata['processing_path']]
        client = current.get('client')
        client_data = Path(client['configured_path']).parent if client is not None else None
        transaction.validate_paths(self.target, current['original'], client_data, self.output, bindings,
                                   recovery=True, temporary=current['temporary'])
        self.record = current
        transaction.recover(current, verify_maintenance_integrity, self.register_owner)
        current['state'] = transaction.JournalState.ROLLED_BACK.value
        try:
            transaction.atomic_json(self.pending, current)
        except Exception as error:
            if self.confirm_pending() != transaction.JournalState.ROLLED_BACK.value:
                raise
            self.record = current
            return error
        self.record = current

    def read_pending(self):
        record = json.loads(self.pending.read_text(encoding='utf-8'))
        state = transaction.classify_record(record)
        transaction.validate_record_target(record, self.target)
        if transaction.directory_identity(Path(record['output'])) != record['output_identity']:
            raise ValueError('更新输出目录身份已变化')
        for key in ('output', 'output_identity', 'env', 'env_backup', 'state_db', 'db_backup', 'lock_paths',
                    'controller', 'original', 'temporary', 'engine'):
            if record[key] != self.record[key]:
                raise ValueError('持久更新记录身份不一致: ' + key)
        if state is not transaction.JournalState.PREPARING:
            if {k: v for k, v in record.items() if k not in ('state', 'temporary_owners')} != {
                    k: v for k, v in self.record.items() if k not in ('state', 'temporary_owners')}:
                raise ValueError('持久更新记录材料不一致')
        for kind, owners in self.record['temporary_owners'].items():
            if any(owner not in record['temporary_owners'][kind] for owner in owners):
                raise ValueError('持久更新记录缺少已确认创建身份: ' + kind)
        return record, state

    def confirm_pending(self):
        record, state = self.read_pending()
        transaction.sync_file(self.pending)
        transaction.sync_dir(self.pending.parent)
        self.record.clear()
        self.record.update(record)
        return state.value

    def register_owner(self, kind, parent):
        identity = transaction.creation_identity(parent)
        if identity in self.record['temporary_owners'][kind]:
            return
        proposed = dict(self.record)
        proposed['temporary_owners'] = {key: list(values) for key, values in
                                        self.record['temporary_owners'].items()}
        proposed['temporary_owners'][kind].append(identity)
        try:
            transaction.atomic_json(self.pending, proposed)
        except Exception as error:
            try:
                actual, _ = self.read_pending()
                transaction.sync_file(self.pending)
                transaction.sync_dir(self.pending.parent)
                self.record.clear()
                self.record.update(actual)
            except Exception as confirm_error:
                raise ExceptionGroup('创建身份发布及确认均失败', [error, confirm_error])
            raise
        self.record.clear()
        self.record.update(proposed)

    def commit(self):
        committed = dict(self.record, state=transaction.JournalState.COMMITTED.value)
        transaction.atomic_json(self.pending, committed)
        self.record = committed

    def config_args(self):
        return ['--config', self.config_source] if self.config_source is not None else []

    def migrate(self, receipt):
        copy = self.output / 'state.migrate.db'
        shutil.copy2(self.record['db_backup'], copy)
        info = Path(self.record['db_backup']).stat()
        os.chown(copy, info.st_uid, info.st_gid)
        shutil.copystat(self.record['db_backup'], copy)
        output = self.run(receipt, '迁移数据库副本', [self.python, '-m', 'camctl.deployment',
                          *self.config_args(), '--migrate-copy', copy])
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
        transaction.replace_file(copy, self.state_db, Path(self.record['temporary']['db_temp']),
                                 self.record, 'db_temp', self.register_owner)
        if transaction.file_metadata(self.state_db) != self.record['db_meta']:
            raise ValueError('发布后的数据库元信息发生变化')
        receipt['new_deployment'] = {'state_db': str(self.state_db), 'metadata': metadata}

    def check_final(self, receipt):
        original = receipt['original']
        self.check_paths(original, recovery=True)
        version = self.run(receipt, '最终检查发行版本', [self.target / 'venv/bin/camctl', '--version'])
        if version != self.expected_version + '\n':
            raise ValueError('最终发行版本不符')
        result = self.run(receipt, '最终检查状态库', [self.python, '-m', 'camctl.deployment',
                          *self.config_args()])
        if json.loads(result) != receipt['new_deployment']:
            raise ValueError('最终数据库身份不一致')
        transaction.verify_config(original)
        if transaction.file_metadata(self.state_db) != self.record['db_meta']:
            raise ValueError('最终数据库元信息不一致')
        if self.client_data is not None:
            client = self.record['client']
            path = Path(client['configured_path'])
            if path.resolve() != Path(client['path']):
                raise ValueError('能力文件路径已变化')
            if transaction.digest(path) != transaction.digest(self.output / 'device-capabilities.json'):
                raise ValueError('能力文件内容不一致')
            if client['present'] and transaction.file_metadata(path) != client['meta']:
                raise ValueError('能力文件元信息不一致')

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
        worker = [sys.executable, str(self.engine_path), '--worker']
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
        text = self.run(receipt, '只读检查原部署', [self.python, '-m', 'camctl.deployment', *self.config_args()])
        if json.loads(text) != receipt['new_deployment']:
            raise ValueError('状态库身份、格式或路径绑定与更新前不一致')
        transaction.verify_config(original)
        capability = self.output / 'device-capabilities.json'
        self.run(receipt, '导出原配置能力说明', [self.target / 'venv/bin/camctl', 'describe', *self.config_args()], output=capability)
        if self.client_data is not None:
            transaction.make_directories(self.client_data)
            shutil.copyfile(capability, self.client_data / 'device-capabilities.json')
            receipt['client_capabilities'] = str(self.client_data / 'device-capabilities.json')
        receipt['capabilities'] = str(capability)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--client-data', type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--recover-only', action='store_true')
    parser.add_argument('--lock-fd', required=True, type=int)
    parser.add_argument('--config', type=absolute_path, help='原配置文件的绝对路径；省略时使用普通入口的默认来源')
    args = parser.parse_args()
    if not args.target.is_absolute() or not args.target.is_dir():
        parser.error('--target 必须是已存在的绝对目录')
    if args.client_data is not None and not args.client_data.is_absolute():
        parser.error('--client-data 必须是绝对路径')
    args.target = Path(os.path.abspath(args.target)).resolve()
    if (sys.version_info[:2] != (3, 11) or sys.implementation.name != 'cpython'
            or sys.prefix != sys.base_prefix):
        parser.error('更新入口必须由虚拟环境外的 CPython 3.11 启动')
    updates = args.target / 'updates'
    pending = updates / 'pending.json'
    target_locks = transaction.Locks()
    target_locks.fds.append(args.lock_fd)
    operations = None
    evidence = None
    archive_record = None
    stage = 'update_lock'
    try:
        if args.lock_fd < 0:
            raise ValueError('维护资格描述符无效')
        try:
            pending_info = pending.lstat()
        except FileNotFoundError:
            pending_info = None
        if pending_info is not None:
            import stat
            if not stat.S_ISREG(pending_info.st_mode):
                raise ValueError('更新记录不是普通文件')
            stage = 'recover_pending'
            record = json.loads(pending.read_text(encoding='utf-8'))
            state = transaction.classify_record(record)
            transaction.validate_record_target(record, args.target)
            if transaction.directory_identity(Path(record['output'])) != record['output_identity']:
                raise ValueError('更新输出目录身份已变化')
            if state in (transaction.JournalState.COMMITTED, transaction.JournalState.ROLLED_BACK):
                transaction.sync_file(pending)
                transaction.sync_dir(pending.parent)
            else:
                if not args.recover_only:
                    raise ValueError('必须先使用冻结引擎恢复未完成事务')
                transaction.validate_active_record(record)
                original = record['original']
                metadata = original['deployment']['metadata']
                bindings = [metadata['staging_path'], metadata['ready_path'], metadata['processing_path']]
                client = record.get('client')
                client_data = Path(client['configured_path']).parent if client is not None else None
                transaction.validate_paths(args.target, original, client_data, Path(record['output']), bindings,
                                           before_lock=True, recovery=True, temporary=record['temporary'])
                evidence = Path(record['output'])
                operations = UpdateOperations(None, args.target, None, evidence)
                operations.update_locks = target_locks
                operations.record = record
                operations.db_locks.database(record['lock_paths'])
                recovery_error = None
                try:
                    transaction.validate_paths(args.target, original, client_data, evidence, bindings,
                                               recovery=True, temporary=record['temporary'])
                    transaction.recover(record, verify_maintenance_integrity, operations.register_owner)
                    record['state'] = transaction.JournalState.ROLLED_BACK.value
                    transaction.atomic_json(pending, record)
                except Exception as error:
                    recovery_error = error
                try:
                    operations.db_locks.close()
                except Exception as close_error:
                    if recovery_error is not None:
                        raise ExceptionGroup('恢复与释放数据库锁均失败', [recovery_error, close_error])
                    raise
                if recovery_error is not None:
                    raise recovery_error
            archive_record = (record, Path(record['output']) / 'journal.json')
            try:
                archive_info = archive_record[1].lstat()
            except FileNotFoundError:
                archive_info = None
            if archive_info is not None:
                import stat
                if (not stat.S_ISREG(archive_info.st_mode)
                        or json.loads(archive_record[1].read_text(encoding='utf-8')) != record):
                    raise ValueError('既有更新记录归档无法核实')
                archive_record = None
        if args.recover_only:
            target_locks.close()
            print(json.dumps({'status': 'recovered' if pending_info is not None and state not in (transaction.JournalState.COMMITTED, transaction.JournalState.ROLLED_BACK) else 'unchanged'}))
            return 0
        if args.bundle is None or args.output is None:
            raise ValueError('应用更新时必须提供交付目录和事务目录')
        if args.output.parent != updates:
            raise ValueError('事务目录与部署不一致')
        stage = 'environment'
        python = args.target / 'venv/bin/python'
        if not python.is_file() or shutil.which('uv') is None:
            raise ValueError('原 Python 环境或 uv 缺失')
        output = args.output
        evidence = None
        operations = UpdateOperations(args.bundle, args.target, args.client_data, output,
                                      config_path=args.config)
        operations.update_locks = target_locks
        stage = 'preflight'
        original = operations.preflight()
        stage = 'check_paths_before_effects'
        archive_files = (archive_record[1],) if archive_record is not None else ()
        operations.check_paths(original, before_lock=True, extra_control_files=archive_files)
        if archive_record is not None:
            record, archive = archive_record
            transaction.atomic_json(archive, record)
        # 在任何依赖引擎的记录发布之前，先同步冻结副本和其目录。
        updates.mkdir(parents=True, exist_ok=True)
        transaction.sync_dir(args.target)
        output.mkdir()
        transaction.sync_dir(updates)
        engine_bytes = Path(sys.argv[0]).read_bytes()
        with (output / 'engine.pyz').open('xb') as frozen:
            frozen.write(engine_bytes)
            frozen.flush()
            os.fsync(frozen.fileno())
        transaction.sync_dir(output)
        (output / 'update.log').touch(exist_ok=False)
        evidence = output
        stage = 'prepare_package'
        operations.prepare_package(original)
        receipt = perform_update(operations, original)
    except Exception as error:
        errors = [(error, stage)]
        try:
            if operations is not None:
                operations.unlock()
            else:
                target_locks.close()
        except Exception as unlock_error:
            errors.append((unlock_error, 'unlock'))
        receipt = {'status': 'failed', 'diagnostics': [freeze_exception(item, label)
                                                        for item, label in errors]}
        if evidence is not None and operations is not None:
            deliver_diagnostics(operations, receipt, receipt['diagnostics'])
        try:
            message = {'stage': stage, 'diagnostics': receipt['diagnostics'],
                       'evidence': str(evidence) if evidence is not None else None}
            sys.stderr.write(json.dumps(message, ensure_ascii=False, default=repr) + '\n')
            sys.stderr.flush()
        except Exception as stderr_error:
            receipt['outer_stderr_error'] = freeze_exception(stderr_error, 'outer_stderr')
            if evidence is not None and operations is not None:
                deliver_diagnostics(operations, receipt, [receipt['outer_stderr_error']])
        if evidence is not None and operations is not None:
            try:
                operations.save_recovery_failure(receipt)
            except Exception as save_error:
                failure = freeze_exception(save_error, 'save_final_receipt')
                deliver_diagnostics(operations, receipt, [failure])
        print(json.dumps({'status': 'failed',
                          'effect': 'recovery_required' if args.recover_only and stage == 'recover_pending' else 'unchanged',
                          'stage': stage, 'evidence': str(evidence) if evidence else None}))
        return 1

    if receipt['status'] == 'updated_stopped':
        effect = 'committed'
    elif receipt.get('rollback') == 'verified':
        effect = 'rolled_back'
    elif receipt.get('rollback') == 'failed':
        effect = 'recovery_required'
    elif receipt.get('rollback') == 'commit_unknown':
        effect = 'unknown'
    elif receipt['environment_may_have_changed']:
        effect = 'unknown'
    else:
        effect = 'unchanged'
    print(json.dumps({'status': receipt['status'], 'effect': effect, 'rollback': receipt.get('rollback'),
                      'evidence': str(output)}, ensure_ascii=False))
    return 0 if receipt['status'] == 'updated_stopped' else 1



if __name__ == '__main__':
    raise SystemExit(main())
