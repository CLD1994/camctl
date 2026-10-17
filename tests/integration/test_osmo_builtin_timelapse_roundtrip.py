"""OSMO 正式合同的源码与安装版联合链。

从真实安装 wheel 导出的两类 OSMO 拍摄能力必须与当前源码一致，
并经客户端、C host、CLI、独立取回和累计 ACK 完成软件交接。
设备命令及业务时间只在各自的进程/时钟端口替换。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from _wsl_host_demo import HostDemo, query_state, wait_for
from camctl.contracts.enums import enum_for
from camctl_fixtures import Deployment
from test_camctl_c_module_roundtrip import _report_with_statuses


_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent.parent
_PROJECT = _REPO / "apps/camctl"
_CAMERA_DEMOS = _REPO / "docs/camctl/examples/camera-demo"
_PYTHON311 = Path(os.environ.get(
    "UV_PYTHON", "/workspaces/camctl/apps/camctl/.venv311/bin/python"))
_OSMO_CONFIG_RESOURCE = "examples/camera-demo/osmo360ii-config.toml"
_OSMO_RECORD_RESOURCE = "examples/camera-demo/osmo360ii-record.json"
_OSMO_TIMELAPSE_RESOURCE = "examples/camera-demo/osmo360ii-timelapse.json"
_OSV_PATH = "/mnt/media_rw/emulated/DCIM/OSMO_0001/VID_0001.OSV"
_LRF_PATH = "/mnt/media_rw/emulated/DCIM/OSMO_0001/VID_0001.LRF"
_SERIAL = "osmo-installed-test-serial"
_OSV_CONTENT = (b"synthetic-osmo-osv-v1\x00" + bytes(range(256))) * 600
_LRF_CONTENT = (b"synthetic-osmo-lrf-v1\x00" + bytes(reversed(range(256)))) * 300


def _scratch() -> Path:
    path = _REPO / "tmp" / f"osmo-installed-chain-{os.getpid()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(command, *, cwd: Path, env: dict[str, str], log_dir: Path,
         timeout_s: int = 180):
    """记录每个子进程的实际工作目录、Python、argv、输出及退出码。"""
    completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                               text=True, timeout=timeout_s)
    records = log_dir / "commands"
    records.mkdir(parents=True, exist_ok=True)
    sequence = len(tuple(records.glob("*.json"))) + 1
    interpreter = command[0] if command and "python" in Path(command[0]).name else sys.executable
    (records / f"{sequence:03d}.json").write_text(json.dumps({
        "cwd": str(cwd),
        "python": interpreter,
        "argv": [str(part) for part in command],
        "exitcode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert completed.returncode == 0, (
        f"子进程退出码为 {completed.returncode}: {command!r}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    return completed


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if (name in ("PYTHONPATH", "PYTHONHOME")
                or name.startswith("CAMCTL_TEST_")
                or name.startswith("CAMCTL_OSMO_TEST_")):
            environment.pop(name)
    environment["PYTHONNOUSERSITE"] = "1"
    # uv must not fall back to the container's parent venv; the install chain
    # is intentionally built with the repository's Python 3.11 runtime.
    environment["UV_PYTHON"] = str(_PYTHON311)
    return environment


@dataclass(frozen=True)
class _InstalledPackage:
    root: Path
    python: Path
    camctl: Path
    environment: dict[str, str]
    log_dir: Path


@pytest.fixture(scope="module")
def osmo_package(tmp_path_factory) -> _InstalledPackage:
    """从源码目录外构建并安装 wheel 与锁定运行依赖。"""
    root = tmp_path_factory.mktemp("osmo-builtin-package")
    log_dir = _scratch()
    environment = _clean_environment()
    assert _PYTHON311.is_file(), f"Python 3.11 interpreter is unavailable: {_PYTHON311}"
    dist = root / "dist"
    _run(["uv", "build", "--project", str(_PROJECT), "--wheel",
          "--out-dir", str(dist)], cwd=root, env=environment, log_dir=log_dir)
    wheels = list(dist.glob("camctl-*.whl"))
    assert len(wheels) == 1, wheels
    requirements = root / "requirements.txt"
    _run(["uv", "export", "--project", str(_PROJECT), "--frozen", "--no-dev",
          "--no-emit-project", "-o", str(requirements)],
         cwd=root, env=environment, log_dir=log_dir)
    venv = root / "venv"
    _run(["uv", "venv", "--python", str(_PYTHON311), str(venv)],
         cwd=root, env=environment, log_dir=log_dir)
    python = venv / "bin/python"
    _run(["uv", "pip", "install", "--python", str(python), "--require-hashes",
          "-r", str(requirements)], cwd=root, env=environment, log_dir=log_dir)
    _run(["uv", "pip", "install", "--python", str(python), "--no-deps",
          str(wheels[0])], cwd=root, env=environment, log_dir=log_dir)
    return _InstalledPackage(root, python, venv / "bin/camctl", environment, log_dir)


def _osmo_config(path: Path, *, serial: str = "osmo-installed-test-serial") -> Path:
    path.write_text(
        "\n".join((
            "[paths]",
            f'state_db = "{path.parent / "state.db"}"',
            f'log_file = "{path.parent / "camctl.log"}"',
            f'staging = "{path.parent / "staging"}"',
            f'ready = "{path.parent / "ready"}"',
            f'processing = "{path.parent / "processing"}"',
            "",
            "[clock]",
            'min_plausible_date = "2025-01-01"',
            "",
            "[copy]",
            'segment_size = "64 KiB"',
            "",
            "[devices.osmo360ii]",
            'kind = "camera"',
            'driver = "dji-osmo360-ii"',
            "[devices.osmo360ii.adb]",
            f'serial = "{serial}"',
            "[devices.osmo360ii.copy]",
            'read_idle_timeout_s = "60"',
            "",
        )), encoding="utf-8")
    return path


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    contents = path.read_text(encoding="utf-8")
    lines = contents.splitlines()
    if contents and not contents.endswith("\n"):
        lines.pop()
    return [json.loads(line) for line in lines]


def _set_clock_offset(path: Path, offset_s: int) -> None:
    temporary = path.with_suffix(".new")
    temporary.write_text(str(offset_s * 1_000_000_000), encoding="ascii")
    temporary.replace(path)


def _scheduled_at(offset_s: int, *, lead_s: int = 2) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=offset_s + lead_s)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _capture_params(action_type: str) -> dict:
    if action_type == "camera_record":
        return {
            "type": "osmo360ii_record",
            "duration_s": 300,
            "exposure": {"mode": "auto", "compensation_ev": 0},
        }
    assert action_type == "camera_timelapse"
    return {
        "type": "osmo360ii_timelapse",
        "interval_s": 1,
        "duration_s": 300,
        "outputs": "video",
        "exposure": {"mode": "auto"},
    }


def _capture_body(action_type: str, params: dict, *, offset_s: int = 0) -> dict:
    return {
        "name": f"osmo-{action_type}",
        "actions": [{
            "name": "capture",
            "type": action_type,
            "device_id": "osmo360ii",
            "scheduled_at": _scheduled_at(offset_s),
            "params": params,
            "policy": {"max_delay_ms": 30000},
        }],
    }


def _install_osmo_process_tools(
        package: _InstalledPackage, deployment: Deployment, *, action_type: str,
        params: dict, scratch: Path) -> tuple[dict[str, str], Path, Path, Path]:
    tools = scratch / "tools"
    tools.mkdir()
    adb_helper = tools / "osmo-adb.py"
    shutil.copyfile(_ROOT / "_osmo_builtin_adb.py", adb_helper)
    adb = tools / "adb"
    adb.write_text(
        "#!/bin/sh\nexec " + shlex.quote(str(package.python)) + " "
        + shlex.quote(str(adb_helper)) + ' "$@"\n', encoding="utf-8")
    adb.chmod(0o755)

    clock_entry = _ROOT / "_osmo_builtin_clock_entry.py"
    camctl = tools / "camctl"
    camctl.write_text(
        "#!/bin/sh\nexec " + shlex.quote(str(package.python)) + " "
        + shlex.quote(str(clock_entry)) + ' "$@"\n', encoding="utf-8")
    camctl.chmod(0o755)

    clock_file = scratch / "clock-offset-ns.txt"
    _set_clock_offset(clock_file, 0)
    adb_root = scratch / "camera-files"
    adb_root.mkdir()
    (adb_root / "mnt/media_rw/emulated/DCIM").mkdir(parents=True)
    sample = scratch / "sample.OSV"
    sample.write_bytes(_OSV_CONTENT)
    sample.with_suffix(".lrf").write_bytes(_LRF_CONTENT)
    adb_log = scratch / "adb-calls.jsonl"
    process_log = scratch / "installed-cli-processes.jsonl"
    read_gate = scratch / "obtain-read.gate"
    read_gate.touch()
    environment = dict(package.environment)
    environment["PATH"] = str(tools) + os.pathsep + environment.get("PATH", "")
    environment.update({
        "CAMCTL_OSMO_TEST_ACTION": action_type,
        "CAMCTL_OSMO_TEST_PARAMS": json.dumps(params, separators=(",", ":")),
        "CAMCTL_OSMO_TEST_SERIAL": _SERIAL,
        "CAMCTL_OSMO_TEST_ADB_ROOT": str(adb_root),
        "CAMCTL_OSMO_TEST_ADB_LOG": str(adb_log),
        "CAMCTL_OSMO_TEST_SAMPLE": str(sample),
        "CAMCTL_OSMO_TEST_CLOCK_FILE": str(clock_file),
        "CAMCTL_OSMO_TEST_PROCESS_LOG": str(process_log),
        "CAMCTL_OSMO_TEST_READ_GATE": str(read_gate),
        "CAMCTL_OSMO_TEST_READ_GATE_AFTER_BYTES": "65536",
    })
    return environment, camctl, clock_file, adb_root


def _client_driver(package: _InstalledPackage, *args: str, output: Path) -> dict:
    command = ["node", "--import", "tsx", str(_ROOT / "client_driver.ts"),
               *args, str(output)]
    _run(command, cwd=_REPO / "apps/client", env=package.environment,
         log_dir=package.log_dir, timeout_s=120)
    return json.loads(output.read_text(encoding="utf-8"))


def _capture_facts(state_db: Path, action_name: str = "capture") -> list[tuple]:
    return query_state(state_db,
        "SELECT a.id,a.device_id,a.driver_id,a.effective_params_json,a.execution_spec_json,"
        "v.started_at,v.sent_at,v.baseline_first_event_id,v.baseline_last_event_id,"
        "v.task_key,v.baseline_state"
        " FROM actions a JOIN device_activities v ON v.action_id=a.id WHERE a.name=?",
        (action_name,))


def _action(report: dict, name: str) -> dict:
    actions = [action for plan in report["plans"] for action in plan["actions"]
               if action["name"] == name]
    assert len(actions) == 1, actions
    return actions[0]


def _describe(command, *, cwd: Path, env: dict[str, str], log_dir: Path) -> dict:
    completed = _run(command, cwd=cwd, env=env, log_dir=log_dir)
    return json.loads(completed.stdout)


def test_installed_osmo_describe_exposes_both_formal_capture_types(osmo_package, tmp_path):
    """源码与独立 wheel 均通过正式内置登记导出两类 OSMO 拍摄能力。"""
    package = osmo_package
    config = _osmo_config(tmp_path / "osmo-config.toml")

    installed_origin = _run([str(package.python), "-c",
        "import camctl; print(camctl.__file__)",], cwd=package.root,
        env=package.environment, log_dir=package.log_dir).stdout.strip()
    origin = Path(installed_origin).resolve()
    assert package.root in origin.parents
    assert _REPO not in origin.parents

    installed = _describe([str(package.camctl), "describe", "--config", str(config)],
                          cwd=tmp_path, env=package.environment, log_dir=package.log_dir)
    source_environment = dict(package.environment)
    source_environment["PYTHONPATH"] = str(_PROJECT / "src")
    source = _describe([sys.executable, "-m", "camctl", "describe", "--config", str(config)],
                       cwd=tmp_path, env=source_environment, log_dir=package.log_dir)
    assert installed == source

    package_resources = _run([str(package.python), "-c", (
        "import json; from camctl.resources import available_resources, resource_bytes; "
        f"names={[_OSMO_CONFIG_RESOURCE, _OSMO_RECORD_RESOURCE, _OSMO_TIMELAPSE_RESOURCE]!r}; "
        "print(json.dumps({name: resource_bytes(name).decode('utf-8') for name in names}))")],
        cwd=package.root, env=package.environment, log_dir=package.log_dir)
    resources = json.loads(package_resources.stdout)
    assert resources[_OSMO_CONFIG_RESOURCE].encode() == (_CAMERA_DEMOS / "osmo360ii-config.toml").read_bytes()
    assert resources[_OSMO_RECORD_RESOURCE].encode() == (_CAMERA_DEMOS / "osmo360ii-record.json").read_bytes()
    assert resources[_OSMO_TIMELAPSE_RESOURCE].encode() == (_CAMERA_DEMOS / "osmo360ii-timelapse.json").read_bytes()

    devices = [device for device in installed["devices"]
               if device["device_id"] == "osmo360ii"]
    assert len(devices) == 1
    assert devices[0]["driver_id"] == "dji-osmo360-ii"
    assert [action["type"] for action in devices[0]["actions"]] == [
        "camera_record", "camera_timelapse"]
    assert [parameter["type"] for action in devices[0]["actions"]
            for parameter in action["parameter_types"]] == [
        "osmo360ii_record", "osmo360ii_timelapse"]


@pytest.mark.skipif(os.name != "posix", reason="安装版 C host 链在 Linux 验证")
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
def test_installed_osmo_capture_recovery_original_and_preview_delivery(
        tmp_path, host_demo, osmo_package, action_type):
    """正式 OSMO 合同经客户端、C host、安装 CLI、恢复、取回与 ACK。

    受控时间只移动安装进程读到的共享时钟偏移。driver 的
    asyncio.sleep(30) 保持原样；普通录像用真实单调时间验证 STOP 后等待。
    """
    package = osmo_package
    serial = _SERIAL
    deployment = Deployment(tmp_path / "world", devices=False)
    _osmo_config(deployment.config_path, serial=serial)
    params = _capture_params(action_type)
    scratch = _scratch() / action_type
    scratch.mkdir(parents=True, exist_ok=True)
    environment, camctl, clock_file, adb_root = _install_osmo_process_tools(
        package, deployment, action_type=action_type, params=params, scratch=scratch)
    adb_log = Path(environment["CAMCTL_OSMO_TEST_ADB_LOG"])
    process_log = Path(environment["CAMCTL_OSMO_TEST_PROCESS_LOG"])

    initialized = _run([str(package.camctl), "init", "--config", str(deployment.config_path)],
                       cwd=deployment.root, env=package.environment, log_dir=package.log_dir)
    assert initialized.stdout == ""
    described = _describe(
        [str(package.camctl), "describe", "--config", str(deployment.config_path)],
        cwd=deployment.root, env=package.environment, log_dir=package.log_dir)
    device = next(item for item in described["devices"] if item["device_id"] == "osmo360ii")
    assert [item["type"] for item in device["actions"]] == ["camera_record", "camera_timelapse"]

    client_store = deployment.root / "client-store"
    client_store.mkdir()
    (client_store / "device-capabilities.json").write_text(
        json.dumps(described, ensure_ascii=False), encoding="utf-8")

    def client_driver(*args: str, output: Path) -> dict:
        return _client_driver(package, *args, output=output)

    deployment.run_client_driver = client_driver
    capture_plan, capture_receipt = deployment.export_plan_with_client(
        _capture_body(action_type, params))
    assert capture_receipt["last_report_id"] is None
    exported_params = json.loads(capture_plan.read_text(encoding="utf-8"))["actions"][0]["params"]
    assert exported_params == params

    demo = HostDemo(deployment, host_demo, launcher=str(camctl))
    try:
        demo.start(env=environment)
        demo.submit(capture_plan)
        started = wait_for(
            lambda: next(iter(_capture_facts(deployment.state_db)), None),
            90, "安装版 OSMO 未保存原动作与活动事实")
        action_id = int(started[0])
        assert (started[1], started[2]) == ("osmo360ii", "dji-osmo360-ii")
        assert json.loads(started[3]) == params
        # 没有历史媒体时目录基准的首尾游标可以为空；恢复后须与本次
        # START 保存的原值一致，而不是要求先前必须存在历史游标。
        start_attempt_kind = int(enum_for("operation_runs.kind").START)
        def saved_start_attempt():
            rows = query_state(deployment.state_db,
                "SELECT t.id,t.status,t.result_event_id FROM operation_attempts t"
                " JOIN operation_runs r ON r.id=t.run_id"
                " WHERE r.action_id=? AND r.kind=? ORDER BY t.id",
                (action_id, start_attempt_kind))
            return rows if len(rows) == 1 and rows[0][2] is not None else None

        attempts = wait_for(saved_start_attempt, 90,
                            "正式 OSMO START 调用结果未可靠保存")
        assert len(attempts) == 1 and attempts[0][2] is not None
        assert any(call.get("kind") == "start" for call in _records(adb_log))
        started = _capture_facts(deployment.state_db)[0]
        if action_type == "camera_timelapse":
            assert started[6] is not None
        original_execution = json.loads(started[4])
        assert original_execution["target_duration_ms"] == 300000
        assert original_execution["file_completion_wait_ms"] == 30000
        if action_type == "camera_timelapse":
            from camctl.devices.tasks import CompletionMode, EndControl, StartReturn

            assert original_execution["end_control"] == int(EndControl.DEVICE)
            assert original_execution["start_return_meaning"] == int(StartReturn.SENT)
            assert original_execution["completion_mode"] == int(CompletionMode.TIME_AND_OUTPUTS)
            assert original_execution["result_wait_margin_ms"] == 30000

        calls_at_start = _records(adb_log)
        start_calls = [call for call in calls_at_start if call.get("kind") == "start"]
        assert len(start_calls) == 1
        assert [call.get("kind") for call in calls_at_start
                if call.get("started_ns", 0) > start_calls[0]["started_ns"]] == []
        # START 已可靠保存，但自然目标尚未到期；确认等待期没有继续发 ADB。
        before_wait = len(calls_at_start)
        time.sleep(0.25)
        assert len(_records(adb_log)) == before_wait
        assert query_state(deployment.state_db, "SELECT id FROM outputs") == []

        # 中断原 C host run，保留刚才确认的 START、原参数、原绑定和基准。
        demo.stop()
        natural_s = 300 if action_type == "camera_record" else 330
        _set_clock_offset(clock_file, natural_s + 1)
        demo = HostDemo(deployment, host_demo, launcher=str(camctl))
        demo.start(env=environment)
        demo.submit(capture_plan)

        capture_report = wait_for(
            lambda: _report_with_statuses(deployment.ready, {"capture": "succeeded"}),
            150, "安装版 OSMO capture 未完成或未发布成功报告")
        final_start = _capture_facts(deployment.state_db)
        assert len(final_start) == 1
        assert final_start[0][:5] == started[:5]
        assert final_start[0][7:] == started[7:]
        if action_type == "camera_timelapse":
            assert final_start[0][6] == started[6]
        all_calls = _records(adb_log)
        start_calls = [call for call in all_calls if call.get("kind") == "start"]
        assert len(start_calls) == 1, all_calls
        response = next(call for call in all_calls if call.get("kind") == "start_response")
        stops = [call for call in all_calls if call.get("kind") == "stop"]

        settings = [call for call in all_calls if call.get("kind") == "setting"]
        assert settings
        ee = [call for call in settings if call.get("response") == "ee"]
        if action_type == "camera_timelapse":
            assert len(ee) == 1 and ee[0]["command"][-3:] == ["-c", "1e", "0000"]
        else:
            assert ee == []
        assert all(call.get("exitcode") == 0 for call in settings)

        result_rows = query_state(deployment.state_db,
            "SELECT t.result_json FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.action_id=? AND r.kind=? ORDER BY t.id",
            (action_id, int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)))
        assert result_rows

        if action_type == "camera_record":
            # 主机计时录像在 300 秒目标后发唯一 STOP 00，再由 driver 独立等待 30 秒。
            assert len(stops) == 1, all_calls
            assert stops[0]["started_clock_ns"] - response["returned_clock_ns"] >= 300 * 1_000_000_000
            assert stops[0]["response"] == "00"
            assert stops[0].get("exitcode") == 0
            directories = [call for call in all_calls if call.get("kind") == "directory"
                           and call.get("started_ns", 0) >= stops[0]["returned_ns"]]
            assert directories, all_calls
            assert directories[0]["started_ns"] - stops[0]["returned_ns"] >= 30_000_000_000
            completion = None
            for (encoded,) in result_rows:
                document = json.loads(encoded)
                candidate = document["settlement"]["evidence"]["data"].get("file_completion")
                if candidate is not None:
                    completion = candidate
                    break
            assert completion is not None
            assert completion["method"] == "stop_return_and_wait"
            assert completion["required_wait_ms"] == 30000
            assert completion["observed_wait_ns"] >= 30_000_000_000
            assert completion["completed"] is True
        else:
            # DEVICE/SENT/TIME_AND_OUTPUTS 自然完成只等待 300 秒目标和 30 秒边界，
            # 随后读取原活动结果；正常结束没有 STOP，也没有 stop_return file_completion。
            assert stops == [], all_calls
            directories = [call for call in all_calls if call.get("kind") == "directory"
                           and call.get("started_clock_ns", 0) >= response["returned_clock_ns"]]
            assert directories, all_calls
            assert directories[0]["started_clock_ns"] - response["returned_clock_ns"] >= 330 * 1_000_000_000

        device_files = query_state(deployment.state_db,
            "SELECT id,locator_json,role,original_device_file_id,pairing_evidence_json,"
            "completion_state,size_bytes,completion_evidence_json FROM device_files"
            " WHERE source_action_id=? ORDER BY role",
            (action_id,))
        roles = enum_for("device_files.role")
        completed = enum_for("device_files.completion_state").COMPLETE
        assert len(device_files) == 2
        by_path = {json.loads(row[1])["path"]: row for row in device_files}
        assert set(by_path) == {_OSV_PATH, _LRF_PATH}
        original = by_path[_OSV_PATH]
        preview = by_path[_LRF_PATH]
        assert original[2] == int(roles.ORIGINAL)
        assert original[3] is None and original[4] is None
        assert original[5] == int(completed) and original[6] == len(_OSV_CONTENT) >= 1
        assert preview[2] == int(roles.PREVIEW)
        assert preview[3] == original[0]
        assert json.loads(preview[4]) == {
            "method": 1, "observation": {"paired_identity": _OSV_PATH}}
        assert preview[5] == int(completed) and preview[6] == len(_LRF_CONTENT) >= 1

        if action_type == "camera_record":
            stop_attempt_kind = int(enum_for("operation_runs.kind").STOP)
            stop_rows = query_state(deployment.state_db,
                "SELECT t.result_json FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
                " WHERE r.action_id=? AND r.kind=? ORDER BY t.id", (action_id, stop_attempt_kind))
            assert len(stop_rows) == 1
            stop_document = json.loads(stop_rows[0][0])
            assert [item["type"] for item in stop_document["observations"]] == ["stop_confirmed"]
            assert stop_document["observations"][0]["data"]["response_payload"] == "00"
        else:
            stop_attempt_kind = int(enum_for("operation_runs.kind").STOP)
            stop_rows = query_state(deployment.state_db,
                "SELECT t.id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
                " WHERE r.action_id=? AND r.kind=? ORDER BY t.id", (action_id, stop_attempt_kind))
            assert stop_rows == []
            activity_rows = query_state(deployment.state_db,
                "SELECT id,wait_completed_event_id,capture_json,completion_basis,"
                "completion_evidence_json,result_check_json FROM device_activities WHERE action_id=?",
                (action_id,))
            assert len(activity_rows) == 1
            activity_id, wait_event_id, capture_json, completion_basis, activity_evidence_json, result_check_json = activity_rows[0]
            assert json.loads(capture_json) == {"status": "completed"}
            assert completion_basis == int(enum_for("device_activities.completion_basis").TIME_AND_OUTPUTS)
            assert wait_event_id is not None
            activity_evidence = json.loads(activity_evidence_json)
            assert activity_evidence["method"] == "time_and_outputs"
            assert activity_evidence["wait_completed_event_id"] == wait_event_id
            result_check = json.loads(result_check_json)
            assert result_check["contract"] == "result_files_listed/v2"
            assert activity_evidence["observation"]["first_page_event_id"] == result_check["observation"]["first_page_event_id"]
            assert activity_evidence["observation"]["last_page_event_id"] == result_check["observation"]["last_page_event_id"]
            start_result_event_id = attempts[0][2]
            page_event_ids = {row[0] for row in query_state(deployment.state_db,
                "SELECT id FROM history_events"
                " WHERE json_extract(body_json,'$.evidence.result_page.activity_id')=?",
                (activity_id,))}
            assert page_event_ids
            for row in device_files:
                evidence = json.loads(row[7])
                assert evidence["activity_id"] == activity_id
                assert evidence["start_result_event_id"] == start_result_event_id
                assert evidence["wait_completed_event_id"] == wait_event_id
                assert evidence["result_page_event_id"] in page_event_ids
                assert start_result_event_id < wait_event_id < evidence["result_page_event_id"]

        output_kinds = enum_for("outputs.kind")
        outputs = query_state(deployment.state_db,
            "SELECT o.kind,f.original_name FROM outputs o JOIN device_files f ON f.id=o.device_file_id"
            " WHERE o.source_action_id=? ORDER BY o.kind", (action_id,))
        assert outputs == [(int(output_kinds.ORIGINAL), "VID_0001.OSV"),
                           (int(output_kinds.PREVIEW), "VID_0001.LRF")]

        if action_type == "camera_record":
            activity_id = query_state(deployment.state_db,
                "SELECT id FROM device_activities WHERE action_id=?", (action_id,))[0][0]
        page_rows = query_state(deployment.state_db,
            "SELECT body_json FROM history_events"
            " WHERE json_extract(body_json,'$.evidence.result_page.activity_id')=?",
                (activity_id,))
        listed = []
        for (body,) in page_rows:
            page = json.loads(body)["evidence"]["result_page"]
            result = page["outcome"]["result"]
            listed.extend(entry for observation in result["observations"]
                          if observation["type"] == "result_files_listed"
                          for entry in observation["data"]["entries"])
        assert {(entry["identity"], entry["kind"], entry["format_id"], entry["paired_identity"])
                for entry in listed} == {
                    (_OSV_PATH, "video", "osv", None),
                    (_LRF_PATH, "video", "lrf", _OSV_PATH),
                }

        demo.claim()
        saved = deployment.import_reports_with_client(deployment.processing)
        assert capture_report["report_id"] in saved["saved_report_ids"]
        assert len(_records(process_log)) >= 3

        delivery_ids = []
        obtain_reports = []
        interrupted_delivery_id = None
        interrupted_copy_id = None
        interrupted_action_id = None
        for obtain_name, output_filter, content in (
                ("obtain-original", "default", _OSV_CONTENT),
                ("obtain-preview", "preview", _LRF_CONTENT)):
            body = {
                "name": obtain_name,
                "actions": [{
                    "name": obtain_name,
                    "type": "obtain_action_outputs",
                    "scheduled_at": _scheduled_at(natural_s + 1),
                    "params": {
                        "source": {"action_instance_id": str(action_id)},
                        "filter": output_filter,
                        "purpose": "manual",
                    },
                }],
            }
            plan, receipt = deployment.export_plan_with_client(body)
            assert receipt["last_report_id"] == (
                capture_report["report_id"] if not obtain_reports else obtain_reports[-1]["report_id"])
            if obtain_name == "obtain-original":
                plan_request_id = int(json.loads(plan.read_text(encoding="utf-8"))["request_id"])
                read_gate = Path(environment["CAMCTL_OSMO_TEST_READ_GATE"])
                read_gate.unlink()
                demo.submit(plan)
                first_read = wait_for(lambda: next((call for call in _records(adb_log)
                    if call.get("kind") == "read_started"
                    and _OSV_PATH in call.get("script", "")), None),
                    120, "原片取回未开始源文件读取")
                accepted = wait_for(lambda: query_state(deployment.state_db,
                    "SELECT p.id,a.id,a.input_fields_json FROM plans p"
                    " JOIN actions a ON a.plan_id=p.id"
                    " WHERE p.request_id=? AND a.name=?",
                    (plan_request_id, obtain_name)),
                    90, "原片取回请求未可靠受理")
                assert len(accepted) == 1
                interrupted_action_id = accepted[0][1]
                expected_params = {
                    "source": {"action_instance_id": str(action_id)},
                    "filter": output_filter, "purpose": "manual",
                }
                assert json.loads(accepted[0][2])["params"] == expected_params
                before_delivery = wait_for(lambda: query_state(deployment.state_db,
                    "SELECT id,file_name FROM deliveries WHERE action_id=?",
                    (interrupted_action_id,)),
                    90, "原片取回未固定独立 delivery 身份")
                assert len(before_delivery) == 1
                interrupted_delivery_id = before_delivery[0][0]
                before_copy = wait_for(lambda: query_state(deployment.state_db,
                    "SELECT c.id,c.source_size,c.source_sha256,c.committed_bytes"
                    " FROM file_copies c WHERE c.delivery_id=?"
                    " AND c.committed_bytes>=65536",
                    (interrupted_delivery_id,)),
                    90, "原片取回未保存第一个完整 64 KiB 数据块")
                assert len(before_copy) == 1
                interrupted_copy_id = before_copy[0][0]
                assert before_copy[0][1] == len(content)
                interrupted_progress = before_copy[0][3]
                assert interrupted_progress >= 65536
                assert any(call.get("kind") == "read_blocked"
                    and call.get("started_ns") == first_read["started_ns"]
                    and call.get("forwarded_bytes", 0) >= 65536
                    for call in _records(adb_log))

                demo.stop()
                assert not any(call.get("kind") == "read"
                    and call.get("started_ns") == first_read["started_ns"]
                    for call in _records(adb_log))
                demo = HostDemo(deployment, host_demo, launcher=str(camctl))
                # 恢复读取在输出任何数据前暂停，验证已保存的段进度和续传偏移。
                environment["CAMCTL_OSMO_TEST_READ_GATE_AFTER_BYTES"] = "0"
                demo.start(env=environment)
                # 使用同一客户端计划文件重送同 request_id，同时保留 gate，
                # 使恢复后的真实取回仍停在同一数据块边界。
                demo.submit(plan)
                resumed_read = wait_for(lambda: next((call for call in _records(adb_log)
                    if call.get("kind") == "read_started"
                    and call.get("started_ns", 0) > first_read["started_ns"]
                    and _OSV_PATH in call.get("script", "")), None),
                    120, "同请求恢复未继续读取已保存的原片")
                assert "skip=1" in resumed_read["script"]
                resumed_gate = wait_for(lambda: next((call for call in _records(adb_log)
                    if call.get("kind") == "read_blocked"
                    and call.get("started_ns") == resumed_read["started_ns"]), None),
                    30, "恢复后的原片读取未在门控边界暂停")
                assert resumed_gate["forwarded_bytes"] == 0
                assert query_state(deployment.state_db,
                    "SELECT p.id,a.id FROM plans p JOIN actions a ON a.plan_id=p.id"
                    " WHERE p.request_id=? AND a.name=?",
                    (plan_request_id, obtain_name)) == [(accepted[0][0], interrupted_action_id)]
                assert query_state(deployment.state_db,
                    "SELECT id FROM deliveries WHERE action_id=?", (interrupted_action_id,)) == [
                        (interrupted_delivery_id,)]
                assert query_state(deployment.state_db,
                    "SELECT id,committed_bytes FROM file_copies WHERE delivery_id=?",
                    (interrupted_delivery_id,)) == [(interrupted_copy_id, interrupted_progress)]
                read_gate.touch()
            else:
                demo.submit(plan)
            report = wait_for(
                lambda name=obtain_name: _report_with_statuses(deployment.ready, {name: "succeeded"}),
                120, f"独立 {obtain_name} 未成功")
            obtain_action = _action(report, obtain_name)
            obtain_action_id = int(obtain_action["action_instance_id"])
            assert len(obtain_action["deliveries"]) == 1
            delivery = obtain_action["deliveries"][0]
            assert delivery["source_action_instance_id"] == str(action_id)
            assert delivery["size"] == len(content)
            expected_sha = hashlib.sha256(content).hexdigest()
            assert delivery["sha256"] == expected_sha
            delivery_row = query_state(deployment.state_db,
                "SELECT id,file_name FROM deliveries WHERE action_id=?", (obtain_action_id,))
            assert len(delivery_row) == 1
            delivery_id, file_name = delivery_row[0]
            if obtain_name == "obtain-original":
                assert obtain_action_id == interrupted_action_id
                assert delivery_id == interrupted_delivery_id
            delivery_ids.append(delivery_id)
            demo.claim()
            claimed = deployment.processing / file_name
            assert claimed.read_bytes() == content
            assert hashlib.sha256(claimed.read_bytes()).hexdigest() == expected_sha
            copies = query_state(deployment.state_db,
                "SELECT c.id,c.source_size,c.source_sha256,c.committed_bytes,c.target_sha256"
                " FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id WHERE d.id=?",
                (delivery_id,))
            assert len(copies) == 1
            if obtain_name == "obtain-original":
                assert copies[0][0] == interrupted_copy_id
            assert copies[0][1:] == (len(content), expected_sha, len(content), expected_sha)
            assert (adb_root / _OSV_PATH.lstrip("/")).read_bytes() == _OSV_CONTENT
            assert (adb_root / _LRF_PATH.lstrip("/")).read_bytes() == _LRF_CONTENT
            saved = deployment.import_reports_with_client(deployment.processing)
            assert report["report_id"] in saved["saved_report_ids"]
            media_receipt_path = deployment.root / f"client-media-{obtain_name}.json"
            media_receipt = _client_driver(
                package, "import-media", str(client_store), str(claimed),
                output=media_receipt_path)
            assert media_receipt["file_name"] == file_name
            assert media_receipt["delivery_id"] == str(delivery_id)
            assert media_receipt["source_action_instance_id"] == str(action_id)
            assert media_receipt["output_kind"] == (
                "original" if obtain_name == "obtain-original" else "preview")
            assert media_receipt["import_kind"] == (
                "video" if obtain_name == "obtain-original" else "media")
            assert media_receipt["video"]["size"] == len(content)
            assert media_receipt["video"]["sha256"] == expected_sha
            assert media_receipt["video"]["status"] == "verified"
            assert media_receipt["video"]["verification"] == {
                "status": "verified",
                "against": {"size": len(content), "sha256": expected_sha},
            }
            assert media_receipt["video"]["verifiedAgainst"] == {
                "size": len(content), "sha256": expected_sha,
            }
            obtain_reports.append(report)

        assert len(set(delivery_ids)) == 2
        assert delivery_ids[0] != delivery_ids[1]
        capture_after_obtains = _capture_facts(deployment.state_db)
        assert len(capture_after_obtains) == 1
        assert capture_after_obtains[0][:5] == started[:5]
        assert capture_after_obtains[0][7:] == started[7:]
        assert len([call for call in _records(adb_log) if call.get("kind") == "start"]) == 1
        acknowledgement, receipt = deployment.export_plan_with_client({
            "name": "osmo-cumulative-ack",
            "actions": [{
                "name": "ack",
                "type": "report_status",
                "scheduled_at": _scheduled_at(natural_s + 1),
                "params": {"scope": "full"},
            }],
        })
        last_report = obtain_reports[-1]
        assert receipt["last_report_id"] == last_report["report_id"]
        demo.submit(acknowledgement)
        wait_for(lambda: query_state(deployment.state_db,
            "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state")
            == [(int(last_report["report_id"]), last_report["to_wm"])],
            120, "客户端导出的累计 ACK 未吸收")

        assert (adb_root / _OSV_PATH.lstrip("/")).read_bytes() == _OSV_CONTENT
        assert (adb_root / _LRF_PATH.lstrip("/")).read_bytes() == _LRF_CONTENT
        process_records = _records(process_log)
        assert process_records
        assert all(record["cwd"] for record in process_records)
        assert all(Path(record["python"]).resolve() == package.python.resolve()
                   for record in process_records)
        assert all(isinstance(record["argv"], list) and "exitcode" in record
                   for record in process_records)
    finally:
        demo.stop()
