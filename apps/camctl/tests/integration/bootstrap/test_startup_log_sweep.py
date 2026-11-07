"""SW-04 run 启动日志扫描的真实数据库、锁与目录边界。"""

from __future__ import annotations

import asyncio
import errno
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

from camctl.acceptance.input import FileInputReader, ParsedInput, parse_input, read_input
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap import lifecycle
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.recovery import RecoveryBoundary
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


def _record_directory_scans(monkeypatch, logs_dir: Path) -> list[Path]:
    scans: list[Path] = []
    original_listdir = os.listdir

    def record_listdir(path):
        if Path(path) == logs_dir:
            scans.append(Path(path))
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", record_listdir)
    return scans


async def _run(config, *, boundary: RecoveryBoundary | None = None):
    kwargs = {} if boundary is None else {"recovery_boundary": boundary}
    deps = build_runtime(CommandMode.RUN, config, **kwargs)
    try:
        return await execute_command(deps, None)
    finally:
        close_runtime(deps)


async def test_host_settled_run_sweeps_under_session_lock(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    loop = asyncio.get_running_loop()
    scan_started = asyncio.Event()
    release_scan = threading.Event()
    original_listdir = os.listdir
    logs_dir = old_copy.parent

    def blocked_listdir(path):
        if Path(path) == logs_dir:
            loop.call_soon_threadsafe(scan_started.set)
            _wait_thread_event(release_scan)
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", blocked_listdir)
    task = asyncio.create_task(execute_command(deps, None, flows={}))
    try:
        await _wait_event(scan_started)
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)
        assert not task.done()
        release_scan.set()
        outcome = await _wait_task(task)
    finally:
        release_scan.set()
        if not task.done():
            task.cancel()
            await _wait_task(asyncio.gather(task, return_exceptions=True))
        close_runtime(deps)

    assert outcome.succeeded is True
    assert not old_copy.exists()
    assert not old_temp.exists()
    assert formal_marker.exists()
    assert unknown.exists()
    lease = acquire_session(deps.session_lock)
    lease.close()


async def test_failed_old_copy_delete_is_retried_by_next_run(
        tmp_path: Path, monkeypatch, capfd) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    original_unlink = os.unlink
    failure_marker = "controlled old-copy unlink failure"
    attempts: list[Path] = []

    def deny_old_copy(path, *args, **kwargs):
        if Path(path) == old_copy:
            attempts.append(Path(path))
            if len(attempts) == 1:
                raise PermissionError(failure_marker)
        return original_unlink(path, *args, **kwargs)

    async def run_without_report_recovery():
        deps = build_runtime(
            CommandMode.RUN, config,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        )
        try:
            return await execute_command(deps, None, flows={})
        finally:
            close_runtime(deps)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", deny_old_copy)
        first = await run_without_report_recovery()
    assert first.succeeded is True
    assert attempts == [old_copy]
    assert old_copy.exists()
    assert not old_temp.exists()
    assert formal_marker.exists() and unknown.exists()
    diagnostics = Path(config.paths.log_file).read_text() + capfd.readouterr().err
    assert failure_marker in diagnostics

    second = await run_without_report_recovery()
    assert second.succeeded is True
    assert not old_copy.exists()
    assert formal_marker.exists() and unknown.exists()


