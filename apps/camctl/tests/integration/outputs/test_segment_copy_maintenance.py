"""分段来源终态之后，检查副本按真实等待者和文件责任清理。"""

import asyncio
from pathlib import Path

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.outputs import work_files
from camctl.outputs.work_files import (
    WorkFileContext, WorkFileLimits, WorkFileSingleOutcome, clean_one_work_file,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository, SettleCanceledObtain
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.supervision import Supervisor

from ..capture.test_action6_segmented_recovery import (
    _entries, _overrun_recording, _publish_ordinary_result,
)
from ..capture.test_record_segment_scope_recovery import _cancel
from ..capture.test_capture_contract import _NOW
from .test_segment_default_scope import _obtain

pytestmark = pytest.mark.asyncio


async def _published_check_copy(tmp_path, *, waiting=False):
    owned, runtime, source, handler, tools = await _overrun_recording(tmp_path, (60,))
    waiters = [_obtain(owned, runtime, source, "default", 120)] if waiting else []
    await _publish_ordinary_result(runtime, owned, source, handler, _entries(1))
    assert runtime.action(source)["status"] == int(enum_for("actions.status").SUCCEEDED)
    assert len(tools.calls) == 1
    file_id, relative_path = owned.connection.execute(
        "SELECT f.id,f.relative_path FROM segment_inspections i"
        " JOIN file_copies c ON c.inspection_id=i.id"
        " JOIN intermediate_files f ON f.id=c.target_file_id"
        " WHERE i.action_id=?", (source,),
    ).fetchone()
    path = tmp_path / "staging" / relative_path
    assert path.is_file()
    return owned, runtime, source, file_id, path, waiters


def _context(owned, tmp_path, executor):
    return WorkFileContext(
        repository=OutputsRepository(), owned=owned, staging=tmp_path / "staging",
        occurred_at=_NOW + 90_000_000,
        limits=WorkFileLimits(batch_size=2, limit_per_run=4), executor=executor,
    )


async def test_unbound_inspection_copy_waits_for_actual_file_task_then_is_cleaned(tmp_path):
    owned, _, source, file_id, path, _ = await _published_check_copy(tmp_path)
    executor = FileTaskExecutor(Supervisor())
    entered, finish = asyncio.Event(), asyncio.Event()

    async def actual_read(_control):
        # 使用真实文件和执行器资格，直至实际读取收场才释放。
        with path.open("rb") as stream:
            first = stream.read(4)
            entered.set()
            await finish.wait()
            return first + stream.read()

    operation = asyncio.create_task(executor.run_owned_async_file_task(AsyncFileTask(
        FileTaskId("maintenance-check-source-read"), (file_id,),
        "read_file", "检查副本实际读取", actual_read)))
    try:
        await entered.wait()
        before = owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (source,),
        ).fetchone()
        kept = await clean_one_work_file(file_id, _context(owned, tmp_path, executor))
        assert kept.outcome is WorkFileSingleOutcome.KEPT
        assert path.exists() and file_id in executor.unfinished_files()
        finish.set()
        assert len(await operation) == path.stat().st_size
        assert file_id not in executor.unfinished_files()
        cleaned = await clean_one_work_file(file_id, _context(owned, tmp_path, executor))
        assert cleaned.outcome is WorkFileSingleOutcome.DELETED
        assert not path.exists()
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (source,),
        ).fetchone() == before
    finally:
        finish.set()
        await operation
        owned.connection.close()


async def test_inspection_copy_delete_failure_is_retried_after_database_reopen(
    tmp_path, monkeypatch,
):
    owned, _, source, file_id, path, _ = await _published_check_copy(tmp_path)
    database = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    executor = FileTaskExecutor(Supervisor())
    remove = work_files._remove_work_file
    removals = []

    def refuse_once(candidate):
        removals.append(candidate)
        if len(removals) == 1:
            raise OSError("检查副本删除被拒绝")
        remove(candidate)

    monkeypatch.setattr(work_files, "_remove_work_file", refuse_once)
    before = owned.connection.execute(
        "SELECT status,error_code,error_details_json FROM actions WHERE id=?", (source,),
    ).fetchone()
    try:
        failed = await clean_one_work_file(file_id, _context(owned, tmp_path, executor))
        assert failed.outcome is WorkFileSingleOutcome.FAILED
        assert path.exists() and len(removals) == 1
        assert owned.connection.execute(
            "SELECT retention_state,cleanup_state FROM intermediate_files WHERE id=?",
            (file_id,),
        ).fetchone() == (
            int(enum_for("intermediate_files.retention_state").RELEASABLE),
            int(enum_for("intermediate_files.cleanup_state").FAILED),
        )
    finally:
        owned.connection.close()

    reopened = open_existing(database, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        retried = await clean_one_work_file(file_id, _context(reopened, tmp_path, executor))
        assert retried.outcome is WorkFileSingleOutcome.DELETED
        assert not path.exists() and removals == [path, path]
        assert reopened.connection.execute(
            "SELECT status,error_code,error_details_json FROM actions WHERE id=?", (source,),
        ).fetchone() == before
    finally:
        reopened.connection.close()


async def test_canceled_bound_waiter_releases_copy_after_its_original_settlement(tmp_path):
    owned, runtime, source, file_id, path, waiters = await _published_check_copy(
        tmp_path, waiting=True)
    executor = FileTaskExecutor(Supervisor())
    try:
        facts = OutputsRepository().load_work_file_state(file_id, owned)
        assert not facts.owner_finished
        kept = await clean_one_work_file(file_id, _context(owned, tmp_path, executor))
        assert kept.outcome is WorkFileSingleOutcome.KEPT and path.exists()
        waiter, _ = waiters[0]
        _cancel(owned, runtime, waiter)
        settled = OutputsRepository().settle_canceled_obtain(
            SettleCanceledObtain(waiter, runtime.wall_us(), ()),
            new_operation_key(), owned)
        assert settled.kind is DbOutcomeKind.COMPLETED, settled.error
        assert settled.value.complete
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (waiter,),
        ).fetchone() == (int(enum_for("actions.status").CANCELED),)
        assert OutputsRepository().load_work_file_state(file_id, owned).owner_finished
        cleaned = await clean_one_work_file(file_id, _context(owned, tmp_path, executor))
        assert cleaned.outcome is WorkFileSingleOutcome.DELETED and not path.exists()
        assert runtime.action(source)["status"] == int(enum_for("actions.status").SUCCEEDED)
    finally:
        owned.connection.close()
