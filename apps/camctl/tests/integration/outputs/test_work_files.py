"""中间文件有限清理的真实组合：生命周期、预算、游标与失败收场。

真实 SQLite、真实 staging 文件与清理事务共同验证
[取回中间文件的保留与清理](../../../../architecture/obtaining-outputs.md#取回中间文件的保留与清理)
及[中间文件清理的运行预算](../../../../architecture/file-handoff.md#中间文件清理的运行预算)：
先可靠保存取消或失败并确认操作停止，再保存意图、删除并保存结果；
历史扫描按固定上界、剩余额度与可靠游标单轮推进，删除失败保留责任
留给后续正常运行，不重开原动作，也不单独要求会话继续。
"""

import asyncio
import hashlib
import json
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from ..bootstrap.test_output_binding_changes import environment  # noqa: F401
from ..support.outputs import output_operation, prepare_cancel_target
from ..support.output_planning import EventSequence, capture_operation
from ..support.database import business_database, database_path
from ..support.public_delivery_cases import prepare_public_delivery
from ..support.output_storage_cases import observed_operation
from ..support.bootstrap_read_cases import work_file_case, release_work_consumer, segment_input_case
from ..support.capture_media_cases import media_cases  # noqa: F401

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import (
    EventValidationError,
)
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTask, FileTaskExecutor, FileTaskId
from camctl.outputs.copy import CompletionContext, CompletionPhase, complete_copy
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryPhase, publish_delivery,
)
from camctl.outputs.work_files import (
    CleanupChecked, CleanupIntent, CleanupResultSave, RetentionRelease,
    WorkFileAction, WorkFileContext, WorkFileFailure, WorkFileLimits,
    WorkFileOutcome, WorkFileSingleOutcome, clean_one_work_file,
    clean_work_files,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.transactions import commit_operation
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.session.supervision import Supervisor

from .test_copy_resume import _command, _qualified
from .test_copy_segments import _CONTENT, _drive
from .test_local_read import local_read  # noqa: F401
from .test_qualification import _NOW
from .test_read_associations import read_targets  # noqa: F401


_CONTENT_DIGEST = hashlib.sha256(_CONTENT).hexdigest()


@pytest_asyncio.fixture
async def work_env(public_work_env):
    """真实拍摄与 READ 后，发布前的独立交付副本。"""
    owned, roots, _directories, qualification = public_work_env
    return owned, roots, qualification


@pytest.fixture
def storage_work_env(read_targets, tmp_path):
    """已准备完成（PREPARED）的交付副本；staging 工作副本仍在原位。"""
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.execute(
        "UPDATE device_files SET size_bytes=10, checksum_support=2, sha256=?"
        " WHERE id=501", (_CONTENT_DIGEST,))
    owned.connection.commit()
    qualification = _qualified(owned, _command())
    staging = tmp_path / "staging"
    for name in ("deliveries", "recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    roots = BoundDirectories(staging=staging)
    asyncio.run(_drive(owned, roots, qualification.copy_id, segment_size=10))
    step = asyncio.run(output_operation(
        owned, complete_copy, qualification.copy_id, CompletionContext,
        repository=OutputsRepository(),
        roots=roots,
        occurred_at=_NOW + 5,
    ))
    assert step.phase is CompletionPhase.PREPARED
    return owned, roots, qualification


def _context(database, roots, *, batch=32, limit=128, occurred_at=_NOW + 20):
    return WorkFileContext(
        repository=OutputsRepository(), database=database, staging=roots.staging,
        occurred_at=occurred_at,
        limits=WorkFileLimits(batch_size=batch, limit_per_run=limit))


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _events(owned, event_type: int, reason: int) -> int:
    return _row(
        owned,
        "SELECT COUNT(*) FROM history_events WHERE event_type=?"
        " AND json_extract(body_json, '$.reason')=?",
        event_type, reason)[0]


def _last_event(owned) -> int:
    return _row(owned, "SELECT MAX(id) FROM history_events")[0]


def _file_state(owned, file_id: int):
    return _row(
        owned,
        "SELECT retention_state, cleanup_state, last_error_json"
        " FROM intermediate_files WHERE id=?", file_id)


def _work_path(roots, owned, file_id: int):
    return roots.staging / _row(
        owned, "SELECT relative_path FROM intermediate_files WHERE id=?",
        file_id)[0]


def _cancel_delivery(owned, delivery_id: int) -> None:
    """测试准备：交付取消终态（模拟取消收场事实已可靠保存）。"""
    owned.connection.execute(
        "UPDATE deliveries SET status=7 WHERE id=?", (delivery_id,))
    owned.connection.commit()


def _seed_action_candidate(owned, roots, *, file_id: int) -> None:
    """登记一条动作归属的额外候选（归属录像动作 11，已终态）。

    测试准备直接构造行，不经被测入口；与交付副本的归属互不影响。
    """
    path = f"recording-inputs/{file_id}.bin"
    owned.connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id,"
        " owner_delivery_id, purpose, relative_path, retention_state,"
        " cleanup_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, 11, NULL, 2, ?, 2, 2, ?, ?, 1)",
        (file_id, path, _last_event(owned), _last_event(owned)))
    owned.connection.commit()
    target = roots.staging / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_CONTENT)


def _release(owned, file_id: int):
    return OutputsRepository().save_retention_release(
        RetentionRelease(file_id=file_id, occurred_at=_NOW + 12),
        new_operation_key(), owned)


