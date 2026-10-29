"""完整默认会话在高位游标绕回时，保持初始化确定的历史上界。"""

import pytest

from camctl.outputs.work_files import WorkFileContext, WorkFileLimits, clean_work_files
from camctl.persistence.repositories.outputs import OutputsRepository

from ..support.database import business_database
from ..support.bootstrap_read_cases import work_file_case, release_work_consumer, default_work_session

pytestmark = pytest.mark.asyncio


async def test_late_release_above_fixed_ceiling_does_not_use_history_budget_or_cursor(
        tmp_path, capture_resources, monkeypatch):
    world = await work_file_case(tmp_path, capture_resources, count=3)
    first, second, high = (row[0] for row in world.files)
    assert first < second < high
    try:
        release_work_consumer(world, 2)
        async with business_database(world.path) as database:
            previous = await clean_work_files(WorkFileContext(
                repository=OutputsRepository(), database=database,
                staging=world.paths[high].parents[1], occurred_at=world.now,
                limits=WorkFileLimits(32, 1)))
        assert previous.checked == 1 and not world.paths[high].exists()
        assert world.owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (high,)
        release_work_consumer(world, 0)
        reads = tuple(world.driver.reads)
        result = await default_work_session(world, monkeypatch, limit=2,
            on_initialized=lambda owned: release_work_consumer(world, 1, owned))
        assert result["ceilings"] == [first]
        assert set(result["cleaned"]) == {first, second} and len(result["cleaned"]) == 2
        assert world.owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (first,)
        assert world.owned.connection.execute("SELECT cleanup_state FROM intermediate_files ORDER BY id").fetchall() == [(4,), (4,), (4,)]
        assert all(not path.exists() for path in world.paths.values())
        assert tuple(world.driver.reads) == reads
    finally:
        world.owned.connection.close()
