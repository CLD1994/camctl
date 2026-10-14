"""SW-04 run 启动日志扫描的真实数据库、锁与目录边界。"""

from __future__ import annotations

import asyncio
import errno
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap import lifecycle
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.recovery import RecoveryBoundary
from camctl.logging_runtime import copies
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.runtime import StateDatabaseError
from camctl.session.locks import (
    SessionLockConflictError,
    acquire_session,
)


_GATE_TIMEOUT = 10


async def _wait_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=_GATE_TIMEOUT)


async def _wait_task(task: asyncio.Task):
    return await asyncio.wait_for(asyncio.shield(task), timeout=_GATE_TIMEOUT)


async def _wait_task_result(task: asyncio.Task):
    done, _pending = await asyncio.wait({task}, timeout=_GATE_TIMEOUT)
    if task not in done:
        raise TimeoutError("受控 asyncio 任务未收场")
    return task.result()


def _wait_thread_event(event: threading.Event) -> None:
    if not event.wait(timeout=_GATE_TIMEOUT):
        raise TimeoutError("受控线程门闩未获释放")


def _config_for(home: Path, *, log_file: Path | None = None):
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(log_file or home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            }
        },
        ConfigDefaults(),
    )


def _symlink_or_skip(
        link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError as error:
        pytest.skip(f"当前平台不支持符号链接: {error}")
    except OSError as error:
        unsupported = {
            code for code in (
                getattr(errno, "ENOTSUP", None),
                getattr(errno, "EOPNOTSUPP", None),
            ) if code is not None
        }
        if (error.errno in unsupported
                or (os.name == "nt"
                    and getattr(error, "winerror", None) == 1314)):
            pytest.skip(f"当前平台不支持符号链接: {error}")
        raise


def _seed_log_paths(config) -> tuple[Path, Path, Path, Path]:
    logs_dir = Path(config.paths.staging) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    old_copy = logs_dir / "log-copy-old.log"
    old_temp = logs_dir / "failure-marker.json.old.tmp"
    formal_marker = logs_dir / "failure-marker.json"
    unknown = logs_dir / "notes.txt"
    old_copy.write_bytes(b"partial old snapshot")
    old_temp.write_bytes(b"partial old marker")
    formal_marker.write_bytes(b"invalid marker stays owned by recovery")
    unknown.write_bytes(b"unknown purpose")
    return old_copy, old_temp, formal_marker, unknown


async def _run(config, *, boundary: RecoveryBoundary | None = None):
    kwargs = {} if boundary is None else {"recovery_boundary": boundary}
    deps = build_runtime(CommandMode.RUN, config, **kwargs)
    try:
        return await execute_command(deps, None)
    finally:
        close_runtime(deps)


async def test_host_settled_run_sweeps_once_under_session_lock(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    original_listdir = copies._listdir
    original_sweep = copies.startup_sweep
    scans: list[Path] = []
    lock_held_during_scan: list[bool] = []
    formal_exists_after_sweep: list[bool] = []

    def listdir_under_session_lock(path: Path):
        scans.append(Path(path))
        try:
            duplicate = acquire_session(deps.session_lock)
        except SessionLockConflictError:
            lock_held_during_scan.append(True)
        else:
            lock_held_during_scan.append(False)
            duplicate.close()
        return original_listdir(path)

    def observe_sweep(*args, **kwargs):
        result = original_sweep(*args, **kwargs)
        formal_exists_after_sweep.append(formal_marker.exists())
        return result

    monkeypatch.setattr(copies, "_listdir", listdir_under_session_lock)
    monkeypatch.setattr(copies, "startup_sweep", observe_sweep)
    try:
        # 注入空业务流程，隔离报告恢复 owner 对 formal marker 的既有删除职责。
        outcome = await execute_command(deps, None, flows={})
    finally:
        close_runtime(deps)

    assert outcome.succeeded is True
    assert (len(scans), lock_held_during_scan) == (1, [True])
    assert not old_copy.exists()
    assert not old_temp.exists()
    assert formal_exists_after_sweep == [True]
    assert unknown.exists()


async def test_failed_old_copy_delete_is_retried_by_next_run(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, _unknown = _seed_log_paths(config)
    original_unlink = copies._unlink
    original_listdir = copies._listdir
    original_sweep = copies.startup_sweep
    scans: list[Path] = []
    formal_exists_after_sweep: list[bool] = []
    failed_deletes: list[Path] = []
    fail_old_copy = True

    def record_listdir(path: Path):
        scans.append(Path(path))
        return original_listdir(path)

    def observe_sweep(*args, **kwargs):
        result = original_sweep(*args, **kwargs)
        formal_exists_after_sweep.append(formal_marker.exists())
        return result

    def fail_first_old_copy_delete(path: Path):
        nonlocal fail_old_copy
        if fail_old_copy and Path(path) == old_copy:
            fail_old_copy = False
            failed_deletes.append(Path(path))
            raise PermissionError("controlled old-copy unlink failure")
        return original_unlink(path)

    monkeypatch.setattr(copies, "_listdir", record_listdir)
    monkeypatch.setattr(copies, "startup_sweep", observe_sweep)
    monkeypatch.setattr(copies, "_unlink", fail_first_old_copy_delete)
    async def run_without_report_recovery():
        deps = build_runtime(
            CommandMode.RUN, config,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        )
        try:
            return await execute_command(deps, None, flows={})
        finally:
            close_runtime(deps)

    first = await run_without_report_recovery()

    assert first.succeeded is True
    assert scans == [Path(config.paths.staging) / "logs"]
    assert failed_deletes == [old_copy]
    assert old_copy.exists()
    assert not old_temp.exists()
    assert formal_exists_after_sweep == [True]

    second = await run_without_report_recovery()

    assert second.succeeded is True
    assert scans == [Path(config.paths.staging) / "logs"] * 2
    assert not old_copy.exists()
    assert formal_exists_after_sweep == [True, True]


async def test_session_lock_conflict_stops_before_startup_sweep(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, _old_temp, _formal_marker, _unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    held = acquire_session(deps.session_lock)
    original_listdir = copies._listdir
    scans: list[Path] = []

    def record_listdir(path: Path):
        scans.append(Path(path))
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", record_listdir)
    try:
        outcome = await execute_command(deps, None)
    finally:
        held.close()
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "lock_error"
    assert scans == []
    assert old_copy.exists()


async def test_direct_runtime_default_keeps_old_files(
        tmp_path: Path) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, _formal_marker, unknown = _seed_log_paths(config)

    outcome = await _run(config)

    assert outcome.succeeded is True
    assert old_copy.exists()
    assert old_temp.exists()
    assert unknown.exists()


async def test_existing_bad_database_uses_minimal_same_lock_sweep(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    Path(config.paths.state_db).write_bytes(b"not a SQLite database")

    original_open_existing = lifecycle.open_existing
    original_acquire = lifecycle.acquire_session
    original_listdir = copies._listdir
    opens: list[Path] = []
    acquisitions: list[Path] = []
    scans: list[Path] = []
    lock_held_during_scan: list[bool] = []

    def open_existing_once(path, mode, db_config):
        opens.append(Path(path))
        return original_open_existing(path, mode, db_config)

    def acquire_once(path):
        acquisitions.append(Path(path))
        return original_acquire(path)

    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )

    def listdir_under_session_lock(path: Path):
        scans.append(Path(path))
        try:
            duplicate = original_acquire(deps.session_lock)
        except SessionLockConflictError:
            lock_held_during_scan.append(True)
        else:
            lock_held_during_scan.append(False)
            duplicate.close()
        return original_listdir(path)

    # 统计只在正式预检查之后发生的数据库打开，不能让最小坏库路径
    # 重新打开或扫描同一个损坏数据库。
    before_execute_opens = len(opens)
    monkeypatch.setattr(lifecycle, "open_existing", open_existing_once)
    monkeypatch.setattr(lifecycle, "acquire_session", acquire_once)
    monkeypatch.setattr(copies, "_listdir", listdir_under_session_lock)

    def fail_if_report_flows_are_assembled(*_args, **_kwargs):
        raise AssertionError("坏库最小收场不能装配完整报告流程")

    monkeypatch.setattr(
        lifecycle, "_report_assembly", fail_if_report_flows_are_assembled)
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert len(opens) == before_execute_opens
    assert (len(acquisitions), len(scans), lock_held_during_scan) == (
        1, 1, [True])
    assert not old_copy.exists()
    assert not old_temp.exists()
    assert formal_marker.exists()
    assert unknown.exists()


@pytest.mark.parametrize("database_state", ["missing", "unknown"])
async def test_missing_or_unknown_database_stops_before_lock_and_sweep(
        tmp_path: Path, monkeypatch, database_state: str) -> None:
    config = _config_for(tmp_path)
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    original_stat = Path.stat
    state_path = Path(config.paths.state_db).expanduser().resolve()
    if database_state == "unknown":
        # 文件确实存在，但本次不能可靠读取它的文件类型／存在性；
        # 不能用高层 StateDatabaseError 覆盖已经观测到的存在事实。
        Path(config.paths.state_db).write_bytes(b"existence unknown")

        def deny_state_db_stat(path, *args, **kwargs):
            if path == state_path:
                import errno
                raise PermissionError(
                    errno.EACCES, "state database stat denied", str(path))
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", deny_state_db_stat)

    original_acquire = lifecycle.acquire_session
    original_listdir = copies._listdir
    acquisitions: list[Path] = []
    scans: list[Path] = []

    def record_acquire(path):
        acquisitions.append(Path(path))
        return original_acquire(path)

    def record_listdir(path):
        scans.append(Path(path))
        return original_listdir(path)

    monkeypatch.setattr(lifecycle, "acquire_session", record_acquire)
    monkeypatch.setattr(copies, "_listdir", record_listdir)
    deps = None
    preflight_error: PermissionError | None = None
    try:
        deps = build_runtime(
            CommandMode.RUN, config,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        )
    except PermissionError as error:
        preflight_error = error
    finally:
        if database_state == "unknown":
            monkeypatch.setattr(Path, "stat", original_stat)

    if deps is None:
        outcome = None
    else:
        try:
            outcome = await execute_command(deps, None)
        finally:
            close_runtime(deps)

    assert acquisitions == []
    assert scans == []
    assert preflight_error is None, (
        "existence uncertainty must become a state_db_error outcome before "
        f"session work, not escape as {preflight_error!r}")
    assert outcome is not None
    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert old_copy.exists()
    assert old_temp.exists()
    assert formal_marker.exists()
    assert unknown.exists()


async def test_nonregular_database_target_stops_before_lock_and_sweep(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    state_path = Path(config.paths.state_db)
    state_path.mkdir()
    _old_copy, _old_temp, _formal_marker, _unknown = _seed_log_paths(config)
    acquisitions: list[Path] = []
    scans: list[Path] = []
    original_acquire = lifecycle.acquire_session
    original_listdir = copies._listdir

    def record_acquire(path):
        acquisitions.append(Path(path))
        return original_acquire(path)

    def record_listdir(path):
        scans.append(Path(path))
        return original_listdir(path)

    monkeypatch.setattr(lifecycle, "acquire_session", record_acquire)
    monkeypatch.setattr(copies, "_listdir", record_listdir)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert deps.state_db_present is True
    assert deps.state_db_regular is False
    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert acquisitions == []
    assert scans == []


@pytest.mark.parametrize("early_failure", ["open", "on_session_open", "accept"])
async def test_locked_early_database_failure_settles_started_sweep(
        tmp_path: Path, monkeypatch, early_failure: str) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    _old_copy, _old_temp, _formal_marker, _unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    loop = asyncio.get_running_loop()
    listdir_started = asyncio.Event()
    release_listdir = threading.Event()
    original_listdir = copies._listdir
    calls: list[Path] = []

    def blocked_listdir(path: Path):
        calls.append(Path(path))
        loop.call_soon_threadsafe(listdir_started.set)
        _wait_thread_event(release_listdir)
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", blocked_listdir)
    source = None
    if early_failure == "open":
        monkeypatch.setattr(
            lifecycle, "_open_runtime_connection",
            lambda _deps: (_ for _ in ()).throw(
                StateDatabaseError("controlled locked open failure")),
        )
    elif early_failure == "on_session_open":
        monkeypatch.setattr(
            lifecycle, "_initialize_recovery",
            lambda *_args: (_ for _ in ()).throw(
                StateDatabaseError("controlled session-open failure")),
        )
    else:
        from camctl.session import service as session_service

        monkeypatch.setattr(
            session_service, "accept_input",
            lambda *_args: (_ for _ in ()).throw(
                StateDatabaseError("controlled acceptance failure")),
        )
        source = object()

    run_task = asyncio.create_task(execute_command(deps, source))
    try:
        await _wait_event(listdir_started)
        assert calls == [Path(config.paths.staging) / "logs"]
        duplicate = None
        try:
            duplicate = acquire_session(deps.session_lock)
        except SessionLockConflictError:
            pass
        else:
            pytest.fail("started startup sweep 前提前释放了 SessionLock")
        finally:
            if duplicate is not None:
                duplicate.close()
        assert not run_task.done()
    finally:
        release_listdir.set()
    try:
        outcome = await _wait_task(run_task)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert calls == [Path(config.paths.staging) / "logs"]


async def test_locked_early_database_failure_waits_for_queued_sweep(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, _formal_marker, _unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="queued-open-failure")
    loop.set_default_executor(pool)
    release_worker = threading.Event()
    worker_entered = asyncio.Event()
    listdir_started = asyncio.Event()
    release_listdir = threading.Event()
    startup_call_queued = asyncio.Event()
    open_attempted = asyncio.Event()
    scans: list[Path] = []

    def occupy_worker():
        loop.call_soon_threadsafe(worker_entered.set)
        _wait_thread_event(release_worker)

    blocker = loop.run_in_executor(pool, occupy_worker)

    original_run_in_executor = loop.run_in_executor

    def observe_submission(executor, function, *args):
        future = original_run_in_executor(executor, function, *args)
        startup_call_queued.set()
        return future

    monkeypatch.setattr(loop, "run_in_executor", observe_submission)

    original_listdir = copies._listdir

    def blocked_listdir(path: Path):
        scans.append(Path(path))
        loop.call_soon_threadsafe(listdir_started.set)
        _wait_thread_event(release_listdir)
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", blocked_listdir)

    def fail_open(_deps):
        open_attempted.set()
        raise StateDatabaseError("controlled locked open failure")

    monkeypatch.setattr(lifecycle, "_open_runtime_connection", fail_open)
    run_task = asyncio.create_task(execute_command(deps, None))
    try:
        await _wait_event(worker_entered)
        await _wait_event(startup_call_queued)
        assert open_attempted.is_set(), "queued startup scan must not delay database open"
        assert scans == []
        assert not run_task.done(), "normal early error must retain the queued scan owner"

        release_worker.set()
        await _wait_task(blocker)
        await _wait_event(listdir_started)
        assert scans == [Path(config.paths.staging) / "logs"]
        assert not run_task.done(), "started scan must settle before SessionLock release"
        duplicate = None
        try:
            duplicate = acquire_session(deps.session_lock)
        except SessionLockConflictError:
            pass
        else:
            pytest.fail("queued startup sweep was not settled under SessionLock")
        finally:
            if duplicate is not None:
                duplicate.close()
        release_listdir.set()
        outcome = await _wait_task(run_task)
    finally:
        release_worker.set()
        release_listdir.set()
        if not blocker.done():
            await _wait_task(blocker)
        if not run_task.done():
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert scans == [Path(config.paths.staging) / "logs"]
    assert not old_copy.exists()
    assert not old_temp.exists()


async def test_cancelled_run_with_queued_sweep_advances_business_and_withdraws(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, _old_temp, _formal_marker, _unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="controlled-default")
    loop.set_default_executor(pool)
    release_worker = threading.Event()
    worker_entered = asyncio.Event()

    def occupy_default_worker():
        loop.call_soon_threadsafe(worker_entered.set)
        _wait_thread_event(release_worker)

    blocker = loop.run_in_executor(pool, occupy_default_worker)

    original_run_in_executor = loop.run_in_executor
    startup_call_queued = asyncio.Event()

    def observe_executor_submission(executor, function, *args):
        future = original_run_in_executor(executor, function, *args)
        startup_call_queued.set()
        return future

    monkeypatch.setattr(loop, "run_in_executor", observe_executor_submission)

    original_listdir = copies._listdir
    scans: list[Path] = []

    def record_listdir(path: Path):
        scans.append(Path(path))
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", record_listdir)

    business_started = asyncio.Event()
    business_stopped = asyncio.Event()
    wait_for_business = asyncio.Event()

    async def controlled_business(_context) -> None:
        business_started.set()
        try:
            await _wait_event(wait_for_business)
        except asyncio.CancelledError:
            business_stopped.set()
            raise

    run_task = asyncio.create_task(
        execute_command(deps, None, flows={"controlled": controlled_business}))
    try:
        # 受控 worker 占满默认线程池后，启动 listdir 已真实提交队列。
        await _wait_event(worker_entered)
        await _wait_event(startup_call_queued)
        assert scans == []
        assert old_copy.exists()

        # 业务流程在同一 SessionLock 内继续推进，不等待 queued 扫描取得线程。
        await _wait_event(business_started)
        run_task.cancel("cancel while startup sweep is queued")
        await _wait_event(business_stopped)
        assert not run_task.done()
        assert old_copy.exists()
        assert scans == []

        # 必要业务停止已交付；随后释放线程池，owner 必须撤回未开始扫描。
        release_worker.set()
        await _wait_task(blocker)
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await _wait_task_result(run_task)
        assert cancelled.value.args == ("cancel while startup sweep is queued",)
        assert scans == []
        assert old_copy.exists()
    finally:
        release_worker.set()
        if not blocker.done():
            await _wait_task(blocker)
        if not run_task.done():
            run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)


async def test_cancel_during_local_drain_withdraws_queued_startup_sweep(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, _old_temp, _formal_marker, _unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="drain-cancel")
    loop.set_default_executor(pool)
    release_worker = threading.Event()
    worker_entered = asyncio.Event()
    startup_call_queued = asyncio.Event()
    scans: list[Path] = []

    def occupy_worker():
        loop.call_soon_threadsafe(worker_entered.set)
        _wait_thread_event(release_worker)

    blocker = loop.run_in_executor(pool, occupy_worker)

    original_run_in_executor = loop.run_in_executor

    def observe_submission(executor, function, *args):
        future = original_run_in_executor(executor, function, *args)
        startup_call_queued.set()
        return future

    monkeypatch.setattr(loop, "run_in_executor", observe_submission)
    original_listdir = copies._listdir

    def record_listdir(path: Path):
        scans.append(Path(path))
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", record_listdir)

    from camctl.logging_runtime.ownership import LoggingOwner

    log_settle_started = asyncio.Event()
    startup_stop_delivered = asyncio.Event()
    original_settle = LoggingOwner.settle
    original_cancel_startup = LoggingOwner.cancel_startup_sweep

    async def observe_settle(owner):
        log_settle_started.set()
        await original_settle(owner)

    def observe_cancel_startup(owner):
        original_cancel_startup(owner)
        startup_stop_delivered.set()

    monkeypatch.setattr(LoggingOwner, "settle", observe_settle)
    monkeypatch.setattr(
        LoggingOwner, "cancel_startup_sweep", observe_cancel_startup)

    def fail_open(_deps):
        raise StateDatabaseError("controlled locked open failure")

    monkeypatch.setattr(lifecycle, "_open_runtime_connection", fail_open)

    run_task = asyncio.create_task(execute_command(deps, None, flows={}))
    try:
        await _wait_event(worker_entered)
        await _wait_event(startup_call_queued)
        await _wait_event(log_settle_started)
        run_task.cancel("cancel while local work drain owns queued sweep")
        await _wait_event(startup_stop_delivered)
        release_worker.set()
        await _wait_task(blocker)
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await _wait_task_result(run_task)
        assert cancelled.value.args == (
            "cancel while local work drain owns queued sweep",)
        assert scans == [], "true cancellation must withdraw queued startup scan"
        assert old_copy.exists()
    finally:
        release_worker.set()
        if not blocker.done():
            await _wait_task(blocker)
        if not run_task.done():
            run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)


async def test_late_clock_winddown_stops_before_blocked_log_settlement(
        tmp_path: Path, monkeypatch) -> None:
    """真实受限录像停止不等待已开始的启动日志目录扫描。"""
    from camctl.bootstrap.flows import winddown_flow
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.session.clock import ClockBecameUntrusted
    from .test_media_assembly import _SessionDriver, _registry
    from .test_restricted_winddown import (
        _FakeWinddownClock, _factory, _recording_in_flight,
    )
    from .test_recording_stop import _RecordCatalog

    driver = _SessionDriver(b"winddown source")
    config, _database, driver, results = await _recording_in_flight(
        tmp_path, "late-clock-log-owner", driver=driver)
    deps = build_runtime(CommandMode.RUN, config, catalog=_RecordCatalog())

    loop = asyncio.get_running_loop()
    listdir_started = asyncio.Event()
    release_listdir = threading.Event()
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    log_settle_started = asyncio.Event()
    phase_observed = asyncio.Event()
    allow_stop_to_finish = asyncio.Event()
    once_report_started = asyncio.Event()
    order: list[str] = []
    logger_state: list[tuple] = []

    original_listdir = copies._listdir

    def blocked_listdir(path: Path):
        loop.call_soon_threadsafe(listdir_started.set)
        _wait_thread_event(release_listdir)
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", blocked_listdir)

    original_stop = driver.stop

    async def observed_stop(request):
        stop_started.set()
        await _wait_event(release_stop)
        return await original_stop(request)

    driver.stop = observed_stop

    original_settle = LoggingOwner.settle
    original_resume = LoggingOwner.resume_restricted_logging
    original_owned_call = LoggingOwner.owned_call

    async def observed_log_settle(owner):
        log_settle_started.set()
        await original_settle(owner)

    def observed_resume(owner):
        original_resume(owner)
        logger_state.append(("resume", owner._stopping.is_set()))

    async def observed_owned_call(owner, stage, operation, *, required=False):
        if stage == "marker_check":
            logger_state.append((
                "marker_check", owner._stopping.is_set(),
                owner._startup_stopping.is_set()))
        return await original_owned_call(
            owner, stage, operation, required=required)

    monkeypatch.setattr(LoggingOwner, "settle", observed_log_settle)
    monkeypatch.setattr(
        LoggingOwner, "resume_restricted_logging", observed_resume)
    monkeypatch.setattr(LoggingOwner, "owned_call", observed_owned_call)

    async def late_clock(_context):
        raise ClockBecameUntrusted("clock changed during ordinary flow")

    async def restricted_report_failure(_context):
        once_report_started.set()
        raise RuntimeError("controlled restricted report failure")

    original_run_session = lifecycle.run_session

    async def run_with_restricted_report(context, source):
        context.once_report = restricted_report_failure
        return await original_run_session(context, source)

    monkeypatch.setattr(lifecycle, "run_session", run_with_restricted_report)

    fake_clock = _FakeWinddownClock()
    restricted_winddown = winddown_flow(
        capture_factory=_factory(
            config, _registry(driver), results, None,
            monotonic_ns=lambda: fake_clock.ns, media_enabled=False),
        wait_cap_s=Decimal("2"),
        sleep=fake_clock.wait,
    )

    async def observe_first_gate() -> None:
        stop_wait = asyncio.create_task(stop_started.wait())
        log_wait = asyncio.create_task(log_settle_started.wait())
        completed, pending = await asyncio.wait_for(
            asyncio.wait(
                (stop_wait, log_wait), return_when=asyncio.FIRST_COMPLETED),
            timeout=_GATE_TIMEOUT,
        )
        for task in pending:
            task.cancel()
        await _wait_task(asyncio.gather(*pending, return_exceptions=True))
        if stop_wait in completed:
            order.append("restricted_stop")
            phase_observed.set()
            await _wait_event(allow_stop_to_finish)
            release_stop.set()
            await _wait_event(log_settle_started)
            order.append("log_settle")
            release_listdir.set()
            return
        order.append("log_settle")
        phase_observed.set()
        release_listdir.set()
        await _wait_event(stop_started)
        order.append("restricted_stop")
        release_stop.set()

    observe_task = asyncio.create_task(observe_first_gate())
    run_task = asyncio.create_task(execute_command(
        deps, None,
        flows={"late_clock": late_clock},
        restricted_flows={"winddown": restricted_winddown},
    ))
    try:
        await _wait_event(listdir_started)
        await _wait_event(phase_observed)
        if order == ["restricted_stop"]:
            assert not release_listdir.is_set()
            assert not once_report_started.is_set()
            allow_stop_to_finish.set()
        outcome = await _wait_task(run_task)
        await _wait_task(observe_task)
    finally:
        release_stop.set()
        release_listdir.set()
        allow_stop_to_finish.set()
        if not run_task.done():
            run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        await _wait_task(asyncio.gather(observe_task, return_exceptions=True))
        close_runtime(deps)

    assert order == ["restricted_stop", "log_settle"]
    assert once_report_started.is_set()
    assert logger_state == [
        ("resume", False), ("marker_check", False, False)]
    assert (Path(config.paths.staging) / "logs" / "failure-marker.json").is_file()
    assert outcome.succeeded is False
    assert outcome.reason == "clock_invalid"


@pytest.mark.parametrize(
    "initial_restricted", [False, True],
    ids=["late_clock", "initial_restricted"],
)
async def test_cancel_during_restricted_stop_preserves_result_before_log_drain(
        tmp_path: Path, monkeypatch, initial_restricted: bool) -> None:
    """初始受限与晚到 clock 的真实 winddown STOP 均不被父取消截断。"""
    from camctl.bootstrap.flows import winddown_flow
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.session.clock import ClockBecameUntrusted
    from camctl.session.locks import probe_admission
    from .test_media_assembly import _SessionDriver, _registry
    from .test_restricted_winddown import (
        _FakeWinddownClock, _factory, _recording_in_flight,
    )
    from .test_recording_stop import _RecordCatalog, _entry, _scalar

    driver = _SessionDriver(b"restricted stop source")
    config, database, driver, results = await _recording_in_flight(
        tmp_path, "cancelled-restricted-stop", driver=driver)
    results.files_by_action[1] = (_entry("clip-1", size=len(b"restricted stop source")),)
    deps = build_runtime(
        CommandMode.RUN, config, catalog=_RecordCatalog(),
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    if initial_restricted:
        class UntrustedClock:
            def utc_micros(self) -> int:
                return 1

            def monotonic_ns(self) -> int:
                return 0

        monkeypatch.setattr(lifecycle, "SystemClock", UntrustedClock)

    loop = asyncio.get_running_loop()
    listdir_started = asyncio.Event()
    release_listdir = threading.Event()
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    stop_cancelled = asyncio.Event()
    ordinary_stop_delivered = asyncio.Event()
    startup_stop_delivered = asyncio.Event()
    log_settle_started = asyncio.Event()
    once_report_started = asyncio.Event()
    opened_connections: list[int] = []
    closed_connections: list[int] = []
    log_settle_facts: list[tuple] = []
    stop_entries: list[object] = []
    ordinary_stop_calls: list[int] = []
    startup_stop_calls: list[int] = []
    original_listdir = copies._listdir

    def blocked_listdir(path: Path):
        loop.call_soon_threadsafe(listdir_started.set)
        _wait_thread_event(release_listdir)
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", blocked_listdir)

    original_stop = driver.stop

    async def gated_stop(request):
        stop_entries.append(request)
        stop_started.set()
        try:
            await _wait_event(release_stop)
        except asyncio.CancelledError:
            stop_cancelled.set()
            raise
        return await original_stop(request)

    driver.stop = gated_stop

    original_local_stop = LoggingOwner.stop_new_work
    original_startup_stop = LoggingOwner.cancel_startup_sweep
    original_log_settle = LoggingOwner.settle

    def observe_local_stop(owner):
        original_local_stop(owner)
        ordinary_stop_calls.append(len(ordinary_stop_calls) + 1)
        ordinary_stop_delivered.set()

    def observe_startup_stop(owner):
        original_startup_stop(owner)
        startup_stop_calls.append(len(startup_stop_calls) + 1)
        startup_stop_delivered.set()

    async def observe_log_settle(owner):
        log_settle_facts.append((
            _scalar(database,
                    "SELECT status, attempts_used FROM operation_runs"
                    " WHERE responsibility_key = 'stop/1'"),
            _scalar(database,
                    "SELECT check_decision FROM recording_processing"
                    " WHERE action_id = 1"),
            tuple(opened_connections),
            tuple(closed_connections),
        ))
        log_settle_started.set()
        await original_log_settle(owner)

    monkeypatch.setattr(LoggingOwner, "stop_new_work", observe_local_stop)
    monkeypatch.setattr(
        LoggingOwner, "cancel_startup_sweep", observe_startup_stop)
    monkeypatch.setattr(LoggingOwner, "settle", observe_log_settle)

    original_run_session = lifecycle.run_session

    async def observe_session(context, source):
        original_open = context.open_connection

        def tracked_open():
            owned = original_open()
            identity = len(opened_connections) + 1
            opened_connections.append(identity)
            connection = owned.connection

            class ConnectionCloseSpy:
                def __getattr__(self, name):
                    return getattr(connection, name)

                def close(self) -> None:
                    closed_connections.append(identity)
                    connection.close()

            return type(owned)(
                connection=ConnectionCloseSpy(), metadata=owned.metadata)

        context.open_connection = tracked_open

        async def observe_once_report(_context):
            once_report_started.set()

        context.once_report = observe_once_report
        return await original_run_session(context, source)

    monkeypatch.setattr(lifecycle, "run_session", observe_session)

    fake_clock = _FakeWinddownClock()
    restricted_winddown = winddown_flow(
        capture_factory=_factory(
            config, _registry(driver), results, None,
            monotonic_ns=lambda: fake_clock.ns, media_enabled=False),
        wait_cap_s=Decimal("2"), sleep=fake_clock.wait,
    )

    async def lose_clock(_context) -> None:
        raise ClockBecameUntrusted("controlled ordinary-to-restricted transition")

    flows = {} if initial_restricted else {"late_clock": lose_clock}
    run_task = asyncio.create_task(execute_command(
        deps, None, flows=flows, restricted_flows={"winddown": restricted_winddown}))

    def assert_session_lock_held() -> None:
        duplicate = None
        try:
            duplicate = acquire_session(deps.session_lock)
        except SessionLockConflictError:
            return
        finally:
            if duplicate is not None:
                duplicate.close()
        pytest.fail("必要 winddown 或日志诊断收场期间提前释放 SessionLock")

    try:
        await _wait_event(listdir_started)
        await _wait_event(stop_started)
        assert len(stop_entries) == 1
        assert not log_settle_started.is_set()
        assert not stop_cancelled.is_set()
        assert_session_lock_held()
        assert probe_admission(deps.admission_lock).status.value == (
            "acquired_and_released")

        run_task.cancel("parent during restricted winddown STOP")
        await _wait_event(ordinary_stop_delivered)
        await _wait_event(startup_stop_delivered)
        first_cancel_stop_counts = (
            len(ordinary_stop_calls), len(startup_stop_calls))
        assert first_cancel_stop_counts[0] >= 1
        assert first_cancel_stop_counts[1] >= 1
        assert not stop_cancelled.is_set()
        assert not log_settle_started.is_set()
        assert not run_task.done()
        assert_session_lock_held()

        run_task.cancel("repeated parent during restricted winddown STOP")
        await asyncio.sleep(0)
        assert len(stop_entries) == 1
        assert (
            len(ordinary_stop_calls), len(startup_stop_calls)
        ) == first_cancel_stop_counts
        assert not stop_cancelled.is_set()

        release_stop.set()
        await _wait_event(log_settle_started)
        assert log_settle_facts == [((3, 1), (3,), (1, 2), (2,))]
        assert not once_report_started.is_set()
        assert not stop_cancelled.is_set()
        assert_session_lock_held()
        assert len([call for call in driver.calls if call[0] == "stop"]) == 1

        release_listdir.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await _wait_task_result(run_task)
        assert cancelled.value.args == ("parent during restricted winddown STOP",)
        assert len(stop_entries) == 1
        assert not stop_cancelled.is_set()
        assert not once_report_started.is_set()
    finally:
        release_stop.set()
        release_listdir.set()
        if not run_task.done():
            run_task.cancel("restricted winddown test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)


async def test_cancel_during_open_admission_drain_stops_all_new_work(
        tmp_path: Path, monkeypatch) -> None:
    """取消开放收场时先停止业务 owner，再等待 queued 日志扫描收场。"""
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.logging_runtime.copies import MarkerStore
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.session import service as session_service
    from camctl.session.supervision import Supervisor

    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, _old_temp, _formal_marker, _unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="open-drain-cancel")
    loop.set_default_executor(pool)
    release_worker = threading.Event()
    worker_entered = asyncio.Event()
    startup_queued = asyncio.Event()
    scans: list[Path] = []

    def occupy_worker():
        loop.call_soon_threadsafe(worker_entered.set)
        _wait_thread_event(release_worker)

    blocker = loop.run_in_executor(pool, occupy_worker)
    original_run_in_executor = loop.run_in_executor

    def observe_submission(executor, function, *args):
        future = original_run_in_executor(executor, function, *args)
        startup_queued.set()
        return future

    monkeypatch.setattr(loop, "run_in_executor", observe_submission)
    original_listdir = copies._listdir

    def record_listdir(path: Path):
        scans.append(Path(path))
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", record_listdir)

    business_settle_started = asyncio.Event()
    release_business_settle = asyncio.Event()
    business_stop_delivered = asyncio.Event()
    ordinary_stop_delivered = asyncio.Event()
    startup_stop_delivered = asyncio.Event()
    log_settle_started = asyncio.Event()
    order: list[str] = []

    class ControlledOwner:
        def required_settlements(self) -> int:
            return 0

        def stop_new_work(self) -> None:
            order.append("business_stop")
            business_stop_delivered.set()

        async def settle(self) -> None:
            business_settle_started.set()
            await _wait_event(release_business_settle)

    marker = MarkerStore(Path(config.paths.staging) / "logs")
    owner = LoggingOwner(
        logs_dir=marker.directory, ownership=marker.ownership,
        supervisor=Supervisor(), owner_settled=True,
        diagnose=lambda _message: asyncio.sleep(0),
    )
    lease = acquire_session(deps.session_lock)
    await owner.start_session()
    local_work = CombinedLocalWork((ControlledOwner(), owner))
    original_stop_new = LoggingOwner.stop_new_work
    original_cancel_sweep = LoggingOwner.cancel_startup_sweep
    original_log_settle = LoggingOwner.settle

    def observe_ordinary_stop(owner):
        original_stop_new(owner)
        order.append("logging_stop")
        ordinary_stop_delivered.set()

    def observe_startup_stop(owner):
        original_cancel_sweep(owner)
        order.append("startup_stop")
        startup_stop_delivered.set()

    async def observe_log_settle(owner):
        order.append("log_settle")
        log_settle_started.set()
        await original_log_settle(owner)

    monkeypatch.setattr(LoggingOwner, "stop_new_work", observe_ordinary_stop)
    monkeypatch.setattr(
        LoggingOwner, "cancel_startup_sweep", observe_startup_stop)
    monkeypatch.setattr(LoggingOwner, "settle", observe_log_settle)

    settle_task = asyncio.create_task(session_service._settle_local_work(
        type("Context", (), {
            "local_work": local_work, "logging_owner": owner,
        })(),
        stop_new=False,
    ))
    try:
        await _wait_event(worker_entered)
        await _wait_event(startup_queued)
        await _wait_event(business_settle_started)
        assert scans == []
        assert not settle_task.done()

        settle_task.cancel("cancel while open local-work drain is waiting")
        await _wait_event(startup_stop_delivered)
        assert business_stop_delivered.is_set()
        assert ordinary_stop_delivered.is_set()
        assert not log_settle_started.is_set()
        assert order == ["business_stop", "logging_stop", "startup_stop"]

        # 受控业务收场仍未完成时，日志拥有者尚未进入最终 drain。
        release_business_settle.set()
        await _wait_event(log_settle_started)
        assert order == [
            "business_stop", "logging_stop", "startup_stop", "log_settle"]
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)

        release_worker.set()
        await _wait_task(blocker)
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await _wait_task_result(settle_task)
        assert cancelled.value.args == (
            "cancel while open local-work drain is waiting",)
        assert scans == []
        assert old_copy.exists()
    finally:
        release_worker.set()
        release_business_settle.set()
        owner.stop_new_work()
        owner.cancel_startup_sweep()
        if not blocker.done():
            await _wait_task(blocker)
        if not settle_task.done():
            settle_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(settle_task, return_exceptions=True))
        lease.close()
        await _wait_task(owner.settle())
        close_runtime(deps)


async def test_cancelled_started_unlink_keeps_lock_and_stops_next_candidate(
        tmp_path: Path, monkeypatch) -> None:
    """取消不撤销已开始的删除，且其诊断完成前保留 SessionLock。"""
    from camctl.logging_runtime.ownership import LoggingOwner

    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    logs_dir = Path(config.paths.staging) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    first = logs_dir / "log-copy-first.log"
    second = logs_dir / "log-copy-second.log"
    first.write_bytes(b"first old copy")
    second.write_bytes(b"second old copy")
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )

    loop = asyncio.get_running_loop()
    first_unlink_started = asyncio.Event()
    release_first_unlink = threading.Event()
    diagnostics_started = asyncio.Event()
    release_diagnostic = asyncio.Event()
    startup_stop_delivered = asyncio.Event()
    unlink_calls: list[Path] = []
    diagnostics: list[str] = []
    original_listdir = copies._listdir
    original_unlink = copies._unlink

    monkeypatch.setattr(
        copies, "_listdir", lambda path: (first.name, second.name)
        if Path(path) == logs_dir else original_listdir(path),
    )

    def controlled_unlink(path: Path) -> None:
        path = Path(path)
        unlink_calls.append(path)
        if path == first:
            loop.call_soon_threadsafe(first_unlink_started.set)
            _wait_thread_event(release_first_unlink)
            raise OSError("controlled started unlink failure")
        original_unlink(path)

    monkeypatch.setattr(copies, "_unlink", controlled_unlink)
    original_cancel_startup = LoggingOwner.cancel_startup_sweep

    def observe_cancel_startup(owner):
        original_cancel_startup(owner)
        startup_stop_delivered.set()

    monkeypatch.setattr(
        LoggingOwner, "cancel_startup_sweep", observe_cancel_startup)

    async def block_diagnostic(_deps, message: str, *, stderr_only=False) -> None:
        diagnostics.append(message)
        diagnostics_started.set()
        await _wait_event(release_diagnostic)

    monkeypatch.setattr(lifecycle, "_emit_log_diagnostic", block_diagnostic)

    def assert_session_lock_held() -> None:
        duplicate = None
        try:
            duplicate = acquire_session(deps.session_lock)
        except SessionLockConflictError:
            return
        finally:
            if duplicate is not None:
                duplicate.close()
        pytest.fail("startup unlink 结果或诊断交付前提前释放 SessionLock")

    run_task = asyncio.create_task(execute_command(deps, None, flows={}))
    try:
        await _wait_event(first_unlink_started)
        assert unlink_calls == [first]
        assert_session_lock_held()
        run_task.cancel("cancel while startup unlink is started")
        await _wait_event(startup_stop_delivered)
        release_first_unlink.set()
        await _wait_event(diagnostics_started)
        assert not run_task.done()
        assert unlink_calls == [first]
        assert first.exists()
        assert second.exists()
        assert len(diagnostics) == 1
        assert "controlled started unlink failure" in diagnostics[0]
        assert_session_lock_held()

        release_diagnostic.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await _wait_task_result(run_task)
        assert cancelled.value.args == ("cancel while startup unlink is started",)
        assert unlink_calls == [first]
        assert first.exists()
        assert second.exists()
    finally:
        release_first_unlink.set()
        release_diagnostic.set()
        if not run_task.done():
            run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)


