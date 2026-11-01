"""实际日志位置登记的内存协作者边界。"""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import MagicMock, Mock, patch

layout = ModuleType('camctl.update_engine.layout')
layout.environment_locator = lambda environment: Path(environment) / '.locator'
layout.read_reference = Mock()
config_paths = ModuleType('camctl.config_paths')
config_paths.log_registration_id = lambda path: 'path-id'
source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/log_registry.py'
spec = importlib.util.spec_from_file_location('log_registry_under_test', source)
registry = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'camctl.update_engine.layout': layout, 'camctl.config_paths': config_paths}):
    spec.loader.exec_module(registry)


def test_unregistered_environment_creates_no_records():
    layout.read_reference.return_value = None
    with patch.object(Path, 'resolve', lambda path: path), patch.object(Path, 'mkdir') as mkdir:
        assert not registry.register('/logs/live.log', None, environment='/venv')
    mkdir.assert_not_called()


def test_log_location_is_atomically_recorded_by_actual_path():
    layout.read_reference.return_value = Path('/maintenance/layout.json')
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.fileno.return_value = 12
    with patch.object(Path, 'resolve', lambda path: path), patch.object(Path, 'read_text',
            return_value=json.dumps({'environment': '/venv'})), patch.object(Path, 'mkdir'), \
            patch.object(registry.os, 'open', return_value=11), patch.object(registry.os, 'close'), \
            patch.object(registry.os, 'fsync'), patch.object(registry.os, 'fdopen', return_value=stream), \
            patch.object(registry.os, 'replace') as replace, patch.object(registry.os, 'unlink'), \
            patch.object(registry.tempfile, 'mkstemp', return_value=(12, '/maintenance/logs/.temp')):
        assert registry.register('/logs/live.log', '/current.toml', environment='/venv')
    assert replace.call_args.args[0] == '/maintenance/logs/.temp'
    assert replace.call_args.args[1].parent == Path('/maintenance/logs')
    rendered = ''.join(call.args[0] for call in stream.write.call_args_list)
    assert json.loads(rendered) == {'path': '/logs/live.log', 'config_source': '/current.toml'}
