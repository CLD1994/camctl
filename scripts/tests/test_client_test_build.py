"""标准网页构建与同轮真实进程、文件副本的协作。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "client_test_build.py"


class ClientBuildIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seed = self.root / "seed"
        self.output = self.root / "round"
        self.output.mkdir()
        self.dependencies = self.root / "dependencies"
        self.dependencies.mkdir()
        self.environment = os.environ.copy()
        self.environment.pop("CAMCTL_TEST_BUILD_ROOT", None)
        self.environment.pop("CAMCTL_TEST_BUILD_SOURCE", None)
        self.environment["BUILD_LOG"] = str(self.root / "build.log")
        self._project(self.seed, "frozen")
        (self.seed / ".git").mkdir()
        (self.seed / ".git/config").write_text("private")

    def tearDown(self):
        for path in self.root.rglob("*"):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        self.temp.cleanup()

    def _project(self, root: Path, value: str) -> Path:
        client = root / "apps/client"
        client.mkdir(parents=True)
        (client / "package.json").write_text(json.dumps({"scripts": {"build": "node build.cjs"}}))
        (client / "input.txt").write_text(value)
        (client / "package-lock.json").write_text(f"lock-{value}")
        (root / "protocol").mkdir()
        (root / "protocol/input.txt").write_text(f"protocol-{value}")
        (client / "build.cjs").write_text(
            "const fs = require('fs');\n"
            "fs.appendFileSync(process.env.BUILD_LOG, 'build\\n');\n"
            "fs.mkdirSync('dist/web', {recursive:true});\n"
            "fs.writeFileSync('dist/web/index.html', [fs.readFileSync('input.txt'),"
            "fs.readFileSync('package-lock.json'), fs.readFileSync('../../protocol/input.txt')].join('|'));\n"
        )
        return client

    def _command(self, project: Path):
        return [sys.executable, str(SCRIPT), "--project", str(project)]

    def test_concurrent_consumers_copy_one_build_from_frozen_source(self):
        clients = [self._project(self.root / f"task-{number}", f"candidate-{number}")
                   for number in range(2)]
        for client in clients:
            (client / "node_modules").symlink_to(self.dependencies, target_is_directory=True)
        (clients[0] / "dist/web").mkdir(parents=True)
        (clients[0] / "dist/web/stale").write_text("stale")
        env = dict(self.environment, CAMCTL_TEST_BUILD_ROOT=str(self.output),
                   CAMCTL_TEST_BUILD_SOURCE=str(self.seed))
        processes = [subprocess.Popen(self._command(client), env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for client in clients]
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stdout + stderr)

        self.assertEqual((self.root / "build.log").read_text().splitlines(), ["build"])
        receipt = json.loads((self.output / "client-web-build.json").read_text())
        self.assertEqual((receipt["status"], receipt["build_count"]), ("ready", 1))
        shared = self.output / "client-web-build/artifacts/web/index.html"
        self.assertEqual(shared.read_text(), "frozen|lock-frozen|protocol-frozen")
        self.assertEqual(shared.stat().st_mode & 0o222, 0)
        self.assertFalse((self.seed / "apps/client/node_modules").exists())
        self.assertFalse((self.seed / "apps/client/dist").exists())
        self.assertFalse((self.output / "client-web-build/source/.git").exists())
        self.assertFalse((clients[0] / "dist/web/stale").exists())
        clone = self.output / "client-web-build/source/apps/client"
        self.assertEqual((clone / "node_modules").resolve(), self.dependencies)
        for client in clients:
            self.assertEqual((client / "dist/web/index.html").read_text(), "frozen|lock-frozen|protocol-frozen")
        (clients[0] / "dist/web/index.html").write_text("changed")
        self.assertEqual((clients[1] / "dist/web/index.html").read_text(), "frozen|lock-frozen|protocol-frozen")
        self.assertEqual(shared.read_text(), "frozen|lock-frozen|protocol-frozen")

    def test_direct_mode_builds_current_project(self):
        client = self._project(self.root / "direct", "current")
        result = subprocess.run(self._command(client), env=self.environment,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((client / "dist/web/index.html").read_text(), "current|lock-current|protocol-current")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_build_is_not_reused_or_retried(self):
        client = self._project(self.root / "task", "candidate")
        (client / "node_modules").symlink_to(self.dependencies, target_is_directory=True)
        (self.seed / "apps/client/build.cjs").write_text(
            "require('fs').appendFileSync(process.env.BUILD_LOG, 'failed\\n'); process.exit(9);\n")
        env = dict(self.environment, CAMCTL_TEST_BUILD_ROOT=str(self.output),
                   CAMCTL_TEST_BUILD_SOURCE=str(self.seed))
        for _ in range(2):
            result = subprocess.run(self._command(client), env=env, capture_output=True,
                                    text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "build.log").read_text().splitlines(), ["failed"])
        self.assertEqual(json.loads((self.output / "client-web-build.json").read_text())["status"], "failed")
        self.assertFalse((client / "dist/web").exists())


if __name__ == "__main__":
    unittest.main()
