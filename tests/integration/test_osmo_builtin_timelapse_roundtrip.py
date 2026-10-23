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
import re
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from _wsl_host_demo import HostDemo, query_state, wait_for
from _osmo_builtin_clock import (
    format_utc, read_clock, set_frozen, set_running, whole_second_utc_us,
)
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
_OSV_PATH = "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261022100000_0007_D.OSV"
_LRF_PATH = "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261022100000_0007_D.LRF"
_SERIAL = "osmo-installed-test-serial"
_OSV_CONTENT = (b"synthetic-osmo-osv-v1\x00" + bytes(range(256))) * 600
_LRF_CONTENT = (b"synthetic-osmo-lrf-v1\x00" + bytes(reversed(range(256)))) * 300


def _archive_logs(package_root: Path, log_dir: Path) -> Path:
    archive = _REPO / "tmp" / f"osmo-installed-chain-archive-{os.getpid()}"
    archive.mkdir(parents=True, exist_ok=True)
    if log_dir.is_dir():
        shutil.copytree(log_dir, archive / "commands", dirs_exist_ok=True)
    runtime = package_root / "runtime"
    for scenario in runtime.iterdir() if runtime.exists() else ():
        if not scenario.is_dir():
            continue
        for name in ("adb-calls.jsonl", "installed-cli-processes.jsonl",
                     "controlled-clock-reads.jsonl", "segment-marker.json", "files.json"):
            source = scenario / name
            if source.is_file():
                target = archive / "runtime" / scenario.name / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    if (package_root / "dist").is_dir():
        shutil.copytree(package_root / "dist", archive / "dist", dirs_exist_ok=True)
    if (package_root / "requirements.txt").is_file():
        shutil.copy2(package_root / "requirements.txt", archive / "requirements.txt")
    return archive


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
        if (name in ("PYTHONPATH", "PYTHONHOME", "PWD")
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
    log_dir = root / "command-logs"
    build_environment = _clean_environment()
    assert _PYTHON311.is_file(), f"Python 3.11 interpreter is unavailable: {_PYTHON311}"
    dist = root / "dist"
    _run(["uv", "build", "--project", str(_PROJECT), "--wheel",
          "--out-dir", str(dist)], cwd=root, env=build_environment, log_dir=log_dir)
    wheels = list(dist.glob("camctl-*.whl"))
    assert len(wheels) == 1, wheels
    requirements = root / "requirements.txt"
    _run(["uv", "export", "--project", str(_PROJECT), "--frozen", "--no-dev",
          "--no-emit-project", "-o", str(requirements)],
         cwd=root, env=build_environment, log_dir=log_dir)
    venv = root / "venv"
    _run(["uv", "venv", "--python", str(_PYTHON311), str(venv)],
         cwd=root, env=build_environment, log_dir=log_dir)
    python = venv / "bin/python"
    _run(["uv", "pip", "install", "--python", str(python), "--require-hashes",
          "-r", str(requirements)], cwd=root, env=build_environment, log_dir=log_dir)
    _run(["uv", "pip", "install", "--python", str(python), "--no-deps",
          str(wheels[0])], cwd=root, env=build_environment, log_dir=log_dir)
    runtime_environment = dict(build_environment)
    runtime_environment.pop("UV_PYTHON", None)
    package = _InstalledPackage(root, python, venv / "bin/camctl",
                                runtime_environment, log_dir)
    try:
        yield package
    finally:
        _archive_logs(root, log_dir)


def _osmo_config(
        path: Path,
        *,
        serial: str = "osmo-installed-test-serial",
        result_mode: str | None = None,
) -> Path:
    lines = [
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
    ]
    if result_mode is not None:
        if result_mode not in {"single_file", "segmented"}:
            raise ValueError("OSMO 测试配置的视频结果模式无效")
        lines.extend((
            "",
            "[devices.osmo360ii.video]",
            f'result_mode = "{result_mode}"',
        ))
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    contents = path.read_text(encoding="utf-8")
    lines = contents.splitlines()
    if contents and not contents.endswith("\n"):
        lines.pop()
    return [json.loads(line) for line in lines]


def _clock_frames(record: dict, *, module: str, function: str) -> list[dict]:
    return [frame for frame in record.get("stack", ())
            if frame.get("module") == module and frame.get("function") == function]


def _matching_boundary_reads(
        clock_log: Path, *, pid: int, epoch: int, module: str,
        function: str, kind: str, utc_us: int, monotonic_ns: int) -> tuple[int, int] | None:
    by_line: dict[int, list[dict]] = {}
    for record in _records(clock_log):
        if (record.get("pid") != pid or record.get("epoch") != epoch
                or record.get("kind") != kind
                or record.get("utc_us") != utc_us
                or record.get("monotonic_ns") != monotonic_ns):
            continue
        for frame in _clock_frames(record, module=module, function=function):
            by_line.setdefault(frame["line"], []).append(record)
    for line, rows in by_line.items():
        rows.sort(key=lambda row: row["real_monotonic_ns"])
        if (len(rows) >= 2
                and rows[-1]["real_monotonic_ns"] - rows[0]["real_monotonic_ns"]
                >= 500_000_000):
            return line, rows[-1]["real_monotonic_ns"] - rows[0]["real_monotonic_ns"]
    return None


def _wait_for_boundary_reads(
        clock_log: Path, *, pid: int, epoch: int, module: str,
        function: str, kind: str, utc_us: int, monotonic_ns: int,
        timeout_s: float = 30) -> tuple[int, int]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        matched = _matching_boundary_reads(
            clock_log, pid=pid, epoch=epoch, module=module, function=function,
            kind=kind, utc_us=utc_us, monotonic_ns=monotonic_ns)
        if matched is not None:
            return matched
        time.sleep(0.05)
    raise AssertionError(
        f"安装版 run 未在决策调用点反复读取冻结边界: pid={pid} epoch={epoch} "
        f"{module}.{function} kind={kind} utc_us={utc_us} monotonic_ns={monotonic_ns}")


def _wait_for_running_read(
        clock_log: Path, *, pid: int, epoch: int, module: str,
        function: str, kind: str, utc_us: int, monotonic_ns: int,
        real_monotonic_anchor_ns: int, timeout_s: float = 30) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for record in _records(clock_log):
            if (record.get("pid") == pid and record.get("epoch") == epoch
                    and record.get("kind") == kind
                    and record.get("utc_us", -1) >= utc_us
                    and record.get("monotonic_ns", -1) >= monotonic_ns
                    and record.get("real_monotonic_ns", -1) >= real_monotonic_anchor_ns
                    and any(frame.get("module") == module
                            and frame.get("function") == function
                            for frame in record.get("stack", ()))):
                return record
        time.sleep(0.05)
    raise AssertionError(
        f"恢复 run 未在释放 epoch 的决策调用点继续读取时钟: pid={pid} epoch={epoch} "
        f"{module}.{function} kind={kind}")


def _wait_for_run_process(
        process_log: Path, module_log: Path, previous_pids: set[int], timeout_s: float = 30
        ) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        host_log = module_log.read_text(encoding="utf-8", errors="replace")
        host_pids = set(map(int, re.findall(
            r"command=run pid=(\d+) spawned execution=unknown", host_log)))
        for record in _records(process_log):
            if (record.get("stage") == "started" and record.get("pid") not in previous_pids
                    and record.get("command", [None])[0] == "run"
                    and record["pid"] in host_pids):
                return record
        time.sleep(0.05)
    raise AssertionError("C host 日志没有对应的安装版 run 子进程 PID")


def _assert_recovery_boundary_empty(
        deployment: Deployment, adb_log: Path, *, action_id: int,
        activity_id: int, recovery_started_ns: int) -> None:
    stop_kind = int(enum_for("operation_runs.kind").STOP)
    result_kind = int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)
    assert query_state(deployment.state_db,
        "SELECT t.id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.action_id=? AND r.kind IN (?,?) ORDER BY t.id",
        (action_id, stop_kind, result_kind)) == []
    assert query_state(deployment.state_db,
        "SELECT id FROM history_events"
        " WHERE json_extract(body_json,'$.evidence.result_page.activity_id')=?",
        (activity_id,)) == []
    assert query_state(deployment.state_db,
        "SELECT id FROM device_files WHERE source_action_id=?", (action_id,)) == []
    assert query_state(deployment.state_db,
        "SELECT id FROM outputs WHERE source_action_id=?", (action_id,)) == []
    assert not any(call.get("kind") in {
        "stop", "directory", "metadata", "digest", "read", "read_started",
    } and call.get("started_ns", 0) >= recovery_started_ns for call in _records(adb_log))
    activity = query_state(deployment.state_db,
        "SELECT sent_at,started_at,capture_json,completion_basis,"
        "completion_evidence_json,result_check_json"
        " FROM device_activities WHERE action_id=?", (action_id,))
    assert len(activity) == 1
    undetermined = int(enum_for("device_activities.completion_basis").UNDETERMINED)
    assert activity[0][0] is not None
    assert activity[0][1] is None and activity[0][2] is None
    assert activity[0][3] in (None, undetermined)
    assert activity[0][4:] == (None, None)