def _release_ok(owned, file_id: int) -> None:
    outcome = _release(owned, file_id)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


# ---- 核心用例：清理失败不重开动作 ----


@pytest.mark.asyncio
async def test_work_file_cleanup_does_not_restart_action(public_file_env, monkeypatch):
    """取消后完整副本删除失败：动作与交付终态保持，责任保留不阻塞收尾。"""
    owned, roots, qualification = public_file_env
    async with business_database(database_path(owned)) as database:
        delivery_id = qualification.delivery_id
        file_id = qualification.target_file_id
        _cancel_public_delivery(owned, qualification)
        action_status = _row(owned, "SELECT status FROM actions WHERE id=?", qualification.action_id)[0]
        assert _work_path(roots, owned, file_id).read_bytes() == _CONTENT
        action_events = _events(owned, 8, 4)

        target = _work_path(roots, owned, file_id)
        original_unlink = Path.unlink
        calls = []

        def _refused(path, *args, **kwargs):
            if path == target:
                calls.append(path)
                raise OSError("删除被拒绝")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _refused)
        result = (await clean_one_work_file(file_id, _context(database, roots)))
        assert result.outcome is WorkFileSingleOutcome.FAILED
        assert calls == [target]
        retention, cleanup, error_json = _file_state(owned, file_id)
        assert (retention, cleanup) == (2, 5)
        error = json.loads(error_json)
        assert error["code"] == "work_file_delete_failed"
        assert error["details"] == {"delivery_id": str(delivery_id)}
        # 动作与交付终态不被清理失败改写，也不产生新的动作收场事件。
        assert _row(
            owned, "SELECT status FROM actions WHERE id=?", qualification.action_id)[0] == action_status
        assert _row(
            owned, "SELECT status FROM deliveries WHERE id=?",
            delivery_id)[0] == 7
        assert _events(owned, 8, 4) == action_events
        # 文件保留，责任留给后续正常运行有限重试。
        assert _work_path(roots, owned, file_id).read_bytes() == _CONTENT

        monkeypatch.undo()
        again = (await clean_one_work_file(
            file_id, _context(database, roots)))
        assert again.outcome is WorkFileSingleOutcome.DELETED
        assert not _work_path(roots, owned, file_id).exists()
        assert _file_state(owned, file_id)[:2] == (2, 4)


# ---- 生命周期判定 ----


