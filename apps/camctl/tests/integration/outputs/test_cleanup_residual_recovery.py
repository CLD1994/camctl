"""原保存责任与持久结果在数据库重开后继续交付，残留责任按原事实发现。"""

from pathlib import Path

import pytest

from support.repository_ports import repository_port
from camctl.session.service import LocalWorkPort
from camctl.persistence.repositories.operations import OperationRepository

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.devices.drivers.registry import DriverRegistry
from camctl.outputs.cleanup_flow import advance_cleanup, delete_source_file
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_cleanup_delete_save_recovery import _FaultOutputs, _prepared, _configured
from ..support.capture import capture_database
from ..support.outputs import cleanup_factory
from ..bootstrap.test_output_binding_changes import _NOW, _registry, environment
from ..support.recording_consumer_cases import recording_cases  # noqa: F401
from ..support.repaired_cleanup_cases import repaired_cleanup, unknown_host_delete  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _stored_runtime(factory, owned, resources):
    return await factory(await resources.restart_database(owned))


@pytest.mark.parametrize(
    "removed", [False, True], ids=["binding-changed", "driver-removed"]
)
@pytest.mark.parametrize("observation", ["file_absent", "file_delete_presence"])
@pytest.mark.parametrize("with_error", [False, True])
async def test_saved_delete_absence_handoff_precedes_current_ports(
    environment, removed, observation, with_error,
    capture_resources,
):
    cfg = environment[0]
    owned, context, runtime, driver, item_id = await _prepared(environment)
    from camctl.devices.evidence import DeviceObservation, EvidenceContract
    from camctl.devices.ports import DeviceCallResult
    from camctl.operations.models import (
        CallOutcome,
        AttemptStatus,
        EffectState,
        ErrorValue,
        Settlement,
        SettlementBasis,
        EvidenceValue,
    )

    original_delete = driver.delete

    async def absent_with_actual_error(request):
        await original_delete(request)
        data = {"cleanup_item_id": str(item_id)}
        if observation == "file_delete_presence":
            data["present"] = False
        return DeviceCallResult.from_outcome(
            CallOutcome(
                status=AttemptStatus.FAILED if with_error else AttemptStatus.SUCCEEDED,
                effect=EffectState.CONFIRMED,
                error=ErrorValue("device_error", "delete", {"actual": "retained"})
                if with_error
                else None,
                settlement=Settlement(
                    SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})
                ),
                observations=(DeviceObservation(observation, 1, data),),
            )
        )

    driver.delete = absent_with_actual_error
    if observation == "file_delete_presence":
        evidence = _registry(driver).entry("camctl-adb").evidence.with_contract(
            EvidenceContract(
                observation,
                1,
                "delete",
                frozenset({"cleanup_item_id", "present"}),
                identity_field="cleanup_item_id",
            )
        )
    if observation == "file_delete_presence":
        runtime = await _configured(environment, owned, driver, evidence=evidence)
    port = _FaultOutputs("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, ("finish_cleanup_item",),
                         matches=lambda _name, args, _kwargs: args[0].item_id == item_id):
        assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
    attempts = tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
    history = tuple(owned.connection.execute("SELECT * FROM history_events"))
    assert len(driver.deletes) == 1
    owned.connection.close()
    fresh = context.open_connection()
    try:
        # 新 factory 不接收原运行时、返回、驱动或任何保存集合，额度也降至已用次数。
        factory = session_cleanup_assembly(
            devices=cfg.devices if removed else {},
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            staging=Path(cfg.paths.staging),
            occurred_at=lambda: _NOW + 50_000_000,
        )
        await advance_cleanup(await _stored_runtime(factory, fresh, capture_resources))
        assert fresh.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (item_id,)
        ).fetchone() == (4,)
        assert (
            tuple(fresh.connection.execute("SELECT * FROM operation_attempts"))
            == attempts
        )
        assert (
            tuple(
                fresh.connection.execute(
                    "SELECT * FROM history_events ORDER BY id LIMIT ?", (len(history),)
                )
            )
            == history
        )
        assert len(driver.deletes) == 1 and not driver.queries
    finally:
        fresh.connection.close()


