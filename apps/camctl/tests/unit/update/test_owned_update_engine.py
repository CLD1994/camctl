"""组件维护事务在受控边界下的提交、恢复和诊断责任。"""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import Mock, patch

root = Path(__file__).absolute().parents[5]
engine_dir = root / 'apps/camctl/src/camctl/update_engine'
encoder_spec = importlib.util.spec_from_file_location('error_diagnostics',
    root / 'apps/camctl/src/camctl/operations/error_diagnostics.py')
encoder = importlib.util.module_from_spec(encoder_spec)
encoder_spec.loader.exec_module(encoder)
fallback_spec = importlib.util.spec_from_file_location('diagnostic_fallback', engine_dir / 'diagnostic_fallback.py')
fallback = importlib.util.module_from_spec(fallback_spec)
fallback_spec.loader.exec_module(fallback)
bundle = ModuleType('bundle_support')
bundle.WHEEL_TARGETS = {}; bundle.current_target = Mock(); bundle.verify_checksums = Mock()
bundle.wheel_target_manifest = Mock()
archive = ModuleType('archive_support'); archive.UpdateArchive = Mock()
integrity = ModuleType('maintenance_integrity'); integrity.verify_maintenance_integrity = Mock()
transaction = ModuleType('update_transaction')
class Locks:
    def __init__(self):
        self.fds = []
        self.database = Mock()
        self.close = Mock()
transaction.Locks = Locks
transaction.snapshot_config = Mock(return_value={'present': False})
spec = importlib.util.spec_from_file_location('owned_update_engine_under_test', engine_dir / 'engine.py')
engine = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'bundle_support': bundle, 'archive_support': archive,
                              'maintenance_integrity': integrity, 'update_transaction': transaction,
                              'error_diagnostics': encoder, 'diagnostic_fallback': fallback}), \
        patch.object(sys, 'dont_write_bytecode', True):
    spec.loader.exec_module(engine)


def operations():
    ops = Mock()
    ops.preflight.return_value = {'state_db': '/state'}
    return ops


def test_install_failure_rolls_back_before_unlock_and_diagnostic_delivery():
    ops = operations()
    try:
        raise OSError(13, 'install denied', '/venv')
    except OSError as error:
        ops.install.side_effect = error
    result = engine.perform_update(ops)
    order = [item[0] for item in ops.mock_calls]
    assert order.index('backup') < order.index('mark_intent') < order.index('install')
    assert order.index('install') < order.index('rollback') < order.index('unlock') < order.index('record_diagnostics')
    assert result['rollback'] == 'verified'
    assert result['diagnostics'][0]['stage'] == 'install'
    assert result['diagnostics'][0]['errno'] == 13
    assert result['diagnostics'][0]['filename'] == '/venv'


def test_failed_rollback_keeps_original_and_rollback_exception_graphs():
    ops = operations()
    ops.install.side_effect = ValueError('original failure')
    ops.rollback.side_effect = OSError(5, 'rollback failure')
    result = engine.perform_update(ops)
    assert result['rollback'] == 'failed'
    assert [item['stage'] for item in result['diagnostics']] == ['install', 'rollback']
    assert result['diagnostics'][1]['errno'] == 5


def test_unconfirmed_commit_preserves_unknown_without_claiming_rollback():
    ops = operations()
    ops.commit.side_effect = OSError('commit write failed')
    ops.confirm_pending.side_effect = OSError('record cannot be read')
    result = engine.perform_update(ops)
    assert result['rollback'] == 'commit_unknown'
    ops.rollback.assert_not_called()
    assert [item['stage'] for item in result['diagnostics']] == ['commit', 'confirm_pending']


def test_durable_commit_is_not_reversed_by_late_receipt_failure():
    ops = operations()
    ops.save.side_effect = [None, None, OSError('receipt unavailable')]
    result = engine.perform_update(ops)
    assert result['status'] == 'updated_stopped'
    assert result['diagnostics'][-1]['stage'] == 'save_final_receipt'
    ops.rollback.assert_not_called()


