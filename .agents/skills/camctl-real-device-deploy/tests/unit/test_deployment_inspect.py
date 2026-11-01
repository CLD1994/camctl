"""部署检查与新数据库准备的协作者边界。"""
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


@patch.object(Path, 'home', new=lambda: Path('/home/unit-user'))
class DeploymentInspectionTests(unittest.TestCase):
    def setUp(self):
        module = ModuleType('camctl.persistence.maintenance_integrity')
        module.verify_maintenance_integrity = Mock()
        patcher = patch.dict(sys.modules, {'camctl.persistence.maintenance_integrity': module})
        patcher.start()
        self.addCleanup(patcher.stop)
    def test_configuration_inspection_uses_formal_loader(self):
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db', staging='/stage',
            ready='/ready', processing='/processing'))
        application = ModuleType('camctl.bootstrap.application')
        adapter = SimpleNamespace(load=Mock(return_value=config))
        application.ConfigAdapter = Mock(return_value=adapter)
        with patch.dict(sys.modules, {'camctl.bootstrap.application': application}), \
                patch.object(Path, 'resolve', lambda path: path):
            result = deployment.inspect_configuration(Path('/candidate.toml'))
        self.assertEqual(result, {'state_db': '/state.db',
                                  'paths': {'staging': '/stage', 'ready': '/ready', 'processing': '/processing'}})
        adapter.load.assert_called_once_with(Path('/candidate.toml'))
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


    def test_new_database_uses_initialization_authority_and_distinct_identity(self):
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db', staging='/stage',
            ready='/ready', processing='/processing'), database=SimpleNamespace(busy_timeout_ms=100))
        application = ModuleType('camctl.bootstrap.application')
        application.ConfigAdapter = Mock(return_value=SimpleNamespace(load=Mock(return_value=config)))
        initialization = ModuleType('camctl.persistence.initialization')
        initialization._build_initial_database = Mock()
        initialization._fsync_directory = Mock()
        integrity = ModuleType('camctl.persistence.maintenance_integrity')
        integrity.verify_maintenance_integrity = Mock()
        runtime = ModuleType('camctl.persistence.runtime')
        runtime.DbConfig = Mock()
        runtime.DbOpenMode = SimpleNamespace(EXISTING_RW=object())
        runtime.configured_directory_binding = Mock(side_effect=lambda path, name: path)
        runtime.verify_directory_binding = Mock()
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (0, 0, 0)
        runtime.open_existing = Mock(return_value=SimpleNamespace(connection=connection, metadata=Metadata('new', 9)))
        modules = {'camctl.bootstrap.application': application, 'camctl.persistence.initialization': initialization,
                   'camctl.persistence.maintenance_integrity': integrity, 'camctl.persistence.runtime': runtime}
        with patch.dict(sys.modules, modules), patch.object(Path, 'resolve', lambda path: path), \
                patch.object(deployment.os, 'open', return_value=41) as open_file, \
                patch.object(deployment.os, 'close') as close_file, patch.object(deployment.os, 'fsync') as sync:
            result = deployment.create_database_copy(Path('/new.toml'), Path('/transaction/new.db'))
        self.assertEqual(result, {'state_db': '/state.db', 'metadata': {'instance_id': 'new', 'format_version': 9}})
        self.assertTrue(open_file.call_args.args[1] & deployment.os.O_EXCL)
        self.assertEqual(close_file.call_count, 2)
        sync.assert_called_once_with(41)
        initialization._build_initial_database.assert_called_once_with(
            Path('/transaction/new.db'), '/stage', '/ready', '/processing')
        integrity.verify_maintenance_integrity.assert_called_once_with(connection)
        runtime.verify_directory_binding.assert_called_once()
        connection.execute.assert_called_once_with('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.close.assert_called_once()

    def test_new_database_checkpoint_busy_is_not_published(self):
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db', staging='/stage',
            ready='/ready', processing='/processing'), database=SimpleNamespace(busy_timeout_ms=100))
        application = ModuleType('camctl.bootstrap.application')
        application.ConfigAdapter = Mock(return_value=SimpleNamespace(load=Mock(return_value=config)))
        initialization = ModuleType('camctl.persistence.initialization')
        initialization._build_initial_database = Mock(); initialization._fsync_directory = Mock()
        integrity = ModuleType('camctl.persistence.maintenance_integrity')
        integrity.verify_maintenance_integrity = Mock()
        runtime = ModuleType('camctl.persistence.runtime')
        runtime.DbConfig = Mock(); runtime.DbOpenMode = SimpleNamespace(EXISTING_RW=object())
        runtime.configured_directory_binding = Mock(side_effect=lambda path, name: path)
        runtime.verify_directory_binding = Mock()
        connection = Mock(); connection.execute.return_value.fetchone.return_value = (1, 2, 1)
        runtime.open_existing = Mock(return_value=SimpleNamespace(connection=connection, metadata=Metadata('new', 9)))
        modules = {'camctl.bootstrap.application': application, 'camctl.persistence.initialization': initialization,
                   'camctl.persistence.maintenance_integrity': integrity, 'camctl.persistence.runtime': runtime}
        with patch.dict(sys.modules, modules), patch.object(Path, 'resolve', lambda path: path), \
                patch.object(deployment.os, 'open', return_value=41), patch.object(deployment.os, 'close'), \
                patch.object(deployment.os, 'fsync') as sync:
            with self.assertRaises(ValueError):
                deployment.create_database_copy(Path('/new.toml'), Path('/transaction/new.db'))
        connection.close.assert_called_once()
        sync.assert_not_called()

    def test_new_database_cannot_overwrite_configured_database(self):
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db'))
        application = ModuleType('camctl.bootstrap.application')
        application.ConfigAdapter = Mock(return_value=SimpleNamespace(load=Mock(return_value=config)))
        initialization = ModuleType('camctl.persistence.initialization')
        initialization._build_initial_database = Mock()
        initialization._fsync_directory = Mock()
        runtime = ModuleType('camctl.persistence.runtime')
        for name in ('DbConfig', 'DbOpenMode', 'configured_directory_binding', 'open_existing', 'verify_directory_binding'):
            setattr(runtime, name, Mock())
        with patch.dict(sys.modules, {'camctl.bootstrap.application': application,
                                     'camctl.persistence.initialization': initialization,
                                     'camctl.persistence.runtime': runtime}), \
                patch.object(Path, 'resolve', lambda path: path), patch.object(deployment.os, 'open') as open_file:
            with self.assertRaises(ValueError):
                deployment.create_database_copy(Path('/new.toml'), Path('/state.db'))
        open_file.assert_not_called()

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


