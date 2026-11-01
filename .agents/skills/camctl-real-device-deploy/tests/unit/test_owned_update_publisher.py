"""发布器在入口之前发布 wheel 内标准库资源的隔离单元。"""
import importlib.util
import json
from pathlib import Path
import sys
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
with patch.dict(sys.modules, {'camctl': camctl, 'camctl.operations': operations, 'camctl.update_engine': engine,
                              'camctl.operations.error_diagnostics': diagnostics,
                              'camctl.update_engine.diagnostic_fallback': fallback}):
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
            patch.object(publisher.subprocess, 'run', side_effect=calls), \
            patch.object(publisher, '_publish_file') as publish:
        result = publisher.publish('/new.whl', Path('/target'), '/python',
                                           '/target/venv/bin/camctl', '/config')
    assert result == Path('/target/maintenance/camctl-updater')
    names = [call.args[0].name for call in publish.call_args_list]
    assert names[-1] == 'camctl-updater'
    assert names.index('archive_support.py') < names.index('camctl-updater')
    assert names.index('bundle_support.py') < names.index('camctl-updater')
    assert names.index('error_diagnostics.py') < names.index('camctl-updater')
    assert names.index('binding.json') < names.index('camctl-updater')
    wheel.read.assert_any_call('camctl/update_engine/bundle_support.py')
    transaction.validate_paths.assert_called_once()
    flock.assert_called_once()
    close.assert_called_once_with(31)