def test_diagnostic_output_failure_preserves_original_and_channel_failure():
    ops = operations()
    ops.install.side_effect = ValueError('original failure')
    ops.record_diagnostics.side_effect = OSError('log unavailable')
    stream = Mock(spec=['write', 'flush'])
    stream.write.side_effect = BrokenPipeError('stderr unavailable')
    with patch.object(sys, 'stderr', stream):
        result = engine.perform_update(ops)
    assert result['rollback'] == 'verified'
    assert result['stderr_error']['stage'] == 'diagnostic_stderr'
    assert result['diagnostics'][0]['message'] == 'original failure'


def test_backup_failure_never_installs_or_attempts_rollback():
    ops = operations()
    ops.backup.side_effect = OSError('backup failure')
    result = engine.perform_update(ops)
    assert result['status'] == 'failed'
    ops.mark_intent.assert_not_called()
    ops.install.assert_not_called()
    ops.rollback.assert_not_called()


def test_intent_write_failure_uses_confirmed_persistent_record():
    for state, should_restore in (('preparing', False), ('updating', True)):
        ops = operations()
        ops.mark_intent.side_effect = OSError('intent failure')
        ops.confirm_pending.return_value = state
        result = engine.perform_update(ops)
        assert result['status'] == 'failed'
        assert ops.rollback.called is should_restore
        ops.install.assert_not_called()


def test_visible_durable_commit_keeps_success_and_original_error():
    ops = operations()
    ops.commit.side_effect = OSError('commit write reported failure')
    ops.confirm_pending.return_value = 'committed'
    result = engine.perform_update(ops)
    assert result['status'] == 'updated_stopped'
    assert result['diagnostics'][0]['stage'] == 'commit'
    ops.rollback.assert_not_called()


def test_commit_failure_with_updating_record_rolls_back():
    ops = operations()
    ops.commit.side_effect = OSError('commit failure')
    ops.confirm_pending.return_value = 'updating'
    result = engine.perform_update(ops)
    assert result['status'] == 'failed' and result['rollback'] == 'verified'
    ops.rollback.assert_called_once()


def test_committed_result_survives_unlock_error_with_diagnostic():
    ops = operations()
    ops.unlock.side_effect = OSError(5, 'close failure')
    result = engine.perform_update(ops)
    assert result['status'] == 'updated_stopped'
    assert result['diagnostics'][-1]['stage'] == 'unlock'
    assert result['diagnostics'][-1]['errno'] == 5
    ops.rollback.assert_not_called()


def test_preflight_uses_old_interpreter_probe_and_preserves_explicit_source():
    config_path = Path('/etc/config')
    release = {'components': {'camctl': '0.3.0'}, 'source_sha256': 'source',
               'wheel_targets': {'x86_64': {'files': ['dependency.whl']}}}
    old_config = {'config': str(config_path), 'state_db': '/old/state',
                  'lock_paths': ['/old/session', '/old/admission'], 'old_version': '0.2.0',
                  'paths': {'staging': '/staging', 'ready': '/ready', 'processing': '/processing'}}
    wheel = Mock()
    wheel.namelist.return_value = ['camctl/deployment.py', 'camctl-0.3.0.dist-info/METADATA',
                                   'camctl/persistence/maintenance_integrity.py', 'camctl/update_engine.pyz']
    wheel.read.side_effect = lambda name: (b'Name: camctl\nVersion: 0.3.0\n\n' if name.endswith('METADATA')
                                           else b'engine' if name.endswith('.pyz') else b'integrity')
    inner = Mock(); inner.read.return_value = b'integrity'
    wheel_context, inner_context = Mock(), Mock()
    wheel_context.__enter__ = Mock(return_value=wheel); wheel_context.__exit__ = Mock(return_value=False)
    inner_context.__enter__ = Mock(return_value=inner); inner_context.__exit__ = Mock(return_value=False)
    with patch.dict(engine.os.environ, {}, clear=True), \
            patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
            patch.object(Path, 'read_text', return_value=json.dumps(release)), \
            patch.object(Path, 'read_bytes', side_effect=[b'manifest', b'engine']), \
            patch.object(Path, 'glob', return_value=iter([Path('/bundle/camctl/camctl.whl')])), \
            patch.object(engine, 'verify_checksums'), patch.object(engine, 'current_target', return_value='x86_64'), \
            patch.object(engine, 'wheel_target_manifest', return_value=release['wheel_targets']), \
            patch.object(engine.zipfile, 'ZipFile', side_effect=[wheel_context, inner_context]), \
            patch.object(engine.UpdateOperations, 'probe', return_value=old_config) as probe:
        ops = engine.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'),
                                       config_path=config_path)
        original = ops.preflight()
        ops.lock(original)
    probe.assert_called_once_with('config')
    assert original['config'] == '/etc/config' and original['state_db'] == '/old/state'
    ops.db_locks.database.assert_called_once_with(['/old/session', '/old/admission'])
    assert ops.config_args() == ['--config', config_path]


