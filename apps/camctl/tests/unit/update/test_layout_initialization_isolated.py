"""初始化入口在写入前核对维护布局，所有资源边界使用替身。"""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


history = module('camctl.history.initial_state', runtime_state_values=Mock())
paths = module('camctl.host_files.paths', PURPOSE_DIRECTORIES={})
resources = module('camctl.resources', resource_bytes=Mock())
switch_names = ('SwitchCommitError', 'SwitchCommitObservation', 'canonical_to_path',
                'check_atomic_move_support', 'check_distinct_roots', 'classify_switch_eligibility',
                'load_switch_facts', 'scan_new_directories', 'scan_original_directories',
                'switch_directory_binding')
switch = module('camctl.persistence.directory_switch', **{name: Mock() for name in switch_names})
runtime_names = ('DbConfig', 'DbOpenMode', 'StateDatabaseError', 'CURRENT_FORMAT_VERSION',
                 'canonical_directory_binding', 'ensure_runtime_library', 'open_existing',
                 'verify_directory_binding', '_set_and_verify', '_SCHEMA_RESOURCES')
runtime = module('camctl.persistence.runtime', **{name: Mock() for name in runtime_names})
runtime.StateDatabaseError = type('StateDatabaseError', (Exception,), {})
locks = module('camctl.session.locks', SessionLockConflictError=type('SessionLockConflictError', (Exception,), {}),
               acquire_session=Mock(), lock_error_facts=Mock(), lock_file_paths=Mock())
layout = module('camctl.update_engine.layout', check_initialization=Mock())
source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/persistence/initialization.py'
spec = importlib.util.spec_from_file_location('layout_initialization_under_test', source)
initialization = importlib.util.module_from_spec(spec)
modules = {m.__name__: m for m in (history, paths, resources, switch, runtime, locks, layout)}
for name in ('camctl', 'camctl.history', 'camctl.host_files', 'camctl.persistence',
             'camctl.session', 'camctl.update_engine'):
    package = module(name)
    package.__path__ = []
    modules[name] = package
modules[spec.name] = initialization
with patch.dict(sys.modules, modules):
    spec.loader.exec_module(initialization)


def test_changed_layout_is_rejected_before_writing_or_locking():
    config = SimpleNamespace(paths=SimpleNamespace(staging='/staging', ready='/ready', processing='/processing'))
    with patch.object(Path, 'resolve', lambda path: path), patch.object(Path, 'mkdir') as mkdir, \
            patch.object(initialization, 'ensure_runtime_library'), \
            patch.object(initialization, 'check_initialization', side_effect=ValueError('layout mismatch')) as check, \
            patch.object(initialization, 'acquire_session') as acquire:
        result = initialization.initialize_state(config, Path('/new/state.db'))
    assert result.outcome is initialization.InitOutcome.FAILED
    check.assert_called_once()
    mkdir.assert_not_called()
    acquire.assert_not_called()


def test_matching_layout_proceeds_under_existing_session_lease():
    config = SimpleNamespace(paths=SimpleNamespace(staging='/staging', ready='/ready', processing='/processing'))
    lease = Mock()
    expected = initialization.InitResult(initialization.InitOutcome.VERIFIED_EXISTING, 'verified')
    with patch.object(Path, 'resolve', lambda path: path), patch.object(Path, 'mkdir'), \
            patch.object(initialization, 'ensure_runtime_library'), \
            patch.object(initialization, 'check_initialization') as check, \
            patch.object(initialization, 'check_distinct_roots', return_value=None), \
            patch.object(initialization, 'check_atomic_move_support', return_value=None), \
            patch.object(initialization, 'lock_file_paths', return_value=(Path('/session'), Path('/admission'))), \
            patch.object(initialization, 'acquire_session', return_value=lease), \
            patch.object(initialization, '_initialize_under_lease', return_value=expected) as initialize:
        result = initialization.initialize_state(config, Path('/state/db'))
    assert result is expected
    check.assert_called_once()
    initialize.assert_called_once()
    lease.close.assert_called_once()
