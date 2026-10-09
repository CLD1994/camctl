"""显式切换目录后，真实 CLI 发布与 C 模块领取采用新绑定。"""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from _wsl_host_demo import HostDemo, wait_for


_ENTRY = "from camctl.cli import main; raise SystemExit(main())"


def _cli(config, *arguments):
    result = subprocess.run([sys.executable, "-c", _ENTRY, *arguments, "--config", str(config)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result


def _config(path, root, state_db):
    paths = {"state_db": state_db, "log_file": root.parent / "camctl.log",
             "staging": root / "staging", "ready": root / "ready", "processing": root / "processing"}
    path.write_text("[paths]\n" + "\n".join(f"{name} = {json.dumps(str(value))}" for name, value in paths.items()))
    return paths


def _plan(path, request_id, name):
    path.write_text(json.dumps({"request_id": request_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "name": name, "actions": [{"name": name, "type": "report_status", "params": {"scope": "full"}}]}))


@pytest.mark.skipif(os.name != "posix", reason="真实 C 目录切换组合在 Linux 部署环境验证")
def test_new_binding_publishes_and_claims_after_explicit_init(tmp_path, host_demo):
    state_db, config = tmp_path / "state.db", tmp_path / "config.toml"
    old = _config(config, tmp_path / "old", state_db)
    _cli(config, "init")
    first = tmp_path / "first.json"
    _plan(first, "1", "原目录报告")
    _cli(config, "run", str(first))
    originals = {path.name: path.read_bytes() for path in old["ready"].glob("status-report-*.json")}
    assert originals
    with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as connection:
        identity = connection.execute("SELECT instance_id FROM database_metadata").fetchone()[0]
        old_report_id = connection.execute("SELECT MAX(id) FROM reports").fetchone()[0]

    launcher = tmp_path / "camctl-launcher"
    launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(_ENTRY)} \"$@\"\n")
    launcher.chmod(0o755)
    deployment = SimpleNamespace(root=tmp_path, ready=old["ready"], processing=old["processing"], config_path=config)
    demo = HostDemo(deployment, host_demo, launcher=str(launcher))
    try:
        demo.start()
        demo.claim()
        assert not tuple(old["ready"].iterdir())
        for name, content in originals.items():
            claimed = old["processing"] / name
            assert claimed.read_bytes() == content
            claimed.unlink()
    finally:
        demo.stop()

    new = _config(config, tmp_path / "new", state_db)
    _cli(config, "init")
    with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT instance_id FROM database_metadata").fetchone()[0] == identity
        assert connection.execute("SELECT staging_path, ready_path, processing_path FROM database_metadata").fetchone() == (
            str(new["staging"]), str(new["ready"]), str(new["processing"]))
    second = tmp_path / "second.json"
    _plan(second, "2", "新目录报告")
    deployment = SimpleNamespace(root=tmp_path, ready=new["ready"], processing=new["processing"], config_path=config)
    demo = HostDemo(deployment, host_demo, launcher=str(launcher))

    def completed_report():
        for path in new["ready"].glob("status-report-*.json"):
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                continue
            document = json.loads(content)
            if any(action["name"] == "新目录报告" and action["status"] == "succeeded"
                   for plan in document.get("plans", []) for action in plan["actions"]):
                return path, content
        return None

    try:
        demo.start()
        demo.submit(second)
        published, content = wait_for(completed_report, 30, "新目录的报告动作没有完成")
        demo.claim()
        assert (new["processing"] / published.name).read_bytes() == content
        assert not published.exists()
        assert not tuple(old["ready"].iterdir())
        assert not tuple(old["processing"].iterdir())
        with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT MAX(id) FROM reports").fetchone()[0] > old_report_id
    finally:
        demo.stop()
