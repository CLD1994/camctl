"""默认轮次区分原结果交接、可靠未派发票据和普通调用资格。"""

from decimal import Decimal
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from support.repository_ports import repository_port
from camctl.session.service import LocalWorkPort

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceConfigurationError
from camctl.devices.drivers.registry import DriverRegistry
from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
from camctl.outputs.cleanup_flow import (
    advance_cleanup,
    delete_source_file,
    CleanupQueryConsumer,
)
from camctl.persistence.models import DbOutcomeKind

from .test_cleanup_delete_save_recovery import _prepared, _configured
from ..support.outputs import cleanup_factory
from ..support.capture import capture_database
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.operations import OperationRepository
from .test_cleanup_residual_recovery import _orphan_intent, _recovering_registry
from ..bootstrap.test_output_binding_changes import _NOW, _registry, environment

pytestmark = pytest.mark.asyncio


async def _runtime(factory, owned, resources):
    return await factory(await resources.database(owned))


async def _intent(environment, owned, runtime, driver, item_id, operation, consumer=None, *, observed=None):
    from dataclasses import dataclass
    class Granted(Exception):
        pass
    @dataclass(frozen=True)
    class OriginalIntent:
        request: object
        key: object
        ticket: object
    class GrantPort(OutputsRepository):
        original = None
        enabled = True
        def begin_cleanup_attempt(self, request, key, worker):
            result = super().begin_cleanup_attempt(request, key, worker)
            if self.enabled and request.operation == operation and result.value.ticket is not None:
                self.original = OriginalIntent(request, key, result.value.ticket)
                return replace(result, handoff_error=Granted())
            return result
    devices = {name: {**declaration, "cleanup": {
        "delete_timeout_s": Decimal(17), "query_timeout_s": Decimal(17),
        "delete_retry_interval_s": Decimal(0), "query_retry_interval_s": Decimal(0)}}
        for name, declaration in environment[0].devices.items()}
    prepared = await _configured(environment, owned, driver, database=await capture_database(owned),
        devices=devices, delete_attempts=5, query_attempts=5)
    port = GrantPort()
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
        with pytest.raises(Granted):
            await delete_source_file(prepared, item_id)
    assert port.original is not None and port.original.request.operation == operation
    if observed is not None:
        observed.append(port.original)
    return port.original.ticket


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_default_round_dispatches_reliable_unsent_original_ticket_once(
    environment, operation,
    capture_resources,
):
    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=operation == "query")
    driver.matrix_enabled, driver.present = True, False
    consumer = None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE
    observed = []
    ticket = await _intent(environment, owned, runtime, driver, item_id, operation, consumer, observed=observed)
    original, = observed
    original_config = owned.connection.execute(
        "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json,intent_event_id"
        " FROM operation_attempts WHERE run_id=?",
        (ticket.run_id,),
    ).fetchone()
    devices = {
        name: {
            **declaration,
            "cleanup": {
                "delete_timeout_s": Decimal(71),
                "delete_retry_interval_s": Decimal(73),
                "query_timeout_s": Decimal(79),
                "query_retry_interval_s": Decimal(83),
            },
        }
        for name, declaration in cfg.devices.items()
    }
    factory = cleanup_factory(
        devices=devices,
        drivers=_registry(driver),
        max_delete_attempts=1,
        max_query_attempts=1,
        staging=Path(cfg.paths.staging),
        occurred_at=lambda: _NOW)
    await advance_cleanup((await _runtime(factory, owned, capture_resources)))
    calls = driver.delete_requests if operation == "delete" else driver.query_requests
    assert len(calls) == 1
    assert (
        calls[0].ticket == ticket
        and calls[0].timeout_s == original.request.config.timeout_s
    )
    assert owned.connection.execute(
        "SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)
    ).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (ticket.run_id,)
    ).fetchone() == (1,)
    history = tuple(owned.connection.execute("SELECT * FROM history_events"))
    await advance_cleanup((await _runtime(factory, owned, capture_resources)))
    assert len(calls) == 1
    assert tuple(owned.connection.execute("SELECT * FROM history_events")) == history
    assert (
        owned.connection.execute(
            "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json,intent_event_id"
            " FROM operation_attempts WHERE run_id=?",
            (ticket.run_id,),
        ).fetchone()
        == original_config
    )


