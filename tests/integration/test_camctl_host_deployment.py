"""独立接入资料的配置示例与真实 CLI 发布、C 模块领取共同工作。"""
from __future__ import annotations

from camctl_memory_plugin import run_camctl, write_budgeted_launcher

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from _wsl_host_demo import HostDemo, query_state, wait_for
from _osmo_builtin_clock import read_clock, set_frozen, whole_second_utc_us
from camctl_fixtures import Deployment

_COMPONENT = Path(__file__).resolve().parents[2] / "apps/host-demo"
_ENTRY = "from camctl.cli import main; raise SystemExit(main())"


@pytest.mark.parametrize("explicit_missing", [False, True])
def test_default_configuration_is_shared_by_init_and_run(tmp_path: Path, explicit_missing: bool):
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home)}
    if os.name == "nt":
        # Windows 的用户目录展开读 USERPROFILE 而非 HOME。
        environment["USERPROFILE"] = str(home)
    options = ["--config", str(tmp_path / "missing.toml")] if explicit_missing else []
    for command in ["init", "run"]:
        result = run_camctl(
            [sys.executable, "-c", _ENTRY, command, *options],
            receipt_root=tmp_path, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
    assert (home / ".camctl/state.db").is_file()


@pytest.mark.skipif(os.name != "posix", reason="独立交付示例在部署侧 Linux 环境验证")
@pytest.mark.parametrize("custom_paths", [False, True])
def test_shipped_deployment_guide_publishes_and_claims_same_report(
    tmp_path: Path, host_demo: str, custom_paths: bool,
):
    home = tmp_path / "运行账户 home"
    runtime = home / ".camctl"
    runtime.mkdir(parents=True)
    config = runtime / "config.toml"
    shutil.copyfile(_COMPONENT / "examples/config.toml", config)
    assert "paths" not in tomllib.loads(config.read_text())
    ready, processing = runtime / "ready", runtime / "processing"
    if custom_paths:
        # 覆盖 TOML 时，host 必须显式使用同一组交接路径。
        ready, processing = runtime / "custom-ready", runtime / "custom-processing"
        with config.open("a") as stream:
            stream.write("\n[paths]\nready = " + json.dumps(str(ready)) +
                         "\nprocessing = " + json.dumps(str(processing)) + "\n")
    environment = {**os.environ, "HOME": str(home)}
    clock_path = tmp_path / "clock.json"
    set_frozen(clock_path, utc_us=whole_second_utc_us(), monotonic_ns=time.monotonic_ns())
    environment.update({"CAMCTL_TEST_CLOCK_FILE": str(clock_path),
                        "CAMCTL_TEST_CLOCK_LOG": str(tmp_path / "clock.jsonl")})
    environment.pop("CAMCTL_TEST_DRIVER_SPEC", None)
    bridge_command = [sys.executable, str(Path(__file__).with_name("_camctl_stub_entry.py"))]
    initialized = run_camctl(
        [*bridge_command, "init"],
        receipt_root=tmp_path, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert initialized.returncode == 0, initialized.stderr
    assert (runtime / "state.db").is_file()
    assert ready.is_dir() and processing.is_dir()
    launcher = runtime / "venv/bin/camctl"
    launcher.parent.mkdir(parents=True)
    if custom_paths:
        launcher.write_text(
            f"#!/bin/sh\nexec {shlex.join(bridge_command)} \"$@\"\n"
        )
        launcher.chmod(0o755)
    else:
        write_budgeted_launcher(
            bridge_command, receipt_root=runtime, target=launcher,
        )
    deployment = SimpleNamespace(
        root=runtime, ready=ready, processing=processing, config_path=None, state_db=runtime / "state.db",
    )
    demo = HostDemo(deployment, host_demo, launcher=str(launcher))
    demo.log_path = runtime / "host.log"
    now = datetime.fromtimestamp(read_clock(clock_path)["utc_us"] / 1_000_000, timezone.utc)
    scheduled_at = now + timedelta(seconds=10)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "request_id": "1", "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "name": "独立接入检查", "actions": [{
            "name": "检查报告", "type": "report_status",
            "scheduled_at": scheduled_at.strftime("%Y-%m-%d %H:%M:%S"),
            "params": {"scope": "full"}}]}, ensure_ascii=False))

    def completed_report():
        for path in ready.glob("status-report-*.json"):
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                continue
            report = json.loads(content)
            assert not report.get("plan_file_diagnostics"), report
            if any(action["name"] == "检查报告" and action["status"] == "succeeded"
                   for saved in report.get("plans", []) for action in saved["actions"]):
                return path, content
        return None

    try:
        demo.start(env=environment, use_default_paths=not custom_paths)
        demo.submit(plan)
        # 成功返回时已持久化，动作仍待执行；删除原输入后重启也从状态库恢复。
        assert query_state(runtime / "state.db", "SELECT status FROM actions") == [(1,)]
        plan.unlink()
        demo.stop()
        reading = read_clock(clock_path)
        target_us = int(scheduled_at.timestamp()) * 1_000_000
        delta_us = target_us - reading["utc_us"]
        assert delta_us >= 0
        set_frozen(clock_path, utc_us=target_us,
                   monotonic_ns=reading["monotonic_ns"] + delta_us * 1_000)
        failure = demo.watch_failures(request_id="1", expected_statuses={"检查报告": "SUCCEEDED"})
        demo.start(env=environment, use_default_paths=not custom_paths)
        published, content = wait_for(completed_report, 30, "接入示例的报告动作未完成", failure=failure)
        demo.claim()
        claimed = processing / published.name
        assert claimed.exists(), f"CLI 发布到 {published.parent}，接入示例却领取 {ready}"
        assert claimed.read_bytes() == content
    finally:
        demo.stop()


@pytest.mark.skipif(os.name != "posix", reason="C 模块在部署侧 Linux 环境验证")
def test_submit_saved_rejection_allows_removing_input(tmp_path: Path, host_demo: str):
    deployment = Deployment(tmp_path, devices=False)
    initialized = deployment.camctl("init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    plan = tmp_path / "rejected.json"
    plan.write_text(json.dumps({"request_id": "2"}))
    demo = HostDemo(deployment, host_demo)
    try:
        demo.start()
        demo.submit(plan)
        # 业务拒绝不等于提交会话失败；成功返回前诊断已经持久化。
        diagnostics = query_state(deployment.state_db,
                                  "SELECT errors_json FROM plan_file_diagnostics")
        assert len(diagnostics) == 1
        assert json.loads(diagnostics[0][0])[0]["code"] == "plan_body_rejected"
        assert query_state(deployment.state_db, "SELECT COUNT(*) FROM plans") == [(0,)]
        plan.unlink()
    finally:
        demo.stop()
