"""A6 两个真实入口的输入受理闭环（无设备范围）。

独立 CLI 进程组合 init/submit/run：坏新输入不破坏已有工作，有
效与无效 ACK、缺失状态库分别给出契约内结果，恢复不依赖输入文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_cli(home: Path, *arguments: str, cwd: Path | None = None):
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "camctl", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(cwd) if cwd else None,
        timeout=60,
    )


def _config(home: Path) -> Path:
    config_dir = home / ".camctl"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = config_dir / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[paths]",
                f'state_db = "{(home / "state.db").as_posix()}"',
                f'staging = "{(home / "staging").as_posix()}"',
                f'ready = "{(home / "ready").as_posix()}"',
                f'processing = "{(home / "processing").as_posix()}"',
                '',
                '[devices.cam-1]',
                'kind = "camera"',
                'driver = "camctl-adb"',
            ]
        ),
        encoding="utf-8",
    )
    return config


def _plan_file(home: Path, request_id: str) -> Path:
    target = home / f"plan-{request_id}.json"
    target.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "created_at": "2026-01-15 08:00:00",
                "name": "plan",
                "actions": [
                    {
                        "name": "shoot",
                        "type": "camera_take_photo",
                        "device_id": "cam-1",
                        "scheduled_at": "2026-01-15 09:00:00",
                        "params": {"type": "single_shot"},
                        "policy": {"max_delay_ms": 1000},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return target


class TestRealEntrypoints:
    def test_init_submit_run_closed_loop(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        config = _config(home)

        initialized = _run_cli(home, "init", "--config", str(config))
        assert initialized.returncode == 0, initialized.stderr
        assert initialized.stdout == ""
        assert (home / "state.db").is_file()

        plan = _plan_file(home, "42")
        submitted = _run_cli(home, "submit", str(plan), "--config", str(config))
        assert submitted.returncode == 0, submitted.stderr
        message = json.loads(submitted.stdout)
        assert message["kind"] == "succeeded"
        # 无接纳者且有待执行动作：请求后续 run。
        assert message["body"]["needs_run"] is True
        assert submitted.stdout.count("\n") == 1

        run = _run_cli(home, "run", "--config", str(config))
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout) == {"kind": "succeeded"}

        # 重送同一请求：复用原计划，仍请求后续 run（动作仍待执行）。
        again = _run_cli(home, "submit", str(plan), "--config", str(config))
        assert again.returncode == 0
        assert json.loads(again.stdout)["body"]["needs_run"] is True

    def test_bad_new_input_preserves_existing_work(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        config = _config(home)
        assert _run_cli(home, "init", "--config", str(config)).returncode == 0
        plan = _plan_file(home, "1")
        assert _run_cli(home, "submit", str(plan), "--config", str(config)).returncode == 0

        broken = home / "broken.json"
        broken.write_bytes(b'{"request_id": "2", "actions": [')
        run = _run_cli(home, "run", str(broken), "--config", str(config))
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout) == {"kind": "succeeded"}

        # 删除输入文件后重启：按持久化状态恢复，已有工作保持。
        plan.unlink()
        rerun = _run_cli(home, "run", "--config", str(config))
        assert rerun.returncode == 0
        assert json.loads(rerun.stdout) == {"kind": "succeeded"}

    def test_invalid_body_submit_still_succeeds_with_diagnostics(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        config = _config(home)
        assert _run_cli(home, "init", "--config", str(config)).returncode == 0
        bad = home / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "request_id": "7",
                    "created_at": "2026-01-15 08:00:00",
                    "name": "plan",
                    "actions": [{"name": "x", "type": "teleport"}],
                }
            ),
            encoding="utf-8",
        )
        submitted = _run_cli(home, "submit", str(bad), "--config", str(config))
        # 整份拒绝是正常提交结果：成功退出；拒绝诊断本身构成待报告
        # 变化，仍请求后续 run 处理报告责任。
        assert submitted.returncode == 0, submitted.stderr
        assert json.loads(submitted.stdout)["kind"] == "succeeded"
        assert json.loads(submitted.stdout)["body"]["needs_run"] is True

    def test_missing_database_errors_without_creation(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        config = _config(home)
        plan = _plan_file(home, "42")
        submitted = _run_cli(home, "submit", str(plan), "--config", str(config))
        assert submitted.returncode == 1
        assert submitted.stdout == ""
        assert "不存在" in submitted.stderr
        assert not (home / "state.db").exists()
