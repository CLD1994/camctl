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
from camera_demo_fixtures import advance_clock, camera_spec, schedule
from _wsl_host_demo import query_state, wait_for


_CASES = [(preset, case) for preset in TIMELAPSE_PRESETS[CameraModel.ACTION6]
          for case in (("complete",) if preset.outputs == "video" else
                       ("complete", "missing_photo", "wrong_format"))]


@pytest.mark.parametrize("preset,product_case", _CASES,
                         ids=[f"{p.interval_s}-{p.duration_s}-{p.outputs}-{c}" for p, c in _CASES])
def test_action6_presets_keep_video_and_require_selected_photo(tmp_path, preset, product_case):
    deployment = Deployment(tmp_path, devices=False)
    params = {"type": "action6_timelapse", "interval_s": preset.interval_s,
              "duration_s": preset.duration_s, "outputs": str(preset.outputs),
              "exposure": {"mode": preset.exposure_mode,
                           **({"iso": 800} if preset.exposure_mode == "manual" else {})}}
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
        advance_clock(clock, preset.duration_s + 2)
        stdout, stderr = run.communicate(timeout=60)
        assert run.returncode == 0, (stdout, stderr)
        status, details, effective, execution = query_state(deployment.state_db,
            "SELECT status,error_details_json,effective_params_json,execution_spec_json FROM actions")[0]
        assert json.loads(effective) == params
        rules = json.loads(execution)["product_rules"]
        assert {(r["kind"], r["format_id"]) for r in rules} == (
            {("video", "mp4")} if preset.outputs == "video" else
            {("video", "mp4"), ("photo", "dng" if preset.outputs == "video_raw" else "jpeg")})
        if product_case == "complete":
            assert status == int(enum_for("actions.status").SUCCEEDED)
            assert details is None
        else:
            assert status == int(enum_for("actions.status").FAILED)
            assert json.loads(details)["reason"] == "invalid_outputs"
        outputs = query_state(deployment.state_db,
            "SELECT f.original_name,f.locator_json,f.size_bytes FROM outputs o JOIN device_files f ON f.id=o.device_file_id")
        formats = {Path(row[0]).suffix[1:] for row in outputs}
        assert "mp4" in formats
        if product_case == "complete" and preset.outputs != "video":
            assert formats == {"mp4", "dng" if preset.outputs == "video_raw" else "jpeg"}
        for _, locator, size in outputs:
            remote = json.loads(locator)["path"]
            assert "/new-1/" in remote
            assert (Path(spec["remote_root"]) / remote.lstrip("/")).stat().st_size == size
    except subprocess.TimeoutExpired:
        pytest.fail("原生延时收尾未在有限期限内完成")
    finally:
        if run.poll() is None:
            terminate_process_tree(run)
