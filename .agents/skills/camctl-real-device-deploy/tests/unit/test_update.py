"""更新入口在隔离 I/O 边界下的行为。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('update_camctl', SCRIPTS / 'update-camctl.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class UpdateTests(unittest.TestCase):
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
        ops = update.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'))
        ops.expected_version = '2.0'
        identity = {'state_db': '/state.db', 'metadata': {'instance_id': 'original'}}
        ops.run = Mock(side_effect=['2.0\n', json.dumps(identity), ''])
        receipt = {'original': {'deployment': identity,
            'config_sha256': hashlib.sha256(b'config').hexdigest()}}
        with patch.object(Path, 'read_bytes', return_value=b'config'):
            ops.check_new(receipt)
        self.assertEqual(receipt['capabilities'], '/output/device-capabilities.json')
        self.assertEqual(ops.run.call_count, 3)

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
