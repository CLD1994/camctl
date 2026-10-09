"""目录切换后，真实取回建档继续全库中间文件身份。"""

from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.outputs.work_files import WorkFileContext, WorkFileLimits, clean_work_files
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_output_binding_changes import (
    _NOW, _accept, _registry, _save_photos, environment,
)

pytestmark = pytest.mark.asyncio


async def _obtain(cfg, owned, context, driver, request_id):
    _accept(owned, request_id, [{
        "name": "取回", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}},
    }])
    factory = session_obtain_assembly(
        devices=cfg.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=1024,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0,
    )
    for _ in range(3):
        await obtain_flow(factory)(context)
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE type=4 ORDER BY id").fetchall() == (
        [(3,)] if request_id == "2" else [(3,), (3,)])


async def test_switch_keeps_old_file_facts_and_allocates_new_global_ids(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    await _obtain(cfg, owned, context, driver, "2")
    old_files = owned.connection.execute(
        "SELECT id FROM intermediate_files ORDER BY id").fetchall()
    assert len(old_files) == 2
    # 主程序消费已发布副本，软件清理入口结束原 staging 责任。
    for published in Path(cfg.paths.ready).iterdir():
        claimed = Path(cfg.paths.processing) / published.name
        published.rename(claimed)
        claimed.unlink()
    await clean_work_files(WorkFileContext(
        repository=OutputsRepository(), owned=owned, staging=Path(cfg.paths.staging),
        occurred_at=_NOW, limits=WorkFileLimits(batch_size=10, limit_per_run=10),
    ))
    before = tuple(owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id"))
    identity = owned.metadata.instance_id
    moved = replace(cfg, paths=replace(cfg.paths, **{
        name: str(Path(cfg.paths.state_db).parent / f"new-{name}")
        for name in ("staging", "ready", "processing")
    }))

    switched = initialize_state(moved, Path(cfg.paths.state_db))

    assert switched.outcome is InitOutcome.SWITCHED, switched
    assert owned.connection.execute("SELECT instance_id FROM database_metadata").fetchone() == (identity,)
    assert tuple(owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id")) == before
    await _obtain(moved, owned, context, driver, "3")
    after = tuple(owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id"))
    assert after[:len(before)] == before
    assert [row[0] for row in after[len(before):]] == [old_files[-1][0] + 1, old_files[-1][0] + 2]
    assert len(tuple(Path(moved.paths.ready).glob("*.jpg"))) == 2
    assert not tuple(Path(cfg.paths.ready).iterdir())
