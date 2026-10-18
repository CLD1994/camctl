"""默认轮次区分原结果交接、可靠未派发票据和普通调用资格。"""

from decimal import Decimal
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceConfigurationError
from camctl.devices.drivers.registry import DriverRegistry
from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
from camctl.outputs.cleanup_flow import (
    advance_cleanup,
    delete_source_file,
    _begin_cleanup_intent,
    _run_query,
    CleanupQueryConsumer,
)
from camctl.persistence.models import DbOutcomeKind

from .test_cleanup_delete_save_recovery import _prepared
from .test_cleanup_residual_recovery import _orphan_intent, _recovering_registry
from ..bootstrap.test_output_binding_changes import _NOW, _registry, environment

pytestmark = pytest.mark.asyncio


def _shared(runtime):
    return {
        "pending_intents": runtime.pending_intents,
        "pending_deletes": runtime.pending_deletes,
        "pending_queries": runtime.pending_queries,
        "pending_saves": runtime.pending_saves,
        "pending_companions": runtime.pending_companions,
        "consumed_queries": runtime.consumed_queries,
        "active_calls": runtime.active_calls,
    }


def _intent(runtime, item_id, operation, consumer=None):
    action_id = runtime.owned.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    config = replace(
        runtime.delete_config if operation == "delete" else runtime.query_config,
        timeout_s=Decimal(17),
        max_attempts=5,
    )
    request = AttemptIntent(
        operation,
        action_id,
        OperationKind.DELETE_FILE
        if operation == "delete"
        else OperationKind.CHECK_FILE_EXISTS,
        AttemptTarget(cleanup_item_id=item_id),
        None,
        config,
        _NOW,
    )
    result = _begin_cleanup_intent(runtime, request, consumer)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.ticket is not None
    return result.value.ticket


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_default_round_dispatches_reliable_unsent_original_ticket_once(
    environment, operation
):
    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment)
    driver.matrix_enabled, driver.present = True, False
    consumer = None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE
    ticket = _intent(runtime, item_id, operation, consumer)
    original = runtime.pending_intents[item_id]
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
    factory = session_cleanup_assembly(
        devices=devices,
        drivers=_registry(driver),
        max_delete_attempts=1,
        max_query_attempts=1,
        staging=Path(cfg.paths.staging),
        occurred_at=lambda: _NOW,
        **_shared(runtime),
    )
    await advance_cleanup(factory(owned))
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
    await advance_cleanup(factory(owned))
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
async def test_default_round_consumes_durable_query_before_current_ports_and_allows_next_round(
    environment, consumer, present
):
    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment)
    driver.matrix_enabled, driver.present = True, present
    if consumer == "after":
        driver.unknown_delete = True
        result = await delete_source_file(runtime, item_id)
        assert result.phase in ("still_present", "query_unknown")
    else:
        ticket = _intent(runtime, item_id, "query", CleanupQueryConsumer.BEFORE_DELETE)
        assert (
            await _run_query(
                runtime, ticket, item_id, CleanupQueryConsumer.BEFORE_DELETE
            )
            is True
        )
    attempts = tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
    old_calls = (len(driver.deletes), len(driver.queries))
    factory = session_cleanup_assembly(
        devices=cfg.devices,
        drivers=DriverRegistry(),
        max_delete_attempts=3,
        max_query_attempts=3,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    await advance_cleanup(factory(owned))
    assert (
        tuple(owned.connection.execute("SELECT * FROM operation_attempts")) == attempts
    )
    assert (len(driver.deletes), len(driver.queries)) == old_calls
    with pytest.raises(DeviceConfigurationError):
        await advance_cleanup(factory(owned))
    assert (
        tuple(owned.connection.execute("SELECT * FROM operation_attempts")) == attempts
    )
    assert (len(driver.deletes), len(driver.queries)) == old_calls


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_qualified_old_unknown_recovery_uses_whole_round_then_next_round_can_call(
    environment, operation
):
    from camctl.operations.recovery import RecoveryBoundary

    cfg = environment[0]
    owned, _, driver, item_id, ticket, horizon = await _orphan_intent(
        environment, operation
    )
    try:
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
        await advance_cleanup(factory(owned))
        assert not driver.deletes and not driver.queries
        original = owned.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone()
        assert owned.connection.execute(
            "SELECT status FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone() == (4,)
        await advance_cleanup(factory(owned))
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
async def test_absent_original_intent_is_not_granted_locally_before_current_eligibility(
    environment, operation, fault, change
):
    from camctl.persistence.repositories.outputs import OutputsRepository
    from .test_cleanup_delete_save_recovery import _FaultOutputs
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel

    cfg = environment[0]
    owned, context, runtime, driver, item_id = await _prepared(environment)
    action_id = owned.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    request = AttemptIntent(
        operation,
        action_id,
        OperationKind.DELETE_FILE
        if operation == "delete"
        else OperationKind.CHECK_FILE_EXISTS,
        AttemptTarget(cleanup_item_id=item_id),
        None,
        runtime.delete_config if operation == "delete" else runtime.query_config,
        _NOW,
    )
    consumer = None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE

    class FirstException(OutputsRepository):
        def begin_cleanup_attempt(self, *args):
            raise RuntimeError("原意图首次保存没有回执")

    runtime.outputs = (
        FirstException()
        if fault == "first_exception"
        else _FaultOutputs(
            "begin_cleanup_attempt",
            DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
        )
    )
    if fault == "first_exception":
        with pytest.raises(RuntimeError, match="首次保存"):
            _begin_cleanup_intent(runtime, request, consumer)
    else:
        result = _begin_cleanup_intent(runtime, request, consumer)
        assert result.kind is (
            DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN
        )
    original = runtime.pending_intents[item_id]
    original_values = (original.request, original.key, original.consumer)
    assert original.ticket is None
    assert (original.last_kind is None) == (fault == "first_exception")
    owned.connection.close()
    fresh = context.open_connection()
    try:
        if change == "cancel":
            _apply_cleanup_cancel(fresh, action_id)
        registry = _registry(driver)
        if change == "capability_false":
            entry = registry.entry("camctl-adb")
            registry = DriverRegistry(
                (
                    replace(
                        entry,
                        declaration=replace(
                            entry.declaration,
                            delete_supported=False,
                            file_query_supported=False,
                        ),
                    ),
                )
            )
        factory = session_cleanup_assembly(
            devices={} if change == "binding" else cfg.devices,
            drivers=registry,
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
            **_shared(runtime),
        )
        await advance_cleanup(factory(fresh))
        assert fresh.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?",
            (item_id,),
        ).fetchone() == (0,)
        assert not driver.deletes and not driver.queries
        assert (original.request, original.key, original.consumer) == original_values
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize(
    "delete_supported,query_supported", [(True, True), (False, True), (False, False)]
)
async def test_unsent_ticket_uses_current_operation_capability_and_preserves_original_config(
    environment, operation, delete_supported, query_supported
):
    from camctl.contracts.workflow_errors import item_error_id

    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment)
    driver.matrix_enabled, driver.present = True, False
    ticket = _intent(
        runtime,
        item_id,
        operation,
        None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE,
    )
    original = runtime.pending_intents[item_id]
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
    factory = session_cleanup_assembly(
        devices=cfg.devices,
        drivers=registry,
        max_delete_attempts=1,
        max_query_attempts=1,
        occurred_at=lambda: _NOW,
        **_shared(runtime),
    )
    await advance_cleanup(factory(owned))
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
    environment, present, query_supported
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

    runtime.outputs = QueryNotGranted()
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
    factory = session_cleanup_assembly(
        devices=cfg.devices,
        drivers=registry,
        max_delete_attempts=1,
        max_query_attempts=2,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: clock[0],
        **_shared(runtime),
    )
    for _ in range(4):
        clock[0] += 10_000_000_000
        await advance_cleanup(factory(owned))
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
):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel

    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment)
    runtime.query_config = replace(runtime.query_config, retry_interval_s=Decimal(60))
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
    factory = session_cleanup_assembly(
        devices=devices,
        drivers=_registry(driver),
        max_delete_attempts=3,
        max_query_attempts=4,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
        **_shared(runtime),
    )
    await advance_cleanup(factory(owned))
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
    environment, operation, change
):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    from camctl.contracts.enums import enum_for
    import json

    cfg = environment[0]
    owned, _, runtime, driver, item_id = await _prepared(environment)
    ticket = _intent(
        runtime,
        item_id,
        operation,
        None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE,
    )
    original = runtime.pending_intents[item_id]
    if change == "cancel":
        _apply_cleanup_cancel(owned, original.request.action_id)
    factory = session_cleanup_assembly(
        devices={} if change == "binding" else cfg.devices,
        drivers=_registry(driver),
        max_delete_attempts=1,
        max_query_attempts=1,
        occurred_at=lambda: _NOW,
        **_shared(runtime),
    )
    await advance_cleanup(factory(owned))
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
    environment, entry
):
    from .test_cleanup_query_local_consumption import _world

    cfg = environment[0]
    if entry == "before_query":
        runtime, driver, _, item_id, _ = await _world(environment, "before", True)
        owned = runtime.owned
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
    factory = session_cleanup_assembly(
        devices=devices,
        drivers=_registry(driver),
        max_delete_attempts=5,
        max_query_attempts=4,
        staging=Path(cfg.paths.staging),
        occurred_at=lambda: _NOW,
        **_shared(runtime),
    )
    await advance_cleanup(factory(owned))
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
    environment, operation
):
    import asyncio

    owned, context, runtime, driver, item_id = await _prepared(environment)
    driver.matrix_enabled, driver.present = True, False
    if operation == "query":
        _intent(runtime, item_id, operation, CleanupQueryConsumer.BEFORE_DELETE)
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
        rebuilt = session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
            **_shared(runtime),
        )(fresh)
        before = tuple(fresh.connection.iterdump())
        with pytest.raises(ConsistencyError):
            await delete_source_file(rebuilt, item_id)
        assert tuple(fresh.connection.iterdump()) == before
        await advance_cleanup(rebuilt)
        assert tuple(fresh.connection.iterdump()) == before
        assert len(calls) == 1 and runtime.active_calls
        release.set()
        result = await asyncio.wait_for(owner, timeout=5)
        assert result.phase == "succeeded"
        assert not runtime.active_calls and len(calls) == 1
    finally:
        release.set()
        if not owner.done():
            await owner
        if fresh is not None:
            fresh.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_default_round_rejects_isolated_dispatched_marker_without_owner(
    environment, operation
):
    owned, _, runtime, driver, item_id = await _prepared(environment)
    consumer = None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE
    ticket = _intent(runtime, item_id, operation, consumer)
    runtime.pending_intents[item_id].dispatched = True
    rebuilt = session_cleanup_assembly(
        devices={},
        drivers=DriverRegistry(),
        max_delete_attempts=3,
        max_query_attempts=3,
        occurred_at=lambda: _NOW,
        **_shared(runtime),
    )(owned)
    before = tuple(owned.connection.iterdump())
    with pytest.raises(ConsistencyError):
        await advance_cleanup(rebuilt)
    assert tuple(owned.connection.iterdump()) == before
    assert not driver.deletes and not driver.queries and not runtime.active_calls
    assert runtime.pending_intents[item_id].ticket == ticket


