"""组件维护事务在受控边界下的提交、恢复和诊断责任。"""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sys
from enum import Enum
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

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
bundle.update_metadata = Mock(return_value={'force': False})
archive = ModuleType('archive_support'); archive.UpdateArchive = Mock()
integrity = ModuleType('maintenance_integrity'); integrity.verify_maintenance_integrity = Mock()
transaction = ModuleType('update_transaction')
config_paths = ModuleType('config_paths')
config_paths.log_registration_id = Mock()
config_paths.rotated_log_names = Mock()
config_paths.default_config_path = Mock()
config_paths.lock_file_paths = Mock()
force_record = ModuleType('force_record')
force_snapshot = ModuleType('force_snapshot')
class Locks:
    def __init__(self):
        self.fds = []
        self.database = Mock()
        self.close = Mock()
transaction.Locks = Locks
transaction.snapshot_config = Mock(return_value={'present': False})
class JournalState(Enum):
    PREPARING = 'preparing'
    UPDATING = 'updating'
    COMMITTED = 'committed'
    ROLLED_BACK = 'rolled_back'
transaction.JournalState = JournalState
spec = importlib.util.spec_from_file_location('owned_update_engine_under_test', engine_dir / 'engine.py')
engine = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'bundle_support': bundle, 'archive_support': archive,
                              'maintenance_integrity': integrity, 'update_transaction': transaction,
                              'error_diagnostics': encoder, 'diagnostic_fallback': fallback,
                              'config_paths': config_paths, 'force_record': force_record,
                              'force_snapshot': force_snapshot}), \
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
    for method in ('write', 'flush'):
        ops = operations()
        ops.install.side_effect = ValueError('original failure')
        ops.record_diagnostics.side_effect = OSError('log unavailable')
        stream = Mock(spec=['write', 'flush'])
        getattr(stream, method).side_effect = BrokenPipeError('stderr unavailable')
        with patch.object(sys, 'stderr', stream):
            result = engine.perform_update(ops)
        assert result['rollback'] == 'verified'
        assert result['stderr_error']['stage'] == 'diagnostic_stderr'
        assert result['diagnostics'][0]['message'] == 'original failure'
        ops.unlock.assert_called_once()
        assert ops.mock_calls[-1][0] == 'save'


def test_backup_failure_never_installs_or_attempts_rollback():
    ops = operations()
    ops.backup.side_effect = OSError('backup failure')
    result = engine.perform_update(ops)
    assert result['status'] == 'failed'
    ops.mark_intent.assert_not_called()
    ops.install.assert_not_called()
    ops.rollback.assert_not_called()


def test_force_missing_lock_parent_fails_before_backup_or_mutation():
    ops = Mock(spec=engine.ForceOperations)
    ops.preflight.return_value = {'state_db': '/missing/state.db'}
    ops.lock.side_effect = FileNotFoundError('/missing/state.db.session.lock')
    result = engine.perform_update(ops)
    assert result['status'] == 'failed'
    assert result['diagnostics'][0]['stage'] == 'lock'
    ops.prepare_new.assert_not_called()
    ops.backup.assert_not_called()
    ops.mark_intent.assert_not_called()


def test_finalize_archives_derives_journal_only_after_confirmed_commit():
    token = 'a' * 32
    output = Path('/target/updates') / token
    pending = {'state': 'committed', 'output': str(output), 'mode': 'force'}
    frozen = dict(pending, state='updating')
    job = {'record': frozen}
    force_archive = ModuleType('force_archive')
    force_archive.finalize = Mock()
    import fcntl
    def read(path, **kwargs):
        del kwargs
        if path == output / 'archive-job.json':
            return json.dumps(job)
        if path == output / 'journal.json':
            raise FileNotFoundError(str(path))
        if path == Path('/target/updates/pending.json'):
            return json.dumps(pending)
        raise AssertionError(str(path))
    with patch.dict(sys.modules, {'force_archive': force_archive}), \
            patch.object(Path, 'read_text', read), patch.object(engine.force_record, 'validate_target', create=True), \
            patch.object(engine.force_record, 'classify', create=True), \
            patch.object(engine.transaction, 'classify_record', return_value=JournalState.COMMITTED, create=True), \
            patch.object(engine.transaction, 'validate_record_target', create=True), \
            patch.object(engine.transaction, 'sync_file', create=True), \
            patch.object(engine.transaction, 'sync_dir', create=True), \
            patch.object(engine.transaction, 'atomic_json', create=True) as atomic, \
            patch.object(engine.os, 'open', return_value=11), patch.object(engine.os, 'close'), \
            patch.object(fcntl, 'flock'):
        engine.finalize_archives(Path('/target'), output)
    atomic.assert_called_once_with(output / 'journal.json', pending)
    force_archive.finalize.assert_called_once_with(pending)