async def test_terminal_action_does_not_hide_open_cleanup_companion(environment, capture_resources):
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.cleanup_flow import FinishCleanupAction
    from camctl.persistence.repositories.outputs import OutputsRepository
    from .test_cleanup_delete_save_recovery import _FaultCompanion

    cfg = environment[0]
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultCompanion()
    with repository_port(OperationRepository, port, ("finish_stale_runs",),
                         matches=lambda _name, args, _kwargs: f"delete/{item_id}" in args[0].responsibility_keys):
        assert (await delete_source_file(runtime, item_id)).phase == "companion_rejected"
    action_id = owned.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    saved = OutputsRepository().finish_cleanup_action(
        FinishCleanupAction(action_id, _NOW), new_operation_key(), owned
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    member = owned.connection.execute(
        "SELECT * FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()
    attempts = tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
    owned.connection.close()
    fresh = context.open_connection()
    try:
        runtime = await _stored_runtime(session_cleanup_assembly(
            devices=cfg.devices,
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            staging=Path(cfg.paths.staging),
            occurred_at=lambda: _NOW + 50_000_000,
        ), fresh, capture_resources)
        await advance_cleanup(runtime)
        assert fresh.connection.execute(
            "SELECT status FROM operation_runs WHERE cleanup_item_id=?", (item_id,)
        ).fetchall() == [(3,)]
        assert (
            fresh.connection.execute(
                "SELECT * FROM cleanup_items WHERE id=?", (item_id,)
            ).fetchone()
            == member
        )
        assert (
            tuple(fresh.connection.execute("SELECT * FROM operation_attempts"))
            == attempts
        )
        assert len(driver.deletes) == 1 and not driver.queries
    finally:
        fresh.connection.close()


async def _orphan_intent(environment, operation="delete"):
    return await _orphan_intent_for_prepared(
        environment, await _prepared(environment, before_query=operation == "query"), operation
    )


async def _orphan_intent_for_prepared(environment, prepared, operation, *, host_query=False, file_executor=None):
    owned, context, runtime, driver, item_id = prepared
    from dataclasses import replace
    class IntentSaved(Exception):
        pass
    class IntentBoundary(OutputsRepository):
        ticket = None
        original = None
        def begin_cleanup_attempt(self, request, key, current):
            receipt = super().begin_cleanup_attempt(request, key, current)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            assert receipt.value.ticket is not None
            assert request.operation == operation
            self.ticket = receipt.value.ticket
            self.original = (request, key)
            return replace(receipt, handoff_error=IntentSaved())
    cfg = environment[0]
    factory = session_cleanup_assembly(devices=cfg.devices, drivers=_registry(driver),
        max_delete_attempts=3, max_query_attempts=3, staging=Path(cfg.paths.staging),
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0, file_executor=file_executor)
    runtime = await factory(await capture_database(owned))
    boundary = IntentBoundary()
    with repository_port(OutputsRepository, boundary, ("begin_cleanup_attempt",),
            matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id
            and args[0].operation == operation):
        with pytest.raises(IntentSaved):
            await delete_source_file(runtime, item_id)
    ticket = boundary.ticket
    assert ticket is not None
    if operation == "query":
        from decimal import Decimal
        request, key = boundary.original
        assert (request.config.max_attempts, request.config.timeout_s,
                request.config.retry_interval_s) == ((3, None, None) if host_query else (3, Decimal(10), Decimal(3)))
        assert request.occurred_at == _NOW
        assert owned.connection.execute(
            "SELECT operation_key FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (str(key),)
        assert not driver.queries and not driver.deletes
    horizon, = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()
    owned.connection.close()
    return context.open_connection(), context, driver, item_id, ticket, horizon


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_running_intent_is_never_replayed_without_recovery_qualification(
    environment, operation,
    capture_resources,
):
    from camctl.contracts.values import ConsistencyError

    owned, _, driver, item_id, ticket, _ = await _orphan_intent(environment, operation)
    before = tuple(owned.connection.iterdump())
    runtime = await _stored_runtime(session_cleanup_assembly(
        devices={},
        drivers=DriverRegistry(),
        max_delete_attempts=3,
        max_query_attempts=3,
        occurred_at=lambda: _NOW,
    ), owned, capture_resources)
    try:
        with pytest.raises(ConsistencyError, match="恢复|收场"):
            await advance_cleanup(runtime)
        assert tuple(owned.connection.iterdump()) == before
        assert not driver.deletes and not driver.queries
    finally:
        owned.connection.close()


def _recovering_registry(driver, operation):
    from dataclasses import replace
    from camctl.devices.evidence import EvidenceContract
    from ..bootstrap.test_output_binding_changes import _registry

    old = _registry(driver).entry("camctl-adb")
    return DriverRegistry(
        (
            replace(
                old,
                declaration=replace(
                    old.declaration,
                    adb_foreground_recovery_operations=frozenset({operation}),
                ),
                evidence=old.evidence.with_contract(
                    EvidenceContract(
                        "adb_foreground_recovery", 1, operation, frozenset()
                    )
                ),
            ),
        )
    )


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_qualified_old_running_attempt_saves_unknown_on_original_ticket(
    environment, operation,
    capture_resources,
):
    import json
    from camctl.capture.recovery import RecoveryBoundary

    owned, _, driver, item_id, ticket, horizon = await _orphan_intent(
        environment, operation
    )
    try:
        runtime = await _stored_runtime(session_cleanup_assembly(
            devices={},
            drivers=_recovering_registry(driver, operation),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW + 10,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon,
        ), owned, capture_resources)
        await advance_cleanup(runtime)
        row = owned.connection.execute(
            "SELECT status,effect_state,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone()
        assert row[:2] == (4, 1)
        result = json.loads(row[2])
        assert result["settlement"] == {
            "basis": "assumed",
            "evidence": {"type": "adb_foreground_recovery", "version": 1, "data": {}},
        }
        assert result["observations"] == [] and "call_info" not in result
        assert not driver.deletes and not driver.queries
    finally:
        owned.connection.close()


async def test_later_reliably_not_dispatched_delete_keeps_saved_query(environment, capture_resources):
    import json
    from camctl.contracts.enums import enum_for
    from camctl.contracts.workflow_errors import item_error_id
    from .test_cleanup_query_cancel_before_delete import _EntryPort
    from .test_cleanup_query_local_consumption import _world

    cfg = environment[0]
    _, driver, action_id, item_id, _ = await _world(environment, "after", True)
    owned = environment[1]
    clock = {"ns": 0}
    factory = cleanup_factory(
        devices=cfg.devices, drivers=_registry(driver), max_delete_attempts=3,
        max_query_attempts=3, staging=Path(cfg.paths.staging),
        occurred_at=lambda: _NOW, monotonic_ns=lambda: clock["ns"],
    )
    runtime = await factory(await capture_database(owned))
    first = await delete_source_file(runtime, item_id)
    assert first.phase == "still_present", first
    assert len(driver.deletes) == len(driver.queries) == 1
    query = owned.connection.execute(
        "SELECT a.run_id,a.attempt_no,a.status,a.effect_state,a.result_json,"
        " a.error_json,a.intent_event_id,a.result_event_id"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind=5", (item_id,),
    ).fetchone()
    assert query is not None
    assert (query[0], query[1]) == (
        driver.query_requests[0].ticket.run_id, driver.query_requests[0].ticket.attempt_id)
    observations = json.loads(query[4])["observations"]
    assert any(value["type"] == "file_presence" and value["data"] == {
        "cleanup_item_id": str(item_id), "present": True} for value in observations)

    # 等待已经确认仍在后的删除间隔；票据提交后取消，生产流程保存未派发。
    clock["ns"] = 10_000_000_000
    port = _EntryPort(owned, item_id, action_id, "cancel_after_grant")
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs:
                         args[0].target.cleanup_item_id == item_id):
        step = await delete_source_file(runtime, item_id)

    assert (step.phase, step.detail) == ("canceled", "file_delete_failed"), step
    assert [entry[:2] for entry in port.entries] == [("delete", DbOutcomeKind.COMPLETED)]
    attempts = owned.connection.execute(
        "SELECT a.run_id,a.attempt_no,a.status,a.effect_state,a.result_json,"
        " a.intent_event_id,a.result_event_id FROM operation_attempts a"
        " JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind=4 ORDER BY a.attempt_no", (item_id,),
    ).fetchall()
    assert len(attempts) == 2
    assert (attempts[0][0], attempts[0][1]) == (
        driver.delete_requests[0].ticket.run_id, driver.delete_requests[0].ticket.attempt_id)
    later = attempts[1]
    assert later[0] == attempts[0][0] and later[1] == attempts[0][1] + 1
    assert later[2:4] == (int(enum_for("operation_attempts.status").FAILED),
                          int(enum_for("operation_attempts.effect_state").NO_EFFECT))
    assert json.loads(later[4])["settlement"] == {
        "basis": "not_dispatched",
        "evidence": {"type": "dispatch_prevented", "version": 1, "data": {}}}
    assert attempts[0][6] < query[6] <= query[7] < later[5] < later[6]
    assert owned.connection.execute(
        "SELECT a.run_id,a.attempt_no,a.status,a.effect_state,a.result_json,"
        " a.error_json,a.intent_event_id,a.result_event_id"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind=5", (item_id,),
    ).fetchone() == query
    assert len(driver.deletes) == len(driver.queries) == 1
    status, restriction, error, final_event = owned.connection.execute(
        "SELECT status,restriction_state,error_code,final_event_id"
        " FROM cleanup_items WHERE id=?", (item_id,),
    ).fetchone()
    assert (status, restriction, error) == (
        int(enum_for("cleanup_items.status").CANCELED),
        int(enum_for("cleanup_items.restriction_state").IRREVERSIBLE),
        item_error_id("cleanup_items", "file_delete_failed"))
    assert owned.connection.execute(
        "SELECT cancel_requested FROM actions WHERE id=?", (action_id,)
    ).fetchone() == (1,)
    cancel_events = [event_id for event_id, body in owned.connection.execute(
        "SELECT id,body_json FROM history_events WHERE id>? AND id<?",
        (later[5], later[6]),
    ) if any(row["table"] == "actions" and row["id"] == action_id
             and row["before"]["values"].get("cancel_requested") == 0
             and row["after"]["values"].get("cancel_requested") == 1
             for row in json.loads(body)["rows"])]
    assert len(cancel_events) == 1
    assert later[6] < final_event


async def test_terminal_flow_running_attempt_is_work_and_cancellation_cannot_finish(
    environment,
    capture_resources,
):
    """真实收场端口仍等待残留调用；终态投影不消除独立调用责任。"""
    from camctl.bootstrap.application import query_work_facts
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.cancellation.ports import TargetSettlementPort
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.repositories.cancellation import CancellationRepository

    owned, _, driver, item_id, ticket, _ = await _orphan_intent(environment)
    try:
        # 构造遗留矛盾只改变投影，验证发现不会因三个终态维度漏掉真实调用。
        owned.connection.execute(
            "UPDATE operation_runs SET status=3 WHERE id=?", (ticket.run_id,)
        )
        owned.connection.execute(
            "UPDATE cleanup_items SET status=4,restriction_state=4,outcome=1,final_event_id=1 WHERE id=?",
            (item_id,),
        )
        action_id = owned.connection.execute(
            "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)
        ).fetchone()[0]
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=?", (action_id,))
        owned.connection.commit()
        assert query_work_facts(owned.connection).required_settlements > 0
        settlement: TargetSettlementPort = TargetSettlement(
            await capture_resources.restart_database(owned),
            OutputsRepository(),
            CancellationRepository(),
            lambda _: "unknown",
            lambda: _NOW,
        )
        assert not (await settlement.settle(action_id)).complete
        assert not driver.deletes and not driver.queries
    finally:
        owned.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize("missing", ["boundary", "horizon", "outside", "support"])
async def test_running_recovery_requires_each_original_qualification(
    environment, operation, missing,
    capture_resources,
):
    from camctl.capture.recovery import RecoveryBoundary, RecoveryBlockedReason
    from camctl.contracts.values import ConsistencyError

    owned, _, driver, item_id, ticket, horizon = await _orphan_intent(
        environment, operation
    )
    try:
        registry = _recovering_registry(driver, operation)
        if missing == "support":
            from ..bootstrap.test_output_binding_changes import _registry

            registry = _registry(driver)
        diagnostics = []
        before_intent = (
            owned.connection.execute(
                "SELECT intent_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id),
            ).fetchone()[0]
            - 1
        )
        runtime = await _stored_runtime(session_cleanup_assembly(
            devices={},
            drivers=registry,
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
            recovery_boundary=(
                RecoveryBoundary.UNCONFIRMED
                if missing == "boundary"
                else RecoveryBoundary.HOST_LOCAL_SETTLED
            ),
            recovery_max_event_id=None
            if missing == "horizon"
            else before_intent
            if missing == "outside"
            else horizon,
            on_recovery_diagnostic=diagnostics.append,
        ), owned, capture_resources)
        before = tuple(owned.connection.iterdump())
        with pytest.raises(ConsistencyError, match="恢复收场"):
            await advance_cleanup(runtime)
        assert tuple(owned.connection.iterdump()) == before
        assert len(diagnostics) == 1
        assert (
            diagnostics[0].reason
            is {
                "boundary": RecoveryBlockedReason.UNCONFIRMED_BOUNDARY,
                "horizon": RecoveryBlockedReason.MISSING_HORIZON,
                "outside": RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON,
                "support": RecoveryBlockedReason.EVIDENCE_UNAVAILABLE,
            }[missing]
        )
        assert not driver.deletes and not driver.queries
    finally:
        owned.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_host_recovery_uses_independent_empty_local_file_evidence(
    repaired_cleanup, operation, capture_resources, monkeypatch,
):
    import json
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.bootstrap.cleanup_assembly import HostArtifacts

    host_calls = []
    for name in ("delete", "query_file"):
        actual = getattr(HostArtifacts, name)
        async def observe(host, request, *, method=name, call=actual):
            host_calls.append((method, request.ticket))
            return await call(host, request)
        monkeypatch.setattr(HostArtifacts, name, observe)

    environment, runtime, item_id, _, _, target, content, executor = repaired_cleanup
    cfg, owned, context, driver = environment
    with unknown_host_delete(target) as deletes:
        owned, _, driver, item_id, ticket, horizon = await _orphan_intent_for_prepared(
            environment, (owned, context, runtime, driver, item_id), operation,
            host_query=operation == "query", file_executor=executor,
        )
    assert len(deletes) == (1 if operation == "query" else 0)
    try:
        runtime = await _stored_runtime(session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            staging=Path(cfg.paths.staging),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW + 20,
            recovery_boundary=RecoveryBoundary.HOST_POWER_CYCLE,
            recovery_max_event_id=horizon,
        ), owned, capture_resources)
        await advance_cleanup(runtime)
        raw = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone()[0]
        assert json.loads(raw)["settlement"] == {
            "basis": "assumed",
            "evidence": {"type": "local_file_recovery", "version": 1, "data": {}},
        }
        assert target.read_bytes() == content
        assert [name for name, _ in host_calls] == (["delete"] if operation == "query" else [])
        assert not driver.deletes and not driver.queries
    finally:
        owned.connection.close()


async def test_all_saved_local_handoffs_precede_any_current_port_resolution(
    environment,
    capture_resources,
):
    from camctl.devices.bindings import DeviceConfigurationError
    from .test_cleanup_query_save_recovery import _start_cleanup

    cfg = environment[0]
    owned, context, runtime, driver, first_item = await _prepared(environment, devices=("cam-a", "cam-b"))
    second_output = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id WHERE a.device_id='cam-b'"
    ).fetchone()[0]
    _, second_item = _start_cleanup(owned, second_output, "3")
    port = _FaultOutputs("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, ("finish_cleanup_item",),
                         matches=lambda _name, args, _kwargs: args[0].item_id == second_item):
        assert (await delete_source_file(runtime, second_item)).phase == "result_rejected"
    owned.connection.close()
    fresh = context.open_connection()
    try:
        runtime = await _stored_runtime(session_cleanup_assembly(
            devices=cfg.devices,
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
        ), fresh, capture_resources)
        with pytest.raises(DeviceConfigurationError):
            await advance_cleanup(runtime)
        assert fresh.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (second_item,)
        ).fetchone() == (4,)
        assert len(driver.deletes) == 1 and not driver.queries
    finally:
        fresh.connection.close()