@pytest.mark.parametrize(
    "consumer,present", [("after", True), ("after", None), ("before", True)]
)
async def test_default_round_consumes_durable_query_before_current_ports_and_allows_next_round(environment, consumer, present, capture_resources):
    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=consumer == "before")
    driver.matrix_enabled, driver.present = True, present
    driver.unknown_delete = True
    result = await delete_source_file(runtime, item_id)
    assert result.phase in ("still_present", "query_unknown")
    attempts = tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
    old_calls = len(driver.deletes), len(driver.queries)
    factory = session_cleanup_assembly(devices=cfg.devices, drivers=DriverRegistry(),
        max_delete_attempts=3, max_query_attempts=3, occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    database = await capture_resources.restart_database(owned)
    await advance_cleanup(await factory(database))
    assert tuple(owned.connection.execute("SELECT * FROM operation_attempts")) == attempts
    assert (len(driver.deletes), len(driver.queries)) == old_calls
    with pytest.raises(DeviceConfigurationError):
        await advance_cleanup(await factory(database))
    assert tuple(owned.connection.execute("SELECT * FROM operation_attempts")) == attempts
    assert (len(driver.deletes), len(driver.queries)) == old_calls


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_qualified_old_unknown_recovery_uses_whole_round_then_next_round_can_call(
    environment, operation,
    capture_resources,
):
    from camctl.operations.recovery import RecoveryBoundary

    cfg = environment[0]
    owned, _, driver, item_id, ticket, horizon = await _orphan_intent(
        environment, operation
    )
    try:
        await capture_resources.restart_database(owned)
        driver.matrix_enabled, driver.present = True, None
        registry = _recovering_registry(driver, operation)
        factory = session_cleanup_assembly(
            devices=cfg.devices,
            drivers=registry,
            max_delete_attempts=2,
            max_query_attempts=2,
            occurred_at=lambda: _NOW,
            monotonic_ns=lambda: 0,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon,
        )
        await advance_cleanup((await _runtime(factory, owned, capture_resources)))
        assert not driver.deletes and not driver.queries
        original = owned.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone()
        assert owned.connection.execute(
            "SELECT status FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone() == (4,)
        await advance_cleanup((await _runtime(factory, owned, capture_resources)))
        assert len(driver.deletes) + len(driver.queries) == 1
        assert (
            owned.connection.execute(
                "SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id),
            ).fetchone()
            == original
        )
    finally:
        owned.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize("fault", ["rollback", "unknown_before", "first_exception"])
@pytest.mark.parametrize("change", ["cancel", "binding", "capability_false"])
async def test_absent_original_intent_is_not_granted_locally_before_current_eligibility(environment, operation, fault, change, capture_resources):
    from .test_cleanup_delete_save_recovery import _FaultOutputs
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment, before_query=operation == "query")
    action_id, = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    class FirstException(_FaultOutputs):
        def begin_cleanup_attempt(self, request, key, worker):
            self.calls.append((request, key))
            raise RuntimeError("原意图首次保存没有回执")
    port = (FirstException("begin_cleanup_attempt", DbOutcomeKind.UNKNOWN) if fault == "first_exception" else
        _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN))
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
        try:
            result = await delete_source_file(runtime, item_id)
            assert result.phase in ("delete_budget_rejected", "query_rejected")
        except ConsistencyError:
            assert fault == "first_exception"
        original = port.calls[0]
        assert original[0].operation == operation
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            if change == "cancel":
                _apply_cleanup_cancel(fresh, action_id)
            entry = _registry(driver).entry("camctl-adb")
            registry = _registry(driver) if change != "capability_false" else DriverRegistry((
                replace(entry, declaration=replace(entry.declaration, delete_supported=False, file_query_supported=False)),))
            factory = cleanup_factory(devices={} if change == "binding" else environment[0].devices,
                drivers=registry, max_delete_attempts=1, max_query_attempts=1, occurred_at=lambda: _NOW)
            await advance_cleanup(await factory(await capture_resources.restart_database(fresh)))
            assert fresh.connection.execute("SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone() == (0,)
            assert not driver.deletes and not driver.queries
            assert port.calls == [original]
            assert fresh.connection.execute("SELECT id FROM history_transactions WHERE operation_key=?", (str(original[1]),)).fetchone() is None
        finally:
            fresh.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize(
    "delete_supported,query_supported", [(True, True), (False, True), (False, False)]
)
async def test_unsent_ticket_uses_current_operation_capability_and_preserves_original_config(
    environment, operation, delete_supported, query_supported,
    capture_resources,
):
    from camctl.contracts.workflow_errors import item_error_id

    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=operation == "query")
    driver.matrix_enabled, driver.present = True, False
    observed = []
    ticket = await _intent(
        environment, owned, runtime, driver,
        item_id,
        operation,
        None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE,
        observed=observed,
    )
    original, = observed
    entry = _registry(driver).entry("camctl-adb")
    registry = DriverRegistry(
        (
            replace(
                entry,
                declaration=replace(
                    entry.declaration,
                    delete_supported=delete_supported,
                    file_query_supported=query_supported,
                ),
            ),
        )
    )
    factory = cleanup_factory(
        devices=cfg.devices,
        drivers=registry,
        max_delete_attempts=1,
        max_query_attempts=1,
        occurred_at=lambda: _NOW)
    await advance_cleanup((await _runtime(factory, owned, capture_resources)))
    supported = delete_supported if operation == "delete" else query_supported
    requests = (
        driver.delete_requests if operation == "delete" else driver.query_requests
    )
    assert len(requests) == int(supported)
    row = owned.connection.execute(
        "SELECT status,effect_state,result_json,error_json FROM operation_attempts WHERE run_id=?",
        (ticket.run_id,),
    ).fetchone()
    member = owned.connection.execute(
        "SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()
    assert (
        member == (4, None)
        if supported
        else member == (5, item_error_id("cleanup_items", "file_delete_failed"))
    )
    import json

    if not supported:
        assert row[:2] == (3, 2)
        assert json.loads(row[2])["settlement"] == {
            "basis": "not_dispatched",
            "evidence": {"type": "dispatch_prevented", "version": 1, "data": {}},
        }
        assert json.loads(row[3]) == {
            "code": "file_delete_failed",
            "stage": operation,
            "details": {
                "output_id": str(
                    owned.connection.execute(
                        "SELECT output_id FROM cleanup_items WHERE id=?", (item_id,)
                    ).fetchone()[0]
                )
            },
        }
    else:
        assert (
            requests[0].ticket == ticket
            and requests[0].timeout_s == original.request.config.timeout_s
        )
    assert owned.connection.execute(
        "SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)
    ).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (ticket.run_id,)
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    "present,query_supported",
    [(False, True), (True, True), (None, True), (None, False)],
)
async def test_delete_disabled_keeps_only_needed_finite_query_and_original_delete(
    environment, present, query_supported,
    capture_resources,
):
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.models import DbOutcome
    from camctl.contracts.workflow_errors import item_error_id

    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment)
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, present

    class QueryNotGranted(OutputsRepository):
        def begin_cleanup_attempt(self, request, key, owned):
            if request.operation == "query":
                return DbOutcome(
                    DbOutcomeKind.ROLLED_BACK, error=RuntimeError("原查询尚未授票")
                )
            return super().begin_cleanup_attempt(request, key, owned)

    port = QueryNotGranted()
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
        assert (await delete_source_file(runtime, item_id)).phase == "query_rejected"
    assert len(driver.deletes) == 1 and not driver.queries
    entry = _registry(driver).entry("camctl-adb")
    registry = DriverRegistry(
        (
            replace(
                entry,
                declaration=replace(
                    entry.declaration,
                    delete_supported=False,
                    file_query_supported=query_supported,
                ),
            ),
        )
    )
    clock = [0]
    factory = cleanup_factory(
        devices=cfg.devices,
        drivers=registry,
        max_delete_attempts=1,
        max_query_attempts=2,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: clock[0])
    for _ in range(4):
        clock[0] += 10_000_000_000
        await advance_cleanup((await _runtime(factory, owned, capture_resources)))
        member = owned.connection.execute(
            "SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)
        ).fetchone()
        if member[0] in (4, 5, 6):
            break
    expected = (
        None
        if present is False
        else item_error_id(
            "cleanup_items",
            "file_query_attempts_exhausted"
            if present is None and query_supported
            else "file_delete_failed",
        )
    )
    assert member == (4, None) if present is False else member == (5, expected)
    assert len(driver.deletes) == 1
    assert len(driver.queries) == (
        2 if present is None and query_supported else int(query_supported)
    )
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
        (item_id,),
    ).fetchone() == (1,)
    if not query_supported:
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=5",
            (item_id,),
        ).fetchone() == (0,)