def _scheduled_at(offset_s: int, *, lead_s: int = 2) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=offset_s + lead_s)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _capture_params(action_type: str) -> dict:
    if action_type == "camera_record":
        return {
            "type": "osmo360ii_record",
            "duration_s": 10,
            "bitrate": "high",
            "exposure": {"mode": "auto"},
        }
    assert action_type == "camera_timelapse"
    return {
        "type": "osmo360ii_timelapse",
        "interval_s": 2,
        "duration_s": 300,
        "bitrate": "high",
        "exposure": {"mode": "auto"},
    }


def _capture_body(action_type: str, params: dict, *, scheduled_at: str | None = None) -> dict:
    return {
        "name": f"osmo-{action_type}",
        "actions": [{
            "name": "capture",
            "type": action_type,
            "device_id": "osmo360ii",
            "scheduled_at": scheduled_at or _scheduled_at(0),
            "params": params,
            "policy": {"max_delay_ms": 30000},
        }],
    }


def _install_osmo_process_tools(
        package: _InstalledPackage, deployment: Deployment, *, action_type: str,
        params: dict, scratch: Path, include_preview: bool = True
        ) -> tuple[dict[str, str], Path, Path, Path]:
    tools = scratch / "tools"
    tools.mkdir(parents=True)
    clock_helper = tools / "_osmo_builtin_clock.py"
    shutil.copyfile(_ROOT / "_osmo_builtin_clock.py", clock_helper)
    adb_helper = tools / "osmo-adb.py"
    shutil.copyfile(_ROOT / "_osmo_builtin_adb.py", adb_helper)
    adb = tools / "adb"
    adb.write_text(
        "#!/bin/sh\nexec " + shlex.quote(str(package.python)) + " "
        + shlex.quote(str(adb_helper)) + ' "$@"\n', encoding="utf-8")
    adb.chmod(0o755)

    clock_entry = tools / "osmo-clock-entry.py"
    shutil.copyfile(_ROOT / "_osmo_builtin_clock_entry.py", clock_entry)
    camctl = tools / "camctl"
    camctl.write_text(
        "#!/bin/sh\nexec " + shlex.quote(str(package.python)) + " "
        + shlex.quote(str(clock_entry)) + ' "$@"\n', encoding="utf-8")
    camctl.chmod(0o755)

    clock_file = scratch / "clock-state.json"
    clock_log = scratch / "controlled-clock-reads.jsonl"
    set_frozen(clock_file, utc_us=whole_second_utc_us(),
               monotonic_ns=time.monotonic_ns())
    adb_root = scratch / "camera-files"
    adb_root.mkdir()
    (adb_root / "mnt/media_rw/emulated/DCIM").mkdir(parents=True)
    sample = scratch / "sample.OSV"
    sample.write_bytes(_OSV_CONTENT)
    if include_preview:
        sample.with_suffix(".lrf").write_bytes(_LRF_CONTENT)
    manifest = scratch / "files.json"
    files = [{"path": _OSV_PATH, "source": str(sample)}]
    if include_preview:
        files.append({"path": _LRF_PATH, "source": str(sample.with_suffix(".lrf"))})
    manifest.write_text(json.dumps(files), encoding="utf-8")
    adb_log = scratch / "adb-calls.jsonl"
    process_log = scratch / "installed-cli-processes.jsonl"
    read_gate = scratch / "obtain-read.gate"
    read_gate.touch()
    environment = dict(package.environment)
    environment["PATH"] = str(tools) + os.pathsep + environment.get("PATH", "")
    environment.update(
        {
            "CAMCTL_OSMO_TEST_ACTION": action_type,
            "CAMCTL_OSMO_TEST_PARAMS": json.dumps(params, separators=(",", ":")),
            "CAMCTL_OSMO_TEST_INCLUDE_PREVIEW": "1" if include_preview else "0",
            "CAMCTL_OSMO_TEST_SERIAL": _SERIAL,
            "CAMCTL_OSMO_TEST_ADB_ROOT": str(adb_root),
            "CAMCTL_OSMO_TEST_ADB_LOG": str(adb_log),
            "CAMCTL_OSMO_TEST_SAMPLE": str(sample),
            "CAMCTL_OSMO_TEST_FILES": str(manifest),
            "CAMCTL_OSMO_TEST_CLOCK_FILE": str(clock_file),
            "CAMCTL_OSMO_TEST_CLOCK_LOG": str(clock_log),
            "CAMCTL_OSMO_TEST_PROCESS_LOG": str(process_log),
            "CAMCTL_OSMO_TEST_READ_GATE": str(read_gate),
            "CAMCTL_OSMO_TEST_READ_GATE_AFTER_BYTES": "65536",
        }
    )
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


