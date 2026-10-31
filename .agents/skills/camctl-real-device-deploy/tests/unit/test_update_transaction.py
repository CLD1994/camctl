"""更新记录状态及对象身份决策的隔离单元。"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).absolute().parents[2] / 'scripts/update_transaction.py'
spec = importlib.util.spec_from_file_location('update_transaction_under_test', SOURCE)
transaction = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transaction)


def record(state='preparing', intent=False):
    result = {'state': state, 'mutation_intent': intent, 'env': '/target/venv',
              'env_backup': '/target/updates/id/venv.backup', 'state_db': '/db/state',
              'db_backup': '/target/updates/id/state.backup.db', 'output': '/target/updates/id',
              'output_identity': {'dev': 1, 'ino': 2, 'uid': 3, 'gid': 4},
              'controller': '/usr/bin/python3.11', 'lock_paths': ['/db/session', '/db/admission'],
              'original': {'config': '/etc/config', 'state_db': '/db/state',
                           'lock_paths': ['/db/session', '/db/admission'],
                           'config_sha256': 'a' * 64,
                           'deployment': {'state_db': '/db/state', 'metadata': {}}}}
    result['temporary'] = transaction.temporary_paths(Path('/target'), Path('/target/updates/id'),
                                                       Path('/db/state'), None)
    if intent:
        result.update(env_manifest={'': {'kind': 'dir'}}, db_sha256='b' * 64,
                      db_meta={'uid': 1, 'gid': 2, 'mode': 0o600, 'xattrs': {}})
    return result


class JournalTests(unittest.TestCase):
    def test_legal_states_and_no_intent_recovery(self):
        for state, intent in [('preparing', False), ('updating', True), ('committed', True),
                              ('rolled_back', False), ('rolled_back', True)]:
            with self.subTest(state=state, intent=intent):
                self.assertEqual(transaction.classify_record(record(state, intent)).value, state)
        transaction.recover(record(), lambda connection: None)
        with self.assertRaises(ValueError):
            transaction.recover(record('committed', True), lambda connection: None)

    def test_missing_invalid_and_inconsistent_state_never_permit_recovery(self):
        invalid = []
        for state, intent in [('preparing', True), ('updating', False), ('committed', False),
                              ('unknown', False), (None, False), ('preparing', 0), ('updating', None)]:
            item = record()
            item.update(state=state, mutation_intent=intent)
            invalid.append(item)
        for field in ('state', 'mutation_intent'):
            item = record()
            del item[field]
            invalid.append(item)
        item = record('updating', True)
        del item['env_manifest']
        invalid.append(item)
        item = record()
        item['client'] = None
        invalid.append(item)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                transaction.recover(item, lambda connection: None)

    def test_recovery_record_is_bound_to_target(self):
        item = record('updating', True)
        transaction.validate_record_target(item, Path('/target'))
        item['env_backup'] = '/elsewhere/backup'
        with self.assertRaises(ValueError):
            transaction.validate_record_target(item, Path('/target'))

    def test_preparing_record_preserves_selected_client_without_completed_backup(self):
        for present in (False, True):
            with self.subTest(present=present):
                item = record()
                client_path = Path('/client/device-capabilities.json')
                item['client'] = {'configured_path': str(client_path), 'path': str(client_path),
                                  'present': present}
                item['temporary'] = transaction.temporary_paths(Path('/target'), Path('/target/updates/id'),
                                                               Path('/db/state'), client_path)
                self.assertEqual(transaction.classify_record(item), transaction.JournalState.PREPARING)
                with patch.object(Path, 'resolve', new=lambda path: path):
                    transaction.validate_record_target(item, Path('/target'))

    def test_backup_metadata_failure_stops_before_environment_replacement(self):
        item = record('updating', True)
        with patch.object(transaction, 'digest', return_value='a' * 64), \
                patch.object(transaction, 'manifest', return_value=item['env_manifest']), \
                patch.object(transaction, 'assert_file', side_effect=ValueError('metadata differs')), \
                patch.object(transaction, 'copy_environment') as copy:
            with self.assertRaises(ValueError):
                transaction.recover(item, lambda connection: None)
        copy.assert_not_called()

    def test_unverified_recovery_staging_is_never_removed(self):
        item = record('updating', True)
        stage = Path(item['temporary']['env_namespace']) / 'staging'
        with patch.object(transaction, 'digest', return_value='a' * 64), \
                patch.object(transaction, 'manifest', return_value=item['env_manifest']), \
                patch.object(transaction, 'assert_file'), \
                patch.object(transaction, 'verify_database_snapshot'), \
                patch.object(transaction, 'owned_namespace'), \
                patch.object(Path, 'exists', autospec=True, side_effect=lambda path: path == stage), \
                patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o120777, st_uid=1)), \
                patch.object(transaction.os, 'geteuid', return_value=1), \
                patch.object(transaction.shutil, 'rmtree') as remove:
            with self.assertRaises(ValueError):
                transaction.recover(item, lambda connection: None)
        remove.assert_not_called()

    def test_unverified_file_temporary_is_never_removed(self):
        for info in (SimpleNamespace(st_mode=0o120777, st_uid=1, st_nlink=1),
                     SimpleNamespace(st_mode=0o100600, st_uid=1, st_nlink=2),
                     SimpleNamespace(st_mode=0o100600, st_uid=2, st_nlink=1)):
            with self.subTest(info=info), patch.object(Path, 'lstat', return_value=info), \
                    patch.object(Path, 'unlink') as unlink, \
                    patch.object(transaction.os, 'geteuid', return_value=1):
                with self.assertRaises(ValueError):
                    transaction.replace_file(Path('/backup'), Path('/destination'), Path('/temporary'))
            unlink.assert_not_called()

    def test_snapshot_identity_and_integrity_are_checked_without_opening_real_database(self):
        expected = {'application_id': 'camctl', 'instance_id': 'instance', 'format_version': 9,
                    'staging_path': '/s', 'ready_path': '/r', 'processing_path': '/p'}
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = ('camctl', 'instance', 9, '/s', '/r', '/p')
        verify = Mock()
        with patch.object(Path, 'resolve', new=lambda path: path), \
                patch.object(transaction.sqlite3, 'connect', return_value=connection):
            transaction.verify_database_snapshot(Path('/backup'), expected, verify)
            connection.execute.return_value.fetchone.return_value = ('camctl', 'other', 9, '/s', '/r', '/p')
            with self.assertRaises(ValueError):
                transaction.verify_database_snapshot(Path('/backup'), expected, verify)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(connection.close.call_count, 2)


class PathIdentityTests(unittest.TestCase):
    def setUp(self):
        self.original = {'config': '/etc/config', 'state_db': '/db/state',
                         'lock_paths': ['/db/session', '/db/admission']}
        self.ids = {'/etc/config': (1, 1), '/db/state': (1, 2), '/db/session': (1, 3),
                    '/db/admission': (1, 4), '/target/venv': (1, 5),
                    '/media/staging': (2, 1), '/media/ready': (2, 2), '/media/processing': (2, 3)}
        self.bindings = ['/media/staging', '/media/ready', '/media/processing']

    def check(self, client, aliases=None, walk=None, before_lock=False):
        aliases = aliases or {}
        walk = walk or {}
        def resolve(path, *args, **kwargs):
            return Path(aliases.get(str(path), str(path)))
        def file_stat(path, *args, **kwargs):
            value = self.ids.get(str(path))
            if value is None:
                raise FileNotFoundError(str(path))
            return SimpleNamespace(st_dev=value[0], st_ino=value[1], st_mode=0o100600)
        with patch.object(Path, 'resolve', autospec=True, side_effect=resolve), \
                patch.object(Path, 'stat', autospec=True, side_effect=file_stat), \
                patch.object(Path, 'lstat', autospec=True, side_effect=file_stat), \
                patch.object(transaction.os, 'walk', side_effect=lambda path, **kwargs: iter(walk.get(str(path), ()))):
            transaction.validate_paths(Path('/target'), self.original, client,
                                       Path('/target/updates/id'), self.bindings, before_lock=before_lock)

    def test_independent_target_is_allowed_and_aliases_are_rejected(self):
        self.check(Path('/client'))
        for client, aliases in [(Path('/etc'), {'/etc/device-capabilities.json': '/etc/config'}),
                                (Path('/target/venv'), {}), (Path('/target/updates'), {})]:
            with self.subTest(client=client), self.assertRaises(ValueError):
                self.check(client, aliases)
        self.ids['/client/device-capabilities.json'] = self.ids['/db/state']
        with self.assertRaises(ValueError):
            self.check(Path('/client'))

    def test_cannot_silently_ignore_missing_protected_identity(self):
        del self.ids['/db/state']
        with self.assertRaises(ValueError):
            self.check(None)

    def test_missing_database_locks_can_be_checked_before_creation(self):
        del self.ids['/db/session']
        del self.ids['/db/admission']
        self.check(None, before_lock=True)
        with self.assertRaises(ValueError):
            self.check(Path('/db'), {'/db/device-capabilities.json': '/db/session'}, before_lock=True)

    def test_capability_hardlink_to_control_material_is_rejected(self):
        self.ids['/client/device-capabilities.json'] = (7, 8)
        self.ids['/target/updates'] = (7, 9)
        self.ids['/target/updates/id/state.backup.db'] = (7, 8)
        walk = {'/target/updates': [('/target/updates/id', [], ['state.backup.db'])]}
        with self.assertRaises(ValueError):
            self.check(Path('/client'), walk=walk)

    def test_old_fixed_names_remain_ordinary_preserved_paths(self):
        self.original['config'] = '/target/venv.camctl-displaced/config.toml'
        self.ids[self.original['config']] = self.ids.pop('/etc/config')
        self.check(None)

    def test_configuration_cannot_be_a_transaction_file(self):
        self.original['config'] = '/db/.state.camctl-id-temp'
        self.ids[self.original['config']] = self.ids.pop('/etc/config')
        with self.assertRaises(ValueError):
            self.check(None)

    def test_bound_directory_cannot_overlap_control_or_recovery_tree(self):
        for bound in ('/target/updates/media', '/target/.camctl-update-id/media', '/target'):
            with self.subTest(bound=bound):
                self.bindings = [bound, '/media/ready', '/media/processing']
                with self.assertRaises(ValueError):
                    self.check(None)

    def test_existing_transaction_namespace_is_not_claimed(self):
        self.ids['/target/.camctl-update-id'] = (4, 4)
        with self.assertRaises(ValueError):
            self.check(None)


if __name__ == '__main__':
    unittest.main()
