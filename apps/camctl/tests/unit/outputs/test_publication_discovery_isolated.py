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

    def close(self):
        pass


class Connection:
    """只解释候选查询所需的 SQL 参数，不打开数据库。"""
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, parameters):
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
