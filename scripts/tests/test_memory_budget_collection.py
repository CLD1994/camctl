"""真实 pytest 根目录发现与组件预算插件加载的协作测试。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class MemoryBudgetCollectionTest(unittest.TestCase):
    def test_component_and_mixed_collection_accept_the_shared_budget_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("conftest.py", "apps/camctl/conftest.py", "apps/camctl/pyproject.toml",
                             "scripts/camctl_memory.py", "scripts/camctl_memory_plugin.py",
                             "scripts/test_kind_plugin.py"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            component = root / "apps/camctl"
            (component / "tests").mkdir()
            (component / "tests/test_capacity_probe.py").write_text(
                "def test_capacity_probe():\n    pass\n", encoding="utf-8")
            (root / "tests").mkdir()
            # 根跨组件配置会为辅助函数先导入插件模块；导入不等于向 pytest 注册。
            (root / "tests/conftest.py").write_text(
                "from pathlib import Path\nimport sys\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))\n"
                "import camctl_memory_plugin\n", encoding="utf-8")
            (root / "tests/test_root_probe.py").write_text(
                "def test_root_probe():\n    pass\n", encoding="utf-8")
            cases = (
                ("repository", root, ["apps/camctl/tests/test_capacity_probe.py"], 1),
                ("component", component, ["tests/test_capacity_probe.py"], 1),
                ("mixed", root, ["tests/test_root_probe.py", "apps/camctl/tests/test_capacity_probe.py"], 2),
                ("recursive", root, ["."], 2),
            )
            for name, cwd, targets, count in cases:
                with self.subTest(entrypoint=name):
                    result = subprocess.run(
                        [sys.executable, "-m", "pytest", *targets, "--collect-only", "-q",
                         "--camctl-memory-limit", "64MiB", "--camctl-memory-cgroup-root", "/fake/delegated"],
                        cwd=cwd, env={**os.environ, "PYTHONPATH": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                        capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"{count} test", result.stdout)


if __name__ == "__main__":
    unittest.main()
