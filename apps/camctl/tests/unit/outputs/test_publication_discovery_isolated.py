"""取回发布发现：导入前隔离所有生产协作者及其外部资源。"""

import importlib.util
from enum import IntEnum
from importlib.abc import MetaPathFinder
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class BlockCamctl(MetaPathFinder):
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


class DeliveryStatus(IntEnum):
    PENDING = 1
    PREPARING = 2
    PREPARED = 3
    PUBLISHING = 4
    PUBLISHED = 5
    FAILED = 6
    CANCELED = 7
    WITHDRAWN = 8


class Stub:
    UNCONFIRMED = object()

    def __init__(self, *args, **kwargs):
        pass


MODULES = (
    'camctl.capture.files', 'camctl.capture.recovery', 'camctl.capture.input_copy',
    'camctl.capture.media_flow', 'camctl.contracts.enums', 'camctl.contracts.json_values',
    'camctl.contracts.values', 'camctl.devices.bindings', 'camctl.devices.evidence',
    'camctl.devices.read_session', 'camctl.host_files.io', 'camctl.host_files.models',
    'camctl.host_files.tasks', 'camctl.operations.attempts', 'camctl.operations.models',
    'camctl.operations.validation', 'camctl.outputs.copy', 'camctl.outputs.dispatch',
    'camctl.outputs.diagnostics', 'camctl.outputs.read_discovery', 'camctl.outputs.handoff',
    'camctl.outputs.qualification', 'camctl.outputs.read_attempts', 'camctl.outputs.slots',
    'camctl.outputs.sources', 'camctl.persistence.models', 'camctl.persistence.business',
    'camctl.logging_runtime.diagnostics', 'camctl.logging_runtime.events',
    'camctl.outputs.database_reads', 'camctl.persistence.repositories.capture',
    'camctl.persistence.repositories.operations', 'camctl.persistence.repositories.outputs',
    'camctl.outputs.obtain_summary',
)