def _assert_cli_process_provenance(package: _InstalledPackage, process_log: Path) -> None:
    records = _records(process_log)
    assert records
    expected_origins = {
        "camctl",
        "camctl.cli",
        "camctl.capture.handlers",
        "camctl.devices.drivers.adb_cameras.osmo360ii_capture",
        "camctl.devices.drivers.adb_cameras.osmo360ii_results",
    }
    installed_site = (package.python.parent.parent / "lib" / "python3.11"
                      / "site-packages").resolve()
    for record in records:
        cwd = Path(record["cwd"]).resolve()
        assert cwd != _REPO and _REPO not in cwd.parents
        assert Path(record["python"]).resolve() == package.python.resolve()
        assert record["stage"] in ("started", "finished")
        assert type(record["pid"]) is int and record["pid"] > 0
        assert record["command"]
        if record["stage"] == "finished":
            assert type(record.get("exitcode")) is int
            assert type(record.get("finished_ns")) is int
        else:
            assert "exitcode" not in record
        assert record["argv"]
        helper = Path(record["argv"][0]).resolve()
        assert helper != _REPO and _REPO not in helper.parents
        origins = record.get("import_origins")
        assert isinstance(origins, dict) and set(origins) == expected_origins, record
        for origin in origins.values():
            path = Path(origin).resolve()
            assert installed_site in path.parents
            assert _REPO not in path.parents


