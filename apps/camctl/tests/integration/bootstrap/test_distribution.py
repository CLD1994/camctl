"""B7 正式发行物在源码仓库之外的安装与运行验证（组件集成测试）。

从仓库权威输入构建正式 wheel，在仓库之外的独立虚拟环境中按锁文件
安装运行时依赖并安装发行物；随后完全经安装后的 camctl CLI 执行
init、describe、submit 与设备替身 run，工作目录与进程环境都不依赖
源码仓库。另验证两条故障分支（包资源缺失、SQLite 条件不满足）明确
失败，以及安装环境不携带测试依赖。

这是集成测试：执行包构建、依赖导出与安装，不连接真实设备；设备
行为由与跨组件测试同源的受契约约束替身提供，替身脚本随测试复制
到部署目录，不把仓库路径带入被测进程。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import tomllib
import zipfile
from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = PROJECT_DIR.parents[1]
_CROSS_DIR = REPO_ROOT / "tests" / "integration"
_BRIDGE_SOURCE = _CROSS_DIR / "_camctl_stub_entry.py"
_FIXTURES_SOURCE = _CROSS_DIR / "camctl_fixtures.py"

#: 直接运行时依赖（wheel 声明与锁文件导出都应只包含这些包）。
_DIRECT_DEPENDENCIES = ("concurrent-log-handler", "janus", "jsonschema")

#: 模拟不满足统一运行条件的 SQLite 版本（低于主线最低版本）。
_NONCOMPLIANT_SQLITE = (3, 40, 0)


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, (
        f"命令失败: {command}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _venv_python(env_dir: Path) -> Path:
    if sys.platform == "win32":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _clean_environment() -> dict[str, str]:
    """运行环境：不继承任何指向源码仓库的 Python 查找路径。"""
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    return environment


@dataclass(frozen=True)
class _Installed:
    """仓库之外的安装环境：解释器、部署目录与状态库路径。"""

    python: Path
    deploy: Path
    state_db: Path
    config: Path
    ready: Path
    wheel: Path
    requirements: Path

    def cli(self, *args: str, driver: dict | None = None,
            timeout_s: float = 180.0) -> subprocess.CompletedProcess:
        """经部署装配桥运行安装后的 camctl；driver 提供时接入替身。

        工作目录保持在仓库之外；桥与替身脚本都来自部署目录中的
        副本，被测进程的 import 查找不经过源码仓库。
        """
        environment = _clean_environment()
        environment["CAMCTL_TEST_STATE_DB"] = str(self.state_db)
        command = [str(self.python), str(self.deploy / "_camctl_stub_entry.py"), *args]
        if driver is not None:
            spec = dict(driver)
            spec["gate_dir"] = str(self.deploy / "gates")
            spec_path = self.deploy / "driver-spec.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            environment["CAMCTL_TEST_DRIVER_SPEC"] = str(spec_path)
        return subprocess.run(
            command, capture_output=True, text=True, env=environment,
            cwd=str(self.deploy), timeout=timeout_s)


def _toml_path(path: Path) -> str:
    """TOML 基本字符串路径：反斜杠转义后可移植。"""
    return str(path).replace("\\", "\\\\")


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> _Installed:
    """构建正式 wheel 并安装到仓库之外的独立环境。"""
    root = tmp_path_factory.mktemp("b7-distribution")

    dist_dir = root / "dist"
    dist_dir.mkdir()
    _run(["uv", "build", "--project", str(PROJECT_DIR), "--wheel",
          "--out-dir", str(dist_dir)])
    wheels = sorted(dist_dir.glob("camctl-*.whl"))
    assert len(wheels) == 1, f"期望恰好一个 wheel，实际: {wheels}"
    wheel = wheels[0]

    # 运行时依赖只从锁文件导出版本取得；--no-dev 排除测试组。
    requirements = root / "requirements.txt"
    _run(["uv", "export", "--project", str(PROJECT_DIR), "--frozen",
          "--no-dev", "--no-emit-project", "-o", str(requirements)])

    # 独立解释器环境：复用当前测试解释器（目标 Python 3.11，
    # 实际链接的 SQLite 已满足统一运行条件）。
    env_dir = root / "env"
    _run(["uv", "venv", "--python", sys.executable, str(env_dir)])
    python = _venv_python(env_dir)
    _run(["uv", "pip", "install", "--python", str(python),
          "-r", str(requirements)])
    _run(["uv", "pip", "install", "--no-deps", "--python", str(python),
          str(wheel)])

    deploy = root / "deploy"
    home = deploy / "deployment"
    for directory in ((home / "staging"), (home / "ready"),
                      (home / "processing"), (deploy / "gates")):
        directory.mkdir(parents=True)
    state_db = home / "state.db"
    config = home / "config.toml"
    config.write_text(
        "\n".join([
            "[paths]",
            f'state_db = "{_toml_path(state_db)}"',
            f'log_file = "{_toml_path(home / "camctl.log")}"',
            f'staging = "{_toml_path(home / "staging")}"',
            f'ready = "{_toml_path(home / "ready")}"',
            f'processing = "{_toml_path(home / "processing")}"',
            "",
            "[clock]",
            'min_plausible_date = "2025-01-01"',
            "",
            "[devices.cam-1]",
            'kind = "camera"',
            'driver = "test-stub"',
            "",
            "[devices.cam-1.result_check]",
            'retry_interval_s = "0"',
            "",
            "[devices.cam-1.cleanup]",
            'delete_retry_interval_s = "0"',
            'query_retry_interval_s = "0"',
            "",
        ]),
        encoding="utf-8",
    )

    # 替身基础设施以副本进入部署目录：被测进程不经仓库路径取用。
    shutil.copy2(_BRIDGE_SOURCE, deploy / _BRIDGE_SOURCE.name)
    shutil.copy2(_FIXTURES_SOURCE, deploy / _FIXTURES_SOURCE.name)
    shutil.copy2(_CROSS_DIR / "camera_demo_fixtures.py", deploy / "camera_demo_fixtures.py")

    return _Installed(
        python=python, deploy=deploy, state_db=state_db,
        config=config, ready=home / "ready", wheel=wheel,
        requirements=requirements)


def _probe(installed: _Installed, code: str) -> str:
    result = _run([str(installed.python), "-c", code],
                  cwd=str(installed.deploy), env=_clean_environment())
    return result.stdout.strip()


def _production_cli(installed: _Installed, *args: str) -> subprocess.CompletedProcess:
    """直接运行安装包 CLI，不经过设备替身装配桥。"""
    environment = _clean_environment()
    environment.pop("CAMCTL_TEST_DRIVER_SPEC", None)
    environment.pop("CAMCTL_TEST_STATE_DB", None)
    return subprocess.run(
        [str(installed.python), "-m", "camctl", *args],
        capture_output=True, text=True, env=environment,
        cwd=str(installed.deploy), timeout=180.0)


_HASSELBLAD_ORIGINAL_BYTES = b"II*\x00small integration fixture"
_HASSELBLAD_PREVIEW_BYTES = b"\xff\xd8small preview fixture\xff\xd9"


def _hasselblad_tool_source(call_log: Path, serial: str) -> str:
    """受 hb_cli 实际 argv/文本响应契约约束的最小可执行替身。"""
    return "#!" + sys.executable + "\n" + textwrap.dedent(
        f"""\
        import json
        import pathlib
        import sys

        CALL_LOG = pathlib.Path({str(call_log)!r})
        SERIAL = {serial!r}
        RAW = bytes.fromhex({_HASSELBLAD_ORIGINAL_BYTES.hex()!r})
        PREVIEW = bytes.fromhex({_HASSELBLAD_PREVIEW_BYTES.hex()!r})
        argv = sys.argv
        with CALL_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(argv, ensure_ascii=True) + "\\n")

        args = argv[1:]
        if args[:2] != ["--pid", "0x000a"]:
            raise SystemExit("unexpected fixed camera PID arguments")
        if args == ["--pid", "0x000a", "list"]:
            sys.stdout.write("vid=0x2756 pid=0x000a  bus=  1 addr= 23\\n")
        elif args == ["--pid", "0x000a", "info"]:
            sys.stdout.write(f"serial : {{SERIAL}}\\nfirmware : v4.1.0\\n")
        elif args in (
            ["--pid", "0x000a", "set", "drive-mode", "single"],
            ["--pid", "0x000a", "set", "capture-format", "raw"],
        ):
            pass
        elif len(args) == 5 and args[:4] == [
            "--pid", "0x000a", "capture", "--output"
        ]:
            work = pathlib.Path(args[4])
            if not work.is_absolute():
                raise SystemExit("capture output directory must be absolute")
            original = work / "fixture_capture.3FR"
            preview = work / "fixture_capture.3FR.thumbnail.jpg"
            original.write_bytes(RAW)
            preview.write_bytes(PREVIEW)
            sys.stdout.write(
                "capture: OK\\n"
                f"saved {{original}} ({{len(RAW)}} bytes)\\n"
                f"saved {{preview}} ({{len(PREVIEW)}} bytes)\\n"
            )
        elif len(args) == 7 and args[2] == "download" and args[4] == "fixture_capture.3FR" and args[5] == "--output":
            if args[3] not in ("original", "thumbnail"):
                raise SystemExit("unexpected download variant")
            work = pathlib.Path(args[6])
            (work / "download.bin").write_bytes(RAW if args[3] == "original" else PREVIEW)
        else:
            raise SystemExit("unexpected hb_cli argument vector")
        """
    )


def _published_reports(ready: Path) -> list[tuple[int, Path, dict]]:
    reports = []
    for path in ready.glob("status-report-*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        reports.append((int(document["report_id"]), path, document))
    return sorted(reports, key=lambda item: item[0])


def _actions_in_report(report: dict) -> list[dict]:
    return [action for plan in report.get("plans", [])
            for action in plan.get("actions", [])]


def _report_containing_actions(ready: Path, names: set[str]) -> tuple[Path, dict]:
    candidates = []
    observed = []
    for report_id, path, report in _published_reports(ready):
        actions = _actions_in_report(report)
        observed.append((report_id, [(item.get("name"), item.get("status"))
                                     for item in actions]))
        by_name = {item.get("name"): item for item in actions}
        if names <= by_name.keys():
            candidates.append((report_id, path, report, by_name))
    assert candidates, f"已发布报告未包含动作 {sorted(names)}；观察到 {observed}"
    _, path, report, _ = candidates[-1]
    return path, report


def _locked_versions(requirements_text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _DIRECT_DEPENDENCIES:
        match = re.search(rf"^{re.escape(name)}==([^\s\\]+)",
                          requirements_text, re.MULTILINE)
        assert match is not None, f"锁文件导出缺少 {name} 的钉定版本"
        versions[name] = match.group(1)
    return versions


def _photo_plan(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


def _photo_driver() -> dict:
    """单张照片替身剧本：首个动作的列举结果是一张完整照片。"""
    return {
        "driver_id": "test-stub",
        "timelapse_duration_s": 3.0,
        "record_duration_s": 1.0,
        "files": {"1": [{
            "identity": "shot-1",
            "locator": {"path": "/DCIM/shot-1"},
            "size_bytes": 4096,
            "complete": True,
            "kind": "photo",
            "original_name": "shot-1.jpg",
            "media_type": "image/jpeg",
        }]},
        "gates": {},
        "device_files": {},
        "delete_error": None,
        "photo_preview_supported": False,
    }


@pytest.mark.parametrize("model", ["dji-action6", "dji-osmo360-ii"])
def test_installed_and_checkout_cli_have_same_camera_catalog(installed, model):
    config = installed.deploy / f"{model}.toml"
    config.write_text(f'[devices.cam-1]\nkind = "camera"\ndriver = "{model}"\n'
                      '[devices.cam-1.adb]\nserial = "explicit-serial"\n', encoding="utf-8")
    args = ["-m", "camctl", "describe", "--config", str(config)]
    packaged = _run([str(installed.python), *args], cwd=installed.deploy, env=_clean_environment())
    checkout_environment = _clean_environment()
    checkout_environment["PYTHONPATH"] = str(PROJECT_DIR / "src")
    checkout = _run([sys.executable, *args], cwd=installed.deploy, env=checkout_environment)
    document = json.loads(packaged.stdout)
    assert document == json.loads(checkout.stdout)
    assert document["devices"][0]["device_id"] == "cam-1"
    assert document["devices"][0]["driver_id"] == model
    assert [action["type"] for action in document["devices"][0]["actions"]] == (
        ["camera_record"] if model == "dji-action6" else [])
    with zipfile.ZipFile(installed.wheel) as archive:
        assert "camctl/devices/drivers/adb_cameras/registration.py" in archive.namelist()
        assert "camctl/devices/drivers/adb_cameras/driver.py" in archive.namelist()


def _demo_generator(installed):
    target = installed.deploy / "prepare-plan.py"
    _probe(installed, f"from pathlib import Path; from camctl.resources import resource_bytes; "
           f"Path({str(target)!r}).write_bytes(resource_bytes('examples/camera-demo/prepare-plan.py'))")
    return target


@pytest.mark.parametrize("stem", ["action6-record", "action6-timelapse", "osmo360ii-record", "osmo360ii-timelapse"])
def test_installed_camera_sample_is_accepted_from_actual_describe(installed, tmp_path, stem):
    """安装后的样例消费实际能力说明，经正式受理保存原参数。"""
    sys.path.insert(0, str(_CROSS_DIR))
    from camctl_fixtures import Deployment
    from camera_demo_fixtures import camera_spec, recording_params
    from camctl.contracts.json_values import parse_exact_json
    from camctl.devices.drivers.adb_cameras.commands import CameraModel
    model = CameraModel.ACTION6 if stem.startswith("action6") else CameraModel.OSMO360II
    if stem.endswith("-record"):
        expected = recording_params(model)
    else:
        sample_path = REPO_ROOT / "docs" / "camctl" / "examples" / "camera-demo" / f"{stem}.json"
        sample = parse_exact_json(sample_path.read_text(encoding="utf-8"))
        sample_action = sample["actions"][0]
        assert sample_action["type"] == "camera_timelapse"
        expected = sample_action["params"]
        expected_type_by_model = {
            CameraModel.ACTION6: "action6_timelapse",
            CameraModel.OSMO360II: "osmo360ii_timelapse",
        }
        expected_exposure_by_model = {
            CameraModel.ACTION6: {
                "mode": "auto",
            },
            CameraModel.OSMO360II: {
                "mode": "manual", "iso": 800,
            },
        }
        assert set(expected) == {"type", "interval_s", "duration_s", "outputs", "exposure"}
        assert expected["type"] == expected_type_by_model[model]
        assert expected["interval_s"] == 1
        assert expected["duration_s"] == 300
        assert expected["outputs"] == "video"
        assert expected["exposure"] == expected_exposure_by_model[model]
    if stem == "action6-record":
        expected.pop("aperture")
        expected.pop("bitrate")
    deployment = Deployment(tmp_path, devices=False)
    spec = camera_spec(deployment, model, expected)
    initialized = installed.cli("init", "--config", str(deployment.config_path))
    assert initialized.returncode == 0, initialized.stderr
    described = installed.cli("describe", "--config", str(deployment.config_path), driver=spec)
    assert described.returncode == 0, described.stderr
    capabilities = tmp_path / "capabilities.json"
    capabilities.write_text(described.stdout)
    plan_path = tmp_path / "sample-plan.json"
    generator = _demo_generator(installed)
    _run([str(installed.python), str(generator), stem, "--capabilities", str(capabilities),
          "--device-id", "camera", "--output", str(plan_path)],
         cwd=installed.deploy, env=_clean_environment())
    document = json.loads(plan_path.read_text())
    assert document["actions"][0]["params"] == expected
    assert 1 <= int(document["request_id"]) <= 9223372036854775807
    submitted = installed.cli("submit", str(plan_path), "--config", str(deployment.config_path), driver=spec)
    assert submitted.returncode == 0, submitted.stderr
    with closing(sqlite3.connect(deployment.state_db)) as connection:
        rows = connection.execute("SELECT status,effective_params_json FROM actions").fetchall()
    assert len(rows) == 1 and rows[0][0] == 1
    assert json.loads(rows[0][1]) == expected


def test_installed_osmo_camera_config_is_single_device_default_config(installed):
    """安装资源只绑定 OSMO 设备，使用默认状态库和交接目录。"""
    config_path = installed.deploy / "osmo360ii-config.toml"
    _probe(
        installed,
        "from pathlib import Path; from camctl.resources import resource_bytes; "
        f"Path({str(config_path)!r}).write_bytes(resource_bytes("
        "'examples/camera-demo/osmo360ii-config.toml'))",
    )
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config == {
        "devices": {
            "osmo360ii": {
                "kind": "camera",
                "driver": "dji-osmo360-ii",
                "adb": {"serial": "REPLACE_OSMO360II_SERIAL"},
            },
        },
    }


def test_installed_obtain_and_ack_samples_preserve_exact_ids(installed, tmp_path):
    generator = _demo_generator(installed)
    maximum = "9223372036854775807"
    for stem, extra in (("obtain", ["--source-action-id", maximum]),
                        ("obtain-preview", ["--source-action-id", maximum]),
                        ("report-ack", [])):
        target = tmp_path / f"{stem}.json"
        _run([str(installed.python), str(generator), stem, *extra,
              "--last-report-id", maximum, "--output", str(target)],
             cwd=installed.deploy, env=_clean_environment())
        plan = json.loads(target.read_text())
        assert plan["last_report_id"] == maximum
        if stem in ("obtain", "obtain-preview"):
            assert plan["actions"][0]["params"]["source"]["action_instance_id"] == maximum
        if stem == "obtain-preview":
            assert plan["actions"][0]["params"]["filter"] == "preview"
        _probe(installed, "from camctl.contracts.json_values import parse_exact_json; "
               "from camctl.contracts.schemas import validate_document; "
               f"from pathlib import Path; validate_document('protocol/plan.schema.json', "
               f"parse_exact_json(Path({str(target)!r}).read_text()))")


def test_installed_hasselblad_sample_uses_actual_unique_capability(installed, tmp_path):
    """安装物的 Hasselblad 配置与样例经真实 describe 和 submit 组合运行。"""
    resource_names = (
        "examples/camera-demo/hasselblad-config.toml",
        "examples/camera-demo/hasselblad-photo.json",
        "examples/camera-demo/obtain-preview.json",
    )
    available = set(json.loads(_probe(
        installed,
        "import json, sys; from camctl.resources import available_resources; "
        "sys.stdout.write(json.dumps(sorted(available_resources())))")))
    assert set(resource_names) <= available

    config_path = installed.deploy / "hasselblad-config.toml"
    # 从安装包资源复制配置，测试进程不读取源码树中的样例。
    _probe(
        installed,
        "from pathlib import Path; from camctl.resources import resource_bytes; "
        f"Path({str(config_path)!r}).write_bytes(resource_bytes({resource_names[0]!r}))")
    packaged_config = config_path.read_text(encoding="utf-8")
    data_root = tmp_path / "hasselblad-data"
    fake_executable = tmp_path / "fake-bin" / "hb_cli"
    configured = (packaged_config
                  .replace("$HOME/.camctl/hasselblad-demo", str(data_root))
                  .replace("/home/cld/hb_cli/hb_cli", str(fake_executable))
                  .replace("REPLACE_HASSELBLAD_SERIAL", "TEST-HASSELBLAD-SERIAL"))
    # 已安装默认流程分批发现两份主机源及两次独立取回。
    configured += "\n[session]\nread_discovery_batch_size = 1\n"
    assert "$HOME/.camctl/hasselblad-demo" not in configured
    assert "REPLACE_HASSELBLAD_SERIAL" not in configured
    assert str(fake_executable) in configured
    assert "product_id = 10" in configured
    assert "password" not in configured.lower() and "ip_address" not in configured.lower()
    config_path.write_text(configured, encoding="utf-8")
    for directory in ("state", "log", "staging", "ready", "processing"):
        (data_root / directory).mkdir(parents=True)

    initialized = _production_cli(installed, "init", "--config", str(config_path))
    assert initialized.returncode == 0, initialized.stderr
    described = _production_cli(installed, "describe", "--config", str(config_path))
    assert described.returncode == 0, described.stderr
    capabilities = json.loads(described.stdout)
    assert len(capabilities["devices"]) == 1
    device = capabilities["devices"][0]
    assert device["device_id"] == "hb0"
    assert device["driver_id"] == "hasselblad_cfv100c"
    photo_actions = [item for item in device["actions"]
                     if item["type"] == "camera_take_photo"]
    assert len(photo_actions) == 1
    photo_parameters = [parameter for action in photo_actions
                        for parameter in action["parameter_types"]
                        if parameter["type"] == "hasselblad_photo"]
    assert len(photo_parameters) == 1

    generator = _demo_generator(installed)
    capability_path = tmp_path / "capabilities.json"
    capability_path.write_text(described.stdout, encoding="utf-8")
    plan_path = tmp_path / "hasselblad-photo-plan.json"
    generated = datetime.now(timezone.utc)
    generated_plan = _run([str(installed.python), str(generator), "hasselblad-photo",
                           "--capabilities", str(capability_path), "--device-id", "hb0",
                           "--output", str(plan_path)],
                          cwd=installed.deploy, env=_clean_environment())
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    action = plan["actions"][0]
    assert action["type"] == "camera_take_photo"
    assert action["device_id"] == "hb0"
    assert action["params"] == {"type": "hasselblad_photo"}
    assert action["policy"] == {"max_delay_ms": 30000}
    assert 1 <= int(plan["request_id"]) <= 9223372036854775807
    assert plan["request_id"] != "1"
    assert generated_plan.stdout.splitlines()[0] == "request_id=" + plan["request_id"]
    assert plan["created_at"] != "2026-01-01 00:00:00"
    assert action["scheduled_at"] != "2026-01-01 00:00:10"
    assert datetime.strptime(action["scheduled_at"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc) > generated

    submitted = _production_cli(
        installed, "submit", str(plan_path), "--config", str(config_path))
    assert submitted.returncode == 0, submitted.stderr
    state_db = data_root / "state" / "state.db"
    with closing(sqlite3.connect(state_db)) as connection:
        rows = connection.execute(
            "SELECT effective_params_json,execution_spec_json FROM actions").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == {"type": "hasselblad_photo"}
    assert json.loads(rows[0][1]) == {
        "output_storage": "host", "start_call_timeout_s": 180,
    }

    # 缺少唯一的实际参数能力时，生成器必须在创建计划文件前拒绝。
    for case in ("missing", "duplicate"):
        altered = deepcopy(capabilities)
        altered_device = altered["devices"][0]
        altered_action = next(item for item in altered_device["actions"]
                              if item["type"] == "camera_take_photo")
        if case == "missing":
            altered_device["actions"].remove(altered_action)
        else:
            altered_action["parameter_types"].append(
                deepcopy(next(parameter for parameter in altered_action["parameter_types"]
                              if parameter["type"] == "hasselblad_photo")))
        altered_path = tmp_path / f"capabilities-{case}.json"
        altered_path.write_text(json.dumps(altered), encoding="utf-8")
        refused_plan = tmp_path / f"hasselblad-photo-{case}.json"
        refused = subprocess.run(
            [str(installed.python), str(generator), "hasselblad-photo",
             "--capabilities", str(altered_path), "--output", str(refused_plan)],
            cwd=installed.deploy, env=_clean_environment(), capture_output=True,
            text=True, timeout=30)
        assert refused.returncode != 0
        assert not refused_plan.exists()


@pytest.mark.skipif(os.name == "nt", reason="受管工具替身使用 POSIX 可执行脚本")
def test_installed_hasselblad_photo_pair_obtain_and_source_reuse(installed, tmp_path):
    """安装版 CLI 完成主机拍摄、原图/预览交付及保留源再次取回。"""
    resource_names = (
        "examples/camera-demo/hasselblad-config.toml",
        "examples/camera-demo/hasselblad-photo.json",
        "examples/camera-demo/obtain.json",
        "examples/camera-demo/obtain-preview.json",
    )
    available = set(json.loads(_probe(
        installed,
        "import json, sys; from camctl.resources import available_resources; "
        "sys.stdout.write(json.dumps(sorted(available_resources())))")))
    assert set(resource_names) <= available

    config_path = installed.deploy / "hasselblad-full-chain.toml"
    _probe(
        installed,
        "from pathlib import Path; from camctl.resources import resource_bytes; "
        f"Path({str(config_path)!r}).write_bytes(resource_bytes({resource_names[0]!r}))")
    packaged_config = config_path.read_text(encoding="utf-8")
    data_root = tmp_path / "hasselblad-installed-data"
    fake_executable = tmp_path / "fake-bin" / "hb_cli"
    fake_executable.parent.mkdir()
    call_log = tmp_path / "hb-cli-argv.jsonl"
    fake_executable.write_text(
        _hasselblad_tool_source(call_log, "TEST-HASSELBLAD-SERIAL"), encoding="utf-8")
    fake_executable.chmod(0o700)
    configured = (packaged_config
                  .replace("$HOME/.camctl/hasselblad-demo", str(data_root))
                  .replace("/home/cld/hb_cli/hb_cli", str(fake_executable))
                  .replace("REPLACE_HASSELBLAD_SERIAL", "TEST-HASSELBLAD-SERIAL"))
    assert "$HOME/.camctl/hasselblad-demo" not in configured
    assert "REPLACE_HASSELBLAD_SERIAL" not in configured
    assert str(fake_executable) in configured
    config_path.write_text(configured, encoding="utf-8")

    initialized = _production_cli(installed, "init", "--config", str(config_path))
    assert initialized.returncode == 0, (
        f"init 阶段失败: stdout={initialized.stdout!r}; stderr={initialized.stderr!r}")
    assert (data_root / "staging" / "capture-work").is_dir()
    assert (data_root / "ready").is_dir()
    assert (data_root / "processing").is_dir()

    described = _production_cli(installed, "describe", "--config", str(config_path))
    assert described.returncode == 0, (
        f"describe 阶段失败: stdout={described.stdout!r}; stderr={described.stderr!r}")
    capabilities = json.loads(described.stdout)
    assert len(capabilities["devices"]) == 1
    device = capabilities["devices"][0]
    assert device["device_id"] == "hb0"
    assert device["driver_id"] == "hasselblad_cfv100c"
    photo_actions = [action for action in device["actions"]
                     if action["type"] == "camera_take_photo"]
    assert len(photo_actions) == 1
    assert [parameter["type"] for parameter in photo_actions[0]["parameter_types"]] == [
        "hasselblad_photo"]

    generator = _demo_generator(installed)
    capability_path = tmp_path / "hasselblad-capabilities.json"
    capability_path.write_text(described.stdout, encoding="utf-8")
    generated_plan_path = tmp_path / "hasselblad-photo-generated.json"
    generation_started = datetime.now(timezone.utc)
    generated = _run([
        str(installed.python), str(generator), "hasselblad-photo",
        "--capabilities", str(capability_path), "--device-id", "hb0",
        "--delay-s", "1", "--output", str(generated_plan_path),
    ], cwd=installed.deploy, env=_clean_environment())
    plan = json.loads(generated_plan_path.read_text(encoding="utf-8"))
    photo_action = plan["actions"][0]
    assert photo_action["type"] == "camera_take_photo"
    assert photo_action["device_id"] == "hb0"
    assert photo_action["params"] == {"type": "hasselblad_photo"}
    assert plan["request_id"] != "1"
    scheduled_at = datetime.strptime(
        photo_action["scheduled_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert scheduled_at > generation_started
    assert generated.stdout.splitlines()[0] == "request_id=" + plan["request_id"]

    # 从已安装发行物读取两个正式取回模板，沿用其筛选规则；两项均以
    # 当前计划中的拍摄动作名解析来源，由受理层保存依赖关系。
    for resource_name, action_name in (
        (resource_names[2], "obtain-original"),
        (resource_names[3], "obtain-preview"),
    ):
        template = json.loads(_probe(
            installed,
            "import sys; from camctl.resources import resource_bytes; "
            f"sys.stdout.write(resource_bytes({resource_name!r}).decode('utf-8'))"))
        obtain_action = deepcopy(template["actions"][0])
        obtain_action["name"] = action_name
        obtain_action["scheduled_at"] = photo_action["scheduled_at"]
        obtain_action["params"]["source"] = {"action_name": "capture"}
        plan["actions"].append(obtain_action)
    plan["name"] = "hasselblad-paired-photo"
    plan_path = tmp_path / "hasselblad-paired-photo-plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    assert plan["actions"][1]["params"] == {"source": {"action_name": "capture"}}
    assert plan["actions"][2]["params"] == {
        "source": {"action_name": "capture"}, "filter": "preview",
    }

    submitted = _production_cli(
        installed, "submit", str(plan_path), "--config", str(config_path))
    assert submitted.returncode == 0, (
        f"paired submit 阶段失败: stdout={submitted.stdout!r}; stderr={submitted.stderr!r}")
    state_db = data_root / "state" / "state.db"
    with closing(sqlite3.connect(state_db)) as connection:
        format_version = connection.execute(
            "SELECT format_version FROM database_metadata WHERE id=1").fetchone()[0]
        rows = connection.execute(
            "SELECT name,effective_params_json,execution_spec_json FROM actions"
            " ORDER BY input_index").fetchall()
    assert format_version == 2
    assert len(rows) == 3
    assert json.loads(rows[0][1]) == {"type": "hasselblad_photo"}
    assert json.loads(rows[0][2]) == {
        "output_storage": "host", "start_call_timeout_s": 180,
    }

    first_run = _production_cli(
        installed, "run", str(plan_path), "--config", str(config_path))
    assert first_run.returncode == 0, (
        f"paired run 阶段失败: stdout={first_run.stdout!r}; stderr={first_run.stderr!r}")
    first_report_path, first_report = _report_containing_actions(
        data_root / "ready", {"capture", "obtain-original", "obtain-preview"})
    first_actions = {action["name"]: action for action in _actions_in_report(first_report)}
    for action_name in ("capture", "obtain-original", "obtain-preview"):
        assert first_actions[action_name]["status"] == "succeeded", (
            f"paired run 报告动作 {action_name} 非 succeeded: "
            f"{first_actions[action_name]!r}")
    assert "device_execution" not in first_actions["capture"]
    photo_id = first_actions["capture"]["action_instance_id"]
    public_outputs = {item["kind"]: item for item in first_actions["capture"]["outputs"]}
    assert set(public_outputs) == {"original", "preview"}
    original_public = public_outputs["original"]
    preview_public = public_outputs["preview"]
    assert preview_public["preview_of_output_id"] == original_public["output_id"]

    expected_files = {
        "fixture_capture.3FR": (
            _HASSELBLAD_ORIGINAL_BYTES, "image/x-hasselblad-3fr", "original"),
        "fixture_capture.3FR.thumbnail.jpg": (
            _HASSELBLAD_PREVIEW_BYTES, "image/jpeg", "preview"),
    }
    staging = data_root / "staging"
    with closing(sqlite3.connect(state_db)) as connection:
        source_rows = connection.execute(
            "SELECT o.id,o.original_name,o.media_type,i.id,i.relative_path,i.size_bytes,i.sha256"
            " FROM outputs AS o JOIN intermediate_files AS i"
            " ON i.id=o.intermediate_file_id"
            " WHERE o.source_action_id=? ORDER BY o.id", (int(photo_id),),
        ).fetchall()
    assert len(source_rows) == 2
    source_by_name = {row[1]: row for row in source_rows}
    assert set(source_by_name) == set(expected_files)
    source_snapshots = {}
    for name, (expected_bytes, expected_media_type, kind) in expected_files.items():
        output_id, _, media_type, file_id, relative_path, size_bytes, sha256 = source_by_name[name]
        source_path = staging / relative_path
        assert source_path.is_relative_to(staging)
        content = source_path.read_bytes()
        expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
        assert media_type == expected_media_type
        assert content == expected_bytes
        assert size_bytes == len(expected_bytes)
        assert sha256 == expected_sha256
        source_snapshots[name] = (output_id, file_id, relative_path, size_bytes, sha256, content)
        public = public_outputs[kind]
        assert public["output_id"] == str(output_id)
        assert public["source_action_instance_id"] == photo_id
        assert public["original_name"] == name
        assert public["media_type"] == expected_media_type
        assert public["size"] == len(expected_bytes)
        assert public["checksum"] == {"status": "available", "sha256": expected_sha256}

    obtain_ids = tuple(int(first_actions[name]["action_instance_id"])
                       for name in ("obtain-original", "obtain-preview"))
    with closing(sqlite3.connect(state_db)) as connection:
        first_delivery_rows = connection.execute(
            "SELECT d.id,d.output_id,d.file_name,c.target_file_id,t.relative_path,"
            "t.size_bytes,t.sha256 FROM deliveries AS d"
            " JOIN file_copies AS c ON c.delivery_id=d.id"
            " JOIN intermediate_files AS t ON t.id=c.target_file_id"
            " WHERE d.action_id IN (?,?) ORDER BY d.output_id", obtain_ids,
        ).fetchall()
    assert len(first_delivery_rows) == 2
    public_deliveries = {}
    for action_name in ("obtain-original", "obtain-preview"):
        action = first_actions[action_name]
        assert len(action["deliveries"]) == 1
        delivery = action["deliveries"][0]
        public_deliveries[delivery["output_id"]] = delivery
    ready = data_root / "ready"
    processing = data_root / "processing"
    first_delivery_snapshot = {}
    for row in first_delivery_rows:
        delivery_id, output_id, file_name, target_id, relative_path, size_bytes, sha256 = row
        source_name = next(name for name, facts in source_by_name.items()
                           if str(facts[0]) == str(output_id))
        expected_bytes = expected_files[source_name][0]
        expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
        ready_file = ready / file_name
        delivered_bytes = ready_file.read_bytes()
        assert delivered_bytes == expected_bytes
        assert size_bytes == len(expected_bytes)
        assert sha256 == expected_sha256
        public = public_deliveries[str(output_id)]
        assert public["delivery_id"] == str(delivery_id)
        assert public["output_id"] == str(output_id)
        assert public["source_action_instance_id"] == photo_id
        assert public["file_name"] == file_name
        assert public["status"] == "published"
        assert public["size"] == len(expected_bytes)
        assert public["sha256"] == expected_sha256
        first_delivery_snapshot[str(output_id)] = {
            "delivery_id": delivery_id,
            "file_name": file_name,
            "target_file_id": target_id,
            "relative_path": relative_path,
            "bytes": delivered_bytes,
            "sha256": sha256,
        }
    assert len({item["target_file_id"] for item in first_delivery_snapshot.values()}) == 2

    # 仅模拟主机领取并删除这两份 ready 文件，报告和拍摄正式源均保留。
    for delivery in first_delivery_snapshot.values():
        ready_file = ready / delivery["file_name"]
        processing_file = processing / delivery["file_name"]
        ready_file.replace(processing_file)
        assert processing_file.read_bytes() == delivery["bytes"]
        processing_file.unlink()
        assert not processing_file.exists()
    assert first_report_path.exists()
    assert not any((ready / delivery["file_name"]).exists()
                   for delivery in first_delivery_snapshot.values())
    assert not any((processing / delivery["file_name"]).exists()
                   for delivery in first_delivery_snapshot.values())
    for name, snapshot in source_snapshots.items():
        assert (staging / snapshot[2]).read_bytes() == expected_files[name][0]

    # 使用生产样例生成器从已发布报告取得的 action_instance_id 再次取原图。
    second_plan_path = tmp_path / "hasselblad-obtain-original-again.json"
    _run([
        str(installed.python), str(generator), "obtain",
        "--source-action-id", photo_id, "--delay-s", "1",
        "--output", str(second_plan_path),
    ], cwd=installed.deploy, env=_clean_environment())
    second_plan = json.loads(second_plan_path.read_text(encoding="utf-8"))
    second_action = second_plan["actions"][0]
    second_action["name"] = "obtain-original-again"
    second_plan["name"] = "hasselblad-obtain-original-again"
    second_action_instance_source = second_action["params"]["source"]["action_instance_id"]
    assert second_action_instance_source == photo_id
    assert second_action["type"] == "obtain_action_outputs"
    assert second_action["params"] == {"source": {"action_instance_id": photo_id}}
    second_plan_path.write_text(
        json.dumps(second_plan, ensure_ascii=False), encoding="utf-8")
    second_submit = _production_cli(
        installed, "submit", str(second_plan_path), "--config", str(config_path))
    assert second_submit.returncode == 0, (
        f"再次 obtain submit 阶段失败: stdout={second_submit.stdout!r}; "
        f"stderr={second_submit.stderr!r}")
    second_run = _production_cli(
        installed, "run", str(second_plan_path), "--config", str(config_path))
    assert second_run.returncode == 0, (
        f"再次 obtain run 阶段失败: stdout={second_run.stdout!r}; "
        f"stderr={second_run.stderr!r}")
    _, second_report = _report_containing_actions(ready, {"obtain-original-again"})
    second_actions = {action["name"]: action for action in _actions_in_report(second_report)}
    final_obtain = second_actions["obtain-original-again"]
    assert final_obtain["status"] == "succeeded", final_obtain
    assert len(final_obtain["deliveries"]) == 1
    second_public_delivery = final_obtain["deliveries"][0]
    assert second_public_delivery["output_id"] == original_public["output_id"]
    assert second_public_delivery["source_action_instance_id"] == photo_id
    with closing(sqlite3.connect(state_db)) as connection:
        second_delivery_row = connection.execute(
            "SELECT d.id,d.output_id,d.file_name,c.target_file_id,t.size_bytes,t.sha256"
            " FROM deliveries AS d JOIN file_copies AS c ON c.delivery_id=d.id"
            " JOIN intermediate_files AS t ON t.id=c.target_file_id"
            " WHERE d.action_id=?", (int(final_obtain["action_instance_id"]),),
        ).fetchone()
        retained_source_rows = connection.execute(
            "SELECT o.id,o.original_name,o.media_type,i.id,i.relative_path,i.size_bytes,i.sha256"
            " FROM outputs AS o JOIN intermediate_files AS i"
            " ON i.id=o.intermediate_file_id WHERE o.source_action_id=? ORDER BY o.id",
            (int(photo_id),),
        ).fetchall()
    assert second_delivery_row is not None
    (second_delivery_id, second_output_id, second_file_name, second_target_id,
     second_size, second_sha) = second_delivery_row
    original_id = original_public["output_id"]
    original_snapshot = first_delivery_snapshot[original_id]
    assert second_output_id == int(original_id)
    assert second_delivery_id != original_snapshot["delivery_id"]
    assert second_file_name != original_snapshot["file_name"]
    assert second_target_id != original_snapshot["target_file_id"]
    assert second_public_delivery["delivery_id"] == str(second_delivery_id)
    assert second_public_delivery["file_name"] == second_file_name
    assert second_public_delivery["status"] == "published"
    assert second_public_delivery["size"] == len(_HASSELBLAD_ORIGINAL_BYTES)
    assert second_public_delivery["sha256"] == hashlib.sha256(
        _HASSELBLAD_ORIGINAL_BYTES).hexdigest()
    assert second_size == len(_HASSELBLAD_ORIGINAL_BYTES)
    assert second_sha == hashlib.sha256(_HASSELBLAD_ORIGINAL_BYTES).hexdigest()
    assert (ready / second_file_name).read_bytes() == _HASSELBLAD_ORIGINAL_BYTES
    assert retained_source_rows == source_rows
    for name, snapshot in source_snapshots.items():
        assert (staging / snapshot[2]).read_bytes() == expected_files[name][0]

    logged_calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
    capture_calls = [argv for argv in logged_calls if len(argv) > 3 and argv[3] == "capture"]
    assert len(capture_calls) == 1
    assert capture_calls[0][1:5] == ["--pid", "0x000a", "capture", "--output"]


def test_installed_sample_rejects_unavailable_camera_before_writing_plan(installed, tmp_path):
    config = tmp_path / "pending.toml"
    config.write_text('[devices.osmo360ii]\nkind="camera"\ndriver="dji-osmo360-ii"\n'
                      '[devices.osmo360ii.adb]\nserial="explicit-serial"\n')
    described = installed.cli("describe", "--config", str(config))
    assert described.returncode == 0, described.stderr
    capabilities = tmp_path / "pending.json"
    capabilities.write_text(described.stdout)
    target = tmp_path / "plan.json"
    refused = subprocess.run([str(installed.python), str(_demo_generator(installed)), "osmo360ii-record",
        "--capabilities", str(capabilities), "--output", str(target)],
        cwd=installed.deploy, env=_clean_environment(), capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0
    assert not target.exists()


@pytest.mark.parametrize("stem,args", [("obtain", []), ("obtain", ["--source-action-id", "01"]),
                                      ("report-ack", []), ("report-ack", ["--last-report-id", "0"])])
def test_installed_sample_requires_valid_source_and_ack_ids(installed, tmp_path, stem, args):
    target = tmp_path / "plan.json"
    refused = subprocess.run([str(installed.python), str(_demo_generator(installed)), stem,
        *args, "--output", str(target)], cwd=installed.deploy, env=_clean_environment(),
        capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0
    assert not target.exists()


def test_installed_emergency_errors_keep_registered_details(installed: _Installed) -> None:
    """正式安装包保留两类应急错误登记，并拒绝不完整或无效的详情。"""
    result = _probe(installed, """
