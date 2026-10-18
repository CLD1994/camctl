"""原尝试配置、原结果保存及后轮真实授票之间的合同。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.recovery import RecoveryBoundary
from camctl.outputs.cleanup_flow import CleanupQueryConsumer, _begin_cleanup_intent, advance_cleanup
from camctl.persistence.models import DbOutcomeKind

from .test_osmo_file_cleanup import world, _factory, _lost_intent
from .test_output_binding_changes import _NOW

pytestmark = pytest.mark.asyncio


def _configure(world, *, interval, timeout=Decimal("7.125")):
    declaration = world.cfg.devices["cam-1"]
    world.cfg = replace(world.cfg, devices={**world.cfg.devices, "cam-1": {
        **declaration, "cleanup": {"delete_timeout_s": timeout, "query_timeout_s": timeout + 1,
            "delete_retry_interval_s": interval, "query_retry_interval_s": interval}}})


async def _lost_operation(world, operation):
    item_id, ticket, horizon = _lost_intent(world, "delete")
    if operation == "query":
        # 原 D 先由正式装配合法恢复，只留下实际效果未知；原 Q 责任从此开始。
        await advance_cleanup(_factory(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon)(world.owned))
        runtime = _factory(world)(world.owned).for_operation(item_id, "query")
        action_id = world.owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
        begun = _begin_cleanup_intent(runtime, AttemptIntent(operation="query", action_id=action_id,
            kind=OperationKind.CHECK_FILE_EXISTS, target=AttemptTarget(cleanup_item_id=item_id),
            query_purpose=None, config=runtime.query_config, occurred_at=_NOW), CleanupQueryConsumer.AFTER_DELETE)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        ticket = begun.value.ticket
        horizon = world.owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        runtime.pending_intents.clear()
    return item_id, ticket, horizon


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize("interval", [Decimal("0"), Decimal("0.625")])
async def test_old_device_result_keeps_original_config_and_authorizes_new_ticket(world, operation, interval):
    _configure(world, interval=interval)
    item_id, ticket, horizon = await _lost_operation(world, operation)
    old = world.owned.connection.execute("SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()
    _configure(world, interval=Decimal("0"), timeout=Decimal("17.375"))
    fresh = world.context.open_connection()
    try:
        factory = _factory(world, max_delete_attempts=5, max_query_attempts=4,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=horizon)
        await advance_cleanup(factory(fresh))
        assert world.channel.operations == []
        recovered_wait = fresh.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone()
        if operation == "query":
            world.channel.behaviors = ["unreceived"]
        for _ in range(6):
            await advance_cleanup(factory(fresh))
            if world.channel.operations == (["query", "delete"] if operation == "delete" else ["query"]):
                break
        assert world.channel.operations == (["query", "delete"] if operation == "delete" else ["query"])
        assert fresh.connection.execute("SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == old
        assert fresh.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2,)
        new = fresh.connection.execute("SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=2", (ticket.run_id,)).fetchone()
        assert new == ((5, "17.375", "0") if operation == "delete" else (4, "18.375", "0"))
        assert recovered_wait == (1,)
        from camctl.outputs.cleanup_results import cleanup_attempt_config
        restored = cleanup_attempt_config(fresh, ticket, database_id=fresh.metadata.instance_id, item_id=item_id)
        assert restored.max_attempts == old[0]
        assert restored.timeout_s == (Decimal("7.125") if operation == "delete" else Decimal("8.125"))
        assert restored.retry_interval_s == interval
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_pending_only_actual_survives_config_read_failure_and_port_removal(world, monkeypatch, operation):
    from sqlite3 import OperationalError
    from camctl.contracts.values import new_operation_key
    from camctl.outputs import cleanup_results
    from camctl.outputs.cleanup_flow import (
        PendingCleanupDelete, PendingCleanupQuery, _control_request, _save_delete_return, _run_query,
    )
    _configure(world, interval=Decimal("0.625"))
    item_id, ticket, _ = await _lost_operation(world, operation)
    original_runtime = _factory(world)(world.owned).for_operation(item_id, operation)
    request = _control_request(original_runtime, item_id, operation, ticket)
    assert request.timeout_s == (Decimal("7.125") if operation == "delete" else Decimal("8.125"))
    world.channel.behaviors = ["unreceived"]
    actual = await getattr(world.entry.driver, "delete" if operation == "delete" else "query_file")(request)
    assert world.channel.operations == [operation]
    world.cfg = replace(world.cfg, devices={})
    runtime = _factory(world)(world.owned)
    key = new_operation_key()
    common = (world.owned.metadata.instance_id, item_id)
    if operation == "delete":
        pending = PendingCleanupDelete(*common, ticket, actual, world.entry.evidence, None, None, key)
        runtime.pending_deletes[item_id] = pending
    else:
        pending = PendingCleanupQuery(*common, CleanupQueryConsumer.AFTER_DELETE,
            ticket, actual, world.entry.evidence, None, None, key)
        runtime.pending_queries[(ticket.run_id, ticket.attempt_id)] = pending
    reader = cleanup_results.cleanup_attempt_config
    fail = [True]

    def interrupted_read(*args, **kwargs):
        if fail:
            fail.pop()
            raise OperationalError("原配置读取暂时不可用")
        return reader(*args, **kwargs)

    monkeypatch.setattr(cleanup_results, "cleanup_attempt_config", interrupted_read)

    async def save():
        if operation == "delete":
            return _save_delete_return(runtime, pending)
        return await _run_query(runtime, ticket, item_id, pending.consumer)

    with pytest.raises(OperationalError, match="原配置读取"):
        await save()
    assert pending.actual is actual and pending.key == key
    assert pending.finish is None and pending.validation_diagnostic is None
    timestamp = pending.occurred_at
    await save()
    assert pending.actual is actual and pending.key == key and pending.occurred_at == timestamp
    saved = cleanup_results.saved_cleanup_attempt(world.owned, ticket,
        database_id=world.owned.metadata.instance_id, item_id=item_id)
    assert saved.actual.outcome == actual.outcome and saved.key == key
    assert world.channel.operations == [operation]
    assert world.owned.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)


@pytest.mark.parametrize("mismatch", ["database", "item", "run", "attempt", "target", "operation", "responsibility", "projection"])
async def test_real_config_reader_rejects_foreign_or_changed_creation_without_io(world, mismatch):
    from camctl.contracts.values import ConsistencyError
    from camctl.outputs.cleanup_results import cleanup_attempt_config
    _configure(world, interval=Decimal("0.625"))
    item_id, ticket, _ = await _lost_operation(world, "delete")
    kwargs = {"database_id": world.owned.metadata.instance_id, "item_id": item_id}
    if mismatch == "database":
        kwargs["database_id"] = "different"
    elif mismatch == "item":
        kwargs["item_id"] += 1
    elif mismatch in ("run", "attempt"):
        ticket = replace(ticket, **{"run_id" if mismatch == "run" else "attempt_id": 900})
    elif mismatch == "target":
        ticket = replace(ticket, target_id="900")
    elif mismatch == "operation":
        ticket = replace(ticket, operation="query", responsibility_key=f"exists/{item_id}")
    elif mismatch == "responsibility":
        ticket = replace(ticket, responsibility_key="delete/900")
    else:
        world.owned.connection.execute("UPDATE operation_attempts SET timeout_s_json='99' WHERE run_id=?", (ticket.run_id,))
        world.owned.connection.commit()
    with pytest.raises(ConsistencyError):
        cleanup_attempt_config(world.owned, ticket, **kwargs)
    assert world.channel.operations == []


@pytest.mark.parametrize("output_id", [703, 704, 705])
@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_host_original_preview_repaired_recovery_preserves_null_config_and_new_task(world, monkeypatch, output_id, operation):
    from camctl.bootstrap import cleanup_assembly
    from .test_output_binding_changes import _NOW
    from ..outputs.test_cleanup_query_save_recovery import _start_cleanup
    action_id, item_id = _start_cleanup(world.owned, output_id, "2")
    runtime = _factory(world)(world.owned)
    begun = _begin_cleanup_intent(runtime, AttemptIntent(operation=operation, action_id=action_id,
        kind=OperationKind.DELETE_FILE if operation == "delete" else OperationKind.CHECK_FILE_EXISTS,
        target=AttemptTarget(cleanup_item_id=item_id), query_purpose=None,
        config=runtime.delete_config if operation == "delete" else runtime.query_config, occurred_at=_NOW),
        None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    ticket = begun.value.ticket
    horizon = world.owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    runtime.pending_intents.clear()
    fresh = world.context.open_connection()
    try:
        def forbidden_ports(*args):
            raise AssertionError("主机历史不能解析当前设备端口")
        monkeypatch.setattr(cleanup_assembly, "port_for", forbidden_ports)
        factory = _factory(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon)
        await advance_cleanup(factory(fresh))
        assert fresh.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (0,)
        for _ in range(5):
            await advance_cleanup(factory(fresh))
            if fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (4,):
                break
        assert not world.paths[output_id].exists()
        assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (4,)
        assert fresh.connection.execute("SELECT timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (None, None)
        assert world.channel.operations == []
        assert all(path.exists() for identity, path in world.paths.items() if identity != output_id)
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_already_saved_missing_wait_is_diagnosed_without_rewriting_or_replaying(world, operation):
    from camctl.contracts.values import ConsistencyError, new_operation_key
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.validation import validate_outcome
    from camctl.outputs.cleanup_recovery import recover_cleanup_call
    _configure(world, interval=Decimal("0"))
    item_id, ticket, horizon = await _lost_operation(world, operation)
    runtime = _factory(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=horizon)(world.owned)
    recover_cleanup_call(runtime, item_id)
    pending = (runtime.pending_deletes[item_id] if operation == "delete" else
               runtime.pending_queries[(ticket.run_id, ticket.attempt_id)])
    # 正式结束仓储可重现历史版本已经保存的缺陷状态；本修复不迁移它。
    finish = AttemptFinish(ticket, validate_outcome(ticket, pending.actual.outcome, pending.evidence),
        _NOW, retry_wait=False, recovery_max_event_id=horizon)
    saved = (runtime.outputs.finish_cleanup_attempt(finish, new_operation_key(), world.owned)
        if operation == "delete" else runtime.operations.finish_attempt(finish, new_operation_key(), world.owned))
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    row = world.owned.connection.execute("SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone()
    fresh = world.context.open_connection()
    try:
        factory = _factory(world)
        with pytest.raises(ConsistencyError, match="后续尝试要求先建立重试等待"):
            for _ in range(6):
                await advance_cleanup(factory(fresh))
        assert fresh.connection.execute("SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone() == row
        assert world.channel.operations == (["query"] if operation == "delete" else [])
        assert world.paths[701].exists()
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("output_id", [703, 704, 705])
@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_pending_only_host_actual_reads_original_null_config_without_current_ports(world, monkeypatch, output_id, operation):
    from camctl.bootstrap import cleanup_assembly
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.cleanup_flow import (
        PendingCleanupDelete, PendingCleanupQuery, _local_artifact_request, _save_delete_return, _run_query,
    )
    from camctl.outputs.cleanup_results import cleanup_attempt_config
    from ..outputs.test_cleanup_query_save_recovery import _start_cleanup
    action_id, item_id = _start_cleanup(world.owned, output_id, "2")
    runtime = _factory(world)(world.owned)
    consumer = None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE
    begun = _begin_cleanup_intent(runtime, AttemptIntent(operation=operation, action_id=action_id,
        kind=OperationKind.DELETE_FILE if operation == "delete" else OperationKind.CHECK_FILE_EXISTS,
        target=AttemptTarget(cleanup_item_id=item_id), query_purpose=None,
        config=runtime.delete_config if operation == "delete" else runtime.query_config, occurred_at=_NOW), consumer)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    ticket = begun.value.ticket
    request = _local_artifact_request(runtime, item_id, ticket, operation)
    actual = await getattr(runtime.local_files, "delete" if operation == "delete" else "query_file")(request)
    runtime.pending_intents.clear()
    key = new_operation_key()
    if operation == "delete":
        pending = PendingCleanupDelete(world.owned.metadata.instance_id, item_id, ticket,
            actual, runtime.local_files.evidence, _NOW, 0, key)
        runtime.pending_deletes[item_id] = pending
    else:
        pending = PendingCleanupQuery(world.owned.metadata.instance_id, item_id, consumer, ticket,
            actual, runtime.local_files.evidence, _NOW, 0, key)
        runtime.pending_queries[(ticket.run_id, ticket.attempt_id)] = pending
    def forbidden_ports(*args):
        raise AssertionError("主机原返回保存不能解析当前设备端口")
    monkeypatch.setattr(cleanup_assembly, "port_for", forbidden_ports)
    if operation == "delete":
        assert _save_delete_return(runtime, pending) is None
    else:
        assert await _run_query(runtime, ticket, item_id, consumer) is True
    assert pending.actual is actual and pending.key == key
    original = cleanup_attempt_config(world.owned, ticket, database_id=world.owned.metadata.instance_id, item_id=item_id)
    assert original.timeout_s is None and original.retry_interval_s is None
    assert world.owned.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (0,)
    assert world.channel.operations == []