@pytest.mark.asyncio
async def test_release_requires_owner_terminal(work_env):
    """交付仍有效待发布：释放被拒绝，副本保留继续原发布流程。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    target = _work_path(roots, owned, file_id)
    original = target.read_bytes()
    async with business_database(database_path(owned)) as database:
        outcome = _release(owned, file_id)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(outcome.error, ConsistencyError)
        result = await clean_one_work_file(file_id, _context(database, roots))
        assert result.decision.action is WorkFileAction.KEEP_REQUIRED
        assert result.outcome is WorkFileSingleOutcome.KEPT
        assert _file_state(owned, file_id)[:2] == (1, 1)
        assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_release_requires_stopped_operations(environment, monkeypatch):
    """存储格式专项：交付投影提前终态时，真实未结束 READ 仍禁止释放。"""
    import os
    from ..bootstrap.test_output_binding_changes import (
        Catalog, _CONTENT as photo, _NOW as now, _read_registry, _save_photos,
    )
    from ..support.outputs import obtain_factory, submit_output_document
    from camctl.outputs.obtain_flow import advance_obtain

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    source_id, = owned.connection.execute(
        "SELECT id FROM actions WHERE device_id='cam-a'").fetchone()
    accepted = await submit_output_document(context.database, catalog=Catalog(), occurred_at=now, body={
        "request_id": "2", "created_at": "2026-01-15 08:00:00", "name": "取回照片",
        "actions": [{"name": "取回", "type": "obtain_action_outputs",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"source": {"action_instance_id": str(source_id)}}}],
    })
    assert accepted.plan_id is not None, accepted
    action_id, = owned.connection.execute(
        "SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
    roots = BoundDirectories(staging=Path(cfg.paths.staging))
    for directory in (cfg.paths.staging, cfg.paths.ready, cfg.paths.processing):
        Path(directory).mkdir(parents=True, exist_ok=True)
    executor = FileTaskExecutor(Supervisor())
    runtime = obtain_factory(
        devices=cfg.devices, drivers=_read_registry(driver), staging=roots.staging,
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing),
        occurred_at=lambda: now + 20, monotonic_ns=lambda: 0,
        segment_size=len(photo), file_executor=executor)(context.database)
    entered, release = threading.Event(), threading.Event()
    paths = []
    actual_open = os.open

    def hold_hash(path, flags, *args, **kwargs):
        if (isinstance(path, (str, Path)) and Path(path).is_absolute()
                and Path(path).parent == roots.staging / "deliveries"
                and flags & os.O_ACCMODE == os.O_RDONLY and not entered.is_set()):
            paths.append(Path(path))
            entered.set()
            assert release.wait(10), "测试未放行实际摘要计算"
        return actual_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", hold_hash)
    running = asyncio.create_task(advance_obtain(runtime))
    waiter = asyncio.create_task(asyncio.to_thread(entered.wait, 5))
    original_status = delivery_id = None
    try:
        done, _ = await asyncio.wait({waiter, running}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if running in done:
            await running
            raise AssertionError("实际读取未到摘要阶段便结束")
        assert waiter in done and waiter.result(), "实际读取未到摘要阶段"
        file_id, delivery_id, copy_id = owned.connection.execute(
            "SELECT f.id,d.id,c.id FROM intermediate_files f"
            " JOIN file_copies c ON c.target_file_id=f.id"
            " JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=?", (action_id,)).fetchone()
        assert paths == [_work_path(roots, owned, file_id)]
        assert paths[0].read_bytes() == photo
        attempts, maximum, timeout, interval, number, status, intent, result = owned.connection.execute(
            "SELECT r.attempts_used,r.max_attempts_used,r.timeout_s_json,r.retry_interval_s_json,"
            "a.attempt_no,a.status,a.intent_event_id,a.result_json FROM operation_runs r JOIN operation_attempts a"
            " ON a.run_id=r.id WHERE r.copy_id=?", (copy_id,)).fetchone()
        assert (attempts, maximum, number, status, result) == (1, 3, 1, 1, None)
        assert timeout is not None and interval is not None
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE id=?", (intent,)).fetchone() == (1,)
        prepare_cancel_target(owned, catalog=Catalog(), now=now + 30, action_id=action_id, request_id="4")
        original_status, = owned.connection.execute(
            "SELECT status FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        # 只破坏交付投影：模拟错误的提前终态，使释放事务到达未结束尝试守卫。
        # READ 原意图、预算、次数、文件与取消事实均来自正式入口。
        owned.connection.execute("UPDATE deliveries SET status=7 WHERE id=?", (delivery_id,))
        owned.connection.commit()
        before = tuple(owned.connection.iterdump())
        outcome = OutputsRepository().save_retention_release(
            RetentionRelease(file_id, now + 40), new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(outcome.error, ConsistencyError)
        assert str(outcome.error) == f"中间文件 {file_id} 仍有 1 个未结束操作尝试"
        assert not owned.connection.in_transaction and tuple(owned.connection.iterdump()) == before
        assert _file_state(owned, file_id)[:2] == (1, 1)
        assert paths[0].read_bytes() == photo
    finally:
        if original_status is not None:
            owned.connection.execute("UPDATE deliveries SET status=? WHERE id=?", (original_status, delivery_id))
            owned.connection.commit()
        release.set()
        await asyncio.wait_for(asyncio.shield(running), 10)
        settled = await asyncio.wait_for(asyncio.shield(executor.drain_required()), 10)
        assert not settled.failed
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_promoted_file_is_not_cleaned(tmp_path, media_cases):
    """已成正式产物（PROMOTED）的中间文件不进入自动清理。"""
    from decimal import Decimal

    from camctl.capture.handlers import capture_handler
    from camctl.capture.result_inputs import ObservedFile
    from camctl.capture.results import FileKind
    from camctl.host_files.media import MediaProbe, MediaStream
    from ..support.capture import capture_database
    from ..support.capture_media_cases import (
        CONTENT, ScriptTools, media_case, recording_environment,
        recording_media, recording_runtime,
    )

    owned, staging = await recording_environment(
        tmp_path, check_decision=3,
        check_basis='{"reason": 2, "target_duration_ms": 60000}')
    try:
        entry = ObservedFile(
            identity="recording.mp4", locator={"path": "/DCIM/recording.mp4"},
            evidence={"listing": "recording"}, complete=True,
            size_bytes=len(CONTENT), kind=FileKind.VIDEO,
            original_name="recording.mp4", media_type="video/mp4", format_id="mp4")
        tools = ScriptTools(MediaProbe(Decimal("200"), None, (
            MediaStream(0, "video", "h264", 64, 64, Decimal("200"), False),)))
        media = await recording_media(owned, tools, staging)
        runtime = await recording_runtime(owned, {1: (entry,)}, flow=media)
        await capture_handler("camera_record")(1, runtime)
        assert tools.calls == ["probe", "repair"]
        assert _row(owned, "SELECT status FROM actions WHERE id=1") == (3,)
        assert _row(owned, "SELECT check_state, repair_state FROM recording_processing WHERE id=1") == (3, 5)

        output_id, file_id, purpose, retention, cleanup, relative = _row(
            owned,
            "SELECT o.id, f.id, f.purpose, f.retention_state, f.cleanup_state, f.relative_path"
            " FROM outputs o JOIN intermediate_files f ON f.id=o.intermediate_file_id"
            " WHERE o.source_action_id=1 AND o.kind=2")
        assert (purpose, retention, cleanup) == (4, 3, 1)
        assert relative.startswith("derived/")
        assert _row(owned, "SELECT owner_action_id, owner_delivery_id FROM intermediate_files WHERE id=?", file_id) == (1, None)
        original_id, = _row(owned, "SELECT id FROM outputs WHERE source_action_id=1 AND kind=1")
        assert _row(owned, "SELECT original_output_id FROM output_origins WHERE output_id=?", output_id) == (original_id,)
        target = staging / relative
        assert target.read_bytes() == tools.content

        input_id, = _row(owned, "SELECT target_file_id FROM file_copies WHERE processing_id=1")
        assert input_id != file_id
        assert _row(owned, "SELECT purpose, retention_state, cleanup_state FROM intermediate_files WHERE id=?", input_id) == (2, 1, 1)
        assert _work_path(BoundDirectories(staging=staging), owned, input_id).read_bytes() == CONTENT
        assert {row[0] for row in owned.connection.execute("SELECT id FROM intermediate_files")} == {input_id, file_id}

        case = media_case(media)
        settled = await case.executor.drain_required()
        assert not settled.failed
        assert case.executor.unfinished_files() == ()
        database = await capture_database(owned)
        roots = BoundDirectories(staging=staging)

        def context():
            return WorkFileContext(
                repository=OutputsRepository(), database=database, staging=staging,
                occurred_at=_NOW + 100_000_001,
                limits=WorkFileLimits(batch_size=32, limit_per_run=128),
                executor=case.executor)

        cleared = await clean_one_work_file(input_id, context())
        assert cleared.outcome is WorkFileSingleOutcome.DELETED
        assert not _work_path(roots, owned, input_id).exists()
        assert _file_state(owned, input_id)[:2] == (2, 4)
        result = await clean_one_work_file(file_id, context())
        assert result.decision.action is WorkFileAction.NOT_MANAGED
        assert result.outcome is WorkFileSingleOutcome.NOT_MANAGED
        assert target.read_bytes() == tools.content
        assert _file_state(owned, file_id)[:2] == (3, 1)
        scan = await clean_work_files(context())
        assert scan.checked == 0
        assert target.read_bytes() == tools.content
        assert _file_state(owned, file_id)[:2] == (3, 1)
        assert _row(owned, "SELECT intermediate_file_id FROM outputs WHERE id=?", output_id) == (file_id,)
        assert _row(owned, "SELECT original_output_id FROM output_origins WHERE output_id=?", output_id) == (original_id,)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_missing_file_after_interrupt_records_completion(public_file_env):
    """文件已删而结果未保存就中断：重新核实不存在后补记完成。"""
    owned, roots, qualification = public_file_env
    async with business_database(database_path(owned)) as database:
        file_id = qualification.target_file_id
        _cancel_public_delivery(owned, qualification)
        _work_path(roots, owned, file_id).unlink()
        result = (await clean_one_work_file(
            file_id, _context(database, roots)))
        assert result.outcome is WorkFileSingleOutcome.DELETED
        assert _file_state(owned, file_id)[:2] == (2, 4)


@pytest.mark.asyncio
async def test_observation_failure_is_not_missing(public_file_env, monkeypatch):
    """工作位置观察失败：保存失败诊断，不冒充清理完成。"""
    owned, roots, qualification = public_file_env
    async with business_database(database_path(owned)) as database:
        _cancel_public_delivery(owned, qualification)

        target = _work_path(roots, owned, qualification.target_file_id)
        original_stat = Path.stat
        calls = []

        def _broken(path, *args, **kwargs):
            if path == target:
                calls.append(path)
                raise OSError("目录不可访问")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _broken)
        result = (await clean_one_work_file(
            qualification.target_file_id, _context(database, roots)))
        assert result.outcome is WorkFileSingleOutcome.FAILED
        assert calls == [target]
        retention, cleanup, error_json = _file_state(
            owned, qualification.target_file_id)
        assert (retention, cleanup) == (2, 5)
        assert json.loads(error_json)["code"] == "work_file_delete_failed"


@pytest_asyncio.fixture
async def public_quota_env(tmp_path, capture_resources):
    world = await work_file_case(tmp_path, capture_resources, count=2)
    try:
        assert all(world.paths[file_id].read_bytes() == b"bind" for file_id, _, _ in world.files)
        yield world
    finally:
        world.owned.connection.close()


# ---- 历史扫描预算与游标 ----


@pytest.mark.asyncio
async def test_history_scan_respects_run_limit_and_resumes(public_quota_env):
    """额度先尽即止；已检查记录推进游标，下次运行从其后继续。"""
    world = public_quota_env
    owned = world.owned
    first_id, second_id = (row[0] for row in world.files)
    assert first_id < second_id
    release_work_consumer(world, 0)
    release_work_consumer(world, 1)
    roots = BoundDirectories(staging=Path(world.cfg.paths.staging))
    async with business_database(world.path) as database:
        first = await clean_work_files(_context(database, roots, batch=32, limit=1))
        assert (first.checked, first.cleaned) == (1, 1)
        assert _row(owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == first_id
        assert not world.paths[first_id].exists()
        assert world.paths[second_id].read_bytes() == b"bind"
        assert _file_state(owned, second_id)[:2] == (2, 2)
        second = await clean_work_files(_context(database, roots, batch=32, limit=1))
        assert (second.checked, second.cleaned) == (1, 1)
        assert _row(owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == second_id
        assert _file_state(owned, second_id)[:2] == (2, 4)
        assert not world.paths[second_id].exists()
        third = await clean_work_files(_context(database, roots, batch=32, limit=1))
        assert (third.checked, third.cleaned) == (0, 0)


@pytest.mark.asyncio
async def test_history_scan_wraps_to_start_once(public_quota_env):
    """游标处于高位时绕回尚待清理的低位候选。"""
    world = public_quota_env
    owned = world.owned
    low, high = (row[0] for row in world.files)
    assert low < high
    release_work_consumer(world, 1)
    roots = BoundDirectories(staging=Path(world.cfg.paths.staging))
    async with business_database(world.path) as database:
        first = await clean_work_files(_context(database, roots, batch=1, limit=1))
        assert (first.checked, first.cleaned) == (1, 1)
        assert _row(owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == high
        assert not world.paths[high].exists()
        assert world.paths[low].read_bytes() == b"bind"
        release_work_consumer(world, 0)
        second = await clean_work_files(_context(database, roots, batch=32, limit=2))
        assert (second.checked, second.cleaned) == (1, 1)
        assert _row(owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == low
        assert _file_state(owned, low)[:2] == (2, 4)
        assert not world.paths[low].exists()
        third = await clean_work_files(_context(database, roots, batch=32, limit=2))
        assert (third.checked, third.cleaned) == (0, 0)


@pytest.mark.asyncio
async def test_kept_candidate_consumes_budget_and_advances(public_quota_env):
    """实际文件任务占用的候选消耗额度并推进游标，下一轮继续后续记录。"""
    world = public_quota_env
    owned = world.owned
    first_id, second_id = (row[0] for row in world.files)
    assert first_id < second_id
    release_work_consumer(world, 0)
    release_work_consumer(world, 1)
    roots = BoundDirectories(staging=Path(world.cfg.paths.staging))
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    started, release = threading.Event(), threading.Event()

    def hold_file(_stop):
        assert world.paths[first_id].read_bytes() == b"bind"
        started.set()
        if not release.wait(timeout=10):
            raise TimeoutError("文件任务等待释放超时")
        return "actual file task ended"

    class Owner:
        async def take_over(self, task):
            await task.pending.wait()

    running = asyncio.create_task(executor.run_file_task(FileTask(
        task_id=FileTaskId("media/quota-cleanup"), file_id=first_id,
        stage="media", business="original file", body=hold_file), Owner()))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        async with business_database(world.path) as database:
            context = replace(_context(database, roots, batch=32, limit=1), executor=executor)
            result = await clean_work_files(context)
            assert (result.checked, result.cleaned, result.kept) == (1, 0, 1)
            assert world.paths[first_id].read_bytes() == b"bind"
            assert world.paths[second_id].read_bytes() == b"bind"
            assert _row(owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == first_id

            next_context = replace(_context(database, roots, batch=32, limit=1), executor=executor)
            following = await clean_work_files(next_context)
            assert (following.checked, following.cleaned) == (1, 1)
            assert _row(owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == second_id
            assert _file_state(owned, second_id)[:2] == (2, 4)
            assert not world.paths[second_id].exists()
            assert world.paths[first_id].read_bytes() == b"bind"
    finally:
        release.set()
        try:
            finished = await asyncio.wait_for(running, timeout=10)
            assert finished.ran and finished.value == "actual file task ended"
        finally:
            settled = await asyncio.wait_for(supervisor.drain_required(), timeout=10)
            assert not settled.failed


@pytest.mark.asyncio
async def test_batch_configuration_cannot_exceed_remaining(public_quota_env, monkeypatch):
    """批量配置大于剩余额度时按剩余额度读取。"""
    world = public_quota_env
    first_id, second_id = (row[0] for row in world.files)
    assert first_id < second_id
    release_work_consumer(world, 0)
    release_work_consumer(world, 1)
    roots = BoundDirectories(staging=Path(world.cfg.paths.staging))
    original = OutputsRepository.next_cleanup_candidates
    batches = []

    def observe(repository, after, ceiling, limit, owned):
        result = original(repository, after, ceiling, limit, owned)
        batches.append((after, ceiling, limit, result))
        return result

    monkeypatch.setattr(OutputsRepository, "next_cleanup_candidates", observe)
    async with business_database(world.path) as database:
        result = await clean_work_files(_context(database, roots, batch=32, limit=1))
    assert (result.checked, result.cleaned) == (1, 1)
    assert batches
    assert all(0 < limit <= 1 and len(rows) <= limit
               and set(rows) <= {first_id, second_id}
               for _after, _ceiling, limit, rows in batches)
    assert not world.paths[first_id].exists()
    assert world.paths[second_id].read_bytes() == b"bind"
    assert _file_state(world.owned, second_id)[:2] == (2, 2)


@pytest.mark.asyncio
async def test_first_cleanup_does_not_consume_history_budget(public_quota_env):
    """首次清理不占同一次运行的历史额度。"""
    world = public_quota_env
    first_id, second_id = (row[0] for row in world.files)
    assert first_id < second_id
    release_work_consumer(world, 0)
    release_work_consumer(world, 1)
    roots = BoundDirectories(staging=Path(world.cfg.paths.staging))
    async with business_database(world.path) as database:
        context = _context(database, roots, limit=1)
        single = await clean_one_work_file(first_id, context)
        assert single.outcome is WorkFileSingleOutcome.DELETED
        assert not world.paths[first_id].exists()
        assert _row(world.owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] is None
        assert world.paths[second_id].read_bytes() == b"bind"
        scan = await clean_work_files(context)
        assert (scan.checked, scan.cleaned) == (1, 1)
        assert _file_state(world.owned, second_id)[:2] == (2, 4)
        assert _row(world.owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == second_id
        assert not world.paths[second_id].exists()


@pytest.mark.asyncio
async def test_same_run_reuses_first_attempt_for_same_file(public_file_env):
    """首次入口已处理的文件被历史扫描再次发现时复用结果不重试。"""
    owned, roots, qualification = public_file_env
    async with business_database(database_path(owned)) as database:
        file_id = qualification.target_file_id
        _cancel_public_delivery(owned, qualification)
        _release_ok(owned, file_id)
        supervisor = Supervisor()
        executor = FileTaskExecutor(supervisor)
        started, release = threading.Event(), threading.Event()

        def hold_file(_stop):
            started.set()
            if not release.wait(timeout=10):
                raise TimeoutError("文件任务等待释放超时")
            return "actual file task ended"

        class Owner:
            async def take_over(self, task):
                await task.pending.wait()

        running = asyncio.create_task(executor.run_file_task(FileTask(
            task_id=FileTaskId("media/reused-cleanup"), file_id=file_id,
            stage="media", business="original file", body=hold_file), Owner()))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            assert executor.unfinished_files() == (file_id,)
            context = replace(_context(database, roots), executor=executor)
            first = await clean_one_work_file(file_id, context)
            assert first.outcome is WorkFileSingleOutcome.KEPT
            events_before = (_events(owned, 26, 3), _events(owned, 26, 4))
            # 实际任务结束后再扫描，证明复用首次保留结果而非再次判定占用。
            release.set()
            finished = await asyncio.wait_for(running, timeout=10)
            assert finished.ran and finished.value == "actual file task ended"
            settled = await asyncio.wait_for(supervisor.drain_required(), timeout=10)
            assert not settled.failed
            assert executor.unfinished_files() == ()
            scan = await clean_work_files(context)
            assert (scan.checked, scan.kept) == (1, 1)
            assert (_events(owned, 26, 3), _events(owned, 26, 4)) == events_before
            assert _file_state(owned, file_id)[:2] == (2, 2)
            assert _work_path(roots, owned, file_id).read_bytes() == _CONTENT
            assert _row(
                owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == file_id
        finally:
            release.set()
            try:
                await asyncio.wait_for(
                    asyncio.gather(running, return_exceptions=True), timeout=10)
            finally:
                await asyncio.wait_for(supervisor.drain_required(), timeout=10)


@pytest.mark.asyncio
async def test_action_owned_delete_failure_keeps_responsibility(
        tmp_path, capture_resources, monkeypatch):
    """真实录像输入删除失败后仍清理同库内的独立交付副本。"""
    from types import SimpleNamespace

    world = await segment_input_case(tmp_path, capture_resources, monkeypatch, (60,),
                                     early_waiters=(120,), segment_size=4)
    try:
        await world.publish()
        assert len(world.inputs) == 1
        file_id, _, _ = world.inputs[0]
        target = world.paths[file_id]
        assert target.read_bytes() == _CONTENT
        source_status = _row(world.owned, "SELECT status FROM actions WHERE id=?", world.source_id)[0]
        await world.advance_obtains(1)
        waiter, _ = world.early[0]
        delivery = _row(world.owned,
            "SELECT f.id,f.relative_path,d.id,c.id FROM intermediate_files f"
            " JOIN file_copies c ON c.target_file_id=f.id"
            " JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=?", waiter)
        assert delivery is not None
        delivery_file_id, relative_path, delivery_id, copy_id = delivery
        delivery_path = Path(world.cfg.paths.staging) / relative_path
        assert file_id < delivery_file_id and delivery_path.is_file()
        delivery_bytes = delivery_path.read_bytes()
        assert delivery_bytes == _CONTENT
        _cancel_public_delivery(world.owned, SimpleNamespace(
            action_id=waiter, delivery_id=delivery_id, copy_id=copy_id), now=world.wall[0])
        world.wall[0] += 1
        assert not world.executor.unfinished_files()
        assert target.read_bytes() == _CONTENT and delivery_path.read_bytes() == delivery_bytes
        repository = OutputsRepository()
        assert repository.next_cleanup_candidates(0, delivery_file_id, 2, world.owned) == (
            file_id, delivery_file_id)
        target_facts = repository.load_work_file_state(file_id, world.owned)
        delivery_facts = repository.load_work_file_state(delivery_file_id, world.owned)
        assert target_facts.owner_finished and target_facts.operations_stopped
        assert delivery_facts.owner_finished and delivery_facts.operations_stopped
        original_unlink = Path.unlink
        calls = []

        def _refused(path, *args, **kwargs):
            if path == target:
                calls.append(path)
                raise OSError("动作副本删除被拒绝")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "unlink", _refused)
            context = WorkFileContext(repository=repository, database=world.database,
                staging=Path(world.cfg.paths.staging), occurred_at=world.wall[0],
                limits=WorkFileLimits(batch_size=32, limit_per_run=128), executor=world.executor)
            scan = await clean_work_files(context)
        assert (scan.checked, scan.cleaned, scan.failed) == (2, 1, 1)
        assert calls == [target] and target.read_bytes() == _CONTENT
        retention, cleanup, error = _file_state(world.owned, file_id)
        assert (retention, cleanup) == (2, 5)
        assert json.loads(error)["code"] == "action_work_file_delete_failed"
        assert json.loads(error)["details"] == {"file_id": str(file_id)}
        assert _file_state(world.owned, delivery_file_id)[:2] == (2, 4)
        assert not delivery_path.exists()
        assert _row(world.owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == delivery_file_id
        assert _row(world.owned, "SELECT status FROM actions WHERE id=?", world.source_id)[0] == source_status
        assert _row(world.owned, "SELECT status FROM deliveries WHERE id=?", delivery_id)[0] == 7
    finally:
        await world.close()

# ---- 发布后的生命周期 ----


@pytest_asyncio.fixture
async def public_work_env(tmp_path):
    """正式来源与成功 READ 已保存，完整交付副本等待发布。"""
    world = await prepare_public_delivery(tmp_path, content=_CONTENT, now=_NOW)
    try:
        yield world
    finally:
        world[0].connection.close()


@pytest_asyncio.fixture
async def public_file_env(public_work_env):
    owned, roots, _directories, qualification = public_work_env
    return owned, roots, qualification


def _cancel_public_delivery(owned, qualification, *, now=_NOW + 10):
    """保存目标取回取消，发起者等待后续首次文件清理。"""
    from ..capture.result_consumer_fixtures import ResultCatalog
    from camctl.persistence.repositories.outputs import SettleCanceledObtain

    class Catalog(ResultCatalog):
        def action_types(self):
            return super().action_types() | {"obtain_action_outputs"}

    action_id = qualification.action_id
    prepare_cancel_target(owned, catalog=Catalog(), now=now,
                          action_id=action_id, request_id="4")
    members = tuple(row[0] for row in owned.connection.execute(
        "SELECT i.id FROM obtain_items i JOIN obtain_source_selections s ON s.id=i.selection_id"
        " JOIN action_dependencies d ON d.id=s.dependency_id WHERE d.action_id=? ORDER BY i.id",
        (action_id,)))
    settled = OutputsRepository().settle_canceled_obtain(
        SettleCanceledObtain(action_id, now + 1, members), new_operation_key(), owned)
    assert settled.kind is DbOutcomeKind.COMPLETED and settled.value.complete, settled.error
    statuses = owned.connection.execute(
        "SELECT target.status, d.status, origin.status, ci.status"
        " FROM actions target JOIN deliveries d ON d.action_id=target.id"
        " JOIN cancel_items ci ON ci.target_action_id=target.id"
        " JOIN actions origin ON origin.id=ci.action_id"
        " WHERE target.id=? AND d.id=?", (action_id, qualification.delivery_id)).fetchone()
    # 目标已取消；发起取消的动作和明细仍在处理中，尚未汇总结果。
    assert statuses == (6, 7, 2, 2)
    run_status, attempts, attempt_status, result_event = owned.connection.execute(
        "SELECT r.status,r.attempts_used,a.status,a.result_event_id FROM operation_runs r"
        " JOIN operation_attempts a ON a.run_id=r.id AND a.attempt_no=r.attempts_used"
        " WHERE r.copy_id=? AND r.kind=?",
        (qualification.copy_id, int(enum_for("operation_runs.kind").READ_FILE)),
    ).fetchone()
    assert (run_status, attempts, attempt_status) == (
        int(enum_for("operation_runs.status").SUCCEEDED), 1,
        int(enum_for("operation_attempts.status").SUCCEEDED))
    assert result_event is not None


@pytest.mark.asyncio
async def test_publication_hands_off_intermediate_file(public_work_env):
    """发布确认后中间文件转 HANDED_OFF，交接所有权不归自动清理。"""
    owned, roots, directories, qualification = public_work_env
    work_path = _work_path(roots, owned, qualification.target_file_id)
    assert work_path.read_bytes() == _CONTENT
    file_name = _row(owned, "SELECT file_name FROM deliveries WHERE id=?", qualification.delivery_id)[0]
    async with business_database(database_path(owned)) as database:
        result = (await output_operation(
            owned, publish_delivery, qualification.delivery_id, DeliveryContext,
            repository=OutputsRepository(),
            directories=directories,
            occurred_at=_NOW + 10,
        ))
        assert result.phase is DeliveryPhase.PUBLISHED
        assert _file_state(owned, qualification.target_file_id)[0] == 4
        assert not work_path.exists()
        assert (directories.ready / file_name).read_bytes() == _CONTENT
        assert _events(owned, 26, 2) >= 1
        # 已交接文件不进入历史清理候选。
        scan = (await clean_work_files(_context(database, roots)))
        assert scan.checked == 0
        assert (directories.ready / file_name).read_bytes() == _CONTENT
        assert _file_state(owned, qualification.target_file_id)[0] == 4


@pytest.mark.asyncio
async def test_published_leftover_copy_is_released_and_cleaned(
        public_work_env):
    """发布经同名文件确认而 staging 副本留存：转 HANDED_OFF 后释放清理。"""
    owned, roots, directories, qualification = public_work_env
    async with business_database(database_path(owned)) as database:
        file_name = _row(owned, "SELECT file_name FROM deliveries WHERE id=?",
                         qualification.delivery_id)[0]
        leftover = _work_path(roots, owned, qualification.target_file_id)
        assert leftover.read_bytes() == _CONTENT
        (directories.ready / file_name).write_bytes(_CONTENT)
        result = (await output_operation(
            owned, publish_delivery, qualification.delivery_id, DeliveryContext,
            repository=OutputsRepository(),
            directories=directories,
            occurred_at=_NOW + 10,
        ))
        assert result.phase is DeliveryPhase.PUBLISHED
        assert leftover.read_bytes() == _CONTENT
        assert (directories.ready / file_name).read_bytes() == _CONTENT
        assert _file_state(owned, qualification.target_file_id)[0] == 4
        _release_ok(owned, qualification.target_file_id)
        assert _file_state(owned, qualification.target_file_id)[:2] == (2, 2)
        single = (await clean_one_work_file(
            qualification.target_file_id, _context(database, roots)))
        assert single.outcome is WorkFileSingleOutcome.DELETED
        assert not leftover.exists()
        assert _file_state(owned, qualification.target_file_id)[:2] == (2, 4)
        assert (directories.ready / file_name).read_bytes() == _CONTENT


# ---- 原键恢复 ----


@pytest.mark.asyncio
async def test_release_intent_result_checked_recover_first_response(storage_work_env):
    """四个清理事务各自按原键恢复首次响应，输入不符拒绝。"""
    owned, roots, qualification = storage_work_env
    repository = OutputsRepository()
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)

    release = RetentionRelease(file_id=file_id, occurred_at=_NOW + 12)
    key = new_operation_key()
    first = repository.save_retention_release(release, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_retention_release(release, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_retention_release(
        replace(release, occurred_at=_NOW + 13), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK

    intent = CleanupIntent(file_id=file_id, occurred_at=_NOW + 14)
    key = new_operation_key()
    first = repository.save_cleanup_intent(intent, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_cleanup_intent(intent, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before

    result_save = CleanupResultSave(
        file_id=file_id, outcome=WorkFileOutcome.COMPLETED,
        error=None, occurred_at=_NOW + 15, advance_cursor=True)
    key = new_operation_key()
    first = repository.save_cleanup_result(result_save, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_cleanup_result(result_save, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_cleanup_result(
        replace(result_save, occurred_at=_NOW + 17), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK

    _seed_action_candidate(owned, roots, file_id=900)
    checked = CleanupChecked(file_id=900, occurred_at=_NOW + 16)
    key = new_operation_key()
    first = repository.save_cleanup_checked(checked, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_cleanup_checked(checked, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == 900


# ---- 正式守卫 ----


def _proposal(owned, operation, request):
    return capture_operation(owned, lambda database: operation(
        request, new_operation_key(), database))


def _commit_proposal(owned, proposal, events=None, *, rejection=None):
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(
        EventSequence(proposal.events if events is None else events, proposal),
        new_operation_key(), owned)
    assert not owned.connection.in_transaction
    if rejection is not None:
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert rejection in str(receipt.error)
        assert tuple(owned.connection.iterdump()) == before
    else:
        assert receipt.kind == "completed", receipt.error


@pytest.mark.asyncio
async def test_guard_accepts_real_cleanup_events(work_env):
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_public_delivery(owned, qualification)
    release = _proposal(owned, OutputsRepository().save_retention_release,
                        RetentionRelease(file_id=file_id, occurred_at=_NOW + 12))
    _commit_proposal(owned, release)
    intent = _proposal(owned, OutputsRepository().save_cleanup_intent,
                       CleanupIntent(file_id=file_id, occurred_at=_NOW + 14))
    _commit_proposal(owned, intent)
    failure = WorkFileFailure(
        code="work_file_delete_failed",
        details={"delivery_id": str(qualification.delivery_id)})
    failed_result = _proposal(owned, OutputsRepository().save_cleanup_result,
                              _result_request(file_id, WorkFileOutcome.FAILED, failure))
    observed_operation(owned, lambda actual: _commit_proposal(actual, failed_result))
    result = _proposal(owned, OutputsRepository().save_cleanup_result,
                       _result_request(file_id, WorkFileOutcome.COMPLETED, None))
    _commit_proposal(owned, result)


def _result_request(file_id, outcome, error):
    return CleanupResultSave(
        file_id=file_id, outcome=outcome, error=error,
        occurred_at=_NOW + 15, advance_cursor=True)


@pytest.mark.asyncio
async def test_guard_rejects_result_without_required_error(work_env):
    """删除失败结果必须携带按公共登记构造的错误对象。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_public_delivery(owned, qualification)
    _release_ok(owned, file_id)
    plan = _proposal(
        owned, OutputsRepository().save_cleanup_result, _result_request(
            file_id, WorkFileOutcome.COMPLETED, None))
    event = next(
        event for event in plan.events
        if event.event_type == 26 and event.reason == 4)
    row = next(
        row for row in event.rows if row.table == "intermediate_files")
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values, "cleanup_state": 5}))
    _commit_proposal(owned, plan, (replace(event, rows=(variant,)),),
                     rejection="清理失败必须保存结构化错误")
    _commit_proposal(owned, plan)


