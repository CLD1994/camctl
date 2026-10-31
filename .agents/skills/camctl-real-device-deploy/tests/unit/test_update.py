"""更新入口在隔离 I/O 边界下的行为。"""
import builtins
import argparse
import contextlib
import linecache
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).absolute().parents[2] / 'scripts'
bundle_support = ModuleType('bundle_support')
bundle_support.current_target = Mock()
bundle_support.verify_checksums = Mock()
bundle_support.wheel_target_manifest = Mock()
deploy_support = ModuleType('deploy_support')
def absolute_path(value):
    path = Path(value)
    if not path.is_absolute() or '\0' in value:
        raise argparse.ArgumentTypeError('需要绝对路径')
    return path
deploy_support.absolute_path = absolute_path
transaction = ModuleType('update_transaction')
class Locks:
    def __init__(self):
        self.fds = []
    def acquire(self, path):
        raise AssertionError('未隔离的锁获取')
    def acquire_directory(self, path):
        raise AssertionError('未隔离的目录锁获取')
    def database(self, path):
        raise AssertionError('未隔离的数据库锁获取')
    def close(self):
        pass
transaction.Locks = Locks
transaction.sync_dir = Mock(side_effect=AssertionError('未隔离的目录同步'))
integrity = ModuleType('maintenance_integrity')
integrity.verify_maintenance_integrity = Mock()
spec = importlib.util.spec_from_file_location('update_camctl', SCRIPTS / 'update-camctl.py')
update = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'bundle_support': bundle_support, 'deploy_support': deploy_support,
                              'update_transaction': transaction, 'maintenance_integrity': integrity}), \
        patch.object(sys, 'dont_write_bytecode', True):
    spec.loader.exec_module(update)