def test_default_configuration_is_not_rewritten_as_explicit_argument():
    with patch.dict(engine.os.environ, {}, clear=True), patch.object(Path, 'is_dir', return_value=True):
        ops = engine.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'))
    assert ops.config_args() == []


def create_operations(config_path=None):
    with patch.dict(engine.os.environ, {}, clear=True), patch.object(Path, 'is_dir', return_value=True):
        return engine.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'),
                                       config_path=config_path)


def test_offline_install_uses_hashes_and_reinstalls_only_component():
    ops = create_operations()
    ops.wheelhouse, ops.wheel = Path('/bundle/wheels'), Path('/bundle/camctl.whl')
    ops.run = Mock()
    ops.install({'steps': []})
    commands = [item.args[2] for item in ops.run.call_args_list]
    assert '--offline' in commands[0] and '--no-index' in commands[0] and '--require-hashes' in commands[0]
    assert '--reinstall-package' in commands[1] and 'camctl' in commands[1]
    assert commands[2][:3] == ['uv', 'pip', 'check']


def test_new_version_mismatch_never_checks_database():
    ops = create_operations()
    ops.expected_version = '0.3.0'
    ops.run = Mock(return_value='camctl 0.2.0')
    try:
        ops.check_new({'original': {}})
    except ValueError as error:
        assert '版本不符' in str(error)
    else:
        raise AssertionError('错误版本不能通过')
    ops.run.assert_called_once()


def test_new_version_requires_exact_single_line():
    for output in ('camctl 0.3.0\n', '0.3.0', '0.3.0\n\n'):
        ops = create_operations()
        ops.expected_version = '0.3.0'
        ops.run = Mock(return_value=output)
        try:
            ops.check_new({'original': {}})
        except ValueError as error:
            assert '版本不符' in str(error)
        else:
            raise AssertionError('非精确版本不能通过')


def test_new_database_and_capabilities_use_original_explicit_config():
    config = Path('/etc/config')
    ops = create_operations(config)
    ops.expected_version = '0.3.0'
    identity = {'state_db': '/state', 'metadata': {'instance_id': 'original'}}
    ops.run = Mock(side_effect=['0.3.0\n', json.dumps(identity), ''])
    receipt = {'new_deployment': identity, 'original': {'deployment': identity}}
    with patch.object(transaction, 'verify_config', create=True) as verify:
        ops.check_new(receipt)
    verify.assert_called_once_with(receipt['original'])
    assert receipt['capabilities'] == '/output/device-capabilities.json'
    assert ops.run.call_args_list[1].args[2][-2:] == ['--config', config]
    assert ops.run.call_args_list[2].args[2][-2:] == ['--config', config]


def test_lock_conflict_and_new_check_failure_keep_distinct_results():
    ops = operations(); ops.lock.side_effect = OSError(11, 'lock conflict')
    receipt = engine.perform_update(ops)
    assert receipt['status'] == 'failed' and not receipt['environment_may_have_changed']
    ops.install.assert_not_called()
    ops = operations(); ops.check_new.side_effect = ValueError('format unsupported')
    receipt = engine.perform_update(ops)
    assert receipt['status'] == 'failed' and receipt['environment_may_have_changed']
    assert receipt['rollback'] == 'verified'
    ops.unlock.assert_called_once()
