"""原尝试配置、原结果保存及后轮真实授票之间的合同。"""

from support.repository_ports import repository_port

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.recovery import RecoveryBoundary, recovery_registry
from camctl.outputs.cleanup_flow import advance_cleanup, delete_source_file
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
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, Settlement, SettlementBasis, EvidenceValue, ErrorValue
    from camctl.operations.validation import validate_outcome
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.outputs import OutputsRepository
    item_id, ticket, horizon = _lost_intent(world, "delete")
    if operation == "query":
        evidence = recovery_registry(world.entry, "delete")
        actual = CallOutcome(status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN, error=ErrorValue("result_not_saved", "recovery"),
            settlement=Settlement(SettlementBasis.ASSUMED, EvidenceValue("adb_foreground_recovery", 1, {})))
        finish = AttemptFinish(ticket, validate_outcome(ticket, actual, evidence), _NOW,
            retry_wait=True, recovery_max_event_id=horizon)
        saved = OutputsRepository().finish_cleanup_attempt(finish, new_operation_key(), world.owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        item_id, ticket, horizon = _lost_intent(world, "query", item_id=item_id)
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
    await world.resources.restart_database(fresh)
    try:
        factory = _factory(world, max_delete_attempts=5, max_query_attempts=4,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=horizon)
        await advance_cleanup(await factory(fresh))
        assert world.channel.operations == []
        recovered_wait = fresh.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone()
        if operation == "query":
            world.channel.behaviors = ["unreceived"]
        for _ in range(6):
            await advance_cleanup(await factory(fresh))
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


@pytest.mark.parametrize("role", ["host_original", "host_preview", "repaired"])
@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_host_original_preview_repaired_recovery_preserves_null_config_and_new_task(world, monkeypatch, role, operation):
    output_id = world.ids[role]
    from camctl.bootstrap import cleanup_assembly
    from .test_output_binding_changes import _NOW
    from ..outputs.test_cleanup_query_save_recovery import _start_cleanup
    action_id, item_id = _start_cleanup(world.owned, output_id, "2")
    item_id, ticket, horizon = _lost_intent(world, operation, output_id=output_id, item_id=item_id)
    fresh = world.context.open_connection()
    await world.resources.restart_database(fresh)
    try:
        def forbidden_ports(*args):
            raise AssertionError("主机历史不能解析当前设备端口")
        monkeypatch.setattr(cleanup_assembly, "port_for", forbidden_ports)
        factory = _factory(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon)
        await advance_cleanup(await factory(fresh))
        assert fresh.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (0,)
        for _ in range(5):
            await advance_cleanup(await factory(fresh))
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
    _configure(world, interval=Decimal("0"))
    item_id, ticket, horizon = await _lost_operation(world, operation)
    from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, Settlement, SettlementBasis, EvidenceValue, ErrorValue
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.repositories.operations import OperationRepository
    actual = CallOutcome(status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN, error=ErrorValue("result_not_saved", "recovery"),
        settlement=Settlement(SettlementBasis.ASSUMED, EvidenceValue("adb_foreground_recovery", 1, {})))
    finish = AttemptFinish(ticket, validate_outcome(ticket, actual, recovery_registry(world.entry, operation)),
        _NOW, retry_wait=False, recovery_max_event_id=horizon)
    saved = (OutputsRepository().finish_cleanup_attempt(finish, new_operation_key(), world.owned)
        if operation == "delete" else OperationRepository().finish_attempt(finish, new_operation_key(), world.owned))
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    row = world.owned.connection.execute("SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone()
    fresh = world.context.open_connection()
    await world.resources.restart_database(fresh)
    try:
        factory = _factory(world)
        with pytest.raises(ConsistencyError, match="后续尝试要求先建立重试等待"):
            for _ in range(6):
                await advance_cleanup(await factory(fresh))
        assert fresh.connection.execute("SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone() == row
        assert world.channel.operations == (["query"] if operation == "delete" else [])
        assert world.paths[world.ids["original"]].exists()
    finally:
        fresh.connection.close()


async def _actual_save_failure(world, monkeypatch, operation, *, output_id=None, commit_after=False):
    if output_id is None:
        output_id = world.ids["original"]
    from pathlib import Path
    import sqlite3
    from ..support.outputs import SqliteOperationFault
    from camctl.bootstrap.cleanup_assembly import HostArtifacts
    from camctl.persistence.repositories.outputs import OutputsRepository
    from ..outputs.test_cleanup_query_save_recovery import _start_cleanup
    _, item_id = _start_cleanup(world.owned, output_id, "2")
    factory = _factory(world)
    runtime = await factory(world.owned)
    local = output_id in {world.ids[role] for role in ("host_original", "host_preview", "repaired")}
    if operation == "query":
        class QueryGate(OutputsRepository):
            calls = []
            def begin_cleanup_attempt(self, request, key, worker):
                if request.operation == "query":
                    self.calls.append((request, key))
                    faulty = SqliteOperationFault(worker.connection, sqlite3.SQLITE_INSERT, 'operation_attempts')
                    saved = super().begin_cleanup_attempt(request, key, replace(worker, connection=faulty))
                    assert faulty.triggered and saved.kind is DbOutcomeKind.ROLLED_BACK
                    assert isinstance(saved.error, sqlite3.DatabaseError)
                    return saved
                return super().begin_cleanup_attempt(request, key, worker)
        gate = QueryGate()
        if local:
            unlink = Path.unlink
            def blocked(path, *args, **kwargs):
                if path == world.paths[output_id]:
                    raise PermissionError("删除结果仍须查询核实")
                return unlink(path, *args, **kwargs)
            with monkeypatch.context() as patch, repository_port(
                    OutputsRepository, gate, ("begin_cleanup_attempt",),
                    matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
                patch.setattr(Path, "unlink", blocked)
                await delete_source_file(runtime, item_id)
        else:
            world.channel.behaviors = ["unreceived"]
            with repository_port(OutputsRepository, gate, ("begin_cleanup_attempt",),
                                 matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
                await delete_source_file(runtime, item_id)
        assert len(gate.calls) == 1
    calls = []
    if local:
        method = getattr(HostArtifacts, "delete" if operation == "delete" else "query_file")
        async def local_call(port, request):
            actual = await method(port, request)
            calls.append((request, actual))
            return actual
        monkeypatch.setattr(HostArtifacts, "delete" if operation == "delete" else "query_file", local_call)
    else:
        method = getattr(world.entry.driver, "delete" if operation == "delete" else "query_file")
        async def device_call(request):
            actual = await method(request)
            calls.append((request, actual))
            return actual
        monkeypatch.setattr(world.entry.driver, "delete" if operation == "delete" else "query_file", device_call)
        world.channel.behaviors = ["unreceived"]
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.contracts.values import ConsistencyError
    owner, name = (OutputsRepository, "finish_cleanup_attempt") if operation == "delete" else (OperationRepository, "finish_attempt")
    save = getattr(owner, name)
    saves, rejected = [], [True]
    failures = []
    def retained(port, request, key, worker):
        if calls and request.ticket == calls[0][0].ticket:
            saves.append((request, key))
            if rejected:
                before = tuple(worker.connection.iterdump())
                if commit_after:
                    from .test_binding_transactions import _CommitFailure
                    faulty = _CommitFailure(worker.connection, True)
                else:
                    faulty = SqliteOperationFault(worker.connection, sqlite3.SQLITE_UPDATE, 'operation_attempts')
                saved = save(port, request, key, replace(worker, connection=faulty))
                if commit_after:
                    assert saved.kind is DbOutcomeKind.UNKNOWN
                else:
                    assert faulty.triggered and saved.kind is DbOutcomeKind.ROLLED_BACK
                    assert tuple(worker.connection.iterdump()) == before
                assert isinstance(saved.error, sqlite3.DatabaseError)
                failures.append(saved.error)
                return saved
        return save(port, request, key, worker)
    monkeypatch.setattr(owner, name, retained)
    if operation == "query":
        with pytest.raises(ConsistencyError) as caught:
            await delete_source_file(runtime, item_id)
        assert failures and caught.value.__cause__ is failures[0]
    else:
        step = await delete_source_file(runtime, item_id)
        assert step.phase == "finish_rejected"
        assert failures and step.exception.__cause__ is failures[0]
    assert len(calls) == len(saves) == len(failures) == 1
    request, actual = calls[0]
    status, result = world.owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (request.ticket.run_id, request.ticket.attempt_id)).fetchone()
    assert (status != 1 and result is not None) if commit_after else (status, result) == (1, None)
    return item_id, request, actual, calls, saves, rejected


async def _retry_original_actual(world, item_id, request, actual, calls, saves, rejected):
    from camctl.outputs.cleanup_results import saved_cleanup_attempt
    before = list(world.channel.operations)
    rejected.clear()
    runtime = await _factory(world)(world.owned)
    await advance_cleanup(runtime)
    assert len(calls) == 1 and len(saves) == 2 and saves[0] == saves[1]
    assert world.channel.operations == before
    saved = saved_cleanup_attempt(world.owned, request.ticket,
        database_id=world.owned.metadata.instance_id, item_id=item_id)
    assert saved.key == saves[0][1] and saved.occurred_at == saves[0][0].occurred_at
    assert saved.actual.outcome == (actual.outcome or saves[0][0].outcome.outcome)
    return saved


async def test_actual_device_return_retries_original_save_after_current_port_removal(world, monkeypatch):
    _configure(world, interval=Decimal("0.625"))
    case = await _actual_save_failure(world, monkeypatch, "delete")
    assert case[1].timeout_s == Decimal("7.125")
    world.cfg = replace(world.cfg, devices={})
    await _retry_original_actual(world, *case)
    assert world.channel.operations == ["delete"]
    assert world.owned.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE id=?", (case[1].ticket.run_id,)).fetchone() == (1,)


async def test_actual_host_query_return_keeps_original_null_config_after_save_failure(world, monkeypatch):
    from camctl.bootstrap import cleanup_assembly
    case = await _actual_save_failure(world, monkeypatch, "query", output_id=world.ids["host_original"])
    def forbidden_ports(*args):
        raise AssertionError("主机原返回保存不能解析当前设备端口")
    monkeypatch.setattr(cleanup_assembly, "port_for", forbidden_ports)
    await _retry_original_actual(world, *case)
    assert world.owned.connection.execute("SELECT timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=?", (case[1].ticket.run_id,case[1].ticket.attempt_id)).fetchone() == (None, None)
    assert world.channel.operations == []
    assert all(path.exists() for path in world.paths.values())


async def test_actual_device_query_unknown_commit_retries_original_save(world, monkeypatch):
    _configure(world, interval=Decimal("0.625"))
    case = await _actual_save_failure(world, monkeypatch, "query", commit_after=True)
    assert case[1].timeout_s == Decimal("8.125")
    world.cfg = replace(world.cfg, devices={})
    await _retry_original_actual(world, *case)
    assert world.channel.operations == ["delete", "query"]
    assert world.paths[world.ids["original"]].exists()


async def test_actual_host_delete_unknown_commit_retries_original_save(world, monkeypatch):
    from camctl.bootstrap import cleanup_assembly
    case = await _actual_save_failure(world, monkeypatch, "delete", output_id=world.ids["host_original"], commit_after=True)
    def forbidden_ports(*args):
        raise AssertionError("主机原返回保存不能解析当前设备端口")
    monkeypatch.setattr(cleanup_assembly, "port_for", forbidden_ports)
    await _retry_original_actual(world, *case)
    assert world.owned.connection.execute(
        "SELECT timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (case[1].ticket.run_id, case[1].ticket.attempt_id)).fetchone() == (None, None)
    assert world.channel.operations == [] and not world.paths[world.ids["host_original"]].exists()
    assert all(path.exists() for identity, path in world.paths.items() if identity != world.ids["host_original"])