import sys
from pathlib import Path
from camctl.contracts import workflow_errors

assert Path(workflow_errors.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
for code, reason in (
    ("emergency_not_attempted", "会话退出前已无时间发起停止尝试"),
    ("emergency_stop_unconfirmed", "停止请求的响应未知，仍无法确认目标活动已停止"),
):
    spec = workflow_errors.registered_error(code)
    assert spec["stage"] == "emergency", code
    assert "action_error_id" not in spec and "item_error_ids" not in spec, code
    details_schema = spec["details_schema"]
    assert details_schema["type"] == "object", code
    assert set(details_schema["properties"]) == {"activity_id", "reason"}, code
    assert set(details_schema["required"]) == {"activity_id", "reason"}, code
    assert details_schema["additionalProperties"] is False, code

    error = {
        "code": code,
        "stage": "emergency",
        "details": {
            "activity_id": "9223372036854775807",
            "reason": reason,
        },
    }
    workflow_errors.validate_public_error(error)
    for invalid_details in (
        {"activity_id": "9223372036854775807"},
        {"activity_id": "0", "reason": "目标活动编号无效"},
    ):
        try:
            workflow_errors.validate_public_error({
                "code": code, "stage": "emergency", "details": invalid_details,
            })
        except ValueError:
            pass
        else:
            raise AssertionError(f"{code} 接受了无效详情: {invalid_details!r}")
print("validated")
""")
    assert result == "validated"


def test_distribution_works_outside_repository(installed: _Installed) -> None:
    """安装后的发行物独立完成 init/describe/submit 与设备替身 run。"""
    # 安装来源：包来自安装环境，不经源码仓库；包资源自包含可读。
    origin = _probe(
        installed,
        "import camctl; import sys; sys.stdout.write(camctl.__file__)")
    assert str(installed.python.parents[1]) in origin, (
        f"camctl 未从安装环境装载: {origin}")
    assert "workspace" not in origin.replace(str(installed.python.parents[1]), ""), (
        f"camctl 解析到了仓库路径: {origin}")
    resources = _probe(
        installed,
        "from camctl.resources import available_resources, resource_bytes;"
        "import json, sys;"
        "names = sorted(available_resources());"
        "data = resource_bytes('sql/core.sql');"
        "sys.stdout.write(json.dumps({'names': names, 'sql_bytes': len(data)}))")
    payload = json.loads(resources)
    assert "sql/core.sql" in payload["names"]
    assert payload["sql_bytes"] > 0

    # 依赖边界：wheel 只声明运行时依赖；直接依赖按锁文件版本安装，
    # 测试组不进入安装环境。
    with zipfile.ZipFile(installed.wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist()
            if name.endswith(".dist-info/METADATA"))
        metadata_text = archive.read(metadata_name).decode("utf-8")
    declared = re.findall(r"^Requires-Dist: ([A-Za-z0-9._-]+)",
                          metadata_text, re.MULTILINE)
    assert sorted(declared) == sorted(_DIRECT_DEPENDENCIES), (
        f"wheel 声明的依赖越界: {declared}")
    locked = _locked_versions(installed.requirements.read_text(encoding="utf-8"))
    installed_versions = json.loads(_probe(
        installed,
        "import importlib.metadata, json, sys;"
        "names = " + repr(list(_DIRECT_DEPENDENCIES)) + ";"
        "sys.stdout.write(json.dumps({n: importlib.metadata.version(n)"
        " for n in names}))"))
    assert installed_versions == locked, (
        f"安装版本与锁文件不一致: {installed_versions} != {locked}")
    for test_package in ("pytest", "pytest_asyncio", "pytest_mock"):
        found = _probe(
            installed,
            f"import importlib.util, sys;"
            f"sys.stdout.write(str(importlib.util.find_spec({test_package!r}) is not None))")
        assert found == "False", f"安装环境携带了测试依赖: {test_package}"

    # init：显式初始化状态库成功。
    initialized = installed.cli("init", "--config", str(installed.config))
    assert initialized.returncode == 0, initialized.stderr
    assert initialized.stdout == ""
    assert installed.state_db.is_file()

    # describe：替身登记的设备动作出现在能力说明中。
    described = installed.cli(
        "describe", "--config", str(installed.config), driver=_photo_driver())
    assert described.returncode == 0, described.stderr
    document = json.loads(described.stdout)
    assert len(document["devices"]) == 1, document
    assert "camera_take_photo" in described.stdout

    # submit：受理计划保存成功。
    scheduled = datetime.now(timezone.utc) + timedelta(seconds=2)
    plan_path = installed.deploy / "plan-8001.json"
    plan_path.write_text(json.dumps(
        _photo_plan("8001", scheduled.strftime("%Y-%m-%d %H:%M:%S")),
        ensure_ascii=False), encoding="utf-8")
    submitted = installed.cli(
        "submit", str(plan_path), "--config", str(installed.config),
        driver=_photo_driver())
    assert submitted.returncode == 0, submitted.stderr
    assert json.loads(submitted.stdout)["kind"] == "succeeded"

    # run：设备替身完成拍摄，会话收场并发布报告。
    finished = installed.cli(
        "run", "--config", str(installed.config), driver=_photo_driver())
    assert finished.returncode == 0, (
        f"run stdout: {finished.stdout}\nrun stderr: {finished.stderr}")
    assert json.loads(finished.stdout) == {"kind": "succeeded"}

    reports = list(installed.ready.glob("status-report-*.json"))
    assert reports, "run 会话未发布任何状态报告"
    uri = f"file:{installed.state_db.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        statuses = [row[0] for row in connection.execute(
            "SELECT status FROM actions ORDER BY id")]
    assert statuses == [3], f"照片动作未成功终态: {statuses}"

    # 版本入口：安装物自带版本说明。
    versioned = installed.cli("--version")
    assert versioned.returncode == 0, versioned.stderr
    assert versioned.stdout.strip() == "0.1.0", versioned.stdout

    # 运行日志：会话失败记录经日志链写入配置的日志文件；干净会话
    # 无记录不创建文件是既定行为，日志验证走失败路径。
    failed_home = installed.deploy / "deployment-failure"
    for name in ("staging", "ready", "processing"):
        (failed_home / name).mkdir(parents=True)
    failed_state = failed_home / "state.db"
    failed_config = failed_home / "config.toml"
    failed_config.write_text(
        "\n".join([
            "[paths]",
            f'state_db = "{_toml_path(failed_state)}"',
            f'log_file = "{_toml_path(failed_home / "camctl.log")}"',
            f'staging = "{_toml_path(failed_home / "staging")}"',
            f'ready = "{_toml_path(failed_home / "ready")}"',
            f'processing = "{_toml_path(failed_home / "processing")}"',
            "",
            "[clock]",
            'min_plausible_date = "2025-01-01"',
            "",
            "[devices.cam-1]",
            'kind = "camera"',
            'driver = "test-stub"',
            "",
        ]),
        encoding="utf-8",
    )
    # 日常入口对缺失状态库的拒绝与安装物边界：不创建库、明确失败。
    refused = installed.cli(
        "run", "--config", str(failed_config), driver=_photo_driver())
    assert refused.returncode == 1, (
        f"缺失状态库仍成功: stdout={refused.stdout!r}")
    assert refused.stdout.endswith("\n") and len(refused.stdout.splitlines()) == 1
    refusal_message = json.loads(refused.stdout)
    assert refusal_message["kind"] == "error", refused.stdout
    assert refusal_message["body"]["reason"] == "state_db_error", refused.stdout
    details = refusal_message["body"]["details"]
    assert isinstance(details, dict), refused.stdout
    assert any(isinstance(value, str) and str(failed_state) in value
               for value in details.values()), refused.stdout
    assert not failed_state.exists(), "日常入口创建了状态库"

    # 运行日志：显式初始化后损坏状态库，会话层失败经日志链写入
    # 配置的日志文件；干净会话无记录不创建文件是既定行为。
    initialized_failure = installed.cli(
        "init", "--config", str(failed_config))
    assert initialized_failure.returncode == 0, initialized_failure.stderr
    failed_state.write_bytes(b"not a sqlite database at all")
    failed = installed.cli(
        "run", "--config", str(failed_config), driver=_photo_driver())
    log_file = failed_home / "camctl.log"
    assert failed.returncode == 1, (
        f"损坏状态库仍成功: stdout={failed.stdout!r}")
    failure_message = json.loads(failed.stdout)
    assert failure_message["kind"] == "error", failed.stdout
    assert failure_message["body"]["reason"] == "state_db_error", failed.stdout
    assert log_file.is_file() and log_file.stat().st_size > 0, (
        f"会话失败未写运行日志: stdout={failed.stdout!r} "
        f"stderr={failed.stderr!r}")


def test_missing_package_resource_fails_init(installed: _Installed) -> None:
    """包资源缺失时 init 明确失败，不静默创建不完整状态库。"""
    origin = _probe(installed, "import camctl, sys; sys.stdout.write(camctl.__file__)")
    resource = Path(origin).parent / "_resources" / "runtime" / "sqlite-runtime.json"
    assert resource.is_file(), f"安装包缺少运行条件资源: {resource}"
    saved = resource.read_bytes()
    resource.unlink()
    try:
        failed = installed.cli("init", "--config", str(installed.config))
    finally:
        resource.write_bytes(saved)
    assert failed.returncode != 0, (
        f"资源缺失仍成功: stdout={failed.stdout!r}")
    assert "sqlite-runtime.json" in failed.stderr, failed.stderr


def test_noncompliant_sqlite_fails_init(installed: _Installed) -> None:
    """实际链接的 SQLite 不满足统一运行条件时 init 明确拒绝。

    版本条件判定本身有单元测试；此处以解释器内属性替换模拟不达标
    的 SQLite，验证 CLI 入口把运行库条件不满足报告为明确失败。
    """
    wrapper = installed.deploy / "bad_sqlite_entry.py"
    wrapper.write_text(
        "\n".join([
            "import sqlite3",
            "import sys",
            f"sqlite3.sqlite_version_info = {_NONCOMPLIANT_SQLITE!r}",
            f"sqlite3.sqlite_version = {'.'.join(map(str, _NONCOMPLIANT_SQLITE))!r}",
            "from camctl.cli import main",
            "raise SystemExit(main(sys.argv[1:]))",
        ]),
        encoding="utf-8",
    )
    environment = _clean_environment()
    environment["CAMCTL_TEST_STATE_DB"] = str(installed.state_db)
    failed = subprocess.run(
        [str(installed.python), str(wrapper), "init", "--config", str(installed.config)],
        capture_output=True, text=True, env=environment,
        cwd=str(installed.deploy), timeout=60)
    assert failed.returncode != 0, (
        f"运行条件不满足仍成功: stdout={failed.stdout!r}")
    assert "运行库条件不满足" in failed.stderr, failed.stderr
    assert "统一运行条件" in failed.stderr, failed.stderr
    assert failed.stdout == ""


@pytest.mark.skipif(os.name != 'posix', reason='部署侧通知描述符继承')
def test_installed_motor_plan_pipe_and_report_use_only_packaged_resources(installed):
    """在仓库外消费共享 Schema、SQL、登记以及真实 submit/run 报告。"""
    config = installed.deploy / 'motor.toml'
    config.write_text(installed.config.read_text().split('[devices.cam-1]')[0]
                      .replace('state.db', 'motor.db'))
    plan = installed.deploy / 'motor-plan.json'
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    plan.write_text(json.dumps({'request_id':'8801','created_at':now,'name':'独立电机发行',
        'actions':[{'name':'位置','type':'motor_control','scheduled_at':now,
                    'params':{'position':2147483647},'policy':{'max_delay_ms':60000}}]}))
    for command,args in [('init',[]),('submit',[str(plan)])]:
        result=installed.cli(command,*args,'--config',str(config))
        assert result.returncode==0,result.stderr
    read_fd,write_fd=os.pipe()
    try:
        result=subprocess.run([str(installed.python),'-m','camctl','run','--config',str(config),
                               '--host-notification-fd',str(write_fd)],pass_fds=(write_fd,),
                              capture_output=True,text=True,cwd=installed.deploy,
                              env=_clean_environment(),timeout=60)
    finally:
        os.close(write_fd)
    try:
        payload=os.read(read_fd,4096)
        assert os.read(read_fd,1)==b''
    finally:
        os.close(read_fd)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'kind':'succeeded'}
    assert json.loads(payload)=={'type':'motor_control','action_instance_id':'1',
                                 'params':{'position':2147483647}}
    # 安装侧校验实际报告；跨文件 $ref 必须完全由 wheel 资源解析。
    code=f'''
from pathlib import Path
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import validate_document
count=0
for path in Path({str(installed.ready)!r}).glob('status-report-*.json'):
    document=parse_exact_json(path.read_text())
    validate_document('protocol/status-report.schema.json', document)
    count+=sum(a['type']=='motor_control' and a['status']=='succeeded'
               for p in document.get('plans',[]) for a in p['actions'])
assert count > 0
'''
    _probe(installed,code)
