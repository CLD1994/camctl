"""检查与释放事件的历史归属，不访问数据库或包资源。"""

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest


REPOSITORIES = Path(__file__).parents[3] / "src" / "camctl" / "persistence" / "repositories"


@dataclass(frozen=True)
class EventContext:
    transaction: object
    owners: dict
    state_rows: dict


def extract(path, name, facts):
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {"row_facts": lambda connection, table, identity: facts.get((table, identity)),
                 "EventContext": EventContext, "ConsistencyError": ValueError}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(path), "exec"), namespace)
    return namespace[name]


class AvailabilityOwnershipTests(unittest.TestCase):
    def test_creation_and_release_keep_trigger_and_old_action_distinct(self):
        rows = {("actions", 12): {"id": 12}, ("actions", 7): {"id": 7},
                ("operation_runs", 3): {"id": 3, "action_id": 12},
                ("operation_attempts", 5): {"id": 5, "run_id": 3},
                ("device_availability_checks", 31): {"id": 31, "action_id": 12},
                ("device_availability_targets", 32): {"id": 32, "action_id": 12, "activity_id": 25},
                ("device_activities", 25): {"id": 25, "action_id": 7, "activity_state": 1,
                                            "occupancy_state": 1, "availability_release_attempt_id": None}}
        create = extract(REPOSITORIES / "availability_create.py", "_context", rows)
        release = extract(REPOSITORIES / "availability_release.py", "_release_context", rows)
        transaction = object()
        creation = SimpleNamespace(rows=(SimpleNamespace(table="device_availability_checks", row_id=31),
                                         SimpleNamespace(table="device_availability_targets", row_id=32)))
        result = create(None, creation, transaction, 12)
        self.assertEqual(result.owners, {("device_availability_checks", 31): ("action", 12),
                                         ("device_availability_targets", 32): ("action", 12)})
        self.assertEqual(result.state_rows["device_availability_targets"][32]["activity_id"], 25)
        released = SimpleNamespace(rows=(SimpleNamespace(table="device_activities", row_id=25),))
        result = release(None, released, transaction, 3, 31, 5)
        self.assertEqual(result.owners, {("device_activities", 25): ("action", 7)})
        self.assertEqual(result.state_rows["device_activities"][25]["activity_state"], 1)
        self.assertEqual(result.state_rows["operation_attempts"][5]["run_id"], 3)
        self.assertEqual(result.state_rows["device_availability_checks"][31]["action_id"], 12)

    def test_release_rejects_missing_original_activity(self):
        facts = {("operation_runs", 3): {"id": 3, "action_id": 12},
                 ("operation_attempts", 5): {"id": 5},
                 ("device_availability_checks", 31): {"id": 31}}
        release = extract(REPOSITORIES / "availability_release.py", "_release_context", facts)
        event = SimpleNamespace(rows=(SimpleNamespace(table="device_activities", row_id=25),))
        with self.assertRaises(ValueError):
            release(None, event, object(), 3, 31, 5)


if __name__ == "__main__":
    unittest.main()
