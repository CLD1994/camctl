"""正常默认装配按一次运行额度维护真实取回中间文件。"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.contracts.values import new_operation_key
from camctl.devices.read_session import ReadSession
from camctl.outputs.work_files import RetentionRelease
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_output_binding_changes import (
    Catalog, Driver, _BrokenStream, _CONTENT, _NOW, _accept, _registry, _save_photos, environment,
)

pytestmark = pytest.mark.asyncio


class _InterruptedReadDriver(Driver):
    """每份文件保存四字节后可靠结束，用原接口留下真实读取进度。"""

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        return ReadSession(source, offset, _BrokenStream(_CONTENT[offset:]), idle_timeout_s)


async def _history_cleanup(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])
    interrupted = _InterruptedReadDriver(owned)
    factory = session_obtain_assembly(
        devices=cfg.devices, drivers=_registry(interrupted), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    await obtain_flow(factory)(context)
    failed = session_obtain_assembly(
        devices={}, drivers=_registry(interrupted), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    await obtain_flow(failed)(context)
    # 逐份交付结束后，真实取回流程下一轮汇总原业务结果。
    await obtain_flow(failed)(context)
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE type=4").fetchone() == (4,)
    assert owned.connection.execute("SELECT committed_bytes FROM file_copies ORDER BY id").fetchall() == [(4,), (4,)]
    for (file_id,) in owned.connection.execute("SELECT id FROM intermediate_files ORDER BY id").fetchall():
        released = OutputsRepository().save_retention_release(
            RetentionRelease(file_id, _NOW), new_operation_key(), owned)
        assert released.kind is DbOutcomeKind.COMPLETED, released.error
    rows = owned.connection.execute(
        "SELECT id,retention_state,cleanup_state FROM intermediate_files ORDER BY id").fetchall()
    assert len(rows) == 2
    assert all(row[1:] == (2, 2) for row in rows)
    return replace(cfg, devices={}, cleanup=replace(
        cfg.cleanup, work_file_batch_size=32, work_file_limit_per_run=1))


async def _execute(cfg, mode=CommandMode.RUN, source=None):
    deps = build_runtime(mode, cfg, catalog=Catalog())
    try:
        return await execute_command(deps, source, poll_interval_s=0.001)
    finally:
        close_runtime(deps)


async def test_default_run_keeps_one_budget_across_rounds_and_resumes_next_run(environment):
    cfg = await _history_cleanup(environment)
    owned = environment[1]
    published = {path.name: path.read_bytes() for path in Path(cfg.paths.ready).glob("*.jpg")}
    before = owned.connection.execute("SELECT status FROM actions ORDER BY id").fetchall()

    first = await _execute(cfg)

    assert first.succeeded, first
    assert owned.connection.execute(
        "SELECT cleanup_state FROM intermediate_files ORDER BY id").fetchall() == [(4,), (2,)]
    assert owned.connection.execute(
        "SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (1,)
    assert owned.connection.execute("SELECT status FROM actions ORDER BY id").fetchall() == before
    assert {path.name: path.read_bytes() for path in Path(cfg.paths.ready).glob("*.jpg")} == published

    second = await _execute(cfg)

    assert second.succeeded, second
    assert owned.connection.execute(
        "SELECT cleanup_state FROM intermediate_files ORDER BY id").fetchall() == [(4,), (4,)]
    assert owned.connection.execute(
        "SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (2,)


async def test_submit_keeps_work_files_and_history_position(environment):
    cfg = await _history_cleanup(environment)
    owned = environment[1]
    before = owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id").fetchall()
    position = owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone()
    source = ParsedInput("plan.json", {
        "request_id": "3", "created_at": "2026-01-15 08:00:00", "name": "稍后取回",
        "actions": [{"name": "取回", "type": "obtain_action_outputs",
            "scheduled_at": "2099-01-01 09:00:00",
            "params": {"source": {"plan_instance_id": "1", "group": "files"}}}],
    })

    result = await _execute(cfg, CommandMode.SUBMIT, source)

    assert result.succeeded, result
    assert owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id").fetchall() == before
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == position


async def test_clock_restricted_run_keeps_ordinary_work_cleanup(environment):
    cfg = await _history_cleanup(environment)
    owned = environment[1]
    before = owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id").fetchall()
    position = owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone()

    result = await _execute(replace(cfg, clock=replace(cfg.clock, min_plausible_date=date(2099, 1, 1))))

    assert not result.succeeded
    assert result.reason == "clock_invalid"
    assert owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id").fetchall() == before
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == position