def load_flow():
    modules = {}
    for name in MODULES:
        module = ModuleType(name)
        module.__getattr__ = lambda name: Stub
        modules[name] = module
    enums = modules['camctl.contracts.enums']
    enums.enum_for = lambda name: DeliveryStatus if name == 'deliveries.status' else Stub
    module_name = 'camctl.outputs._isolated_obtain_flow'
    source = Path(__file__).parents[3] / 'src/camctl/outputs/obtain_flow.py'
    spec = importlib.util.spec_from_file_location(module_name, source)
    module = importlib.util.module_from_spec(spec)
    modules[module_name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module, modules


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


class Connection:
    """只解释候选查询所需的 SQL 参数，不打开数据库。"""
    def __init__(self, rows, active=True):
        self.rows = rows
        self.active = active

    def execute(self, sql, parameters):
        if sql.startswith('SELECT type,status,cancel_requested FROM actions'):
            return Cursor([(4, 2, 0) if self.active else (4, 6, 1)])
        action, *statuses, limit = parameters
        if 'd.status IN' not in sql:
            assert len(statuses) == 1
        eligible = [row for row in self.rows if row[1] == action and row[2] in statuses]
        if 'EXISTS' in sql:
            eligible = [row for row in eligible if row[3]]
        return Cursor([(row[0],) for row in sorted(eligible)[:limit]])


class PublicationDiscoveryTests(unittest.TestCase):
    def test_prepared_and_publishing_are_discovered_with_original_ids_and_budget(self):
        flow, _ = load_flow()
        db = Connection([(11, 7, DeliveryStatus.PREPARED, True),
                         (12, 7, DeliveryStatus.PUBLISHING, True),
                         (13, 7, DeliveryStatus.PUBLISHED, True),
                         (14, 7, DeliveryStatus.WITHDRAWN, True),
                         (15, 8, DeliveryStatus.PUBLISHING, True)])
        self.assertEqual(flow._unpublished_deliveries(db, 7, limit=2, read_succeeded=True), [11, 12])

    def test_ineligible_publishing_still_blocks_action_completion(self):
        flow, _ = load_flow()
        db = Connection([(12, 7, DeliveryStatus.PUBLISHING, False)])
        self.assertEqual(flow._unpublished_deliveries(db, 7, limit=1, read_succeeded=True), [])
        self.assertEqual(flow._unpublished_deliveries(db, 7, limit=1), [12])


class PublicationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_photo_binding_failure_recovers_from_original_read(self):
        flow, modules = load_flow()
        class RoundContinues(Exception):
            pass
        class ConsistencyError(Exception):
            pass
        class Database:
            async def read(self, callback):
                return callback(SimpleNamespace(connection=self))
            def execute(self, sql, parameters):
                return Cursor([(9, 2, 4, None, 23, 12, 1, 4, 13, 14)])
        error = {"code": "device_binding_unavailable", "details": {
            "reason": "missing", "device_id": "camera", "expected_driver_id": "driver"}}
        class Discovery:
            async def begin_round(self, database, now): pass
            async def rows(self, database, kind, now, query, *args, **kwargs):
                if kind == "failed": return SimpleNamespace(items=[(9, error)])
                raise RoundContinues
        async def noop(*args): pass
        captured = []
        async def save(runtime, copy_id, stage, request, method):
            captured.append(request)
            return SimpleNamespace(kind="completed")
        flow.ConsistencyError = ConsistencyError
        flow.parse_exact_json = lambda value: value
        flow.BindingStatus = SimpleNamespace(DEVICE_MISSING="missing", DRIVER_MISMATCH="mismatch",
            PHYSICAL_IDENTITY_MISMATCH="physical_identity_mismatch", PHYSICAL_IDENTITY_CONFLICT="physical_identity_conflict")
        flow.DeviceBinding = lambda *args: args
        flow.BindingResult = lambda *args: args
        flow.FailReadBinding = lambda *args: args
        flow.DbOutcomeKind = SimpleNamespace(COMPLETED="completed")
        flow._retry_read_results = noop
        flow._settle_canceled_read_ends = noop
        flow._settle_ready_read_ends = noop
        reads = modules['camctl.outputs.read_attempts']
        reads.retry_read_business = reads.retry_read_ends = noop
        reads.save_read_business = save
        binding = ModuleType('camctl.persistence.repositories.binding_read_cancel')
        def load_failure(connection, action, copy, value):
            self.assertEqual((action, copy, value), (4, 9, error))
            return ("original-key", None, 17)
        binding.load_saved_binding_read_failure = load_failure
        transaction = ModuleType('camctl.persistence.transaction')
        transaction.row_facts = lambda connection, table, identity: {"id": identity, "source_intermediate_file_id": 23}
        def saved_events(connection, key):
            self.assertEqual(key, "original-key")
            return [dict(event_id=101)]
        transaction.saved_transaction_events = saved_events
        photo = ModuleType('camctl.outputs.photo_cache_reuse')
        invalid = [False]
        def verify(connection, copy, *, before_event_id, historical):
            self.assertEqual((copy["id"], before_event_id, historical), (9, 101, True))
            if invalid[0]:
                raise ConsistencyError("非照片来源或原关联不符")
            return 15
        photo.verify_photo_cache_binding_failure = verify
        modules.update({m.__name__: m for m in (binding, transaction, photo)})
        runtime = SimpleNamespace(database=Database(), discovery=Discovery(), occurred_at=lambda: 90,
            outputs=SimpleNamespace(fail_read_binding=object()))
        with patch.dict(sys.modules, modules):
            for reason in ("missing", "mismatch"):
                error["details"]["reason"] = reason
                actual = None if reason == "missing" else "other-driver"
                if actual is not None:
                    error["details"]["actual_driver_id"] = actual
                with self.assertRaises(RoundContinues):
                    await flow.advance_obtain(runtime)
                self.assertEqual(captured.pop(), (9, 15, (reason, ("camera", "driver"), actual, None), 17, "original-key"))
            invalid[0] = True
            with self.assertRaises(ConsistencyError):
                await flow.advance_obtain(runtime)
            self.assertEqual(captured, [])

    async def test_recovery_uses_original_delivery_and_defers_finish_until_budget_clears(self):
        flow, modules = load_flow()
        db = Connection([(11, 7, DeliveryStatus.PREPARED, True),
                         (12, 7, DeliveryStatus.PUBLISHING, True)])
        observed = []
        finished = []

        class Database:
            async def read(self, callback):
                return callback(SimpleNamespace(connection=db))

        async def publish(runtime, delivery_id, now):
            observed.append(delivery_id)
            db.rows = [row for row in db.rows if row[0] != delivery_id]

        async def save(*args):
            finished.append(True)
            return SimpleNamespace(kind='completed', value=SimpleNamespace(disposition='already'))

        modules['camctl.outputs.obtain_summary'].decide_obtain_finish = lambda _: SimpleNamespace(may_publish=True)
        modules['camctl.persistence.repositories.outputs'].delivery_read_succeeded = lambda conn, identity: True
        flow.load_obtain_facts = lambda conn, action: object()
        flow._publish_delivery = publish
        flow._save_repository = save
        flow.DbOutcomeKind = SimpleNamespace(COMPLETED='completed')
        flow.ObtainFinishDisposition = SimpleNamespace(SAVED='saved')
        runtime = SimpleNamespace(database=Database(), outputs=SimpleNamespace(finish_obtain=object()))
        with patch.dict(sys.modules, modules):
            self.assertEqual(await flow._publish_and_finish(runtime, 7, 20, publish_limit=1), 1)
            self.assertEqual(observed, [11])
            self.assertEqual(finished, [])
            self.assertEqual(await flow._publish_and_finish(runtime, 7, 21, publish_limit=1), 1)
        self.assertEqual(observed, [11, 12])
        self.assertEqual(finished, [True])

    async def test_unsuccessful_read_neither_publishes_nor_finishes(self):
        flow, modules = load_flow()
        db = Connection([(12, 7, DeliveryStatus.PUBLISHING, False)])

        class Database:
            async def read(self, callback):
                return callback(SimpleNamespace(connection=db))

        modules['camctl.outputs.obtain_summary'].decide_obtain_finish = lambda _: SimpleNamespace(may_publish=True)
        flow.load_obtain_facts = lambda conn, action: object()
        runtime = SimpleNamespace(database=Database())
        with patch.dict(sys.modules, modules):
            self.assertEqual(await flow._publish_and_finish(runtime, 7, 20, publish_limit=1), 0)

    async def test_canceled_action_is_not_finished_by_old_publication_round(self):
        flow, modules = load_flow()
        db = Connection([], active=False)
        class Database:
            async def read(self, callback):
                return callback(SimpleNamespace(connection=db))
        modules['camctl.outputs.obtain_summary'].decide_obtain_finish = lambda _: SimpleNamespace(may_publish=True)
        flow.load_obtain_facts = lambda conn, action: object()
        runtime = SimpleNamespace(database=Database())
        with patch.dict(sys.modules, modules):
            self.assertEqual(await flow._publish_and_finish(runtime, 7, 20, publish_limit=1), 0)

    async def test_owner_canceled_publication_is_handed_to_cancellation(self):
        from contextlib import nullcontext
        flow, _ = load_flow()
        class DeliveryError(Exception):
            def __init__(self, stage): self.stage = stage
        async def publish(*args): raise DeliveryError('owner_canceled')
        flow.DeliveryHandoffError = DeliveryError
        flow.measure_stage = lambda *args, **kwargs: nullcontext()
        flow.publish_delivery = publish
        flow.DeliveryContext = lambda **kwargs: kwargs
        runtime = SimpleNamespace(outputs=object(), database=SimpleNamespace(metadata=SimpleNamespace(instance_id='db')),
                                  directories=object(), file_executor=None)
        self.assertIsNone(await flow._publish_delivery(runtime, 19, 100))