@patch.object(Path, 'home', new=lambda: Path('/home/unit-user'))
class DeploymentMigrationTests(unittest.TestCase):
    def setUp(self):
        module = ModuleType('camctl.persistence.maintenance_integrity')
        module.verify_maintenance_integrity = Mock()
        patcher = patch.dict(sys.modules, {'camctl.persistence.maintenance_integrity': module})
        patcher.start()
        self.addCleanup(patcher.stop)
    def test_copy_cannot_be_original_database_even_via_an_alias(self):
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db'))
        app = ModuleType('camctl.bootstrap.application')
        app.ConfigAdapter = Mock(return_value=SimpleNamespace(load=Mock(return_value=config)))
        runtime = ModuleType('camctl.persistence.runtime')
        runtime.DbConfig = Mock()
        runtime.DbOpenMode = SimpleNamespace(EXISTING_RO=object())
        runtime._FORMAT_VERSIONS = SimpleNamespace(V9=9)
        runtime.CURRENT_FORMAT_VERSION = 9
        runtime.verify_directory_binding = Mock()
        runtime.open_existing = Mock()
        with patch.dict(sys.modules, {'camctl.bootstrap.application': app, 'camctl.persistence.runtime': runtime}), \
                patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'samefile', return_value=True):
            with self.assertRaises(ValueError):
                deployment.migrate_copy(Path('/config'), Path('/alias'))
        runtime.open_existing.assert_not_called()

    def test_copy_rejects_integrity_failure_and_closes_connection(self):
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db', staging='/s', ready='/r', processing='/p'),
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
        integrity = sys.modules['camctl.persistence.maintenance_integrity']
        integrity.verify_maintenance_integrity.side_effect = ValueError('foreign key failure')
        with patch.dict(sys.modules, {'camctl.bootstrap.application': app, 'camctl.persistence.runtime': runtime}), \
                patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'samefile', return_value=False):
            with self.assertRaises(ValueError):
                deployment.migrate_copy(Path('/config'), Path('/copy'))
        connection.close.assert_called_once()

    def test_copy_accepts_baseline_and_rejects_unsupported_formats(self):
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db', staging='/s', ready='/r', processing='/p'),
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
                patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'samefile', return_value=False):
            self.assertEqual(deployment.migrate_copy(Path('/config'), Path('/copy')), {'instance_id': 'x', 'format_version': 9})
            runtime.open_existing.return_value.metadata = Metadata('x', 8)
            with self.assertRaises(ValueError):
                deployment.migrate_copy(Path('/config'), Path('/copy'))
        self.assertEqual(connection.close.call_count, 2)

    def test_copy_rejects_missing_path_to_current_format(self):
        config = SimpleNamespace(paths=SimpleNamespace(state_db='/state.db', staging='/s', ready='/r', processing='/p'),
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
                patch.object(Path, 'is_file', return_value=True), patch.object(Path, 'samefile', return_value=False):
            with self.assertRaises(ValueError):
                deployment.migrate_copy(Path('/config'), Path('/copy'))


if __name__ == '__main__':
    unittest.main()
