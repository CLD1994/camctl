"""安装版 Action6 有限延时经客户端、C host、恢复、取回与精确清理。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import time

import pytest

from _action6_builtin_adb import NEW_VIDEO, NEW_PREVIEW, NEW_PHOTO
from _osmo_builtin_clock import format_utc, read_clock, set_frozen, set_running, whole_second_utc_us
from _wsl_host_demo import HostDemo, query_state, wait_for
from camctl.contracts.enums import enum_for
from camctl_fixtures import Deployment
from test_camctl_c_module_roundtrip import _report_with_statuses
from test_osmo_builtin_timelapse_roundtrip import (
    osmo_package, _run, _client_driver, _wait_for_run_process, _wait_for_boundary_reads,
    _assert_recovery_boundary_empty, _records, _action,
)

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent


def _tools(package, deployment, params):
    tools = deployment.root / "tools"
    tools.mkdir()
    for name in ("_osmo_builtin_clock.py", "_osmo_builtin_clock_entry.py", "_action6_builtin_adb.py"):
        shutil.copyfile(ROOT / name, tools / name)
    for command, helper in (("adb", "_action6_builtin_adb.py"), ("camctl", "_osmo_builtin_clock_entry.py")):
        path = tools / command
        path.write_text("#!/bin/sh\nexec " + shlex.quote(str(package.python)) + " "
            + shlex.quote(str(tools / helper)) + ' "$@"\n')
        path.chmod(0o755)
    clock = deployment.root / "clock.json"
    set_frozen(clock, utc_us=whole_second_utc_us(), monotonic_ns=time.monotonic_ns())
    remote = deployment.root / "camera-files"
    (remote / "mnt/media_rw/emulated/DCIM").mkdir(parents=True)
    content = b"Action6 synthetic MP4 original"
    (deployment.root / "sample.mp4").write_bytes(content)
    environment = dict(package.environment)
    environment.update({
        "PATH": str(tools) + os.pathsep + environment.get("PATH", ""),
        "CAMCTL_ACTION6_ADB_ROOT": str(remote),
        "CAMCTL_ACTION6_TEST_CAPTURE": "timelapse",
        "CAMCTL_ACTION6_TEST_PARAMS": json.dumps(params),
        "CAMCTL_OSMO_TEST_CLOCK_FILE": str(clock),
        "CAMCTL_OSMO_TEST_CLOCK_LOG": str(deployment.root / "clock-reads.jsonl"),
        "CAMCTL_OSMO_TEST_PROCESS_LOG": str(deployment.root / "cli-processes.jsonl"),
    })
    return environment, tools / "camctl", clock, remote, content


@pytest.mark.skipif(os.name != "posix", reason="安装版 POSIX C host 联合链")
def test_installed_action6_timelapse_restores_natural_deadline_obtains_and_deletes_exact_outputs(
        tmp_path, host_demo, osmo_package, monkeypatch):
    package = osmo_package
    deployment = Deployment(tmp_path / "action6-timelapse", devices=False)
    extract = _run([str(package.python), "-c", (
        "import json; from camctl.resources import resource_bytes; "
        "print(json.dumps({name:resource_bytes('examples/camera-demo/'+name).decode() "
        "for name in ('config.toml','action6-timelapse.json')}))")],
        cwd=deployment.root, env=package.environment, log_dir=package.log_dir)
    resources = json.loads(extract.stdout)
    for name, data in resources.items():
        assert data.encode() == (REPO / "docs/camctl/examples/camera-demo" / name).read_bytes()
    sample = json.loads(resources["action6-timelapse.json"])
    # 使用 wheel 样例的合法间隔、完整 300 秒目标及曝光；代表性照片输出单独选择。
    params = {**sample["actions"][0]["params"], "outputs": "video_jpeg"}
    assert params["interval_s"] == 1 and params["duration_s"] == 300
    with deployment.config_path.open("a") as stream:
        stream.write("\n" + resources["config.toml"].replace("REPLACE_ACTION6_SERIAL", "action6-test-serial"))
    environment, camctl, clock, remote, content = _tools(package, deployment, params)
    initial = read_clock(clock)
    adb_log = deployment.root / "adb-calls.jsonl"
    process_log = deployment.root / "cli-processes.jsonl"
    clock_log = deployment.root / "clock-reads.jsonl"
    for command in ("init", "describe"):
        result = _run([str(camctl), command, "--config", str(deployment.config_path)],
            cwd=deployment.root, env=environment, log_dir=package.log_dir)
    described = json.loads(result.stdout)
    source_env = dict(package.environment, PYTHONPATH=str(REPO / "apps/camctl/src"))
    source = _run([str(REPO / "apps/camctl/.venv311/bin/python"), "-m", "camctl", "describe",
        "--config", str(deployment.config_path)], cwd=deployment.root, env=source_env, log_dir=package.log_dir)
    assert json.loads(source.stdout) == described
    device = described["devices"][0]
    assert device["delete_supported"] is True
    assert {p["type"] for a in device["actions"] for p in a["parameter_types"]} == {"action6_record", "action6_timelapse"}
    assert all(p["preview_supported"] for a in device["actions"] for p in a["parameter_types"])
    store = deployment.root / "client-store"
    store.mkdir()
    (store / "device-capabilities.json").write_text(result.stdout)
    deployment.run_client_driver = lambda *args, output: _client_driver(package, *args, output=output)
    body = {"name": "Action6有限延时", "actions": [{"name": "capture", "type": "camera_timelapse",
        "device_id": "action6", "scheduled_at": format_utc(initial["utc_us"]), "params": params,
        "policy": {"max_delay_ms": 30000}}]}
    plan, _ = deployment.export_plan_with_client(body)
    assert json.loads(plan.read_text())["actions"][0]["params"] == params
    demo = HostDemo(deployment, host_demo, launcher=str(camctl))
    monkeypatch.chdir(package.root)
    try:
        demo.start(env=environment)
        demo.submit(plan)
        starts = wait_for(lambda: query_state(deployment.state_db,
            "SELECT a.id,a.effective_params_json,a.execution_spec_json,t.result_json,v.id,v.sent_at,v.started_at"
            " FROM actions a JOIN operation_runs r ON r.action_id=a.id JOIN operation_attempts t ON t.run_id=r.id"
            " JOIN device_activities v ON v.action_id=a.id WHERE r.kind=1 AND t.result_json IS NOT NULL"),
            90, "Action6 START 没有可靠保存")
        assert len(starts) == 1
        action_id, effective, execution, start_json, activity_id, sent, started = starts[0]
        assert json.loads(effective) == params and sent == initial["utc_us"] and started is None
        spec = json.loads(execution)
        assert {key: spec[key] for key in ("target_duration_ms", "startup_margin_ms", "result_wait_margin_ms", "file_completion_wait_ms")} == {
            "target_duration_ms": 300000, "startup_margin_ms": 0, "result_wait_margin_ms": 30000, "file_completion_wait_ms": 30000}
        assert [o["type"] for o in json.loads(start_json)["observations"]] == ["timelapse_sent"]
        assert query_state(deployment.state_db, "SELECT id FROM outputs") == []
        demo.stop()
        negative = set_frozen(clock, utc_us=initial["utc_us"] + 329_000_000,
            monotonic_ns=initial["monotonic_ns"] + 329_000_000_000)
        previous_pids = {r["pid"] for r in _records(process_log)}
        demo = HostDemo(deployment, host_demo, launcher=str(camctl))
        demo.start(env=environment)
        demo.submit(plan)
        resumed = _wait_for_run_process(process_log, demo.log_path, previous_pids)
        _wait_for_boundary_reads(clock_log, pid=resumed["pid"], epoch=negative["epoch"],
            module="camctl.capture.handlers", function="_advance_timelapse_natural_wait", kind="monotonic_ns",
            utc_us=initial["utc_us"] + 329_000_000, monotonic_ns=initial["monotonic_ns"] + 329_000_000_000)
        _assert_recovery_boundary_empty(deployment, adb_log, action_id=action_id, activity_id=activity_id,
            recovery_started_ns=resumed["started_ns"])
        release = set_running(clock, utc_us=initial["utc_us"] + 330_000_000,
            monotonic_ns=initial["monotonic_ns"] + 330_000_000_000)
        report = wait_for(lambda: _report_with_statuses(deployment.ready, {"capture": "succeeded"}),
            90, "Action6 延时自然完成未成功")
        assert query_state(deployment.state_db, "SELECT effective_params_json,execution_spec_json FROM actions WHERE id=?", (action_id,)) == [(effective, execution)]
        calls = _records(adb_log)
        assert len([c for c in calls if c["kind"] == "start"]) == 1
        assert not [c for c in calls if c["kind"] == "stop"]
        preset = [c for c in calls if c.get("code") == "6c"]
        assert len(preset) == 1 and preset[0]["payload"] == "0400020a002c0100000000000000000000"
        assert not [c for c in calls if c.get("code") in {"26"} or c["script"].startswith("simulate_device ")]
        listing = [c for c in calls if c.get("file_kind") == "directory" and c.get("started_epoch") == release["epoch"]]
        assert listing and listing[0]["started_clock_ns"] >= initial["monotonic_ns"] + 330_000_000_000
        files = query_state(deployment.state_db,
            "SELECT f.id,json_extract(f.locator_json,'$.path'),f.role,f.original_device_file_id,o.id,o.kind,f.completion_evidence_json"
            " FROM device_files f JOIN outputs o ON o.device_file_id=f.id WHERE o.source_action_id=?", (action_id,))
        by_path = {row[1]: row for row in files}
        assert set(by_path) == {NEW_VIDEO, NEW_PREVIEW, NEW_PHOTO}
        assert by_path[NEW_PREVIEW][3] == by_path[NEW_VIDEO][0]
        assert by_path[NEW_PHOTO][3] is None
        assert by_path[NEW_PREVIEW][5] == int(enum_for("outputs.kind").PREVIEW)
        for row in files:
            evidence = json.loads(row[6])
            assert evidence["activity_id"] == activity_id
            assert evidence["wait_completed_event_id"] is not None
        demo.claim()
        deployment.import_reports_with_client(deployment.processing)
        expected_contents = {NEW_VIDEO: content, NEW_PREVIEW: b"Action6 auxiliary preview",
                             NEW_PHOTO: b"Action6 independent JPEG original"}
        for index, path in enumerate((NEW_VIDEO, NEW_PREVIEW, NEW_PHOTO)):
            name = f"obtain-{index}"
            obtain, _ = deployment.export_plan_with_client({"name": name, "actions": [{
                "name": name, "type": "obtain_action_outputs", "scheduled_at": format_utc(read_clock(clock)["utc_us"]),
                "params": {"source": {"action_instance_id": str(action_id)}, "output_ids": [str(by_path[path][4])]}}]})
            demo.submit(obtain)
            obtained = wait_for(lambda: _report_with_statuses(deployment.ready, {name: "succeeded"}), 90, name)
            delivery = _action(obtained, name)["deliveries"][0]
            demo.claim()
            assert (deployment.processing / delivery["file_name"]).read_bytes() == expected_contents[path]
            deployment.import_reports_with_client(deployment.processing)
        # 精确 output ID 删除 LRF，再删除 MP4；JPEG 和另一份配对文件分别保留。
        for index, path in enumerate((NEW_PREVIEW, NEW_VIDEO)):
            name = f"delete-{index}"
            cleanup, _ = deployment.export_plan_with_client({"name": name, "actions": [{
                "name": name, "type": "delete_action_outputs", "scheduled_at": format_utc(read_clock(clock)["utc_us"]),
                "params": {"output_ids": [str(by_path[path][4])]}}]})
            demo.submit(cleanup)
            deleted = wait_for(lambda: _report_with_statuses(deployment.ready, {name: "succeeded"}), 90, name)
            assert not (remote / path.lstrip("/")).exists()
            assert (remote / NEW_PHOTO.lstrip("/")).read_bytes() == expected_contents[NEW_PHOTO]
            if index == 0:
                assert (remote / NEW_VIDEO.lstrip("/")).read_bytes() == content
            demo.claim()
            deployment.import_reports_with_client(deployment.processing)
        assert len([c for c in _records(adb_log) if c.get("file_kind") == "delete"]) == 2
        assert len([c for c in _records(adb_log) if c["kind"] == "start"]) == 1
        ack, receipt = deployment.export_plan_with_client({"name": "ack", "actions": [{"name": "ack", "type": "report_status",
            "scheduled_at": format_utc(read_clock(clock)["utc_us"]), "params": {"scope": "full"}}]})
        assert receipt["last_report_id"] == deleted["report_id"]
        demo.submit(ack)
        wait_for(lambda: query_state(deployment.state_db, "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state") == [
            (int(deleted["report_id"]), deleted["to_wm"])], 90, "累计 ACK 未吸收")
    finally:
        demo.stop()
