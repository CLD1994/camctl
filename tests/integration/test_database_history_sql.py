"""真实 SQLite 结构与查询测试；不模拟生产历史恢复或设备操作。"""
import itertools
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / 'docs/camctl/database/schema'
META = dict(created_event_id=1, last_event_id=2, change_count=2)


def insert(db, table, **row):
    db.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))


class DatabaseRuntimeRequirements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('check_database_spec', ROOT / 'scripts/check-database-spec.py')
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)
        cls.rules = cls.checker.read_runtime_requirements()

    def test_version_boundaries_use_numeric_comparison_and_exact_backports(self):
        # 预期来自正式版本契约，不能用被测规则反向计算允许集合。
        cases = [((3, 9, 99), False), ((3, 37, 0), False),
                 ((3, 44, 5), False), ((3, 44, 6), True), ((3, 44, 7), False),
                 ((3, 45, 0), False), ((3, 50, 6), False), ((3, 50, 7), True), ((3, 50, 8), False),
                 ((3, 51, 2), False), ((3, 51, 3), True), ((3, 51, 4), True),
                 ((3, 52, 0), True), ((3, 100, 0), True), ((4, 0, 0), True)]
        for version, expected in cases:
            with self.subTest(version=version):
                self.assertEqual(self.checker.sqlite_version_allowed(version, self.rules), expected)

    def test_missing_or_malformed_version_is_rejected(self):
        for version in (None, '3.51.3', (), (3, 51), (3, 51, 3, 0),
                        (3, '51', 3), (3, 51, True), (3, 51, -1), (3, 51, 3.0)):
            with self.subTest(version=version):
                self.assertFalse(self.checker.sqlite_version_allowed(version, self.rules))