async def test_session_lock_conflict_stops_before_startup_sweep(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    scans = _record_directory_scans(monkeypatch, old_copy.parent)
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    held = acquire_session(deps.session_lock)
    try:
        outcome = await execute_command(deps, None)
    finally:
        held.close()
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "lock_error"
    assert scans == []
    assert all(path.exists() for path in (old_copy, old_temp, formal_marker, unknown))


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


async def test_existing_bad_database_sweeps_under_session_lock(
        tmp_path: Path, monkeypatch) -> None:
    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy, old_temp, formal_marker, unknown = _seed_log_paths(config)
    Path(config.paths.state_db).write_bytes(b"not a SQLite database")
    deps = build_runtime(
        CommandMode.RUN, config,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    loop = asyncio.get_running_loop()
    scan_started = asyncio.Event()
    release_scan = threading.Event()
    original_listdir = os.listdir
    original_connect = sqlite3.connect
    database_opens = []

    def record_connect(*args, **kwargs):
        database_opens.append(args[0])
        return original_connect(*args, **kwargs)

    def blocked_listdir(path):
        if Path(path) == old_copy.parent:
            loop.call_soon_threadsafe(scan_started.set)
            _wait_thread_event(release_scan)
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", blocked_listdir)
    monkeypatch.setattr(sqlite3, "connect", record_connect)
    task = asyncio.create_task(execute_command(deps, None))
    try:
        await _wait_event(scan_started)
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)
        assert not task.done()
        release_scan.set()
        outcome = await _wait_task(task)
    finally:
        release_scan.set()
        if not task.done():
            task.cancel()
            await _wait_task(asyncio.gather(task, return_exceptions=True))
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert database_opens == []
    assert not old_copy.exists() and not old_temp.exists()
    assert formal_marker.exists() and unknown.exists()
    lease = acquire_session(deps.session_lock)
    lease.close()


@pytest.mark.parametrize("database_state", ["missing", "unknown", "nonregular"])
async def test_ineligible_database_preserves_startup_candidates(
        tmp_path: Path, monkeypatch, database_state: str) -> None:
    config = _config_for(tmp_path)
    candidates = _seed_log_paths(config)
    scans = _record_directory_scans(monkeypatch, candidates[0].parent)
    state_path = Path(config.paths.state_db).expanduser().resolve()
    if database_state == "nonregular":
        state_path.mkdir()
    elif database_state == "unknown":
        state_path.write_bytes(b"existence unknown")

    original_stat = Path.stat

    def deny_state_db_stat(path, *args, **kwargs):
        if path == state_path:
            raise PermissionError(errno.EACCES, "state database stat denied", str(path))
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        if database_state == "unknown":
            patch.setattr(Path, "stat", deny_state_db_stat)
        deps = build_runtime(
            CommandMode.RUN, config,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        )
    try:
        outcome = await execute_command(deps, None)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert scans == []
    assert all(path.exists() for path in candidates)
    lease = acquire_session(deps.session_lock)
    lease.close()


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
    recovery_read_denied = asyncio.Event()
    original_listdir = os.listdir
    calls: list[Path] = []

    def blocked_listdir(path: Path):
        if Path(path) == Path(config.paths.staging) / "logs":
            calls.append(Path(path))
            loop.call_soon_threadsafe(listdir_started.set)
            _wait_thread_event(release_listdir)
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", blocked_listdir)
    source = None
    if early_failure == "open":
        def fail_open(*_args, **_kwargs):
            raise sqlite3.OperationalError("controlled locked open failure")

        monkeypatch.setattr(sqlite3, "connect", fail_open)
    elif early_failure == "on_session_open":
        original_connect = sqlite3.connect
        database_uri = Path(config.paths.state_db).resolve().as_uri()

        def connect_with_recovery_read_denied(database, *args, **kwargs):
            connection = original_connect(database, *args, **kwargs)
            if str(database).partition("?")[0] == database_uri:
                def authorize(action, table, _column, _database, _trigger):
                    if action == sqlite3.SQLITE_READ and table == "software_recoveries":
                        loop.call_soon_threadsafe(recovery_read_denied.set)
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(authorize)
            return connection

        monkeypatch.setattr(sqlite3, "connect", connect_with_recovery_read_denied)
    else:
        from camctl.session import service as session_service

        monkeypatch.setattr(
            session_service, "accept_input",
            lambda *_args: (_ for _ in ()).throw(
                StateDatabaseError("controlled acceptance failure")),
        )
        input_path = tmp_path / "plan.json"
        input_path.write_bytes(
            b'{"request_id":"1","created_at":"2026-01-15 08:00:00",'
            b'"name":"status","actions":[{"name":"full status",'
            b'"type":"report_status","params":{"scope":"full"}}]}'
        )
        source = parse_input(await read_input(str(input_path), FileInputReader()))
        assert isinstance(source, ParsedInput)

    run_task = asyncio.create_task(execute_command(deps, source))
    try:
        await _wait_event(listdir_started)
        if early_failure == "on_session_open":
            await _wait_event(recovery_read_denied)
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
    open_attempted = asyncio.Event()
    scans: list[Path] = []

    def occupy_worker():
        loop.call_soon_threadsafe(worker_entered.set)
        _wait_thread_event(release_worker)

    blocker = loop.run_in_executor(pool, occupy_worker)

    original_listdir = os.listdir

    def blocked_listdir(path: Path):
        if Path(path) == Path(config.paths.staging) / "logs":
            scans.append(Path(path))
            loop.call_soon_threadsafe(listdir_started.set)
            _wait_thread_event(release_listdir)
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", blocked_listdir)

    def fail_open(*_args, **_kwargs):
        loop.call_soon_threadsafe(open_attempted.set)
        raise sqlite3.OperationalError("controlled locked open failure")

    monkeypatch.setattr(sqlite3, "connect", fail_open)
    run_task = asyncio.create_task(execute_command(deps, None))
    try:
        await _wait_event(worker_entered)
        await _wait_event(open_attempted)  # 数据库专用线程已实际进入打开。
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


async def test_cancelled_run_preserves_unprocessed_log_copy(tmp_path: Path) -> None:
    import sys

    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    old_copy = Path(config.paths.staging) / "logs" / "log-copy-old.log"
    old_copy.parent.mkdir(parents=True, exist_ok=True)
    content = b"partial old snapshot"
    old_copy.write_bytes(content)

    loop = asyncio.get_running_loop()
    release_worker = threading.Event()
    worker_started = asyncio.Event()
    business_started = asyncio.Event()
    business_stopped = asyncio.Event()
    release_business = asyncio.Event()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="controlled-default")
    blocker = None
    deps = None
    run_task = None
    try:
        loop.set_default_executor(pool)

        def occupy_worker():
            loop.call_soon_threadsafe(worker_started.set)
            release_worker.wait()

        async def controlled_business(_context) -> None:
            business_started.set()
            try:
                await release_business.wait()
            except asyncio.CancelledError:
                business_stopped.set()
                raise

        blocker = loop.run_in_executor(pool, occupy_worker)
        await asyncio.wait_for(worker_started.wait(), timeout=_GATE_TIMEOUT)
        deps = build_runtime(
            CommandMode.RUN, config,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        )
        run_task = asyncio.create_task(
            execute_command(deps, None, flows={"controlled": controlled_business}))
        await asyncio.wait_for(business_started.wait(), timeout=_GATE_TIMEOUT)
        assert run_task.cancel("cancel before log copy processing")
        await asyncio.wait_for(business_stopped.wait(), timeout=_GATE_TIMEOUT)
        assert not blocker.done()

        release_worker.set()
        await asyncio.wait_for(asyncio.shield(blocker), timeout=_GATE_TIMEOUT)
        done, _ = await asyncio.wait({run_task}, timeout=_GATE_TIMEOUT)
        assert run_task in done
        with pytest.raises(asyncio.CancelledError):
            await run_task
        assert old_copy.read_bytes() == content
        lease = acquire_session(deps.session_lock)
        lease.close()
    finally:
        failure = sys.exception()
        release_worker.set()
        release_business.set()
        cleanup_errors = []
        if run_task is not None and not run_task.done():
            run_task.cancel("test cleanup")
        for task in (blocker, run_task):
            if task is not None:
                try:
                    results = await asyncio.wait_for(
                        asyncio.shield(asyncio.gather(task, return_exceptions=True)),
                        timeout=_GATE_TIMEOUT,
                    )
                    result = results[0]
                    if isinstance(result, BaseException) and (
                            task is blocker or not isinstance(result, asyncio.CancelledError)):
                        cleanup_errors.append(result)
                except BaseException as error:
                    cleanup_errors.append(error)
        if deps is not None:
            try:
                close_runtime(deps)
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            pool.shutdown(wait=True)
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if failure is not None:
                failure.add_note(f"收场错误: {cleanup_errors!r}")
            else:
                raise BaseExceptionGroup("测试收场失败", cleanup_errors)


