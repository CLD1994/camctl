"""检查及旧活动的历史归属，不加载真实登记或数据库。"""

import ast
import importlib.abc
import sys
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).parents[3] / "src" / "camctl" / "history" / "replay.py"


def history_api():
    source = ast.parse(SOURCE.read_text())
    names = {"ReplayError", "EntityImage", "_own_rows", "_require_values", "apply_forward", "apply_reverse"}
    nodes = [node for node in source.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    program = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                               *nodes], type_ignores=[])
    namespace = {"__name__": __name__, "dataclass": dataclass, "replace": replace,
                 "_primary_table": lambda entity_type: "actions", "json_equal": lambda a, b: a == b}
    exec(compile(ast.fix_missing_locations(program), str(SOURCE), "exec"), namespace)
    return namespace


def row(table, identity, before, after):
    return SimpleNamespace(table=table, row_id=identity,
        before=SimpleNamespace(exists=before is not None, values={} if before is None else before),
        after=SimpleNamespace(exists=after is not None, values={} if after is None else after))


def event(number, changes, owners, references):
    return SimpleNamespace(envelope=SimpleNamespace(event_id=number, rows=tuple(changes)),
                           row_owners=owners, references=tuple(references))


class AvailabilityHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class BlockCamctl(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "camctl" or fullname.startswith("camctl."):
                    raise AssertionError(f"禁止导入真实包: {fullname}")
                return None
        cls.blocker = BlockCamctl()
        sys.meta_path.insert(0, cls.blocker)

    @classmethod
    def tearDownClass(cls):
        sys.meta_path.remove(cls.blocker)

    def test_forward_snapshot_and_reverse_keep_original_action_ownership(self):
        api = history_api()
        image = api["EntityImage"]
        old, trigger = (1, 7), (1, 12)
        old_before = image(*old, True, {("actions", 7): {"status": "failed", "error": "original"},
            ("device_activities", 25): {"activity_state": "unknown", "occupancy_state": "held",
                                        "availability_release_attempt_id": None}}, 9, 1)
        new_before = image(*trigger, True, {("actions", 12): {"status": "running"}}, 9, 1)
        creation = event(10, [row("device_availability_checks", 31, None, {"action_id": 12}),
                              row("device_availability_targets", 32, None, {"action_id": 12, "activity_id": 25})],
            {("device_availability_checks", 31): trigger, ("device_availability_targets", 32): trigger}, [trigger])
        release = event(11, [row("device_activities", 25,
            {"occupancy_state": "held", "availability_release_attempt_id": None},
            {"occupancy_state": "released", "availability_release_attempt_id": 5})],
            {("device_activities", 25): old}, [old])
        old_snapshot = api["apply_forward"](old_before, creation)
        new_snapshot = api["apply_forward"](new_before, creation)
        old_after = api["apply_forward"](old_snapshot, release)
        new_after = api["apply_forward"](new_snapshot, release)
        self.assertEqual(old_after.rows[("device_activities", 25)]["activity_state"], "unknown")
        self.assertEqual(old_after.rows[("actions", 7)]["error"], "original")
        self.assertEqual(old_after.rows[("device_activities", 25)]["availability_release_attempt_id"], 5)
        self.assertNotIn(("device_availability_checks", 31), old_after.rows)
        self.assertEqual(new_after.rows[("device_availability_targets", 32)]["activity_id"], 25)
        self.assertNotIn(("device_activities", 25), new_after.rows)
        self.assertEqual(api["apply_reverse"](old_after, release).rows, old_snapshot.rows)
        self.assertEqual(api["apply_reverse"](new_snapshot, creation).rows, new_before.rows)

    def test_reverse_rejects_a_different_saved_release(self):
        api = history_api()
        image = api["EntityImage"](1, 7, True, {("actions", 7): {},
            ("device_activities", 25): {"occupancy_state": "released", "availability_release_attempt_id": 6}}, 11, 2)
        change = row("device_activities", 25, {"occupancy_state": "held", "availability_release_attempt_id": None},
                     {"occupancy_state": "released", "availability_release_attempt_id": 5})
        with self.assertRaises(api["ReplayError"]):
            api["apply_reverse"](image, event(11, [change], {("device_activities", 25): (1, 7)}, [(1, 7)]))


if __name__ == "__main__":
    unittest.main()
