"""组件维护事务在受控边界下的提交、恢复和诊断责任。"""
import importlib.util
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
