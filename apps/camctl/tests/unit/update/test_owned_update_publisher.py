"""发布器在入口之前发布 wheel 内标准库资源的隔离单元。"""
import importlib.util
import json
from pathlib import Path
import sys
import stat
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/publisher.py'
spec = importlib.util.spec_from_file_location('owned_update_publisher_under_test', source)
publisher = importlib.util.module_from_spec(spec)
camctl = ModuleType('camctl'); camctl.__path__ = []
operations = ModuleType('camctl.operations'); operations.__path__ = []
engine = ModuleType('camctl.update_engine'); engine.__path__ = []
diagnostics = ModuleType('camctl.operations.error_diagnostics'); diagnostics.exception_diagnostic = Mock()
fallback = ModuleType('camctl.update_engine.diagnostic_fallback'); fallback.freeze = Mock()
layout = ModuleType('camctl.update_engine.layout')
layout.environment_locator = lambda environment: Path(environment) / '.camctl-maintenance-layout'
layout.database_locator = lambda db: Path(str(db) + '.camctl-maintenance-layout')
with patch.dict(sys.modules, {'camctl': camctl, 'camctl.operations': operations, 'camctl.update_engine': engine,
                              'camctl.operations.error_diagnostics': diagnostics,
                              'camctl.update_engine.diagnostic_fallback': fallback,
                              'camctl.update_engine.layout': layout}):
    spec.loader.exec_module(publisher)


def test_wheel_resources_are_published_before_executable_entry():
    wheel, embedded = MagicMock(), MagicMock()
    wheel.read.return_value = b'resource'
    embedded.read.return_value = b'probe'
    wheel_context, engine_context = MagicMock(), MagicMock()
    wheel_context.__enter__.return_value = wheel
    engine_context.__enter__.return_value = embedded
    transaction = Mock()
    transaction.snapshot_config.return_value = {'present': False}
    config = {'config': '/config', 'state_db': '/state', 'lock_paths': ['/lock/a', '/lock/b'],
              'paths': {'staging': '/staging', 'ready': '/ready', 'processing': '/processing'}}
    calls = [SimpleNamespace(returncode=0, stderr=b''),
             SimpleNamespace(returncode=0, stdout=json.dumps(config).encode(), stderr=b'')]
    with patch.dict(publisher.os.environ, {}, clear=True), \
            patch.object(Path, 'resolve', new=lambda path: path), \
            patch.object(Path, 'samefile', return_value=True), \
            patch.object(Path, 'lstat', side_effect=FileNotFoundError()), \
            patch.object(Path, 'mkdir'), patch.object(publisher.os, 'urandom', return_value=b'fixed'), \
            patch.object(publisher.os, 'open', return_value=31), \
            patch.object(publisher.fcntl, 'flock') as flock, patch.object(publisher.os, 'close') as close, \
            patch.object(publisher, '_module_from_wheel', return_value=transaction), \
            patch.object(publisher.zipfile, 'ZipFile', side_effect=[wheel_context, engine_context]), \
            patch.object(publisher.subprocess, 'run', side_effect=calls) as run, \
            patch.object(publisher, '_publish_file') as publish, \
            patch.object(publisher, '_publish_launcher') as launcher:
        result = publisher.publish('/new.whl', Path('/target'), '/python',
                                           '/target/venv/bin/camctl', '/config')
    assert result == Path('/target/maintenance/camctl-updater')
    names = [call.args[0].name for call in publish.call_args_list]
    assert 'archive_support.py' in names
    assert 'bundle_support.py' in names
    assert 'error_diagnostics.py' in names
    assert 'binding.json' in names
    assert 'layout.json' in names
    assert names.count('.camctl-maintenance-layout') == 1
    assert 'state.camctl-maintenance-layout' in names
    launcher.assert_called_once()
    assert run.call_args_list[-1].args[0][-1] == 'inspect'
    wheel.read.assert_any_call('camctl/update_engine/bundle_support.py')
    transaction.validate_paths.assert_called_once()
    flock.assert_called_once()
    close.assert_called_once_with(31)


def test_existing_launcher_is_replaced_only_after_resources_are_synced():
    directory = Path('/target/maintenance')
    launcher = directory / 'camctl-updater'
    binding = {'target': '/target', 'python': '/python', 'camctl_path': '/target/venv/bin/camctl'}
    original = publisher._launcher('/python', directory / 'router.py')
    updated = publisher._launcher('/python', directory / 'versions/new/router.py')
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o700, st_uid=123, st_nlink=1)
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.fileno.return_value = 41
    with patch.object(Path, 'lstat', return_value=info), patch.object(Path, 'read_bytes', return_value=original), \
            patch.object(Path, 'read_text', return_value=json.dumps(binding)), \
            patch.object(publisher.os, 'geteuid', return_value=123), \
            patch.object(publisher.tempfile, 'mkstemp', return_value=(41, '/target/maintenance/.tmp')), \
            patch.object(publisher.os, 'fdopen', return_value=stream), \
            patch.object(publisher.os, 'fchmod') as chmod, patch.object(publisher.os, 'fsync') as fsync, \
            patch.object(publisher.os, 'replace') as replace, patch.object(publisher.os, 'unlink'):
        publisher._publish_launcher(launcher, updated, '/python', directory, binding)
    stream.write.assert_called_once_with(updated)
    chmod.assert_called_once_with(41, 0o700)
    fsync.assert_called_once_with(41)
    replace.assert_called_once_with('/target/maintenance/.tmp', launcher)


def test_unrecognized_existing_launcher_is_not_replaced():
    directory = Path('/target/maintenance')
    binding = {'target': '/target', 'python': '/python', 'camctl_path': '/target/venv/bin/camctl'}
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o700, st_uid=123, st_nlink=1)
    with patch.object(Path, 'lstat', return_value=info), patch.object(Path, 'read_bytes', return_value=b'unknown'), \
            patch.object(publisher.os, 'geteuid', return_value=123), \
            patch.object(publisher.os, 'replace') as replace, \
            patch.object(publisher.tempfile, 'mkstemp') as temporary:
        try:
            publisher._publish_launcher(directory / 'camctl-updater', b'new', '/python', directory, binding)
        except ValueError:
            pass
        else:
            raise AssertionError('应拒绝无法核实的维护入口')
    temporary.assert_not_called()
    replace.assert_not_called()
