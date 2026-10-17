"""同源时间资料、测试合同 describe 与三处真实校验的接缝。"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys

import pytest

from camctl.acceptance.schema import precise_validator_for
from camctl.contracts.json_values import parse_exact_json
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.definitions import candidate_capabilities
from camctl.devices.drivers.adb_cameras.timelapse_parameters import (
    TIMELAPSE_DURATIONS_S, timelapse_intervals_s, timelapse_model_ids,
)
from camctl_fixtures import Deployment, future_schedule
from camera_demo_fixtures import camera_spec

REPO = Path(__file__).resolve().parents[2]
GENERATOR = Path("apps/camctl/scripts/render_timelapse_time_parameters.py")
RESOURCE = Path("docs/hardware/timelapse-time-parameters.json")
DOCUMENT = Path("docs/hardware/camera-control-handoff.md")
AUTHORITY = Path("apps/camctl/src/camctl/devices/drivers/adb_cameras/timelapse_parameters.py")


def test_generated_views_are_current_and_independently_deliverable(tmp_path):
    # 删除输出、改变生成区或生产域后，--check 必须检测到漂移；--write 可重建。
    checked = subprocess.run([sys.executable, str(REPO / GENERATOR), "--check"],
                             capture_output=True, text=True, cwd=REPO)
    assert checked.returncode == 0, checked.stderr
    for relative in (GENERATOR, AUTHORITY, DOCUMENT, RESOURCE):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    # 副本保留实际包层级，避免 editable 安装优先于不完整的命名空间副本。
    package = tmp_path / AUTHORITY.parent
    source_root = tmp_path / "apps/camctl/src"
    while package != source_root:
        (package / "__init__.py").touch()
        package = package.parent
    resource = parse_exact_json((tmp_path / RESOURCE).read_text())
    assert "models" in resource, "时间资源必须按型号导出参数视图"
    assert resource["duration_s"] == list(TIMELAPSE_DURATIONS_S)
    assert resource["relation"] == "duration_s > interval_s"
    assert set(resource["models"]) == set(timelapse_model_ids())
    action6 = resource["models"]["dji-action6"]
    osmo = resource["models"]["dji-osmo360-ii"]
    assert action6["interval_s"][0] == 1
    assert osmo["interval_s"][0] == 2 and 1 not in osmo["interval_s"]
    assert action6["legal_pair_count"] == 134
    assert osmo["legal_pair_count"] == 126
    assert action6["shortest_pair"] == {"interval_s": 1, "duration_s": 300}
    assert osmo["shortest_pair"] == {"interval_s": 2, "duration_s": 300}
    expected = (tmp_path / RESOURCE).read_bytes()
    (tmp_path / RESOURCE).write_text("{}\n")
    run = lambda mode: subprocess.run([sys.executable, str(tmp_path / GENERATOR), mode],
                                     capture_output=True, text=True, cwd=tmp_path)
    stale = run("--check")
    assert stale.returncode == 1
    assert RESOURCE.as_posix() in stale.stderr
    assert (tmp_path / RESOURCE).read_text() == "{}\n"
    assert run("--write").returncode == 0
    assert (tmp_path / RESOURCE).read_bytes() == expected
    assert run("--check").returncode == 0
    authority = tmp_path / AUTHORITY
    authority.write_text(authority.read_text()
                         + "\n_TIMELAPSE_INTERVALS_S = tuple(sorted((*_TIMELAPSE_INTERVALS_S, 7)))\n")
    changed = run("--check")
    assert changed.returncode == 1
    assert DOCUMENT.as_posix() in changed.stderr and RESOURCE.as_posix() in changed.stderr
    assert run("--write").returncode == 0
    updated = json.loads((tmp_path / RESOURCE).read_text())
    assert 7 in updated["models"]["dji-action6"]["interval_s"]
    assert updated["models"]["dji-action6"]["legal_pair_count"] == 142
    document = tmp_path / DOCUMENT
    document.write_text(document.read_text().replace("<!-- END GENERATED TIMELAPSE TIME PARAMETERS -->", ""))
    broken = run("--write")
    assert broken.returncode == 1 and DOCUMENT.as_posix() in broken.stderr


def parameter_cases(parameter_type, model):
    """完整枚举来自权威域，判定预期用独立有理数比较。"""
    intervals = timelapse_intervals_s(model.value)
    minimum_interval = 1 if model is CameraModel.ACTION6 else 2
    base = {"type": parameter_type, "interval_s": minimum_interval, "duration_s": 300,
            "outputs": "video", "exposure": {"mode": "auto"}}
    cases = []
    def add(name, params, valid, text=None):
        cases.append({"name": name, "params_text": text or json.dumps(params), "valid": valid})
    for interval in intervals:
        for duration in TIMELAPSE_DURATIONS_S:
            add(f"pair-{interval}-{duration}", {**base, "interval_s": interval, "duration_s": duration},
                Fraction(duration) > Fraction(interval))
    for interval, duration, valid in (
        (f"{minimum_interval}.0", "300.0", True), (f"{minimum_interval}e0", "3e2", True),
        (f"{minimum_interval}.00000000000000001", "300", False),
        ("0.99999999999999999", "300", False),
        ("1", "300.00000000000000001", False),
        ("0.5", "300", False), ("7", "300", False),
        (str(minimum_interval), "60", False), (str(minimum_interval), "5400", False),
        (str(minimum_interval), "6000", False),
    ):
        text = json.dumps(base).replace(f'"interval_s": {minimum_interval},',
                                        f'"interval_s": {interval},').replace(
            '"duration_s": 300,', f'"duration_s": {duration},')
        add(f"exact-{interval}-{duration}", base, valid, text)
    if model is CameraModel.OSMO360II:
        one_second = json.dumps({**base, "interval_s": 1})
        add("one-second-input-is-preserved-for-correction", {**base, "interval_s": 1}, False, one_second)
    for key in ("interval_s", "duration_s"):
        for value in (True, str(base[key]), None, {}, []):
            add(f"type-{key}-{value!r}", {**base, key: value}, False)
        missing = deepcopy(base)
        del missing[key]
        add(f"missing-{key}", missing, False)
    for outputs in ("video", "video_raw", "video_jpeg"):
        for exposure in ({"mode": "auto"}, {"mode": "manual", "iso": 800}):
            add(f"output-{outputs}-{exposure['mode']}", {**base, "outputs": outputs, "exposure": exposure},
                parameter_type == "action6_timelapse" or outputs == "video")
    for exposure in ({"mode": "auto", "compensation_ev": 0}, {"mode": "auto", "iso": 800},
                     {"mode": "manual"}, {"mode": "manual", "iso": 400},
                     {"mode": "manual", "iso": 800, "compensation_ev": 0}):
        add(f"exposure-{exposure}", {**base, "exposure": exposure}, False)
    for iso_token, valid in (("800.0", True), ("8e2", True), ("800.00000000000000001", False)):
        manual = {**base, "exposure": {"mode": "manual", "iso": "ISO_TOKEN"}}
        add(f"exact-iso-{iso_token}", manual, valid,
            json.dumps(manual).replace('"ISO_TOKEN"', iso_token))
    other = "osmo360ii_timelapse" if parameter_type == "action6_timelapse" else "action6_timelapse"
    add("other-model", {**base, "type": other}, False)
    add("estimate-is-not-params", {**base, "video_size_estimate": {}}, False)
    return cases


@pytest.mark.parametrize("model", list(CameraModel))
def test_describe_python_browser_and_node_export_agree(tmp_path, model):
    deployment = Deployment(tmp_path, devices=False)
    driver = camera_spec(deployment, model, {})
    deployment.install_client_capabilities(driver)
    raw = (tmp_path / "client-store/device-capabilities.json").read_text()
    described = parse_exact_json(raw)
    parameter = next(a for a in described["devices"][0]["actions"]
                     if a["type"] == "camera_timelapse")["parameter_types"][0]
    source = next(c for c in candidate_capabilities(model) if c.action_type == "camera_timelapse")
    assert parameter["schema"] == {**source.schema, "default": {}}
    assert source.defaults == {}
    assert parameter["video_size_estimate"] == source.video_size_estimate
    assert all(type(value) is int for value in parameter["schema"]["properties"]["interval_s"]["enum"])
    cases = parameter_cases(parameter["type"], model)
    validator = precise_validator_for(parameter["schema"])
    for case in cases:
        params = parse_exact_json(case["params_text"])
        before = deepcopy(params)
        assert validator.is_valid(params) == case["valid"], case["name"]
        assert params == before
    expected_pair_count = 134 if model is CameraModel.ACTION6 else 126
    assert sum(c["valid"] for c in cases if c["name"].startswith("pair-")) == expected_pair_count
    inputs = tmp_path / "parameter-cases.json"
    inputs.write_text(json.dumps({"scheduled_at": future_schedule(3600), "cases": cases}))
    result = deployment.run_client_driver("validate-parameters", str(tmp_path / "client-store"), str(inputs),
                                          output=tmp_path / "parameter-receipt.json")
    for case, observed in zip(cases, result["cases"], strict=True):
        assert observed["name"] == case["name"]
        assert observed["browser_valid"] == observed["shared_valid"] == observed["export_valid"] == case["valid"], case["name"]
        assert parse_exact_json(observed["browser_params"]) == parse_exact_json(case["params_text"])
        assert parse_exact_json(observed["shared_params"]) == parse_exact_json(case["params_text"])
        if case["valid"]:
            exported = parse_exact_json(observed["exported"])
            assert exported["actions"][0]["params"] == parse_exact_json(case["params_text"])
            assert "video_size_estimate" not in observed["exported"]
        else:
            assert observed["export_error"] == "invalid_plan"

    # 受理真实候选后，在尚未开始时取消，核对生效参数和发布报告。
    # 此处不依赖尚未交付的新命令映射，也不冒充完整拍摄验收。
    minimum_interval = 1 if model is CameraModel.ACTION6 else 2
    accepted = next(row for row in result["cases"]
                    if row["name"] == f"exact-{minimum_interval}.0-300.0")
    plan = tmp_path / "accepted-plan.json"
    plan.write_text(accepted["exported"])
    original = parse_exact_json(accepted["exported"])
    initialized = deployment.camctl("init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    submitted = deployment.camctl("submit", str(plan), "--config", str(deployment.config_path), driver=driver)
    assert submitted.exit_code == 0, submitted.stderr
    with closing(sqlite3.connect(deployment.state_db)) as connection:
        effective, execution = connection.execute(
            "SELECT effective_params_json, execution_spec_json FROM actions").fetchone()
    assert parse_exact_json(effective) == original["actions"][0]["params"]
    assert parse_exact_json(execution)["target_duration_ms"] == 300000
    assert "video_size_estimate" not in effective + execution
    cancel, _ = deployment.export_plan_with_client({"name": "取消未启动采集", "actions": [{
        "name": "取消", "type": "cancel_task", "scheduled_at": future_schedule(1),
        "params": {"target": {"request_id": original["request_id"]}},
    }]})
    submitted_cancel = deployment.camctl("submit", str(cancel), "--config", str(deployment.config_path), driver=driver)
    assert submitted_cancel.exit_code == 0, submitted_cancel.stderr
    ran = deployment.camctl("run", "--config", str(deployment.config_path), driver=driver)
    assert ran.exit_code == 0, ran.stderr
    reports = list(deployment.ready.glob("status-report-*.json"))
    assert reports
    observed = []
    for path in reports:
        text = path.read_text()
        assert "video_size_estimate" not in text
        report = parse_exact_json(text)
        observed.extend(action for report_plan in report["plans"] for action in report_plan["actions"]
                        if action["name"] == "延时摄影")
    assert any(action["status"] == "canceled" for action in observed)
    assert all(action["effective_params"] == original["actions"][0]["params"] for action in observed)
    assert not (deployment.gates / "camera-calls.jsonl").exists()
    assert deployment.import_reports_with_client(deployment.ready)["saved_report_ids"]
