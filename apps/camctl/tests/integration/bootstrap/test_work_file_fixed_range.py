"""正常维护从高位游标绕回时，历史范围保持初始化的固定上界。"""

from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.contracts.values import new_operation_key
from camctl.outputs.work_files import (
    RetentionRelease, WorkFileContext, WorkFileLimits, clean_work_files,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import OutputsRepository, register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..outputs.test_work_files import (
    _file_state, _seed_action_candidate,
    local_read, read_targets, work_env,  # noqa: F401
)
from ..outputs.test_qualification import _NOW, _seed_action

register_operation_guards()
register_capture_guards()
register_outputs_guards()


def _release(owned, repository, file_id, occurred_at):
    released = repository.save_retention_release(
        RetentionRelease(file_id, occurred_at), new_operation_key(), owned)
    assert released.kind is DbOutcomeKind.COMPLETED, released.error


@pytest.mark.asyncio
async def test_late_release_above_fixed_ceiling_does_not_use_history_budget_or_cursor(work_env):
    owned, roots, _qualification = work_env
    repository = OutputsRepository()
    _seed_action(owned.connection, 13, 1, action_type=2, status=2)
    owned.connection.commit()
    # 三个已登记录像副本的身份先固定。100/500 仍需保留，900 是前次
    # 历史清理的唯一候选；前次真实清理产生可靠的高位继续位置。
    for file_id in (100, 500, 900):
        _seed_action_candidate(owned, roots, file_id=file_id)
    owned.connection.execute(
        "UPDATE intermediate_files SET retention_state=1, cleanup_state=1"
        " WHERE id IN (100, 500)")
    owned.connection.execute("UPDATE intermediate_files SET owner_action_id=12 WHERE id=100")
    owned.connection.execute("UPDATE intermediate_files SET owner_action_id=13 WHERE id=500")
    owned.connection.commit()
    previous = await clean_work_files(WorkFileContext(
        repository=repository, owned=owned, staging=roots.staging,
        occurred_at=_NOW + 20, limits=WorkFileLimits(32, 1)))
    assert previous.checked == 1
    assert _file_state(owned, 900)[:2] == (2, 4)
    assert owned.connection.execute(
        "SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (900,)

    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.commit()
    _release(owned, repository, 100, _NOW + 30)
    runtime = WorkFileRuntime(staging=roots.staging, limits=WorkFileLimits(32, 2))
    runtime.initialize(owned)
    assert runtime.history.scan.start_after == 900
    assert runtime.history.scan.ceiling == 100

    # 本次业务结束对 500 的保留责任。它可以由首次入口清理，不能
    # 因历史绕回上界错误而进入本次固定范围、占额度或推进其游标。
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=13")
    owned.connection.commit()
    _release(owned, repository, 500, _NOW + 40)
    database = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    opened = []

    def open_connection():
        connection = open_existing(database, DbOpenMode.EXISTING_RW, DbConfig())
        opened.append(connection)
        assert connection.connection is not owned.connection
        return connection

    context = SimpleNamespace(
        open_connection=open_connection,
        clock=SimpleNamespace(utc_micros=lambda: _NOW + 50))
    try:
        await runtime.flow(context)
        await runtime.settle()
        assert opened
        for connection in opened:
            with pytest.raises(sqlite3.ProgrammingError):
                connection.connection.execute("SELECT 1")
        assert _file_state(owned, 100)[:2] == (2, 4)
        assert runtime.history.scan.remaining == 1
        assert owned.connection.execute(
            "SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (100,)
        assert runtime.history.scan.ceiling == 100
    finally:
        await runtime.settle()
