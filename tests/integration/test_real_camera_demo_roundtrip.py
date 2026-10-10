"""四条双相机软件演示使用实际 C host、CLI、状态库及客户端消费者。"""

import hashlib
import json
from pathlib import Path

import pytest

from camctl.contracts.enums import enum_for
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from _wsl_host_demo import HostDemo, query_state, wait_for, write_stub_launcher
from camctl_fixtures import Deployment
from camera_demo_fixtures import advance_clock, camera_spec, recording_params, schedule, timelapse_params
from test_camctl_c_module_roundtrip import _report_with_statuses


def _actions(report):
    return {action["name"]: action for plan in report["plans"] for action in plan["actions"]}


def _started(deployment, name):
    rows = query_state(deployment.state_db,
        "SELECT v.action_id,v.started_at,v.sent_at,v.baseline_first_event_id,v.baseline_last_event_id"
        " FROM device_activities v JOIN actions a ON a.id=v.action_id"
        " JOIN operation_runs r ON r.action_id=a.id AND r.kind=?"
        " JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE a.name=? AND t.status=? AND t.result_event_id IS NOT NULL",
        (int(enum_for("operation_runs.kind").START), name, int(enum_for("operation_attempts.status").SUCCEEDED)))
    return rows[0] if rows and (rows[0][1] is not None or rows[0][2] is not None) else None


def _body(name, actions):
    return {"name": name, "actions": actions}


def _capture(params, clock, *, name="capture"):
    return {"name": name, "type": "camera_record" if params["type"].endswith("_record") else "camera_timelapse",
            "device_id": "camera", "scheduled_at": schedule(clock), "params": params,
            "policy": {"max_delay_ms": 30000}}


def _idle(demo, previous=0):
    return wait_for(lambda: _completed_runs(demo) > previous, 60, "本次 run 未完成回收")


def _completed_runs(demo):
    return sum("command=run " in line and " result=success " in line and " needs_run=0 " in line
               for line in demo.module_log().splitlines())


def _reading(deployment, demo):
    assert '"reason" : "state_db_error"' not in demo.module_log(), demo.module_log()[-3000:]
    failed = query_state(deployment.state_db, "SELECT error_code,error_details_json FROM actions WHERE name='obtain' AND status=?",
                         (int(enum_for("actions.status").FAILED),))
    assert not failed, failed
    return (deployment.gates / "read-started").exists()


