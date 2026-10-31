"""只读部署检查的公共协作者边界。"""
import contextlib
from dataclasses import dataclass
import io
import json
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/deployment.py'
spec = importlib.util.spec_from_file_location('deployment_under_test', SOURCE)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


@dataclass
class Metadata:
    instance_id: str
    format_version: int


class DeploymentInspectionTests(unittest.TestCase):
    def test_reads_existing_database_and_checks_binding_then_closes(self):
        connection = Mock()
        metadata = Metadata('original', 9)
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db', staging='/stage',
            ready='/ready', processing='/processing'), database=SimpleNamespace(busy_timeout_ms=9000))
        adapter = Mock()
        adapter.load.return_value = config
        application = ModuleType('camctl.bootstrap.application')
        application.ConfigAdapter = Mock(return_value=adapter)
        runtime = ModuleType('camctl.persistence.runtime')
        runtime.DbConfig = Mock(side_effect=lambda **kwargs: kwargs)
        readonly = object()
        runtime.DbOpenMode = SimpleNamespace(EXISTING_RO=readonly)
        runtime.open_existing = Mock(return_value=SimpleNamespace(connection=connection, metadata=metadata))
        runtime.verify_directory_binding = Mock()
        modules = {'camctl.bootstrap.application': application, 'camctl.persistence.runtime': runtime}
        with patch.dict(sys.modules, modules), patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path):
            result = deployment.inspect_deployment(Path('/config.toml'))
        self.assertEqual(result, {'state_db': '/state.db',
            'metadata': {'instance_id': 'original', 'format_version': 9}})
        runtime.open_existing.assert_called_once_with(Path('/state.db'), readonly, {'busy_timeout_ms': 9000})
        runtime.verify_directory_binding.assert_called_once_with(metadata, '/stage', '/ready', '/processing')
        connection.close.assert_called_once()

    def test_main_sends_full_cause_to_stderr_without_stdout(self):
        encoder_spec = importlib.util.spec_from_file_location('error_diagnostics_for_deployment',
            Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/operations/error_diagnostics.py')
        encoder = importlib.util.module_from_spec(encoder_spec)
        encoder_spec.loader.exec_module(encoder)
        try:
            try:
                raise OSError(5, 'database failure')
            except OSError as cause:
                raise ValueError('inspection failed') from cause
        except ValueError as error:
            original = error
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {'camctl.operations.error_diagnostics': encoder}), \
                patch.object(sys, 'argv', ['deployment', '--config', '/config.toml']), \
                patch.object(deployment, 'inspect_deployment', side_effect=original), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as result:
                deployment.main()
        self.assertEqual(result.exception.code, 1)
        self.assertEqual(stdout.getvalue(), '')
        diagnostic = json.loads(stderr.getvalue())
        self.assertEqual(diagnostic['message'], 'inspection failed')
        self.assertEqual(diagnostic['cause']['errno'], 5)

    def test_binding_failure_closes_connection(self):
        connection = Mock()
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db', staging='/s',
            ready='/r', processing='/p'), database=SimpleNamespace(busy_timeout_ms=100))
        app = ModuleType('camctl.bootstrap.application')
        app.ConfigAdapter = Mock(return_value=SimpleNamespace(load=Mock(return_value=config)))
        runtime = ModuleType('camctl.persistence.runtime')
        runtime.DbConfig = Mock()
        runtime.DbOpenMode = SimpleNamespace(EXISTING_RO=object())
        runtime.open_existing = Mock(return_value=SimpleNamespace(connection=connection, metadata=Metadata('x', 9)))
        runtime.verify_directory_binding = Mock(side_effect=ValueError('绑定不一致'))
        with patch.dict(sys.modules, {'camctl.bootstrap.application': app, 'camctl.persistence.runtime': runtime}), \
                patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path):
            with self.assertRaisesRegex(ValueError, '绑定不一致'):
                deployment.inspect_deployment(Path('/config.toml'))
        connection.close.assert_called_once()


class DeploymentMigrationTests(unittest.TestCase):
    def test_copy_accepts_baseline_and_rejects_unsupported_formats(self):
        config = SimpleNamespace(paths=SimpleNamespace(staging='/s', ready='/r', processing='/p'),
            database=SimpleNamespace(busy_timeout_ms=100))
        app = ModuleType('camctl.bootstrap.application')
        app.ConfigAdapter = Mock(return_value=SimpleNamespace(load=Mock(return_value=config)))
        runtime = ModuleType('camctl.persistence.runtime')
        runtime.DbConfig = Mock()
        runtime.DbOpenMode = SimpleNamespace(EXISTING_RO=object())
        runtime._FORMAT_VERSIONS = SimpleNamespace(V9=9)
        runtime.CURRENT_FORMAT_VERSION = 9
        runtime.verify_directory_binding = Mock()
        connection = Mock()
        runtime.open_existing = Mock(return_value=SimpleNamespace(connection=connection, metadata=Metadata('x', 9)))
        with patch.dict(sys.modules, {'camctl.bootstrap.application': app, 'camctl.persistence.runtime': runtime}), \
                patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(deployment.migrate_copy(Path('/config'), Path('/copy')), {'instance_id': 'x', 'format_version': 9})
            runtime.open_existing.return_value.metadata = Metadata('x', 8)
            with self.assertRaises(ValueError):
                deployment.migrate_copy(Path('/config'), Path('/copy'))
        self.assertEqual(connection.close.call_count, 2)

    def test_copy_rejects_missing_path_to_current_format(self):
        config = SimpleNamespace(paths=SimpleNamespace(staging='/s', ready='/r', processing='/p'),
            database=SimpleNamespace(busy_timeout_ms=100))
        app = ModuleType('camctl.bootstrap.application')
        app.ConfigAdapter = Mock(return_value=SimpleNamespace(load=Mock(return_value=config)))
        runtime = ModuleType('camctl.persistence.runtime')
        runtime.DbConfig = Mock()
        runtime.DbOpenMode = SimpleNamespace(EXISTING_RO=object())
        runtime._FORMAT_VERSIONS = SimpleNamespace(V9=9)
        runtime.CURRENT_FORMAT_VERSION = 10
        runtime.verify_directory_binding = Mock()
        runtime.open_existing = Mock(return_value=SimpleNamespace(connection=Mock(), metadata=Metadata('x', 9)))
        with patch.dict(sys.modules, {'camctl.bootstrap.application': app, 'camctl.persistence.runtime': runtime}), \
                patch.object(Path, 'is_file', return_value=True):
            with self.assertRaises(ValueError):
                deployment.migrate_copy(Path('/config'), Path('/copy'))


if __name__ == '__main__':
    unittest.main()