async def test_cancel_extra_query_uses_cancel_slot_without_ordinary_retry_delay(
    environment,
    capture_resources,
):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel

    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment)
    initial_devices = {name: {**declaration, "cleanup": {"query_retry_interval_s": Decimal(60)}}
                       for name, declaration in cfg.devices.items()}
    runtime = await _configured(environment, owned, driver, devices=initial_devices)
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, None
    assert (await delete_source_file(runtime, item_id)).phase == "query_unknown"
    action_id = owned.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    _apply_cleanup_cancel(owned, action_id)
    driver.present = False
    devices = {
        name: {
            **declaration,
            "cleanup": {
                **declaration.get("cleanup", {}),
                "query_retry_interval_s": Decimal(60),
                "query_timeout_s": Decimal(19),
            },
        }
        for name, declaration in cfg.devices.items()
    }
    factory = cleanup_factory(
        devices=devices,
        drivers=_registry(driver),
        max_delete_attempts=3,
        max_query_attempts=4,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0)
    await advance_cleanup((await _runtime(factory, owned, capture_resources)))
    assert len(driver.deletes) == 1 and len(driver.queries) == 2
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone() == (4,)

    from camctl.contracts.json_values import parse_exact_json

    query_config = owned.connection.execute(
        "SELECT a.max_attempts_used,a.timeout_s_json,a.retry_interval_s_json"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind=5 ORDER BY a.id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    assert (
        query_config[0],
        parse_exact_json(query_config[1]),
        parse_exact_json(query_config[2]),
    ) == (4, Decimal(19), Decimal(60))


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize("change", ["cancel", "binding"])
async def test_unsent_original_ticket_is_settled_without_side_effect_after_eligibility_change(
    environment, operation, change,
    capture_resources,
):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    from camctl.contracts.enums import enum_for
    import json

    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=operation == "query")
    observed = []
    ticket = await _intent(
        environment, owned, runtime, driver,
        item_id,
        operation,
        None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE,
        observed=observed,
    )
    original, = observed
    if change == "cancel":
        _apply_cleanup_cancel(owned, original.request.action_id)
    factory = cleanup_factory(
        devices={} if change == "binding" else cfg.devices,
        drivers=_registry(driver),
        max_delete_attempts=1,
        max_query_attempts=1,
        occurred_at=lambda: _NOW)
    await advance_cleanup((await _runtime(factory, owned, capture_resources)))
    assert not driver.deletes and not driver.queries
    row = owned.connection.execute(
        "SELECT status,effect_state,result_json FROM operation_attempts WHERE run_id=?",
        (ticket.run_id,),
    ).fetchone()
    assert row[:2] == (
        int(enum_for("operation_attempts.status").FAILED),
        int(enum_for("operation_attempts.effect_state").NO_EFFECT),
    )
    assert json.loads(row[2])["settlement"]["basis"] == "not_dispatched"
    member = owned.connection.execute(
        "SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()
    assert member[0] == (6 if change == "cancel" else 5)
    if change == "cancel":
        assert member[1] is None
    assert owned.connection.execute(
        "SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)
    ).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (ticket.run_id,)
    ).fetchone() == (1,)


