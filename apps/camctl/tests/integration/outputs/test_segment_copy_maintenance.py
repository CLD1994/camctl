"""分段来源终态之后，检查副本按真实等待者和文件责任清理。"""

import asyncio
from pathlib import Path
from decimal import Decimal
import json

import pytest

from camctl.contracts.enums import enum_for
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.outputs.work_files import (
    WorkFileContext, WorkFileLimits, WorkFileSingleOutcome, clean_one_work_file,
)
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.supervision import Supervisor

from ..capture.test_capture_contract import _NOW
from ..capture.test_input_copy import _CONTENT
from ..support.bootstrap_read_cases import segment_input_case

pytestmark = pytest.mark.asyncio


async def _published_check_copy(tmp_path, capture_resources, monkeypatch, *, waiting=False):
    world = await segment_input_case(tmp_path, capture_resources, monkeypatch, (60,),
        early_waiters=(120,) if waiting else (), read_retry_s=Decimal(3), segment_size=4 * 1024 * 1024,
        result_check={"max_attempts": 3, "call_timeout_s": Decimal("1.25"), "retry_interval_s": Decimal(2)})
    try:
        await world.publish()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?',
            (world.source_id,)).fetchone() == (int(enum_for("actions.status").SUCCEEDED),)
        assert len(world.early) == int(waiting)
        assert world.wall[0] == _NOW + 80_000_000 and world.mono[0] == 85_000_000_000
        action, = world.owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=?', (world.source_id,)).fetchone()
        assert json.loads(action)['target_duration_ms'] == 60_000
        assert world.owned.connection.execute("SELECT kind,max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_runs WHERE kind IN (2,3,7) ORDER BY kind").fetchall() == [
            (2, 3, '10', '3'), (3, 3, '10', '3'), (7, 3, '1.25', '2')]
        budget, = world.owned.connection.execute('SELECT budget_json FROM segment_inspections').fetchone()
        assert json.loads(budget) == {'max_attempts': 3, 'timeout_s': 10, 'retry_interval_s': 3}
        assert len(world.reader.sessions) == len(world.reader.streams) == 1
        end = world.reader.sessions[0].poll_stopped()
        assert end is not None and end.stopped and end.error is None and end.bytes_read == len(_CONTENT)
        assert world.reader.streams[0].closed
        assert len(world.tool_log.read_text().splitlines()) == 1
        assert len(world.inputs) == 1
        file_id, relative_path, _state = world.inputs[0]
        path = Path(world.cfg.paths.staging) / relative_path
        assert path.is_file()
        return world, file_id, path
    except BaseException:
        await world.close()
        raise


def _context(world, executor):
    return WorkFileContext(
        repository=OutputsRepository(), database=world.database, staging=Path(world.cfg.paths.staging),
        occurred_at=_NOW + 90_000_000,
        limits=WorkFileLimits(batch_size=2, limit_per_run=4), executor=executor,
    )


async def test_unbound_inspection_copy_waits_for_actual_file_task_then_is_cleaned(tmp_path, capture_resources, monkeypatch):
    world, file_id, path = await _published_check_copy(tmp_path, capture_resources, monkeypatch)
    owned, source = world.owned, world.source_id
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
        marker_wait = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait((marker_wait, operation), timeout=5,
                                         return_when=asyncio.FIRST_COMPLETED)
            if operation in done:
                await operation
                pytest.fail("实际读取在到达标记之前结束")
            assert marker_wait in done, "等待实际读取标记超时"
        finally:
            marker_wait.cancel()
            await asyncio.gather(marker_wait, return_exceptions=True)
        before = owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (source,),
        ).fetchone()
        kept = await clean_one_work_file(file_id, _context(world, executor))
        assert kept.outcome is WorkFileSingleOutcome.KEPT
        assert path.exists() and file_id in executor.unfinished_files()
        finish.set()
        assert len(await asyncio.wait_for(asyncio.shield(operation), 5)) == path.stat().st_size
        assert file_id not in executor.unfinished_files()
        cleaned = await clean_one_work_file(file_id, _context(world, executor))
        assert cleaned.outcome is WorkFileSingleOutcome.DELETED
        assert not path.exists()
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (source,),
        ).fetchone() == before
    finally:
        finish.set()
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(operation, return_exceptions=True)), 5)
        finally:
            await asyncio.wait_for(asyncio.shield(world.close()), 10)


async def test_inspection_copy_delete_failure_is_retried_after_database_reopen(
    tmp_path, capture_resources, monkeypatch,
):
    world, file_id, path = await _published_check_copy(tmp_path, capture_resources, monkeypatch)
    owned, source = world.owned, world.source_id
    executor = FileTaskExecutor(Supervisor())
    remove = Path.unlink
    removals = []

    def refuse_once(candidate, *args, **kwargs):
        if candidate == path:
            removals.append(candidate)
            if len(removals) == 1:
                raise OSError("检查副本删除被拒绝")
        return remove(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_once)
    before = owned.connection.execute(
        "SELECT status,error_code,error_details_json FROM actions WHERE id=?", (source,),
    ).fetchone()
    try:
        failed = await clean_one_work_file(file_id, _context(world, executor))
        assert failed.outcome is WorkFileSingleOutcome.FAILED
        assert path.exists() and len(removals) == 1
        assert owned.connection.execute(
            "SELECT retention_state,cleanup_state FROM intermediate_files WHERE id=?",
            (file_id,),
        ).fetchone() == (
            int(enum_for("intermediate_files.retention_state").RELEASABLE),
            int(enum_for("intermediate_files.cleanup_state").FAILED),
        )
        await world.executor.drain_required()
        world.database = await capture_resources.restart_database(world.path)
        owned.connection.close()
        world.owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        reopened = world.owned
        retried = await clean_one_work_file(file_id, _context(world, executor))
        assert retried.outcome is WorkFileSingleOutcome.DELETED
        assert not path.exists() and removals == [path, path]
        assert reopened.connection.execute(
            "SELECT status,error_code,error_details_json FROM actions WHERE id=?", (source,),
        ).fetchone() == before
    finally:
        await world.close()


async def test_canceled_bound_waiter_releases_copy_after_its_original_settlement(tmp_path, capture_resources, monkeypatch):
    world, file_id, path = await _published_check_copy(
        tmp_path, capture_resources, monkeypatch, waiting=True)
    owned, source, waiters = world.owned, world.source_id, world.early
    executor = FileTaskExecutor(Supervisor())
    try:
        facts = OutputsRepository().load_work_file_state(file_id, owned)
        assert not facts.owner_finished
        kept = await clean_one_work_file(file_id, _context(world, executor))
        assert kept.outcome is WorkFileSingleOutcome.KEPT and path.exists()
        waiter, _ = waiters[0]
        await world.cancel(waiter)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (waiter,),
        ).fetchone() == (int(enum_for("actions.status").CANCELED),)
        assert OutputsRepository().load_work_file_state(file_id, owned).owner_finished
        cleaned = await clean_one_work_file(file_id, _context(world, executor))
        assert cleaned.outcome is WorkFileSingleOutcome.DELETED and not path.exists()
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (source,)).fetchone() == (int(enum_for("actions.status").SUCCEEDED),)
    finally:
        await world.close()