@pytest.mark.parametrize("model", list(CameraModel))
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
def test_camera_demo_capture_obtain_and_report(tmp_path: Path, host_demo: str, model, action_type):
    deployment = Deployment(tmp_path, devices=False)
    params = recording_params(model) if action_type == "camera_record" else timelapse_params(model)
    spec = camera_spec(deployment, model, params)
    clock = Path(spec["clock_path"])
    result = deployment.camctl("init", "--config", str(deployment.config_path))
    assert result.exit_code == 0, result.stderr
    deployment.install_client_capabilities(spec)
    capture, _ = deployment.export_plan_with_client(_body("camera-demo", [_capture(params, clock)]))
    demo = HostDemo(deployment, host_demo, launcher=write_stub_launcher(deployment))
    try:
        demo.start()
        demo.submit(capture)
        started = wait_for(lambda: _started(deployment, "capture"), 60, "原 START/SENT 未可靠保存")
        assert started[3] is not None and started[4] is not None
        if action_type == "camera_timelapse":
            # 跨进程恢复使用原发送和原基准，不重新设置或启动。
            demo.stop()
            advance_clock(clock, params["duration_s"] + 2)
            demo.start()
            demo.submit(capture)
        else:
            advance_clock(clock, params["duration_s"])
        capture_report = wait_for(lambda: _report_with_statuses(deployment.ready, {"capture": "succeeded"}),
                                  60, "capture 未成功或最终报告未发布")
        _idle(demo)
        assert _started(deployment, "capture")[3:] == started[3:]
        action_id = _actions(capture_report)["capture"]["action_instance_id"]
        assert action_id == str(started[0])
        rows = query_state(deployment.state_db,
            "SELECT f.locator_json,f.size_bytes FROM outputs o JOIN device_files f ON f.id=o.device_file_id"
            " WHERE o.source_action_id=?", (int(action_id),))
        assert len(rows) == 1
        remote = json.loads(rows[0][0])["path"]
        assert "/new-1/" in remote and "old.mp4" not in remote
        source = Path(spec["remote_root"]) / remote.lstrip("/")
        content = source.read_bytes()
        assert rows[0][1] == len(content)
        demo.claim()
        saved = deployment.import_reports_with_client(deployment.processing)
        assert saved["saved_report_ids"] == [capture_report["report_id"]]

        # 取回首拍期间另起同设备十秒录像；长读取不能挡住停止或报告。
        (deployment.gates / "hold-read").touch()
        obtain_body = _body("camera-demo-obtain", [
            _capture(recording_params(model), clock, name="next-record"),
            {"name": "obtain", "type": "obtain_action_outputs", "scheduled_at": schedule(clock),
             "params": {"source": {"action_instance_id": action_id}}},
        ])
        obtain, receipt = deployment.export_plan_with_client(obtain_body)
        assert receipt["last_report_id"] == capture_report["report_id"]
        previous = _completed_runs(demo)
        demo.submit(obtain)
        wait_for(lambda: _reading(deployment, demo), 60, "原片读取未开始")
        wait_for(lambda: _started(deployment, "next-record"), 60, "后续录像未开始")
        advance_clock(clock, 10)
        wait_for(lambda: _report_with_statuses(deployment.ready, {"next-record": "succeeded", "obtain": "running"}),
                 45, "长读取期间到期停止及报告未独立推进")
        assert query_state(deployment.state_db,
            "SELECT t.status,t.result_event_id FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.copy_id IS NOT NULL") == [(int(enum_for("operation_attempts.status").RUNNING), None)]
        (deployment.gates / "release-read").touch()
        report = wait_for(lambda: _report_with_statuses(deployment.ready, {"obtain": "succeeded", "next-record": "succeeded"}),
                          60, "独立 obtain 未成功")
        _idle(demo, previous)
        assert query_state(deployment.state_db,
            "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state") == [(int(capture_report["report_id"]), capture_report["to_wm"])]
        copy = query_state(deployment.state_db,
            "SELECT source_size,source_sha256,committed_bytes,target_sha256 FROM file_copies")[0]
        expected_sha = hashlib.sha256(content).hexdigest()
        assert copy == (len(content), expected_sha, len(content), expected_sha)
        file_name = query_state(deployment.state_db, "SELECT file_name FROM deliveries")[0][0]
        assert (deployment.ready / file_name).read_bytes() == content
        demo.claim()
        claimed = deployment.processing / file_name
        assert claimed.stat().st_size == len(content)
        assert hashlib.sha256(claimed.read_bytes()).hexdigest() == expected_sha
        assert source.read_bytes() == content
        delivery = _actions(report)["obtain"]["deliveries"][0]
        assert delivery["source_action_instance_id"] == action_id
        assert (delivery["file_name"], delivery["size"], delivery["sha256"]) == (file_name, len(content), expected_sha)
        outputs = query_state(deployment.state_db,
            "SELECT o.source_action_id,f.locator_json FROM outputs o JOIN device_files f ON f.id=o.device_file_id ORDER BY o.id")
        assert len(outputs) == 2 and outputs[0][0] != outputs[1][0]
        assert "/new-2/" in json.loads(outputs[1][1])["path"]
        saved = deployment.import_reports_with_client(deployment.processing)
        assert report["report_id"] in saved["saved_report_ids"]
        ack, receipt = deployment.export_plan_with_client(_body("camera-demo-ack", [
            {"name": "ack", "type": "report_status", "scheduled_at": schedule(clock), "params": {"scope": "full"}}]))
        assert receipt["last_report_id"] == report["report_id"]
        demo.submit(ack)
        wait_for(lambda: query_state(deployment.state_db,
            "SELECT acknowledged_report_id,acknowledged_wm FROM runtime_state") == [(int(report["report_id"]), report["to_wm"])], 60, "取回报告 ACK 未吸收")
        calls = [json.loads(line) for line in (deployment.gates / "camera-calls.jsonl").read_text().splitlines()]
        assert sum(call["kind"] == "start" for call in calls) == 2
        assert sum(call["kind"] == "stop" for call in calls) == (2 if action_type == "camera_record" else 1)
        assert (Path(spec["remote_root"]) / "mnt/media_rw/emulated/DCIM/old.mp4").read_bytes() == b"old camera source"
    finally:
        (deployment.gates / "release-read").touch()
        demo.stop()
