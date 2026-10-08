"""独立接入资料的配置示例与真实 CLI 发布、C 模块领取共同工作。"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from _wsl_host_demo import HostDemo, wait_for

_COMPONENT = Path(__file__).resolve().parents[2] / "apps/host-demo"
_ENTRY = "from camctl.cli import main; raise SystemExit(main())"


@pytest.mark.parametrize("explicit_missing", [False, True])
def test_default_configuration_is_shared_by_init_and_run(tmp_path: Path, explicit_missing: bool):
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home)}
    options = ["--config", str(tmp_path / "missing.toml")] if explicit_missing else []
    for command in ["init", "run"]:
        result = subprocess.run(
            [sys.executable, "-c", _ENTRY, command, *options],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
    assert (home / ".camctl/state.db").is_file()


@pytest.mark.skipif(os.name != "posix", reason="独立交付示例在部署侧 Linux 环境验证")
def test_shipped_deployment_guide_publishes_and_claims_same_report(
    tmp_path: Path, host_demo: str,
):
    materials = tmp_path / "materials"
    materials.mkdir()
    shutil.copyfile(_COMPONENT / "README.md", materials / "README.md")
    if (_COMPONENT / "examples").exists():
        shutil.copytree(_COMPONENT / "examples", materials / "examples")
    example = dict(re.findall(
        r'config\.(\w+)_path\s*=\s*(NULL|"[^"]*");',
        (materials / "README.md").read_text(),
    ))
    original_root = Path(example["ready"].strip('"')).parent
    runtime = tmp_path / "deployment"
    runtime.mkdir()

    def relocated(key: str) -> Path:
        return runtime / Path(example[key].strip('"')).relative_to(original_root)

    ready, processing = relocated("ready"), relocated("processing")
    ready.mkdir()
    processing.mkdir()
    config = None
    if example["config"] != "NULL":
        config = relocated("config")
        config.write_text((materials / "examples/config.toml").read_text().replace(
            original_root.as_posix(), runtime.as_posix()))
    home = tmp_path / "home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home)}
    options = ["--config", str(config)] if config else []
    cli_ready = (Path(tomllib.loads(config.read_text())["paths"]["ready"])
                 if config else home / ".camctl/ready")
    initialized = subprocess.run(
        [sys.executable, "-c", _ENTRY, "init", *options],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert initialized.returncode == 0, initialized.stderr
    launcher = tmp_path / "camctl"
    launcher.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(_ENTRY)} \"$@\"\n"
    )
    launcher.chmod(0o755)
    deployment = SimpleNamespace(
        root=runtime, ready=ready, processing=processing, config_path=config,
    )
    demo = HostDemo(deployment, host_demo, launcher=str(launcher))
    demo.log_path = relocated("log")
    now = datetime.now(timezone.utc)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "request_id": "1", "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "name": "独立接入检查", "actions": [{
            "name": "检查报告", "type": "report_status",
            "scheduled_at": (now + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "params": {"scope": "full"}}]}, ensure_ascii=False))

    def completed_report():
        for path in cli_ready.glob("status-report-*.json"):
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
        demo.start(env=environment)
        demo.submit(plan)
        published, content = wait_for(completed_report, 30, "接入示例的报告动作未完成")
        demo.claim()
        claimed = processing / published.name
        assert claimed.exists(), f"CLI 发布到 {published.parent}，接入示例却领取 {ready}"
        assert claimed.read_bytes() == content
    finally:
        demo.stop()
