"""真实子进程验证标准 wheel 的本轮构建与副本隔离。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1]

_RUN = '''
import json, os, subprocess, sys
from pathlib import Path
from python_test_build import prepare_standard_wheel

def run(command):
    with open(os.environ["TEST_COMMANDS"], "a", encoding="utf8") as stream:
        stream.write(json.dumps(command) + "\\n")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{command}: {result.stdout} {result.stderr}")

wheel, requirements = prepare_standard_wheel(Path(sys.argv[1]), Path(sys.argv[2]), run)
print(json.dumps([str(wheel), str(requirements)]))
'''


class PythonWheelBuildTest(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "apps/camctl"
        (project / "src/camctl").mkdir(parents=True)
        (project / "src/camctl/__init__.py").write_text('VALUE = "frozen"\n')
        (project / "pyproject.toml").write_text(
            '[project]\nname = "camctl"\nversion = "1.0.0"\n'
            'requires-python = ">=3.11"\ndependencies = []\n'
            '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n')
        result = subprocess.run(["uv", "lock", "--offline", "--project", str(project)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return project

    def _environment(self, root: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(PYTHONPATH=str(SCRIPTS), UV_OFFLINE="1", UV_PYTHON=sys.executable,
                   TEST_COMMANDS=str(root / "commands"))
        env.pop("CAMCTL_TEST_BUILD_SOURCE", None)
        env.pop("CAMCTL_TEST_BUILD_ROOT", None)
        return env

    def _start(self, project: Path, destination: Path, env: dict[str, str]):
        return subprocess.Popen([sys.executable, "-c", _RUN, str(project), str(destination)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _result(self, process):
        stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        return [Path(value) for value in json.loads(stdout)]

    def _contents(self, wheel: Path) -> str:
        with zipfile.ZipFile(wheel) as archive:
            return archive.read("camctl/__init__.py").decode()

    def test_direct_mode_builds_current_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root / "live")
            (project / "src/camctl/__init__.py").write_text('VALUE = "current"\n')
            wheel, requirements = self._result(self._start(project, root / "result", self._environment(root)))
            self.assertEqual(self._contents(wheel), 'VALUE = "current"\n')
            self.assertTrue(requirements.is_file())
            commands = [json.loads(line) for line in (root / "commands").read_text().splitlines()]
            self.assertEqual([command[1] for command in commands], ["export", "build"])
            self.assertEqual(commands[1][commands[1].index("--build-constraints") + 1], str(requirements))

    def test_parallel_requests_build_frozen_source_once_and_copy_private_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self._project(root / "frozen")
            shutil.copytree(root / "frozen", root / "live")
            live = root / "live/apps/camctl"
            (live / "src/camctl/__init__.py").write_text('VALUE = "changed"\n')
            env = self._environment(root)
            env.update(CAMCTL_TEST_BUILD_SOURCE=str(root / "frozen"),
                       CAMCTL_TEST_BUILD_ROOT=str(root))
            processes = [self._start(live, root / f"consumer-{number}", env) for number in range(2)]
            results = [self._result(process) for process in processes]
            commands = [json.loads(line) for line in (root / "commands").read_text().splitlines()]
            self.assertEqual([command[1] for command in commands], ["export", "build"])
            receipt = json.loads((root / "python-wheel-build.json").read_text())
            self.assertEqual((receipt["status"], receipt["build_count"]), ("ready", 1))
            shared = root / "python-wheel-build/artifacts"
            shared_wheel, = (shared / "dist").glob("camctl-*.whl")
            for wheel, requirements in results:
                self.assertEqual(self._contents(wheel), 'VALUE = "frozen"\n')
                self.assertNotEqual(wheel.stat().st_ino, shared_wheel.stat().st_ino)
                self.assertNotEqual(requirements.stat().st_ino, (shared / "requirements.txt").stat().st_ino)
            self.assertFalse(shared_wheel.stat().st_mode & 0o222)
            self.assertFalse((shared / "requirements.txt").stat().st_mode & 0o222)
            results[0][0].write_bytes(b"private")
            results[0][1].write_text("private")
            self.assertEqual(self._contents(results[1][0]), 'VALUE = "frozen"\n')
            self.assertEqual(self._contents(shared_wheel), 'VALUE = "frozen"\n')
            self.assertNotEqual(results[1][1].read_text(), "private")
            self.assertEqual((project / "src/camctl/__init__.py").read_text(), 'VALUE = "frozen"\n')


if __name__ == "__main__":
    unittest.main()