def test_force_commit_publishes_only_pending_authority():
    with patch.dict(engine.os.environ, {}, clear=True), patch.object(Path, 'is_dir', return_value=True):
        ops = engine.ForceOperations(Path('/bundle'), Path('/target'), None, Path('/target/updates/id'))
    ops.record = {'state': 'updating', 'mutation_intent': True}
    with patch.object(engine.transaction, 'atomic_json', create=True) as atomic:
        ops.commit()
    atomic.assert_called_once_with(Path('/target/updates/pending.json'),
                                   {'state': 'committed', 'mutation_intent': True})


def test_finalize_never_treats_uncommitted_pending_as_success():
    token = 'a' * 32
    output = Path('/target/updates') / token
    pending = {'state': 'updating', 'output': str(output), 'mode': 'force'}
    job = {'record': dict(pending)}
    archive_module = ModuleType('force_archive')
    archive_module.finalize = Mock()
    import fcntl
    def read(path, **kwargs):
        del kwargs
        if path == output / 'archive-job.json': return json.dumps(job)
        if path == output / 'journal.json': raise FileNotFoundError(str(path))
        if path == Path('/target/updates/pending.json'): return json.dumps(pending)
        raise AssertionError(str(path))
    with patch.dict(sys.modules, {'force_archive': archive_module}), \
            patch.object(Path, 'read_text', read), patch.object(engine.force_record, 'validate_target', create=True), \
            patch.object(engine.force_record, 'classify', create=True), \
            patch.object(engine.transaction, 'classify_record', return_value=JournalState.UPDATING, create=True), \
            patch.object(engine.transaction, 'sync_file', create=True), \
            patch.object(engine.transaction, 'sync_dir', create=True), \
            patch.object(engine.transaction, 'atomic_json', create=True) as atomic, \
            patch.object(engine.os, 'open', return_value=11), patch.object(engine.os, 'close'), \
            patch.object(fcntl, 'flock'):
        engine.finalize_archives(Path('/target'), output)
    atomic.assert_not_called()
    archive_module.finalize.assert_not_called()


def test_force_installs_configuration_at_frozen_symlink_target():
    with patch.dict(engine.os.environ, {}, clear=True), patch.object(Path, 'is_dir', return_value=True):
        ops = engine.ForceOperations(Path('/bundle'), Path('/target'), None, Path('/target/updates/id'),
                                     config_path=Path('/link/config'))
    ops.objects = {'configuration': '/actual/config', 'staging': '/stage', 'ready': '/ready'}
    ops.layout = {'state_db': '/state/db', 'staging': '/stage', 'ready': '/ready', 'processing': '/processing'}
    ops.processing_identity = {'dev': 1}
    ops.record = {'namespaces': {}}
    ops.environment_locator = Path('/target/venv/.locator')
    ops.update = {'force': True, 'config_toml': '[paths]\n'}
    ops.run = Mock(return_value=json.dumps({'state_db': '/state/db', 'paths': {
        'staging': '/stage', 'ready': '/ready', 'processing': '/processing'}}))
    ops.install_environment = Mock()
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.__exit__.return_value = False
    stream.fileno.return_value = 9
    opened = Mock(return_value=stream)
    def open_path(path, *args, **kwargs):
        return opened(path, *args, **kwargs)
    with patch.object(Path, 'resolve', return_value=Path('/actual/config')), \
            patch.object(Path, 'open', open_path), patch.object(Path, 'mkdir'), patch.object(Path, 'write_text'), \
            patch.object(engine.force_snapshot, 'displace', create=True), \
            patch.object(engine.force_snapshot, 'ensure_directories', create=True), \
            patch.object(engine.transaction, 'directory_identity', return_value={'dev': 1}, create=True), \
            patch.object(engine.transaction, 'make_directories', create=True), \
            patch.object(engine.transaction, 'sync_file', create=True), \
            patch.object(engine.transaction, 'sync_dir', create=True), patch.object(engine.os, 'fsync'):
        ops.install({'steps': []})
    assert opened.call_args.args[:2] == (Path('/actual/config'), 'x')
    stream.write.assert_called_once_with('[paths]\n')