async def test_late_clock_winddown_stops_before_blocked_log_settlement(
        tmp_path: Path, monkeypatch) -> None:
    """真实受限录像停止不等待已开始的启动日志目录扫描。"""
    from camctl.bootstrap.flows import winddown_flow
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
    stop_completed = asyncio.Event()
    once_report_started = asyncio.Event()
    old_copy = Path(config.paths.staging) / "logs" / "log-copy-prior.log"
    old_copy.parent.mkdir(parents=True, exist_ok=True)
    old_copy.write_bytes(b"old snapshot")

    original_listdir = os.listdir

    def blocked_listdir(path: Path):
        if Path(path) == Path(config.paths.staging) / "logs":
            loop.call_soon_threadsafe(listdir_started.set)
            _wait_thread_event(release_listdir)
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", blocked_listdir)

    original_stop = driver.stop

    async def observed_stop(request):
        stop_started.set()
        await _wait_event(release_stop)
        result = await original_stop(request)
        stop_completed.set()
        return result

    driver.stop = observed_stop

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

    run_task = asyncio.create_task(execute_command(
        deps, None,
        flows={"late_clock": late_clock},
        restricted_flows={"winddown": restricted_winddown},
    ))
    try:
        await _wait_event(listdir_started)
        await _wait_event(stop_started)
        assert not once_report_started.is_set()
        assert not run_task.done()
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)
        release_stop.set()
        await _wait_event(stop_completed)
        assert not release_listdir.is_set()
        assert not once_report_started.is_set()
        assert not run_task.done()
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)
        release_listdir.set()
        outcome = await _wait_task(run_task)
    finally:
        release_stop.set()
        release_listdir.set()
        if not run_task.done():
            run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)

    assert len([call for call in driver.calls if call[0] == "stop"]) == 1
    assert once_report_started.is_set()
    assert old_copy.exists()  # 本次未声明旧 owner 已收场，不能删除旧副本。
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
    from camctl.session.clock import ClockBecameUntrusted
    from camctl.session.locks import probe_admission
    from .test_media_assembly import _SessionDriver, _registry
    from .test_restricted_winddown import (
        _FakeWinddownClock, _factory, _recording_in_flight,
    )
    from .test_recording_stop import _RecordCatalog, _entry
    from ..session.support import assert_database_closed

    driver = _SessionDriver(b"restricted stop source")
    config, database, driver, results = await _recording_in_flight(
        tmp_path, "cancelled-restricted-stop", driver=driver)
    results.files_by_action[1] = (_entry("clip-1", size=len(b"restricted stop source")),)
    deps = build_runtime(
        CommandMode.RUN, config, catalog=_RecordCatalog(),
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
    )
    if initial_restricted:
        monkeypatch.setattr(time, "time_ns", lambda: 1_000)
    old_copy = Path(config.paths.staging) / "logs" / "log-copy-prior.log"
    old_copy.parent.mkdir(parents=True, exist_ok=True)
    old_copy.write_bytes(b"old copy awaiting scan")

    loop = asyncio.get_running_loop()
    listdir_started = asyncio.Event()
    release_listdir = threading.Event()
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    stop_cancelled = asyncio.Event()
    once_report_started = asyncio.Event()
    session_contexts = []
    stop_entries: list[object] = []
    original_listdir = os.listdir

    def blocked_listdir(path: Path):
        if Path(path) == Path(config.paths.staging) / "logs":
            loop.call_soon_threadsafe(listdir_started.set)
            _wait_thread_event(release_listdir)
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", blocked_listdir)

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

    original_run_session = lifecycle.run_session

    async def observe_session(context, source):
        session_contexts.append(context)

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

    async def wait_for_saved_stop() -> None:
        while True:
            with closing(sqlite3.connect(database)) as connection:
                facts = (
                    connection.execute(
                        "SELECT status, attempts_used FROM operation_runs"
                        " WHERE responsibility_key = 'stop/1'").fetchone(),
                    connection.execute(
                        "SELECT check_decision FROM recording_processing"
                        " WHERE action_id = 1").fetchone(),
                )
            if facts == ((3, 1), (3,)):
                return
            if run_task.done():
                run_task.result()
                raise AssertionError(f"会话结束但 STOP 结果未保存: {facts!r}")
            await asyncio.sleep(0.01)

    try:
        await _wait_event(listdir_started)
        await _wait_event(stop_started)
        assert len(stop_entries) == 1
        assert not stop_cancelled.is_set()
        assert_session_lock_held()
        assert probe_admission(deps.admission_lock).status.value == (
            "acquired_and_released")

        run_task.cancel("parent during restricted winddown STOP")
        cancellation_checkpoint = asyncio.Event()
        loop.call_soon(cancellation_checkpoint.set)
        await _wait_event(cancellation_checkpoint)
        assert not stop_cancelled.is_set()
        assert not run_task.done()
        assert_session_lock_held()

        run_task.cancel("repeated parent during restricted winddown STOP")
        await asyncio.sleep(0)
        assert len(stop_entries) == 1
        assert not stop_cancelled.is_set()

        release_stop.set()
        await asyncio.wait_for(wait_for_saved_stop(), timeout=_GATE_TIMEOUT)
        assert not release_listdir.is_set()
        assert not run_task.done()
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
        assert old_copy.read_bytes() == b"old copy awaiting scan"
        await assert_database_closed(session_contexts[0].database)
        lease = acquire_session(deps.session_lock)
        lease.close()
    finally:
        release_stop.set()
        release_listdir.set()
        if not run_task.done():
            run_task.cancel("restricted winddown test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)


async def test_cancel_during_open_settlement_stops_new_work_and_waits(
        tmp_path: Path) -> None:
    """正常业务结束后取消，先停新工作，再完成已开始的本地收场。"""
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.bootstrap.clocks import SystemClock
    from camctl.session.locks import probe_admission
    from camctl.session.service import run_session
    from ..session.support import assert_database_closed
    from ..support.session import session_context

    config = _config_for(tmp_path)
    assert initialize_state(config, Path(config.paths.state_db)).outcome is InitOutcome.CREATED
    flow_completed = asyncio.Event()
    release_settlement = asyncio.Event()

    class Owner:
        def __init__(self, *, gated: bool = False) -> None:
            self.gated = gated
            self.stopped = asyncio.Event()
            self.settle_started = asyncio.Event()
            self.settle_finished = asyncio.Event()
            self.settle_calls = 0

        def required_settlements(self) -> int:
            return 0

        def stop_new_work(self) -> None:
            self.stopped.set()

        async def settle(self) -> None:
            self.settle_calls += 1
            attempt = self.settle_calls
            self.settle_started.set()
            if self.gated:
                await _wait_event(release_settlement)
            if attempt == 1:
                self.settle_finished.set()

    class LoggingOwner(Owner):
        def __init__(self) -> None:
            super().__init__()
            self.startup_cancelled = asyncio.Event()

        def cancel_startup_sweep(self) -> None:
            self.startup_cancelled.set()

    business = Owner(gated=True)
    logging = LoggingOwner()

    async def business_flow(_context) -> None:
        flow_completed.set()

    context = session_context(
        Path(config.paths.state_db), clock=SystemClock(),
        flows={"business": business_flow},
        local_work=CombinedLocalWork((business, logging)),
        logging_owner=logging,
    )
    task = asyncio.create_task(run_session(context, None))
    try:
        await _wait_event(business.settle_started)
        assert flow_completed.is_set()
        assert not business.settle_finished.is_set()
        assert not business.stopped.is_set()
        assert not logging.stopped.is_set()
        assert not logging.startup_cancelled.is_set()
        assert not task.done()
        with pytest.raises(SessionLockConflictError):
            acquire_session(context.paths.session_lock)
        assert not probe_admission(context.paths.admission_lock).is_free

        task.cancel("cancel while open local-work settlement is waiting")
        await asyncio.wait_for(asyncio.gather(
            business.stopped.wait(), logging.stopped.wait(),
            logging.startup_cancelled.wait()), timeout=_GATE_TIMEOUT)
        assert not business.settle_finished.is_set()
        assert not task.done()
        with pytest.raises(SessionLockConflictError):
            acquire_session(context.paths.session_lock)
        assert not probe_admission(context.paths.admission_lock).is_free

        task.cancel("repeated cancel while settlement waits")
        await asyncio.sleep(0)
        assert business.settle_calls == 1
        assert not task.done()
        release_settlement.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await _wait_task_result(task)
        assert cancelled.value.args == (
            "cancel while open local-work settlement is waiting",)
        assert business.settle_finished.is_set()
        assert logging.settle_finished.is_set()
        await assert_database_closed(context.database)
        lease = acquire_session(context.paths.session_lock)
        lease.close()
        assert probe_admission(context.paths.admission_lock).is_free
    finally:
        release_settlement.set()
        if not task.done():
            task.cancel("test cleanup")
            await _wait_task(asyncio.gather(task, return_exceptions=True))


async def test_cancelled_started_unlink_keeps_lock_and_stops_next_candidate(
        tmp_path: Path, monkeypatch, capfd) -> None:
    """已开始的删除返回真实失败，取消后保留其他候选并交付诊断。"""
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
    diagnostic_sync_started = asyncio.Event()
    release_diagnostic_sync = threading.Event()
    business_started = asyncio.Event()
    business_cancelled = asyncio.Event()
    release_business = asyncio.Event()
    original_listdir = os.listdir
    original_unlink = os.unlink
    original_fsync = os.fsync
    ordinary_log = Path(config.paths.log_file)
    failure_marker = "controlled started unlink failure"

    def ordered_listdir(path):
        if Path(path) == logs_dir:
            return [first.name, second.name]
        return original_listdir(path)

    def controlled_unlink(path, *args, **kwargs):
        if Path(path) == first:
            loop.call_soon_threadsafe(first_unlink_started.set)
            _wait_thread_event(release_first_unlink)
            raise OSError(failure_marker)
        return original_unlink(path, *args, **kwargs)

    def gated_fsync(fd):
        try:
            fd_info = os.fstat(fd)
            log_info = ordinary_log.stat()
            is_log = (fd_info.st_dev, fd_info.st_ino) == (log_info.st_dev, log_info.st_ino)
            is_failure = failure_marker.encode() in ordinary_log.read_bytes()
        except FileNotFoundError:
            is_log = is_failure = False
        if is_log and is_failure and not release_diagnostic_sync.is_set():
            loop.call_soon_threadsafe(diagnostic_sync_started.set)
            _wait_thread_event(release_diagnostic_sync)
        return original_fsync(fd)

    async def business(_context):
        business_started.set()
        try:
            await release_business.wait()
        except asyncio.CancelledError:
            business_cancelled.set()
            raise

    monkeypatch.setattr(os, "listdir", ordered_listdir)
    monkeypatch.setattr(os, "unlink", controlled_unlink)
    monkeypatch.setattr(os, "fsync", gated_fsync)
    run_task = asyncio.create_task(execute_command(deps, None, flows={"business": business}))
    try:
        await _wait_event(first_unlink_started)
        await _wait_event(business_started)
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)
        run_task.cancel("cancel while startup unlink is started")
        await _wait_event(business_cancelled)
        assert not run_task.done()
        assert first.exists() and second.exists()
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)
        release_first_unlink.set()
        await _wait_event(diagnostic_sync_started)
        assert not run_task.done()
        assert first.exists() and second.exists()
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)
        release_diagnostic_sync.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await _wait_task_result(run_task)
        assert cancelled.value.args == ("cancel while startup unlink is started",)
    finally:
        release_first_unlink.set()
        release_diagnostic_sync.set()
        release_business.set()
        if not run_task.done():
            run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)

    assert first.read_bytes() == b"first old copy"
    assert second.read_bytes() == b"second old copy"
    diagnostics = Path(config.paths.log_file).read_text() + capfd.readouterr().err
    assert failure_marker in diagnostics
    lease = acquire_session(deps.session_lock)
    lease.close()


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

    def occupy_worker():
        loop.call_soon_threadsafe(worker_entered.set)
        _wait_thread_event(release_worker)

    blocker = loop.run_in_executor(pool, occupy_worker)
    scan_started = asyncio.Event()
    release_scan = threading.Event()
    restricted_stop = asyncio.Event()
    once_report = asyncio.Event()
    scans: list[Path] = []
    order: list[str] = []
    original_listdir = os.listdir

    def blocked_listdir(path: Path):
        if Path(path) == Path(config.paths.staging) / "logs":
            scans.append(Path(path))
            order.append("scan_started")
            loop.call_soon_threadsafe(scan_started.set)
            _wait_thread_event(release_scan)
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", blocked_listdir)

    monkeypatch.setattr(time, "time_ns", lambda: 1_000)
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
    enumeration_started = asyncio.Event()
    release_enumeration = threading.Event()
    business_started = asyncio.Event()
    release_business = asyncio.Event()
    resolved_paths: list[Path] = []
    original_listdir = os.listdir
    original_resolve = Path.resolve

    async def deliver(message: str) -> None:
        result = await deps.log_runtime.channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message=message))
        assert result.delivered is True

    def observe_resolved_log_target(path: Path, *args, **kwargs):
        result = original_resolve(path, *args, **kwargs)
        if (Path(path) == ordinary_log_path
                and threading.current_thread() is not threading.main_thread()):
            resolved_paths.append(Path(result))
        return result

    def block_before_directory_enumeration(path):
        if Path(path) == logs_dir:
            loop.call_soon_threadsafe(enumeration_started.set)
            _wait_thread_event(release_enumeration)
        return original_listdir(path)

    async def hold_session(_context):
        business_started.set()
        await release_business.wait()

    async def wait_for_deletions():
        while any(path.exists() for path in expected_deletes):
            await asyncio.sleep(0.01)

    try:
        prime = f"SW04 ordinary log priming record: {scenario}"
        await deliver(prime)
        assert prefix in ordinary_log_file.read_bytes()
        assert prime.encode("utf-8") in ordinary_log_file.read_bytes()

        if replacement_after_resolve:
            monkeypatch.setattr(Path, "resolve", observe_resolved_log_target)
            monkeypatch.setattr(os, "listdir", block_before_directory_enumeration)

        run_task = asyncio.create_task(
            execute_command(deps, None, flows={"hold_session": hold_session}))
        await _wait_event(business_started)
        if replacement_after_resolve:
            await _wait_event(enumeration_started)
            assert resolved_paths and all(path == resolved_target for path in resolved_paths)
            assert alias_path is not None and alias_path.is_symlink()
            alias_path.unlink()
            alias_path.write_bytes(replacement_prefix)
            release_enumeration.set()

        await asyncio.wait_for(wait_for_deletions(), timeout=_GATE_TIMEOUT)
        for path in expected_deletes:
            assert not path.exists()
        assert ordinary_log_file.exists()
        assert prefix in ordinary_log_file.read_bytes()
        assert formal_marker.exists() and unknown.exists()
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
        release_business.set()
        outcome = await _wait_task_result(run_task)
    finally:
        release_business.set()
        release_enumeration.set()
        if run_task is not None:
            if not run_task.done():
                run_task.cancel("test cleanup")
            await _wait_task(asyncio.gather(run_task, return_exceptions=True))
        close_runtime(deps)

    assert outcome.succeeded is True
    assert ordinary_log_file.exists()
    assert prefix in ordinary_log_file.read_bytes()
    if replacement_after_resolve:
        assert alias_path is not None and alias_path.read_bytes().startswith(
            replacement_prefix)


