"""A6 两个真实入口的输入受理闭环（无设备范围）。

独立 CLI 进程组合 init/submit/run：坏新输入不破坏已有工作，有
效与无效 ACK、缺失状态库分别给出契约内结果，恢复不依赖输入文
件。run 会话装配生产报告流程：报告责任处理完毕后正常退出，按
持久化状态与 ready 交付验证受理契约。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest


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
                        "name": "status",
                        "type": "report_status",
                        "params": {"scope": "full"},
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

        # run 装配生产报告流程：同步动作完成、报告发布后正常退出。
        finished = _run_cli(home, "run", "--config", str(config))
        assert finished.returncode == 0, finished.stderr
        with sqlite3.connect(home / "state.db") as connection:
            assert connection.execute(
                "SELECT status FROM actions").fetchall() == [(3,)]
            assert connection.execute(
                "SELECT COUNT(*) FROM reports WHERE status = 4"
            ).fetchone()[0] >= 1
        assert list((home / "ready").glob("status-report-*.json"))

        # 重送同一请求：复用原计划；报告责任已完成，不再请求后续
        # run。
        again = _run_cli(home, "submit", str(plan), "--config", str(config))
        assert again.returncode == 0
        message = json.loads(again.stdout)
        assert message["kind"] == "succeeded"
        assert message["body"]["needs_run"] is False
        with sqlite3.connect(home / "state.db") as connection:
            assert connection.execute("SELECT id,request_id FROM plans").fetchall() == [(1,42)]
            assert connection.execute("SELECT name,status,execution_spec_json FROM actions").fetchall() == [("status",3,"{}")]

    def test_bad_new_input_preserves_existing_work(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        config = _config(home)
        assert _run_cli(home, "init", "--config", str(config)).returncode == 0
        plan = _plan_file(home, "1")
        assert _run_cli(home, "submit", str(plan), "--config", str(config)).returncode == 0

        broken = home / "broken.json"
        broken.write_bytes(b'{"request_id": "2", "actions": [')
        result = _run_cli(home, "run", str(broken), "--config", str(config))
        # 拒绝诊断被受理并报告，报告动作完成：会话正常退出。
        assert result.returncode == 0, result.stderr
        with sqlite3.connect(home / "state.db") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM plan_file_diagnostics").fetchone() == (1,)

        # 删除输入文件后重启：按持久化状态恢复，无新责任即正常退出。
        plan.unlink()
        recovery = _run_cli(home, "run", "--config", str(config))
        assert recovery.returncode == 0, recovery.stderr
        with sqlite3.connect(home / "state.db") as connection:
            assert connection.execute("SELECT request_id FROM plans").fetchall() == [(1,)]
            assert connection.execute("SELECT name,status FROM actions").fetchall() == [("status",3)]
            assert connection.execute("SELECT COUNT(*) FROM plan_file_diagnostics").fetchone() == (1,)

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
        message = json.loads(submitted.stdout)
        assert message["kind"] == "error"
        assert message["body"]["reason"] == "state_db_error"
        assert str(home / "state.db") in message["body"]["details"]["error"]
        assert not (home / "state.db").exists()

    def test_undeployed_driver_rejects_run_and_submit_composition(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        config = _config(home)
        assert _run_cli(home, "init", "--config", str(config)).returncode == 0
        with config.open("a", encoding="utf-8") as stream:
            stream.write('\n[devices.cam-1]\nkind = "camera"\ndriver = "camctl-adb"\n')
        for arguments in (("run",), ("submit", str(_plan_file(home,"42")))):
            result = _run_cli(home, *arguments, "--config", str(config))
            assert result.returncode == 1
            assert result.stdout == ""
            assert "camctl-adb" in result.stderr
        with sqlite3.connect(home / "state.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)

    @pytest.mark.parametrize("mode", ["run", "submit"])
    @pytest.mark.parametrize("scenario", ["bad_identity", "bad_ack", "bad_action", "retry_missing_body", "retry_changed_body"])
    def test_identity_ack_and_body_are_independent(self, tmp_path, mode, scenario):
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
        from camctl.reporting.policy import (
            ReportingRepository, register_report_guards,
        )
        home = tmp_path / "home"
        home.mkdir()
        config = _config(home)
        assert _run_cli(home, "init", "--config", str(config)).returncode == 0
        original_file = _plan_file(home, "42")
        original = json.loads(original_file.read_text(encoding="utf-8"))["actions"][0]
        assert _run_cli(home, "submit", str(original_file), "--config", str(config)).returncode == 0
        register_report_guards()
        owned = open_existing(home / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        try:
            outcome = ReportingRepository().freeze_report(
                new_operation_key(), owned, occurred_at=1,
            )
            assert outcome.kind.value == "completed", outcome.error
            report_id = str(outcome.value.report.report_id)
            to_wm = outcome.value.report.to_wm
        finally:
            owned.connection.close()

        body = json.loads(_plan_file(home, "43").read_text(encoding="utf-8"))
        body["last_report_id"] = report_id
        if scenario == "bad_identity":
            body["request_id"] = 43
        elif scenario == "bad_ack":
            body["last_report_id"] = None
        elif scenario == "bad_action":
            body["actions"][0]["params"] = {"scope":5}
        elif scenario == "retry_missing_body":
            body = {"request_id":"42", "last_report_id":report_id}
        else:
            body = {"request_id":"42", "last_report_id":report_id, "actions":False, "extra":"ignored"}
        incoming = home / "incoming.json"
        incoming.write_text(json.dumps(body), encoding="utf-8")
        if mode == "run":
            # run 完成受理事实（诊断、重用或 ACK 推进）并报告后正
            # 常退出，按持久化状态验证。
            result = _run_cli(home, "run", str(incoming), "--config", str(config))
            assert result.returncode == 0, result.stderr
        else:
            result = _run_cli(home, mode, str(incoming), "--config", str(config))
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["kind"] == "succeeded"
            assert result.stdout.count("\n") == 1
        incoming.unlink()
        original_file.unlink()
        with sqlite3.connect(home / "state.db") as connection:
            expected_requests = [(42,), (43,)] if scenario in {"bad_ack", "bad_action"} else [(42,)]
            assert connection.execute("SELECT request_id FROM plans ORDER BY id").fetchall() == expected_requests
            # run 会话驱动报告动作到终态；submit 不驱动，动作保持
            # 受理时状态；受理失败的动作两种模式下都保持失败。
            terminal = 3 if mode == "run" else 1
            assert connection.execute("SELECT status FROM actions ORDER BY id").fetchall() == (
                [(terminal,), (4,)] if scenario == "bad_action" else [(terminal,)] * len(expected_requests))
            assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone() == (
                (0,) if scenario == "bad_ack" else (to_wm,))
            diagnostics = connection.execute("SELECT errors_json FROM plan_file_diagnostics").fetchall()
            assert len(diagnostics) == (1 if scenario in {"bad_identity", "bad_ack"} else 0)
            if diagnostics:
                assert json.loads(diagnostics[0][0])[0]["details"]["field"] == (
                    "request_id" if scenario == "bad_identity" else "last_report_id")
            cursor = connection.execute("SELECT * FROM actions WHERE id=1")
            row = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
            from camctl.contracts.input_fields import reconstruct_action_input
            assert reconstruct_action_input(row) == original
            for table in ("deliveries", "device_activities", "obtain_items", "cleanup_items"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