@pytest.mark.asyncio
async def test_guard_rejects_completed_with_error(work_env):
    """完成结果不得携带清理错误。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_public_delivery(owned, qualification)
    _release_ok(owned, file_id)
    failure = WorkFileFailure(
        code="work_file_delete_failed",
        details={"delivery_id": str(qualification.delivery_id)})
    plan = _proposal(
        owned, OutputsRepository().save_cleanup_result, _result_request(
            file_id, WorkFileOutcome.FAILED, failure))
    event = next(
        event for event in plan.events
        if event.event_type == 26 and event.reason == 4)
    row = next(
        row for row in event.rows if row.table == "intermediate_files")
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values, "cleanup_state": 4}))
    _commit_proposal(owned, plan, (replace(event, rows=(variant,)),),
                     rejection="清理完成不得携带错误")
    _commit_proposal(owned, plan)


@pytest.mark.asyncio
async def test_guard_rejects_intent_with_retention_change(work_env):
    """意图事件只推进清理状态，不得改动保留状态。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_public_delivery(owned, qualification)
    _release_ok(owned, file_id)
    plan = _proposal(
        owned, OutputsRepository().save_cleanup_intent, CleanupIntent(file_id=file_id, occurred_at=_NOW + 14))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row,
        before=replace(row.before, values={**row.before.values, "retention_state": 2}),
        after=replace(row.after, values={**row.after.values, "retention_state": 1}))
    _commit_proposal(owned, plan, (replace(event, rows=(variant,)),),
                     rejection="retention_state")
    _commit_proposal(owned, plan)
