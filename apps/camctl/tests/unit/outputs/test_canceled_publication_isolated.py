"""取消期间发布对账的纯状态分区；生产协作者在导入前隔离。"""

import importlib.util
from enum import IntEnum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


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
    names = ('camctl.contracts.enums', 'camctl.logging_runtime.diagnostics',
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
    def test_observed_staging_is_canceled_in_same_group_as_first_cleanup(self):
        repo = load_repository()
        repo._ACTION_STATUS = SimpleNamespace(RUNNING=2, CANCELED=6)
        repo._ACTION_TERMINAL = (3, 4, 5, 6)
        repo._ITEM_STATUS = SimpleNamespace(UNRESOLVED=1, SELECTED=2, DELIVERY_CREATED=3, FAILED=4, CANCELED=5)
        repo._RUN_TERMINAL = {3}
        repo.plan_complete = lambda siblings, action: False
        command = object.__new__(repo._SettleCanceledObtainCommand)
        command._command = SimpleNamespace(action_id=7, publishing_staging=((19, 21, 23),))
        command._members = [(8, 19, 21, 22)]
        command._state = {'actions': {7: {'id': 7, 'plan_id': 1, 'status': 2, 'cancel_requested': 1}},
            'plans': {1: {'id': 1, 'status': 2}},
            'obtain_items': {8: {'status': 3, 'source_dependency': 0}},
            'deliveries': {19: {'status': 4, 'publication_intent_event_id': 90}},
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