class DatabaseStructure(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('\n'.join(p.read_text() for p in sorted(SCHEMA.glob('*.sql'))))
        insert(self.db, 'history_transactions', id=1, operation_key='a' * 32, first_event_id=1, last_event_id=2)
        for event in (1, 2):
            insert(self.db, 'history_events', id=event, transaction_id=1, event_type=1, event_version=1,
                   occurred_at=0, clock_status=2, change_seq=event, body_json='{}')
        insert(self.db, 'plans', id=1, request_id=1, name='计划', created_at=0, status=2, **META)
        for ident, kind in ((1, 1), (2, 4), (3, 5), (4, 5)):
            fields = dict(id=ident, plan_id=1, input_index=ident - 1, name=str(ident), type=kind,
                          input_fields_json='{}', execution_spec_json='{}', status=2,
                          execution_started=1, cancel_requested=0, **META)
            if kind == 1:
                fields.update(device_id='device', scheduled_at=0, effective_params_json='{}', driver_id='driver', max_delay_ms=0)
            else:
                fields.update(scheduled_at=0, source_resolution_state=2, resolved_source_plan_id=1)
            insert(self.db, 'actions', **fields)
        self.db.commit()

    def columns(self, table):
        return {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}

    def test_directory_binding_requires_three_distinct_absolute_paths(self):
        row = dict(id=1, application_id='camctl', instance_id='c' * 32, format_version=1,
                   staging_path='/srv/camctl/staging', ready_path='/srv/camctl/ready', processing_path='/srv/camctl/processing')
        self.accepts('database_metadata', row, True)
        for column in ('staging_path', 'ready_path', 'processing_path'):
            for value in (None, '', '/', 'relative/directory', '/srv/invalid\0path'):
                with self.subTest(column=column, value=value):
                    self.accepts('database_metadata', {**row, column: value}, False)
        for left, right in itertools.combinations(('staging_path', 'ready_path', 'processing_path'), 2):
            with self.subTest(left=left, right=right):
                self.accepts('database_metadata', {**row, left: row[right]}, False)

    def seed_output(self):
        # 这些必填事实也是测试输入，不用缺省值掩盖新结构缺失。
        self.assertIn('created_event_id', self.columns('device_files'))
        self.assertIn('cleanup_status', self.columns('outputs'))
        insert(self.db, 'device_files', id=1, observer_action_id=1, source_action_id=1,
               identity_key='device/1', locator_json='{}', ownership_evidence_json='{}',
               role=1, presence_state=2, completion_state=3, completion_evidence_json='{}',
               size_bytes=100, checksum_support=1, **META)
        insert(self.db, 'outputs', id=1, source_action_id=1, kind=1, device_file_id=1,
               availability=1, cleanup_status=1, media_json='{}', **META)
        self.db.commit()

    def seed_selection(self):
        self.assertIn('depends_on_action_id', self.columns('action_dependencies'))
        insert(self.db, 'action_dependencies', id=1, action_id=2, depends_on_action_id=1)
        insert(self.db, 'obtain_source_selections', id=1, dependency_id=1, status=2)

    def accepts(self, table, row, expected):
        self.db.execute('SAVEPOINT candidate')
        try:
            try:
                insert(self.db, table, **row)
            except sqlite3.IntegrityError:
                self.assertFalse(expected, (table, row))
            else:
                self.assertTrue(expected, (table, row))
        finally:
            self.db.execute('ROLLBACK TO candidate')
            self.db.execute('RELEASE candidate')

    def test_dependency_and_selection_identity(self):
        self.seed_selection()
        self.accepts('action_dependencies', dict(id=2, action_id=2, depends_on_action_id=1), False)
        self.accepts('action_dependencies', dict(id=2, action_id=3, depends_on_action_id=1), True)
        self.accepts('action_dependencies', dict(id=2, action_id=2, depends_on_action_id=2), False)
        self.accepts('obtain_source_selections', dict(id=2, dependency_id=1, status=1), False)
        self.db.commit()

    def test_dependency_requires_real_source_at_commit(self):
        self.assertIn('depends_on_action_id', self.columns('action_dependencies'))
        insert(self.db, 'action_dependencies', id=1, action_id=2, depends_on_action_id=999)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.commit()
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT count(*) FROM action_dependencies').fetchone()[0], 0)

    def test_sync_can_reference_transaction_end_inserted_later(self):
        # 只验证真实 SQL 的延迟引用；事件正文及业务归属由生产验收另行检查。
        insert(self.db, 'history_transactions', id=2, operation_key='b' * 32, first_event_id=3, last_event_id=5)
        insert(self.db, 'history_events', id=3, transaction_id=2, event_type=1, event_version=1,
               occurred_at=0, clock_status=2, body_json='{}')
        insert(self.db, 'actions', id=5, plan_id=1, input_index=4, name='同步', type=7,
               input_fields_json='{}', execution_spec_json='{}', status=2, execution_started=1,
               cancel_requested=0, scheduled_at=0, created_event_id=3, last_event_id=3, change_count=1)
        insert(self.db, 'state_syncs', id=1, action_id=5, mode=1, from_wm=0, status=1, started_boundary_event_id=5)
        self.assertIsNone(self.db.execute('SELECT id FROM history_events WHERE id=5').fetchone())
        for event in (4, 5):
            insert(self.db, 'history_events', id=event, transaction_id=2, event_type=1, event_version=1,
                   occurred_at=0, clock_status=2, body_json='{}')
        self.db.commit()
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(self.db.execute('SELECT started_boundary_event_id FROM state_syncs').fetchall(), [(5,)])

    def test_missing_transaction_end_rejects_commit_and_rolls_back_group(self):
        insert(self.db, 'history_transactions', id=2, operation_key='b' * 32, first_event_id=3, last_event_id=5)
        insert(self.db, 'history_events', id=3, transaction_id=2, event_type=1, event_version=1,
               occurred_at=0, clock_status=2, body_json='{}')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.commit()
        self.db.rollback()
        self.assertIsNone(self.db.execute('SELECT id FROM history_transactions WHERE id=2').fetchone())
        self.assertIsNone(self.db.execute('SELECT id FROM history_events WHERE id=3').fetchone())

    def test_obtain_source_dependency_requires_permanent_delivery(self):
        self.seed_output()
        self.seed_selection()
        insert(self.db, 'deliveries', id=1, action_id=2, output_id=1, file_name='1.bin', display_name='文件',
               status=1, withdrawal_state=1, **META)
        for status, dependent in itertools.product(range(1, 6), (0, 1)):
            with self.subTest(status=status, dependent=dependent):
                row = dict(id=1, selection_id=1, basis=5, requested_output_id=1, status=status, source_dependency=dependent)
                if status != 1:
                    row['output_id'] = 1
                if status == 3:
                    row['delivery_id'] = 1
                if status == 4:
                    row.update(error_code=3, error_details_json='{}')
                self.accepts('obtain_items', row, dependent == 0 or status == 3)

    def test_cleanup_complete_state_matrix(self):
        self.seed_output()
        self.assertIn('final_event_id', self.columns('cleanup_items'))
        # 来自正式决策表：状态、限制、是否需要错误、是否需要成功依据。
        partitions = {(1, 1, False, False), (2, 2, False, False), (3, 2, False, False),
                      (3, 4, False, False), (4, 4, False, True), (5, 1, True, False),
                      (5, 2, True, False), (5, 4, True, False), (6, 1, False, False),
                      (6, 3, False, False), (6, 4, True, False)}
        for status, restriction, error, outcome, terminal_event in itertools.product(range(1, 7), range(1, 5), (False, True), (False, True), (False, True)):
            with self.subTest(status=status, restriction=restriction, error=error, outcome=outcome, terminal_event=terminal_event):
                row = dict(id=1, action_id=3, requested_output_id=1, output_id=1, status=status,
                           restriction_state=restriction, outcome=1 if outcome else None,
                           final_event_id=2 if terminal_event else None,
                           error_code=6 if error else None, error_details_json='{}' if error else None)
                allowed = (status, restriction, error, outcome) in partitions and terminal_event == (status in (4, 5, 6))
                self.accepts('cleanup_items', row, allowed)

    def test_canceled_cleanup_only_records_actual_delete_error(self):
        self.seed_output()
        for code in range(1, 8):
            with self.subTest(code=code):
                self.accepts('cleanup_items', dict(id=1, action_id=3, requested_output_id=1, output_id=1,
                             status=6, restriction_state=4, final_event_id=2, error_code=code, error_details_json='{}'), code in (4, 6))

    def test_cleanup_unknown_target_keeps_request_without_file_reference(self):
        self.assertIn('final_event_id', self.columns('cleanup_items'))
        for status in (1, 5, 6):
            row = dict(id=1, action_id=3, requested_output_id=999, status=status, restriction_state=1)
            if status != 1:
                row['final_event_id'] = 2
            if status == 5:
                row.update(error_code=1, error_details_json='{}')
            self.accepts('cleanup_items', row, True)

    def test_cleanup_success_accepts_registered_basis_values(self):
        self.seed_output()
        for outcome in (1, 2, 3, 0, 4):
            self.accepts('cleanup_items', dict(id=1, action_id=3, requested_output_id=1, output_id=1,
                         status=4, restriction_state=4, outcome=outcome, final_event_id=2), outcome in (1, 2, 3))

    def test_multiple_final_results_may_share_event(self):
        self.seed_output()
        for ident, action in ((1, 3), (2, 4)):
            insert(self.db, 'cleanup_items', id=ident, action_id=action, requested_output_id=1, output_id=1,
                   status=4, restriction_state=4, outcome=2, final_event_id=2)
        self.db.commit()
        self.assertEqual(self.db.execute('SELECT final_event_id FROM cleanup_items ORDER BY id').fetchall(), [(2,), (2,)])

    def test_cleanup_final_result_requires_existing_event(self):
        self.seed_output()
        insert(self.db, 'cleanup_items', id=1, action_id=3, requested_output_id=1, output_id=1,
               status=4, restriction_state=4, outcome=2, final_event_id=999)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.commit()

    def test_one_cleanup_holder_per_output(self):
        self.seed_output()
        insert(self.db, 'cleanup_items', id=1, action_id=3, requested_output_id=1, output_id=1, status=3, restriction_state=2)
        self.accepts('cleanup_items', dict(id=2, action_id=4, requested_output_id=1, output_id=1, status=3, restriction_state=2), False)
        self.accepts('cleanup_items', dict(id=2, action_id=4, requested_output_id=1, output_id=1, status=2, restriction_state=2), True)

    def test_output_cleanup_availability_and_error_matrix(self):
        self.seed_output()
        combinations = {(1, 1), (1, 4), (1, 5), (2, 2), (3, 2), (4, 3), (5, 2), (6, 1), (6, 4), (6, 5)}
        for cleanup, availability, error in itertools.product(range(1, 7), range(1, 6), (False, True)):
            with self.subTest(cleanup=cleanup, availability=availability, error=error):
                row = dict(id=2, source_action_id=1, kind=1, intermediate_file_id=999,
                           availability=availability, cleanup_status=cleanup, cleanup_error_json='{}' if error else None,
                           error_json='{}' if availability in (4, 5) else None, media_json='{}', **META)
                self.accepts('outputs', row, (cleanup, availability) in combinations and error == (cleanup == 5))

    def objects(self):
        registry = json.loads((ROOT / 'docs/camctl/database/enum-registry.json').read_text())
        self.assertIn('history_objects', registry)
        return registry['history_objects']

    def test_file_history_metadata_requires_valid_order_and_events(self):
        self.seed_output()
        insert(self.db, 'intermediate_files', id=1, owner_action_id=1, purpose=2,
               relative_path='recording-inputs/1.bin', retention_state=1, cleanup_state=1, **META)
        self.db.commit()
        for table in ('device_files', 'intermediate_files'):
            for assignment in ('created_event_id=NULL', 'last_event_id=0', 'change_count=0', 'last_event_id=created_event_id-1'):
                with self.subTest(table=table, assignment=assignment), self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(f'UPDATE {table} SET {assignment} WHERE id=1')
            self.db.execute(f'UPDATE {table} SET last_event_id=999 WHERE id=1')
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.commit()
            self.db.rollback()

    def test_history_report_and_snapshot_type_scopes(self):
        for name, kind in self.objects().items():
            with self.subTest(kind=name):
                self.accepts('entity_event_links', dict(id=1, entity_type=kind['id'], entity_id=1, event_id=1, change_count=1), True)
                self.accepts('report_entity_changes', dict(id=1, entity_type=kind['id'], entity_id=1, event_id=1, change_seq=1), kind['report_target'])
                self.accepts('entity_snapshots', dict(id=1, entity_type=kind['id'], entity_id=1, boundary_event_id=2,
                             change_count=1, format_version=1, content=b'{}\n'), kind['snapshot'])
                self.accepts('entity_snapshot_progress', dict(id=1, entity_type=kind['id'], entity_id=1,
                             current_change_count=1, snapshot_change_count=0), kind['snapshot'])
        for invalid in (0, max(x['id'] for x in self.objects().values()) + 1):
            self.accepts('entity_event_links', dict(id=1, entity_type=invalid, entity_id=1, event_id=1, change_count=1), False)

    def test_file_history_and_affected_output_have_separate_directories(self):
        self.seed_output()
        objects = self.objects()
        insert(self.db, 'entity_event_links', id=1, entity_type=objects['device_file']['id'], entity_id=1, event_id=2, change_count=2)
        insert(self.db, 'report_entity_changes', id=1, entity_type=objects['output']['id'], entity_id=1, event_id=2, change_seq=2)
        self.db.commit()
        self.assertNotIn('change_seq', self.columns('entity_event_links'))
        self.assertNotIn('change_count', self.columns('report_entity_changes'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM entity_event_links WHERE entity_type=?', (objects['output']['id'],)).fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT change_count FROM outputs').fetchone()[0], 2)

    def test_report_targets_share_event_but_not_duplicate_object(self):
        kind = self.objects()['output']['id']
        insert(self.db, 'report_entity_changes', id=1, entity_type=kind, entity_id=1, event_id=1, change_seq=1)
        self.accepts('report_entity_changes', dict(id=2, entity_type=kind, entity_id=2, event_id=1, change_seq=1), True)
        self.accepts('report_entity_changes', dict(id=2, entity_type=kind, entity_id=1, event_id=1, change_seq=1), False)
        self.accepts('report_entity_changes', dict(id=2, entity_type=kind, entity_id=1, event_id=2, change_seq=None), False)
        self.db.commit()

    def test_report_target_rejects_event_sequence_mismatch_at_commit(self):
        kind = self.objects()['output']['id']
        for event, sequence in ((1, 2), (2, 1), (999, 1)):
            with self.subTest(event=event, sequence=sequence):
                insert(self.db, 'report_entity_changes', id=1, entity_type=kind, entity_id=1, event_id=event, change_seq=sequence)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.commit()
                self.db.rollback()
                self.assertEqual(self.db.execute('SELECT count(*) FROM report_entity_changes').fetchone()[0], 0)

    def test_snapshot_candidates_use_type_identifier_then_exact_integer_id(self):
        objects = self.objects()
        snapshots = {name: kind for name, kind in objects.items() if kind['snapshot']}
        for ident, (name, kind) in enumerate(snapshots.items(), 1):
            insert(self.db, 'entity_snapshot_progress', id=ident, entity_type=kind['id'], entity_id=10,
                   current_change_count=64, snapshot_change_count=0)
        for ident, entity_id, changes in ((100, 2, 64), (101, 100, 65), (102, 2**53 + 1, 64), (103, 2**63 - 1, 64)):
            insert(self.db, 'entity_snapshot_progress', id=ident, entity_type=objects['action']['id'], entity_id=entity_id,
                   current_change_count=changes, snapshot_change_count=0)
        rows = self.db.execute('SELECT entity_type_name, entity_id FROM entity_snapshot_progress '
                               'WHERE pending_changes>=64 ORDER BY pending_changes DESC,entity_type_name COLLATE BINARY,entity_id').fetchall()
        self.assertEqual(rows, [('action', 100), ('action', 2), ('action', 10), ('action', 2**53 + 1), ('action', 2**63 - 1),
                                ('delivery', 10), ('device_file', 10), ('intermediate_file', 10), ('output', 10), ('plan', 10)])

    def test_one_read_flow_per_copy_and_one_flow_per_cleanup_kind(self):
        self.seed_output()
        insert(self.db, 'cleanup_items', id=1, action_id=3, requested_output_id=1, output_id=1, status=3, restriction_state=2)
        row = dict(id=1, action_id=3, cleanup_item_id=1, kind=4, responsibility_key='delete/1', status=2,
                   attempts_used=1, max_attempts_used=3, retry_wait_required=0)
        insert(self.db, 'operation_runs', **row)
        self.accepts('operation_runs', {**row, 'id': 2, 'responsibility_key': 'duplicate'}, False)
        self.accepts('operation_runs', {**row, 'id': 2, 'kind': 5, 'responsibility_key': 'exists/1'}, True)
        # 拷贝与流程的前向引用可以在同一事务中共同建立。
        insert(self.db, 'intermediate_files', id=1, owner_action_id=1, purpose=2,
               relative_path='recording-inputs/1.bin', retention_state=1, cleanup_state=1, **META)
        insert(self.db, 'recording_processing', id=1, action_id=1, check_state=1, check_decision=1,
               media_json='{}', repair_state=1, discard_state=1)
        insert(self.db, 'file_copies', id=1, processing_id=1, source_device_file_id=1, target_file_id=1,
               round=1, recopies_used=0, max_recopies_used=2, source_size=100, committed_bytes=0, reset_state=1, verification_state=1)
        read = dict(id=2, action_id=1, copy_id=1, kind=3, responsibility_key='read/1', status=1,
                    attempts_used=0, max_attempts_used=3, retry_wait_required=0)
        insert(self.db, 'operation_runs', **read)
        self.accepts('operation_runs', {**read, 'id': 3, 'responsibility_key': 'duplicate'}, False)
        self.db.commit()

    def test_expiration_keeps_start_attempts_and_limits_residual_stop(self):
        # 只验证过期结果的 SQL 组合；过期资格、尝试对应关系和共同事件由生产事务核验。
        insert(self.db, 'device_activities', id=1, action_id=1, task_key='e' * 32,
               state_query_supported=0, stop_supported=0, safe_repeat_stop=0,
               start_return_meaning=1, completion_mode=2, ownership_mode=1,
               output_scope_json='{}', baseline_state=1, dispatch_state=1,
               activity_state=1, occupancy_state=1, result_set_state=1)
        self.db.commit()
        cases = [(1, 0, True), (1, 1, True), (1, 3, True),
                 (8, 0, True), (8, 1, False), (8, 3, False),
                 (2, 0, False), (2, 1, False)]
        for kind, used, allowed in cases:
            with self.subTest(kind=kind, attempts_used=used):
                self.db.execute('SAVEPOINT expiration_case')
                try:
                    insert(self.db, 'operation_runs', id=1, action_id=1, activity_id=1,
                           kind=kind, responsibility_key={1: 'start/1', 2: 'stop/1', 8: 'followup/1/1'}[kind],
                           status=1 if used == 0 else 2, attempts_used=used,
                           max_attempts_used=1, retry_wait_required=0 if used == 0 else 1)
                    sql = 'UPDATE operation_runs SET status=7,retry_wait_required=0 WHERE id=1'
                    if allowed:
                        self.db.execute(sql)
                        self.assertEqual(self.db.execute('SELECT status,attempts_used,max_attempts_used,retry_wait_required '
                                                         'FROM operation_runs WHERE id=1').fetchone(), (7, used, 1, 0))
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.db.execute('UPDATE operation_runs SET retry_wait_required=1 WHERE id=1')
                    else:
                        with self.assertRaises(sqlite3.IntegrityError):
                            self.db.execute(sql)
                    self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
                finally:
                    self.db.execute('ROLLBACK TO expiration_case')
                    self.db.execute('RELEASE expiration_case')

    def seed_report_directory(self):
        self.assertIn('change_seq', self.columns('report_entity_changes'))
        insert(self.db, 'history_transactions', id=2, operation_key='b' * 32, first_event_id=3, last_event_id=6002)
        self.db.executemany('INSERT INTO history_events VALUES (?,?,1,1,0,2,?,?)',
                            ((i, 2, i, '{}') for i in range(3, 6003)))
        kind = self.objects()['output']['id']
        self.db.executemany('INSERT INTO report_entity_changes (id,entity_type,entity_id,event_id,change_seq) VALUES (?,?,?,?,?)',
                            ((i, kind, (i - 3) % 300 + 1, i, i) for i in range(3, 6003)))
        self.db.commit()
        return kind

    def test_report_probe_and_cursor_use_covering_indexes_with_or_without_statistics(self):
        kind = self.seed_report_directory()
        probe = ('SELECT change_seq,entity_id FROM report_entity_changes WHERE entity_type=? '
                 'AND change_seq>? AND change_seq<=? ORDER BY change_seq,entity_id LIMIT ?')
        page = ('SELECT DISTINCT entity_id FROM report_entity_changes WHERE entity_type=? AND entity_id>? '
                'AND +change_seq>? AND +change_seq<=? ORDER BY entity_id LIMIT ?')
        for statistics in (False, True):
            with self.subTest(statistics=statistics):
                if statistics:
                    self.db.execute('ANALYZE')
                for query, params, index in ((probe, (kind, 2, 6002, 4097), 'report_changes_by_sequence'),
                                            (page, (kind, 17, 2, 6002, 17), 'report_changes_by_entity')):
                    plan = ' '.join(row[3] for row in self.db.execute('EXPLAIN QUERY PLAN ' + query, params))
                    self.assertIn('COVERING INDEX ' + index, plan)
                    self.assertNotIn('TEMP B-TREE', plan)
                self.assertEqual(len(self.db.execute(probe, (kind, 2, 6002, 4097)).fetchall()), 4097)
                self.assertEqual(self.db.execute(probe, (kind, 6000, 6002, 4097)).fetchall(), [(6001, 299), (6002, 300)])
                self.assertEqual(self.db.execute(probe, (kind, 6002, 6002, 4097)).fetchall(), [])
                found, after = [], 0
                while rows := self.db.execute(page, (kind, after, 2, 6002, 17)).fetchall():
                    found.extend(row[0] for row in rows)
                    after = rows[-1][0]
                self.assertEqual(found, list(range(1, 301)))

    def test_frozen_report_range_excludes_later_commits_and_keeps_large_ids(self):
        kind = self.objects()['output']['id']
        for ident, event, output in ((1, 1, 2**53 + 1), (2, 2, 2**63 - 1)):
            insert(self.db, 'report_entity_changes', id=ident, entity_type=kind, entity_id=output, event_id=event, change_seq=event)
        self.db.commit()
        query = ('SELECT DISTINCT entity_id FROM report_entity_changes WHERE entity_type=? AND entity_id>? '
                 'AND +change_seq>? AND +change_seq<=? ORDER BY entity_id LIMIT 1')
        first = self.db.execute(query, (kind, 0, 0, 2)).fetchone()[0]
        self.assertEqual(first, 2**53 + 1)
        insert(self.db, 'history_transactions', id=2, operation_key='b' * 32, first_event_id=3, last_event_id=3)
        insert(self.db, 'history_events', id=3, transaction_id=2, event_type=1, event_version=1,
               occurred_at=0, clock_status=2, change_seq=3, body_json='{}')
        insert(self.db, 'report_entity_changes', id=3, entity_type=kind, entity_id=2**53 + 2, event_id=3, change_seq=3)
        self.db.commit()
        self.assertEqual(self.db.execute(query, (kind, first, 0, 2)).fetchall(), [(2**63 - 1,)])

    def test_file_candidate_indexes_keep_old_and_cleaned_records(self):
        self.seed_output()
        for ident in (2, 3, 4):
            insert(self.db, 'device_files', id=ident, observer_action_id=1, source_action_id=1,
                   identity_key=f'device/{ident}', locator_json='{}', ownership_evidence_json='{}',
                   role=1, presence_state=3, completion_state=3, completion_evidence_json='{}', size_bytes=100,
                   checksum_support=1, created_event_id=1 if ident != 3 else 2, last_event_id=2, change_count=2)
        # 固定上界 3 排除后来登记的 4；创建边界 1 排除边界后登记的 3。
        for column, index in (('source_action_id', 'device_files_source'), ('observer_action_id', 'device_files_observer')):
            query = f'SELECT id FROM device_files WHERE {column}=? AND id>? AND id<=? AND created_event_id<=? ORDER BY id LIMIT ?'
            self.assertEqual(self.db.execute(query, (1, 0, 3, 1, 1)).fetchall(), [(1,)])
            self.assertEqual(self.db.execute(query, (1, 1, 3, 1, 1)).fetchall(), [(2,)])
            plan = ' '.join(row[3] for row in self.db.execute('EXPLAIN QUERY PLAN ' + query, (1, 0, 3, 1, 1)))
            self.assertIn(index, plan)
        for ident, owner, purpose in ((1, {'owner_action_id': 1}, 2), (2, {'owner_delivery_id': 999}, 1)):
            insert(self.db, 'intermediate_files', id=ident, **owner, purpose=purpose,
                   relative_path=f'{"recording-inputs" if purpose == 2 else "deliveries"}/{ident}.bin', retention_state=2, cleanup_state=4, **META)
            key = next(iter(owner))
            query = f'SELECT id FROM intermediate_files WHERE {key}=? AND id>? AND id<=? AND created_event_id<=? ORDER BY id LIMIT ?'
            params = (owner[key], 0, 2, 1, 1)
            self.assertEqual(self.db.execute(query, params).fetchall(), [(ident,)])
            self.assertIn('intermediate_action' if purpose == 2 else 'intermediate_delivery',
                          ' '.join(row[3] for row in self.db.execute('EXPLAIN QUERY PLAN ' + query, params)))


if __name__ == '__main__':
    unittest.main()
