"""R6 专用部署、工具、只读证据和真实报告交接支持。"""

from __future__ import annotations

import base64
from contextlib import closing
from copy import copy, deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from _osmo_builtin_clock import format_utc, read_clock, set_frozen, whole_second_utc_us
from _wsl_host_demo import HostDemo
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import validate_document
from camctl_fixtures import Deployment


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ARTIFACTS = REPO / ".local/r6/artifacts"
OSV = "/mnt/media_rw/emulated/DCIM/OSMO_0001/VID_0001.OSV"
LRF = "/mnt/media_rw/emulated/DCIM/OSMO_0001/VID_0001.LRF"
SERIAL = "r6-osmo-serial"
RAW = b"II*\x00small integration fixture"
PREVIEW = b"\xff\xd8small preview fixture\xff\xd9"


def _hasselblad_photo_sample_params():
    sample = REPO / "docs/camctl/examples/camera-demo/hasselblad-photo.json"
    return json.loads(sample.read_text(encoding="utf-8"))["actions"][0]["params"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_write(path, data):
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


def rows(database, sql, params=()):
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params)]


def wait(predicate, message, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.025)


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def process_identity(pid):
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return {"pid": pid, "state": stat[0], "ppid": int(stat[1]),
            "pgid": int(stat[2]), "starttime": int(stat[19]), "utc_ns": time.time_ns()}


@dataclass(frozen=True)
class Package:
    root: Path
    python: Path
    cli: Path
    env: dict
    logs: Path