async def _durable_delete_only(environment, present):
    from camctl.contracts.values import new_operation_key
    from camctl.devices.evidence import EvidenceContract, DeviceObservation
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.models import (
        AttemptStatus,
        CallOutcome,
        EffectState,
        EvidenceValue,
        Settlement,
        SettlementBasis,
    )
    from camctl.operations.validation import validate_outcome
    from .test_cleanup_query_save_recovery import _MatrixDriver

    owned, context, runtime, _, item_id = await _prepared(environment)
    ticket = _intent(runtime, item_id, "delete")
    observations = ()
    if present is not None:
        runtime.evidence = runtime.evidence.with_contract(
            EvidenceContract(
                "file_delete_presence",
                1,
                "delete",
                frozenset({"cleanup_item_id", "present"}),
                identity_field="cleanup_item_id",
            )
        )
        observations = (
            DeviceObservation(
                "file_delete_presence",
                1,
                {"cleanup_item_id": str(item_id), "present": present},
            ),
        )
    outcome = CallOutcome(
        status=AttemptStatus.SUCCEEDED,
        effect=EffectState.UNKNOWN if present is None else EffectState.CONFIRMED,
        settlement=Settlement(
            SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})
        ),
        observations=observations,
    )
    key = new_operation_key()
    saved = runtime.outputs.finish_cleanup_attempt(
        AttemptFinish(
            ticket,
            validate_outcome(ticket, outcome, runtime.evidence),
            _NOW,
            retry_wait=True,
        ),
        key,
        owned,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    before = tuple(owned.connection.iterdump())
    owned.connection.close()
    fresh = context.open_connection()
    driver = _MatrixDriver(fresh)
    driver.matrix_enabled, driver.present = True, None
    driver.unknown_delete = False
    return fresh, driver, item_id, ticket, key, before


@pytest.mark.parametrize("present", [None, True], ids=["unknown", "present"])
@pytest.mark.parametrize("missing", [False, True], ids=["registered", "removed"])
async def test_durable_delete_is_consumed_locally_before_any_current_port(
    environment, monkeypatch, present, missing
):
    from camctl.bootstrap import cleanup_assembly

    fresh, driver, item_id, ticket, key, before = await _durable_delete_only(
        environment, present
    )
    ports = []
    original_port = cleanup_assembly.port_for

    def port_for(entry, operation):
        ports.append(operation)
        return original_port(entry, operation)

    monkeypatch.setattr(cleanup_assembly, "port_for", port_for)
    factory = session_cleanup_assembly(
        devices=environment[0].devices,
        drivers=DriverRegistry() if missing else _registry(driver),
        max_delete_attempts=3,
        max_query_attempts=2,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    try:
        runtime = factory(fresh)
        await advance_cleanup(runtime)
        assert not ports and not driver.deletes and not driver.queries
        assert tuple(fresh.connection.iterdump()) == before
        assert runtime.consumed_queries[item_id].delete_identity == (
            fresh.metadata.instance_id,
            item_id,
            ticket,
            key,
        )
        if missing:
            with pytest.raises(DeviceConfigurationError):
                await advance_cleanup(factory(fresh))
            assert tuple(fresh.connection.iterdump()) == before
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("present", [None, True], ids=["unknown", "present"])
@pytest.mark.parametrize(
    "delete_supported,query_supported", [(True, True), (False, True), (False, False)]
)
async def test_durable_delete_consumption_survives_factory_rebuild_and_finite_budget(
    environment, monkeypatch, present, delete_supported, query_supported
):
    from camctl.bootstrap import cleanup_assembly
    from camctl.contracts.workflow_errors import item_error_id

    fresh, driver, item_id, ticket, key, before = await _durable_delete_only(
        environment, present
    )
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
    ports = []
    original_port = cleanup_assembly.port_for

    def port_for(entry, operation):
        ports.append(operation)
        return original_port(entry, operation)

    monkeypatch.setattr(cleanup_assembly, "port_for", port_for)
    kwargs = dict(
        devices=devices,
        drivers=registry,
        max_delete_attempts=3,
        max_query_attempts=2,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    try:
        runtime = session_cleanup_assembly(**kwargs)(fresh)
        await advance_cleanup(runtime)
        assert not ports and tuple(fresh.connection.iterdump()) == before
        original_attempt = fresh.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id=?", (ticket.run_id,)
        ).fetchone()
        for _ in range(4):
            runtime = session_cleanup_assembly(**kwargs, **_shared(runtime))(fresh)
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
        assert item_id not in runtime.consumed_queries
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
async def test_newer_query_responsibility_precedes_unconsumed_durable_delete(
    environment, monkeypatch, present, state
):
    from camctl.bootstrap import cleanup_assembly
    from camctl.operations.recovery import RecoveryBoundary
    from camctl.outputs.cleanup_flow import _pending_query_for_item
    from .test_cleanup_query_save_recovery import _QueryFinishPort

    fresh, driver, item_id, delete_ticket, _, _ = await _durable_delete_only(
        environment, present
    )
    kwargs = dict(
        devices=environment[0].devices,
        drivers=_registry(driver),
        max_delete_attempts=3,
        max_query_attempts=2,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    runtime = session_cleanup_assembly(**kwargs)(fresh).for_item(item_id)
    runtime.for_item = None
    query_ticket = _intent(runtime, item_id, "query", CleanupQueryConsumer.AFTER_DELETE)
    pending_original = None
    try:
        if not state.startswith("running"):
            driver.present = (
                None if state.endswith("unknown") else state.endswith("present")
            )
            if state.startswith("pending"):
                runtime.operations = _QueryFinishPort(
                    DbOutcomeKind.ROLLED_BACK, item_id, driver, runtime.retry_gate
                )
                with pytest.raises(ConsistencyError):
                    await _run_query(
                        runtime,
                        query_ticket,
                        item_id,
                        CleanupQueryConsumer.AFTER_DELETE,
                    )
                pending = _pending_query_for_item(runtime, item_id)
                pending_original = (
                    pending,
                    pending.finish,
                    pending.key,
                    pending.occurred_at,
                    pending.actual,
                )
            else:
                await _run_query(
                    runtime, query_ticket, item_id, CleanupQueryConsumer.AFTER_DELETE
                )
        horizon = fresh.connection.execute(
            "SELECT MAX(id) FROM history_events"
        ).fetchone()[0]
        before = tuple(fresh.connection.iterdump())
        delete_row = fresh.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id=?", (delete_ticket.run_id,)
        ).fetchone()
        old_calls = (len(driver.deletes), len(driver.queries))

        def forbidden_port(*args, **kwargs):
            raise AssertionError("原 QUERY 交接前不能解析当前端口")

        monkeypatch.setattr(cleanup_assembly, "port_for", forbidden_port)
        shared = _shared(runtime) if state.startswith("pending") else {}
        registry = (
            _recovering_registry(driver, "query")
            if state == "running_qualified"
            else DriverRegistry()
        )
        resumed = session_cleanup_assembly(
            **{**kwargs, "drivers": registry},
            **shared,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon,
        )(fresh)
        if state == "running_unqualified":
            with pytest.raises(ConsistencyError, match="恢复收场"):
                await advance_cleanup(resumed)
            assert tuple(fresh.connection.iterdump()) == before
            assert resumed.last_recovery_diagnostic is not None
        else:
            await advance_cleanup(resumed)
            member = fresh.connection.execute(
                "SELECT status FROM cleanup_items WHERE id=?", (item_id,)
            ).fetchone()[0]
            if state.endswith("absent"):
                assert member == 4
            else:
                record = resumed.consumed_queries[item_id]
                assert record.delete_identity is None
                assert record.query_identity[2] == query_ticket
                assert record.result_identity == record.query_identity
                if state.startswith("saved"):
                    assert tuple(fresh.connection.iterdump()) == before
            if pending_original is not None:
                pending, finish, key, occurred, actual = pending_original
                assert pending.finish is finish and pending.key == key
                assert pending.occurred_at == occurred and pending.actual is actual
                assert not resumed.pending_queries and not resumed.pending_intents
            if state.startswith(("saved", "pending")) and not state.endswith("absent"):
                after_handoff = tuple(fresh.connection.iterdump())
                with pytest.raises(DeviceConfigurationError):
                    await advance_cleanup(resumed)
                assert tuple(fresh.connection.iterdump()) == after_handoff
        assert (len(driver.deletes), len(driver.queries)) == old_calls
        assert (
            fresh.connection.execute(
                "SELECT * FROM operation_attempts WHERE run_id=?",
                (delete_ticket.run_id,),
            ).fetchone()
            == delete_row
        )
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("present", [None, True], ids=["unknown", "present"])
async def test_direct_durable_delete_consumption_keeps_original_single_call_semantics(
    environment, present
):
    fresh, driver, item_id, _, _, _ = await _durable_delete_only(environment, present)
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
        runtime = session_cleanup_assembly(**kwargs)(fresh)
        step = await delete_source_file(runtime, item_id)
        assert step.phase == ("still_present" if present else "succeeded")
        assert not driver.deletes and len(driver.queries) == int(present is None)
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("present", [None, True], ids=["unknown", "present"])
@pytest.mark.parametrize("query_supported", [False, True])
async def test_consumed_durable_delete_never_creates_another_cancel_query_slot(
    environment, present, query_supported
):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    from camctl.contracts.workflow_errors import item_error_id

    fresh, driver, item_id, ticket, _, _ = await _durable_delete_only(
        environment, present
    )
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
        await advance_cleanup(factory(fresh))
        assert not driver.deletes and not driver.queries
        for _ in range(3):
            await advance_cleanup(factory(fresh))
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
async def test_durable_selection_respects_new_delete_dispatch_fact_over_saved_query(
    environment, monkeypatch, not_dispatched
):
    from camctl.bootstrap import cleanup_assembly
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.validation import validate_outcome
    from camctl.devices.ports import DeviceCallResult
    from camctl.operations.models import (
        CallOutcome,
        AttemptStatus,
        EffectState,
        Settlement,
        SettlementBasis,
        EvidenceValue,
    )
    from camctl.outputs.cleanup_flow import (
        _consume_saved_query,
        _pending_query_for_item,
        _not_dispatched,
        _not_dispatched_evidence,
    )

    fresh, driver, item_id, _, _, _ = await _durable_delete_only(environment, None)
    kwargs = dict(
        devices=environment[0].devices,
        drivers=_registry(driver),
        max_delete_attempts=3,
        max_query_attempts=2,
        occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    runtime = session_cleanup_assembly(**kwargs)(fresh).for_item(item_id)
    runtime.for_item = None
    driver.present = True
    query_ticket = _intent(runtime, item_id, "query", CleanupQueryConsumer.AFTER_DELETE)
    await _run_query(runtime, query_ticket, item_id, CleanupQueryConsumer.AFTER_DELETE)
    query = _pending_query_for_item(runtime, item_id)
    query_key = query.key
    _consume_saved_query(runtime, query)
    runtime.monotonic_ns = lambda: 4_000_000_000
    delete_ticket = _intent(runtime, item_id, "delete")
    if not_dispatched:
        actual = _not_dispatched("delete")
        evidence = _not_dispatched_evidence(runtime.evidence, delete_ticket)
    else:
        actual = DeviceCallResult.from_outcome(
            CallOutcome(
                status=AttemptStatus.SUCCEEDED,
                effect=EffectState.UNKNOWN,
                settlement=Settlement(
                    SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})
                ),
            )
        )
        evidence = runtime.evidence
    key = new_operation_key()
    saved = runtime.outputs.finish_cleanup_attempt(
        AttemptFinish(
            delete_ticket,
            validate_outcome(delete_ticket, actual.outcome, evidence),
            _NOW,
            retry_wait=True,
        ),
        key,
        fresh,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    before = tuple(fresh.connection.iterdump())

    def forbidden_port(*args, **kwargs):
        raise AssertionError("必须先选择并消费原结果")

    monkeypatch.setattr(cleanup_assembly, "port_for", forbidden_port)
    try:
        resumed = session_cleanup_assembly(**{**kwargs, "drivers": DriverRegistry()})(
            fresh
        )
        await advance_cleanup(resumed)
        assert tuple(fresh.connection.iterdump()) == before
        record = resumed.consumed_queries[item_id]
        if not_dispatched:
            assert record.query_identity == (
                fresh.metadata.instance_id,
                item_id,
                query_ticket,
                query_key,
            )
            assert record.delete_identity is None
        else:
            assert record.delete_identity == (
                fresh.metadata.instance_id,
                item_id,
                delete_ticket,
                key,
            )
            assert record.query_identity is None
    finally:
        fresh.connection.close()
