"""真实受理、取回会话及清理会话共同维护独立交付与正式源。"""

from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.bootstrap.obtain_assembly import obtain_flow
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories.acceptance import AcceptanceRepository

from ..bootstrap.test_output_binding_changes import (
    Catalog, _CONTENT, _NOW, _registry, _save_photos, environment,  # noqa: F401
)
from ..support.outputs import obtain_factory

pytestmark = pytest.mark.asyncio


async def _submit(context, request_id, action):
    receipt = await accept_input(
        ParsedInput("outputs.json", {
            "request_id": str(request_id), "created_at": "2026-01-15 08:00:00",
            "name": "产物生命周期", "actions": [action],
        }),
        AcceptanceContext(mode=CommandMode.RUN, catalog=Catalog(),
                          repository=AcceptanceRepository(), clock=context.clock),
        new_operation_key(), context.database,
    )
    assert receipt.plan_id is not None, receipt
    return receipt


async def test_independent_obtain_flows_preserve_deliveries_after_source_cleanup(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    output_id, source_id = owned.connection.execute(
        "SELECT o.id,o.source_action_id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()
    for request_id in (100, 101):
        await _submit(context, request_id, {
            "name": "取回", "type": "obtain_action_outputs",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"source": {"action_instance_id": str(source_id)}},
        })
    factory = obtain_factory(
        devices=cfg.devices, drivers=_registry(driver),
        staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready),
        processing=Path(cfg.paths.processing), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0, segment_size=4,
    )
    flow = obtain_flow(factory)
    for _ in range(3):
        await flow(context)
    deliveries = owned.connection.execute(
        "SELECT id,output_id,file_name,status FROM deliveries ORDER BY id").fetchall()
    assert len(deliveries) == 2
    assert len({row[0] for row in deliveries}) == len({row[2] for row in deliveries}) == 2
    assert all(row[1] == output_id and row[3] == 5 for row in deliveries)
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE type=4 ORDER BY id").fetchall() == [(3,), (3,)]
    assert owned.connection.execute(
        "SELECT availability,cleanup_status FROM outputs WHERE id=?", (output_id,),
    ).fetchone() == (1, 1)
    before = {row[2]: (Path(cfg.paths.ready) / row[2]).read_bytes() for row in deliveries}
    assert set(before.values()) == {_CONTENT}

    # 同一请求再次提交仍由受理复用原计划，不追加交付或再次读取。
    reads = tuple(driver.reads)
    await _submit(context, 100, {
        "name": "取回", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"action_instance_id": str(source_id)}},
    })
    await flow(context)
    assert driver.reads == list(reads)
    assert owned.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (2,)

    await _submit(context, 102, {
        "name": "清理", "type": "delete_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00", "params": {"output_ids": [str(output_id)]},
    })
    clean = cleanup_flow(session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver),
        max_delete_attempts=1, max_query_attempts=1,
        staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    ))
    await clean(context)
    await clean.settle()
    assert len(driver.deletes) == 1 and driver.queries == []
    assert owned.connection.execute(
        "SELECT availability,cleanup_status FROM outputs WHERE id=?", (output_id,),
    ).fetchone() == (3, 4)
    assert owned.connection.execute("SELECT status FROM actions WHERE type=5").fetchone() == (3,)
    assert {name: (Path(cfg.paths.ready) / name).read_bytes() for name in before} == before