def test_intent_write_failure_uses_confirmed_persistent_record():
    for state, should_restore in (('preparing', False), ('updating', True)):
        ops = operations()
        ops.mark_intent.side_effect = OSError('intent failure')
        ops.confirm_pending.return_value = state
        result = engine.perform_update(ops)
        assert result['status'] == 'failed'
        assert ops.rollback.called is should_restore
        ops.install.assert_not_called()


def test_late_intent_sync_error_compares_exact_proposal_and_rolls_back():
    keys = ('output', 'output_identity', 'env', 'env_backup', 'state_db', 'db_backup', 'lock_paths',
            'controller', 'original', 'temporary', 'engine')
    original = {key: key for key in keys}
    original.update(state='preparing', mutation_intent=False, temporary_owners={})
    pending = dict(original)
    with patch.dict(engine.os.environ, {}, clear=True), patch.object(Path, 'is_dir', return_value=True):
        ops = engine.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/target/updates/id'))
    ops.record = original
    def publish(path, proposal):
        pending.clear()
        pending.update(proposal)
        raise OSError(5, 'directory sync')
    def classify(record):
        assert (record['state'], record['mutation_intent']) in (('preparing', False), ('updating', True))
        return JournalState(record['state'])
    receipt = Mock()
    ops.save = Mock(); ops.lock = Mock(); ops.check_paths = Mock(); ops.check_original = Mock()
    ops.backup = Mock(); ops.install = Mock(); ops.rollback = Mock(); ops.unlock = Mock()
    ops.record_diagnostics = Mock()
    with patch.object(engine.transaction, 'atomic_json', side_effect=publish, create=True), \
            patch.object(engine.transaction, 'classify_record', side_effect=classify, create=True), \
            patch.object(engine.transaction, 'validate_record_target', create=True), \
            patch.object(engine.transaction, 'directory_identity', return_value='output_identity', create=True), \
            patch.object(engine.transaction, 'sync_file', create=True), \
            patch.object(engine.transaction, 'sync_dir', create=True), \
            patch.object(Path, 'read_text', side_effect=lambda *args, **kwargs: json.dumps(pending)), \
            patch.object(Path, 'is_dir', return_value=True):
        result = engine.perform_update(ops, original=receipt)
    assert result['status'] == 'failed'
    assert result['rollback'] == 'verified'
    assert result['diagnostics'][0]['stage'] == 'save_install_intent'
    ops.rollback.assert_called_once()
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


def test_default_configuration_uses_absolute_path_from_original_probe():
    config = {'config': '/home/user/.config/camctl/config.toml', 'state_db': '/old/state',
              'lock_paths': ['/old/session', '/old/admission'], 'old_version': '0.2.0'}
    release = {'components': {'camctl': '0.3.0'}, 'source_sha256': 'source',
               'wheel_targets': {'x86_64': {'files': ['dependency.whl']}}}
    wheel = Mock()
    wheel.namelist.return_value = ['camctl/deployment.py', 'camctl-0.3.0.dist-info/METADATA',
                                   'camctl/persistence/maintenance_integrity.py', 'camctl/update_engine.pyz']
    wheel.read.side_effect = lambda name: (b'Name: camctl\nVersion: 0.3.0\n\n' if name.endswith('METADATA')
                                           else b'engine' if name.endswith('.pyz') else b'integrity')
    inner = Mock(); inner.read.return_value = b'integrity'
    outer_context, inner_context = Mock(), Mock()
    outer_context.__enter__ = Mock(return_value=wheel); outer_context.__exit__ = Mock(return_value=False)
    inner_context.__enter__ = Mock(return_value=inner); inner_context.__exit__ = Mock(return_value=False)
    with patch.dict(engine.os.environ, {}, clear=True), \
            patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
            patch.object(Path, 'read_text', return_value=json.dumps(release)), \
            patch.object(Path, 'read_bytes', side_effect=[b'manifest', b'engine']), \
            patch.object(Path, 'glob', return_value=iter([Path('/bundle/camctl/camctl.whl')])), \
            patch.object(engine, 'verify_checksums'), patch.object(engine, 'current_target', return_value='x86_64'), \
            patch.object(engine, 'wheel_target_manifest', return_value=release['wheel_targets']), \
            patch.object(engine.zipfile, 'ZipFile', side_effect=[outer_context, inner_context]), \
            patch.object(engine.UpdateOperations, 'probe', return_value=config), \
            patch.object(transaction, 'snapshot_config', return_value={'present': False}) as snapshot:
        ops = engine.UpdateOperations(Path('/bundle'), Path('/target'), None, Path('/output'))
        original = ops.preflight()
    assert original['config'] == config['config']
    assert Path(original['config']).is_absolute()
    snapshot.assert_called_once_with(Path(config['config']), None)
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