def _assert_adb_process_provenance(package: _InstalledPackage, adb_log: Path) -> None:
    records = _records(adb_log)
    assert records
    expected_origins = {
        "camctl.devices.drivers.adb_cameras.commands",
        "camctl.devices.drivers.adb_cameras.filesystem",
    }
    installed_site = (package.python.parent.parent / "lib" / "python3.11"
                      / "site-packages").resolve()
    for record in records:
        cwd = Path(record["cwd"]).resolve()
        helper = Path(record["argv"][0]).resolve()
        assert cwd != _REPO and _REPO not in cwd.parents
        assert helper != _REPO and _REPO not in helper.parents
        assert Path(record["python"]).resolve() == package.python.resolve()
        assert type(record["pid"]) is int and record["pid"] > 0
        assert type(record["cli_pid"]) is int and record["cli_pid"] > 0
        origins = record["import_origins"]
        assert isinstance(origins, dict) and set(origins) == expected_origins
        for origin in origins.values():
            path = Path(origin).resolve()
            assert installed_site in path.parents
            assert _REPO not in path.parents


def _assert_runtime_paths_external(
        package: _InstalledPackage, environment: dict[str, str], scratch: Path,
        camctl: Path, adb_root: Path, include_preview: bool) -> None:
    tools = scratch / "tools"
    paths = [
        scratch, tools, camctl, tools / "adb", tools / "osmo-adb.py",
        tools / "osmo-clock-entry.py", tools / "_osmo_builtin_clock.py",
        Path(environment["CAMCTL_OSMO_TEST_CLOCK_FILE"]),
        Path(environment["CAMCTL_OSMO_TEST_CLOCK_LOG"]),
        adb_root, Path(environment["CAMCTL_OSMO_TEST_SAMPLE"]),
        Path(environment["CAMCTL_OSMO_TEST_ADB_LOG"]),
        Path(environment["CAMCTL_OSMO_TEST_PROCESS_LOG"]),
        Path(environment["CAMCTL_OSMO_TEST_READ_GATE"]),
    ]
    assert "UV_PYTHON" not in environment
    assert all(str(_REPO) not in value for value in environment.values())
    assert package.root in scratch.parents
    assert scratch.is_dir() and tools.is_dir() and adb_root.is_dir()
    assert camctl.is_file() and (tools / "adb").is_file()
    assert (tools / "osmo-adb.py").is_file()
    assert (tools / "osmo-clock-entry.py").is_file()
    assert (tools / "_osmo_builtin_clock.py").is_file()
    assert Path(environment["CAMCTL_OSMO_TEST_CLOCK_FILE"]).is_file()
    assert Path(environment["CAMCTL_OSMO_TEST_SAMPLE"]).is_file()
    preview_sample = Path(environment["CAMCTL_OSMO_TEST_SAMPLE"]).with_suffix(".lrf")
    assert preview_sample.is_file() is include_preview
    assert Path(environment["CAMCTL_OSMO_TEST_READ_GATE"]).is_file()
    assert all(path.resolve() != _REPO and _REPO not in path.resolve().parents
               for path in paths)
    assert all(package.root in path.resolve().parents for path in paths)
    path_entries = [Path(entry).resolve() for entry in environment["PATH"].split(os.pathsep)
                    if entry]
    assert path_entries[0] == tools.resolve()
    assert all(path != _REPO and _REPO not in path.parents for path in path_entries)
    assert package.root in package.python.parents