@pytest.mark.parametrize(
    "same_session", [False, True], ids=["new-process", "rebuilt-factory"]
)
async def test_cancel_saved_unknown_respects_available_consumption_evidence(environment, same_session, capture_resources):
    from camctl.contracts.workflow_errors import item_error_id
    from .test_cleanup_query_local_consumption import _world, _NoDeviceCalls
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel, _MatrixDriver
    cfg, owned, context, _ = environment
    base, old_driver, action_id, item_id, _ = await _world(environment, "after", None)
    assert (await delete_source_file(base, item_id)).phase == "query_unknown"
    _apply_cleanup_cancel(owned, action_id)
    await (await capture_database(owned)).close()
    owned.connection.close()
    fresh = context.open_connection()
    driver = _MatrixDriver(fresh) if same_session else _NoDeviceCalls()
    if same_session:
        driver.matrix_enabled, driver.present = True, False
    try:
        assembly = cleanup_factory if same_session else session_cleanup_assembly
        factory = assembly(devices=cfg.devices, drivers=_registry(driver),
            max_delete_attempts=3, max_query_attempts=3, occurred_at=lambda: _NOW)
        await advance_cleanup(await _stored_runtime(factory, fresh, capture_resources))
        status, error = fresh.connection.execute(
            "SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        assert (status, error) == ((4, None) if same_session else
            (6, item_error_id("cleanup_items", "delete_unconfirmed")))
        assert len(old_driver.deletes) == len(old_driver.queries) == 1
        if same_session:
            assert len(driver.queries) == 1 and not driver.deletes
        assert fresh.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key=?",
            (f"exists/{item_id}",)).fetchone() == ((2,) if same_session else (1,))
    finally:
        fresh.connection.close()