async def test_initial_restricted_run_sweeps_once(
        tmp_path: Path, monkeypatch) -> None:
    """初始时钟受限仍完成一次扫描、必要停止和唯一报告机会。"""
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, _unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="restricted-sweep")
    loop.set_default_executor(pool)
    release_worker = threading.Event()
    worker_entered = asyncio.Event()
    startup_queued = asyncio.Event()

    def occupy_worker():
        loop.call_soon_threadsafe(worker_entered.set)
        _wait_thread_event(release_worker)

    blocker = loop.run_in_executor(pool, occupy_worker)
    original_run_in_executor = loop.run_in_executor

    def observe_submission(executor, function, *args):
        future = original_run_in_executor(executor, function, *args)
        startup_queued.set()
        return future

    monkeypatch.setattr(loop, "run_in_executor", observe_submission)
    scan_started = asyncio.Event()
    release_scan = threading.Event()
    restricted_stop = asyncio.Event()
    once_report = asyncio.Event()
    scans: list[Path] = []
    order: list[str] = []
    original_listdir = copies._listdir

    def blocked_listdir(path: Path):
        scans.append(Path(path))
        order.append("scan_started")
        loop.call_soon_threadsafe(scan_started.set)
        _wait_thread_event(release_scan)
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", blocked_listdir)

    class UntrustedClock:
        def utc_micros(self) -> int:
            return 1

    monkeypatch.setattr(lifecycle, "SystemClock", UntrustedClock)
    original_run_session = lifecycle.run_session

    async def install_once_report(context, source):
        async def report_once(_context) -> None:
            order.append("once_report")
            once_report.set()

        context.once_report = report_once
        return await original_run_session(context, source)

    monkeypatch.setattr(lifecycle, "run_session", install_once_report)

    async def stop_required_work(_context) -> None:
        order.append("restricted_stop")
        restricted_stop.set()

    run_task = asyncio.create_task(execute_command(
        deps, None, flows={}, restricted_flows={"stop": stop_required_work}))
    try:
        await _wait_event(worker_entered)
        await _wait_event(startup_queued)
        await _wait_event(restricted_stop)
        await _wait_event(once_report)
        assert order == ["restricted_stop", "once_report"]
        assert scans == []
        assert not run_task.done()
        duplicate = None
        try:
            duplicate = acquire_session(deps.session_lock)
        except SessionLockConflictError:
            pass
        else:
            pytest.fail("受限报告仍执行时提前释放了 SessionLock")
        finally:
            if duplicate is not None:
                duplicate.close()

        release_worker.set()
        await _wait_task(blocker)
        await _wait_event(scan_started)
        assert order == ["restricted_stop", "once_report", "scan_started"]
        assert scans == [Path(config.paths.staging) / "logs"]
        assert not run_task.done()
        release_scan.set()
        outcome = await _wait_task(run_task)
    finally:
        release_worker.set()
        release_scan.set()
        if not blocker.done():
            await _wait_task(blocker)
        if not run_task.done():
            run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "clock_invalid"
    assert order.count("restricted_stop") == 1
    assert order.count("once_report") == 1
    assert scans == [Path(config.paths.staging) / "logs"]
    assert not old_copy.exists()
    assert not old_temp.exists()
    assert formal_marker.exists()


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param("copy-name", id="copy-name"),
        pytest.param("temp-name", id="marker-temp-name"),
        pytest.param("relative-segments", id="relative-segments"),
        pytest.param("directory-alias", id="directory-symlink-alias"),
        pytest.param("file-alias", id="file-symlink-alias"),
        pytest.param("file-alias-replaced", id="file-alias-position-replaced"),
        pytest.param("other-directory", id="same-name-other-directory"),
    ],
)
async def test_configured_ordinary_log_survives_startup_sweep(
        tmp_path: Path, monkeypatch, scenario: str) -> None:
    """默认 run 按实际日志目标保护候选名，并继续删除其他旧候选。"""
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogRecord

    logs_dir = tmp_path / "staging" / "logs"
    logs_dir.mkdir(parents=True)
    prefix = b"SW04 ordinary log original bytes\n"
    replacement_prefix = b"SW04 replacement config-position bytes\n"
    replacement_after_resolve = scenario == "file-alias-replaced"
    alias_path: Path | None = None
    resolved_target: Path | None = None
    ordinary_log_path: Path
    ordinary_log_file: Path
    expected_deletes: list[Path] = []
    append_target: Path | None

    if scenario == "copy-name":
        ordinary_log_file = logs_dir / "log-copy-configured.log"
        ordinary_log_path = ordinary_log_file
        append_target = ordinary_log_file
    elif scenario == "temp-name":
        ordinary_log_file = logs_dir / "failure-marker.json.ordinary.tmp"
        ordinary_log_path = ordinary_log_file
        append_target = ordinary_log_file
    elif scenario == "relative-segments":
        nested = logs_dir / "nested"
        nested.mkdir()
        ordinary_log_file = logs_dir / "log-copy-relative.log"
        ordinary_log_path = nested / ".." / ordinary_log_file.name
        append_target = ordinary_log_file
    elif scenario == "directory-alias":
        alias_dir = tmp_path / "logs-directory-alias"
        _symlink_or_skip(alias_dir, logs_dir, target_is_directory=True)
        ordinary_log_file = logs_dir / "log-copy-directory-alias.log"
        ordinary_log_path = alias_dir / ordinary_log_file.name
        append_target = ordinary_log_file
    elif scenario == "file-alias":
        ordinary_log_file = logs_dir / "failure-marker.json.file-target.tmp"
        ordinary_log_path = logs_dir / "log-copy-file-alias.log"
        _symlink_or_skip(ordinary_log_path, ordinary_log_file)
        append_target = ordinary_log_file
    elif replacement_after_resolve:
        ordinary_log_file = logs_dir / "failure-marker.json.resolved-target.tmp"
        alias_path = logs_dir / "log-copy-config-position.log"
        ordinary_log_path = alias_path
        _symlink_or_skip(alias_path, ordinary_log_file)
        resolved_target = ordinary_log_file.resolve()
        append_target = None
    elif scenario == "other-directory":
        other_dir = tmp_path / "other-log-directory"
        other_dir.mkdir()
        ordinary_log_file = other_dir / "log-copy-same-name.log"
        ordinary_log_path = ordinary_log_file
        same_name_candidate = logs_dir / ordinary_log_file.name
        same_name_candidate.write_bytes(b"old candidate in scanned directory")
        expected_deletes.append(same_name_candidate)
        append_target = ordinary_log_file
    else:  # pragma: no cover - pytest parameter list is exhaustive
        raise AssertionError(f"unknown ordinary-log scenario: {scenario}")

    ordinary_log_file.parent.mkdir(parents=True, exist_ok=True)
    ordinary_log_file.write_bytes(prefix)
    config = _config_for(tmp_path, log_file=ordinary_log_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    expected_deletes.extend((old_copy, old_temp))

    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    run_task: asyncio.Task | None = None
    loop = asyncio.get_running_loop()
    scan_finished = asyncio.Event()
    release_scan = threading.Event()
    enumeration_started = asyncio.Event()
    release_enumeration = threading.Event()
    unlinked: list[Path] = []
    sweep_calls: list[tuple[tuple, dict]] = []
    session_lock_during_scan: list[bool] = []
    resolved_paths: list[Path] = []
    resolution_preceded_enumeration: list[bool] = []
    original_unlink = copies._unlink
    original_listdir = copies._listdir
    original_sweep = copies.startup_sweep
    original_resolve = Path.resolve

    async def deliver(message: str) -> None:
        result = await deps.log_runtime.channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message=message))
        assert result.delivered is True

    def lock_is_held() -> bool:
        duplicate = None
        try:
            duplicate = acquire_session(deps.session_lock)
        except SessionLockConflictError:
            return True
        finally:
            if duplicate is not None:
                duplicate.close()
        return False

    def record_unlink(path: Path) -> None:
        unlinked.append(Path(path))
        original_unlink(path)

    def observe_sweep(*args, **kwargs):
        result = original_sweep(*args, **kwargs)
        sweep_calls.append((args, dict(kwargs)))
        session_lock_during_scan.append(lock_is_held())
        loop.call_soon_threadsafe(scan_finished.set)
        _wait_thread_event(release_scan)
        return result

    def observe_resolved_log_target(path: Path, *args, **kwargs):
        result = original_resolve(path, *args, **kwargs)
        if (
                replacement_after_resolve
                and Path(path) == ordinary_log_path
                and threading.current_thread() is not threading.main_thread()):
            resolved_paths.append(Path(result))
        return result

    def block_before_directory_enumeration(path: Path):
        if (replacement_after_resolve
                and Path(path) == logs_dir
                and not resolution_preceded_enumeration):
            resolution_preceded_enumeration.append(bool(resolved_paths))
            loop.call_soon_threadsafe(enumeration_started.set)
            _wait_thread_event(release_enumeration)
        return original_listdir(path)

    try:
        # 先通过真实日志通道打开并追加；文件别名竞态随后替换配置位置时，
        # 原文件句柄仍代表此前解析出的实际普通日志目标。
        prime = f"SW04 ordinary log priming record: {scenario}"
        await deliver(prime)
        assert prefix in ordinary_log_file.read_bytes()
        assert prime.encode("utf-8") in ordinary_log_file.read_bytes()

        monkeypatch.setattr(copies, "_unlink", record_unlink)
        monkeypatch.setattr(copies, "startup_sweep", observe_sweep)
        if replacement_after_resolve:
            monkeypatch.setattr(Path, "resolve", observe_resolved_log_target)
            monkeypatch.setattr(
                copies, "_listdir", block_before_directory_enumeration)

        run_task = asyncio.create_task(execute_command(deps, None, flows={}))
        if replacement_after_resolve:
            await _wait_event(enumeration_started)
            assert resolved_paths
            assert all(path == resolved_target for path in resolved_paths)
            assert resolution_preceded_enumeration == [True]
            assert alias_path is not None and alias_path.is_symlink()
            alias_path.unlink()
            alias_path.write_bytes(replacement_prefix)
            release_enumeration.set()

        await _wait_event(scan_finished)
        assert not run_task.done()
        assert len(sweep_calls) == 1
        assert sweep_calls[0][1].get("ordinary_log_path") == (
            Path(config.paths.log_file).expanduser())
        assert session_lock_during_scan == [True]
        assert Counter(unlinked) == Counter(expected_deletes)
        for path in expected_deletes:
            assert not path.exists()

        assert ordinary_log_file.exists()
        assert prefix in ordinary_log_file.read_bytes()
        assert formal_marker.exists()
        assert unknown.exists()
        if replacement_after_resolve:
            assert alias_path is not None and alias_path.read_bytes().startswith(
                replacement_prefix)
            assert resolved_target is not None
            assert resolved_target.read_bytes().startswith(prefix)
        else:
            assert ordinary_log_path.exists()

        if append_target is not None:
            later = f"SW04 ordinary log append after sweep: {scenario}"
            await deliver(later)
            assert prefix in append_target.read_bytes()
            assert later.encode("utf-8") in append_target.read_bytes()

        release_scan.set()
        outcome = await _wait_task_result(run_task)
    finally:
        release_enumeration.set()
        release_scan.set()
        if run_task is not None:
            if not run_task.done():
                run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)

    assert outcome.succeeded is True


