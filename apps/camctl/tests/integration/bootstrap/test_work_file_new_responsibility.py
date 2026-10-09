"""本次业务形成的首次责任与历史检查共享记录，额度分别使用。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.outputs.work_files import WorkFileLimits
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..outputs.test_work_files import (
    _cancel_delivery, _file_state, _release_ok, _seed_action_candidate,
    local_read, read_targets, work_env,  # noqa: F401
)


@pytest.mark.asyncio
async def test_newly_released_file_is_first_cleaned_after_history_budget_ends(work_env):
    owned, roots, qualification = work_env
    _seed_action_candidate(owned, roots, file_id=900)
    runtime = WorkFileRuntime(staging=roots.staging, limits=WorkFileLimits(32, 1))
    runtime.initialize(owned)
    database = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    context = SimpleNamespace(open_connection=lambda: open_existing(database, DbOpenMode.EXISTING_RW, DbConfig()),
                              clock=SimpleNamespace(utc_micros=lambda: 1_736_935_220_000_000))
    await runtime.flow(context)
    await runtime.settle()
    assert _file_state(owned, 900)[:2] == (2, 4)
    assert _file_state(owned, qualification.target_file_id)[:2] == (1, 1)
    before = owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone()

    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, qualification.target_file_id)
    await runtime.flow(context)
    await runtime.settle()

    assert _file_state(owned, qualification.target_file_id)[:2] == (2, 4)
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == before
    assert runtime.history.scan.remaining == 0
    assert len(runtime.processed) == 2


@pytest.mark.asyncio
async def test_old_terminal_required_file_is_a_finite_history_responsibility(work_env):
    owned, roots, qualification = work_env
    _cancel_delivery(owned, qualification.delivery_id)
    file_id = qualification.target_file_id
    assert _file_state(owned, file_id)[:2] == (1, 1)
    runtime = WorkFileRuntime(staging=roots.staging, limits=WorkFileLimits(32, 1))

    runtime.initialize(owned)

    assert runtime.history.scan.ceiling == file_id
    database = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    context = SimpleNamespace(
        open_connection=lambda: open_existing(database, DbOpenMode.EXISTING_RW, DbConfig()),
        clock=SimpleNamespace(utc_micros=lambda: 1_736_935_220_000_000))
    await runtime.flow(context)
    await runtime.settle()
    assert _file_state(owned, file_id)[:2] == (2, 4)
    assert runtime.history.scan.remaining == 0
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (file_id,)