async def test_cancel_query_grant_reserves_unique_slot_before_dispatch_and_after_not_dispatched(environment, capture_resources):
    from camctl.operations.attempts import BeginDisposition
    from camctl.outputs.cleanup_results import cleanup_cancel_query_available
    from .test_cleanup_query_local_consumption import _world
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    base, driver, action_id, item_id, _ = await _world(environment, "after", None)
    owned = environment[1]
    assert (await delete_source_file(base, item_id)).phase == "query_unknown"
    _apply_cleanup_cancel(owned, action_id)
    assert cleanup_cancel_query_available(owned.connection, item_id)
    devices = dict(environment[0].devices)
    runtime = await _configured(environment, owned, driver, devices=devices)
    class CancelGrantPort(OutputsRepository):
        ticket = None
        def begin_cleanup_attempt(self, request, key, worker):
            result = super().begin_cleanup_attempt(request, key, worker)
            if request.operation == "query" and result.value.disposition is BeginDisposition.GRANTED:
                self.ticket = result.value.ticket
                assert not cleanup_cancel_query_available(worker.connection, item_id)
                assert cleanup_cancel_query_available(worker.connection, item_id, ticket=self.ticket)
                devices.clear()
            return result
    port = CancelGrantPort()
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
        await delete_source_file(runtime, item_id)
    assert port.ticket is not None
    assert not cleanup_cancel_query_available(owned.connection, item_id)
    assert not cleanup_cancel_query_available(owned.connection, item_id, ticket=port.ticket)
    import json
    raw, = owned.connection.execute("SELECT result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (port.ticket.run_id, port.ticket.attempt_id)).fetchone()
    assert json.loads(raw)["settlement"]["basis"] == "not_dispatched"
    assert len(driver.deletes) == len(driver.queries) == 1


async def test_discovery_pages_release_terminal_history_and_do_not_duplicate_calls(
    environment,
    capture_resources,
):
    from camctl.outputs.cleanup_discovery import cleanup_item_batches
    from .test_cleanup_query_save_recovery import _start_cleanup

    owned, _, runtime, driver, first_item = await _prepared(environment)
    output_id = owned.connection.execute(
        "SELECT output_id FROM cleanup_items WHERE id=?", (first_item,)
    ).fetchone()[0]
    members = [first_item]
    for request_id in range(10, 16):
        members.append(_start_cleanup(owned, output_id, str(request_id))[1])
    batches = [batch async for batch in cleanup_item_batches(await capture_database(owned), batch_size=2)]
    assert [len(batch) for batch in batches] == [2, 2, 2, 1]
    assert [item for batch in batches for item in batch] == members
    runtime = await _configured(environment, owned, driver, discovery_batch_size=2)
    await advance_cleanup(runtime)
    assert len(driver.deletes) == 1 and not driver.queries
    assert not [batch async for batch in cleanup_item_batches(await capture_database(owned), batch_size=2)]
    before = tuple(owned.connection.iterdump())
    await advance_cleanup(runtime)
    assert tuple(owned.connection.iterdump()) == before


async def test_terminal_member_with_running_call_is_diagnosed_without_reopening(
    environment,
    capture_resources,
):
    from camctl.contracts.values import ConsistencyError

    owned, _, _, item_id, ticket, _ = await _orphan_intent(environment)
    try:
        owned.connection.execute(
            "UPDATE cleanup_items SET status=4,restriction_state=4,outcome=1,final_event_id=1 WHERE id=?",
            (item_id,),
        )
        owned.connection.commit()
        before = tuple(owned.connection.iterdump())
        runtime = await _stored_runtime(session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
        ), owned, capture_resources)
        with pytest.raises(ConsistencyError, match="终态清理成员仍有未收场物理尝试"):
            await advance_cleanup(runtime)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