def test_original_facts_and_distinct_unlock_error_survive_install_failure():
    ops = operations()
    ops.preflight.return_value = {'state_db': '/state', 'old_version': '1.0'}
    ops.install.side_effect = ValueError('install error')
    ops.unlock.side_effect = OSError(5, 'unlock error')
    result = engine.perform_update(ops)
    assert result['original']['old_version'] == '1.0'
    assert result['environment_may_have_changed']
    assert [item['stage'] for item in result['diagnostics']] == ['install', 'unlock']
    assert result['diagnostics'][1]['errno'] == 5
    ops.check_new.assert_not_called()
    assert ops.record_diagnostics.call_args.args[0] == result['diagnostics']


def test_success_checks_new_deployment_before_unlock():
    ops = operations()
    result = engine.perform_update(ops)
    calls = [item[0] for item in ops.mock_calls]
    assert result['status'] == 'updated_stopped'
    assert calls.index('lock') < calls.index('install') < calls.index('check_new') < calls.index('unlock')


def test_log_failure_delivers_original_and_log_error_to_stderr():
    ops = operations()
    ops.preflight.side_effect = ValueError('original error')
    ops.record_diagnostics.side_effect = OSError(28, 'disk full')
    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        result = engine.perform_update(ops)
    delivered = json.loads(stream.getvalue())
    assert delivered['diagnostics'][0]['message'] == 'original error'
    assert delivered['log_error']['errno'] == result['log_error']['errno'] == 28


def test_rollback_relocks_and_recovers_formal_record_with_original_bindings():
    ops = create_operations()
    ops.record = {'lock_paths': ['/db/session', '/db/admission']}
    ops.update_locks, ops.db_locks = Mock(), Mock()
    active = object()
    formal = {'state': 'updating', 'temporary': {}, 'client': {'configured_path': '/client/cap.json'},
              'original': {'deployment': {'metadata': {'staging_path': '/stage',
                           'ready_path': '/ready', 'processing_path': '/processing'}}}}
    ops.read_pending = Mock(return_value=(formal, active))
    states = SimpleNamespace(UPDATING=active, ROLLED_BACK=SimpleNamespace(value='rolled_back'))
    with patch.object(transaction, 'JournalState', states, create=True), \
            patch.object(transaction, 'validate_active_record', create=True), \
            patch.object(transaction, 'validate_paths', create=True) as validate, \
            patch.object(transaction, 'recover', create=True) as recover, \
            patch.object(transaction, 'atomic_json', create=True):
        ops.rollback()
    ops.update_locks.acquire_directory.assert_called_once_with(Path('/target'))
    ops.db_locks.database.assert_called_once_with(['/db/session', '/db/admission'])
    assert recover.call_args.args[0] is formal
    assert validate.call_args.args[2] == Path('/client')
    assert validate.call_args.args[4] == ['/stage', '/ready', '/processing']
    assert formal['state'] == 'rolled_back'


