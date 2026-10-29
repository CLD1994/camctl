"""部署路径覆盖与安装入口参数在内存协作者下验证。"""

import argparse
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
from types import ModuleType
import tomllib
import unittest
from unittest.mock import create_autospec, patch


SCRIPTS = Path(__file__).parents[2] / "scripts"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


support = load("deployment_path_support", "deploy_support.py")


def parse(argv):
    parser = argparse.ArgumentParser()
    support.add_path_arguments(parser)
    return support.path_overrides(parser.parse_args(argv))


class DeploymentPaths(unittest.TestCase):
    def test_defaults_keep_new_instance_isolated_and_preserve_devices(self):
        config = {"paths": {"ready": "/old/ready"}, "devices": {"camera": {"driver": "driver"}}}
        original = deepcopy(config["devices"])
        support.configure_paths(config, Path("/new/instance"), overrides=parse([]))
        self.assertEqual(config["devices"], original)
        self.assertEqual(config["paths"]["ready"], "/new/instance/ready")
        self.assertEqual(config["paths"]["state_db"], "/new/instance/state.db")
        self.assertEqual(config["paths"]["log_file"], "/new/instance/camctl.log")

    def test_partial_overrides_keep_spaces_and_other_defaults(self):
        config = {}
        support.configure_paths(config, Path("/new/instance"), overrides=parse([
            "--ready", "/external/待传 文件", "--processing", "/external/sending",
        ]))
        self.assertEqual(config["paths"]["ready"], "/external/待传 文件")
        self.assertEqual(config["paths"]["processing"], "/external/sending")
        self.assertEqual(config["paths"]["staging"], "/new/instance/staging")
        self.assertEqual(config["paths"]["state_db"], "/new/instance/state.db")

    def test_all_supported_path_arguments_override_target_defaults(self):
        config = {}
        support.configure_paths(config, Path("/new/instance"), overrides=parse([
            "--state-db", "/custom/state.db", "--log-file", "/logs/camctl.log",
            "--staging", "/data/staging", "--ready", "/data/ready", "--processing", "/data/processing",
        ]))
        self.assertEqual(config["paths"], {
            "state_db": "/custom/state.db", "log_file": "/logs/camctl.log",
            "staging": "/data/staging", "ready": "/data/ready", "processing": "/data/processing",
        })

    def test_invalid_paths_are_rejected_by_argument_parsing(self):
        for value in ("", "relative/path", "/absolute/with\0null"):
            with self.subTest(value=value), patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
                parse(["--ready", value])
            self.assertEqual(caught.exception.code, 2)

    def test_existing_database_or_symlink_is_not_reused(self):
        config = {"paths": {"state_db": "/custom/state.db"}}
        for exists, symlink in ((True, False), (False, True), (False, False)):
            with self.subTest(exists=exists, symlink=symlink), \
                    patch.object(Path, "exists", return_value=exists), \
                    patch.object(Path, "is_symlink", return_value=symlink):
                if exists or symlink:
                    with self.assertRaises(ValueError):
                        support.require_new_state_db(config)
                else:
                    support.require_new_state_db(config)

    def test_python36_bootstrap_forwards_custom_paths_unchanged(self):
        installer = load("path_installer", "install-host.py")
        argv = [str(SCRIPTS / "install-host.py"), "--target", "/new/instance",
            "--ready", "/external/ready with spaces", "--processing", "/external/processing",
            "--state-db", "/external/new.db", "--log-file", "/logs/camctl.log", "--staging", "/data/staging"]
        with patch.dict("sys.modules", {"deploy_support": support}), \
                patch.dict(installer.os.environ, {"PATH": "/tools"}, clear=True), \
                patch.object(installer.Path, "resolve", lambda path: path), \
                patch.object(installer.sys, "version_info", (3, 6, 15)), \
                patch.object(installer.sys, "argv", argv), \
                patch.object(installer.shutil, "which", return_value="/tools/uv"), \
                patch.object(installer.subprocess, "call", return_value=37) as call, patch("builtins.print"):
            self.assertEqual(installer.main(), 37)
        self.assertEqual(call.call_args.args[0][10:], argv[1:])

    def test_installer_exports_initializes_and_launches_with_the_final_paths(self):
        installer = load("configured_path_installer", "install-host.py")
        target = Path("/deployment/new-instance")
        bundle = SCRIPTS
        paths = {
            "state_db": "/custom/new.db", "log_file": "/logs/camctl.log",
            "staging": "/media/staging", "ready": "/media/待传 文件", "processing": "/media/sending",
        }
        argv = [str(bundle / "install-host.py"), "--target", str(target),
            "--state-db", paths["state_db"], "--log-file", paths["log_file"],
            "--staging", paths["staging"], "--ready", paths["ready"], "--processing", paths["processing"]]
        contents = {str(bundle / "RELEASE.json"): '{}'}
        consumers = []
        discovery = ModuleType("camera_discovery")
        discovery.resolve_hb_cli = lambda value: None
        discovery.format_report = lambda report: "设备身份查询完成"
        observed = {"devices": {}, "environment": {}, "placeholder_devices": []}

        def write(path, text, **kwargs):
            contents[str(path)] = text
            return len(text)

        def glob(path, pattern):
            if pattern == "*.whl":
                return iter([path / "camctl.whl"])
            if pattern == "*-Source.tar.gz":
                return iter([path / "host-Source.tar.gz"])
            if pattern == "*/CMakeLists.txt":
                return iter([path / "host/CMakeLists.txt"])
            raise AssertionError(pattern)

        def run(command, **kwargs):
            if command[0].endswith("/venv/bin/python"):
                contents[str(target / "camera-discovery.json")] = json.dumps(observed)
            if command[0].endswith("/venv/bin/camctl"):
                config = tomllib.loads(contents[command[-1]])
                consumers.append((command[1], config["paths"]))
            return subprocess.CompletedProcess(command, 0)

        archive = create_autospec(tarfile.TarFile, instance=True, spec_set=True)
        archive.__enter__.return_value = archive
        archive.getmembers.return_value = [tarfile.TarInfo("host/CMakeLists.txt")]
        with patch.dict("sys.modules", {"deploy_support": support, "camera_discovery": discovery}), \
                patch.object(installer.sys, "argv", argv), \
                patch.object(installer.sys, "version_info", (3, 11, 17)), \
                patch.object(installer.Path, "resolve", lambda path: path), \
                patch.object(installer.Path, "exists", return_value=False), \
                patch.object(installer.Path, "is_symlink", return_value=False), \
                patch.object(installer.Path, "is_file", lambda path: str(path) in contents), \
                patch.object(installer.Path, "mkdir"), patch.object(installer.Path, "chmod"), \
                patch.object(installer.Path, "open", lambda path, *args, **kwargs: io.BytesIO()), \
                patch.object(installer.Path, "read_text", lambda path, **kwargs: contents[str(path)]), \
                patch.object(installer.Path, "write_text", write), \
                patch.object(installer.Path, "glob", glob), \
                patch.object(installer.shutil, "which", return_value="/tools/tool"), \
                patch.object(installer.subprocess, "run", side_effect=run), \
                patch.object(installer.tarfile, "open", return_value=archive), patch("builtins.print"):
            self.assertEqual(installer.main(), 0)

        self.assertEqual(consumers, [("describe", paths), ("init", paths)])
        self.assertEqual(json.loads(contents[str(target / "install-receipt.json")])["paths"], paths)
        launcher = contents[str(target / "start-host-demo.sh")]
        command = shlex.split(next(line.removeprefix("exec ") for line in launcher.splitlines() if line.startswith("exec ")))
        self.assertEqual(command[command.index("--ready") + 1], paths["ready"])
        self.assertEqual(command[command.index("--processing") + 1], paths["processing"])


if __name__ == "__main__":
    unittest.main()