async def test_unknown_ordinary_log_target_preserves_startup_candidates(
        tmp_path: Path, monkeypatch) -> None:
    """普通日志位置无法可靠解析时，真实扫描保留全部候选并交付诊断。"""
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogRecord

    logs_dir = tmp_path / "staging" / "logs"
    logs_dir.mkdir(parents=True)
    ordinary_log_path = logs_dir / "log-copy-unknown-target.log"
    prefix = b"SW04 ordinary log before controlled resolve failure\n"
    failure_marker = "controlled ordinary log target resolve denial"
    ordinary_log_path.write_bytes(prefix)
    config = _config_for(tmp_path, log_file=ordinary_log_path)
    assert initialize_state(
        config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )

    run_task: asyncio.Task | None = None
    original_resolve = Path.resolve
    original_sweep = copies.startup_sweep
    original_unlink = copies._unlink
    original_diagnostic = lifecycle._emit_log_diagnostic
    resolve_threads: list[threading.Thread] = []
    sweep_calls: list[tuple[tuple, dict]] = []
    unlinked: list[Path] = []
    diagnostics: list[str] = []
    session_lock_during_scan: list[bool] = []
    session_lock_during_diagnostic: list[bool] = []

    def lock_is_held() -> bool:
        duplicate = None
        try:
            duplicate = acquire_session(deps.session_lock)
        except SessionLockConflictError:
            return True
        finally:
            if duplicate is not None:
                duplicate.close()
        return False

    def fail_ordinary_target_resolution(path: Path, *args, **kwargs):
        if (Path(path) == ordinary_log_path
                and threading.current_thread() is not threading.main_thread()):
            resolve_threads.append(threading.current_thread())
            raise PermissionError(failure_marker)
        return original_resolve(path, *args, **kwargs)

    def observe_sweep(*args, **kwargs):
        result = original_sweep(*args, **kwargs)
        sweep_calls.append((args, dict(kwargs)))
        session_lock_during_scan.append(lock_is_held())
        return result

    def record_unlink(path: Path) -> None:
        unlinked.append(Path(path))
        original_unlink(path)

    async def observe_diagnostic(
            deps_arg, message: str, *, stderr_only: bool = False) -> None:
        session_lock_during_diagnostic.append(lock_is_held())
        await original_diagnostic(
            deps_arg, message, stderr_only=stderr_only)
        diagnostics.append(message)

    try:
        prime = "SW04 ordinary log priming before unknown-path scan"
        result = await deps.log_runtime.channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message=prime))
        assert result.delivered is True
        assert prefix in ordinary_log_path.read_bytes()

        monkeypatch.setattr(Path, "resolve", fail_ordinary_target_resolution)
        monkeypatch.setattr(copies, "startup_sweep", observe_sweep)
        monkeypatch.setattr(copies, "_unlink", record_unlink)
        monkeypatch.setattr(
            lifecycle, "_emit_log_diagnostic", observe_diagnostic)
        run_task = asyncio.create_task(execute_command(deps, None, flows={}))
        outcome = await _wait_task_result(run_task)
    finally:
        if run_task is not None:
            if not run_task.done():
                run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)

    assert outcome.succeeded is True
    assert len(resolve_threads) == 1
    assert resolve_threads[0] is not threading.main_thread()
    assert len(sweep_calls) == 1
    assert sweep_calls[0][1].get("ordinary_log_path") == (
        Path(config.paths.log_file).expanduser())
    assert session_lock_during_scan == [True]
    assert unlinked == []
    assert old_copy.exists()
    assert old_temp.exists()
    assert formal_marker.exists()
    assert unknown.exists()
    assert prefix in ordinary_log_path.read_bytes()
    assert len(diagnostics) == 1
    assert session_lock_during_diagnostic == [True]
    assert failure_marker in diagnostics[0]
    assert diagnostics[0].encode("utf-8") in ordinary_log_path.read_bytes()