@pytest.mark.parametrize("entry", ["delete", "before_query"])
async def test_fresh_default_call_keeps_its_original_query_delete_chain(
    environment, entry,
    capture_resources,
):
    from .test_cleanup_query_local_consumption import _world

    cfg = environment[0]
    if entry == "before_query":
        runtime, driver, _, item_id, _ = await _world(environment, "before", True)
        owned = environment[1]
    else:
        owned, _, runtime, driver, item_id = await _prepared(environment)
        driver.unknown_delete = True
        driver.matrix_enabled, driver.present = True, False
    order = []
    original_delete, original_query = driver.delete, driver.query_file

    async def delete(request):
        order.append("delete")
        return await original_delete(request)

    async def query(request):
        order.append("query")
        return await original_query(request)

    driver.delete, driver.query_file = delete, query
    devices = {
        name: {
            **declaration,
            "cleanup": {
                "delete_timeout_s": Decimal(17),
                "delete_retry_interval_s": Decimal(13),
                "query_timeout_s": Decimal(19),
                "query_retry_interval_s": Decimal(7),
            },
        }
        for name, declaration in cfg.devices.items()
    }
    factory = cleanup_factory(
        devices=devices,
        drivers=_registry(driver),
        max_delete_attempts=5,
        max_query_attempts=4,
        staging=Path(cfg.paths.staging),
        occurred_at=lambda: _NOW)
    await advance_cleanup((await _runtime(factory, owned, capture_resources)))
    assert order == (
        ["delete", "query"] if entry == "delete" else ["query", "delete", "query"]
    )
    if entry == "delete":
        assert owned.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (item_id,)
        ).fetchone() == (4,)
    from camctl.contracts.json_values import parse_exact_json

    rows = owned.connection.execute(
        "SELECT r.kind,a.max_attempts_used,a.timeout_s_json,a.retry_interval_s_json"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? ORDER BY a.id",
        (item_id,),
    ).fetchall()
    assert rows
    for kind, maximum, timeout, interval in rows:
        assert (maximum, parse_exact_json(timeout), parse_exact_json(interval)) == (
            (5, Decimal(17), Decimal(13)) if kind == 4 else (4, Decimal(19), Decimal(7))
        )


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_default_round_waits_for_actual_original_owner_after_factory_rebuild(
    environment, operation,
    capture_resources,
):
    import asyncio

    owned, context, runtime, driver, item_id = await _prepared(environment, before_query=operation == "query")
    driver.matrix_enabled, driver.present = True, False
    if operation == "query":
        await _intent(environment, owned, runtime, driver, item_id, operation, CleanupQueryConsumer.BEFORE_DELETE)
    entered, release = asyncio.Event(), asyncio.Event()
    method = "delete" if operation == "delete" else "query_file"
    original = getattr(driver, method)
    calls = []

    async def gated(request):
        calls.append(request.ticket)
        entered.set()
        await release.wait()
        return await original(request)

    setattr(driver, method, gated)
    owner = asyncio.create_task(delete_source_file(runtime, item_id))
    fresh = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        fresh = context.open_connection()
        rebuilt = (await _runtime(cleanup_factory(
            devices={},
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW), fresh, capture_resources))
        before = tuple(fresh.connection.iterdump())
        with pytest.raises(ConsistencyError):
            await delete_source_file(rebuilt, item_id)
        assert tuple(fresh.connection.iterdump()) == before
        await advance_cleanup(rebuilt)
        assert tuple(fresh.connection.iterdump()) == before
        from camctl.bootstrap.cleanup_assembly import cleanup_flow
        owner_view: LocalWorkPort = cleanup_flow(cleanup_factory(devices={}, drivers=DriverRegistry(),
            max_delete_attempts=1, max_query_attempts=1))
        assert len(calls) == 1 and owner_view.required_settlements() > 0
        release.set()
        result = await asyncio.wait_for(owner, timeout=5)
        assert result.phase == "succeeded"
        assert owner_view.required_settlements() == 0 and len(calls) == 1
    finally:
        release.set()
        if not owner.done():
            await owner
        if fresh is not None:
            fresh.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_default_round_rejects_isolated_dispatched_marker_without_owner(environment, operation, capture_resources):
    # 原调用结果缺失且没有可接手的原 owner；未确认的主机边界不能授权重新派发。
    from camctl.operations.recovery import RecoveryBoundary
    owned, _, driver, item_id, ticket, _ = await _orphan_intent(environment, operation)
    try:
        factory = session_cleanup_assembly(devices={}, drivers=DriverRegistry(),
            max_delete_attempts=3, max_query_attempts=3, occurred_at=lambda: _NOW,
            recovery_boundary=RecoveryBoundary.UNCONFIRMED)
        rebuilt = await factory(await capture_resources.restart_database(owned))
        before = tuple(owned.connection.iterdump())
        with pytest.raises(ConsistencyError):
            await advance_cleanup(rebuilt)
        assert tuple(owned.connection.iterdump()) == before
        assert not driver.deletes and not driver.queries
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)
        assert owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
    finally:
        owned.connection.close()


