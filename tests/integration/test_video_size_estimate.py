"""视频估算的真实 describe、客户端、执行与报告交接。

设备端采用同源虚构驱动；参考码率和估算值不表示硬件测量。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest

from camctl_fixtures import Deployment, future_schedule, stub_driver_spec, video_file


def _action(action_type="camera_record", params=None):
    return {
        "name": "估算交接",
        "type": action_type,
        "device_id": "cam-1",
        "scheduled_at": future_schedule(3),
        "policy": {"max_delay_ms": 5000},
        "params": params or {"type": "video"},
    }


def _estimate(deployment, action, *, replacement=None, action_text=None):
    action_file = deployment.root / "estimate-action.json"
    action_file.write_text(action_text or json.dumps(action), encoding="utf-8")
    arguments = ["estimate", str(deployment.root / "client-store"), str(action_file)]
    if replacement is not None:
        arguments[0] = "reload-estimate"
        arguments.append(str(replacement))
    return deployment.run_client_driver(
        *arguments, output=deployment.root / "estimate-receipt.json")


def _execute_and_import(deployment, driver, action):
    initialized = deployment.camctl("init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    plan, receipt = deployment.export_plan_with_client({
        "name": "估算与文件事实", "actions": [action],
    })
    body = json.loads(plan.read_text(encoding="utf-8"))
    assert body["actions"][0] == action
    submitted = deployment.camctl(
        "submit", str(plan), "--config", str(deployment.config_path), driver=driver)
    assert submitted.exit_code == 0, submitted.stderr
    assert submitted.message() == {"kind": "succeeded", "body": {"needs_run": True}}
    executed = deployment.camctl("run", "--config", str(deployment.config_path), driver=driver)
    assert executed.exit_code == 0, executed.stderr
    assert executed.message() == {"kind": "succeeded"}
    with closing(sqlite3.connect(deployment.state_db)) as connection:
        effective, status = connection.execute(
            "SELECT effective_params_json, status FROM actions").fetchone()
        assert json.loads(effective) == action["params"]
        assert status == 3
    reports = list(deployment.ready.glob("status-report-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert "video_size_estimate" not in reports[0].read_text(encoding="utf-8")
    recorded = report["plans"][0]["actions"][0]
    assert recorded["status"] == "succeeded"
    assert recorded["effective_params"] == action["params"]
    assert recorded["outputs"][0]["size"] == 8192
    imported = deployment.import_reports_with_client(deployment.ready)
    assert imported["saved_report_ids"] == [report["report_id"]]
    assert imported["ack_id"] == report["report_id"]
    assert receipt["has_ack"] is False


def test_record_estimate_stays_out_of_execution_and_report(tmp_path):
    deployment = Deployment(tmp_path)
    driver = stub_driver_spec({"1": [video_file("record-1")]})
    driver["video_size_estimate_json"] = {"camera_record": json.dumps({
        "bitrate_mbps": 130,
        "duration": {"method": "direct", "seconds": {"source": "constant", "value": 1}},
    })}
    deployment.install_client_capabilities(driver)
    action = _action()
    result = _estimate(deployment, action)
    expected = {"kind": "ready", "sizeBytes": 16_250_000,
                "playbackSeconds": 1, "bitrateMbps": 130}
    assert result["estimate"] == expected
    assert result["http_estimate"] == expected
    assert result["drafts_before"] == result["drafts_after"]
    assert len(result["drafts_before"]) == 1
    _execute_and_import(deployment, driver, action)


def _schema(parameter_type, properties, required):
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "properties": {
                "type": {"const": parameter_type}, **properties},
            "required": ["type", *required], "additionalProperties": False}


def test_timelapse_estimate_uses_same_capture_duration_as_execution(tmp_path):
    deployment = Deployment(tmp_path)
    driver = stub_driver_spec({"1": [video_file("timelapse-1")]})
    driver["parameter_schema_json"] = {"camera_timelapse": json.dumps(_schema(
        "timelapse", {"capture_duration_s": {"type": "integer", "minimum": 1},
                      "interval_s": {"type": "integer", "minimum": 1}},
        ["capture_duration_s", "interval_s"]))}
    driver["video_size_estimate_json"] = {"camera_timelapse": json.dumps({
        "bitrate_mbps": 175,
        "duration": {"method": "timelapse_interval",
                     "capture_seconds": {"source": "parameter", "path": "/capture_duration_s"},
                     "interval_seconds": {"source": "parameter", "path": "/interval_s"},
                     "playback_fps": {"source": "constant", "value": 30}},
    })}
    deployment.install_client_capabilities(driver)
    action = _action("camera_timelapse", {
        "type": "timelapse", "capture_duration_s": 3, "interval_s": 1})
    result = _estimate(deployment, action)
    expected = {"kind": "ready", "sizeBytes": 2_187_500,
                "playbackSeconds": 0.1, "bitrateMbps": 175}
    assert result["estimate"] == result["http_estimate"] == expected
    _execute_and_import(deployment, driver, action)
    with closing(sqlite3.connect(deployment.state_db)) as connection:
        # 参数3秒进入执行定义；估算成片0.1秒不替换采集时长。
        execution = json.loads(connection.execute(
            "SELECT execution_spec_json FROM actions").fetchone()[0])
        assert execution["target_duration_ms"] == 3000


@pytest.mark.parametrize("declared, params, reason", [
    (False, {"type": "video"}, "not_provided"),
    (True, {"type": "video", "bitrate_mode": "high"}, "lookup_missing"),
    (True, {"type": "video"}, "value_missing"),
])
def test_valid_unavailable_estimate_does_not_prevent_plan_export(tmp_path, declared, params, reason):
    deployment = Deployment(tmp_path)
    driver = stub_driver_spec({})
    if declared:
        driver["parameter_schema_json"] = {"camera_record": json.dumps(_schema(
            "video", {"bitrate_mode": {"type": "string", "enum": ["standard", "high"],
                                       "default": "standard"}}, []))}
        driver["video_size_estimate_json"] = {"camera_record": json.dumps({
            "bitrate_mbps": {"by": "/bitrate_mode", "values": {"standard": 95}},
            "duration": {"method": "direct", "seconds": {"source": "constant", "value": 1}},
        })}
    deployment.install_client_capabilities(driver)
    action = _action(params=params)
    result = _estimate(deployment, action)
    assert result["estimate"]["kind"] == "unavailable"
    assert result["estimate"]["reason"] == reason
    assert result["http_estimate"] == result["estimate"]
    plan, _ = deployment.export_plan_with_client({"name": "无有效估算仍合法", "actions": [action]})
    assert json.loads(plan.read_text(encoding="utf-8"))["actions"][0] == action


@pytest.mark.parametrize("frames", ["240", "240.0", "2.4e2"])
def test_exact_frame_constants_survive_describe_and_http(tmp_path, frames):
    deployment = Deployment(tmp_path)
    driver = stub_driver_spec({})
    driver["video_size_estimate_json"] = {"camera_record": (
        '{"bitrate_mbps":130.125,"duration":{"method":"timelapse_frames",'
        '"frames":{"source":"constant","value":' + frames + '},'
        '"playback_fps":{"source":"constant","value":30}}}')}
    deployment.install_client_capabilities(driver)
    described = json.loads((tmp_path / "client-store" / "device-capabilities.json").read_text(
        encoding="utf-8"), parse_float=Decimal)
    record = next(action for action in described["devices"][0]["actions"]
                  if action["type"] == "camera_record")
    assert record["parameter_types"][0]["video_size_estimate"]["bitrate_mbps"] == Decimal("130.125")
    result = _estimate(deployment, _action())
    expected = {"kind": "ready", "sizeBytes": 130_125_000,
                "playbackSeconds": 8, "bitrateMbps": 130.125}
    assert result["estimate"] == result["http_estimate"] == expected


@pytest.mark.parametrize("text", [
    '{}',
    '{"bitrate_mbps":0,"duration":{"method":"direct","seconds":{"source":"constant","value":1}}}',
    '{"bitrate_mbps":130,"duration":{"method":"timelapse_frames","frames":{"source":"constant","value":1.00000000000000000001},"playback_fps":{"source":"constant","value":30}}}',
])
def test_invalid_source_fails_cli_and_client_reload_keeps_enabled_definition(tmp_path, text):
    deployment = Deployment(tmp_path)
    driver = stub_driver_spec({})
    driver["video_size_estimate_json"] = {"camera_record": json.dumps({
        "bitrate_mbps": 130,
        "duration": {"method": "direct", "seconds": {"source": "constant", "value": 1}},
    })}
    deployment.install_client_capabilities(driver)
    invalid_driver = {**driver, "video_size_estimate_json": {"camera_record": text}}
    result = deployment.camctl("describe", "--config", str(deployment.config_path), driver=invalid_driver)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "video_size_estimate" in result.stderr
    # CLI 没有发布非法文件；独立将同一非法声明写入待加载文件，验证客户端整份失败。
    document = json.loads((tmp_path / "client-store" / "device-capabilities.json").read_text(encoding="utf-8"))
    record = next(item for item in document["devices"][0]["actions"] if item["type"] == "camera_record")
    record["parameter_types"][0]["video_size_estimate"] = "ESTIMATE_TEXT"
    replacement = tmp_path / "invalid-capabilities.json"
    replacement.write_text(json.dumps(document).replace('"ESTIMATE_TEXT"', text), encoding="utf-8")
    receipt = _estimate(deployment, _action(), replacement=replacement)
    assert receipt["load_error"]
    assert receipt["active_unchanged"] is True
    assert receipt["estimate"] == receipt["http_estimate"] == receipt["initial"]
    assert receipt["estimate"]["sizeBytes"] == 16_250_000
    assert receipt["drafts_before"] == receipt["drafts_after"]
    assert len(receipt["drafts_before"]) == 1


def test_fractional_frame_parameter_is_not_rounded_by_http_or_plan_export(tmp_path):
    deployment = Deployment(tmp_path)
    driver = stub_driver_spec({})
    driver["parameter_schema_json"] = {"camera_record": json.dumps(_schema(
        "video", {"frames": {"type": "number", "exclusiveMinimum": 0}}, ["frames"]))}
    driver["video_size_estimate_json"] = {"camera_record": json.dumps({
        "bitrate_mbps": 130,
        "duration": {"method": "timelapse_frames",
                     "frames": {"source": "parameter", "path": "/frames"},
                     "playback_fps": {"source": "constant", "value": 30}},
    })}
    deployment.install_client_capabilities(driver)
    action = _action(params={"type": "video", "frames": "FRAMES_TEXT"})
    action_text = json.dumps(action).replace('"FRAMES_TEXT"', '1.00000000000000000001')
    receipt = _estimate(deployment, action, action_text=action_text)
    assert receipt["estimate"] == receipt["http_estimate"] == {
        "kind": "unavailable", "reason": "value_invalid", "path": "/frames"}
    raw_receipt = json.loads((tmp_path / "estimate-receipt.json").read_text(
        encoding="utf-8"), parse_float=Decimal)
    assert raw_receipt["http_action"]["params"]["frames"] == Decimal("1.00000000000000000001")
    body = tmp_path / "precise-plan-body.json"
    body.write_text('{"name":"精确参数交接","actions":[' + action_text + ']}', encoding="utf-8")
    plan = tmp_path / "precise-plan.json"
    deployment.run_client_driver(
        "export", str(tmp_path / "client-store"), str(body), str(plan),
        output=tmp_path / "precise-export.json")
    parsed = json.loads(plan.read_text(encoding="utf-8"), parse_float=Decimal)
    assert parsed["actions"][0]["params"]["frames"] == Decimal("1.00000000000000000001")