async def test_cancel_waits_original_pending_application_before_member_save(environment, capture_resources):
    """真实目标收场端口消费原保存拥有者；原保存责任未提交时不推进取消成员。"""
    from camctl.bootstrap.cleanup_assembly import cleanup_flow
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.cancellation.ports import TargetSettlementPort
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, _, runtime, _, item_id = await _prepared(environment)
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
        assert (await delete_source_file(runtime, item_id)).phase == "delete_budget_rejected"
    action_id, = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    _apply_cleanup_cancel(owned, action_id)
    owner: LocalWorkPort = cleanup_flow(cleanup_factory(devices={}, drivers=DriverRegistry(),
        max_delete_attempts=3, max_query_attempts=3))
    assert owner.required_settlements() == 1
    original_database = await capture_database(owned)
    async def cleanup_pending(database, target):
        assert database is original_database
        assert target == action_id
        return owner.required_settlements() > 0
    before = tuple(owned.connection.iterdump())
    settlement: TargetSettlementPort = TargetSettlement(original_database, OutputsRepository(), CancellationRepository(),
        lambda _: "unknown", lambda: _NOW, cleanup_pending=cleanup_pending)
    assert not (await settlement.settle(action_id)).complete
    assert tuple(owned.connection.iterdump()) == before


async def test_pending_query_with_original_intent_is_saved_before_other_member_ports(environment, capture_resources):
    from camctl.devices.bindings import DeviceConfigurationError
    from .test_cleanup_query_save_recovery import _start_cleanup, _QueryFinishPort
    cfg = environment[0]
    owned, context, runtime, driver, _ = await _prepared(environment, devices=("cam-a", "cam-b"))
    second_output, = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id WHERE a.device_id='cam-b'"
    ).fetchone()
    _, second_item = _start_cleanup(owned, second_output, "3")
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, False
    port = _QueryFinishPort(DbOutcomeKind.ROLLED_BACK, second_item, driver)
    with repository_port(OperationRepository, port, ("finish_attempt",),
                         matches=lambda _name, args, _kwargs: (args[0].ticket.operation == "query" and args[0].ticket.target_id == str(second_item))):
        from camctl.contracts.values import ConsistencyError
        with pytest.raises(ConsistencyError):
            await delete_source_file(runtime, second_item)
        original = port.finishes[0]
        assert original[0].ticket.target_id == str(second_item)
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            port.kind = DbOutcomeKind.COMPLETED
            factory = cleanup_factory(devices=cfg.devices, drivers=DriverRegistry(),
                max_delete_attempts=1, max_query_attempts=1, occurred_at=lambda: _NOW)
            resumed = await _stored_runtime(factory, fresh, capture_resources)
            with pytest.raises(DeviceConfigurationError):
                await advance_cleanup(resumed)
            assert port.finishes == [original, original]
            assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (second_item,)).fetchone() == (4,)
            assert fresh.connection.execute("SELECT operation_key FROM history_transactions WHERE operation_key=?",
                (str(original[1]),)).fetchone() == (str(original[1]),)
            assert len(driver.deletes) == len(driver.queries) == 1
        finally:
            fresh.connection.close()