async def test_unknown_ordinary_log_target_preserves_startup_candidates(
        tmp_path: Path, monkeypatch, capfd) -> None:
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

    original_resolve = Path.resolve
    expected_files = {
        path: path.read_bytes()
        for path in (old_copy, old_temp, formal_marker, unknown)
    }

    def fail_ordinary_target_resolution(path: Path, *args, **kwargs):
        if Path(path) == ordinary_log_path:
            raise PermissionError(failure_marker)
        return original_resolve(path, *args, **kwargs)

    try:
        prime = "SW04 ordinary log priming before unknown-path scan"
        result = await deps.log_runtime.channel.deliver_important(
            LogRecord(level=LogLevel.ERROR, message=prime))
        assert result.delivered is True
        assert prefix in ordinary_log_path.read_bytes()

        monkeypatch.setattr(Path, "resolve", fail_ordinary_target_resolution)
        outcome = await asyncio.wait_for(
            execute_command(deps, None, flows={}), timeout=_GATE_TIMEOUT)
    finally:
        close_runtime(deps)

    assert outcome.succeeded is True
    for path, expected in expected_files.items():
        assert path.read_bytes() == expected
    assert prefix in ordinary_log_path.read_bytes()
    diagnostics = ordinary_log_path.read_text() + capfd.readouterr().err
    assert "ordinary_log_path_resolve_failed" in diagnostics
    assert failure_marker in diagnostics
