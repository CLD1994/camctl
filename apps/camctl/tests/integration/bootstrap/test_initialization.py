"""B4 显式初始化与既有库验证的组件集成测试。

真实文件系统、会话锁与 SQLite 组合：首次创建发布完整初始库，
重复 init 验证并保留全部事实，无效目标不被覆盖，锁竞争失败。
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import sqlite3
import sys
from io import StringIO
from pathlib import Path

import pytest

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.values import new_operation_key
from camctl.host_files.paths import PURPOSE_DIRECTORIES
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.runtime import (
    CURRENT_FORMAT_VERSION,
    DbOpenMode,
    DbConfig,
    StateDatabaseError,
    canonical_directory_binding,
    open_existing,
)
from camctl.session.locks import (
    LockActionResult,
    LockError,
    LockOperation,
    acquire_session,
    lock_error_facts,
    lock_file_paths,
)

from ..persistence.test_transactions import PlanCreateCommand, plan_guards
from ..persistence.test_runtime import _create_legacy_v1_database


def _expected_staging_subdirs() -> tuple[str, ...]:
    return (*PURPOSE_DIRECTORIES.values(), "logs", "capture-work")


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


def _database_image(path: Path) -> tuple[list[tuple], dict[str, list[tuple]]]:
    """读取所有建表 SQL 及行事实，核对 init 没有升级或改写既有库。"""
    connection = sqlite3.connect(path)
    try:
        schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger') ORDER BY type, name"
        ).fetchall()
    finally:
        connection.close()
    return schema, _dump(path)


def _install_linux_flock_failure(
    monkeypatch,
    *,
    stage: str,
    error: BaseException,
    close_error: BaseException | None = None,
):
    """仅在真实 Linux 锁系统调用边界注入失败，保留实际 open/close。"""
    if sys.platform != "linux":
        pytest.skip("此项验证要求真实 Linux flock")

    import fcntl

    lock_flags = fcntl.LOCK_EX | fcntl.LOCK_NB
    calls = {
        "fd": None,
        "acquire": [],
        "unlock": [],
        "close": [],
        "actual_close": [],
        "close_error": [],
    }
    actual_flock = fcntl.flock
    actual_close = os.close

    def controlled_flock(fd: int, operation: int) -> None:
        if operation == lock_flags:
            calls["fd"] = fd
            calls["acquire"].append(fd)
            if stage == "acquire":
                raise error
        elif operation == fcntl.LOCK_UN:
            calls["unlock"].append(fd)
            if stage == "unlock":
                raise error
        actual_flock(fd, operation)

    def tracked_close(fd: int) -> None:
        if fd == calls["fd"]:
            calls["close"].append(fd)
        actual_close(fd)
        if fd == calls["fd"]:
            calls["actual_close"].append(fd)
            if close_error is not None and not calls["close_error"]:
                calls["close_error"].append(fd)
                raise close_error

    monkeypatch.setattr(fcntl, "flock", controlled_flock)
    monkeypatch.setattr(os, "close", tracked_close)
    return calls


def _assert_one_lock_fd_closed(calls) -> int:
    assert len(calls["acquire"]) == 1
    assert calls["fd"] == calls["acquire"][0]
    assert calls["close"] == [calls["fd"]]
    assert calls["actual_close"] == [calls["fd"]]
    return calls["fd"]


def _directory_file_image(roots: tuple[Path, ...]) -> tuple[tuple[str, str, bytes], ...]:
    """记录实际目录树中的文件路径和字节，不从数据库行推断文件存在。"""
    files: list[tuple[str, str, bytes]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files.append((str(root), path.relative_to(root).as_posix(), path.read_bytes()))
    return tuple(files)


def _bound_roots(config) -> tuple[Path, ...]:
    return tuple(Path(getattr(config.paths, name)) for name in ("staging", "ready", "processing"))


def _assert_switch_kept_business_image(before, after, config) -> None:
    before_schema, before_tables = before
    after_schema, after_tables = after
    assert after_schema == before_schema
    assert after_tables.keys() == before_tables.keys()
    for table in before_tables:
        if table != "database_metadata":
            assert after_tables[table] == before_tables[table], table

    old_metadata = before_tables["database_metadata"][0]
    new_metadata = after_tables["database_metadata"][0]
    assert new_metadata[:4] == old_metadata[:4]
    assert new_metadata[4:7] == tuple(
        canonical_directory_binding(Path(getattr(config.paths, name)))
        for name in ("staging", "ready", "processing")
    )
    assert new_metadata[7:] == old_metadata[7:]
    assert before_tables["device_files"]
    assert before_tables["outputs"]
    assert before_tables["reports"]
    assert before_tables["history_events"]
    assert before_tables["state_syncs"]


def _diagnostic_evidence(value: BaseException) -> str:
    """检查错误对象、notes、可信锁事实和因果链中的实际诊断。"""
    seen: set[int] = set()
    parts: list[str] = []

    def visit(error: BaseException | None) -> None:
        if error is None or id(error) in seen:
            return
        seen.add(id(error))
        parts.append(str(error))
        parts.append(repr(getattr(error, "args", ())))
        parts.append(repr(getattr(error, "__notes__", ())))
        parts.append(repr(getattr(error, "__dict__", {})))
        facts = lock_error_facts(error)
        if facts is not None:
            parts.append(repr(facts))
        visit(error.__cause__)
        visit(error.__context__)
        for item in getattr(error, "exceptions", ()):
            visit(item)

    visit(value)
    return "\n".join(parts)


async def _completed_history_with_files(tmp_path: Path):
    """复用真实初始化、受理、文件事实和报告输出，为复验提供完整旧库。"""
    from .test_directory_switch_history import _completed_history

    config, report, report_bytes = await _completed_history(tmp_path)
    return config, report, report_bytes


def _write_cli_config(path: Path, config) -> None:
    values = {
        name: getattr(config.paths, name)
        for name in ("state_db", "log_file", "staging", "ready", "processing")
    }
    path.write_text(
        "[paths]\n"
        + "".join(f"{name} = {json.dumps(value)}\n" for name, value in values.items()),
        encoding="utf-8",
    )


class TestFirstCreation:
    def test_init_creates_complete_initial_database(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        result = initialize_state(cfg, Path(cfg.paths.state_db))
        assert result.outcome is InitOutcome.CREATED
        owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert owned.metadata.application_id == "camctl"
            assert owned.metadata.format_version == CURRENT_FORMAT_VERSION
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
        for name in _expected_staging_subdirs():
            assert (staging / name).is_dir(), name
        assert Path(cfg.paths.ready).is_dir()
        assert Path(cfg.paths.processing).is_dir()

    def test_init_builds_schema_and_initial_records_in_one_transaction(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        cfg = _config(tmp_path)
        actual_connect = sqlite3.connect
        traces: list[list[tuple[str, bool]]] = []

        def traced_connect(database, *args, **kwargs):
            connection = actual_connect(database, *args, **kwargs)
            if str(database) != ":memory:":
                statements: list[tuple[str, bool]] = []
                connection.set_trace_callback(
                    lambda statement: statements.append(
                        (statement, connection.in_transaction)
                    )
                )
                traces.append(statements)
            return connection

        monkeypatch.setattr(sqlite3, "connect", traced_connect)
        result = initialize_state(cfg, Path(cfg.paths.state_db))
        assert result.outcome is InitOutcome.CREATED
        assert len(traces) == 1

        statements = traces[0]
        schema_indexes = [
            index for index, (statement, _) in enumerate(statements)
            if statement.lstrip().upper().startswith("CREATE ")
        ]
        metadata_indexes = [
            index for index, (statement, _) in enumerate(statements)
            if statement.lstrip().upper().startswith("INSERT INTO DATABASE_METADATA")
        ]
        state_indexes = [
            index for index, (statement, _) in enumerate(statements)
            if statement.lstrip().upper().startswith("INSERT INTO RUNTIME_STATE")
        ]
        transaction_indexes = [
            (index, statement.strip().rstrip(";").upper())
            for index, (statement, _) in enumerate(statements)
            if statement.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))
        ]

        assert schema_indexes
        assert len(metadata_indexes) == len(state_indexes) == 1
        assert len(transaction_indexes) == 2
        begin_index, begin_statement = transaction_indexes[0]
        commit_index, commit_statement = transaction_indexes[1]
        assert begin_statement.startswith("BEGIN ")
        assert commit_statement == "COMMIT"
        assert begin_index < schema_indexes[0]
        assert schema_indexes[-1] < metadata_indexes[0] < state_indexes[0]
        assert state_indexes[0] < commit_index
        assert all(
            statements[index][1]
            for index in (*schema_indexes, *metadata_indexes, *state_indexes)
        )

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

    def test_init_preserves_legacy_v1_sql_metadata_and_history(self, tmp_path: Path, plan_guards) -> None:
        cfg = _config(tmp_path)
        target = Path(cfg.paths.state_db)
        directories = tuple(
            canonical_directory_binding(Path(value))
            for value in (
                cfg.paths.staging,
                cfg.paths.ready,
                cfg.paths.processing,
            )
        )
        _create_legacy_v1_database(target, format_version=1, directories=directories)
        owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            from camctl.persistence.transaction import commit_operation

            receipt = commit_operation(PlanCreateCommand((1,)), new_operation_key(), owned)
            assert receipt.kind == "completed"
            assert owned.metadata.format_version == 1
        finally:
            owned.connection.close()
        before = _database_image(target)

        result = initialize_state(cfg, target)

        assert result.outcome is InitOutcome.VERIFIED_EXISTING
        assert _database_image(target) == before

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

    def test_binding_mismatch_with_liability_keeps_binding(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        # 登记仍为 REQUIRED 的中间文件：原目录责任未结束，拒绝切换。
        connection = sqlite3.connect(cfg.paths.state_db)
        try:
            connection.execute(
                "INSERT INTO intermediate_files (id,owner_action_id,purpose,relative_path,"
                "retention_state,cleanup_state,created_event_id,last_event_id,change_count)"
                " VALUES (1,1,2,'derived/1.bin',1,1,1,1,1)")
            connection.commit()
        finally:
            connection.close()
        moved = _config(tmp_path, prefix="new-")
        result = initialize_state(moved, Path(moved.paths.state_db))
        assert result.outcome is InitOutcome.FAILED
        assert "REQUIRED" in result.detail
        owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        try:
            # 绑定按平台规范字符串保存与核对（Windows 为可逆映射约定）。
            from camctl.persistence.initialization import _canonical_binding

            assert owned.metadata.staging_path == _canonical_binding(Path(cfg.paths.staging))
        finally:
            owned.connection.close()


class TestRuntimeLibraryGate:
    def test_incompatible_runtime_library_fails_before_any_file(
            self, tmp_path: Path, monkeypatch) -> None:
        from camctl.persistence import initialization as initialization_module
        from camctl.persistence.runtime import RuntimeLibraryError

        def incompatible() -> None:
            raise RuntimeLibraryError(
                "Python 实际链接的 SQLite 3.30.1 不满足统一运行条件")

        monkeypatch.setattr(
            initialization_module, "ensure_runtime_library", incompatible)
        cfg = _config(tmp_path)
        state_db = Path(cfg.paths.state_db)
        result = initialize_state(cfg, state_db)
        assert result.outcome is InitOutcome.FAILED
        assert "运行库条件不满足" in result.detail
        # 拒绝发生在任何文件创建之前：不建库、不建目录。
        assert not state_db.exists()
        assert not Path(cfg.paths.staging).exists()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_state", ["created", "verified_existing", "switched"])
async def test_release_oserror_keeps_completed_initialization_facts(
    tmp_path: Path, monkeypatch, completed_state: str
) -> None:
    if completed_state == "created":
        source_config = _config(tmp_path)
        target_config = source_config
        report_bytes = None
    else:
        source_config, _, report_bytes = await _completed_history_with_files(tmp_path)
        target_config = (
            source_config
            if completed_state == "verified_existing"
            else _config(tmp_path, prefix="new-")
        )
        if completed_state == "verified_existing":
            note = Path(source_config.paths.ready) / "operator-note.bin"
            note.write_bytes(b"existing directory content survives init cleanup")

    state_db = Path(target_config.paths.state_db)
    if completed_state == "created":
        before = None
    else:
        before = _database_image(state_db)
        assert before[1]["device_files"]
        assert before[1]["outputs"]

    report_path = tmp_path / "original-report.json"
    source_roots = _bound_roots(source_config)
    target_roots = _bound_roots(target_config)
    file_roots = tuple(dict.fromkeys((*source_roots, *target_roots)))
    files_before = _directory_file_image(file_roots)
    if report_bytes is not None:
        assert report_path.read_bytes() == report_bytes

    release_error = OSError(errno.EIO, f"init-release-{completed_state}-sentinel")
    with monkeypatch.context() as patch:
        calls = _install_linux_flock_failure(
            patch, stage="unlock", error=release_error
        )
        result = initialize_state(target_config, state_db)

    assert result.outcome is InitOutcome.FAILED
    assert isinstance(result.error, LockError)
    assert result.secondary_errors == ()
    assert result.error.__cause__ is release_error
    facts = lock_error_facts(result.error)
    assert facts is not None
    assert facts.operation is LockOperation.UNLOCK
    assert facts.path == lock_file_paths(state_db)[0]
    assert facts.original_error is release_error
    assert facts.close_result is LockActionResult.CONFIRMED
    lock_fd = _assert_one_lock_fd_closed(calls)
    assert calls["unlock"] == [lock_fd]

    after_failure = _database_image(state_db)
    if before is None:
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert owned.metadata.application_id == "camctl"
            assert owned.metadata.format_version == CURRENT_FORMAT_VERSION
            instance_id = owned.metadata.instance_id
            assert instance_id
            assert owned.connection.execute(
                "SELECT acknowledged_wm FROM runtime_state"
            ).fetchone() == (0,)
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM history_events"
            ).fetchone() == (0,)
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM history_transactions"
            ).fetchone() == (0,)
        finally:
            owned.connection.close()
    elif completed_state == "switched":
        _assert_switch_kept_business_image(before, after_failure, target_config)
    else:
        assert after_failure == before

    assert _directory_file_image(file_roots) == files_before
    if report_bytes is not None:
        assert report_path.read_bytes() == report_bytes
    assert not tuple(tmp_path.glob(".state.db.init-*"))

    verified = initialize_state(target_config, state_db)
    assert verified.outcome is InitOutcome.VERIFIED_EXISTING
    assert _database_image(state_db) == after_failure
    assert _directory_file_image(file_roots) == files_before
    if report_bytes is not None:
        assert report_path.read_bytes() == report_bytes


def test_failed_existing_database_keeps_primary_error_and_release_secondary(
    tmp_path: Path, monkeypatch
) -> None:
    from camctl.persistence import initialization as initialization_module

    cfg = _config(tmp_path)
    state_db = Path(cfg.paths.state_db)
    state_db.write_bytes(b"preserve this invalid database file")
    original_bytes = state_db.read_bytes()
    original_verify = initialization_module._verify_existing
    body_results = []

    def observe_real_validation(*args, **kwargs):
        result = original_verify(*args, **kwargs)
        body_results.append(result)
        return result

    release_error = OSError(errno.EIO, "init-failed-library-release-sentinel")
    with monkeypatch.context() as patch:
        patch.setattr(initialization_module, "_verify_existing", observe_real_validation)
        calls = _install_linux_flock_failure(
            patch, stage="unlock", error=release_error
        )
        result = initialize_state(cfg, state_db)

    assert len(body_results) == 1
    body_result = body_results[0]
    assert body_result.outcome is InitOutcome.FAILED
    assert isinstance(body_result.error, StateDatabaseError)
    assert result.outcome is InitOutcome.FAILED
    assert result.detail == body_result.detail
    assert result.error is body_result.error
    assert len(result.secondary_errors) == 1
    secondary = result.secondary_errors[0]
    assert isinstance(secondary, LockError)
    assert secondary.__cause__ is release_error
    facts = lock_error_facts(secondary)
    assert facts is not None
    assert facts.operation is LockOperation.UNLOCK
    assert facts.close_result is LockActionResult.CONFIRMED
    lock_fd = _assert_one_lock_fd_closed(calls)
    assert calls["unlock"] == [lock_fd]
    assert state_db.read_bytes() == original_bytes

    still_invalid = initialize_state(cfg, state_db)
    assert still_invalid.outcome is InitOutcome.FAILED
    assert isinstance(still_invalid.error, StateDatabaseError)
    assert state_db.read_bytes() == original_bytes


def test_cli_init_reports_release_lock_error_only_on_stderr(
    tmp_path: Path, monkeypatch
) -> None:
    from camctl.cli import main

    cfg = _config(tmp_path)
    config_path = tmp_path / "camctl.toml"
    _write_cli_config(config_path, cfg)
    stdout = StringIO()
    stderr = StringIO()
    release_error = OSError(errno.EIO, "cli-init-release-diagnostic-sentinel")

    with monkeypatch.context() as patch:
        calls = _install_linux_flock_failure(
            patch, stage="unlock", error=release_error
        )
        exit_code = main(
            ["init", "--config", str(config_path)], stdout=stdout, stderr=stderr
        )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue()
    assert "cli-init-release-diagnostic-sentinel" in stderr.getvalue()
    assert len(stderr.getvalue().splitlines()) == 1
    lock_fd = _assert_one_lock_fd_closed(calls)
    assert calls["unlock"] == [lock_fd]

    state_db = Path(cfg.paths.state_db)
    owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert owned.metadata.instance_id
        assert owned.metadata.format_version == CURRENT_FORMAT_VERSION
    finally:
        owned.connection.close()
    assert initialize_state(cfg, state_db).outcome is InitOutcome.VERIFIED_EXISTING


@pytest.mark.parametrize(
    ("body_state", "inject_close_error"),
    [("success", False), ("failed", False), ("success", True)],
    ids=["success", "failed", "success-close-error"],
)
def test_cleanup_control_after_result_keeps_same_object_and_diagnostics(
    tmp_path: Path, monkeypatch, body_state: str, inject_close_error: bool
) -> None:
    from camctl.persistence import initialization as initialization_module

    cfg = _config(tmp_path)
    state_db = Path(cfg.paths.state_db)
    if body_state == "failed":
        state_db.write_bytes(b"keep invalid library after cancellation")
        original_bytes = state_db.read_bytes()
        original_body = initialization_module._verify_existing
        body_results = []

        def observe_real_validation(*args, **kwargs):
            result = original_body(*args, **kwargs)
            body_results.append(result)
            return result

        original_result = observe_real_validation
        observed_method = "_verify_existing"
    else:
        original_body = initialization_module._create_new
        body_results = []

        def observe_real_creation(*args, **kwargs):
            result = original_body(*args, **kwargs)
            body_results.append(result)
            return result

        original_result = observe_real_creation
        observed_method = "_create_new"

    control = asyncio.CancelledError(f"cleanup-{body_state}-control-sentinel")
    control.add_note("caller-owned initialization note")
    close_error = (
        OSError(errno.EIO, "cleanup close secondary sentinel")
        if inject_close_error
        else None
    )
    with monkeypatch.context() as patch:
        patch.setattr(initialization_module, observed_method, original_result)
        calls = _install_linux_flock_failure(
            patch, stage="unlock", error=control, close_error=close_error
        )
        with pytest.raises(asyncio.CancelledError) as caught:
            initialize_state(cfg, state_db)

    assert caught.value is control
    assert len(body_results) == 1
    body_result = body_results[0]
    assert body_result.outcome is (
        InitOutcome.CREATED if body_state == "success" else InitOutcome.FAILED
    )
    assert "caller-owned initialization note" in getattr(control, "__notes__", ())
    assert body_result.detail in _diagnostic_evidence(control)
    facts = lock_error_facts(control)
    assert facts is not None
    assert facts.operation is LockOperation.UNLOCK
    assert facts.close_result is (
        LockActionResult.UNCONFIRMED
        if close_error is not None
        else LockActionResult.CONFIRMED
    )
    if close_error is not None:
        notes = tuple(getattr(control, "__notes__", ()))
        assert any("cleanup close secondary sentinel" in note for note in notes)
        assert any(body_result.detail in note for note in notes)
        assert len(facts.secondary_errors) == 1
        close_facts = facts.secondary_errors[0]
        assert close_facts.operation is LockOperation.CLOSE
        assert close_facts.original_error is close_error
    lock_fd = _assert_one_lock_fd_closed(calls)
    assert calls["unlock"] == [lock_fd]
    assert calls["close_error"] == ([lock_fd] if close_error is not None else [])
    assert not tuple(tmp_path.glob(".state.db.init-*"))

    if body_state == "success":
        after_control = _database_image(state_db)
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert owned.metadata.instance_id
            assert owned.metadata.format_version == CURRENT_FORMAT_VERSION
        finally:
            owned.connection.close()
        assert initialize_state(cfg, state_db).outcome is InitOutcome.VERIFIED_EXISTING
        assert _database_image(state_db) == after_control
    else:
        assert state_db.read_bytes() == original_bytes
        still_invalid = initialize_state(cfg, state_db)
        assert still_invalid.outcome is InitOutcome.FAILED
        assert isinstance(still_invalid.error, StateDatabaseError)
        assert state_db.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "release_kind", ["oserror", "control", "oserror_and_close_oserror"]
)
def test_original_control_survives_later_release_failure(
    tmp_path: Path, monkeypatch, release_kind: str
) -> None:
    from camctl.persistence import initialization as initialization_module

    cfg = _config(tmp_path)
    state_db = Path(cfg.paths.state_db)
    body_control = asyncio.CancelledError("original initialization control sentinel")
    body_control.add_note("original initialization control note")
    release_error: BaseException = (
        KeyboardInterrupt("later initialization control sentinel")
        if release_kind == "control"
        else OSError(errno.EIO, "later initialization release sentinel")
    )
    close_error = (
        OSError(errno.EIO, "later initialization close sentinel")
        if release_kind == "oserror_and_close_oserror"
        else None
    )

    def interrupt_real_initialization(*args, **kwargs):
        raise body_control

    with monkeypatch.context() as patch:
        patch.setattr(
            initialization_module, "_prepare_directories", interrupt_real_initialization
        )
        calls = _install_linux_flock_failure(
            patch, stage="unlock", error=release_error, close_error=close_error
        )
        with pytest.raises(asyncio.CancelledError) as caught:
            initialize_state(cfg, state_db)

    assert caught.value is body_control
    assert "original initialization control note" in getattr(
        body_control, "__notes__", ()
    )
    release_marker = (
        "later initialization control sentinel"
        if release_kind == "control"
        else "later initialization release sentinel"
    )
    assert release_marker in _diagnostic_evidence(body_control)
    lock_fd = _assert_one_lock_fd_closed(calls)
    assert calls["unlock"] == [lock_fd]
    if close_error is not None:
        notes = tuple(getattr(body_control, "__notes__", ()))
        assert any(release_marker in note for note in notes)
        assert any("later initialization close sentinel" in note for note in notes)
        assert calls["close_error"] == [lock_fd]
    else:
        assert calls["close_error"] == []
    assert not state_db.exists()
    assert not tuple(tmp_path.glob(".state.db.init-*"))

    created = initialize_state(cfg, state_db)
    assert created.outcome is InitOutcome.CREATED


@pytest.mark.parametrize("acquire_kind", ["runtime", "oserror", "control"])
def test_acquire_failure_keeps_init_contract_without_mutating_library(
    tmp_path: Path, monkeypatch, acquire_kind: str
) -> None:
    cfg = _config(tmp_path)
    state_db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, state_db).outcome is InitOutcome.CREATED
    image_before = _database_image(state_db)
    files_before = _directory_file_image(_bound_roots(cfg))

    if acquire_kind == "runtime":
        injected: BaseException = RuntimeError("init-acquire-runtime-sentinel")
    elif acquire_kind == "oserror":
        injected = OSError(errno.EIO, "init-acquire-oserror-sentinel")
    else:
        injected = asyncio.CancelledError("init-acquire-control-sentinel")
        injected.add_note("caller-owned acquisition note")

    with monkeypatch.context() as patch:
        calls = _install_linux_flock_failure(
            patch, stage="acquire", error=injected
        )
        if acquire_kind == "control":
            with pytest.raises(asyncio.CancelledError) as caught:
                initialize_state(cfg, state_db)
            assert caught.value is injected
            assert "caller-owned acquisition note" in getattr(
                injected, "__notes__", ()
            )
            facts = lock_error_facts(injected)
            assert facts is not None
            assert facts.operation is LockOperation.TRY_LOCK
            assert facts.close_result is LockActionResult.CONFIRMED
        else:
            result = initialize_state(cfg, state_db)
            assert result.outcome is InitOutcome.FAILED
            assert result.secondary_errors == ()
            if acquire_kind == "runtime":
                assert result.error is injected
            else:
                assert isinstance(result.error, LockError)
                assert result.error.__cause__ is injected
                facts = lock_error_facts(result.error)
                assert facts is not None
                assert facts.operation is LockOperation.TRY_LOCK
                assert facts.close_result is LockActionResult.CONFIRMED

    # 取得失败时没有租约可释放；锁端口只关闭已打开的实际句柄。
    _assert_one_lock_fd_closed(calls)
    assert calls["unlock"] == []
    assert _database_image(state_db) == image_before
    assert _directory_file_image(_bound_roots(cfg)) == files_before
    assert initialize_state(cfg, state_db).outcome is InitOutcome.VERIFIED_EXISTING
    assert _database_image(state_db) == image_before
