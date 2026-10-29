"""部署脚本与真实 TOML 文件、CMake 构建目录的组合验证。"""
from __future__ import annotations

from copy import deepcopy
import datetime
from decimal import Decimal
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[2]
SCRIPTS = SKILL / "scripts"
spec = importlib.util.spec_from_file_location("deploy_support", SCRIPTS / "deploy_support.py")
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class DeploymentFiles(unittest.TestCase):
    def test_python36_entry_bootstraps_python311_and_preserves_exit_code(self):
        spec = importlib.util.spec_from_file_location("host_installer", SCRIPTS / "install-host.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        argv = [str(SCRIPTS / "install-host.py"), "--target", "/tmp/installation with spaces",
            "--hb-cli", "/opt/camera tool/hb_cli"]
        with patch.object(installer.sys, "version_info", (3, 6, 15)), \
             patch.object(installer.sys, "argv", argv), \
             patch.object(installer.shutil, "which", return_value="/tools/uv"), \
             patch.dict(installer.os.environ, {"PYTHONPATH": "/unrelated", "PYTHONHOME": "/unrelated", "VIRTUAL_ENV": "/unrelated"}), \
             patch.object(installer.subprocess, "call", return_value=37) as call, \
             patch("builtins.print"):
            self.assertEqual(installer.main(), 37)
        command = call.call_args.args[0]
        self.assertEqual(command[:9], ["/tools/uv", "run", "--no-project", "--isolated", "--no-config",
            "--python", "3.11", "--", "python"])
        self.assertEqual(command[10:], argv[1:])
        environment = call.call_args.kwargs["env"]
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            self.assertNotIn(key, environment)

    def test_python36_entry_reports_missing_uv_before_installation(self):
        spec = importlib.util.spec_from_file_location("host_installer", SCRIPTS / "install-host.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime"
            with patch.object(installer.sys, "version_info", (3, 6, 15)), \
                 patch.object(installer.sys, "argv", [str(SCRIPTS / "install-host.py"), "--target", str(target)]), \
                 patch.object(installer.shutil, "which", return_value=None), \
                 patch.object(installer.sys, "stderr"), \
                 self.assertRaises(SystemExit) as error:
                installer.main()
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(target.exists())

    def test_readable_sections_preserve_all_config_values(self):
        source = {"devices": {
            "action6": {"kind": "camera", "driver": "dji-action6", "adb": {"serial": "A"}},
            "osmo360ii": {"kind": "camera", "driver": "dji-osmo360-ii", "adb": {"serial": "O"}},
            "hasselblad": {"kind": "camera", "driver": "hasselblad_cfv100c",
                "hasselblad": {"executable": "/opt/hb_cli", "serial": "H"}}}}
        source["devices"]["带点.空格 id"] = source["devices"].pop("action6")
        source["devices"]["带点.空格 id"]["connection"] = {"prepare_lead_s": Decimal("0.1234567890123456789")}
        source["devices"]["osmo360ii"]["software_recovery"] = {"enabled": True}
        source["paths"] = {"ready": "/old/ready"}
        original = deepcopy(source)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            support.configure_paths(source, target)
            path = target / "config.toml"
            support.write_config(path, source)
            text = path.read_text()
            actual = tomllib.loads(text, parse_float=Decimal)
            self.assertEqual(actual, source)
            self.assertEqual(actual["devices"], original["devices"])
            self.assertEqual(actual["paths"]["ready"], str(target / "ready"))
            self.assertEqual(len(actual["paths"]), 5)
            self.assertIn('[devices."带点.空格 id"]', text)
            self.assertIn('[devices.hasselblad]', text)
            self.assertIn('adb.serial = "A"', text)
            self.assertIn('hasselblad.executable = "/opt/hb_cli"', text)
            self.assertIn('connection.prepare_lead_s = 0.1234567890123456789', text)
            self.assertNotIn('[devices."带点.空格 id".adb]', text)
            self.assertNotIn('[devices.hasselblad.hasselblad]', text)
            self.assertNotIn('[devices]', text)
            self.assertNotIn("{", text)

    def test_toml_types_and_empty_tables_round_trip(self):
        config = {"devices": {}, "空.表": {}, "metadata": {
            "date": datetime.date(2026, 10, 29), "time": datetime.time(12, 30, 0),
            "stamp": datetime.datetime(2026, 10, 29, tzinfo=datetime.timezone.utc),
            "array": ["引号\"和换行\n", {"a.b": True}], "boolean": False,
            "a.b": {"space key": {"empty": {}}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            support.write_config(path, config)
            self.assertEqual(tomllib.loads(path.read_text(), parse_float=Decimal), config)

    def test_cmake_builds_in_new_directory_and_installs(self):
        # 真实配置/编译/安装，最小 C 工程隔离业务组件的编译成本。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "源码 with spaces"
            source.mkdir()
            (source / "main.c").write_text("int main(void) { return 0; }\n")
            (source / "CMakeLists.txt").write_text(
                'cmake_minimum_required(VERSION 3.10.2)\nproject(deploy_test C)\n'
                'add_executable(host-demo main.c)\ninstall(TARGETS host-demo DESTINATION bin)\n')

            def run(label, argv, *, cwd):
                result = subprocess.run(list(map(str, argv)), cwd=cwd, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, label + result.stdout + result.stderr)

            support.install_host(run, source, root)
            subprocess.run([root / "host/bin/host-demo"], check=True)

    def test_installer_rejects_missing_or_nonexecutable_hb_cli_without_creating_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = root / "hb_cli"
            for content in (None, "not executable"):
                if content is not None:
                    tool.write_text(content)
                target = root / "deployment"
                result = subprocess.run([sys.executable, SCRIPTS / "install-host.py",
                    "--target", target, "--hb-cli", tool], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse(target.exists())

    def test_installer_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            marker = target / "state.db"
            marker.write_bytes(b"existing database")
            result = subprocess.run([sys.executable, SCRIPTS / "install-host.py",
                "--target", target],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(marker.read_bytes(), b"existing database")


if __name__ == "__main__":
    unittest.main()
