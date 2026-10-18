"""安装版设备声明经公共协议、客户端加载与网页消费的软件联合验收。

wheel 与锁定运行依赖在仓库外离线安装，设备工具只作为禁止调用的
进程边界。任意驱动通过真实登记接口装配；不替换内置能力或源解析。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


REPO = Path(__file__).resolve().parents[2]
PROJECT = REPO / "apps/camctl"
CLIENT = REPO / "apps/client"


def _document(text: str):
    return json.loads(text, parse_float=Decimal)


def _environment():
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in {"PYTHONPATH", "PYTHONHOME", "PWD"} or name.startswith("CAMCTL_TEST_"):
            environment.pop(name)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["UV_PYTHON"] = sys.executable
    environment["UV_OFFLINE"] = "1"
    return environment


@dataclass
class Installed:
    root: Path
    python: Path
    wheel: Path
    environment: dict[str, str]

    def run(self, command, *, cwd=None, env=None, timeout=180):
        environment = dict(self.environment)
        environment.update(env or {})
        result = subprocess.run(
            [str(part) for part in command], cwd=cwd or self.root,
            env=environment, capture_output=True, text=True, timeout=timeout,
        )
        logs = self.root / "commands"
        logs.mkdir(exist_ok=True)
        (logs / f"{len(list(logs.glob('*.json'))) + 1:03}.json").write_text(
            json.dumps({"argv": [str(part) for part in command],
                        "cwd": str(cwd or self.root), "exitcode": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr},
                       ensure_ascii=False, indent=2), encoding="utf8")
        assert result.returncode == 0, (
            f"命令失败 {command!r}:\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def cli(self, *args, checkout=False, fixture=False):
        environment = {"PYTHONPATH": str(PROJECT / "src")} if checkout else {}
        python = sys.executable if checkout else self.python
        entry = [self.root / "declaration_entry.py"] if fixture else ["-m", "camctl"]
        return self.run([python, *entry, *args], env=environment)


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    assert sys.version_info[:2] == (3, 11), "安装链使用部署版 Python 3.11"
    root = tmp_path_factory.mktemp("device-declarations-installed")
    package = Installed(root, root / "venv/bin/python", root / "pending.whl", _environment())
    try:
        package.run(["uv", "build", "--project", PROJECT, "--wheel", "--out-dir", root / "dist"])
        wheels = list((root / "dist").glob("*.whl"))
        assert len(wheels) == 1
        package.wheel = wheels[0]
        requirements = root / "requirements.txt"
        package.run(["uv", "export", "--project", PROJECT, "--frozen", "--no-dev",
                     "--no-emit-project", "-o", requirements])
        package.run(["uv", "venv", "--python", sys.executable, root / "venv"])
        package.run(["uv", "pip", "install", "--python", package.python,
                     "--require-hashes", "-r", requirements])
        package.run(["uv", "pip", "install", "--python", package.python,
                     "--no-deps", package.wheel])
        # 任意标识仍通过正式静态与运行登记的交叉约束，不提供导出替身。
        for name in ("camctl_fixtures.py", "_capability_declaration_entry.py"):
            target = "declaration_entry.py" if name.startswith("_") else name
            shutil.copy2(REPO / "tests/integration" / name, root / target)
        guard = root / "bin"
        guard.mkdir()
        for tool in ("adb", "hb_cli"):
            executable = guard / tool
            executable.write_text(
                f"#!{package.python}\nfrom pathlib import Path\n"
                f"Path({str(root / 'device-calls')!r}).write_text({tool!r})\n"
                "raise SystemExit(91)\n", encoding="utf8")
            executable.chmod(0o700)
        package.environment["PATH"] = str(guard) + os.pathsep + package.environment["PATH"]
        yield package
    finally:
        archive = REPO / ".local/acceptance/c5" / f"installed-{os.getpid()}"
        if (root / "commands").exists():
            shutil.copytree(root / "commands", archive, dirs_exist_ok=True)


@pytest.fixture(scope="module")
def client_build(package):
    package.run(["pnpm", "run", "build"], cwd=CLIENT)


def _config(package, directory, *, host_storage="host", fixtures=False):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.toml"
    data = ["[paths]"]
    for key, filename in (("state_db", "state.db"), ("log_file", "camctl.log"),
                          ("staging", "staging"), ("ready", "ready"), ("processing", "processing")):
        data.append(f"{key} = {json.dumps(str(directory / filename))}")
    for device, driver in (("action6", "dji-action6"), ("osmo", "dji-osmo360-ii")):
        data += [f"[devices.{device}]", 'kind = "camera"', f'driver = "{driver}"',
                 f"[devices.{device}.adb]", f'serial = "offline-{device}"']
    for device, storage in (("hb-host", host_storage), ("hb-device", "device")):
        data += [f"[devices.{device}]", 'kind = "camera"', 'driver = "hasselblad_cfv100c"',
                 f"[devices.{device}.hasselblad]",
                 f"executable = {json.dumps(str(package.root / 'bin/hb_cli'))}",
                 f'serial = "offline-{device}"', f'output_storage = "{storage}"']
    if fixtures:
        for device, driver, storage in (("future-host", "vendor.future_empty.v9", "host"),
                                         ("future-device", "vendor.future_empty.v9", "device"),
                                         ("future-delete", "vendor.future_capture.v9", "device")):
            data += [f"[devices.{device}]", 'kind = "camera"', f'driver = "{driver}"',
                     f'fixture_storage = "{storage}"']
    path.write_text("\n".join(data) + "\n", encoding="utf8")
    return path


def _describe(package, path, **kwargs):
    text = package.cli("describe", "--config", str(path), **kwargs)
    assert text.endswith("\n")
    assert not (package.root / "device-calls").exists(), "describe 访问了设备工具"
    assert not (package.root / "driver-calls").exists(), "describe 调用了拍摄或删除端口"
    return text


def test_installed_declarations_match_checkout_and_protocol_resources(package, tmp_path):
    config = _config(package, tmp_path / "builtin")
    installed = _describe(package, config)
    assert _document(installed) == _document(_describe(package, config, checkout=True))
    document = _document(installed)
    assert {device["device_id"]: device["delete_supported"] for device in document["devices"]} == {
        "action6": False, "osmo": False, "hb-host": True, "hb-device": False,
    }
    schema = json.loads((REPO / "protocol/schemas/capabilities.schema.json").read_text())
    registry = Registry().with_resources(
        (path.name, Resource.from_contents(json.loads(path.read_text())))
        for path in (REPO / "protocol/schemas").glob("*.json")
    )
    Draft202012Validator(schema, registry=registry).validate(document)
    with zipfile.ZipFile(package.wheel) as archive:
        assert archive.read("camctl/_resources/protocol/capabilities.schema.json") == (
            REPO / "protocol/schemas/capabilities.schema.json").read_bytes()
    module = package.run([package.python, "-c", "import camctl; print(camctl.__file__)"])
    assert Path(module.strip()).is_relative_to(package.root / "venv")
    assert not (tmp_path / "builtin/state.db").exists(), "describe 不应初始化状态库"


def test_installed_arbitrary_driver_keeps_empty_actions_and_individual_sources(package, tmp_path):
    config = _config(package, tmp_path / "arbitrary", fixtures=True)
    text = _describe(package, config, fixture=True)
    assert _document(text) == _document(_describe(package, config, fixture=True, checkout=True))
    devices = {device["device_id"]: device for device in _document(text)["devices"]}
    assert devices["future-host"]["actions"] == devices["future-device"]["actions"] == []
    assert devices["future-host"]["delete_supported"] is True
    assert devices["future-device"]["delete_supported"] is False
    assert devices["future-delete"]["delete_supported"] is True


def test_installed_source_change_keeps_accepted_execution_definition(package, tmp_path):
    directory = tmp_path / "source-change"
    config = _config(package, directory)
    package.cli("init", "--config", str(config))
    now = datetime.now(timezone.utc)
    plan = {"request_id": "81001", "name": "源模式固化", "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "actions": [{"name": "capture", "type": "camera_take_photo", "device_id": "hb-host",
                         "scheduled_at": (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                         "policy": {"max_delay_ms": 30000}, "params": {"type": "hasselblad_photo"}}]}
    request = directory / "request.json"
    request.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf8")
    package.cli("submit", str(request), "--config", str(config))
    with sqlite3.connect(directory / "state.db") as connection:
        original = connection.execute("SELECT execution_spec_json FROM actions").fetchone()[0]
    assert json.loads(original)["output_storage"] == "host"
    _config(package, directory, host_storage="device")
    changed = _document(_describe(package, config))
    assert next(device for device in changed["devices"] if device["device_id"] == "hb-host")["delete_supported"] is False
    package.cli("submit", str(request), "--config", str(config))
    plan["request_id"] = "81002"
    request.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf8")
    package.cli("submit", str(request), "--config", str(config))
    with sqlite3.connect(directory / "state.db") as connection:
        rows = connection.execute("SELECT execution_spec_json FROM actions ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0][0] == original
    assert json.loads(rows[1][0])["output_storage"] == "device"
    assert not (package.root / "device-calls").exists()


def test_installed_document_survives_client_loader_http_and_invalid_reload(package, tmp_path):
    config = _config(package, tmp_path / "client-config", fixtures=True)
    text = _describe(package, config, fixture=True)
    store = tmp_path / "client"
    store.mkdir()
    (store / "device-capabilities.json").write_text(text, encoding="utf8")
    receipt = tmp_path / "receipt.json"
    package.run(["node", "--import", CLIENT / "node_modules/tsx/dist/loader.mjs",
                 REPO / "tests/integration/client_driver.ts", "capability-state", store, receipt], cwd=REPO)
    result = _document(receipt.read_text())
    assert result["loaded"] == result["http"] == _document(text)
    assert result["invalid_reloads"] == [True] * 5
    legacy = _document(text)
    for device in legacy["devices"]:
        del device["delete_supported"]
    assert result["legacy"] == legacy
    assert result["failed_startup"] is True
    assert result["empty"] == {"devices": []}


def test_installed_metadata_reaches_parameter_form_and_device_guide(package, client_build, tmp_path):
    config = _config(package, tmp_path / "browser-config")
    capabilities = tmp_path / "installed-capabilities.json"
    capabilities.write_text(_describe(package, config), encoding="utf8")
    package.run([CLIENT / "node_modules/.bin/vitest", "run",
                 "tests/integration/browser-camera-parameters.test.ts",
                 "tests/integration/browser-editing.test.ts", "-t", "能力源"],
                cwd=CLIENT, env={"CAMCTL_TEST_CAPABILITIES_FILE": str(capabilities)}, timeout=180)


def test_installed_sources_control_browser_cleanup_across_reload_and_restart(package, client_build, tmp_path):
    directory = tmp_path / "visibility-config"
    config = _config(package, directory, fixtures=True)
    current = tmp_path / "current.json"
    current.write_text(_describe(package, config, fixture=True), encoding="utf8")
    _config(package, directory, host_storage="device", fixtures=True)
    changed = tmp_path / "changed.json"
    changed.write_text(_describe(package, config, fixture=True), encoding="utf8")
    receipt = tmp_path / "visibility.json"
    package.run(["node", "--import", CLIENT / "node_modules/tsx/dist/loader.mjs",
                 REPO / "tests/integration/device_capability_browser.ts",
                 tmp_path / "browser-store", current, changed, receipt], cwd=CLIENT)
    result = json.loads(receipt.read_text())
    assert result == {
        "installed": True, "changed_source": True, "restored_source": True,
        "invalid_reload": True, "duplicate_key_reload": True,
        "failed_startup": True, "empty_directory": True, "saved_inputs_unchanged": True,
    }
