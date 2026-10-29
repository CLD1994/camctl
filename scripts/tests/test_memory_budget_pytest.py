"""真实 pytest 中断与预算收据恢复的协作；资源组由受控替身提供。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]


class MemoryBudgetPytestTest(unittest.TestCase):
    def test_keyboard_interrupt_recovers_after_fixture_cleanup_and_keeps_exit_two(self):
        for oom in (0, 1):
            with self.subTest(oom=oom), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "conftest.py").write_text(textwrap.dedent('''\
                    import os
                    from pathlib import Path
                    import pytest
                    import camctl_memory as memory

                    ROOT = Path(__file__).parent

                    def record(event):
                        with (ROOT / "cleanup-order").open("a", encoding="utf-8") as stream:
                            stream.write(event + "\\n")

                    @pytest.fixture
                    def owned_world():
                        yield
                        (ROOT / "fixture-finished").touch()
                        record("fixture")

                    class FakeGroup:
                        def __init__(self, path, version, limit):
                            self.path = path
                            self.limit = limit

                        def terminate(self):
                            assert (ROOT / "fixture-finished").exists()
                            record("terminate")

                        def snapshot(self):
                            record("evidence")
                            return {"budget_scope": "memory_and_swap",
                                "limit_bytes": self.limit, "combined_limit_bytes": self.limit,
                                "peak_bytes": 32, "combined_peak_bytes": 32,
                                "combined_peak_method": "memory.peak", "swap_limit_bytes": 0,
                                "swap_current_bytes": 0, "swap_peak_bytes": 0,
                                "events": {"oom": 0, "oom_kill": int(os.environ["FAKE_OOM"])}}

                        def remove(self):
                            self.path.rmdir()
                            record("remove")

                    memory._Group = FakeGroup
                    '''), encoding="utf-8")
                (root / "test_interrupt.py").write_text(textwrap.dedent('''\
                    import json
                    from pathlib import Path
                    import camctl_memory_plugin as plugin

                    def test_interrupt(owned_world):
                        root = Path(__file__).parent
                        parent = root / "groups"
                        group = parent / ("camctl-memory-123-" + "a" * 32)
                        group.mkdir(parents=True)
                        directory = plugin.receipt_directory(root / "world")
                        directory.mkdir(parents=True)
                        (directory / "camctl-owned.json").write_text(json.dumps({
                            "schema": 1, "budget_scope": "memory_and_swap", "status": "pending",
                            "limit_bytes": 64, "group": str(group), "owned_root": str(parent),
                            "controller": "cgroup-v2"}), encoding="utf-8")
                        raise KeyboardInterrupt
                    '''), encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", "-p", "camctl_memory_plugin",
                     "test_interrupt.py", "-q", "--tb=short", "--basetemp", str(root / "pytest-data")],
                    cwd=root, env={**os.environ, "PYTHONPATH": str(SCRIPTS),
                        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "FAKE_OOM": str(oom)},
                    capture_output=True, text=True, timeout=15,
                )

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual((root / "cleanup-order").read_text(encoding="utf-8").splitlines(),
                                 ["fixture", "terminate", "evidence", "remove"])
                receipt = json.loads((root / "world/memory-budget/camctl-owned.json").read_text(encoding="utf-8"))
                self.assertEqual(receipt["status"], "budget_exceeded" if oom else "recovered")
                self.assertTrue(receipt["group_removed"])
                self.assertEqual(receipt["evidence"]["events"]["oom_kill"], oom)
                if oom:
                    self.assertIn("camctl", result.stdout)
                    self.assertIn("内存不足", result.stdout)


if __name__ == "__main__":
    unittest.main()
