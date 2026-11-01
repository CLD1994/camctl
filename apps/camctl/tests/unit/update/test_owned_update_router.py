"""稳定入口对请求结果、恢复事实和启动资格的隔离判断。"""
import importlib.util
import io
import json
from enum import Enum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


class JournalState(Enum):
    PREPARING = 'preparing'
    UPDATING = 'updating'
    COMMITTED = 'committed'
    ROLLED_BACK = 'rolled_back'


transaction = ModuleType('update_transaction')
transaction.JournalState = JournalState
diagnostics = ModuleType('error_diagnostics')
diagnostics.exception_diagnostic = lambda error: {'type': type(error).__name__, 'message': str(error)}
fallback = ModuleType('diagnostic_fallback')
def freeze(error, stage, encoder):
    return dict(encoder(error), stage=stage)
fallback.freeze = freeze
archive_support = ModuleType('archive_support')
archive_support.UpdateArchive = Mock()
source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/router.py'
spec = importlib.util.spec_from_file_location('owned_update_router_under_test', source)
router = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'update_transaction': transaction, 'error_diagnostics': diagnostics, 'diagnostic_fallback': fallback, 'archive_support': archive_support}), patch.object(sys, 'path', list(sys.path)):
    spec.loader.exec_module(router)


binding = {'target': '/target', 'python': '/python', 'camctl_path': '/target/venv/bin/camctl'}
active = {'original': {'config_source': '/config'}, 'mutation_intent': True}


def call(action='apply'):
    with patch.object(sys, 'stderr', io.StringIO()):
        return router.execute(binding, action, binding['camctl_path'], '/config', '/bundle', None)


def test_new_bundle_failure_preserves_confirmed_eligibility_without_old_transaction():
    with patch.object(router, '_lock', return_value=11), patch.object(router, '_pending', return_value=(None, None)), \
            patch.object(router, '_engine_memfd', side_effect=ValueError('wheel missing')), \
            patch.object(router.os, 'close'):
        result = call()
    assert (result['outcome'], result['effect'], result['eligibility'], result['prior_recovery']) == ('failed', 'unchanged', 'allowed', 'none')
    assert 'wheel missing' in result['reason']


def test_new_bundle_failure_keeps_prior_recovery_and_eligibility():
    with patch.object(router, '_lock', side_effect=[11, 12]), \
            patch.object(router, '_pending', side_effect=[(active, JournalState.UPDATING),
                                                          ({}, JournalState.ROLLED_BACK)]), \
            patch.object(router, '_frozen', return_value=Path('/frozen')), \
            patch.object(router, '_invoke', return_value={'status': 'recovered'}), \
            patch.object(router, '_engine_memfd', side_effect=ValueError('invalid wheel')), \
            patch.object(router.os, 'close'):
        result = call()
    assert (result['outcome'], result['effect'], result['eligibility'], result['prior_recovery']) == ('failed', 'unchanged', 'allowed', 'recovered')


def test_failed_current_update_is_separate_from_prior_recovery():
    with patch.object(router, '_lock', side_effect=[11, 12, 13]), \
            patch.object(router, '_pending', side_effect=[(active, JournalState.UPDATING),
                                                          ({}, JournalState.ROLLED_BACK),
                                                          ({'output': '/target/updates/id'}, JournalState.ROLLED_BACK)]), \
            patch.object(router, '_frozen', return_value=Path('/frozen')), \
            patch.object(router, '_invoke', side_effect=[{'status': 'recovered'}, {'status': 'failed', 'effect': 'rolled_back'}]), \
            patch.object(router, '_engine_memfd', return_value=14), \
            patch.object(router.uuid, 'uuid4', return_value=SimpleNamespace(hex='id')), \
            patch.object(router.os, 'close'):
        result = call()
    assert (result['outcome'], result['effect'], result['eligibility'], result['prior_recovery']) == ('failed', 'rolled_back', 'allowed', 'recovered')


def test_lost_qualification_after_invocation_is_not_claimed_allowed():
    with patch.object(router, '_lock', side_effect=[11, router.LockBusy()]), \
            patch.object(router, '_pending', return_value=(None, None)), \
            patch.object(router, '_engine_memfd', return_value=14), \
            patch.object(router, '_invoke', return_value={'status': 'updated_stopped', 'effect': 'committed'}), \
            patch.object(router.uuid, 'uuid4', return_value=SimpleNamespace(hex='id')), \
            patch.object(router.os, 'close'):
        result = call()
    assert (result['outcome'], result['effect'], result['eligibility']) == ('unknown', 'unknown', 'blocked')


