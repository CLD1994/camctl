"""稳定入口对请求结果、恢复事实和启动资格的隔离判断。"""
import importlib.util
import io
from enum import Enum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


class JournalState(Enum):
    PREPARING = 'preparing'
    UPDATING = 'updating'
    COMMITTED = 'committed'
    ROLLED_BACK = 'rolled_back'


transaction = ModuleType('update_transaction')
transaction.JournalState = JournalState
diagnostics = ModuleType('error_diagnostics')
diagnostics.exception_diagnostic = lambda error: {'type': type(error).__name__, 'message': str(error)}
source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/router.py'
spec = importlib.util.spec_from_file_location('owned_update_router_under_test', source)
router = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'update_transaction': transaction, 'error_diagnostics': diagnostics}), patch.object(sys, 'path', list(sys.path)):
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