def test_creator_publication_failure_preserves_formal_record():
    ops = create_operations()
    ops.record = {'temporary_owners': {'staging': []}}
    formal = {'temporary_owners': {'staging': [{'uid': 0, 'gid': 0}]}}
    ops.read_pending = Mock(return_value=(formal, object()))
    with patch.object(transaction, 'creation_identity', return_value={'uid': 0, 'gid': 0}, create=True), \
            patch.object(transaction, 'atomic_json', side_effect=OSError('sync failed'), create=True), \
            patch.object(transaction, 'sync_file', create=True), patch.object(transaction, 'sync_dir', create=True):
        try:
            ops.register_owner('staging', Path('/parent'))
        except OSError as error:
            assert str(error) == 'sync failed'
        else:
            raise AssertionError('发布错误必须交付')
    assert ops.record == formal


def test_pending_record_rejects_other_transaction_but_accepts_added_creator():
    ops = create_operations()
    base = {'output': '/output', 'output_identity': {'ino': 1}, 'env': '/target/venv',
            'env_backup': '/output/venv.backup', 'state_db': '/db/state',
            'db_backup': '/output/state.backup', 'lock_paths': ['/db/a', '/db/b'],
            'controller': '/python', 'original': {}, 'temporary': {}, 'engine': '/engine',
            'state': 'updating', 'temporary_owners': {'staging': [{'uid': 0, 'gid': 0}]}}
    ops.record = base
    formal = dict(base, temporary_owners={'staging': [{'uid': 0, 'gid': 0}, {'uid': 1, 'gid': 1}]})
    states = SimpleNamespace(PREPARING=object(), UPDATING=object())
    with patch.object(Path, 'read_text', return_value=json.dumps(formal)) as read, \
            patch.object(transaction, 'classify_record', return_value=object(), create=True), \
            patch.object(transaction, 'validate_record_target', create=True), \
            patch.object(transaction, 'directory_identity', return_value={'ino': 1}, create=True), \
            patch.object(transaction, 'JournalState', states, create=True):
        actual, _ = ops.read_pending()
        assert actual['temporary_owners'] == formal['temporary_owners']
        read.return_value = json.dumps(dict(formal, output='/other'))
        try:
            ops.read_pending()
        except ValueError:
            pass
        else:
            raise AssertionError('其他事务记录不能用于当前事务')


def test_preflight_failure_reaches_actual_log_consumer():
    ops = create_operations()
    ops.preflight = Mock(side_effect=ValueError('delivery invalid'))
    ops.save = Mock()
    stream = io.StringIO()
    with patch.object(Path, 'open', return_value=contextlib.nullcontext(stream)):
        result = engine.perform_update(ops)
    assert json.loads(stream.getvalue()) == result['diagnostics'][0]
    assert result['diagnostics'][0]['message'] == 'delivery invalid'


def test_final_receipt_failure_delivers_both_original_and_receipt_error():
    ops = operations()
    ops.install.side_effect = ValueError('install failed')
    ops.save.side_effect = [None, None, OSError(28, 'receipt full')]
    result = engine.perform_update(ops)
    assert [item['stage'] for item in result['diagnostics']] == ['install', 'save_final_receipt']
    assert ops.record_diagnostics.call_count == 2
    assert ops.record_diagnostics.call_args_list[0].args[0][0]['message'] == 'install failed'
    assert ops.record_diagnostics.call_args_list[1].args[0][0]['errno'] == 28


def test_cause_and_attached_facts_reach_consumer_after_unlock():
    ops = operations()
    try:
        try:
            raise OSError(13, 'denied')
        except OSError as cause:
            error = RuntimeError('lock failed')
            encoder.attach_diagnostic_facts(error, {'lock': {'operation': 'unlock', 'path': '/state.lock'}})
            raise error from cause
    except RuntimeError as error:
        ops.install.side_effect = error
    result = engine.perform_update(ops)
    diagnostic = result['diagnostics'][0]
    assert diagnostic['cause']['errno'] == 13
    assert diagnostic['diagnostic_facts']['lock']['path'] == '/state.lock'
    calls = [item[0] for item in ops.mock_calls]
    assert calls.index('unlock') < calls.index('record_diagnostics')
    assert ops.record_diagnostics.call_args.args[0] == result['diagnostics']