encoder_spec = importlib.util.spec_from_file_location('error_diagnostics_under_test',
    Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/operations/error_diagnostics.py')
encoder = importlib.util.module_from_spec(encoder_spec)
encoder_spec.loader.exec_module(encoder)


@patch.object(Path, 'home', new=lambda: Path('/home/unit-user'))
class UpdateTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(sys.modules, {'camctl.operations.error_diagnostics': encoder})
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(update.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        random_id = patch.object(update.uuid, 'uuid4', return_value=SimpleNamespace(hex='update-id'))
        random_id.start()
        self.addCleanup(random_id.stop)
        paths = patch.object(Path, 'resolve', new=lambda path, *args, **kwargs: path)
        paths.start()
        self.addCleanup(paths.stop)

    def test_failed_diagnostic_channels_preserve_original_result_and_final_receipt(self):
        for failed_method in ('write', 'flush'):
            with self.subTest(failed_method=failed_method):
                ops = Mock()
                ops.preflight.return_value = {'state_db': '/state.db'}
                ops.install.side_effect = ValueError('original install failure')
                ops.record_diagnostics.side_effect = OSError('log unavailable')
                stream = Mock(spec=['write', 'flush'])
                getattr(stream, failed_method).side_effect = BrokenPipeError('stderr unavailable')
                with patch.object(sys, 'stderr', stream):
                    result = update.perform_update(ops)
                self.assertEqual(result['status'], 'failed')
                self.assertIn('original install failure', result['error'])
                self.assertEqual(result['stderr_error']['stage'], 'diagnostic_stderr')
                ops.unlock.assert_called_once()
                self.assertEqual(ops.mock_calls[-1][0], 'save')

    def test_install_failure_rolls_back_before_unlock_and_diagnostics(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.install.side_effect = ValueError('install failed')
        result = update.perform_update(ops)
        self.assertEqual(result['rollback'], 'verified')
        calls = [item[0] for item in ops.mock_calls]
        self.assertLess(calls.index('backup'), calls.index('mark_intent'))
        self.assertLess(calls.index('mark_intent'), calls.index('install'))
        self.assertLess(calls.index('rollback'), calls.index('unlock'))
        self.assertLess(calls.index('unlock'), calls.index('record_diagnostics'))

    def test_rollback_failure_preserves_both_errors(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.install.side_effect = ValueError('original failure')
        ops.rollback.side_effect = OSError(5, 'restore failure')
        result = update.perform_update(ops)
        self.assertEqual(result['rollback'], 'failed')
        self.assertEqual([item['stage'] for item in result['diagnostics']], ['install', 'rollback'])
        self.assertEqual(result['diagnostics'][1]['errno'], 5)

    def test_backup_failure_does_not_restore_or_install(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.backup.side_effect = OSError('backup failed')
        result = update.perform_update(ops)
        self.assertEqual(result['status'], 'failed')
        ops.mark_intent.assert_not_called()
        ops.install.assert_not_called()
        ops.rollback.assert_not_called()

    def test_commit_visible_and_synced_reports_success_with_original_error(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.commit.side_effect = OSError('commit result unknown')
        ops.confirm_pending.return_value = 'committed'
        result = update.perform_update(ops)
        self.assertEqual(result['status'], 'updated_stopped')
        self.assertEqual(result['diagnostics'][0]['stage'], 'commit')
        ops.rollback.assert_not_called()

    def test_commit_still_updating_is_rolled_back(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.commit.side_effect = OSError('commit failed')
        ops.confirm_pending.return_value = 'updating'
        result = update.perform_update(ops)
        self.assertEqual(result['rollback'], 'verified')
        ops.rollback.assert_called_once()

    def test_commit_cannot_be_confirmed_keeps_pending(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.commit.side_effect = OSError('commit failed')
        ops.confirm_pending.side_effect = OSError('sync failed')
        result = update.perform_update(ops)
        self.assertEqual(result['rollback'], 'commit_unknown')
        self.assertEqual([item['stage'] for item in result['diagnostics']], ['commit', 'confirm_pending'])
        ops.rollback.assert_not_called()

    def test_intent_write_failure_uses_persisted_state(self):
        for state, expected in [('preparing', False), ('updating', True)]:
            with self.subTest(state=state):
                ops = Mock()
                ops.preflight.return_value = {'state_db': '/state.db'}
                ops.mark_intent.side_effect = OSError('intent write failed')
                ops.confirm_pending.return_value = state
                update.perform_update(ops)
                self.assertEqual(ops.rollback.called, expected)
                ops.install.assert_not_called()

    def test_late_receipt_failure_does_not_reverse_committed_update(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.save.side_effect = [None, None, OSError('receipt failed')]
        result = update.perform_update(ops)
        self.assertEqual(result['status'], 'updated_stopped')
        self.assertEqual(result['diagnostics'][-1]['stage'], 'save_final_receipt')
        ops.rollback.assert_not_called()

    def test_config_path_defaults_to_target_or_uses_explicit_path(self):
        target = Path('/target')
        default = update.UpdateOperations(Path('/bundle'), target, None, Path('/output'))
        external = update.UpdateOperations(Path('/bundle'), target, None, Path('/output'),
                                           config_path=Path('/etc/camctl/config.toml'))
        self.assertEqual(default.config_path, Path('/target/config.toml'))
        self.assertEqual(external.config_path, Path('/etc/camctl/config.toml'))

    def test_external_config_selects_original_database_and_locks(self):
        config_path = Path('/etc/camctl/config.toml')
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'),
                                      config_path=config_path)
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/outside/state.db'))
        adapter = Mock()
        adapter.load.return_value = config
        application = ModuleType('camctl.bootstrap.application')
        application.ConfigAdapter = Mock(return_value=adapter)
        locks = ModuleType('camctl.session.locks')
        locks.lock_file_paths = Mock(return_value=(Path('/session.lock'), Path('/admission.lock')))
        archive = Mock()
        archive.namelist.return_value = ['camctl/deployment.py', 'camctl-2.dist-info/METADATA',
                                         'camctl/persistence/maintenance_integrity.py']
        archive.read.side_effect = lambda name: (b'Name: camctl\nVersion: 2.0\n\n'
                                                  if name.endswith('/METADATA') else b'original')
        release = {'components': {'camctl': '2.0'}, 'source_sha256': 'source',
                   'wheel_targets': {'x86_64': {'files': ['dependency.whl']}}}
        wheel = Path('/bundle/camctl/camctl-2.0.whl')
        with patch.dict(sys.modules, {'camctl.bootstrap.application': application, 'camctl.session.locks': locks}), \
                patch.object(update, 'verify_checksums'), patch.object(update, 'current_target', return_value='x86_64'), \
                patch.object(update, 'wheel_target_manifest', return_value=release['wheel_targets']), \
                patch.object(Path, 'read_text', return_value=json.dumps(release)), \
                patch.object(Path, 'read_bytes', return_value=b'original'), \
                patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'is_dir', return_value=True), \
                patch.object(Path, 'glob', autospec=True, side_effect=lambda path, pattern: iter([wheel])), \
                patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path), \
                patch.object(update.zipfile, 'ZipFile') as zip_file, \
                patch.object(update.importlib.metadata, 'version', return_value='1.0'), \
                patch.object(ops.db_locks, 'database') as acquire_database:
            zip_file.return_value.__enter__.return_value = archive
            original = ops.preflight()
            ops.lock(original)
        adapter.load.assert_called_once_with(config_path)
        self.assertEqual(original['config'], str(config_path))
        self.assertEqual(original['state_db'], '/outside/state.db')
        locks.lock_file_paths.assert_called_once_with(Path('/outside/state.db'))
        acquire_database.assert_called_once_with(['/session.lock', '/admission.lock'])

    def test_main_passes_explicit_config_to_update(self):
        argv = ['update-camctl.py', '--target', '/target', '--config', '/etc/camctl/config.toml']
        with patch.object(sys, 'argv', argv), patch.object(sys, 'executable', '/usr/bin/python3.11'), \
                patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
                patch.object(sys, 'path', list(sys.path)), patch.object(Path, 'exists', return_value=False), \
                patch.object(update.transaction, 'sync_dir'), patch.object(update.transaction.Locks, 'acquire_directory'), \
                patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'lstat', side_effect=FileNotFoundError), \
                patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path), \
                patch.object(Path, 'mkdir'), patch.object(Path, 'touch'), \
                patch.object(update.shutil, 'which', return_value='/usr/bin/uv'), \
                patch.object(update.UpdateOperations, 'preflight', return_value={'state_db': '/state.db'}), \
                patch.object(update.UpdateOperations, 'check_paths'), \
                patch.object(update, 'perform_update', return_value={'status': 'updated_stopped'}) as perform, \
                patch('builtins.print'):
            self.assertEqual(update.main(), 0)
        self.assertEqual(perform.call_args.args[0].config_path, Path('/etc/camctl/config.toml'))

    def test_main_rejects_relative_config(self):
        with patch.object(sys, 'argv', ['update-camctl.py', '--target', '/target', '--config', 'relative.toml']), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_info:
                update.main()
        self.assertEqual(exit_info.exception.code, 2)

    def test_main_lock_failure_reports_stage_error_and_evidence(self):
        stderr = Mock(spec=['write', 'flush'])
        with patch.object(sys, 'argv', ['update-camctl.py', '--target', '/target']), \
                patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
                patch.object(sys, 'stderr', stderr), patch.object(Path, 'is_dir', return_value=True), \
                patch.object(Path, 'mkdir'), patch.object(Path, 'touch'), \
                patch.object(update.transaction, 'sync_dir'), \
                patch.object(update.transaction.Locks, 'acquire_directory', side_effect=OSError(5, 'lock failure')), \
                patch.object(update.UpdateOperations, 'record_diagnostics'), \
                patch.object(update.UpdateOperations, 'save') as save:
            self.assertEqual(update.main(), 1)
        message = json.loads(stderr.write.call_args.args[0])
        self.assertEqual(message['stage'], 'update_lock')
        self.assertEqual(message['diagnostics'][0]['errno'], 5)
        self.assertIsNone(message['evidence'])
        save.assert_not_called()

    def test_path_conflict_is_rejected_before_control_directory_or_log_creation(self):
        with patch.object(sys, 'argv', ['update-camctl.py', '--target', '/target']), \
                patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
                patch.object(sys, 'stderr', io.StringIO()), patch.object(sys, 'path', list(sys.path)), \
                patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'lstat', side_effect=FileNotFoundError), \
                patch.object(Path, 'mkdir') as mkdir, patch.object(Path, 'touch') as touch, \
                patch.object(update.shutil, 'which', return_value='/tools/uv'), \
                patch.object(transaction.Locks, 'acquire_directory'), \
                patch.object(update.UpdateOperations, 'preflight', return_value={'state_db': '/db/state'}), \
                patch.object(update.UpdateOperations, 'check_paths', side_effect=ValueError('path overlap')):
            self.assertEqual(update.main(), 1)
        mkdir.assert_not_called()
        touch.assert_not_called()

    def test_main_stderr_failure_preserves_original_and_output_error(self):
        for method in ('write', 'flush'):
            with self.subTest(method=method):
                stderr = Mock(spec=['write', 'flush'])
                getattr(stderr, method).side_effect = BrokenPipeError('stderr failed')
                with patch.object(sys, 'argv', ['update-camctl.py', '--target', '/target']), \
                        patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
                        patch.object(sys, 'stderr', stderr), patch.object(Path, 'is_dir', return_value=True), \
                        patch.object(Path, 'mkdir'), patch.object(Path, 'touch'), \
                        patch.object(update.transaction, 'sync_dir'), \
                        patch.object(update.transaction.Locks, 'acquire_directory', side_effect=OSError(5, 'lock failure')), \
                        patch.object(update.UpdateOperations, 'record_diagnostics'), \
                        patch.object(update.UpdateOperations, 'save') as save, \
                        patch.object(update, 'freeze_exception', wraps=update.freeze_exception) as freeze:
                    self.assertEqual(update.main(), 1)
                self.assertEqual([call.args[1] for call in freeze.call_args_list], ['update_lock', 'outer_stderr'])
                save.assert_not_called()

    def test_invalid_pending_record_blocks_new_update_at_entry(self):
        with patch.object(sys, 'argv', ['update-camctl.py', '--target', '/target']), \
                patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
                patch.object(sys, 'stderr', io.StringIO()), patch.object(Path, 'is_dir', return_value=True), \
                patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o100600)), \
                patch.object(Path, 'read_text', return_value='{}'), \
                patch.object(Path, 'mkdir'), patch.object(Path, 'touch'), \
                patch.object(update.transaction, 'sync_dir'), \
                patch.object(update.transaction.Locks, 'acquire_directory'), \
                patch.object(update.transaction, 'classify_record', create=True,
                             side_effect=ValueError('invalid journal')), \
                patch.object(update.UpdateOperations, 'record_diagnostics'), \
                patch.object(update.UpdateOperations, 'save'), \
                patch.object(update, 'perform_update') as perform:
            self.assertEqual(update.main(), 1)
        perform.assert_not_called()

    def test_recovery_uses_recorded_bindings_before_current_update_arguments(self):
        order = []
        metadata = {'staging_path': '/old/stage', 'ready_path': '/old/ready',
                    'processing_path': '/old/processing'}
        record = {'output': '/target/updates/old', 'output_identity': {'ino': 1},
                  'original': {'deployment': {'metadata': metadata}},
                  'client': {'configured_path': '/old-client/device-capabilities.json'},
                  'temporary': {}, 'lock_paths': ['/db/session', '/db/admission']}
        states = SimpleNamespace(UPDATING=object(), COMMITTED=object(),
                                 ROLLED_BACK=SimpleNamespace(value='rolled_back'))
        old_ops, new_ops = Mock(), Mock()
        new_ops.preflight.side_effect = lambda: (order.append('preflight'),
                                                 (_ for _ in ()).throw(ValueError('stop')))[1]
        def lstat(path):
            if str(path) == '/target/updates/pending.json':
                return SimpleNamespace(st_mode=0o100600)
            raise FileNotFoundError(str(path))
        with patch.object(sys, 'argv', ['update-camctl.py', '--target', '/target', '--client-data', '/new-client']), \
                patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
                patch.object(sys, 'stderr', io.StringIO()), patch.object(sys, 'path', list(sys.path)), \
                patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'lstat', autospec=True, side_effect=lstat), \
                patch.object(Path, 'read_text', return_value=json.dumps(record)), \
                patch.object(update.shutil, 'which', return_value='/tools/uv'), \
                patch.object(transaction.Locks, 'acquire_directory'), \
                patch.object(transaction, 'classify_record', return_value=states.UPDATING, create=True), \
                patch.object(transaction, 'JournalState', states, create=True), \
                patch.object(transaction, 'validate_record_target', create=True), \
                patch.object(transaction, 'validate_active_record', create=True), \
                patch.object(transaction, 'directory_identity', return_value={'ino': 1}, create=True), \
                patch.object(transaction, 'validate_paths', create=True) as validate, \
                patch.object(transaction, 'recover', side_effect=lambda *args: order.append('recover'), create=True), \
                patch.object(transaction, 'atomic_json', create=True), \
                patch.object(update, 'UpdateOperations', side_effect=[old_ops, new_ops]):
            self.assertEqual(update.main(), 1)
        self.assertEqual(order, ['recover', 'preflight'])
        self.assertEqual(validate.call_args_list[0].args[2], Path('/old-client'))
        self.assertEqual(validate.call_args_list[0].args[4], ['/old/stage', '/old/ready', '/old/processing'])

    def test_recovery_failure_keeps_original_receipt(self):
        metadata = {'staging_path': '/stage', 'ready_path': '/ready', 'processing_path': '/processing'}
        record = {'output': '/target/updates/old', 'output_identity': {'ino': 1},
                  'original': {'deployment': {'metadata': metadata}}, 'temporary': {},
                  'lock_paths': ['/db/session', '/db/admission']}
        states = SimpleNamespace(UPDATING=object(), COMMITTED=object(), ROLLED_BACK=object())
        old_ops = Mock()
        with patch.object(sys, 'argv', ['update-camctl.py', '--target', '/target']), \
                patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
                patch.object(sys, 'stderr', io.StringIO()), patch.object(Path, 'is_dir', return_value=True), \
                patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o100600)), \
                patch.object(Path, 'read_text', return_value=json.dumps(record)), \
                patch.object(transaction.Locks, 'acquire_directory'), \
                patch.object(transaction, 'classify_record', return_value=states.UPDATING, create=True), \
                patch.object(transaction, 'JournalState', states, create=True), \
                patch.object(transaction, 'validate_record_target', create=True), \
                patch.object(transaction, 'validate_active_record', create=True), \
                patch.object(transaction, 'directory_identity', return_value={'ino': 1}, create=True), \
                patch.object(transaction, 'validate_paths', create=True), \
                patch.object(transaction, 'recover', side_effect=ValueError('restore failed'), create=True), \
                patch.object(update, 'UpdateOperations', return_value=old_ops):
            self.assertEqual(update.main(), 1)
        old_ops.save.assert_not_called()
        self.assertEqual(old_ops.save_recovery_failure.call_args.args[0]['diagnostics'][0]['stage'], 'recover_pending')

    def test_terminal_record_does_not_consult_obsolete_deployment(self):
        for state_name in ('COMMITTED', 'ROLLED_BACK'):
            with self.subTest(state=state_name):
                record = {'output': '/target/updates/old', 'output_identity': {'ino': 1},
                          'original': {'deployment': {'metadata': {}}},
                          'client': {'configured_path': '/obsolete/link/device-capabilities.json'}}
                states = SimpleNamespace(UPDATING=object(), COMMITTED=object(), ROLLED_BACK=object())
                new_ops = Mock()
                new_ops.preflight.side_effect = ValueError('current preflight reached')
                with patch.multiple(sys, argv=['update-camctl.py', '--target', '/target', '--config', '/new/config'],
                                    prefix='/base', base_prefix='/base', stderr=io.StringIO(), path=list(sys.path)), \
                        patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
                        patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o100600)), \
                        patch.object(Path, 'read_text', return_value=json.dumps(record)), \
                        patch.object(update.shutil, 'which', return_value='/tools/uv'), \
                        patch.object(transaction.Locks, 'acquire_directory'), \
                        patch.object(transaction, 'classify_record', return_value=getattr(states, state_name), create=True), \
                        patch.object(transaction, 'JournalState', states, create=True), \
                        patch.object(transaction, 'validate_record_target', create=True), \
                        patch.object(transaction, 'directory_identity', return_value={'ino': 1}, create=True), \
                        patch.object(transaction, 'validate_active_record', create=True) as active, \
                        patch.object(transaction, 'validate_paths', create=True) as old_layout, \
                        patch.object(transaction, 'sync_file', create=True), \
                        patch.object(transaction, 'sync_dir'), \
                        patch.object(update, 'UpdateOperations', return_value=new_ops):
                    self.assertEqual(update.main(), 1)
                new_ops.preflight.assert_called_once()
                active.assert_not_called()
                old_layout.assert_not_called()

    def test_rollback_uses_persisted_record_after_reacquiring_locks(self):
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/target/updates/id'))
        ops.record = {'lock_paths': ['/db/session', '/db/admission']}
        ops.update_locks = Mock()
        ops.db_locks = Mock()
        actual_state = object()
        formal = {'state': 'updating', 'other': 'persisted', 'temporary': {},
                  'original': {'deployment': {'metadata': {'staging_path': '/stage',
                               'ready_path': '/ready', 'processing_path': '/processing'}}}}
        ops.read_pending = Mock(return_value=(formal, actual_state))
        states = SimpleNamespace(UPDATING=actual_state, ROLLED_BACK=SimpleNamespace(value='rolled_back'))
        with patch.object(transaction, 'JournalState', states, create=True), \
                patch.object(transaction, 'validate_active_record', create=True), \
                patch.object(transaction, 'validate_paths', create=True), \
                patch.object(transaction, 'recover', create=True) as recover, \
                patch.object(transaction, 'atomic_json', create=True):
            ops.rollback()
        self.assertIs(recover.call_args.args[0], formal)
        self.assertEqual(formal['state'], 'rolled_back')
        ops.update_locks.acquire_directory.assert_called_once_with(Path('/target'))

    def test_persisted_record_from_another_update_cannot_be_consumed(self):
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/target/updates/id'))
        ops.record = {'output': '/target/updates/id'}
        states = SimpleNamespace(PREPARING=object())
        with patch.object(Path, 'read_text', return_value=json.dumps(
                {'output': '/target/updates/other', 'output_identity': {'ino': 1}})), \
                patch.object(transaction, 'classify_record', return_value=object(), create=True), \
                patch.object(transaction, 'validate_record_target', create=True), \
                patch.object(transaction, 'directory_identity', return_value={'ino': 1}, create=True), \
                patch.object(transaction, 'JournalState', states, create=True):
            with self.assertRaises(ValueError):
                ops.read_pending()

    def test_persisted_new_creator_does_not_conflict_with_unchanged_core(self):
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/target/updates/id'))
        base = {'output': '/target/updates/id', 'output_identity': {'ino': 1}, 'env': '/target/venv',
                'env_backup': '/target/updates/id/venv.backup', 'state_db': '/db/state',
                'db_backup': '/target/updates/id/state.backup.db', 'lock_paths': ['/db/a', '/db/b'],
                'controller': '/python', 'original': {}, 'temporary': {}, 'state': 'updating',
                'temporary_owners': {'staging': [{'uid': 0, 'gid': 0}]}}
        ops.record = base
        formal = dict(base, temporary_owners={'staging': [{'uid': 0, 'gid': 0}, {'uid': 1000, 'gid': 1000}]})
        states = SimpleNamespace(PREPARING=object())
        with patch.object(Path, 'read_text', return_value=json.dumps(formal)), \
                patch.object(transaction, 'classify_record', return_value=object(), create=True), \
                patch.object(transaction, 'validate_record_target', create=True), \
                patch.object(transaction, 'directory_identity', return_value={'ino': 1}, create=True), \
                patch.object(transaction, 'JournalState', states, create=True):
            actual, _ = ops.read_pending()
        self.assertEqual(actual['temporary_owners']['staging'], formal['temporary_owners']['staging'])

    def test_uncertain_creator_record_publication_retains_formal_facts(self):
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/target/updates/id'))
        ops.record = {'temporary_owners': {'staging': []}}
        formal = {'temporary_owners': {'staging': [{'uid': 0, 'gid': 0}]}}
        ops.read_pending = Mock(return_value=(formal, object()))
        with patch.object(transaction, 'creation_identity', return_value={'uid': 0, 'gid': 0}, create=True), \
                patch.object(transaction, 'atomic_json', side_effect=OSError('sync failed'), create=True), \
                patch.object(transaction, 'sync_file', create=True), patch.object(transaction, 'sync_dir'):
            with self.assertRaises(OSError):
                ops.register_owner('staging', Path('/parent'))
        self.assertEqual(ops.record, formal)

    def test_failure_diagnostic_keeps_cause_and_facts_after_unlock(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        try:
            try:
                raise OSError(13, 'denied')
            except OSError as cause:
                error = RuntimeError('lock failed')
                encoder.attach_diagnostic_facts(error, {'lock': {'operation': 'unlock', 'path': '/state.lock'}})
                raise error from cause
        except RuntimeError as caught:
            ops.install.side_effect = caught
        with patch.dict(sys.modules, {'camctl.operations.error_diagnostics': encoder}):
            receipt = update.perform_update(ops)
        diagnostic = receipt['diagnostics'][0]
        self.assertEqual(diagnostic['cause']['errno'], 13)
        self.assertEqual(diagnostic['diagnostic_facts']['lock']['path'], '/state.lock')
        calls = [item[0] for item in ops.mock_calls]
        self.assertLess(calls.index('unlock'), calls.index('record_diagnostics'))
        self.assertEqual(ops.record_diagnostics.call_args.args[0], receipt['diagnostics'])

    def test_log_write_failure_delivers_original_and_write_error_to_stderr(self):
        ops = Mock()
        ops.preflight.side_effect = ValueError('original error')
        ops.record_diagnostics.side_effect = OSError(28, 'disk full')
        stderr = io.StringIO()
        with patch.dict(sys.modules, {'camctl.operations.error_diagnostics': encoder}), \
                contextlib.redirect_stderr(stderr):
            receipt = update.perform_update(ops)
        delivered = json.loads(stderr.getvalue())
        self.assertEqual(delivered['diagnostics'][0]['message'], 'original error')
        self.assertEqual(delivered['log_error']['errno'], 28)
        self.assertEqual(receipt['log_error']['errno'], 28)

    def test_main_and_unlock_errors_are_reported_separately(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.install.side_effect = ValueError('install error')
        ops.unlock.side_effect = OSError(5, 'unlock error')
        with patch.dict(sys.modules, {'camctl.operations.error_diagnostics': encoder}):
            receipt = update.perform_update(ops)
        self.assertEqual([item['stage'] for item in receipt['diagnostics']], ['install', 'unlock'])
        self.assertEqual(receipt['diagnostics'][0]['message'], 'install error')
        self.assertEqual(receipt['diagnostics'][1]['errno'], 5)
        self.assertEqual(ops.record_diagnostics.call_args.args[0], receipt['diagnostics'])

    def test_missing_encoder_preserves_cause_group_and_import_failure(self):
        original_import = builtins.__import__
        def missing_encoder(name, *args, **kwargs):
            if name == 'camctl.operations.error_diagnostics':
                raise ModuleNotFoundError('missing camctl encoder')
            return original_import(name, *args, **kwargs)
        try:
            try:
                raise ValueError('inner')
            except ValueError as cause:
                raise ExceptionGroup('group', [RuntimeError('child')]) from cause
        except ExceptionGroup as error:
            with patch('builtins.__import__', side_effect=missing_encoder), \
                    patch.object(linecache, 'getline', side_effect=AssertionError('unexpected source read')):
                diagnostic = update.freeze_exception(error, 'preflight')
        self.assertEqual(diagnostic['cause']['message'], 'inner')
        self.assertEqual(diagnostic['exceptions'][0]['message'], 'child')
        self.assertEqual(diagnostic['encoder_error']['message'], 'missing camctl encoder')
        self.assertEqual(diagnostic['stage'], 'preflight')

    def test_final_receipt_failure_does_not_skip_original_diagnostic(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.install.side_effect = ValueError('install failed')
        ops.save.side_effect = [None, None, OSError(28, 'receipt full')]
        with patch.dict(sys.modules, {'camctl.operations.error_diagnostics': encoder}):
            receipt = update.perform_update(ops)
        self.assertEqual([item['stage'] for item in receipt['diagnostics']], ['install', 'save_final_receipt'])
        self.assertEqual(ops.record_diagnostics.call_count, 2)
        self.assertEqual(ops.record_diagnostics.call_args_list[0].args[0][0]['message'], 'install failed')
        self.assertEqual(ops.record_diagnostics.call_args_list[1].args[0][0]['errno'], 28)

    def test_preflight_error_reaches_actual_log_consumer(self):
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'))
        ops.preflight = Mock(side_effect=ValueError('delivery invalid'))
        ops.save = Mock()
        stream = io.StringIO()
        with patch.dict(sys.modules, {'camctl.operations.error_diagnostics': encoder}), \
                patch.object(Path, 'open', return_value=contextlib.nullcontext(stream)):
            receipt = update.perform_update(ops)
        delivered = json.loads(stream.getvalue())
        self.assertEqual(delivered, receipt['diagnostics'][0])
        self.assertEqual(delivered['stage'], 'preflight')
        self.assertEqual(delivered['message'], 'delivery invalid')

    def test_record_diagnostics_writes_json_to_log(self):
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'))
        stream = io.StringIO()
        diagnostic = {'type': 'ValueError', 'message': 'original'}
        with patch.object(Path, 'open', return_value=contextlib.nullcontext(stream)):
            ops.record_diagnostics([diagnostic])
        self.assertEqual(json.loads(stream.getvalue()), diagnostic)

    def test_lock_conflict_prevents_installation(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.lock.side_effect = RuntimeError('会话锁冲突')
        receipt = update.perform_update(ops)
        self.assertEqual(receipt['status'], 'failed')
        self.assertFalse(receipt['environment_may_have_changed'])
        ops.install.assert_not_called()
        self.assertIn('会话锁冲突', receipt['error'])

    def test_install_failure_retains_original_identity_and_warning(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db', 'old_version': '1.0'}
        ops.install.side_effect = RuntimeError('安装失败')
        receipt = update.perform_update(ops)
        self.assertEqual(receipt['original']['old_version'], '1.0')
        self.assertTrue(receipt['environment_may_have_changed'])
        self.assertEqual(receipt['status'], 'failed')
        ops.check_new.assert_not_called()
        ops.unlock.assert_called_once()

    def test_success_checks_before_unlock(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        receipt = update.perform_update(ops)
        self.assertEqual(receipt['status'], 'updated_stopped')
        calls = [item[0] for item in ops.mock_calls]
        self.assertLess(calls.index('lock'), calls.index('install'))
        self.assertLess(calls.index('check_new'), calls.index('unlock'))


    def test_install_commands_require_offline_hashes_and_reinstall(self):
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'))
        ops.wheelhouse, ops.wheel = Path('/bundle/wheels'), Path('/bundle/camctl.whl')
        ops.run = Mock()
        ops.install({'steps': []})
        commands = [call.args[2] for call in ops.run.call_args_list]
        self.assertIn('--offline', commands[0])
        self.assertIn('--no-index', commands[0])
        self.assertIn('--require-hashes', commands[0])
        self.assertIn('--reinstall-package', commands[1])
        self.assertIn('camctl', commands[1])
        self.assertEqual(commands[2][:3], ['uv', 'pip', 'check'])

    def test_new_version_mismatch_stops_before_deployment_check(self):
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'))
        ops.expected_version = '2.0'
        ops.run = Mock(return_value='camctl 1.0')
        with self.assertRaisesRegex(ValueError, '版本不符'):
            ops.check_new({'original': {}})
        ops.run.assert_called_once()

    def test_new_deployment_matches_exact_version_identity_and_config(self):
        config_path = Path('/etc/camctl/config.toml')
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'),
                                      config_path=config_path)
        ops.expected_version = '2.0'
        identity = {'state_db': '/state.db', 'metadata': {'instance_id': 'original'}}
        ops.run = Mock(side_effect=['2.0\n', json.dumps(identity), ''])
        receipt = {'new_deployment': identity, 'original': {'deployment': identity,
            'config_sha256': hashlib.sha256(b'config').hexdigest()}}
        with patch.object(Path, 'read_bytes', return_value=b'config'):
            ops.check_new(receipt)
        self.assertEqual(receipt['capabilities'], '/output/device-capabilities.json')
        self.assertEqual(ops.run.call_count, 3)
        self.assertEqual(ops.run.call_args_list[1].args[2][-2:], ['--config', config_path])
        self.assertEqual(ops.run.call_args_list[2].args[2][-2:], ['--config', config_path])

    def test_version_output_must_include_only_version_and_newline(self):
        for output in ('camctl 2.0\n', '2.0', '2.0\n\n'):
            with self.subTest(output=output):
                ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'))
                ops.expected_version = '2.0'
                ops.run = Mock(return_value=output)
                with self.assertRaisesRegex(ValueError, '版本不符'):
                    ops.check_new({'original': {}})

    def test_release_error_still_unlocks(self):
        ops = Mock()
        ops.preflight.return_value = {'state_db': '/state.db'}
        ops.check_new.side_effect = ValueError('格式不支持')
        result = update.perform_update(ops)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('格式不支持', result['error'])
        self.assertTrue(result['environment_may_have_changed'])
        ops.unlock.assert_called_once()

if __name__ == '__main__':
    unittest.main()
