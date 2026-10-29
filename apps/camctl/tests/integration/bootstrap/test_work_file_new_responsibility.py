"""默认会话分别消费历史额度与本次新形成的文件责任。"""

import asyncio
from pathlib import Path

import pytest

from ..support.bootstrap_read_cases import work_file_case, release_work_consumer, default_work_session

pytestmark = pytest.mark.asyncio


async def test_newly_released_file_is_first_cleaned_after_history_budget_ends(tmp_path, capture_resources, monkeypatch):
    world = await work_file_case(tmp_path, capture_resources)
    first, second = (row[0] for row in world.files)
    release_work_consumer(world, 0)
    calls = tuple(world.driver.reads)
    used = asyncio.Event()
    loop = asyncio.get_running_loop()

    def after_first(file_id, owned):
        if file_id == first:
            loop.call_soon_threadsafe(used.set)

    task = asyncio.create_task(default_work_session(world, monkeypatch, limit=1, on_cleaned=after_first))
    try:
        await asyncio.wait_for(used.wait(), 10)
        from camctl.acceptance.service import CommandMode
        from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
        from ..support.session import parsed_plan
        source = await parsed_plan(tmp_path, {
            "request_id": "200", "created_at": "2026-01-15 09:00:00", "name": "历史额度用完后取消",
            "actions": [{"name": "取消第二消费者", "type": "cancel_task",
                "params": {"target": {"action_instance_id": str(world.files[1][1])}}}]})
        deps = build_runtime(CommandMode.SUBMIT, world.cfg, catalog=world.catalog)
        try:
            submitted = await execute_command(deps, source)
            assert submitted.succeeded, submitted
        finally:
            close_runtime(deps)
        result = await task
        assert result["ceilings"] == [first]
        assert result["cleaned"] == [first, second]
        assert world.owned.connection.execute("SELECT cleanup_state FROM intermediate_files ORDER BY id").fetchall() == [(4,), (4,)]
        assert world.owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (first,)
        assert tuple(world.driver.reads) == calls
        assert all(not path.exists() for path in world.paths.values())
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        world.owned.connection.close()


async def test_old_terminal_required_file_is_a_finite_history_responsibility(tmp_path, capture_resources, monkeypatch):
    world = await work_file_case(tmp_path, capture_resources, count=1)
    file_id = world.files[0][0]
    release_work_consumer(world, 0, release=False)
    assert world.owned.connection.execute("SELECT retention_state,cleanup_state FROM intermediate_files WHERE id=?",
        (file_id,)).fetchone() == (1, 1)
    try:
        result = await default_work_session(world, monkeypatch, limit=1)
        assert result["ceilings"] == [file_id] and result["cleaned"] == [file_id]
        assert world.owned.connection.execute("SELECT retention_state,cleanup_state FROM intermediate_files WHERE id=?",
            (file_id,)).fetchone() == (2, 4)
        assert world.owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (file_id,)
    finally:
        world.owned.connection.close()
