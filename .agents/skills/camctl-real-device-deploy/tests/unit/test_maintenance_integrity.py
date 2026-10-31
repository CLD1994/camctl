"""SQLite 维护检查仅消费隔离的连接契约。"""
import importlib.util
from pathlib import Path
from unittest.mock import Mock
import unittest

SOURCE = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/persistence/maintenance_integrity.py'
spec = importlib.util.spec_from_file_location('maintenance_integrity_under_test', SOURCE)
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


class IntegrityTests(unittest.TestCase):
    def test_valid_database_checks_integrity_and_actual_foreign_keys(self):
        connection = Mock()
        connection.execute.side_effect = [Mock(fetchall=Mock(return_value=[('ok',)])),
                                          Mock(fetchone=Mock(return_value=None))]
        maintenance.verify_maintenance_integrity(connection)
        self.assertEqual([item.args[0] for item in connection.execute.call_args_list],
                         ['PRAGMA integrity_check', 'PRAGMA foreign_key_check'])

    def test_rejects_structural_failure_or_suspended_foreign_key(self):
        for rows, foreign_key in [([('damage',)], None), ([('ok',)], ('child', 1, 'parent', 0))]:
            with self.subTest(rows=rows, foreign_key=foreign_key):
                connection = Mock()
                connection.execute.side_effect = [Mock(fetchall=Mock(return_value=rows)),
                    Mock(fetchone=Mock(return_value=foreign_key))]
                with self.assertRaises(ValueError):
                    maintenance.verify_maintenance_integrity(connection)