def test_unverifiable_frozen_engine_preserves_recovery_responsibility():
    with patch.object(router, '_lock', return_value=11), \
            patch.object(router, '_pending', return_value=(active, JournalState.UPDATING)), \
            patch.object(router, '_frozen', side_effect=ValueError('engine hash mismatch')), \
            patch.object(router.os, 'close'):
        result = call()
    assert (result['outcome'], result['eligibility']) == ('recovery_required', 'blocked')


def test_old_recovery_uses_recorded_configuration_before_new_config():
    for new_config in (None, '/new-config'):
        with patch.object(router, '_lock', side_effect=[11, 12]), \
                patch.object(router, '_pending', side_effect=[(active, JournalState.UPDATING),
                                                              ({}, JournalState.ROLLED_BACK)]), \
                patch.object(router, '_frozen', return_value=Path('/frozen')), \
                patch.object(router, '_invoke', return_value={'status': 'recovered'}) as invoke, \
                patch.object(router.os, 'close'), patch.object(sys, 'stderr', io.StringIO()):
            result = router.execute(binding, 'recover', binding['camctl_path'], new_config, None, None)
        assert (result['outcome'], result['effect'], result['eligibility']) == ('recovered', 'rolled_back', 'allowed')
        assert invoke.call_args.args[3] == ['--target', '/target', '--recover-only']


def test_old_recovery_precedes_new_apply_configuration():
    old_default = {'original': {'config_source': None}, 'mutation_intent': True}
    with patch.object(router, '_lock', side_effect=[11, 12, 13]), \
            patch.object(router, '_pending', side_effect=[(old_default, JournalState.UPDATING),
                ({}, JournalState.ROLLED_BACK), ({'output': '/target/updates/id'}, JournalState.COMMITTED)]), \
            patch.object(router, '_frozen', return_value=Path('/frozen')), \
            patch.object(router, '_invoke', side_effect=[{'status': 'recovered'},
                {'status': 'updated_stopped', 'effect': 'committed'}]) as invoke, \
            patch.object(router, '_engine_memfd', return_value=14), \
            patch.object(router.uuid, 'uuid4', return_value=SimpleNamespace(hex='id')), \
            patch.object(router.os, 'close'), patch.object(sys, 'stderr', io.StringIO()):
        result = router.execute(binding, 'apply', binding['camctl_path'], '/new-config', '/bundle', None)
    assert (result['outcome'], result['effect'], result['prior_recovery']) == ('applied', 'committed', 'recovered')
    assert '--config' not in invoke.call_args_list[0].args[3]
    assert invoke.call_args_list[1].args[3][-2:] == ['--config', '/new-config']


def test_check_blocks_active_record_for_any_configuration():
    for new_config in (None, '/new-config'):
        with patch.object(router, '_lock', return_value=11), \
                patch.object(router, '_pending', return_value=(active, JournalState.UPDATING)), \
                patch.object(router.os, 'close'), patch.object(sys, 'stderr', io.StringIO()):
            result = router.execute(binding, 'check', binding['camctl_path'], new_config, None, None)
        assert (result['outcome'], result['eligibility']) == ('recovery_required', 'blocked')


def test_invalid_pending_blocks_apply_before_reading_new_bundle():
    with patch.object(router, '_lock', return_value=11), \
            patch.object(router, '_pending', side_effect=ValueError('invalid journal')), \
            patch.object(router, '_engine_memfd') as engine, \
            patch.object(router, '_invoke') as invoke, patch.object(router.os, 'close'):
        result = call()
    assert result['outcome'] == 'unknown' and result['effect'] == 'unknown'
    assert result['eligibility'] == 'unknown'
    assert result['stage'] == 'pending'
    engine.assert_not_called()
    invoke.assert_not_called()


def test_lock_failure_delivers_original_error_without_starting():
    stream = io.StringIO()
    with patch.object(router, '_lock', side_effect=OSError(5, 'lock failure')), \
            patch.object(router, '_pending') as pending, patch.object(sys, 'stderr', stream):
        result = router.execute(binding, 'apply', binding['camctl_path'], '/config', '/bundle', None)
    assert result['outcome'] == 'unknown' and result['eligibility'] == 'unknown'
    assert result['stage'] == 'lock'
    assert json.loads(stream.getvalue())['message'] == '[Errno 5] lock failure'
    pending.assert_not_called()


