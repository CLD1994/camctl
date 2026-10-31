"""更新入口在隔离 I/O 边界下的行为。"""
import builtins
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
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('update_camctl', SCRIPTS / 'update-camctl.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
encoder_spec = importlib.util.spec_from_file_location('error_diagnostics_under_test',
    Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/operations/error_diagnostics.py')
encoder = importlib.util.module_from_spec(encoder_spec)
encoder_spec.loader.exec_module(encoder)


class UpdateTests(unittest.TestCase):
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
        locks.acquire_session = Mock(return_value=Mock())
        locks.acquire_admission = Mock(return_value=Mock())
        archive = Mock()
        archive.namelist.return_value = ['camctl/deployment.py', 'camctl-2.dist-info/METADATA']
        archive.read.return_value = b'Name: camctl\nVersion: 2.0\n\n'
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
                patch.object(update.importlib.metadata, 'version', return_value='1.0'):
            zip_file.return_value.__enter__.return_value = archive
            original = ops.preflight()
            ops.lock(original)
        adapter.load.assert_called_once_with(config_path)
        self.assertEqual(original['config'], str(config_path))
        self.assertEqual(original['state_db'], '/outside/state.db')
        locks.lock_file_paths.assert_called_once_with(Path('/outside/state.db'))
        locks.acquire_session.assert_called_once_with(Path('/session.lock'))
        locks.acquire_admission.assert_called_once_with(Path('/admission.lock'))

    def test_main_passes_explicit_config_to_update(self):
        argv = ['update-camctl.py', '--target', '/target', '--config', '/etc/camctl/config.toml']
        with patch.object(sys, 'argv', argv), patch.object(sys, 'executable', '/target/venv/bin/python'), \
                patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path), \
                patch.object(Path, 'mkdir'), patch.object(Path, 'touch'), \
                patch.object(update.shutil, 'which', return_value='/usr/bin/uv'), \
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
        ops.save.side_effect = [None, None, None, OSError(28, 'receipt full')]
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
        receipt = {'original': {'deployment': identity,
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