def run(command, *, cwd, env, logs, timeout=180):
    logs.mkdir(parents=True, exist_ok=True)
    number = len(list(logs.glob("command-*.json"))) + 1
    started = time.time_ns()
    record = {
        "argv": command, "cwd": cwd, "python": sys.executable,
        "utc_start_ns": started,
        "env": {key: value for key, value in env.items() if key.startswith((
            "PYTHON", "UV_", "CAMCTL_")) or key == "PATH"},
    }
    target = logs / f"command-{number:04}.json"
    json_write(target, record)
    try:
        result = subprocess.run([str(part) for part in command], cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=timeout)
    except BaseException as error:
        json_write(target, {**record, "utc_end_ns": time.time_ns(),
                            "exception": f"{type(error).__name__}: {error}"})
        raise
    json_write(target, {**record, "utc_end_ns": time.time_ns(),
                       "exitcode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
    assert result.returncode == 0, f"{command}\n{result.stdout}\n{result.stderr}"
    return result


@pytest.fixture(scope="module")
def cleanup_package(tmp_path_factory):
    assert sys.version_info[:2] == (3, 11)
    assert Path(sys.executable).parent == REPO / "apps/camctl/.venv311/bin"
    root = tmp_path_factory.mktemp("r6-installed")
    logs = root / "commands"
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONHOME", "PWD") and not key.startswith("CAMCTL_")}
    env["PYTHONNOUSERSITE"] = "1"
    env["UV_PYTHON"] = sys.executable
    env["PATH"] = os.pathsep.join(part for part in env["PATH"].split(os.pathsep)
                                   if str(REPO) not in part)
    def command(*args):
        return run(args, cwd=root, env=env, logs=logs)
    command("uv", "build", "--project", REPO / "apps/camctl", "--wheel", "--out-dir", root / "dist")
    wheel, = (root / "dist").glob("*.whl")
    command("uv", "export", "--project", REPO / "apps/camctl", "--frozen", "--no-dev",
            "--no-emit-project", "-o", root / "requirements.txt")
    command("uv", "venv", "--python", sys.executable, root / "venv")
    python = root / "venv/bin/python"
    command("uv", "pip", "install", "--python", python, "--require-hashes", "-r", root / "requirements.txt")
    command("uv", "pip", "install", "--python", python, "--no-deps", wheel)
    env.pop("UV_PYTHON")
    provenance = command(python, "-c", '''import importlib,importlib.metadata,json,sys
names=["camctl","camctl.cli","camctl.outputs.cleanup_flow","camctl.outputs.cleanup_results",
"camctl.persistence.repositories.outputs","camctl.devices.drivers.adb_cameras.file_commands",
"camctl.devices.drivers.adb_cameras.osmo360ii_capture","camctl.capture.media",
"camctl.capture.media_flow","camctl.capture.handlers","camctl.bootstrap.cleanup_assembly",
"camctl.bootstrap.background_flow","camctl.session.service"]
from pathlib import Path
from camctl.bootstrap.application import ConfigAdapter
from camctl.devices.drivers.builtin import register_builtin_drivers
from camctl.devices.drivers.runtime import current_registry
from camctl.devices.drivers.registry import port_for
from camctl.devices.drivers.adb_cameras.contracts import CameraCall
register_builtin_drivers(ConfigAdapter(home=Path.cwd()).load(None))
entry=current_registry().entry("dji-osmo360-ii")
declaration={name:getattr(entry.declaration,name) for name in (
    "delete_supported","file_query_supported","query_supported")}
assert declaration=={"delete_supported":True,"file_query_supported":True,"query_supported":False}
assert callable(port_for(entry,"delete").delete) and callable(port_for(entry,"file_query").query_file)
assert all(callable(entry.driver.contract.commands[kind]) for kind in (CameraCall.DELETE,CameraCall.FILE_QUERY))
print(json.dumps({"version":importlib.metadata.version("camctl"),"distribution":str(importlib.metadata.distribution("camctl")._path),"python":sys.executable,"modules":{n:importlib.import_module(n).__file__ for n in names},"osmo_file_declaration":declaration}))''')
    installed = json.loads(provenance.stdout)
    assert all("site-packages" in path and str(REPO) not in path for path in installed["modules"].values())
    json_write(
        root / "provenance.json",
        {
            **installed,
            "wheel_sha256": sha(wheel.read_bytes()),
            "wheel": wheel,
            "source_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
            ).strip(),
            "tracked_diff_sha256": sha(
                subprocess.check_output(["git", "diff", "HEAD", "--binary"], cwd=REPO)
            ),
            "test_inputs": {
                name: sha((HERE / name).read_bytes())
                for name in (
                    "test_device_file_cleanup_roundtrip.py",
                    "_cleanup_roundtrip_support.py",
                    "_cleanup_source_snapshot.py",
                    "_cleanup_roundtrip_adb.py",
                    "_cleanup_roundtrip_fault.py",
                    "_osmo_builtin_adb.py",
                    "_osmo_builtin_clock_entry.py",
                )
            },
        },
    )
    try:
        yield Package(root, python, root / "venv/bin/camctl", env, logs)
    finally:
        archive = ARTIFACTS / (root.parent.name + "-" + root.name)
        archive.mkdir(parents=True, exist_ok=True)
        shutil.copytree(logs, archive / "commands", dirs_exist_ok=True)
        shutil.copy2(root / "provenance.json", archive)
        shutil.copy2(root / "requirements.txt", archive)
        shutil.copytree(root / "dist", archive / "dist", dirs_exist_ok=True)


class World:
    def __init__(self, package, tmp_path, host_demo, monkeypatch, *, osmo=True,
                 prepared=None, case_id=None):
        self.package = package
        self.root = tmp_path / "world"
        self.case_id = case_id or tmp_path.name
        if prepared is None:
            self.deployment = Deployment(self.root, devices=False)
        else:
            assert self.root == prepared.root
            assert package is prepared.package and osmo == prepared.with_osmo
            self.deployment = copy(prepared.deployment)
        self.db = self.deployment.state_db
        self.demo_path = host_demo
        self.monkeypatch = monkeypatch
        self.env = dict(package.env)
        self.tools = self.root / "tools"
        self.remote = self.root / "remote"
        self.clock = self.root / "clock.json"
        self.calls_path = self.root / "adb.jsonl"
        self.manifest = self.root / "manifest.json"
        self.marker = self.root / "marker.json"
        self.sources = {}
        self.frozen = {}
        self.members = {}
        self.host = None
        self.config_extra = ""
        self.with_osmo = osmo
        self.config_original = (self.deployment.config_path.read_text()
                                if prepared is None else prepared.config_original)
        self.monkeypatch.chdir(self.root)
        self.deployment.run_client_driver = self.client
        self.params = {"type": "osmo360ii_record", "duration_s": 1,
                       "bitrate": "high", "exposure": {"mode": "auto"}}
        if prepared is not None:
            self.env = dict(prepared.env)
            self.clock_cli = self.tools / "clock-cli"
            self.register_sources()
            return
        self.tools.mkdir()
        (self.remote / "mnt/media_rw/emulated/DCIM").mkdir(parents=True)
        self.write_config()
        for source in ("_osmo_builtin_clock.py", "_osmo_builtin_clock_entry.py", "_osmo_builtin_adb.py",
                       "_cleanup_roundtrip_adb.py", "_cleanup_roundtrip_fault.py"):
            if (HERE / source).is_file():
                shutil.copy2(HERE / source, self.tools / source)
        self.clock_cli = self.launcher("clock-cli", self.tools / "_osmo_builtin_clock_entry.py")
        self.launcher("adb", self.tools / "_osmo_builtin_adb.py")
        self.env.update(
            {
                "PATH": str(self.tools) + os.pathsep + self.env["PATH"],
                "CAMCTL_OSMO_TEST_SINGLE_ORIGINAL": "1",
                "CAMCTL_OSMO_TEST_ACTION": "camera_record",
                "CAMCTL_OSMO_TEST_PARAMS": json.dumps(self.params),
                "CAMCTL_OSMO_TEST_INCLUDE_PREVIEW": "1",
                "CAMCTL_OSMO_TEST_SERIAL": SERIAL,
                "CAMCTL_OSMO_TEST_ADB_ROOT": str(self.remote),
                "CAMCTL_OSMO_TEST_ADB_LOG": str(self.calls_path),
                "CAMCTL_OSMO_TEST_SAMPLE": str(self.root / "sample.OSV"),
                "CAMCTL_OSMO_TEST_CLOCK_FILE": str(self.clock),
                "CAMCTL_OSMO_TEST_CLOCK_LOG": str(self.root / "clock-reads.jsonl"),
                "CAMCTL_OSMO_TEST_PROCESS_LOG": str(self.root / "clock-cli.jsonl"),
            }
        )
        set_frozen(self.clock, utc_us=whole_second_utc_us(), monotonic_ns=time.monotonic_ns())
        self.command(package.cli, "init", "--config", self.deployment.config_path)
        self.install_capabilities()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        if self.db.exists():
            json_write(self.root / "final-db.json", self.snapshot())
        archive = ARTIFACTS / (self.case_id + "-" + str(os.getpid()))
        shutil.copytree(self.root, archive, dirs_exist_ok=True)

    def write_config(self, max_d=3, max_q=3, timeout="10", interval="0", query_timeout="10"):
        device_section = f'''
[devices.osmo]
kind="camera"
driver="dji-osmo360-ii"
[devices.osmo.adb]
serial="{SERIAL}"
[devices.osmo.recording]
repair_margin_s="0"
[devices.osmo.cleanup]
delete_timeout_s="{timeout}"
query_timeout_s="{query_timeout}"
delete_retry_interval_s="{interval}"
query_retry_interval_s="0"
''' if self.with_osmo else ""
        self.deployment.config_path.write_text(self.config_original + f'''
[cleanup]
max_delete_attempts={max_d}
max_query_attempts={max_q}
''' + device_section + self.config_extra)

    def command(self, *args):
        return run(args, cwd=self.root, env=self.env, logs=self.root / "commands")

    def launcher(self, name, entry):
        target = self.tools / name
        target.write_text("#!/bin/sh\nexec " + shlex.quote(str(self.package.python)) + " "
                          + shlex.quote(str(entry)) + ' "$@"\n')
        target.chmod(0o755)
        return target

    def client(self, *args, output):
        result = run(["node", "--import", "tsx", HERE / "client_driver.ts", *args, output],
                     cwd=REPO / "apps/client", env=self.package.env, logs=self.root / "commands")
        return json.loads(output.read_text())

    def install_capabilities(self):
        described = self.command(self.package.cli, "describe", "--config", self.deployment.config_path)
        store = self.root / "client-store"
        store.mkdir(exist_ok=True)
        (store / "device-capabilities.json").write_text(described.stdout)

    def start(self, launcher=None):
        assert self.host is None
        self.host = HostDemo(self.deployment, self.demo_path, launcher=str(launcher or self.package.cli))
        self.host_log_offset = len(self.host.module_log()) if self.host.log_path.exists() else 0
        self.host.start(env=self.env)

    def wait_normal_run(self):
        def result():
            current = self.host.module_log()[self.host_log_offset:]
            if "result=error" in current:
                raise AssertionError("真实 C host 收到 CLI 错误：\n" + current)
            return "command=run" in current and "result=success exit_kind=0 exit=0" in current
        wait(result, "本次普通 RUN 未完成报告职责")

    def stop(self):
        if self.host is None:
            return
        identities = []
        host_pid = self.host._host_pid
        # 先固定主程序的子进程集合，后续收场和验收采用同一组身份。
        os.kill(host_pid, signal.SIGSTOP)
        identities.append(process_identity(host_pid))
        observed = []
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                item = process_identity(int(path.parent.name))
            except (OSError, ValueError):
                continue
            observed.append(item)
        groups = {item["pgid"] for item in observed if item["ppid"] == host_pid}
        identities.extend(item for item in observed
                          if item["ppid"] == host_pid or item["pgid"] in groups)
        log = self.host.log_path.read_bytes() if self.host.log_path.exists() else b""
        (self.root / f"host-{len(list(self.root.glob('host-*.log')))}.log").write_bytes(log)
        self.host.stop()
        self.host = None
        def active():
            found = []
            for path in Path("/proc").glob("[0-9]*/stat"):
                try:
                    item = process_identity(int(path.parent.name))
                except (OSError, ValueError):
                    continue
                if item["pgid"] in groups and item["state"] not in ("Z", "X"):
                    found.append(item)
            return found
        wait(lambda: not active(), "旧受管组仍有活动成员", 15)
        for old in identities:
            try:
                current = process_identity(old["pid"])
            except OSError:
                continue
            assert current["starttime"] != old["starttime"] or current["state"] in ("Z", "X")
        json_write(self.root / f"settled-{len(list(self.root.glob('settled-*.json')))}.json",
                   {"old": identities, "groups": sorted(groups), "active_after": active()})

    def export(self, name, actions):
        path, receipt = self.deployment.export_plan_with_client({"name": name, "actions": actions})
        body = parse_exact_json(path.read_text())
        validate_document("protocol/plan.schema.json", body)
        shutil.copy2(path, self.root / f"request-{receipt['request_id']}.json")
        return path, receipt

    def action(self, name, action_type, params, device=None, scheduled=None):
        result = {"name": name, "type": action_type, "params": params,
                  "scheduled_at": scheduled or format_utc(time.time_ns() // 1000 - 1_000_000)}
        if device:
            result.update(device_id=device, policy={"max_delay_ms": 60000})
        return result

    def wait_action(self, request, expected=None):
        terminal = {int(enum_for("actions.status")[name]) for name in ("SUCCEEDED", "FAILED", "CANCELED", "EXPIRED")}
        def finished():
            result = rows(self.db, "SELECT a.* FROM actions a JOIN plans p ON p.id=a.plan_id WHERE p.request_id=?", (request,))
            return result if result and all(row["status"] in terminal for row in result) else None
        result = wait(finished, f"动作未结束：{request}", 150)
        if expected:
            assert [row["status"] for row in result] == [int(enum_for("actions.status")[expected])] * len(result)
        return result

    def prepare_osmo(self, *, start_host=True):
        if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
            pytest.skip("R6 媒体准备要求真实 ffmpeg/ffprobe；完整验收尚未完成")
        self.command("ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=10:d=3",
            "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=10:d=3", "-map", "0:v:0", "-map", "1:v:0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-keyint_min", "10",
            "-sc_threshold", "0", "-bf", "0", "-f", "mp4", self.root / "sample.OSV")
        self.probe(self.root / "sample.OSV", 3)
        (self.root / "sample.lrf").write_bytes(b"independent nonempty LRF model bytes")
        initial = read_clock(self.clock)
        plan, receipt = self.export("capture-osmo", [self.action("capture-osmo", "camera_record", self.params,
                                                "osmo", format_utc(initial["utc_us"]))])
        self.start(self.clock_cli)
        self.host.submit(plan)
        wait(lambda: rows(self.db, "SELECT t.result_event_id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id WHERE r.kind=? AND t.result_event_id IS NOT NULL", (int(enum_for("operation_runs.kind").START),)), "START 未保存")
        self.stop()
        set_frozen(self.clock, utc_us=initial["utc_us"] + 3_000_000,
                   monotonic_ns=initial["monotonic_ns"] + 3_000_000_000)
        self.start(self.clock_cli)
        actions = self.wait_action(receipt["request_id"], "SUCCEEDED")
        self.stop()
        (frozen,) = rows(
            self.db, "SELECT execution_spec_json FROM actions WHERE name='capture-osmo'"
        )
        assert json.loads(frozen["execution_spec_json"]).get("result_policy") is None
        calls = records(self.calls_path)
        starts = [row for row in calls if row.get("kind") == "start"]
        stops = [row for row in calls if row.get("kind") == "stop"]
        assert len(starts) == len(stops) == 1
        directory = next(row for row in calls if row.get("kind") == "directory" and row["started_ns"] > stops[0]["returned_ns"])
        assert directory["started_ns"] - stops[0]["returned_ns"] >= 3_000_000_000
        returns = rows(self.db, "SELECT result_json FROM operation_attempts WHERE result_json IS NOT NULL")
        completions = [json.loads(row["result_json"])["settlement"]["evidence"]["data"].get("file_completion") for row in returns]
        assert any(value and value["required_wait_ms"] == 3000 and value["completed"] and value["observed_wait_ns"] >= 3_000_000_000 for value in completions)
        self.register_sources()
        assert {"osv", "lrf", "repaired"} <= self.sources.keys(), self.snapshot()
        self.probe(self.path("repaired"), 1)
        processing, = rows(self.db, "SELECT * FROM recording_processing")
        basis = parse_exact_json(processing["repair_basis_json"])
        assert basis["target_duration_ms"] == 1000 and basis["control_elapsed_ns"] == 3_000_000_000
        assert basis["control_elapsed_ns"] - 1_000_000_000 == 2_000_000_000
        assert processing["repair_state"] == int(enum_for("recording_processing.repair_state").SUCCEEDED)
        events = [parse_exact_json(row["body_json"]) for row in rows(self.db, "SELECT body_json FROM history_events ORDER BY id")]
        for key, minimum in (("source_probe", 3), ("output_probe", 1)):
            probes = [event["evidence"][key] for event in events if key in event.get("evidence", {})]
            assert len(probes) == 1
            probe, = probes
            assert probe["error"] is None and probe["duration_s"] >= minimum
            assert [(s["index"], s["codec_name"], s["width"], s["height"], s["attached_pic"])
                    for s in probe["streams"]] == [(0, "h264", 64, 64, False), (1, "h264", 64, 64, False)]
            assert all(s["duration_s"] >= minimum for s in probe["streams"])
        repaired, = rows(self.db, "SELECT * FROM intermediate_files WHERE id=?", (processing["repair_output_file_id"],))
        content = self.path("repaired").read_bytes()
        assert (repaired["size_bytes"], repaired["sha256"]) == (len(content), sha(content))
        if start_host:
            self.start()

    def probe(self, path, minimum):
        from decimal import Decimal
        result = self.command("ffprobe", "-v", "error", "-show_entries",
            "format=duration,format_name:stream=index,codec_type,codec_name,width,height,duration:stream_disposition=attached_pic",
            "-of", "json", path)
        probe = json.loads(result.stdout)
        assert "mp4" in probe["format"]["format_name"].split(",")
        assert Decimal(probe["format"]["duration"]) >= minimum
        assert len(probe["streams"]) == 2
        assert {stream["index"] for stream in probe["streams"]} == {0, 1}
        for stream in probe["streams"]:
            assert (stream["codec_type"], stream["codec_name"], stream["width"], stream["height"]) == ("video", "h264", 64, 64)
            assert stream["disposition"]["attached_pic"] == 0
            assert Decimal(stream["duration"]) >= minimum
        self.command("ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0", "-f", "null", "-")

    def prepare_hasselblad(self):
        self.stop()
        photo_params = _hasselblad_photo_sample_params()
        for mode in ("host", "device"):
            tool = self.tools / f"hb-{mode}"
            tool.write_text("#!" + str(self.package.python) + "\n" + _hb_tool(mode, photo_params))
            tool.chmod(0o755)
            self.config_extra += f'''\n[devices.hb-{mode}]\nkind="camera"\ndriver="hasselblad_cfv100c"\n[devices.hb-{mode}.hasselblad]\nexecutable="{tool}"\nserial="R6-HB-{mode}"\nproduct_id=10\noutput_storage="{mode}"\n'''
        self.write_config()
        self.install_capabilities()
        self.start()
        plan, receipt = self.export("capture-hb", [self.action("capture-hb-" + mode, "camera_take_photo",
                  deepcopy(photo_params), "hb-" + mode) for mode in ("host", "device")])
        self.host.submit(plan)
        self.wait_action(receipt["request_id"], "SUCCEEDED")
        self.register_sources()
        assert {"hb-original", "hb-preview", "hb-device-original", "hb-device-preview"} <= self.sources.keys()

    def register_sources(self):
        result = rows(self.db, "SELECT o.*,a.name AS action_name,d.locator_json,f.relative_path FROM outputs o JOIN actions a ON a.id=o.source_action_id LEFT JOIN device_files d ON d.id=o.device_file_id LEFT JOIN intermediate_files f ON f.id=o.intermediate_file_id")
        for row in result:
            if row["action_name"] == "capture-osmo":
                key = "repaired" if row["intermediate_file_id"] else ("osv" if json.loads(row["locator_json"])["path"] == OSV else "lrf")
            else:
                kind = "original" if row["kind"] == int(enum_for("outputs.kind").ORIGINAL) else "preview"
                key = "hb-" + ("device-" if row["device_file_id"] else "") + kind
            self.sources[key] = row

    def path(self, key):
        source = self.sources[key]
        return (self.deployment.staging / source["relative_path"] if source["intermediate_file_id"]
                else self.remote / json.loads(source["locator_json"])["path"].lstrip("/"))

    def snapshot(self):
        return {table: rows(self.db, "SELECT * FROM " + table) for table in (
            "plans", "actions", "outputs", "device_files", "intermediate_files", "recording_processing",
            "cleanup_items", "cancel_items", "operation_runs", "operation_attempts", "reports", "runtime_state",
            "history_events", "history_transactions")}

    def calls(self, operation=None):
        return [row for row in records(self.calls_path) if row.get("kind") == "cleanup" and row["stage"] == "entered" and (operation is None or row["operation"] == operation)]

    def manifest_for(self, request, fault=None, query_unknown=False, output_id=None):
        targets = []
        for source in self.sources.values():
            if source["device_file_id"] and source["action_name"] == "capture-osmo":
                path = json.loads(source["locator_json"])["path"]
                parent = str(PurePosixPath(path).parent)
                targets.append({"output_id": source["id"], "path": path, "parent": parent,
                                "scan_root": parent + "/", "operations": ["delete", "query"]})
        json_write(self.manifest, {"root": str(self.remote.resolve()), "case": self.case_id,
            "request_id": request, "targets": targets, "database": str(self.db),
            "fault": fault, "query_unknown": query_unknown, "marker": str(self.marker),
            "fault_output_id": output_id})
        self.env["CAMCTL_CLEANUP_MANIFEST"] = str(self.manifest)

    def begin_cleanup(self, key="osv", *, fault=None, persistence_fault=None):
        self.stop()
        source = self.sources[key]
        plan, receipt = self.export("cleanup-" + key, [self.action("cleanup-" + key,
            "delete_action_outputs", {"output_ids": [str(source["id"])]})])
        self.manifest_for(receipt["request_id"], fault, output_id=source["id"])
        if persistence_fault:
            self.env["CAMCTL_CLEANUP_PERSISTENCE_FAULT"] = persistence_fault
            launcher = self.launcher("fault-cli", self.tools / "_cleanup_roundtrip_fault.py")
        else:
            launcher = None
        self.start(launcher)
        self.host.submit(plan)
        return receipt["request_id"]

    def wait_marker(self, stage=None):
        def observed():
            if not self.marker.exists():
                return None
            try:
                result = json.loads(self.marker.read_text())
            except json.JSONDecodeError:
                return None
            if stage is not None:
                assert result["stage"] == stage, result
            return result
        return wait(observed, "预期真实故障窗口未形成", 30)

    def cleanup_item(self, request):
        item, = rows(self.db, "SELECT c.* FROM cleanup_items c JOIN actions a ON a.id=c.action_id "
                     "JOIN plans p ON p.id=a.plan_id WHERE p.request_id=?", (request,))
        return item

    def attempts(self, item_id):
        return rows(self.db, "SELECT t.*,r.kind,r.responsibility_key FROM operation_attempts t "
                    "JOIN operation_runs r ON r.id=t.run_id WHERE r.cleanup_item_id=? ORDER BY t.id", (item_id,))

    def event(self, event_id):
        event, = rows(self.db, "SELECT e.*,t.operation_key FROM history_events e "
                     "JOIN history_transactions t ON t.id=e.transaction_id WHERE e.id=?", (event_id,))
        return event

    def recover(self, request, expected, *, query_unknown=False, **configuration):
        self.stop()
        self.write_config(**configuration)
        self.manifest_for(request, query_unknown=query_unknown, output_id=self.sources["osv"]["id"])
        self.env.pop("CAMCTL_CLEANUP_PERSISTENCE_FAULT", None)
        self.start()
        self.wait_action(request, expected)
        return self.cleanup_item(request)

    def assert_terminal_report(self, request, expected, error_code="file_delete_failed"):
        snapshot = self.assert_report_ack_and_history()
        action, = rows(self.db, "SELECT a.* FROM actions a JOIN plans p ON p.id=a.plan_id WHERE p.request_id=?", (request,))
        reported = next(a for plan in snapshot["plans"] for a in plan.get("actions", [])
                        if a["action_instance_id"] == str(action["id"]))
        assert reported["status"] == expected.lower()
        item = self.cleanup_item(request)
        assert reported["result"]["items"][0]["output_id"] == str(item["output_id"])
        assert reported["result"]["items"][0]["status"] == enum_for("cleanup_items.status")(item["status"]).name.lower()
        if item["error_code"] is not None:
            assert reported["result"]["items"][0]["error"]["code"] == error_code

    def clean_exact(self, key):
        before = {name: self.path(name).read_bytes() for name in self.sources
                  if not name.startswith("hb-device") and self.path(name).exists()}
        source = self.sources[key]
        outputs_before = {row["id"]: row for row in rows(self.db, "SELECT * FROM outputs")}
        self.stop()
        plan, receipt = self.export("clean-" + key, [self.action("clean-" + key, "delete_action_outputs", {"output_ids": [str(source["id"])]})])
        self.manifest_for(receipt["request_id"])
        self.start()
        self.host.submit(plan)
        self.wait_action(receipt["request_id"], "SUCCEEDED")
        assert not self.path(key).exists()
        for name, content in before.items():
            if name != key:
                assert self.path(name).read_bytes() == content
        outputs_after = {row["id"]: row for row in rows(self.db, "SELECT * FROM outputs")}
        assert outputs_after.keys() == outputs_before.keys()
        for output_id, original in outputs_before.items():
            if output_id != source["id"]:
                assert outputs_after[output_id] == original
        assert outputs_after[source["id"]]["availability"] == int(enum_for("outputs.availability").CLEANED)
        self.assert_report_ack_and_history()

    def publish_copies(self):
        for key in ("repaired", "osv", "lrf", "hb-original", "hb-preview"):
            plan, receipt = self.export("obtain-" + key, [self.action("obtain-" + key, "obtain_action_outputs", {"source": {"action_instance_id": str(self.sources[key]["source_action_id"])}, "output_ids": [str(self.sources[key]["id"])]})])
            self.host.submit(plan)
            self.wait_action(receipt["request_id"], "SUCCEEDED")
        self.assert_report_ack_and_history()
        self.copies = {path.name: path.read_bytes() for path in self.deployment.processing.iterdir() if not path.name.startswith("status-report-")}
        assert len(self.copies) == 5

    def assert_report_ack_and_history(self):
        self.stop()
        self.start()
        self.wait_normal_run()
        published = rows(self.db, "SELECT * FROM reports WHERE status=?", (int(enum_for("reports.status").PUBLISHED),))
        assert published
        ready = {path.name: path.read_bytes() for path in self.deployment.ready.glob("status-report-*.json")}
        assert ready
        for name, content in ready.items():
            document = json.loads(content)
            report = next(row for row in published if row["id"] == int(document["report_id"]))
            assert (report["from_wm"], report["to_wm"], report["size_bytes"], report["sha256"]) == (document["from_wm"], document["to_wm"], len(content), sha(content))
            self.frozen[name] = content
        self.host.claim()
        for name, content in ready.items():
            assert (self.deployment.processing / name).read_bytes() == content
        receipt = self.deployment.import_reports_with_client(self.deployment.processing)
        client_db = self.root / "client-store/client.db"
        saved = rows(client_db, "SELECT * FROM reports")
        for row in saved:
            assert row["bytes"] == self.frozen[row["file_name"]]
        coverage, = rows(client_db, "SELECT value FROM records WHERE namespace='state' AND id='coverage'")
        assert json.loads(coverage["value"]) == receipt["coverage"]
        assert {json.loads(content)["report_id"] for content in ready.values()} <= set(receipt["saved_report_ids"])
        snapshot, = rows(client_db, "SELECT value FROM records WHERE namespace='state' AND id='snapshot'")
        imported = json.loads(snapshot["value"])
        plan, exported = self.export("ack-and-new-facts", [self.action("ack-and-new-facts", "report_status", {"scope": "full"})])
        assert exported["last_report_id"] == receipt["ack_id"]
        self.host.submit(plan)
        wait(lambda: rows(self.db, "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state") == [{"acknowledged_report_id": int(receipt["ack_id"]), "acknowledged_wm": receipt["coverage"]}], "累计 ACK 未吸收")
        self.wait_action(exported["request_id"], "SUCCEEDED")
        for name, content in self.frozen.items():
            assert (self.deployment.processing / name).read_bytes() == content
        for name, content in getattr(self, "copies", {}).items():
            assert (self.deployment.processing / name).read_bytes() == content
        for item in rows(self.db, "SELECT id,final_event_id FROM cleanup_items WHERE final_event_id IS NOT NULL"):
            old = self.members.setdefault(item["id"], item["final_event_id"])
            assert old == item["final_event_id"]
        history = rows(self.db, "SELECT * FROM history_events ORDER BY id")
        transactions = rows(self.db, "SELECT * FROM history_transactions ORDER BY id")
        if hasattr(self, "frozen_history"):
            assert history[:len(self.frozen_history)] == self.frozen_history
            assert transactions[:len(self.frozen_transactions)] == self.frozen_transactions
        self.frozen_history = history
        self.frozen_transactions = transactions
        for call in records(self.calls_path):
            if call.get("kind") == "cleanup" and call["stage"] == "observed":
                frame = base64.b64decode(call["frame_b64"])
                parts = frame.split(b"\0")
                assert len(parts) == 8 and parts[:3] == [b"CAMCTL-FILE/1", call["operation"].encode(), call["path"].encode()]
                assert parts[-2:] == [b"END", b""] and sha(frame) == call["frame_sha256"] and len(frame) == call["frame_size"]
                unknown = parts[3] == b"unknown"
                assert parts[4:6] == ([b"0", b"0"] if call["operation"] == "delete"
                                      else [b"-", b"1" if unknown else b"0"])
                assert call["modeled_unlink_count"] == (1 if call["operation"] == "delete" else 0)
                assert call["modeled_observation_count"] == 1
                if not unknown:
                    assert parts[3] == (b"1" if call["path"] in call["after"] else b"0")
                unchanged = {k: v for k, v in call["before"].items() if k != call["path"]}
                assert {k: v for k, v in call["after"].items() if k != call["path"]} == unchanged
        json_write(self.root / f"report-receipt-{len(self.frozen_history)}.json", {
            "import": receipt, "export": exported, "report_files": list(ready),
            "published": published, "snapshot": imported})
        return imported


def _hb_tool(mode, photo_params=None):
    if photo_params is None:
        photo_params = _hasselblad_photo_sample_params()
    from camctl.devices.drivers.hasselblad.parameters import PHOTO_COMMANDS as C, PHOTO_FIELDS

    set_values = {
        C.DRIVE_MODE_FIELD.value: C.DRIVE_MODE_SINGLE.value,
        C.CAPTURE_FORMAT_FIELD.value: photo_params["capture_format"],
        C.EXPOSURE_MODE_FIELD.value: photo_params["exposure"]["mode"],
        PHOTO_FIELDS["iso"].command_field: str(photo_params["exposure"]["iso"]),
        C.WHITE_BALANCE_MODE_FIELD.value: photo_params["white_balance"]["mode"],
        C.FOCUS_MODE_FIELD.value: photo_params["focus"]["mode"],
    }
    return f'''import sys,json,pathlib,os,time
args=sys.argv[1:]
log=pathlib.Path(__file__).with_suffix(".jsonl")
settings_state=pathlib.Path(__file__).with_suffix(".settings")
set_values={set_values!r}
with log.open("a") as stream: stream.write(json.dumps({{"argv":sys.argv,"pid":os.getpid(),"ppid":os.getppid(),"pgid":os.getpgrp(),"utc_ns":time.time_ns()}})+"\\n")
if args==["--pid","0x000a","list"]: print("vid=0x2756 pid=0x000a  bus=  1 addr= 23")
elif args==["--pid","0x000a","info"]: print("serial : R6-HB-{mode}\\nfirmware : v4.1.0")
elif len(args)==5 and args[:3]==["--pid","0x000a","set"]:
 field,value=args[3:]
 if set_values.get(field)!=value: raise ValueError("unexpected Hasselblad set field or value")
 settings=json.loads(settings_state.read_text()) if settings_state.is_file() else {{}}
 settings[field]=value
 settings_state.write_text(json.dumps(settings))
elif len(args)==5 and args[:4]==["--pid","0x000a","capture","--output"]:
 work=pathlib.Path(args[4]); assert work.is_absolute()
 if not settings_state.is_file(): raise ValueError("capture settings were not set")
 settings=json.loads(settings_state.read_text())
 if settings!=set_values: raise ValueError("capture settings do not match the RAW sample")
 original=work/"host-copy.3FR"; preview=work/"host-copy.3FR.thumbnail.jpg"
 original.write_bytes({RAW!r}); preview.write_bytes({PREVIEW!r})
 camera_path="/CAMERA/DCIM/100HASSELBL/camera-copy.3FR"
 print("capture: OK"); print(f"saved {{original}} ({{original.stat().st_size}} bytes)"); print(f"saved {{preview}} ({{preview.stat().st_size}} bytes)"); print("camera file: " + camera_path)
else: raise ValueError("unexpected hb_cli argv")
'''