async def _durable_delete_only(environment, present, resources):
    from camctl.contracts.values import new_operation_key
    from camctl.devices.evidence import EvidenceContract, DeviceObservation
    from camctl.operations.attempts import AttemptConfig, AttemptFinish
    from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
    from camctl.operations.validation import validate_outcome
    from .test_cleanup_query_save_recovery import _MatrixDriver
    owned, context, runtime, driver, item_id = await _prepared(environment)
    action_id, = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    intent = AttemptIntent("delete", action_id, OperationKind.DELETE_FILE,
        AttemptTarget(cleanup_item_id=item_id), None, AttemptConfig(5, Decimal(17), Decimal(0)), _NOW)
    begun = OutputsRepository().begin_cleanup_attempt(intent, new_operation_key(), owned)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    ticket = begun.value.ticket
    assert ticket is not None
    evidence = _registry(driver).entry("camctl-adb").evidence.with_contract(EvidenceContract(
        "file_delete_presence", 1, "delete", frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"))
    observations = () if present is None else (DeviceObservation("file_delete_presence", 1,
        {"cleanup_item_id": str(item_id), "present": present}),)
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED,
        effect=EffectState.UNKNOWN if present is None else EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})), observations=observations)
    key = new_operation_key()
    saved = OutputsRepository().finish_cleanup_attempt(
        AttemptFinish(ticket, validate_outcome(ticket, outcome, evidence), _NOW, retry_wait=True), key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    before = tuple(owned.connection.iterdump())
    owned.connection.close()
    fresh = context.open_connection()
    await resources.restart_database(fresh)
    driver = _MatrixDriver(fresh)
    driver.matrix_enabled, driver.present = True, None
    driver.unknown_delete = False
    return fresh, driver, item_id, ticket, key, before


@pytest.mark.parametrize("present", [None, True], ids=["unknown", "present"])
@pytest.mark.parametrize("missing", [False, True], ids=["registered", "removed"])
async def test_durable_delete_is_consumed_locally_before_any_current_port(environment, monkeypatch, present, missing, capture_resources):
    fresh, driver, item_id, ticket, key, before = await _durable_delete_only(environment, present, capture_resources)
    factory = session_cleanup_assembly(devices=environment[0].devices,
        drivers=DriverRegistry() if missing else _registry(driver), max_delete_attempts=3,
        max_query_attempts=2, occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    try:
        runtime = await _runtime(factory, fresh, capture_resources)
        await advance_cleanup(runtime)
        assert not driver.deletes and not driver.queries
        assert tuple(fresh.connection.iterdump()) == before
        assert fresh.connection.execute("SELECT operation_key FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (str(key),)
        assert fresh.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)
        if missing:
            with pytest.raises(DeviceConfigurationError):
                await advance_cleanup(await _runtime(factory, fresh, capture_resources))
            assert tuple(fresh.connection.iterdump()) == before
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("present", [None, True], ids=["unknown", "present"])
@pytest.mark.parametrize(
    "delete_supported,query_supported", [(True, True), (False, True), (False, False)]
)
async def test_durable_delete_consumption_survives_factory_rebuild_and_finite_budget(
    environment, monkeypatch, present, delete_supported, query_supported,
    capture_resources,
):
    from camctl.bootstrap import cleanup_assembly
    from camctl.contracts.workflow_errors import item_error_id

    fresh, driver, item_id, ticket, key, before = await _durable_delete_only(
        environment, present
    , capture_resources)
    entry = _registry(driver).entry("camctl-adb")
    registry = DriverRegistry(
        (
            replace(
                entry,
                declaration=replace(
                    entry.declaration,
                    delete_supported=delete_supported,
                    file_query_supported=query_supported,
                ),
            ),
        )
    )
    devices = {
        name: {
            **declaration,
            "cleanup": {
                "delete_retry_interval_s": Decimal(0),
                "query_retry_interval_s": Decimal(0),
            },
        }
        for name, declaration in environment[0].devices.items()
    }
    kwargs = dict(
        devices=devices,
        drivers=registry,
        max_delete_attempts=3,
        max_query_attempts=2,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    try:
        runtime = (await _runtime(cleanup_factory(**kwargs), fresh, capture_resources))
        await advance_cleanup(runtime)
        assert not driver.deletes and not driver.queries
        assert tuple(fresh.connection.iterdump()) == before
        original_attempt = fresh.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id=?", (ticket.run_id,)
        ).fetchone()
        for _ in range(4):
            runtime = (await _runtime(cleanup_factory(**kwargs), fresh, capture_resources))
            await advance_cleanup(runtime)
        status, error = fresh.connection.execute(
            "SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)
        ).fetchone()
        expected = (
            (4, None)
            if present and delete_supported
            else (5, item_error_id("cleanup_items", "file_query_attempts_exhausted"))
            if present is None and query_supported
            else (5, item_error_id("cleanup_items", "file_delete_failed"))
        )
        assert (status, error) == expected
        assert len(driver.deletes) == int(bool(present and delete_supported))
        assert len(driver.queries) == (2 if present is None and query_supported else 0)
        assert (
            fresh.connection.execute(
                "SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id),
            ).fetchone()
            == original_attempt
        )
    finally:
        fresh.connection.close()


@pytest.mark.parametrize(
    "present", [None, True], ids=["unknown-delete", "present-delete"]
)
@pytest.mark.parametrize(
    "state",
    [
        "pending_unknown",
        "pending_present",
        "pending_absent",
        "saved_unknown",
        "saved_present",
        "saved_absent",
        "running_qualified",
        "running_unqualified",
    ],
)
async def test_newer_query_responsibility_precedes_unconsumed_durable_delete(environment, monkeypatch, present, state, capture_resources):
    from camctl.operations.recovery import RecoveryBoundary
    from camctl.operations.attempts import AttemptConfig
    from camctl.contracts.values import new_operation_key
    from .test_cleanup_query_save_recovery import _QueryFinishPort
    fresh, driver, item_id, delete_ticket, _, _ = await _durable_delete_only(environment, present, capture_resources)
    kwargs = dict(devices=environment[0].devices, drivers=_registry(driver), max_delete_attempts=3,
        max_query_attempts=2, occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    query_ticket = None
    port = None
    class SavedQuery(Exception):
        pass
    class QueryPort(_QueryFinishPort):
        enabled = True
        def finish_attempt(self, finish, key, owned):
            result = super().finish_attempt(finish, key, owned)
            if self.enabled and state.startswith("saved") and result.kind is DbOutcomeKind.COMPLETED:
                return replace(result, handoff_error=SavedQuery())
            return result
    ports = ExitStack()
    try:
        if state.startswith("running"):
            action_id, = fresh.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
            request = AttemptIntent("query", action_id, OperationKind.CHECK_FILE_EXISTS,
                AttemptTarget(cleanup_item_id=item_id), None, AttemptConfig(5, Decimal(17), Decimal(3)), _NOW)
            begun = OutputsRepository().begin_cleanup_attempt(request, new_operation_key(), fresh)
            assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
            query_ticket = begun.value.ticket
        else:
            driver.present = None if state.endswith("unknown") else state.endswith("present")
            driver.unknown_delete = True
            devices = {name: {**declaration, "cleanup": {"query_timeout_s": Decimal(17),
                "delete_retry_interval_s": Decimal(0), "query_retry_interval_s": Decimal(0)}}
                for name, declaration in environment[0].devices.items()}
            runtime = await _configured(environment, fresh, driver, devices=devices, query_attempts=5)
            port = QueryPort(DbOutcomeKind.ROLLED_BACK if state.startswith("pending") else DbOutcomeKind.COMPLETED, item_id, driver)
            ports.enter_context(repository_port(
                OperationRepository, port, ("finish_attempt",),
                matches=lambda _name, args, _kwargs: (args[0].ticket.operation == "query"
                    and args[0].ticket.target_id == str(item_id))))
            # 已确认在场的旧DELETE先按公开单次调用消费，下一调用才产生新DELETE/QUERY。
            for _ in range(2):
                try:
                    await delete_source_file(runtime, item_id)
                except (ConsistencyError, SavedQuery):
                    assert port.finishes
                    break
            assert port.finishes
            original = port.finishes[0]
            query_ticket = original[0].ticket
            assert original[0].outcome.outcome is driver.last_query_result.outcome
        horizon, = fresh.connection.execute("SELECT MAX(id) FROM history_events").fetchone()
        before = tuple(fresh.connection.iterdump())
        delete_row = fresh.connection.execute("SELECT * FROM operation_attempts WHERE run_id=?", (delete_ticket.run_id,)).fetchone()
        old_calls = len(driver.deletes), len(driver.queries)
        registry = _recovering_registry(driver, "query") if state == "running_qualified" else DriverRegistry()
        assembly = cleanup_factory if state.startswith("pending") else session_cleanup_assembly
        factory = assembly(**{**kwargs, "drivers": registry}, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon)
        resumed = await factory(await capture_resources.restart_database(fresh))
        if port is not None and state.startswith("pending"):
            port.kind = DbOutcomeKind.COMPLETED
            port.enabled = False
        if state == "running_unqualified":
            with pytest.raises(ConsistencyError, match="恢复收场"):
                await advance_cleanup(resumed)
            assert tuple(fresh.connection.iterdump()) == before
        else:
            await advance_cleanup(resumed)
            if state.endswith("absent"):
                assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (4,)
            elif state.startswith("saved"):
                assert tuple(fresh.connection.iterdump()) == before
            if port is not None:
                if state.startswith("pending"):
                    assert port.finishes == [original, original]
                saved = fresh.connection.execute("SELECT e.occurred_at,t.operation_key,a.result_json FROM operation_attempts a"
                    " JOIN history_events e ON e.id=a.result_event_id JOIN history_transactions t ON t.id=e.transaction_id"
                    " WHERE a.run_id=? AND a.attempt_no=?", (query_ticket.run_id, query_ticket.attempt_id)).fetchone()
                assert saved[:2] == (original[0].occurred_at, str(original[1]))
                assert original[0].outcome.outcome is driver.last_query_result.outcome
            if state.startswith(("saved", "pending")) and not state.endswith("absent"):
                after = tuple(fresh.connection.iterdump())
                with pytest.raises(DeviceConfigurationError):
                    await advance_cleanup(resumed)
                assert tuple(fresh.connection.iterdump()) == after
        assert (len(driver.deletes), len(driver.queries)) == old_calls
        assert fresh.connection.execute("SELECT * FROM operation_attempts WHERE run_id=?", (delete_ticket.run_id,)).fetchone() == delete_row
    finally:
        ports.close()
        fresh.connection.close()


@pytest.mark.parametrize("present", [None, True], ids=["unknown", "present"])
async def test_direct_durable_delete_consumption_keeps_original_single_call_semantics(
    environment, present,
    capture_resources,
):
    fresh, driver, item_id, _, _, _ = await _durable_delete_only(environment, present, capture_resources)
    driver.present = False
    kwargs = dict(
        devices=environment[0].devices,
        drivers=_registry(driver),
        max_delete_attempts=3,
        max_query_attempts=2,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    try:
        runtime = (await _runtime(session_cleanup_assembly(**kwargs), fresh, capture_resources))
        step = await delete_source_file(runtime, item_id)
        assert step.phase == ("still_present" if present else "succeeded")
        assert not driver.deletes and len(driver.queries) == int(present is None)
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("present", [None, True], ids=["unknown", "present"])
@pytest.mark.parametrize("query_supported", [False, True])
async def test_consumed_durable_delete_never_creates_another_cancel_query_slot(
    environment, present, query_supported,
    capture_resources,
):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    from camctl.contracts.workflow_errors import item_error_id

    fresh, driver, item_id, ticket, _, _ = await _durable_delete_only(
        environment, present
    , capture_resources)
    action = fresh.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    _apply_cleanup_cancel(fresh, action)
    entry = _registry(driver).entry("camctl-adb")
    registry = DriverRegistry(
        (
            replace(
                entry,
                declaration=replace(
                    entry.declaration,
                    delete_supported=False,
                    file_query_supported=query_supported,
                ),
            ),
        )
    )
    factory = session_cleanup_assembly(
        devices=environment[0].devices,
        drivers=registry,
        max_delete_attempts=3,
        max_query_attempts=3,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    original = fresh.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id=?", (ticket.run_id,)
    ).fetchone()
    try:
        await advance_cleanup((await _runtime(factory, fresh, capture_resources)))
        assert not driver.deletes and not driver.queries
        for _ in range(3):
            await advance_cleanup((await _runtime(factory, fresh, capture_resources)))
        assert len(driver.queries) == int(present is None and query_supported)
        assert not driver.deletes
        assert fresh.connection.execute(
            "SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)
        ).fetchone() == (
            6,
            item_error_id(
                "cleanup_items",
                "file_delete_failed" if present else "delete_unconfirmed",
            ),
        )
        assert (
            fresh.connection.execute(
                "SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id),
            ).fetchone()
            == original
        )
    finally:
        fresh.connection.close()


@pytest.mark.parametrize(
    "not_dispatched", [True, False], ids=["reliable-unsent", "possible-delete"]
)
async def test_durable_selection_respects_new_delete_dispatch_fact_over_saved_query(environment, monkeypatch, not_dispatched, capture_resources):
    from camctl.contracts.values import new_operation_key
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.evidence import EvidenceContract
    from camctl.devices.ports import ControlRequest
    from camctl.operations.attempts import AttemptConfig, AttemptFinish
    from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, ErrorValue, Settlement, SettlementBasis, EvidenceValue
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.operations import OperationRepository
    fresh, driver, item_id, _, _, _ = await _durable_delete_only(environment, None, capture_resources)
    repository = OutputsRepository()
    evidence = _registry(driver).entry("camctl-adb").evidence.with_contract(
        EvidenceContract("dispatch_prevented", 1, "delete", frozenset()))
    action_id, device_id, driver_id = fresh.connection.execute(
        "SELECT c.action_id,a.device_id,a.driver_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id"
        " JOIN device_files f ON f.id=o.device_file_id JOIN actions a ON a.id=f.observer_action_id WHERE c.id=?", (item_id,)).fetchone()
    def begin(operation, kind):
        request = AttemptIntent(operation, action_id, kind, AttemptTarget(cleanup_item_id=item_id),
            None, AttemptConfig(5, Decimal(17), Decimal(0)), _NOW)
        result = repository.begin_cleanup_attempt(request, new_operation_key(), fresh)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.ticket is not None
        return result.value.ticket
    query_ticket = begin("query", OperationKind.CHECK_FILE_EXISTS)
    driver.present = True
    actual_query = await driver.query_file(ControlRequest("query", DeviceBinding(device_id, driver_id),
        {"cleanup_item_id": str(item_id)}, query_ticket, Decimal(17)))
    query_key = new_operation_key()
    saved_query = OperationRepository().finish_attempt(AttemptFinish(query_ticket,
        validate_outcome(query_ticket, actual_query.outcome, evidence), _NOW, retry_wait=True), query_key, fresh)
    assert saved_query.kind is DbOutcomeKind.COMPLETED, saved_query.error
    query_row = fresh.connection.execute("SELECT * FROM operation_attempts WHERE run_id=?", (query_ticket.run_id,)).fetchone()
    delete_ticket = begin("delete", OperationKind.DELETE_FILE)
    actual_delete = (CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        error=ErrorValue("canceled_before_dispatch", "delete"), settlement=Settlement(
            SettlementBasis.NOT_DISPATCHED, EvidenceValue("dispatch_prevented", 1, {}))) if not_dispatched else
        CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN, settlement=Settlement(
            SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {}))))
    delete_key = new_operation_key()
    saved_delete = repository.finish_cleanup_attempt(AttemptFinish(delete_ticket,
        validate_outcome(delete_ticket, actual_delete, evidence), _NOW, retry_wait=True), delete_key, fresh)
    assert saved_delete.kind is DbOutcomeKind.COMPLETED, saved_delete.error
    before = tuple(fresh.connection.iterdump())
    old_calls = len(driver.deletes), len(driver.queries)
    try:
        factory = cleanup_factory(devices=environment[0].devices, drivers=DriverRegistry(),
            max_delete_attempts=3, max_query_attempts=2, occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
        await advance_cleanup(await factory(await capture_resources.restart_database(fresh)))
        assert tuple(fresh.connection.iterdump()) == before
        assert (len(driver.deletes), len(driver.queries)) == old_calls
        order = []
        original_delete, original_query = driver.delete, driver.query_file
        async def delete(request):
            order.append("delete")
            return await original_delete(request)
        async def query(request):
            order.append("query")
            return await original_query(request)
        driver.delete, driver.query_file = delete, query
        driver.present = False
        devices = {name: {**declaration, "cleanup": {"delete_retry_interval_s": Decimal(0),
            "query_retry_interval_s": Decimal(0)}} for name, declaration in environment[0].devices.items()}
        current = await _configured(environment, fresh, driver, devices=devices, delete_attempts=5)
        await advance_cleanup(current)
        assert order == (["delete"] if not_dispatched else ["query"])
        assert fresh.connection.execute("SELECT * FROM operation_attempts WHERE run_id=?", (query_ticket.run_id,)).fetchone() == query_row
        assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (4,)
        assert fresh.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key IN (?,?)",
            (str(query_key), str(delete_key))).fetchone() == (2,)
    finally:
        fresh.connection.close()