def test_entry_recovery_failure_saves_diagnostic_without_overwriting_receipt():
    args = SimpleNamespace(target=Path('/target'), client_data=None, bundle=None, output=None,
                           recover_only=True, finalize_only=False, finalize_output=None, lock_fd=7, config=None)
    parser = Mock(); parser.parse_args.return_value = args
    active = object()
    states = SimpleNamespace(COMMITTED=object(), ROLLED_BACK=object())
    metadata = {'staging_path': '/stage', 'ready_path': '/ready', 'processing_path': '/processing'}
    record = {'output': '/target/updates/old', 'output_identity': {'ino': 1},
              'original': {'deployment': {'metadata': metadata}}, 'temporary': {},
              'lock_paths': ['/db/session', '/db/admission']}
    ops = Mock(); ops.db_locks = Mock()
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(engine.argparse, 'ArgumentParser', return_value=parser), \
            patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'resolve', lambda p: p), \
            patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o100600)), \
            patch.object(Path, 'read_text', return_value=json.dumps(record)), \
            patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
            patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr), \
            patch.object(transaction, 'JournalState', states, create=True), \
            patch.object(transaction, 'classify_record', return_value=active, create=True), \
            patch.object(transaction, 'validate_record_target', create=True), \
            patch.object(transaction, 'directory_identity', return_value={'ino': 1}, create=True), \
            patch.object(transaction, 'validate_active_record', create=True), \
            patch.object(transaction, 'validate_paths', create=True), \
            patch.object(transaction, 'recover', side_effect=ValueError('restore failed'), create=True), \
            patch.object(engine, 'UpdateOperations', return_value=ops):
        assert engine.main() == 1
    ops.save.assert_not_called()
    receipt = ops.save_recovery_failure.call_args.args[0]
    assert receipt['diagnostics'][0]['stage'] == 'recover_pending'
    assert receipt['diagnostics'][0]['message'] == 'restore failed'
    assert json.loads(stdout.getvalue())['effect'] == 'recovery_required'


def test_entry_path_conflict_precedes_output_creation():
    args = SimpleNamespace(target=Path('/target'), client_data=None, bundle=Path('/bundle'),
                           output=Path('/target/updates/id'), recover_only=False, finalize_only=False,
                           finalize_output=None, lock_fd=7, config=None)
    parser = Mock(); parser.parse_args.return_value = args
    ops = Mock(); ops.preflight.return_value = {'state_db': '/db/state'}; ops.update = {'force': False}
    ops.check_paths.side_effect = ValueError('path overlap')
    with patch.object(engine.argparse, 'ArgumentParser', return_value=parser), \
            patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
            patch.object(Path, 'resolve', lambda p: p), patch.object(Path, 'lstat', side_effect=FileNotFoundError), \
            patch.object(Path, 'mkdir') as mkdir, patch.object(Path, 'touch') as touch, \
            patch.object(engine.shutil, 'which', return_value='/tools/uv'), \
            patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
            patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()), \
            patch.object(engine, 'UpdateOperations', return_value=ops):
        assert engine.main() == 1
    mkdir.assert_not_called()
    touch.assert_not_called()
    ops.check_paths.assert_called_once()


def test_entry_stderr_write_and_flush_failure_preserve_both_errors():
    for method in ('write', 'flush'):
        args = SimpleNamespace(target=Path('/target'), client_data=None, bundle=None, output=None,
                               recover_only=True, finalize_only=False, finalize_output=None, lock_fd=-1, config=None)
        parser = Mock(); parser.parse_args.return_value = args
        stderr = Mock(spec=['write', 'flush'])
        getattr(stderr, method).side_effect = BrokenPipeError('stderr failed')
        with patch.object(engine.argparse, 'ArgumentParser', return_value=parser), \
                patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'resolve', lambda p: p), \
                patch.object(sys, 'prefix', '/base'), patch.object(sys, 'base_prefix', '/base'), \
                patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', stderr), \
                patch.object(engine, 'freeze_exception', wraps=engine.freeze_exception) as freeze:
            assert engine.main() == 1
        assert [item.args[1] for item in freeze.call_args_list] == ['update_lock', 'outer_stderr']
        assert '维护资格描述符无效' in str(freeze.call_args_list[0].args[0])
