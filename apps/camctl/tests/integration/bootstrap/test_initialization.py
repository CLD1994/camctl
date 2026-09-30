"""B4 显式初始化与既有库验证的组件集成测试。

真实文件系统、会话锁与 SQLite 组合：首次创建发布完整初始库，
重复 init 验证并保留全部事实，无效目标不被覆盖，锁竞争失败。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.values import new_operation_key
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.runtime import DbOpenMode, DbConfig, open_existing
from camctl.session.locks import acquire_session, lock_file_paths

from ..persistence.test_transactions import PlanCreateCommand, plan_guards

_STAGING_SUBDIRS = ("deliveries", "recording-inputs", "processing-temp", "derived", "logs")


def _config(tmp_path: Path, *, prefix: str = ""):
    return load_config(
        {
            "paths": {
                "state_db": str(tmp_path / "state.db"),
                "log_file": str(tmp_path / "camctl.log"),
                "staging": str(tmp_path / f"{prefix}staging"),
                "ready": str(tmp_path / f"{prefix}ready"),
                "processing": str(tmp_path / f"{prefix}processing"),
            }
        },
        ConfigDefaults(),
    )


def _dump(path: Path) -> dict[str, list[tuple]]:
    """独立读取全部表的当前事实，用于前后对比。"""
    connection = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
        }
    finally:
        connection.close()


class TestFirstCreation:
    def test_init_creates_complete_initial_database(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        result = initialize_state(cfg, Path(cfg.paths.state_db))
        assert result.outcome is InitOutcome.CREATED
        owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert owned.metadata.application_id == "camctl"
            assert owned.metadata.format_version == 1
            assert owned.metadata.instance_id
        finally:
            owned.connection.close()
        connection = sqlite3.connect(cfg.paths.state_db)
        try:
            assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == (0,)
            assert connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone() == (0,)
        finally:
            connection.close()
        staging = Path(cfg.paths.staging)
        for name in _STAGING_SUBDIRS:
            assert (staging / name).is_dir(), name
        assert Path(cfg.paths.ready).is_dir()
        assert Path(cfg.paths.processing).is_dir()

    def test_failed_directory_preparation_leaves_no_database(self, tmp_path: Path) -> None:
        (tmp_path / "processing").write_text("occupied", encoding="utf-8")
        cfg = _config(tmp_path)
        result = initialize_state(cfg, Path(cfg.paths.state_db))
        assert result.outcome is InitOutcome.FAILED
        assert "目录准备失败" in result.detail
        assert not Path(cfg.paths.state_db).exists()


class TestExistingDatabase:
    def test_init_preserves_existing_database(self, tmp_path: Path, plan_guards) -> None:
        cfg = _config(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        try:
            from camctl.persistence.transaction import commit_operation

            receipt = commit_operation(PlanCreateCommand((1,)), new_operation_key(), owned)
            assert receipt.kind == "completed"
        finally:
            owned.connection.close()
        image_before = _dump(Path(cfg.paths.state_db))

        again = initialize_state(cfg, Path(cfg.paths.state_db))
        assert again.outcome is InitOutcome.VERIFIED_EXISTING
        assert _dump(Path(cfg.paths.state_db)) == image_before

    def test_invalid_existing_file_is_not_replaced(self, tmp_path: Path) -> None:
        target = tmp_path / "state.db"
        target.write_bytes(b"not a database at all")
        original = target.read_bytes()
        cfg = _config(tmp_path)
        result = initialize_state(cfg, target)
        assert result.outcome is InitOutcome.FAILED
        assert target.read_bytes() == original

    def test_empty_existing_file_is_not_rebuilt(self, tmp_path: Path) -> None:
        target = tmp_path / "state.db"
        target.write_bytes(b"")
        cfg = _config(tmp_path)
        result = initialize_state(cfg, target)
        assert result.outcome is InitOutcome.FAILED
        assert target.read_bytes() == b""

    def test_binding_mismatch_fails_and_keeps_binding(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")
        result = initialize_state(moved, Path(moved.paths.state_db))
        assert result.outcome is InitOutcome.FAILED
        assert "绑定不一致" in result.detail
        owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        try:
            # 绑定按平台规范字符串保存与核对（Windows 为可逆映射约定）。
            from camctl.persistence.initialization import _canonical_binding

            assert owned.metadata.staging_path == _canonical_binding(Path(cfg.paths.staging))
        finally:
            owned.connection.close()


class TestLockCoordination:
    def test_lock_conflict_fails_without_waiting(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        session_lock, _ = lock_file_paths(state_db)
        holder = acquire_session(session_lock)
        try:
            result = initialize_state(cfg, state_db)
            assert result.outcome is InitOutcome.FAILED
            assert "会话锁" in result.detail
        finally:
            holder.close()
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED

    def test_init_after_other_process_published_verifies(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        # 另一进程已发布完整库：本次按已有库验证，不重复初始化。
        again = initialize_state(cfg, Path(cfg.paths.state_db))
        assert again.outcome is InitOutcome.VERIFIED_EXISTING
        dump = _dump(Path(cfg.paths.state_db))
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.VERIFIED_EXISTING
        assert _dump(Path(cfg.paths.state_db)) == dump
