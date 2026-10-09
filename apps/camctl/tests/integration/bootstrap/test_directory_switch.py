"""B4 目录切换允许分支的组件集成测试。

真实文件系统、会话锁与既有状态库组合：数据库责任与目录内容分类
判定切换资格；责任全部结束且原目录只剩空目录树、新目录不存在或
同样只剩空目录树时，显式 init 允许切换并三路径共同保存；任一阻
止项或保存失败保持原绑定，提交异常按重读的完整绑定分类。切换保
留数据库身份、历史与对象 ID。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.persistence.directory_switch import (
    SwitchCommitError,
    SwitchCommitObservation,
    SwitchFacts,
    load_switch_facts,
    scan_directory_tree,
    switch_directory_binding,
)
from camctl.persistence.initialization import (
    InitOutcome,
    _canonical_binding,
    initialize_state,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_initialization import _STAGING_SUBDIRS, _config, _dump

_SHA256 = "a" * 64


class _CommitThenError:
    """真实提交已完成，但调用者未取得成功返回；可另使重读失效。"""

    def __init__(self, inner, *, lose_observation: bool = False) -> None:
        self.inner = inner
        self.committed = False
        self.lose_observation = lose_observation

    def execute(self, sql: str, *args):
        statement = sql.strip().upper()
        if self.committed and self.lose_observation and statement.startswith("SELECT"):
            raise sqlite3.OperationalError("injected observation failure")
        result = self.inner.execute(sql, *args)
        if statement == "COMMIT":
            self.committed = True
            raise sqlite3.OperationalError("injected loss after durable commit")
        return result

    def close(self):
        self.inner.close()

    @property
    def in_transaction(self):
        return self.inner.in_transaction


def _seed_liabilities(state_db: Path) -> None:
    """直接登记各类切换责任行；只依赖表约束，不经业务流程。"""
    connection = sqlite3.connect(state_db)
    try:
        connection.executescript(f"""
            INSERT INTO intermediate_files (id,owner_action_id,purpose,relative_path,
                retention_state,cleanup_state,created_event_id,last_event_id,change_count)
            VALUES
                (1,1,2,'derived/1.bin',1,1,1,1,1),
                (2,1,2,'derived/2.bin',2,2,1,1,1),
                (3,1,2,'derived/3.bin',2,4,1,1,1),
                (4,1,2,'derived/4.bin',3,1,1,1,1),
                (5,1,2,'derived/5.bin',3,1,1,1,1),
                (6,1,2,'derived/6.bin',4,1,1,1,1);
            INSERT INTO outputs (id,source_action_id,kind,device_file_id,
                intermediate_file_id,availability,cleanup_status,media_json,
                created_event_id,last_event_id,change_count)
            VALUES
                (1,1,1,NULL,4,1,1,'{{}}',1,1,1),
                (2,1,1,NULL,5,3,4,'{{}}',1,1,1),
                (3,1,1,1,NULL,1,1,'{{}}',1,1,1);
            INSERT INTO deliveries (id,action_id,output_id,file_name,display_name,status,
                publication_intent_event_id,published_event_id,withdrawal_state,
                withdrawal_error_json,created_event_id,last_event_id,change_count)
            VALUES
                (1,1,1,'del-001.bin','展示一',4,1,NULL,1,NULL,1,1,1),
                (2,1,2,'del-002.bin','展示二',7,NULL,NULL,1,NULL,1,1,1),
                (3,1,3,'del-003.bin','展示三',5,1,1,2,NULL,1,1,1),
                (4,2,3,'del-004.bin','展示四',5,1,1,6,'{{}}',1,1,1);
            INSERT INTO operation_runs (id,action_id,kind,responsibility_key,activity_id,
                status,attempts_used,max_attempts_used,retry_wait_required)
            VALUES
                (1,1,1,'start/action-1',1,1,0,1,0),
                (2,1,1,'start/action-2',1,2,0,1,0),
                (3,1,1,'start/action-3',1,3,0,1,0),
                (4,1,1,'start/action-4',1,5,0,1,0);
            INSERT INTO reports (id,frozen_event_id,from_wm,to_wm,format_version,status,
                size_bytes,sha256,publication_count,last_published_event_id,
                last_error_json,created_event_id,last_event_id)
            VALUES
                (1,0,0,0,1,1,NULL,NULL,0,NULL,NULL,1,1),
                (2,0,0,0,1,4,10,'{_SHA256}',1,1,NULL,1,1),
                (3,0,0,0,1,5,NULL,NULL,0,NULL,'{{}}',1,1);
        """)
        connection.commit()
    finally:
        connection.close()


class TestScanDirectoryTree:
    def test_sibling_directories_do_not_accumulate_unbounded_pending_work(
            self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "ready"
        root.mkdir()
        for index in range(600):
            (root / str(index)).mkdir()
        original_scandir = os.scandir
        outstanding = set()
        peak = 0

        class TrackedEntries:
            def __init__(self, path):
                outstanding.discard(str(path))
                self.inner = original_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

            def close(self):
                self.inner.close()

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal peak
                entry = next(self.inner)
                if entry.is_dir(follow_symlinks=False):
                    outstanding.add(entry.path)
                    peak = max(peak, len(outstanding))
                return entry

        monkeypatch.setattr("camctl.persistence.directory_switch.os.scandir", TrackedEntries)
        assert scan_directory_tree(root, required=True) is None
        assert not outstanding
        assert peak <= 256

    def test_missing_required_directory_blocks(self, tmp_path: Path) -> None:
        blocked = scan_directory_tree(tmp_path / "absent", required=True)
        assert blocked is not None
        assert "原目录不存在" in blocked

    def test_missing_optional_directory_passes(self, tmp_path: Path) -> None:
        assert scan_directory_tree(tmp_path / "absent", required=False) is None

    def test_empty_nested_tree_passes(self, tmp_path: Path) -> None:
        root = tmp_path / "ready"
        (root / "nested" / "deeper").mkdir(parents=True)
        assert scan_directory_tree(root, required=True) is None

    def test_file_in_tree_blocks(self, tmp_path: Path) -> None:
        root = tmp_path / "ready"
        root.mkdir()
        (root / "nested").mkdir()
        (root / "nested" / "leftover.bin").write_bytes(b"x")
        blocked = scan_directory_tree(root, required=True)
        assert blocked is not None
        assert "仍存在文件" in blocked

    def test_non_directory_occupying_root_blocks(self, tmp_path: Path) -> None:
        target = tmp_path / "ready"
        target.write_text("occupied", encoding="utf-8")
        blocked = scan_directory_tree(target, required=True)
        assert blocked is not None
        assert "非目录" in blocked

    def test_unreadable_directory_blocks(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "ready"
        root.mkdir()

        def broken_scandir(path):
            raise OSError("injected permission failure")

        monkeypatch.setattr(
            "camctl.persistence.directory_switch.os.scandir", broken_scandir)
        blocked = scan_directory_tree(root, required=True)
        assert blocked is not None
        assert "目录不能可靠检查" in blocked

    @pytest.mark.skipif(os.name == "nt", reason="符号链接对象在 Windows 开发环境不可创建")
    def test_symlink_entry_blocks(self, tmp_path: Path) -> None:
        root = tmp_path / "ready"
        root.mkdir()
        (tmp_path / "elsewhere").mkdir()
        (root / "alias").symlink_to(tmp_path / "elsewhere")
        blocked = scan_directory_tree(root, required=True)
        assert blocked is not None
        assert "符号链接" in blocked


class TestLoadSwitchFacts:
    def test_clean_database_reports_no_liability(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert load_switch_facts(owned.connection) == SwitchFacts(
                required_intermediates=0, pending_cleanups=0,
                promoted_pending_output_cleanup=0, unfinished_runs=0,
                open_deliveries=0, open_withdrawals=0, open_reports=0)
        finally:
            owned.connection.close()

    def test_each_liability_class_is_counted(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        _seed_liabilities(state_db)
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            # 已结束的清理、已 CLEANED 的成品、CANCELED 交付、PUBLISHED
            # 报告与终态运行不构成旧路径责任；其余各类分别计数。
            assert load_switch_facts(owned.connection) == SwitchFacts(
                required_intermediates=1, pending_cleanups=1,
                promoted_pending_output_cleanup=1, unfinished_runs=2,
                open_deliveries=1, open_withdrawals=2, open_reports=2)
        finally:
            owned.connection.close()


class TestSwitchDirectoryBinding:
    def test_uncommitted_new_binding_cannot_confirm_success(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")
        new_binding = tuple(_canonical_binding(Path(value)) for value in (
            moved.paths.staging, moved.paths.ready, moved.paths.processing))
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())

        class UnconfirmedTransaction(_CommitThenError):
            def execute(self, sql, *args):
                if sql.strip().upper() in ("COMMIT", "ROLLBACK"):
                    raise sqlite3.OperationalError("injected unfinished transaction")
                return self.inner.execute(sql, *args)

        old_binding = (owned.metadata.staging_path, owned.metadata.ready_path,
                       owned.metadata.processing_path)
        try:
            with pytest.raises(SwitchCommitError) as caught:
                switch_directory_binding(
                    replace(owned, connection=UnconfirmedTransaction(owned.connection)),
                    old_binding, new_binding)
            assert caught.value.observation is None
            with sqlite3.connect(state_db) as reader:
                assert tuple(reader.execute(
                    "SELECT staging_path, ready_path, processing_path FROM database_metadata"
                ).fetchone()) == old_binding
        finally:
            owned.connection.close()

    def test_commit_error_with_new_binding_returns_completed(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")
        new_binding = tuple(_canonical_binding(Path(value)) for value in (
            moved.paths.staging, moved.paths.ready, moved.paths.processing))
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            old_binding = (owned.metadata.staging_path, owned.metadata.ready_path,
                           owned.metadata.processing_path)
            injected = replace(owned, connection=_CommitThenError(owned.connection))
            assert switch_directory_binding(injected, old_binding, new_binding) is (
                SwitchCommitObservation.COMPLETED)
            assert tuple(owned.connection.execute(
                "SELECT staging_path, ready_path, processing_path FROM database_metadata"
            ).fetchone()) == new_binding
        finally:
            owned.connection.close()

    def test_switch_commits_three_paths_together(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")
        new_binding = (
            _canonical_binding(Path(moved.paths.staging)),
            _canonical_binding(Path(moved.paths.ready)),
            _canonical_binding(Path(moved.paths.processing)),
        )
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            old_binding = (owned.metadata.staging_path,
                           owned.metadata.ready_path,
                           owned.metadata.processing_path)
            assert (switch_directory_binding(owned, old_binding, new_binding)
                    is SwitchCommitObservation.COMPLETED)
        finally:
            owned.connection.close()
        confirmed = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            saved = (confirmed.metadata.staging_path,
                     confirmed.metadata.ready_path,
                     confirmed.metadata.processing_path)
            assert saved == new_binding
        finally:
            confirmed.connection.close()

    def test_commit_failure_rolls_back_to_old_binding(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")
        new_binding = (
            _canonical_binding(Path(moved.paths.staging)),
            _canonical_binding(Path(moved.paths.ready)),
            _canonical_binding(Path(moved.paths.processing)),
        )
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        old_binding = (owned.metadata.staging_path,
                       owned.metadata.ready_path,
                       owned.metadata.processing_path)

        class _FailingCommit:
            """委托真实连接，仅提交语句注入失败，模拟提交错误。"""

            def __init__(self, inner) -> None:
                self._inner = inner

            def execute(self, sql: str, *args):
                if sql.strip().upper().startswith("COMMIT"):
                    raise sqlite3.OperationalError("injected commit failure")
                return self._inner.execute(sql, *args)

            def close(self):
                return self._inner.close()

            @property
            def in_transaction(self):
                return self._inner.in_transaction

        from types import SimpleNamespace

        try:
            injected = SimpleNamespace(
                connection=_FailingCommit(owned.connection))
            with pytest.raises(SwitchCommitError) as caught:
                switch_directory_binding(injected, old_binding, new_binding)
            # 提交异常经回滚确认：重读判定为未完成，不混用新旧目录。
            assert "not_completed" in str(caught.value)
        finally:
            owned.connection.close()
        kept = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            saved = (kept.metadata.staging_path, kept.metadata.ready_path,
                     kept.metadata.processing_path)
            assert saved == old_binding
        finally:
            kept.connection.close()

    def test_transaction_recheck_blocks_on_seeded_liability(
            self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        _seed_liabilities(state_db)
        moved = _config(tmp_path, prefix="new-")
        new_binding = (
            _canonical_binding(Path(moved.paths.staging)),
            _canonical_binding(Path(moved.paths.ready)),
            _canonical_binding(Path(moved.paths.processing)),
        )
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            old_binding = (owned.metadata.staging_path,
                           owned.metadata.ready_path,
                           owned.metadata.processing_path)
            with pytest.raises(SwitchCommitError, match="未结束责任"):
                switch_directory_binding(owned, old_binding, new_binding)
        finally:
            owned.connection.close()
        kept = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            saved = (kept.metadata.staging_path, kept.metadata.ready_path,
                     kept.metadata.processing_path)
            assert saved[0] != new_binding[0]
        finally:
            kept.connection.close()


def _config_roots(tmp_path: Path, staging: str, ready: str, processing: str):
    return load_config(
        {
            "paths": {
                "state_db": str(tmp_path / "state.db"),
                "log_file": str(tmp_path / "camctl.log"),
                "staging": staging,
                "ready": ready,
                "processing": processing,
            }
        },
        ConfigDefaults(),
    )


class TestInitializeStateDirectorySwitch:
    def test_transaction_responsibility_read_error_returns_failed_and_rolls_back(
            self, tmp_path: Path, monkeypatch) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        before = _dump(state_db)
        connections = []

        class ResponsibilityReadError:
            def __init__(self, inner):
                self.inner = inner
                self.closed_in_transaction = None

            def execute(self, sql, *args):
                if self.inner.in_transaction and sql.startswith("SELECT COUNT(*)"):
                    raise sqlite3.OperationalError("injected responsibility read error")
                return self.inner.execute(sql, *args)

            @property
            def in_transaction(self):
                return self.inner.in_transaction

            def close(self):
                self.closed_in_transaction = self.inner.in_transaction
                self.inner.close()

        def wrapped_open(*args, **kwargs):
            owned = open_existing(*args, **kwargs)
            wrapper = ResponsibilityReadError(owned.connection)
            connections.append(wrapper)
            return replace(owned, connection=wrapper)

        monkeypatch.setattr("camctl.persistence.initialization.open_existing", wrapped_open)
        result = initialize_state(_config(tmp_path, prefix="new-"), state_db)
        assert result.outcome is InitOutcome.FAILED
        assert isinstance(result.error, SwitchCommitError)
        assert result.error.observation is SwitchCommitObservation.NOT_COMPLETED
        assert connections[0].closed_in_transaction is False
        assert _dump(state_db) == before

    @pytest.mark.parametrize("lose_observation", [False, True])
    def test_actual_commit_result_controls_init_outcome(
            self, tmp_path: Path, monkeypatch, lose_observation: bool) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")
        original_open = open_existing

        def injected_open(*args, **kwargs):
            owned = original_open(*args, **kwargs)
            return replace(owned, connection=_CommitThenError(
                owned.connection, lose_observation=lose_observation))

        monkeypatch.setattr("camctl.persistence.initialization.open_existing", injected_open)
        outcome = initialize_state(moved, state_db)
        if lose_observation:
            assert outcome.outcome is InitOutcome.FAILED
            assert "原绑定仍有效" not in outcome.detail
        else:
            assert outcome.outcome is InitOutcome.SWITCHED, outcome.detail
        with sqlite3.connect(state_db) as connection:
            assert tuple(connection.execute(
                "SELECT staging_path, ready_path, processing_path FROM database_metadata"
            ).fetchone()) == tuple(_canonical_binding(Path(value)) for value in (
                moved.paths.staging, moved.paths.ready, moved.paths.processing))

    @pytest.mark.skipif(os.name == "nt", reason="需要创建真实符号链接对象")
    @pytest.mark.parametrize("root_name", ["staging", "ready", "processing"])
    @pytest.mark.parametrize("dangling", [False, True])
    @pytest.mark.parametrize("existing", [False, True])
    def test_configured_root_symlink_is_rejected(
            self, tmp_path: Path, root_name: str, dangling: bool, existing: bool) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        if existing:
            assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
            before = _dump(state_db)
        candidate = _config(tmp_path, prefix="new-" if existing else "")
        target = tmp_path / "linked-target"
        if not dangling:
            target.mkdir()
        link = Path(getattr(candidate.paths, root_name))
        link.symlink_to(target, target_is_directory=True)

        result = initialize_state(candidate, state_db)

        assert result.outcome is InitOutcome.FAILED, result.detail
        assert link.is_symlink()
        if existing:
            assert _dump(state_db) == before
        else:
            assert not state_db.exists()

    def test_clean_history_switches_and_preserves_identity(
            self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        before = _dump(state_db)
        moved = _config(tmp_path, prefix="new-")
        result = initialize_state(moved, state_db)
        assert result.outcome is InitOutcome.SWITCHED
        after = _dump(state_db)
        # 元信息只更换部署位置：身份、格式与全部业务历史保持。
        old_meta = before["database_metadata"][0]
        new_meta = after["database_metadata"][0]
        assert new_meta[1:4] == old_meta[1:4]
        assert new_meta[4:7] == (
            _canonical_binding(Path(moved.paths.staging)),
            _canonical_binding(Path(moved.paths.ready)),
            _canonical_binding(Path(moved.paths.processing)),
        )
        for table, rows in before.items():
            if table != "database_metadata":
                assert after[table] == rows
        for name in _STAGING_SUBDIRS:
            assert (Path(moved.paths.staging) / name).is_dir(), name
        # 新配置重复 init 按已有库验证，不再切换。
        assert (initialize_state(moved, state_db).outcome
                is InitOutcome.VERIFIED_EXISTING)
        # 原目录原样保留：不复制、不迁移、不清理。
        assert Path(cfg.paths.staging).is_dir()

    def test_liability_blocks_switch_and_original_config_continues(
            self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        old_binding = None
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            old_binding = (owned.metadata.staging_path,
                           owned.metadata.ready_path,
                           owned.metadata.processing_path)
        finally:
            owned.connection.close()
        _seed_liabilities(state_db)
        moved = _config(tmp_path, prefix="new-")
        result = initialize_state(moved, state_db)
        assert result.outcome is InitOutcome.FAILED
        assert "REQUIRED" in result.detail
        kept = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            saved = (kept.metadata.staging_path, kept.metadata.ready_path,
                     kept.metadata.processing_path)
            assert saved == old_binding
        finally:
            kept.connection.close()
        # 恢复原配置可以继续正常处理：按已有库验证原部署。
        assert initialize_state(cfg, state_db).outcome is InitOutcome.VERIFIED_EXISTING

    def test_original_directory_residual_file_blocks(
            self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        (Path(cfg.paths.ready) / "leftover.bin").write_bytes(b"x")
        moved = _config(tmp_path, prefix="new-")
        result = initialize_state(moved, state_db)
        assert result.outcome is InitOutcome.FAILED
        assert "仍存在文件" in result.detail
        confirmed = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert (confirmed.metadata.ready_path
                    == _canonical_binding(Path(cfg.paths.ready)))
        finally:
            confirmed.connection.close()

    def test_new_directory_conflict_blocks(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")
        new_staging = Path(moved.paths.staging)
        new_staging.mkdir(parents=True)
        (new_staging / "occupied.bin").write_bytes(b"x")
        result = initialize_state(moved, state_db)
        assert result.outcome is InitOutcome.FAILED
        assert "仍存在文件" in result.detail
        confirmed = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert (confirmed.metadata.staging_path
                    == _canonical_binding(Path(cfg.paths.staging)))
        finally:
            confirmed.connection.close()

    def test_nested_or_identical_roots_rejected(self, tmp_path: Path) -> None:
        state_db = tmp_path / "state.db"
        nested = _config_roots(
            tmp_path, str(tmp_path / "roots"), str(tmp_path / "roots" / "ready"),
            str(tmp_path / "roots" / "processing"))
        first = initialize_state(nested, state_db)
        assert first.outcome is InitOutcome.FAILED
        assert "互相包含" in first.detail
        identical = _config_roots(
            tmp_path, str(tmp_path / "a-staging"), str(tmp_path / "a-ready"),
            str(tmp_path / "a-ready"))
        second = initialize_state(identical, state_db)
        assert second.outcome is InitOutcome.FAILED
        assert "指向同一位置" in second.detail
        assert not state_db.exists()

    @pytest.mark.skipif(
        os.name != "nt",
        reason="大小写别名核对依赖 Windows 文件系统的大小写不敏感性")
    def test_case_alias_roots_rejected(self, tmp_path: Path) -> None:
        state_db = tmp_path / "state.db"
        aliased = _config_roots(
            tmp_path, str(tmp_path / "CASE-staging"), str(tmp_path / "case-staging"),
            str(tmp_path / "case-processing"))
        result = initialize_state(aliased, state_db)
        assert result.outcome is InitOutcome.FAILED
        assert "指向同一位置" in result.detail
        assert not state_db.exists()

    def test_cross_device_roots_rejected(self, tmp_path: Path, monkeypatch) -> None:
        state_db = tmp_path / "state.db"
        processing = tmp_path / "cross-processing"
        processing.mkdir()
        devices = {str(processing): 42}

        def fake_device_of(path):
            return devices.get(str(path), 1)

        monkeypatch.setattr(
            "camctl.persistence.directory_switch._device_of", fake_device_of)
        spread = _config_roots(
            tmp_path, str(tmp_path / "cross-staging"),
            str(tmp_path / "cross-ready"), str(processing))
        result = initialize_state(spread, state_db)
        assert result.outcome is InitOutcome.FAILED
        assert "同一文件系统" in result.detail
        assert not state_db.exists()

    def test_switch_rejects_nested_new_roots(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        nested = _config_roots(
            tmp_path, str(tmp_path / "b-staging"),
            str(tmp_path / "b-staging" / "ready"),
            str(tmp_path / "b-processing"))
        result = initialize_state(nested, state_db)
        assert result.outcome is InitOutcome.FAILED
        assert "互相包含" in result.detail
        confirmed = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert (confirmed.metadata.staging_path
                    == _canonical_binding(Path(cfg.paths.staging)))
        finally:
            confirmed.connection.close()

    def test_binding_save_failure_keeps_old_binding_and_empty_new_dirs(
            self, tmp_path: Path, monkeypatch) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")

        def failing_switch(owned, old_binding, new_binding):
            raise SwitchCommitError(
                "绑定保存事务失败: injected；重读判定 not_completed",
                observation=SwitchCommitObservation.NOT_COMPLETED)

        monkeypatch.setattr(
            "camctl.persistence.initialization.switch_directory_binding",
            failing_switch)
        result = initialize_state(moved, state_db)
        assert result.outcome is InitOutcome.FAILED
        assert "原绑定仍有效" in result.detail
        # 新准备的空目录可以保留，但不构成切换成功：库值仍为整套旧绑定。
        assert Path(moved.paths.staging).is_dir()
        confirmed = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert (confirmed.metadata.staging_path
                    == _canonical_binding(Path(cfg.paths.staging)))
        finally:
            confirmed.connection.close()

    def test_round_trip_switch_back_to_original_roots(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
        moved = _config(tmp_path, prefix="new-")
        assert initialize_state(moved, state_db).outcome is InitOutcome.SWITCHED
        # 两侧目录均只剩空目录树：来回切换各保存一整套绑定。
        assert initialize_state(cfg, state_db).outcome is InitOutcome.SWITCHED
        confirmed = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert (confirmed.metadata.ready_path
                    == _canonical_binding(Path(cfg.paths.ready)))
        finally:
            confirmed.connection.close()