async def test_historical_binding_failure_after_saved_absence_keeps_file_handoff(
    environment,
    capture_resources,
):
    from types import SimpleNamespace
    from camctl.devices.bindings import DeviceBinding, check_binding
    from camctl.persistence.repositories.outputs import (
        OutputsRepository,
        FailCleanupBinding,
    )
    from camctl.outputs.cleanup_flow import FinishCleanupAction
    from camctl.contracts.values import new_operation_key

    owned, context, runtime, _, item_id = await _prepared(environment)
    port = _FaultOutputs("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, ("finish_cleanup_item",),
                         matches=lambda _name, args, _kwargs: args[0].item_id == item_id):
        assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
    file_id, output_id, action_id = owned.connection.execute(
        "SELECT o.device_file_id,o.id,c.action_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id WHERE c.id=?",
        (item_id,),
    ).fetchone()
    device_id, driver_id = owned.connection.execute(
        "SELECT a.device_id,a.driver_id FROM device_files f JOIN actions a"
        " ON a.id=f.observer_action_id WHERE f.id=?", (file_id,)).fetchone()
    result = OutputsRepository().fail_cleanup_binding(
        FailCleanupBinding(
            item_id,
            file_id,
            check_binding(DeviceBinding(device_id, driver_id), SimpleNamespace(devices={})),
            (f"delete/{item_id}",),
            _NOW + 1,
            False,
        ),
        new_operation_key(),
        owned,
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    finished = OutputsRepository().finish_cleanup_action(
        FinishCleanupAction(action_id, _NOW + 2), new_operation_key(), owned
    )
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    member = owned.connection.execute(
        "SELECT * FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()
    attempts = tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
    owned.connection.close()
    fresh = context.open_connection()
    try:
        runtime = await _stored_runtime(session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW + 3,
        ), fresh, capture_resources)
        await advance_cleanup(runtime)
        assert (
            fresh.connection.execute(
                "SELECT * FROM cleanup_items WHERE id=?", (item_id,)
            ).fetchone()
            == member
        )
        assert (
            tuple(fresh.connection.execute("SELECT * FROM operation_attempts"))
            == attempts
        )
        assert fresh.connection.execute(
            "SELECT availability,cleanup_status FROM outputs WHERE id=?", (output_id,)
        ).fetchone() == (3, 4)
    finally:
        fresh.connection.close()


async def test_saved_result_wins_over_late_recovery_horizon_request(environment, capture_resources):
    from dataclasses import replace
    from camctl.operations.attempts import FinishDisposition
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.operations import OperationRepository
    class FinishedDeletePort(_FaultOutputs):
        finish = None
        def finish_cleanup_attempt(self, request, key, owned):
            result = super().finish_cleanup_attempt(request, key, owned)
            self.finish = request
            return result
    owned, _, runtime, _, item_id = await _prepared(environment)
    port = FinishedDeletePort("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, ("finish_cleanup_attempt", "finish_cleanup_item"),
                         matches=lambda _name, args, _kwargs: (args[0].ticket.target_id == str(item_id) if _name == "finish_cleanup_attempt" else args[0].item_id == item_id)):
        assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
    assert port.finish is not None
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().finish_attempt(
        replace(port.finish, recovery_max_event_id=0), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.disposition is FinishDisposition.ALREADY_ENDED
    assert tuple(owned.connection.iterdump()) == before


async def test_recovery_writer_rejects_running_intent_outside_fixed_horizon(
    environment,
    capture_resources,
):
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.models import (
        CallOutcome,
        AttemptStatus,
        EffectState,
        ErrorValue,
        Settlement,
        SettlementBasis,
        EvidenceValue,
    )
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.operations import OperationRepository
    from ..bootstrap.test_output_binding_changes import _registry

    owned, _, driver, _, ticket, _ = await _orphan_intent(environment)
    try:
        original = tuple(owned.connection.iterdump())
        actual = CallOutcome(
            status=AttemptStatus.UNKNOWN,
            effect=EffectState.UNKNOWN,
            error=ErrorValue("device_error", "delete"),
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})
            ),
        )
        finish = AttemptFinish(
            ticket,
            validate_outcome(
                ticket, actual, _registry(driver).entry("camctl-adb").evidence
            ),
            _NOW,
            recovery_max_event_id=0,
        )
        result = OperationRepository().finish_attempt(
            finish, new_operation_key(), owned
        )
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert "固定历史上界" in str(result.error)
        assert tuple(owned.connection.iterdump()) == original
    finally:
        owned.connection.close()


@pytest.mark.parametrize("boundary", [None, "host_local_settled", True])
async def test_old_recovery_requires_explicit_known_boundary(environment, boundary, capture_resources):
    from camctl.contracts.values import ConsistencyError
    from camctl.operations.recovery import RecoveryBlockedReason

    owned, _, driver, _, _, horizon = await _orphan_intent(environment)
    diagnostics = []
    try:
        runtime = await _stored_runtime(session_cleanup_assembly(
            devices={},
            drivers=_recovering_registry(driver, "delete"),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
            recovery_boundary=boundary,
            recovery_max_event_id=horizon,
            on_recovery_diagnostic=diagnostics.append,
        ), owned, capture_resources)
        before = tuple(owned.connection.iterdump())
        with pytest.raises(ConsistencyError, match="恢复收场"):
            await advance_cleanup(runtime)
        assert (
            diagnostics[0].reason
            is RecoveryBlockedReason.UNCONFIRMED_BOUNDARY
        )
        assert tuple(owned.connection.iterdump()) == before
        assert not driver.deletes and not driver.queries
    finally:
        owned.connection.close()
