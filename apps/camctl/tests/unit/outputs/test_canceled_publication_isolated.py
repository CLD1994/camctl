"""取消期间发布对账的纯状态分区；生产协作者在导入前隔离。"""

import importlib.util
from enum import IntEnum
from importlib.abc import MetaPathFinder
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class BlockCamctl(MetaPathFinder):
    """禁止测试期间加载未明确替换的 camctl 模块或包资源。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "camctl" or fullname.startswith("camctl."):
            raise AssertionError(f"测试没有隔离生产模块: {fullname}")
        return None


def _forbidden_resource(*args, **kwargs):
    raise AssertionError("单元测试不得读取真实包资源")


_blocker = BlockCamctl()
_resources = ModuleType("camctl.resources")
_resources.resource_bytes = _forbidden_resource
_resource_patch = None


def setUpModule():
    global _resource_patch
    if any(name == "camctl" or name.startswith("camctl.") for name in sys.modules):
        raise AssertionError("隔离单元测试必须在未加载 camctl 的进程内运行")
    sys.meta_path.insert(0, _blocker)
    _resource_patch = patch.dict(sys.modules, {"camctl.resources": _resources})
    _resource_patch.start()


def tearDownModule():
    _resource_patch.stop()
    sys.meta_path.remove(_blocker)


class Status(IntEnum):
    PENDING = 1
    PREPARING = 2
    PREPARED = 3
    PUBLISHING = 4
    PUBLISHED = 5
    FAILED = 6
    CANCELED = 7
    WITHDRAWN = 8


class StubMeta(type):
    def __getattr__(cls, name):
        return 1


class Stub(metaclass=StubMeta):
    def __init__(self, *args, **kwargs):
        pass


def load_handoff():
    names = ('camctl.contracts.enums', 'camctl.contracts.values',
        'camctl.contracts.workflow_errors', 'camctl.host_files.handoff',
        'camctl.host_files.io', 'camctl.host_files.models', 'camctl.host_files.tasks',
        'camctl.outputs.copy', 'camctl.persistence.models', 'camctl.session.supervision',
        'camctl.logging_runtime.diagnostics', 'camctl.logging_runtime.events',
        'camctl.operations.models')
    modules = {}
    for name in names:
        module = ModuleType(name)
        module.__getattr__ = lambda name: Stub
        modules[name] = module
    modules['camctl.contracts.enums'].enum_for = lambda name: Status
    modules['camctl.contracts.values'].ConsistencyError = type('ConsistencyError', (Exception,), {})
    modules['camctl.contracts.values'].ObjectId = lambda value: value
    modules['camctl.contracts.values'].UtcMicros = lambda value: value
    modules['camctl.contracts.values'].MAX_OBJECT_ID = 2**63 - 1
    modules['camctl.contracts.workflow_errors'].registered_error = lambda code: {'stage': 'publication'}
    modules['camctl.contracts.workflow_errors'].validate_error_details = lambda code, details: None
    name = 'camctl.outputs._isolated_cancel_handoff'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[3] / 'src/camctl/outputs/handoff.py')
    module = importlib.util.module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class CanceledPublicationTests(unittest.TestCase):
    def setUp(self):
        self.h = load_handoff()
        self.digest = 'a' * 64
        self.facts = self.h.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=29,
            prepared_size=4, prepared_sha256=self.digest)
        self.absent = self.h.LocationObservation(True, False)
        self.present = self.h.LocationObservation(True, True, 4, self.digest)

    def classify(self, staging=None, ready=None, processing=None):
        locations = self.h.DeliveryLocations(staging or self.absent, ready or self.absent,
                                              processing or self.absent)
        return self.h.decide_canceled_publication(self.facts, locations)

    def test_staging_copy_is_canceled_without_publishing(self):
        self.assertEqual(self.classify(staging=self.present).value, 'cancel_staging')

    def test_ready_and_processing_copies_are_confirmed_for_withdrawal(self):
        for name in ('ready', 'processing'):
            with self.subTest(location=name):
                self.assertEqual(self.classify(**{name: self.present}).value, 'confirm_handoff')

    def test_absent_failed_observation_and_conflicting_copy_are_unknown(self):
        bad = self.h.LocationObservation(False, error=OSError('read failed'))
        conflict = self.h.LocationObservation(True, True, 4, 'b' * 64)
        for locations in ({}, {'ready': bad}, {'ready': conflict},
                          {'ready': self.present, 'processing': self.present},
                          {'staging': self.present, 'ready': self.present}):
            with self.subTest(locations=locations):
                self.assertEqual(self.classify(**locations).value, 'unknown')

    def test_pending_withdrawal_does_not_hide_original_publishing_state(self):
        self.facts = self.h.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=29,
            prepared_size=4, prepared_sha256=self.digest, withdrawal_requested=True)
        self.assertEqual(self.classify(ready=self.present).value, 'confirm_handoff')
        self.assertEqual(self.classify().value, 'unknown')

    def test_terminal_delivery_is_not_reactivated(self):
        self.facts = self.h.DeliveryFacts(19, Status.WITHDRAWN)
        self.assertEqual(self.classify(staging=self.present).value, 'not_active')

    def test_only_a_saved_publication_intent_enters_reconciliation(self):
        for facts in (self.h.DeliveryFacts(19, Status.PREPARED, prepared_size=4,
                                             prepared_sha256=self.digest),
                      self.h.DeliveryFacts(19, Status.PUBLISHING, prepared_size=4,
                                             prepared_sha256=self.digest)):
            with self.subTest(status=facts.status, intent=facts.publication_intent_event_id):
                self.facts = facts
                with self.assertRaises(self.h.ConsistencyError):
                    self.classify(staging=self.present)


def load_repository():
    names = ('camctl.acceptance.definitions', 'camctl.contracts.enums', 'camctl.logging_runtime.diagnostics',
        'camctl.history.decoding', 'camctl.contracts.json_values', 'camctl.contracts.history_values',
        'camctl.contracts.values', 'camctl.devices.bindings', 'camctl.history.reads',
        'camctl.history.events', 'camctl.history.validators', 'camctl.host_files.models',
        'camctl.host_files.paths', 'camctl.operations.attempts', 'camctl.operations.models',
        'camctl.persistence.repositories.operations', 'camctl.outputs.catalog',
        'camctl.outputs.cleanup_flow', 'camctl.outputs.competition', 'camctl.outputs.read_discovery',
        'camctl.outputs.copy', 'camctl.outputs.handoff', 'camctl.outputs.work_files',
        'camctl.outputs.definitions', 'camctl.outputs.obtain_summary', 'camctl.outputs.sources',
        'camctl.persistence.models', 'camctl.persistence.runtime', 'camctl.persistence.row_history',
        'camctl.persistence.transaction', 'camctl.contracts.workflow_errors',
        'camctl.outputs.qualification', 'camctl.outputs.slots', 'camctl.scheduling.order')
    modules = {}
    for name in names:
        module = ModuleType(name)
        module.__getattr__ = lambda name: Stub
        modules[name] = module

    class FakeEnumMeta(type):
        def __getattr__(cls, name):
            return 1
        def __iter__(cls):
            return iter(())
        def __getitem__(cls, name):
            return 1

    class FakeEnum(metaclass=FakeEnumMeta):
        pass

    modules['camctl.contracts.enums'].enum_for = lambda name: Status if name == 'deliveries.status' else FakeEnum
    modules['camctl.history.events'].reason_for_branch = lambda *args: 1
    modules['camctl.contracts.workflow_errors'].action_error_id = lambda *args: 1
    modules['camctl.contracts.workflow_errors'].item_error_id = lambda *args: 1
    modules['camctl.outputs.read_discovery'].DEFAULT_BATCH_SIZE = 32
    modules['camctl.outputs.read_discovery'].positive_limit = lambda value: value
    modules['camctl.contracts.values'].ConsistencyError = type('ConsistencyError', (Exception,), {})
    modules['camctl.persistence.transaction'].TransactionError = type('TransactionError', (Exception,), {})
    modules['camctl.persistence.transaction'].update_change = lambda table, identity, before, after: (table, identity, before, after)
    name = 'camctl.persistence.repositories._isolated_cancel_outputs'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[3] / 'src/camctl/persistence/repositories/outputs.py')
    module = importlib.util.module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class CancelSettlementTests(unittest.TestCase):
    def test_saved_binding_read_retires_after_photo_cache_cleanup(self):
        repo = load_repository()
        repo._RUN_KIND = SimpleNamespace(READ_FILE=3)
        repo.ReadCoverage = lambda value: value
        repo.CommandPlan = lambda **kwargs: SimpleNamespace(**kwargs)
        repo.FailureSaveDisposition = SimpleNamespace(ALREADY='already', RETIRED='retired')
        repo.FailureSaveOutcome = lambda disposition: SimpleNamespace(disposition=disposition)
        repo.commit_operation = lambda command, key, owned: command.plan(owned)
        repo._outcome_of = lambda outcome: outcome
        repo._load_binding_facts = lambda connection, file_id, result, state: {"reason": "missing"}
        rows = {
            'file_copies': {21: {'id': 21, 'delivery_id': 19, 'target_file_id': 23,
                'source_device_file_id': None, 'source_intermediate_file_id': 31, 'verification_state': 4}},
            'deliveries': {19: {'id': 19, 'action_id': 7, 'output_id': 29, 'status': 7}},
            'actions': {7: {'id': 7, 'status': 6, 'cancel_requested': 1}},
            'intermediate_files': {23: {'id': 23}, 31: {'id': 31, 'cleanup_state': 4}},
            'obtain_items': {10: {'id': 10, 'delivery_id': 19, 'output_id': 29, 'selection_id': 9}},
            'outputs': {29: {'id': 29, 'device_file_id': 30, 'source_action_id': 2}},
            'obtain_source_selections': {9: {'id': 9, 'dependency_id': 8}},
            'action_dependencies': {8: {'id': 8, 'action_id': 7, 'depends_on_action_id': 2}},
            'operation_runs': {41: {'id': 41, 'action_id': 7, 'copy_id': 21, 'kind': 3,
                'responsibility_key': 'read/21'}},
            'operation_attempts': {},
        }
        class Cursor:
            def __init__(self, found): self.found = found
            def fetchall(self): return self.found
            def close(self): pass
        class Connection:
            def execute(self, sql, parameters):
                parts = sql.split()
                table, column = parts[3], parts[5].split('=')[0]
                return Cursor([(identity,) for identity, row in rows[table].items()
                               if row.get(column) == parameters[0]])
        repo.row_facts = lambda connection, table, identity: dict(rows[table][identity])
        def saved_events(connection, key):
            return [{'event_id': 101}] if key == 'read-key' else None
        repo.saved_transaction_events = saved_events
        binding = ModuleType('camctl.persistence.repositories.binding_read_cancel')
        def saved_failure(connection, key, action, copy, error, identity):
            self.assertEqual((key, action, copy, error, identity), ('read-key', 7, 21,
                {'code': 'device_binding_unavailable', 'stage': 'execution', 'details': {'reason': 'missing'}}, None))
        binding.saved_binding_read_failure = saved_failure
        cache = ModuleType('camctl.outputs.photo_cache_reuse')
        source = [30]
        def verify(connection, copy, *, before_event_id, historical=False):
            if not historical:
                self.assertEqual(before_event_id, 201)
                raise repo.ConsistencyError('缓存已清理，不具备实时读取资格')
            self.assertEqual((copy['id'], before_event_id), (21, 101))
            return source[0]
        cache.verify_photo_cache_binding_failure = verify
        request = SimpleNamespace(copy_id=21, source_device_file_id=30,
            binding_result=SimpleNamespace(current_identity=None), occurred_at=17, read_result_key='read-key')
        owned = SimpleNamespace(connection=Connection(), max_event_id=200)
        with patch.dict(sys.modules, {binding.__name__: binding, cache.__name__: cache}):
            result = repo.OutputsRepository().fail_read_binding(request, 'business-key', owned)
            self.assertEqual(result.result.disposition, repo.FailureSaveDisposition.ALREADY)
            self.assertTrue(result.read_only)
            self.assertEqual(result.events, ())
            rows['deliveries'][19]['status'] = 1
            rows['actions'][7]['status'] = 2
            result = repo.OutputsRepository().fail_read_binding(request, 'business-key', owned)
            self.assertEqual(result.result.disposition, repo.FailureSaveDisposition.RETIRED)
            self.assertTrue(result.read_only)
            self.assertEqual(result.events, ())
            rows['actions'][7]['cancel_requested'] = 0
            with self.assertRaises(repo.ConsistencyError):
                repo.OutputsRepository().fail_read_binding(request, 'business-key', owned)
            rows['deliveries'][19]['status'] = 7
            request.read_result_key = None
            with self.assertRaises(repo.ConsistencyError):
                repo.OutputsRepository().fail_read_binding(request, 'business-key', owned)
            request.read_result_key = 'read-key'
            source[0] = 99
            with self.assertRaises(repo.TransactionError):
                repo.OutputsRepository().fail_read_binding(request, 'business-key', owned)

    def test_cleaned_photo_cache_preserves_cancel_recovery_identity(self):
        for replay in (True, False):
            for cache_id in (31, 99, None):
                with self.subTest(replay=replay, cache_id=cache_id):
                    repo = load_repository()
                    repo._OBTAIN_TYPE = 9
                    repo._ACTION_STATUS = SimpleNamespace(RUNNING=2, CANCELED=6)
                    repo._ACTION_TERMINAL = (3, 4, 5, 6)
                    repo._RUN_TERMINAL = (5,)
                    repo._RUN_KIND = SimpleNamespace(READ_FILE=7)
                    rows = {
                        'actions': {7: {'id': 7, 'plan_id': 1, 'type': 9, 'status': 6, 'cancel_requested': 1}},
                        'plans': {1: {'id': 1, 'status': 3}},
                        'action_dependencies': {8: {'id': 8, 'action_id': 7, 'depends_on_action_id': 2}},
                        'obtain_source_selections': {9: {'id': 9, 'dependency_id': 8}},
                        'obtain_items': {10: {'id': 10, 'selection_id': 9, 'delivery_id': 19, 'output_id': 29, 'source_dependency': 0}},
                        'deliveries': {19: {'id': 19, 'action_id': 7, 'output_id': 29, 'status': int(Status.FAILED)}},
                        'file_copies': {21: {'id': 21, 'delivery_id': 19, 'target_file_id': 23,
                            'source_device_file_id': None, 'source_intermediate_file_id': 31, 'slot_device_id': None}},
                        'intermediate_files': {23: {'id': 23, 'owner_delivery_id': 19, 'relative_path': 'target/23'},
                            31: {'id': 31, 'retention_state': 2, 'cleanup_state': 4}},
                        'outputs': {29: {'id': 29, 'source_action_id': 2, 'device_file_id': 30,
                            'intermediate_file_id': None, 'kind': 1}},
                        'operation_runs': {41: {'id': 41, 'copy_id': 21, 'kind': 7,
                            'action_id': 7, 'responsibility_key': 'read/21', 'status': 5, 'retry_wait_required': 0}},
                        'operation_attempts': {},
                    }
                    class Cursor:
                        def __init__(self, found): self.found = found
                        def fetchall(self): return self.found
                        def fetchone(self): return self.found[0] if self.found else None
                        def close(self): pass
                    class Connection:
                        def execute(self, sql, parameters):
                            if "SELECT t.operation_key" in sql:
                                return Cursor([("original-key",)])
                            parts = sql.split()
                            table, column = parts[3], parts[5].split('=')[0]
                            return Cursor([(identity,) for identity, row in rows[table].items()
                                if row.get(column) == parameters[0]])
                    repo.row_facts = lambda connection, table, identity: dict(rows[table][identity])
                    repo.validate_relative_file_path = lambda *args: None
                    repo._target_purpose_model = lambda copy, target: object()
                    original = [{'type': repo._ACTION_FINISHED_EVENT, 'reason': 4, 'occurred_at': 100,
                        'transaction': SimpleNamespace(txn_id=1), 'body': {'evidence': {}, 'rows': [{
                            'table': 'actions', 'id': 7,
                            'before': {'exists': True, 'values': {'status': 2}},
                            'after': {'exists': True, 'values': {'status': 6}}}]}}]
                    repo.saved_transaction_events = lambda connection, key: original if replay or key == 'original-key' else None
                    repo.OperationKey = lambda key: key
                    repo.read_row_values_at_boundary = lambda *args, **kwargs: {'status': 2}
                    repo.json_equal = lambda left, right: left == right
                    repo._update = lambda table, identity, before, after: SimpleNamespace(table=table, row_id=identity,
                        before=SimpleNamespace(exists=True, values=before), after=SimpleNamespace(exists=True, values=after))
                    repo.CommandPlan = lambda **kwargs: SimpleNamespace(**kwargs)
                    repo.commit_operation = lambda command, key, owned: command.plan(owned)
                    repo._outcome_of = lambda outcome: outcome
                    cache = ModuleType('camctl.outputs.photo_cache_reuse')
                    def read_bound(**kwargs):
                        # 已清理的缓存仍有历史身份，但不能再授予读取资格。
                        if not (kwargs.get('allow_released') and kwargs.get('historical')):
                            return None
                        self.assertEqual((kwargs['output_id'], kwargs['obtain_action_id'], kwargs['selection_id']),
                                         (29, 7, 9))
                        return None if cache_id is None else SimpleNamespace(intermediate_file_id=cache_id)
                    cache.read_bound_photo_cache = read_bound
                    with patch.dict(sys.modules, {cache.__name__: cache}):
                        request = repo.SettleCanceledObtain(7, 100 if replay else 200, (10,))
                        owned = SimpleNamespace(connection=Connection(), max_txn_id=2, max_event_id=20)
                        if cache_id == 31:
                            restored = repo.OutputsRepository().settle_canceled_obtain(request, 'key', owned)
                            self.assertTrue(restored.result.complete)
                            self.assertEqual(restored.result.file_ids, ())
                            self.assertEqual(restored.events, ())
                            self.assertTrue(restored.read_only)
                        else:
                            with self.assertRaises(repo.ConsistencyError):
                                repo.OutputsRepository().settle_canceled_obtain(request, 'key', owned)

    def test_observed_staging_is_canceled_in_same_group_as_first_cleanup(self):
        repo = load_repository()
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2, CANCELED=6)
        repo._ACTION_TERMINAL = (3, 4, 5, 6)
        repo._ITEM_STATUS = SimpleNamespace(UNRESOLVED=1, SELECTED=2, DELIVERY_CREATED=3, FAILED=4, CANCELED=5)
        repo._RUN_TERMINAL = {3}
        repo._WITHDRAWAL = SimpleNamespace(UNKNOWN=6)
        repo.plan_complete = lambda siblings, action: False
        command = object.__new__(repo._SettleCanceledObtainCommand)
        command._command = SimpleNamespace(action_id=7, publishing_staging=((19, 21, 23),))
        command._members = [(8, 19, 21, 22)]
        command._state = {'actions': {7: {'id': 7, 'plan_id': 1, 'status': 2, 'cancel_requested': 1}},
            'plans': {1: {'id': 1, 'status': 2}},
            'obtain_items': {8: {'status': 3, 'source_dependency': 0}},
            'deliveries': {19: {'status': 4, 'publication_intent_event_id': 90, 'withdrawal_state': 1}},
            'file_copies': {21: {'target_file_id': 23, 'slot_device_id': None}},
            'operation_runs': {22: {'status': 3, 'retry_wait_required': 0}}, 'operation_attempts': {}}
        command._command.publishing_staging = ()
        self.assertFalse(command._templates()[1].complete)
        command._command.publishing_staging = ((19, 21, 24),)
        with self.assertRaises(repo.ConsistencyError):
            command._templates()
        command._command.publishing_staging = ((19, 21, 23),)
        events, result = command._templates()
        self.assertTrue(result.complete)
        self.assertEqual(result.file_ids, (23,))
        self.assertTrue(any(row[0:2] == ('deliveries', 19) and row[3]['status'] == 7
                            for _, _, rows in events for row in rows))

    def test_original_staging_identity_is_part_of_replay_evidence(self):
        repo = load_repository()
        command = object.__new__(repo._SettleCanceledObtainCommand)
        command._command = SimpleNamespace(publishing_staging=((19, 21, 23),))
        rows = (SimpleNamespace(table='deliveries', row_id=19),)
        self.assertEqual(command._canceled_publication_evidence(23, 6, rows),
            {'canceled_publication': {'delivery_id': 19, 'copy_id': 21,
                                      'file_id': 23, 'location': 'staging'}})

    def test_cancel_guard_rejects_missing_or_wrong_original_copy_identity(self):
        repo = load_repository()
        event_error = type('EventError', (Exception,), {})
        repo.EventValidationError = event_error
        repo._OBTAIN_TYPE = 4
        repo._VERIFICATION = SimpleNamespace(MATCHED=3, SOURCE_CHECKSUM_UNAVAILABLE=5)
        event = SimpleNamespace(event_type=23, reason=6,
            rows=[SimpleNamespace(table='deliveries', row_id=19,
                before=SimpleNamespace(values={'status': 4}),
                after=SimpleNamespace(values={'status': 7}))], evidence={})
        context = SimpleNamespace(state_rows={
            'deliveries': {19: {'id': 19, 'action_id': 7, 'publication_intent_event_id': 90}},
            'actions': {7: {'id': 7, 'type': 4, 'status': 2, 'cancel_requested': 1}},
            'file_copies': {21: {'id': 21, 'delivery_id': 19, 'target_file_id': 23,
                                  'verification_state': 3, 'committed_bytes': 4, 'source_size': 4}},
            'intermediate_files': {23: {'id': 23, 'owner_delivery_id': 19}},
        })
        with self.assertRaises(event_error):
            repo._delivery_guard(event, context)
        event.evidence = {'canceled_publication': {'delivery_id': 19, 'copy_id': 21,
                                                    'file_id': 24, 'location': 'staging'}}
        with self.assertRaises(event_error):
            repo._delivery_guard(event, context)
        event.evidence['canceled_publication']['file_id'] = 23
        repo._delivery_guard(event, context)

    def test_replay_rejects_a_different_staging_observation_identity(self):
        repo = load_repository()
        command = object.__new__(repo._SettleCanceledObtainCommand)
        command._command = SimpleNamespace(publishing_staging=((19, 21, 23),))
        row = SimpleNamespace(table='deliveries', row_id=19,
            before=SimpleNamespace(exists=True, values={'status': 4}),
            after=SimpleNamespace(exists=True, values={'status': 7}))
        command._restore_before = lambda scope, saved: None
        command._templates = lambda: (((23, 6, (row,)),), SimpleNamespace(complete=True))
        command._decision = lambda result: result
        repo.json_equal = lambda a, b: a == b
        original = {'type': 23, 'reason': 6, 'body': {
            'evidence': {'canceled_publication': {'delivery_id': 19, 'copy_id': 21,
                                                   'file_id': 99, 'location': 'staging'}},
            'rows': [{'table': 'deliveries', 'id': 19,
                      'before': {'exists': True, 'values': {'status': 4}},
                      'after': {'exists': True, 'values': {'status': 7}}}]}}
        with self.assertRaises(repo.TransactionError):
            command._reuse(None, [original])
        original['body']['evidence']['canceled_publication']['file_id'] = 23
        self.assertTrue(command._reuse(None, [original]).complete)

    def test_withdrawal_only_updates_live_cancellation_waiters(self):
        repo = load_repository()
        command = object.__new__(repo._AdvanceWithdrawalCommand)
        command._command = SimpleNamespace(delivery_id=19)
        command._owners, command._state, command._items = {}, {}, []
        rows = {('deliveries', 19): {'id': 19, 'action_id': 7, 'status': 5,
                                     'withdrawal_state': 2, 'withdrawal_error_json': None},
                ('cancel_delivery_items', 30): {'id': 30, 'cancel_item_id': 40, 'status': 1},
                ('cancel_delivery_items', 31): {'id': 31, 'cancel_item_id': 41, 'status': 1},
                ('cancel_items', 40): {'id': 40, 'action_id': 50, 'target_action_id': 7, 'status': 2},
                ('cancel_items', 41): {'id': 41, 'action_id': 51, 'target_action_id': 7, 'status': 2},
                ('actions', 50): {'id': 50, 'status': 2, 'cancel_requested': 0},
                ('actions', 51): {'id': 51, 'status': 6, 'cancel_requested': 1}}
        repo.row_facts = lambda connection, table, identity: dict(rows[(table, identity)])
        class Cursor:
            def fetchall(self): return [(30, 50), (31, 51)]
            def close(self): pass
        scope = SimpleNamespace(connection=SimpleNamespace(execute=lambda *args: Cursor()))
        command._load(scope)
        self.assertEqual([item['id'] for item in command._items], [30])


def load_settlement():
    names = ('camctl.persistence.models', 'camctl.cancellation.service', 'camctl.cancellation.ports',
        'camctl.contracts.enums', 'camctl.contracts.values', 'camctl.persistence.business',
        'camctl.persistence.repositories.capture_facts', 'camctl.persistence.transaction')
    modules = {}
    for name in names:
        module = ModuleType(name)
        module.__getattr__ = lambda name: Stub
        modules[name] = module
    modules['camctl.contracts.enums'].enum_for = lambda name: Status
    name = 'camctl.cancellation._isolated_settlement'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[3] / 'src/camctl/cancellation/settlement.py')
    module = importlib.util.module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class CancelWaiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_ended_parent_does_not_block_a_live_waiter(self):
        settlement = load_settlement()
        class Cursor:
            def __init__(self, query):
                self.query = query
            def fetchall(self):
                if all(part in self.query for part in ('ci.status=2', 'origin.status=2', 'origin.cancel_requested=0')):
                    return [(19,)]
                return [(19,), (20,)]
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda query, args: Cursor(query))))
        target = object.__new__(settlement.TargetSettlement)
        target._database = Database()
        self.assertEqual(await target._pending_withdrawal_items(7), (19,))

    def test_waiter_creation_requires_active_parent_and_original_delivery(self):
        repo = load_repository()
        command = object.__new__(repo._EnsureWithdrawalWaiterCommand)
        command._command = SimpleNamespace(delivery_id=19, cancel_item_id=40)
        repo._OBTAIN_TYPE = 4
        repo._CANCEL_ACTION_TYPE = 6
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2)
        repo._WITHDRAWAL = SimpleNamespace(UNKNOWN=6)
        delivery = {'id': 19, 'action_id': 7, 'status': 5, 'withdrawal_state': 1}
        target = {'id': 7, 'type': 4, 'cancel_requested': 1}
        parent = {'id': 40, 'action_id': 50, 'target_action_id': 7, 'status': 2}
        origin = {'id': 50, 'type': 6, 'status': 2, 'cancel_requested': 0}
        self.assertTrue(command._eligible(delivery, target, parent, origin))
        delivery.update(status=4, withdrawal_state=1)
        self.assertFalse(command._eligible(delivery, target, parent, origin))
        delivery['withdrawal_state'] = 6
        self.assertTrue(command._eligible(delivery, target, parent, origin))
        delivery['status'] = 5
        origin['status'] = 6
        self.assertFalse(command._eligible(delivery, target, parent, origin))
        origin['status'] = 2
        parent['target_action_id'] = 8
        self.assertFalse(command._eligible(delivery, target, parent, origin))

    def test_new_waiter_saves_original_pair_and_reuses_existing_pair(self):
        repo = load_repository()
        repo._OBTAIN_TYPE, repo._CANCEL_ACTION_TYPE = 4, 6
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2)
        request = repo.EnsureWithdrawalWaiter(40, 19, 123)
        rows = {('deliveries', 19): {'id': 19, 'action_id': 7, 'status': 5},
                ('actions', 7): {'id': 7, 'type': 4, 'cancel_requested': 1},
                ('cancel_items', 40): {'id': 40, 'action_id': 50, 'target_action_id': 7, 'status': 2},
                ('actions', 50): {'id': 50, 'type': 6, 'status': 2, 'cancel_requested': 0}}
        repo.row_facts = lambda connection, table, identity: rows[(table, identity)]
        repo.saved_transaction_events = lambda connection, key: None
        repo.next_row_id = lambda connection, table: 60
        repo._row = lambda table, identity, values: (table, identity, values)
        repo._envelope = lambda *args: args
        repo.CommandPlan = lambda **kwargs: SimpleNamespace(**kwargs)
        class Cursor:
            def __init__(self, existing): self.existing = existing
            def fetchall(self): return self.existing
            def close(self): pass
        class Scope:
            existing = []
            connection = SimpleNamespace(execute=lambda sql, args: Cursor(scope.existing))
            def allocate(self, count): return SimpleNamespace(first_event_id=100, txn_id=10)
        scope = Scope()
        plan = repo._EnsureWithdrawalWaiterCommand(request, 'key').plan(scope)
        self.assertEqual(plan.events[0][4], (('cancel_delivery_items', 60, {
            'cancel_item_id': 40, 'delivery_id': 19, 'status': 1,
            'error_code': None, 'error_details_json': None}),))
        scope.existing = [(60,)]
        self.assertTrue(repo._EnsureWithdrawalWaiterCommand(request, 'key').plan(scope).read_only)

    def test_waiter_replay_rejects_different_original_pair(self):
        repo = load_repository()
        command = object.__new__(repo._EnsureWithdrawalWaiterCommand)
        command._command = SimpleNamespace(cancel_item_id=40, delivery_id=19, occurred_at=123)
        event = {'type': 23, 'reason': 7, 'occurred_at': 123, 'body': {'evidence': {}, 'rows': [{
            'table': 'cancel_delivery_items', 'id': 60, 'before': {'exists': False, 'values': {}},
            'after': {'exists': True, 'values': {'cancel_item_id': 41, 'delivery_id': 19,
                'status': 1, 'error_code': None, 'error_details_json': None}}}]}}
        with self.assertRaises(repo.TransactionError):
            command._reuse(None, [event])

    def test_waiter_replay_uses_original_boundary_after_parent_finishes(self):
        repo = load_repository()
        repo._OBTAIN_TYPE, repo._CANCEL_ACTION_TYPE = 4, 6
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2)
        repo.CommandPlan = lambda **kwargs: SimpleNamespace(**kwargs)
        repo.HistoryBoundary = lambda txn, event: (txn, event)
        repo.read_transaction_range = lambda conn, txn: SimpleNamespace(txn_id=9, last_event_id=99)
        command = object.__new__(repo._EnsureWithdrawalWaiterCommand)
        command._command = SimpleNamespace(cancel_item_id=40, delivery_id=19, occurred_at=123)
        rows = {('cancel_delivery_items', 60): {'id': 60, 'cancel_item_id': 40, 'delivery_id': 19, 'status': 2},
                ('cancel_items', 40): {'id': 40, 'action_id': 50, 'target_action_id': 7, 'status': 3},
                ('deliveries', 19): {'id': 19, 'action_id': 7, 'status': 8},
                ('actions', 7): {'id': 7, 'type': 4, 'status': 6, 'cancel_requested': 1},
                ('actions', 50): {'id': 50, 'type': 6, 'status': 3, 'cancel_requested': 0}}
        repo.row_facts = lambda connection, table, identity: dict(rows[(table, identity)])
        def original(connection, *, table, row_id, **kwargs):
            return {('cancel_items', 40): {'status': 2}, ('deliveries', 19): {'status': 5},
                    ('actions', 7): {'status': 2, 'cancel_requested': 1},
                    ('actions', 50): {'status': 2, 'cancel_requested': 0}}[(table, row_id)]
        repo.read_row_values_at_boundary = original
        event = {'type': 23, 'reason': 7, 'occurred_at': 123,
            'transaction': SimpleNamespace(txn_id=10), 'body': {'evidence': {}, 'rows': [{
                'table': 'cancel_delivery_items', 'id': 60, 'before': {'exists': False, 'values': {}},
                'after': {'exists': True, 'values': {'cancel_item_id': 40, 'delivery_id': 19,
                    'status': 1, 'error_code': None, 'error_details_json': None}}}]}}
        scope = SimpleNamespace(connection=object(), max_txn_id=12, max_event_id=120)
        self.assertTrue(command._reuse(scope, [event]).result)

    def test_unknown_handoff_keeps_original_work_file_cleanup_responsibility(self):
        repo = load_repository()
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2, CANCELED=6)
        repo._ACTION_TERMINAL = (3, 4, 5, 6)
        repo._ITEM_STATUS = SimpleNamespace(UNRESOLVED=1, SELECTED=2, DELIVERY_CREATED=3, FAILED=4, CANCELED=5)
        repo._RUN_TERMINAL = {3}
        repo._WITHDRAWAL = SimpleNamespace(UNKNOWN=6)
        repo.plan_complete = lambda siblings, action: False
        command = object.__new__(repo._SettleCanceledObtainCommand)
        command._command = SimpleNamespace(action_id=7, publishing_staging=())
        command._members = [(8, 19, 21, 22)]
        command._state = {'actions': {7: {'id': 7, 'plan_id': 1, 'status': 2, 'cancel_requested': 1}},
            'plans': {1: {'id': 1, 'status': 2}},
            'obtain_items': {8: {'status': 3, 'source_dependency': 0}},
            'deliveries': {19: {'status': 4, 'publication_intent_event_id': 90, 'withdrawal_state': 6}},
            'file_copies': {21: {'target_file_id': 23, 'slot_device_id': None}},
            'operation_runs': {22: {'status': 3, 'retry_wait_required': 0}}, 'operation_attempts': {}}
        events, result = command._templates()
        self.assertTrue(result.complete)
        self.assertEqual(result.file_ids, (23,))
        self.assertTrue(any(row[:2] == ('deliveries', 19) and row[3]['status'] == 7
                            for _, _, rows in events for row in rows))


def load_withdrawal():
    names = ('camctl.contracts.enums', 'camctl.contracts.values', 'camctl.host_files.handoff',
             'camctl.host_files.tasks', 'camctl.outputs.handoff', 'camctl.persistence.business',
             'camctl.persistence.models', 'camctl.logging_runtime.models')
    modules = {}
    for name in names:
        module = ModuleType(name)
        module.__getattr__ = lambda name: Stub
        modules[name] = module
    modules['camctl.contracts.enums'].enum_for = lambda name: (
        Status if name == 'deliveries.status' else SimpleNamespace(UNKNOWN=6))
    modules['camctl.contracts.values'].ConsistencyError = type('ConsistencyError', (Exception,), {})
    name = 'camctl.outputs._isolated_withdrawal'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[3] / 'src/camctl/outputs/withdrawal.py')
    module = importlib.util.module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules): spec.loader.exec_module(module)
    return module


class UnknownWithdrawalTests(unittest.IsolatedAsyncioTestCase):
    async def test_canceled_unknown_retains_original_result_without_implying_publication(self):
        module = load_withdrawal()
        module._identity_rows = lambda identity, owned: [(7, 'file', 7, 6, '{}', 23, 19)]
        class Database:
            async def read(self, callback, **kwargs): return callback(None)
        context = SimpleNamespace(database=Database(), ready='ready', processing='processing')
        self.assertEqual((await module._identity(19, context))[2:4], (7, 6))
        module._identity_rows = lambda identity, owned: [(7, 'file', 7, 1, None, 23, 19)]
        with self.assertRaises(module.ConsistencyError):
            await module._identity(19, context)


class CancelReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_staging_result_does_not_publish_or_create_withdrawal(self):
        module = load_handoff()
        facts = module.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=90,
            prepared_size=4, prepared_sha256='a' * 64)
        absent = module.LocationObservation(True, False)
        staging = module.LocationObservation(True, True, 4, 'a' * 64)
        calls = []
        async def saved(*args): calls.append(args)
        result = await module.reconcile_canceled_publication(
            facts, module.DeliveryLocations(staging, absent, absent), 21, 23,
            confirm_handoff=saved, save_unknown=saved, ensure_waiters=saved, withdraw=saved)
        self.assertEqual(result, (19, 21, 23))
        self.assertEqual(calls, [])

    async def test_confirmed_handoff_is_saved_before_withdrawal(self):
        module = load_handoff()
        facts = module.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=90,
            prepared_size=4, prepared_sha256='a' * 64)
        absent = module.LocationObservation(True, False)
        present = module.LocationObservation(True, True, 4, 'a' * 64)
        calls = []
        async def save(*args): calls.append('save')
        async def unknown(*args): calls.append('unknown')
        async def waiters(*args): calls.append('waiters')
        async def withdraw(*args): calls.append('withdraw')
        self.assertIsNone(await module.reconcile_canceled_publication(
            facts, module.DeliveryLocations(absent, present, absent), 21, 23,
            confirm_handoff=save, save_unknown=unknown, ensure_waiters=waiters, withdraw=withdraw))
        self.assertEqual(calls, ['save', 'waiters', 'withdraw'])

    async def test_unknown_is_saved_and_reports_failure_without_publication(self):
        module = load_handoff()
        facts = module.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=90,
            prepared_size=4, prepared_sha256='a' * 64)
        absent = module.LocationObservation(True, False)
        calls = []
        async def save(*args): calls.append('save')
        async def unknown(*args): calls.append('unknown')
        async def waiters(*args): calls.append('waiters')
        async def withdraw(*args): calls.append('withdraw')
        self.assertIsNone(await module.reconcile_canceled_publication(
            facts, module.DeliveryLocations(absent, absent, absent), 21, 23,
            confirm_handoff=save, save_unknown=unknown, ensure_waiters=waiters, withdraw=withdraw))
        self.assertEqual(calls, ['unknown', 'waiters', 'withdraw'])

    async def test_pending_unfinished_withdrawal_has_finite_unknown_result(self):
        module = load_handoff()
        facts = module.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=90,
            prepared_size=4, prepared_sha256='a' * 64, withdrawal_requested=True)
        absent = module.LocationObservation(True, False)
        present = module.LocationObservation(True, True, 4, 'a' * 64)
        calls = []
        async def saved(*args): calls.append('saved')
        async def unknown(*args): calls.append('unknown')
        async def waiters(*args): calls.append('waiters')
        async def withdraw(*args): calls.append('withdraw')
        self.assertIsNone(await module.reconcile_canceled_publication(
            facts, module.DeliveryLocations(present, absent, absent), 21, 23,
            confirm_handoff=saved, save_unknown=unknown, ensure_waiters=waiters, withdraw=withdraw))
        self.assertEqual(calls, ['unknown', 'waiters', 'withdraw'])


def load_canceled_runtime():
    handoff = load_handoff()
    names = ('camctl.contracts.enums', 'camctl.contracts.values', 'camctl.host_files.tasks',
             'camctl.logging_runtime.diagnostics', 'camctl.outputs.handoff', 'camctl.outputs.withdrawal',
             'camctl.persistence.models', 'camctl.persistence.repositories.outputs',
             'camctl.outputs.read_discovery', 'camctl.operations.models')
    modules = {}
    for name in names:
        module = ModuleType(name)
        module.__getattr__ = lambda name: Stub
        modules[name] = module
    modules['camctl.outputs.handoff'] = handoff
    modules['camctl.outputs.read_discovery'].DEFAULT_BATCH_SIZE = 100
    class Withdrawal(IntEnum):
        NOT_REQUESTED = 1
        PENDING = 2
        UNKNOWN = 6
    modules['camctl.contracts.enums'].enum_for = lambda name: Status if name == 'deliveries.status' else Withdrawal
    modules['camctl.contracts.values'].ConsistencyError = type('ConsistencyError', (Exception,), {})
    modules['camctl.contracts.values'].new_operation_key = lambda: 'key'
    modules['camctl.persistence.models'].DbOutcomeKind = SimpleNamespace(COMPLETED='completed')
    modules['camctl.outputs.withdrawal']._failure = lambda identity, detail, **kwargs: {
        'code': 'withdrawal_unconfirmed', 'stage': 'publication',
        'details': {'delivery_id': str(identity), 'error': detail}}
    modules['camctl.operations.models'].ErrorValue = lambda code, stage, details: SimpleNamespace(code=code, stage=stage, details=details)
    class Task:
        def __init__(self, task_id, file_ids, stage, business, body, **kwargs): self.body = body
    modules['camctl.host_files.tasks'].AsyncFileTask = Task
    modules['camctl.host_files.tasks'].FileTaskId = lambda value: value
    name = 'camctl.outputs._isolated_canceled_publication'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[3] / 'src/camctl/outputs/canceled_publication.py')
    module = importlib.util.module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules): spec.loader.exec_module(module)
    return module, handoff, modules


class CancelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_file_is_owned_during_observation_and_saved_before_withdrawal(self):
        module, handoff, modules = load_canceled_runtime()
        calls = []
        state = SimpleNamespace(action_id=7, target=SimpleNamespace(file_id=23), facts=handoff.DeliveryFacts(
            19, Status.PUBLISHING, publication_intent_event_id=90, prepared_size=4, prepared_sha256='a' * 64))
        absent = handoff.LocationObservation(True, False)
        present = handoff.LocationObservation(True, True, 4, 'a' * 64)
        locked = False
        class Executor:
            def unfinished_files(self): return ()
            async def run_owned_async_file_task(self, task):
                nonlocal locked
                locked = True
                result = await task.body(None)
                locked = False
                return result
        async def observe(function, *args):
            self.assertTrue(locked)
            calls.append('observe')
            return handoff.DeliveryLocations(absent, present, absent)
        async def withdraw(identity, context):
            self.assertFalse(locked)
            calls.append('withdraw')
            return True
        module.asyncio = SimpleNamespace(to_thread=observe)
        module.withdraw_delivery = withdraw
        class Repository:
            def load_delivery_state(self, identity, owned): return state
            def save_publication(self, request, key, owned):
                self.assert_locked()
                calls.append('save')
                return SimpleNamespace(kind='completed', value=True)
            def assert_locked(self):
                if not locked: raise AssertionError('原文件资格未持有')
        class Cursor:
            def __init__(self, sql): self.sql = sql
            def fetchall(self): return [(19, 21, 23, 1, 4)] if 'SELECT d.id' in self.sql else []
            def fetchone(self): return (4, 1)
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor(sql))))
            async def write(self, callback, **kwargs): return callback(None)
        context = module.CanceledPublicationContext(Database(), Repository(), object(), object(), Executor(), lambda: 100, {})
        with patch.dict(sys.modules, modules):
            self.assertEqual(await module.reconcile_canceled_obtain(7, context), ())
        self.assertEqual(calls, ['observe', 'save', 'withdraw'])

    async def test_unknown_result_is_saved_before_releasing_file_and_reuses_original_waiter(self):
        module, handoff, modules = load_canceled_runtime()
        facts = handoff.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=90,
            prepared_size=4, prepared_sha256='a' * 64)
        state = SimpleNamespace(action_id=7, target=SimpleNamespace(file_id=23), facts=facts)
        absent = handoff.LocationObservation(True, False)
        calls, locked = [], False
        diagnostics = []
        actual_error = OSError('observation failed')
        failed = handoff.LocationObservation(False, error=actual_error)
        module.record_error = lambda error, **kwargs: diagnostics.append((error, kwargs))
        class Executor:
            def unfinished_files(self): return ()
            async def run_owned_async_file_task(self, task):
                nonlocal locked
                locked = True
                result = await task.body(None)
                locked = False
                return result
        async def observe(function, *args): return handoff.DeliveryLocations(absent, failed, absent)
        async def withdraw(identity, context):
            self.assertFalse(locked)
            calls.append('withdraw')
            return True
        module.asyncio, module.withdraw_delivery = SimpleNamespace(to_thread=observe), withdraw
        class Repository:
            def load_delivery_state(self, identity, owned): return state
            def advance_withdrawal(self, request, key, owned):
                self.assert_locked()
                calls.append(request.choice.value)
                return SimpleNamespace(kind='completed', value=True)
            def assert_locked(self):
                if not locked: raise AssertionError('原文件资格未持有')
        class Cursor:
            def __init__(self, sql): self.sql = sql
            def fetchall(self): return [(19, 21, 23, 1, 4)] if 'SELECT d.id' in self.sql else []
            def fetchone(self): return (4, 1)
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor(sql))))
            async def write(self, callback, **kwargs): return callback(None)
        context = module.CanceledPublicationContext(Database(), Repository(), object(), object(), Executor(), lambda: 100, {})
        with patch.dict(sys.modules, modules):
            self.assertEqual(await module.reconcile_canceled_obtain(7, context), ())
        self.assertEqual(calls, ['requested', 'unknown', 'withdraw'])
        self.assertEqual(len(diagnostics), 1)
        error, data = diagnostics[0]
        self.assertEqual((error.code, error.stage), ('withdrawal_unconfirmed', 'publication'))
        self.assertEqual(error.details['delivery_id'], '19')
        self.assertIn('observation failed', error.details['error'])
        self.assertIs(data['details']['observation_errors'][0], actual_error)

    async def test_unknown_save_retries_original_request_and_operation_key(self):
        module, _, modules = load_canceled_runtime()
        module.db_failure = lambda *args: RuntimeError('unknown')
        received = []
        class Database:
            async def write(self, callback, *, key, **kwargs):
                received.append(key)
                callback(None)
                return SimpleNamespace(kind='unknown' if len(received) == 1 else 'completed', value=True)
        requests = []
        class Repository:
            def save_publication(self, request, key, owned): requests.append(request)
        context = SimpleNamespace(database=Database(), repository=Repository(), pending={})
        original, replacement, diagnostic = object(), object(), object()
        with patch.dict(sys.modules, modules):
            with self.assertRaises(RuntimeError):
                await module._save(context, (19, 'publication'), 7, 19, 'save_publication', original, diagnostic)
            self.assertIs(context.pending[(19, 'publication')].diagnostic, diagnostic)
            await module._save(context, (19, 'publication'), 7, 19, 'save_publication', replacement)
        self.assertEqual(received, ['key', 'key'])
        self.assertEqual(requests, [original, original])
        self.assertEqual(context.pending, {})

    async def test_large_target_scans_deliveries_in_batches_and_retains_staging_ids(self):
        module, handoff, modules = load_canceled_runtime()
        absent = handoff.LocationObservation(True, False)
        present = handoff.LocationObservation(True, True, 4, 'a' * 64)
        async def observe(function, *args): return handoff.DeliveryLocations(present, absent, absent)
        module.asyncio = SimpleNamespace(to_thread=observe)
        class Executor:
            def unfinished_files(self): return ()
            async def run_owned_async_file_task(self, task): return await task.body(None)
        class Repository:
            def load_delivery_state(self, identity, owned):
                return SimpleNamespace(action_id=7, target=SimpleNamespace(file_id=identity + 100),
                    facts=handoff.DeliveryFacts(identity, Status.PUBLISHING, publication_intent_event_id=90,
                                                prepared_size=4, prepared_sha256='a' * 64))
        class Cursor:
            def __init__(self, args): self.args = args
            def fetchall(self):
                after, limit = self.args[1], self.args[-1]
                return [(i, i + 10, i + 100, 1, 4) for i in (1, 2, 3) if i > after][:limit]
            def fetchone(self): return (4, 1)
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor(args))))
        wakes = []
        context = module.CanceledPublicationContext(Database(), Repository(), object(), object(), Executor(),
            lambda: 100, {}, batch_size=2, wake=lambda: wakes.append(True))
        with patch.dict(sys.modules, modules):
            self.assertIsNone(await module.reconcile_canceled_obtain(7, context))
            context.remaining = context.batch_size  # 下一轮重新取得本轮额度
            result = await module.reconcile_canceled_obtain(7, context)
        self.assertEqual(result, ((1, 11, 101), (2, 12, 102), (3, 13, 103)))
        self.assertEqual(wakes, [True])

    async def test_pending_settlement_reuses_original_request_before_any_new_observation(self):
        module = load_settlement()
        module.DbOutcomeKind = SimpleNamespace(COMPLETED='completed')
        module.SettlementOutcome = lambda complete, failed=False: SimpleNamespace(complete=complete, failed=failed)
        async def settle_required(awaitable): return await awaitable
        module.settle_required = settle_required
        calls = []
        original = object()
        class Cursor:
            def fetchall(self): return []
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor())))
            async def write(self, callback, *, key, **kwargs):
                calls.append(('key', key))
                return callback(None)
        class Outputs:
            def settle_canceled_obtain(self, request, key, owned):
                calls.append(('request', request))
                return SimpleNamespace(kind='completed', value=SimpleNamespace(complete=True, file_ids=(23,)))
        class WorkFiles:
            executor = SimpleNamespace(unfinished_files=lambda: ())
            async def first_cleanup(self, files, **kwargs):
                calls.append(('cleanup', files))
                return module.SettlementOutcome(True)
        async def forbidden(*args): raise AssertionError('不能重新观察')
        async def no_pending(*args): return ()
        target = object.__new__(module.TargetSettlement)
        target._database, target._outputs, target._work_files = Database(), Outputs(), WorkFiles()
        target._withdrawal_resume = None
        target._reconcile_publications = forbidden
        target._pending_obtain_settlements = {7: (original, 'original-key')}
        target._occurred_at = lambda: 100
        target._pending_withdrawal_items = no_pending
        target._withdrawal_execute = None
        result = await target._settle_obtain(7)
        self.assertTrue(result.complete)
        self.assertEqual(calls, [('key', 'original-key'), ('request', original), ('cleanup', (23,))])

    async def test_active_original_file_blocks_observation_and_save(self):
        module, _, modules = load_canceled_runtime()
        class Cursor:
            def fetchall(self): return [(19, 21, 23, 1, 4)]
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor())))
        class Executor:
            def unfinished_files(self): return (23,)
            async def run_owned_async_file_task(self, task):
                raise AssertionError('实际文件操作仍未结束')
        context = module.CanceledPublicationContext(Database(), object(), object(), object(), Executor(), lambda: 100, {})
        with patch.dict(sys.modules, modules):
            self.assertIsNone(await module.reconcile_canceled_obtain(7, context))

    async def test_file_owner_reloads_publication_after_inflight_operation_finishes(self):
        module, handoff, modules = load_canceled_runtime()
        calls = []
        old = SimpleNamespace(action_id=7, target=SimpleNamespace(file_id=23), facts=handoff.DeliveryFacts(
            19, Status.PUBLISHING, publication_intent_event_id=90, prepared_size=4, prepared_sha256='a' * 64))
        current = SimpleNamespace(action_id=7, target=SimpleNamespace(file_id=23), facts=handoff.DeliveryFacts(
            19, Status.PUBLISHED, publication_intent_event_id=90, published_event_id=91))
        class Executor:
            def unfinished_files(self): return ()
            async def run_owned_async_file_task(self, task): return await task.body(None)
        class Repository:
            count = 0
            def load_delivery_state(self, identity, owned):
                self.count += 1
                return old if self.count == 1 else current
        async def withdraw(identity, context):
            calls.append(identity)
            return True
        module.withdraw_delivery = withdraw
        class Cursor:
            def __init__(self, sql): self.sql = sql
            def fetchall(self): return [(19, 21, 23, 1, 4)] if 'SELECT d.id' in self.sql else []
            def fetchone(self): return (5, 1)
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor(sql))))
        context = module.CanceledPublicationContext(Database(), Repository(), object(), object(), Executor(), lambda: 100, {})
        with patch.dict(sys.modules, modules):
            self.assertEqual(await module.reconcile_canceled_obtain(7, context), ())
        self.assertEqual(calls, [19])

    def test_existing_intent_cannot_start_move_after_cancel_takes_effect(self):
        repo = load_repository()
        repo.saved_transaction_events = lambda connection, key: None
        repo.IntentDisposition = SimpleNamespace(ALREADY='already', SKIPPED='skipped')
        repo.IntentSaveOutcome = lambda disposition, reason=None: SimpleNamespace(disposition=disposition, reason=reason)
        repo._read_owner_eligible = lambda action: action['status'] == 2 and action['cancel_requested'] == 0
        command = object.__new__(repo._PublicationIntentCommand)
        command._key = 'key'
        command._request = SimpleNamespace(delivery_id=19)
        command._load_delivery = lambda connection, identity: ({'id': 19, 'status': 4, 'publication_intent_event_id': 90}, None)
        command._owner_action = lambda connection, delivery: {'status': 2, 'cancel_requested': 1}
        command._decision = lambda result, **kwargs: SimpleNamespace(result=result)
        result = command.plan(SimpleNamespace(connection=object())).result
        self.assertEqual((result.disposition, result.reason), ('skipped', 'cancel_requested'))
        command._owner_action = lambda connection, delivery: {'status': 2, 'cancel_requested': 0}
        repo.delivery_read_succeeded = lambda connection, identity: False
        result = command.plan(SimpleNamespace(connection=object())).result
        self.assertEqual((result.disposition, result.reason), ('skipped', 'read_result_unconfirmed'))
        repo.delivery_read_succeeded = lambda connection, identity: True
        result = command.plan(SimpleNamespace(connection=object())).result
        self.assertEqual(result.disposition, 'already')

    async def test_file_becoming_busy_during_state_read_waits_without_admitting_task(self):
        module, handoff, modules = load_canceled_runtime()
        state = SimpleNamespace(action_id=7, target=SimpleNamespace(file_id=23), facts=handoff.DeliveryFacts(
            19, Status.PUBLISHING, publication_intent_event_id=90, prepared_size=4, prepared_sha256='a' * 64))
        class Executor:
            count = 0
            def unfinished_files(self):
                self.count += 1
                return () if self.count == 1 else (23,)
            async def run_owned_async_file_task(self, task):
                raise AssertionError('已有文件任务，不能再次派发')
        class Repository:
            def load_delivery_state(self, identity, owned): return state
        class Cursor:
            def fetchall(self): return [(19, 21, 23, 1, 4)]
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor())))
        context = module.CanceledPublicationContext(Database(), Repository(), object(), object(), Executor(), lambda: 100, {})
        with patch.dict(sys.modules, modules):
            self.assertIsNone(await module.reconcile_canceled_obtain(7, context))

    def test_finish_reports_canceled_action_without_writing_a_normal_outcome(self):
        repo = load_repository()
        repo._OBTAIN_TYPE = 4
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2)
        repo._ACTION_TERMINAL = (3, 4, 5, 6)
        repo.saved_transaction_events = lambda connection, key: None
        repo.CommandPlan = lambda **kwargs: SimpleNamespace(**kwargs)
        repo.row_facts = lambda connection, table, identity: {
            'id': 7, 'type': 4, 'status': 6, 'cancel_requested': 1}
        command = object.__new__(repo._FinishObtainCommand)
        command._key = 'key'
        command._command = SimpleNamespace(action_id=7)
        command._state = {'actions': {}}
        command._owners = {}
        result = command.plan(SimpleNamespace(connection=object()))
        self.assertTrue(result.read_only)
        self.assertEqual(result.result.disposition.value, 'skipped')
        self.assertIsNone(result.result.prepared)
        self.assertIsNone(result.result.failed)

    def test_finish_original_key_recovers_even_if_action_now_canceled(self):
        repo = load_repository()
        repo.saved_transaction_events = lambda connection, key: ['original']
        command = object.__new__(repo._FinishObtainCommand)
        command._key = 'key'
        command._reuse = lambda connection, events: ('recovered', events)
        self.assertEqual(command.plan(SimpleNamespace(connection=object())), ('recovered', ['original']))

    def test_new_key_for_normal_terminal_action_uses_original_completion(self):
        repo = load_repository()
        repo._OBTAIN_TYPE = 4
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2)
        repo._ACTION_TERMINAL = (3, 4, 5, 6)
        repo.saved_transaction_events = lambda connection, key: None
        repo.row_facts = lambda connection, table, identity: {
            'id': 7, 'type': 4, 'status': 3, 'cancel_requested': 0}
        command = object.__new__(repo._FinishObtainCommand)
        command._key = 'new-key'
        command._command = SimpleNamespace(action_id=7)
        command._state = {'actions': {}}
        command._recover = lambda connection, action: ('already', action['status'])
        self.assertEqual(command.plan(SimpleNamespace(connection=object())), ('already', 3))

    async def test_normal_publication_waits_when_cancel_owns_original_file(self):
        module = load_handoff()
        class Database:
            async def read(self, callback, **kwargs):
                return SimpleNamespace(target=SimpleNamespace(file_id=23))
        class Executor:
            def unfinished_files(self): return (23,)
            async def run_owned_async_file_task(self, task):
                raise AssertionError('已有文件任务，不能再次派发')
        context = module.DeliveryContext(repository=object(), database=Database(),
            directories=object(), occurred_at=100, executor=Executor())
        result = await module.publish_delivery(19, context)
        self.assertEqual(result.phase.value, 'held')
        self.assertEqual(result.decision.error, 'file_in_use')

    async def test_unknown_commit_retry_emits_original_diagnostic_once(self):
        module, handoff, modules = load_canceled_runtime()
        module.db_failure = lambda *args: RuntimeError('unknown commit')
        original_error = OSError('ready inaccessible')
        absent = handoff.LocationObservation(True, False)
        failed = handoff.LocationObservation(False, error=original_error)
        async def observe(function, *args): return handoff.DeliveryLocations(absent, failed, absent)
        async def withdraw(identity, context): return True
        module.asyncio, module.withdraw_delivery = SimpleNamespace(to_thread=observe), withdraw
        records, states, saved_keys = [], set(), []
        def record(error, **kwargs):
            identity = id(kwargs['state'])
            if identity not in states:
                states.add(identity)
                records.append((error, kwargs))
        module.record_error = record
        actual = {'withdrawal': 1, 'unknown_calls': 0}
        class Executor:
            def unfinished_files(self): return ()
            async def run_owned_async_file_task(self, task): return await task.body(None)
        class Repository:
            def load_delivery_state(self, identity, owned):
                return SimpleNamespace(action_id=7, target=SimpleNamespace(file_id=23),
                    facts=handoff.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=90,
                        prepared_size=4, prepared_sha256='a' * 64, withdrawal_requested=actual['withdrawal'] != 1))
            def advance_withdrawal(self, request, key, owned):
                saved_keys.append((request.choice.value, key))
                if request.choice.value == 'unknown':
                    actual['unknown_calls'] += 1
                    if actual['unknown_calls'] == 1: return SimpleNamespace(kind='unknown', value=None)
                    actual['withdrawal'] = 6
                return SimpleNamespace(kind='completed', value=True)
        class Cursor:
            def __init__(self, sql): self.sql = sql
            def fetchall(self):
                return [(19, 21, 23, actual['withdrawal'], 4)] if 'SELECT d.id' in self.sql else []
            def fetchone(self): return (4, actual['withdrawal'])
            def close(self): pass
        class Database:
            async def read(self, callback, **kwargs):
                return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda sql, args: Cursor(sql))))
            async def write(self, callback, **kwargs): return callback(None)
        context = module.CanceledPublicationContext(Database(), Repository(), object(), object(), Executor(), lambda: 100, {})
        with patch.dict(sys.modules, modules):
            with self.assertRaises(RuntimeError):
                await module.reconcile_canceled_obtain(7, context)
            self.assertIsNotNone(context.pending[(19, 'withdrawal_unknown')].diagnostic)
            self.assertEqual(await module.reconcile_canceled_obtain(7, context), ())
        self.assertEqual([pair[0] for pair in saved_keys], ['requested', 'unknown', 'unknown'])
        self.assertEqual(saved_keys[1][1], saved_keys[2][1])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][0].code, 'withdrawal_unconfirmed')
        self.assertIs(records[0][1]['details']['observation_errors'][0], original_error)

    async def test_normal_publication_reloads_canceled_state_inside_file_ownership(self):
        from contextlib import nullcontext
        module = load_handoff()
        class Task:
            def __init__(self, task_id, file_ids, stage, business, body, **kwargs): self.body = body
        module.AsyncFileTask = Task
        module.FileTaskId = lambda value: value
        module.new_operation_key = lambda: 'key'
        module.log_context = lambda **kwargs: nullcontext()
        old = SimpleNamespace(action_id=7, file_name='name', target=SimpleNamespace(file_id=23, relative_path='file'),
                              facts=SimpleNamespace(status=Status.PUBLISHING))
        current = SimpleNamespace(action_id=7, file_name='name', target=SimpleNamespace(file_id=23, relative_path='file'),
                                  facts=SimpleNamespace(status=Status.CANCELED))
        class Repository:
            count = 0
            def load_delivery_state(self, identity, owned):
                self.count += 1
                return old if self.count == 1 else current
        class Database:
            async def read(self, callback, **kwargs): return callback(None)
        class Executor:
            def unfinished_files(self): return ()
            async def run_owned_async_file_task(self, task): return await task.body(None)
        seen = []
        async def publish(identity, context, facts):
            seen.append(facts.facts.status)
            return module.DeliveryResult(module.DeliveryPhase.NOT_ACTIVE,
                module.DeliveryDecision(module.DeliveryHandoffOutcome.NOT_ACTIVE))
        module._publish_delivery = publish
        context = module.DeliveryContext(repository=Repository(), database=Database(), directories=object(),
                                         occurred_at=100, executor=Executor())
        result = await module.publish_delivery(19, context)
        self.assertEqual(result.phase, module.DeliveryPhase.NOT_ACTIVE)
        self.assertEqual(seen, [Status.CANCELED])

    async def test_canceled_publication_returns_without_observing_file_locations(self):
        module = load_handoff()
        async def forbidden(*args): raise AssertionError('已取消交付不能再次观察文件')
        module.asyncio = SimpleNamespace(to_thread=forbidden)
        state = SimpleNamespace(facts=module.DeliveryFacts(19, Status.CANCELED))
        result = await module._publish_delivery(19, object(), state)
        self.assertEqual(result.phase, module.DeliveryPhase.NOT_ACTIVE)

    def test_unconfirmed_delivery_result_is_handed_to_cancel_when_cancel_wins(self):
        repo = load_repository()
        repo.saved_transaction_events = lambda connection, key: None
        repo.FailureSaveDisposition = SimpleNamespace(CANCELED='canceled')
        repo.FailureSaveOutcome = lambda disposition: SimpleNamespace(disposition=disposition)
        command = object.__new__(repo._UnconfirmedFailureCommand)
        command._key = 'key'
        command._request = SimpleNamespace(delivery_id=19)
        command._load_delivery = lambda connection, identity: ({'id': 19, 'status': 4}, None)
        command._owner_action = lambda connection, delivery: {'status': 2, 'cancel_requested': 1}
        command._decision = lambda result, **kwargs: SimpleNamespace(result=result, read_only=True)
        result = command.plan(SimpleNamespace(connection=object()))
        self.assertTrue(result.read_only)
        self.assertEqual(result.result.disposition, 'canceled')

    def test_unconfirmed_failure_guard_rejects_canceled_owner(self):
        repo = load_repository()
        repo.EventValidationError = type('EventError', (Exception,), {})
        repo.registered_error = lambda code: {'stage': 'publication'}
        repo.validate_error_details = lambda code, details: None
        row = SimpleNamespace(table='deliveries', row_id=19,
            after=SimpleNamespace(values={'status': 6, 'error_json': {
                'code': 'delivery_handoff_unconfirmed', 'stage': 'publication', 'details': {'delivery_id': '19'}}}))
        event = SimpleNamespace(event_type=23, reason=5, rows=(row,))
        context = SimpleNamespace(state_rows={'deliveries': {19: {'id': 19, 'action_id': 7}},
                                               'actions': {7: {'id': 7, 'status': 2, 'cancel_requested': 1}}})
        with self.assertRaises(repo.EventValidationError):
            repo._delivery_guard(event, context)
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2)
        context.state_rows['actions'][7]['cancel_requested'] = 0
        repo._delivery_guard(event, context)

    async def test_missing_copy_result_does_not_claim_failure_when_cancel_wins(self):
        module = load_handoff()
        unknown = SimpleNamespace(outcome=module.DeliveryHandoffOutcome.UNCONFIRMED_FINAL,
                                  failure=object(), exceptions=(), cause=None)
        async def observe(*args): return unknown
        async def save(*args, **kwargs): return module.FailureSaveDisposition.CANCELED
        module.asyncio = SimpleNamespace(to_thread=observe)
        module._save_unconfirmed = save
        state = SimpleNamespace(facts=module.DeliveryFacts(19, Status.PUBLISHING,
            publication_intent_event_id=90, prepared_size=4, prepared_sha256='a' * 64))
        result = await module._publish_delivery(19, object(), state)
        self.assertEqual(result.phase, module.DeliveryPhase.NOT_ACTIVE)

    async def test_missing_copy_after_not_moved_is_handed_to_cancel(self):
        module = load_handoff()
        decisions = [SimpleNamespace(outcome=module.DeliveryHandoffOutcome.PUBLISH, exceptions=(), cause=None),
                     SimpleNamespace(outcome=module.DeliveryHandoffOutcome.UNCONFIRMED_FINAL,
                                     failure=object(), exceptions=(), cause=None)]
        async def observe(*args): return decisions.pop(0)
        async def saved(*args, **kwargs): return None
        async def save_failure(*args, **kwargs): return module.FailureSaveDisposition.CANCELED
        async def move(*args): return SimpleNamespace(stage='not_moved', exception=None, error=None)
        module.asyncio = SimpleNamespace(to_thread=observe)
        module._save_intent = saved
        module._save_unconfirmed = save_failure
        module._staging_ref = lambda state, directories: (object(), object(), object())
        module.publish_file = move
        module.ReadyName = lambda name: name
        module.PublishStage = SimpleNamespace(MOVED='moved', NOT_MOVED='not_moved')
        state = SimpleNamespace(file_name='name', facts=module.DeliveryFacts(19, Status.PUBLISHING,
            publication_intent_event_id=90, prepared_size=4, prepared_sha256='a' * 64))
        result = await module._publish_delivery(19, SimpleNamespace(directories=object()), state)
        self.assertEqual(result.phase, module.DeliveryPhase.NOT_ACTIVE)

    async def test_already_observed_handoff_is_saved_even_when_cancel_has_won(self):
        module = load_handoff()
        delivered = SimpleNamespace(outcome=module.DeliveryHandoffOutcome.DELIVERED_LOCALLY,
                                    exceptions=(), cause=None)
        calls = []
        async def observe(*args): return delivered
        async def save(identity, context): calls.append(identity)
        module.asyncio = SimpleNamespace(to_thread=observe)
        module._save_publication = save
        state = SimpleNamespace(facts=module.DeliveryFacts(19, Status.PUBLISHING,
            publication_intent_event_id=90, prepared_size=4, prepared_sha256='a' * 64))
        result = await module._publish_delivery(19, object(), state)
        self.assertEqual(result.phase, module.DeliveryPhase.PUBLISHED)
        self.assertEqual(calls, [19])

    async def test_unreliable_observation_preserves_error_instead_of_missing_copy_failure(self):
        module = load_handoff()
        original = OSError('observation failed')
        undecidable = SimpleNamespace(outcome=module.DeliveryHandoffOutcome.UNDECIDABLE,
            error='observation failed', exceptions=(original,), cause=original)
        async def observe(*args): return undecidable
        module.asyncio = SimpleNamespace(to_thread=observe)
        module.record_error = lambda *args, **kwargs: None
        state = SimpleNamespace(facts=module.DeliveryFacts(19, Status.PUBLISHING,
            publication_intent_event_id=90, prepared_size=4, prepared_sha256='a' * 64))
        with self.assertRaises(module.DeliveryHandoffError) as caught:
            await module._publish_delivery(19, object(), state)
        self.assertIs(caught.exception.__cause__, original)

    async def test_skipped_unconfirmed_save_does_not_publish_normal_failure_diagnostic(self):
        module = load_handoff()
        module.new_operation_key = lambda: 'key'
        module.DbOutcomeKind = SimpleNamespace(COMPLETED='completed')
        records = []
        module.record_error = lambda *args, **kwargs: records.append((args, kwargs))
        class Database:
            async def write(self, callback, **kwargs):
                return SimpleNamespace(kind='completed', value=SimpleNamespace(
                    disposition=module.FailureSaveDisposition.CANCELED))
        failure = module.DeliveryFailure('delivery_handoff_unconfirmed', 'publication', {'delivery_id': '19'})
        context = module.DeliveryContext(repository=object(), database=Database(),
                                         directories=object(), occurred_at=100)
        outcome = await module._save_unconfirmed(19, failure, context)
        self.assertEqual(outcome, module.FailureSaveDisposition.CANCELED)
        self.assertEqual(records, [])

    def test_unconfirmed_failure_original_key_remains_the_original_result(self):
        repo = load_repository()
        repo.saved_transaction_events = lambda connection, key: ['saved-failure']
        command = object.__new__(repo._UnconfirmedFailureCommand)
        command._key = 'original'
        command._reuse = lambda saved: ('already', saved)
        self.assertEqual(command.plan(SimpleNamespace(connection=object())), ('already', ['saved-failure']))