def test_terminal_record_does_not_consult_old_bindings_before_new_apply():
    for state in (JournalState.COMMITTED, JournalState.ROLLED_BACK):
        obsolete = {'original': {'config_source': '/obsolete'},
                    'client': {'configured_path': '/obsolete/client'}}
        with patch.object(router, '_lock', return_value=11), \
                patch.object(router, '_pending', return_value=(obsolete, state)), \
                patch.object(router, '_frozen') as frozen, \
                patch.object(router, '_engine_memfd', side_effect=ValueError('new package invalid')) as engine, \
                patch.object(router.os, 'close'):
            result = call()
        frozen.assert_not_called()
        engine.assert_called_once_with(Path('/bundle'))
        assert result['outcome'] == 'failed' and result['eligibility'] == 'allowed'


def test_error_diagnostic_is_delivered_after_closing_qualification():
    events = []
    class Stream(io.StringIO):
        def write(self, text):
            events.append('diagnostic')
            return super().write(text)
    stream = Stream()
    with patch.object(router, '_lock', return_value=11), \
            patch.object(router, '_pending', return_value=(None, None)), \
            patch.object(router, '_engine_memfd', side_effect=ValueError('bad wheel')), \
            patch.object(router.os, 'close', side_effect=lambda fd: events.append('close')), \
            patch.object(sys, 'stderr', stream):
        result = router.execute(binding, 'apply', binding['camctl_path'], '/config', '/bundle', None)
    assert events == ['close', 'diagnostic']
    assert json.loads(stream.getvalue()) == {'type': 'ValueError', 'message': 'bad wheel', 'stage': 'read_new_engine'}
    assert result['eligibility'] == 'allowed'


def test_failed_recovery_reports_unfinished_effect_after_invocation():
    with patch.object(router, '_lock', return_value=11), \
            patch.object(router, '_pending', return_value=(active, JournalState.UPDATING)), \
            patch.object(router, '_frozen', return_value=Path('/frozen')), \
            patch.object(router, '_invoke', return_value={'status': 'failed', 'effect': 'recovery_required'}), \
            patch.object(router.os, 'close'), patch.object(sys, 'stderr', io.StringIO()):
        result = router.execute(binding, 'recover', binding['camctl_path'], None, None, None)
    assert (result['outcome'], result['effect'], result['eligibility'], result['prior_recovery']) == (
        'recovery_required', 'recovery_required', 'blocked', 'unknown')


def test_invalid_recovery_result_does_not_claim_no_effect():
    with patch.object(router, '_lock', return_value=11), \
            patch.object(router, '_pending', return_value=(active, JournalState.UPDATING)), \
            patch.object(router, '_frozen', return_value=Path('/frozen')), \
            patch.object(router, '_invoke', side_effect=ValueError('invalid result')), \
            patch.object(router.os, 'close'), patch.object(sys, 'stderr', io.StringIO()):
        result = router.execute(binding, 'recover', binding['camctl_path'], None, None, None)
    assert (result['outcome'], result['effect'], result['eligibility']) == ('unknown', 'unknown', 'unknown')


def test_zip_engine_is_selected_after_archive_verification():
    import zipfile
    outer = Mock()
    outer.wheel_name.return_value = 'camctl/camctl.whl'
    outer.read.return_value = b'wheel'
    outer_context = Mock()
    outer_context.__enter__ = Mock(return_value=outer)
    outer_context.__exit__ = Mock(return_value=False)
    inner = Mock()
    inner.read.return_value = b'engine'
    inner_context = Mock()
    inner_context.__enter__ = Mock(return_value=inner)
    inner_context.__exit__ = Mock(return_value=False)
    sink = Mock()
    sink_context = Mock()
    sink_context.__enter__ = Mock(return_value=sink)
    sink_context.__exit__ = Mock(return_value=False)
    with patch.object(Path, 'is_dir', return_value=False), \
            patch.object(router, '_lock', side_effect=[11, 12]), \
            patch.object(router, '_pending', return_value=(None, None)), \
            patch.object(router, '_invoke', return_value={'status': 'failed', 'effect': 'unchanged'}), \
            patch.object(router.uuid, 'uuid4', return_value=SimpleNamespace(hex='id')), \
            patch.object(router, 'UpdateArchive', return_value=outer_context), \
            patch.object(zipfile, 'ZipFile', return_value=inner_context), \
            patch.object(router.os, 'memfd_create', return_value=14), \
            patch.object(router.os, 'dup', return_value=15), \
            patch.object(router.os, 'fdopen', return_value=sink_context), \
            patch.object(router.os, 'lseek'), patch.object(router.os, 'close'):
        result = call()
    assert (result['outcome'], result['effect'], result['eligibility']) == ('failed', 'unchanged', 'allowed')
    assert [call[0] for call in outer.mock_calls[:3]] == ['verify', 'wheel_name', 'read']
    inner.read.assert_called_once_with('camctl/update_engine.pyz')
    sink.write.assert_called_once_with(b'engine')
