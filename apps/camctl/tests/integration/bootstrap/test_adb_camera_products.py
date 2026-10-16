"""实际候选参数、任务工厂和正式产物收尾共同验证必要格式。"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from camctl.contracts.enums import enum_for
from camctl.devices.drivers.adb_cameras.commands import CameraModel, TIMELAPSE_PRESETS

_ROOT_INTEGRATION = Path(__file__).resolve().parents[5] / "tests" / "integration"
sys.path.insert(0, str(_ROOT_INTEGRATION))
from camctl_fixtures import Deployment, terminate_process_tree
from camera_demo_fixtures import advance_clock, camera_spec, schedule, timelapse_params
from _wsl_host_demo import query_state, wait_for


_VARIANTS = tuple(dict.fromkeys(
    (preset.outputs, preset.exposure_mode)
    for preset in TIMELAPSE_PRESETS[CameraModel.ACTION6]
))
_CASES = tuple(
    (outputs, exposure_mode, product_case)
    for outputs, exposure_mode in _VARIANTS
    for product_case in (("complete",) if outputs == "video" else
                         ("complete", "missing_photo", "wrong_format"))
)
_LEGACY_TIME_PRESETS = tuple(
    preset for preset in TIMELAPSE_PRESETS[CameraModel.ACTION6]
    if preset.duration_s in {5400, 6000} and preset.outputs == "video"
)


@pytest.mark.parametrize("output_selection,exposure_mode,product_case", _CASES,
                         ids=[f"{mode}-{outputs}-{case}"
                              for outputs, mode, case in _CASES])
def test_action6_candidates_keep_video_and_require_selected_photo(
        tmp_path, output_selection, exposure_mode, product_case):
    deployment = Deployment(tmp_path, devices=False)
    params = timelapse_params(CameraModel.ACTION6)
    params["outputs"] = str(output_selection)
    params["exposure"] = {"mode": exposure_mode,
                          **({"iso": 800} if exposure_mode == "manual" else {})}
    spec = camera_spec(deployment, CameraModel.ACTION6, params)
    spec["product_case"] = product_case
    clock = Path(spec["clock_path"])
    initialized = deployment.camctl("init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    plan = deployment.write_plan({"request_id": "1", "created_at": schedule(clock, 0),
        "name": "action6-products", "actions": [{"name": "capture", "type": "camera_timelapse",
        "device_id": "camera", "scheduled_at": schedule(clock), "params": params,
        "policy": {"max_delay_ms": 30000}}]})
    submitted = deployment.camctl("submit", str(plan), "--config", str(deployment.config_path), driver=spec)
    assert submitted.exit_code == 0, submitted.stderr
    run = deployment.start_camctl("run", "--config", str(deployment.config_path), driver=spec)
    try:
        wait_for(lambda: query_state(deployment.state_db,
            "SELECT sent_at FROM device_activities WHERE sent_at IS NOT NULL"), 30, "原生延时未发送")
        advance_clock(clock, params["duration_s"] + 2)
        stdout, stderr = run.communicate(timeout=60)
        assert run.returncode == 0, (stdout, stderr)
        status, details, effective, execution = query_state(deployment.state_db,
            "SELECT status,error_details_json,effective_params_json,execution_spec_json FROM actions")[0]
        assert json.loads(effective) == params
        rules = json.loads(execution)["product_rules"]
        assert {(r["kind"], r["format_id"]) for r in rules} == (
            {("video", "mp4")} if output_selection == "video" else
            {("video", "mp4"), ("photo", "dng" if output_selection == "video_raw" else "jpeg")})
        if product_case == "complete":
            assert status == int(enum_for("actions.status").SUCCEEDED)
            assert details is None
        else:
            assert status == int(enum_for("actions.status").FAILED)
            assert json.loads(details)["reason"] == "invalid_outputs"
        output_rows = query_state(deployment.state_db,
            "SELECT f.original_name,f.locator_json,f.size_bytes FROM outputs o JOIN device_files f ON f.id=o.device_file_id")
        formats = {Path(row[0]).suffix[1:] for row in output_rows}
        assert "mp4" in formats
        if product_case == "complete" and output_selection != "video":
            assert formats == {"mp4", "dng" if output_selection == "video_raw" else "jpeg"}
        for original_name, locator, size in output_rows:
            remote = json.loads(locator)["path"]
            assert remote == f"/mnt/media_rw/emulated/DCIM/new-1/{original_name}"
            assert (Path(spec["remote_root"]) / remote.lstrip("/")).stat().st_size == size
    except subprocess.TimeoutExpired:
        pytest.fail("原生延时收尾未在有限期限内完成")
    finally:
        if run.poll() is None:
            terminate_process_tree(run)

@pytest.mark.parametrize("preset", _LEGACY_TIME_PRESETS,
                         ids=[f"legacy-{preset.duration_s}-{preset.exposure_mode}"
                              for preset in _LEGACY_TIME_PRESETS])
def test_action6_legacy_timelapse_is_not_accepted_as_new_request(tmp_path, preset):
    deployment = Deployment(tmp_path, devices=False)
    params = {"type": "action6_timelapse", "interval_s": preset.interval_s,
              "duration_s": preset.duration_s, "outputs": str(preset.outputs),
              "exposure": {"mode": preset.exposure_mode,
                           **({"iso": 800} if preset.exposure_mode == "manual" else {})}}
    spec = camera_spec(deployment, CameraModel.ACTION6, params)
    clock = Path(spec["clock_path"])
    initialized = deployment.camctl("init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    plan = deployment.write_plan({"request_id": "1", "created_at": schedule(clock, 0),
        "name": "action6-legacy-timelapse", "actions": [{"name": "capture",
        "type": "camera_timelapse", "device_id": "camera", "scheduled_at": schedule(clock),
        "params": params, "policy": {"max_delay_ms": 30000}}]})
    submitted = deployment.camctl("submit", str(plan), "--config", str(deployment.config_path),
                                  driver=spec)
    assert submitted.exit_code == 0, submitted.stderr
    status = query_state(deployment.state_db, "SELECT status FROM actions WHERE name='capture'")
    assert status == [(int(enum_for("actions.status").FAILED),)]
    assert query_state(deployment.state_db, "SELECT id FROM device_activities") == []
    call_log = deployment.gates / "camera-calls.jsonl"
    assert not call_log.exists() or not call_log.read_text(encoding="utf-8").strip()
