"""B3/B5 的组件集成测试：真实 CLI 进程的结果通道。

独立进程验证 describe 导出（无状态库、无真实设备）、stdout/stderr
边界与退出码；消息到达后仍等待实际退出。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(*arguments: str, home: Path | None = None) -> subprocess.CompletedProcess:
    import os

    environment = dict(os.environ)
    if home is not None:
        environment["HOME"] = str(home)
        environment["USERPROFILE"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "camctl", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )


class TestDescribeProcess:
    def test_describe_without_database_or_devices(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        completed = _run_cli("describe", home=home)
        assert completed.returncode == 0
        document = json.loads(completed.stdout)
        assert document == {"devices": []}
        assert completed.stdout.endswith("}\n")
        assert completed.stdout.count("\n") == 1
        # 不创建业务文件：无状态库、无交接目录。
        assert not (home / ".camctl" / "state.db").exists()
        assert not (home / ".camctl" / "staging").exists()

    def test_describe_with_config_file(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / ".camctl").mkdir(parents=True)
        (home / ".camctl" / "config.toml").write_text(
            '[log]\nlevel = "INFO"\n', encoding="utf-8"
        )
        completed = _run_cli("describe", home=home)
        assert completed.returncode == 0
        assert json.loads(completed.stdout) == {"devices": []}

    def test_describe_with_invalid_config_exits_one(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / ".camctl").mkdir(parents=True)
        (home / ".camctl" / "config.toml").write_text(
            '[log]\nfile_count = 0\n', encoding="utf-8"
        )
        completed = _run_cli("describe", home=home)
        assert completed.returncode == 1
        assert completed.stdout == ""
        assert completed.stderr != ""


class TestCommandLineProcess:
    def test_syntax_error_exits_one(self) -> None:
        completed = _run_cli("explode")
        assert completed.returncode == 1
        assert completed.stdout == ""

    def test_version_outputs(self) -> None:
        completed = _run_cli("--version")
        assert completed.returncode == 0
        assert completed.stdout.endswith("\n")

    def test_run_not_assembled_exits_one(self) -> None:
        completed = _run_cli("run")
        assert completed.returncode == 1
        assert completed.stdout == ""
        assert completed.stderr != ""