def test_installed_osmo_describe_exposes_both_formal_capture_types(osmo_package, tmp_path):
    """源码与独立 wheel 均通过正式内置登记导出两类 OSMO 拍摄能力。"""
    package = osmo_package
    config = _osmo_config(tmp_path / "osmo-config.toml")
    deployment = Deployment(tmp_path / "describe-world", devices=False)
    params = _capture_params("camera_record")
    scratch = package.root / "runtime" / "describe"
    scratch.mkdir(parents=True)
    environment, camctl, _, _ = _install_osmo_process_tools(
        package, deployment, action_type="camera_record", params=params, scratch=scratch)
    process_log = Path(environment["CAMCTL_OSMO_TEST_PROCESS_LOG"])
    _assert_runtime_paths_external(package, environment, scratch, camctl,
                                   Path(environment["CAMCTL_OSMO_TEST_ADB_ROOT"]), True)

    installed_origin = _run([str(package.python), "-c",
        "import camctl; print(camctl.__file__)",], cwd=package.root,
        env=package.environment, log_dir=package.log_dir).stdout.strip()
    origin = Path(installed_origin).resolve()
    assert package.root in origin.parents
    assert _REPO not in origin.parents

    installed = _describe([str(camctl), "describe", "--config", str(config)],
                          cwd=package.root, env=environment, log_dir=package.log_dir)
    _assert_cli_process_provenance(package, process_log)
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
    timelapse_sample = json.loads(resources[_OSMO_TIMELAPSE_RESOURCE])
    assert timelapse_sample["actions"][0]["params"]["interval_s"] == 2

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
@pytest.mark.parametrize(
    "action_type,include_preview",
    [("camera_record", True), ("camera_timelapse", True), ("camera_timelapse", False)],
    ids=["record-with-preview", "timelapse-with-preview", "timelapse-without-preview"],
)
def test_installed_osmo_capture_recovery_and_available_delivery(
        tmp_path, host_demo, osmo_package, monkeypatch, action_type, include_preview):
    """正式 OSMO 合同经客户端、C host、安装 CLI、恢复、取回与 ACK。

    受控时钟只接入既有业务时钟模块。driver 的 asyncio.sleep(30) 与
    ADB 记录的真实单调时长保持原样。
    """
    package = osmo_package
    serial = _SERIAL
    deployment = Deployment(tmp_path / "world", devices=False)
    _osmo_config(deployment.config_path, serial=serial, result_mode="single_file")
    params = _capture_params(action_type)
    preview_suffix = "with-preview" if include_preview else "without-preview"
    scratch = package.root / "runtime" / f"{action_type}-{preview_suffix}"
    environment, camctl, clock_file, adb_root = _install_osmo_process_tools(
        package, deployment, action_type=action_type, params=params, scratch=scratch,
        include_preview=include_preview)
    adb_log = Path(environment["CAMCTL_OSMO_TEST_ADB_LOG"])
    process_log = Path(environment["CAMCTL_OSMO_TEST_PROCESS_LOG"])
    clock_log = Path(environment["CAMCTL_OSMO_TEST_CLOCK_LOG"])
    initial_clock = read_clock(clock_file)
    assert initial_clock["mode"] == "frozen"
    _assert_runtime_paths_external(package, environment, scratch, camctl, adb_root,
                                   include_preview)

    initialized = _run([str(camctl), "init", "--config", str(deployment.config_path)],
                       cwd=deployment.root, env=environment, log_dir=package.log_dir)
    assert initialized.stdout == ""
    described = _describe(
        [str(camctl), "describe", "--config", str(deployment.config_path)],
        cwd=deployment.root, env=environment, log_dir=package.log_dir)
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
        _capture_body(action_type, params,
                      scheduled_at=format_utc(initial_clock["utc_us"])))
    assert capture_receipt["last_report_id"] is None
    exported_action = json.loads(capture_plan.read_text(encoding="utf-8"))["actions"][0]
    assert exported_action["scheduled_at"] == format_utc(initial_clock["utc_us"])
    exported_params = exported_action["params"]
    assert exported_params == params

    demo = HostDemo(deployment, host_demo, launcher=str(camctl))
    monkeypatch.chdir(package.root)
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
                "SELECT t.id,t.status,t.result_event_id,t.result_json FROM operation_attempts t"
                " JOIN operation_runs r ON r.id=t.run_id"
                " WHERE r.action_id=? AND r.kind=? ORDER BY t.id",
                (action_id, start_attempt_kind))
            return rows if (len(rows) == 1 and rows[0][2] is not None
                            and rows[0][3] is not None) else None

        attempts = wait_for(saved_start_attempt, 90,
                            "正式 OSMO START 调用结果未可靠保存")
        assert len(attempts) == 1 and attempts[0][2] is not None
        start_call = next(call for call in _records(adb_log) if call.get("kind") == "start")
        start_response = next(call for call in _records(adb_log)
                              if call.get("kind") == "start_response")
        assert start_call["started_clock_utc_us"] == initial_clock["utc_us"]
        assert start_call["started_clock_ns"] == initial_clock["monotonic_ns"]
        assert start_call["started_epoch"] == initial_clock["epoch"]
        assert start_response["returned_clock_utc_us"] == initial_clock["utc_us"]
        assert start_response["returned_clock_ns"] == initial_clock["monotonic_ns"]
        assert start_response["returned_epoch"] == initial_clock["epoch"]
        ack_clock_reads = [record for record in _records(clock_log)
                           if record.get("pid") == start_response["cli_pid"]
                           and record.get("epoch") == start_response["returned_epoch"]
                           and record.get("utc_us") == start_response["returned_clock_utc_us"]
                           and record.get("monotonic_ns") == start_response["returned_clock_ns"]
                           and record.get("real_monotonic_ns", -1) >= start_response["returned_ns"]
                           and _clock_frames(
                               record, module="camctl.capture.handlers",
                               function="_control_call")]
        assert {record["kind"] for record in ack_clock_reads} >= {"time", "monotonic_ns"}
        ack_call_lines = {
            kind: {frame["line"] for record in ack_clock_reads
                   if record["kind"] == kind
                   for frame in _clock_frames(
                       record, module="camctl.capture.handlers", function="_control_call")}
            for kind in ("time", "monotonic_ns")
        }
        assert ack_call_lines["time"] & ack_call_lines["monotonic_ns"]
        installed_site = (package.python.parent.parent / "lib" / "python3.11"
                          / "site-packages").resolve()
        assert all(installed_site in Path(frame["file"]).resolve().parents
                   for record in ack_clock_reads
                   for frame in _clock_frames(
                       record, module="camctl.capture.handlers", function="_control_call"))
        started = _capture_facts(deployment.state_db)[0]
        assert started[6] == initial_clock["utc_us"]
        if action_type == "camera_timelapse":
            assert started[6] is not None
        original_execution = json.loads(started[4])
        assert original_execution["video_result_mode"] == "single_file"
        expected_target_ms = 10000 if action_type == "camera_record" else 300000
        assert original_execution["target_duration_ms"] == expected_target_ms
        assert original_execution["file_completion_wait_ms"] == 30000
        assert original_execution["startup_margin_ms"] == 0
        from camctl.devices.tasks import EndControl, StartReturn

        if action_type == "camera_timelapse":
            from camctl.devices.tasks import CompletionMode

            assert original_execution["end_control"] == int(EndControl.DEVICE)
            assert original_execution["start_return_meaning"] == int(StartReturn.SENT)
            assert original_execution["completion_mode"] == int(CompletionMode.TIME_AND_OUTPUTS)
            assert original_execution["result_wait_margin_ms"] == 30000
            assert started[5] is None
            assert started[6] is not None
            expected_start_observation = "timelapse_sent"
        else:
            # SENT records a reliable command acknowledgement while the device's
            # actual recording start time remains unknown.
            start_return_rows = query_state(deployment.state_db,
                "SELECT start_return_meaning FROM device_activities WHERE action_id=?",
                (action_id,))
            assert start_return_rows == [(int(StartReturn.SENT),)]
            assert original_execution["start_return_meaning"] == int(StartReturn.SENT)
            assert started[5] is None and started[6] is not None
            expected_start_observation = "recording_sent"
        start_document = json.loads(attempts[0][3])
        assert [item["type"] for item in start_document["observations"]] == [
            expected_start_observation]
        assert start_document["observations"][0]["data"]["response_payload"] == "00"

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

        # 中断原 C host run，保留已确认的 START、原参数、原绑定和基准。
        demo.stop()
        natural_s = 9 if action_type == "camera_record" else 329
        boundary_utc_us = initial_clock["utc_us"] + natural_s * 1_000_000
        boundary_monotonic_ns = initial_clock["monotonic_ns"] + natural_s * 1_000_000_000
        negative_state = set_frozen(
            clock_file, utc_us=boundary_utc_us, monotonic_ns=boundary_monotonic_ns)
        previous_pids = {row["pid"] for row in _records(process_log)}
        demo = HostDemo(deployment, host_demo, launcher=str(camctl))
        demo.start(env=environment)
        demo.submit(capture_plan)

        recovered_run = _wait_for_run_process(
            process_log, demo.log_path, previous_pids)
        decision = ("_reconcile_recording", "time") if action_type == "camera_record" else (
            "_advance_timelapse_natural_wait", "monotonic_ns")
        line, real_gap_ns = _wait_for_boundary_reads(
            clock_log, pid=recovered_run["pid"], epoch=negative_state["epoch"],
            module="camctl.capture.handlers", function=decision[0], kind=decision[1],
            utc_us=boundary_utc_us, monotonic_ns=boundary_monotonic_ns)
        installed_site = (package.python.parent.parent / "lib" / "python3.11"
                          / "site-packages").resolve()
        line_records = [record for record in _records(clock_log)
                        if record.get("pid") == recovered_run["pid"]
                        and record.get("epoch") == negative_state["epoch"]
                        and record.get("kind") == decision[1]
                        and record.get("utc_us") == boundary_utc_us
                        and record.get("monotonic_ns") == boundary_monotonic_ns
                        and any(frame.get("module") == "camctl.capture.handlers"
                                and frame.get("function") == decision[0]
                                and frame.get("line") == line
                                for frame in record.get("stack", ()))]
        assert line_records
        assert all(installed_site in Path(frame["file"]).resolve().parents
                   for record in line_records
                   for frame in _clock_frames(
                       record, module="camctl.capture.handlers", function=decision[0])
                   if frame["line"] == line)
        assert real_gap_ns >= 500_000_000
        recovered_host_log = demo.module_log()
        assert re.search(
            rf"command=run pid={recovered_run['pid']} spawned execution=unknown",
            recovered_host_log)
        activity_id = int(query_state(deployment.state_db,
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,))[0][0])
        _assert_recovery_boundary_empty(
            deployment, adb_log, action_id=action_id, activity_id=activity_id,
            recovery_started_ns=recovered_run["started_ns"])

        # 仍在运行的同一 CLI 进程释放到原冻结规则的截止时点。
        release_utc_us = initial_clock["utc_us"] + (natural_s + 1) * 1_000_000
        release_monotonic_ns = initial_clock["monotonic_ns"] + (natural_s + 1) * 1_000_000_000
        release_state = set_running(
            clock_file, utc_us=release_utc_us, monotonic_ns=release_monotonic_ns)
        assert release_state["epoch"] == negative_state["epoch"] + 1
        released_read = _wait_for_running_read(
            clock_log, pid=recovered_run["pid"], epoch=release_state["epoch"],
            module="camctl.capture.handlers", function=decision[0], kind=decision[1],
            utc_us=release_utc_us, monotonic_ns=release_monotonic_ns,
            real_monotonic_anchor_ns=release_state["real_monotonic_anchor_ns"])
        assert any(frame.get("line") == line for frame in _clock_frames(
            released_read, module="camctl.capture.handlers", function=decision[0]))

        # This installed-chain case freezes the original single-file path for
        # both OSMO capture types. Segmented completion-unknown behavior is
        # covered by the dedicated segmentation scenarios.
        capture_report = wait_for(
            lambda: _report_with_statuses(deployment.ready, {"capture": "succeeded"}),
            150, "安装版 OSMO capture 未完成或未发布预期报告")
        if action_type == "camera_record":
            # 单文件录像通过已确认 STOP 和完整结果等待完成；它不进入分段
            # assessment，也不适用“尾段无法证明覆盖目标”的失败规则。
            assert _action(capture_report, "capture")["status"] == "succeeded"
        assert query_state(deployment.state_db,
            "SELECT id FROM segment_sets WHERE action_id=?", (action_id,)) == []
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
            # 主机计时录像在 10 秒目标后发唯一 STOP 00，
            # 再由 driver 独立等待 30 秒。
            assert len(stops) == 1, all_calls
            assert stops[0]["started_clock_ns"] - response["returned_clock_ns"] >= 10 * 1_000_000_000
            assert stops[0]["started_clock_utc_us"] - response["returned_clock_utc_us"] >= 10 * 1_000_000
            assert stops[0]["started_epoch"] == release_state["epoch"]
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
            # DEVICE/SENT/TIME_AND_OUTPUTS 自然完成等待 300 秒目标和 30 秒结果余量，
            # 随后读取原活动结果；正常结束没有 STOP，也没有 stop_return file_completion。
            assert stops == [], all_calls
            directories = [call for call in all_calls if call.get("kind") == "directory"
                           and call.get("started_epoch") == release_state["epoch"]
                           and call.get("started_clock_ns", 0) >= response["returned_clock_ns"]]
            assert directories, all_calls
            assert directories[0]["started_clock_ns"] - response["returned_clock_ns"] >= 330 * 1_000_000_000
            assert directories[0]["started_clock_utc_us"] - response["returned_clock_utc_us"] >= 330 * 1_000_000

        device_files = query_state(deployment.state_db,
            "SELECT id,locator_json,role,original_device_file_id,pairing_evidence_json,"
            "completion_state,size_bytes,completion_evidence_json FROM device_files"
            " WHERE source_action_id=? ORDER BY role",
            (action_id,))
        roles = enum_for("device_files.role")
        completed = enum_for("device_files.completion_state").COMPLETE
        by_path = {json.loads(row[1])["path"]: row for row in device_files}
        expected_paths = {_OSV_PATH}
        if include_preview:
            expected_paths.add(_LRF_PATH)
        assert set(by_path) == expected_paths
        assert len(device_files) == len(expected_paths)
        original = by_path[_OSV_PATH]
        assert original[2] == int(roles.ORIGINAL)
        assert original[3] is None and original[4] is None
        assert original[5] == int(completed) and original[6] == len(_OSV_CONTENT) >= 1
        if include_preview:
            preview = by_path[_LRF_PATH]
            assert preview[2] == int(roles.PREVIEW)
            assert preview[3] == original[0]
            pairing = json.loads(preview[4])
            assert pairing == {
                "method": int(enum_for("device_files.pairing_evidence_json.method").DRIVER_PAIRING),
                "observation": {"paired_identity": _OSV_PATH},
            }
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
            assert activity_evidence["observation"]["file_count"] == len(expected_paths)
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
        expected_outputs = [(int(output_kinds.ORIGINAL), Path(_OSV_PATH).name)]
        if include_preview:
            expected_outputs.append((int(output_kinds.PREVIEW), Path(_LRF_PATH).name))
        assert outputs == expected_outputs

        if action_type == "camera_record":
            activity_id = query_state(deployment.state_db,
                "SELECT id FROM device_activities WHERE action_id=?", (action_id,))[0][0]
        from camctl.history.events import load_event_registry
        page_event = load_event_registry()["events"]["RESULT_PAGE"]
        page_rows = query_state(deployment.state_db,
            "SELECT body_json FROM history_events"
            " WHERE event_type=? AND json_extract(body_json,'$.reason')=?"
            " AND json_extract(body_json,'$.evidence.result_page.activity_id')=?",
                (page_event["id"], page_event["branches"]["APPEND"]["reason"], activity_id))
        listed = []
        for (body,) in page_rows:
            page = json.loads(body)["evidence"]["result_page"]
            result = page["outcome"]["result"]
            listed.extend(entry for observation in result["observations"]
                          if observation["type"] == "result_files_listed"
                          for entry in observation["data"]["entries"])
        expected_listed = {(_OSV_PATH, "video", "osv", None)}
        if include_preview:
            expected_listed.add((_LRF_PATH, "video", "lrf", _OSV_PATH))
        assert {(entry["identity"], entry["kind"], entry["format_id"], entry["paired_identity"])
                for entry in listed} == expected_listed
        assert len(listed) == len(expected_listed) * len(result_rows)
        if action_type == "camera_record":
            assert len(result_rows) == 1
            assert query_state(deployment.state_db,
                "SELECT status,attempts_used,max_attempts_used FROM operation_runs"
                " WHERE action_id=? AND kind=?",
                (action_id, int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS))) == [(
                    int(enum_for("operation_runs.status").SUCCEEDED), 1, 3)]

        demo.claim()
        saved = deployment.import_reports_with_client(deployment.processing)
        assert capture_report["report_id"] in saved["saved_report_ids"]
        assert len(_records(process_log)) >= 3

        delivery_ids = []
        obtain_reports = []
        interrupted_delivery_id = None
        interrupted_copy_id = None
        interrupted_action_id = None
        obtains = [("obtain-original", "default", _OSV_CONTENT)]
        if include_preview:
            obtains.append(("obtain-preview", "preview", _LRF_CONTENT))
        for obtain_name, output_filter, content in obtains:
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
            preview_source = adb_root / _LRF_PATH.lstrip("/")
            if include_preview:
                assert preview_source.read_bytes() == _LRF_CONTENT
            else:
                assert not preview_source.exists()
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

        assert len(delivery_ids) == 1 + int(include_preview)
        assert len(set(delivery_ids)) == len(delivery_ids)
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
        preview_source = adb_root / _LRF_PATH.lstrip("/")
        if include_preview:
            assert preview_source.read_bytes() == _LRF_CONTENT
        else:
            assert not preview_source.exists()
        _assert_cli_process_provenance(package, process_log)
        _assert_adb_process_provenance(package, adb_log)
    finally:
        demo.stop()
        archive = _archive_logs(package.root, package.log_dir)
        shutil.copytree(deployment.root,
            archive / "cases" / f"{action_type}-{preview_suffix}", dirs_exist_ok=True)
