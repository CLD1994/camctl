"""丢失所有内存后，原清理结果与伴随责任仍由持久化事实发现。"""

from pathlib import Path

import pytest

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.devices.drivers.registry import DriverRegistry
from camctl.outputs.cleanup_flow import advance_cleanup, delete_source_file
from camctl.persistence.models import DbOutcomeKind

from .test_cleanup_delete_save_recovery import _FaultOutputs, _prepared
from ..bootstrap.test_output_binding_changes import _NOW, environment

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "removed", [False, True], ids=["binding-changed", "driver-removed"]
)
@pytest.mark.parametrize("observation", ["file_absent", "file_delete_presence"])
@pytest.mark.parametrize("with_error", [False, True])
async def test_saved_delete_absence_handoff_precedes_current_ports(
    environment, removed, observation, with_error
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
        runtime.evidence = runtime.evidence.with_contract(
            EvidenceContract(
                observation,
                1,
                "delete",
                frozenset({"cleanup_item_id", "present"}),
                identity_field="cleanup_item_id",
            )
        )
    runtime.outputs = _FaultOutputs("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
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
        await advance_cleanup(factory(fresh))
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


async def test_terminal_action_does_not_hide_open_cleanup_companion(environment):
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.cleanup_flow import FinishCleanupAction
    from camctl.persistence.repositories.outputs import OutputsRepository
    from .test_cleanup_delete_save_recovery import _FaultCompanion

    cfg = environment[0]
    owned, context, runtime, driver, item_id = await _prepared(environment)
    runtime.operations = _FaultCompanion()
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
        runtime = session_cleanup_assembly(
            devices=cfg.devices,
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            staging=Path(cfg.paths.staging),
            occurred_at=lambda: _NOW + 50_000_000,
        )(fresh)
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
        environment, await _prepared(environment), operation
    )


async def _orphan_intent_for_prepared(environment, prepared, operation):
    from camctl.outputs.cleanup_flow import _begin_cleanup_intent, CleanupQueryConsumer
    from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind

    owned, context, runtime, driver, item_id = prepared
    action_id = owned.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    request = AttemptIntent(
        operation=operation,
        action_id=action_id,
        kind=OperationKind.DELETE_FILE
        if operation == "delete"
        else OperationKind.CHECK_FILE_EXISTS,
        target=AttemptTarget(cleanup_item_id=item_id),
        query_purpose=None,
        config=runtime.delete_config if operation == "delete" else runtime.query_config,
        occurred_at=_NOW,
    )
    result = _begin_cleanup_intent(
        runtime,
        request,
        None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE,
    )
    assert result.kind is DbOutcomeKind.COMPLETED and result.value.ticket is not None
    ticket = result.value.ticket
    horizon = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[
        0
    ]
    runtime.pending_intents.clear()
    owned.connection.close()
    return context.open_connection(), context, driver, item_id, ticket, horizon


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_running_intent_is_never_replayed_without_recovery_qualification(
    environment, operation
):
    from camctl.contracts.values import ConsistencyError

    owned, _, driver, item_id, ticket, _ = await _orphan_intent(environment, operation)
    before = tuple(owned.connection.iterdump())
    runtime = session_cleanup_assembly(
        devices={},
        drivers=DriverRegistry(),
        max_delete_attempts=3,
        max_query_attempts=3,
        occurred_at=lambda: _NOW,
    )(owned)
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
    environment, operation
):
    import json
    from camctl.capture.recovery import RecoveryBoundary

    owned, _, driver, item_id, ticket, horizon = await _orphan_intent(
        environment, operation
    )
    try:
        runtime = session_cleanup_assembly(
            devices={},
            drivers=_recovering_registry(driver, operation),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW + 10,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon,
        )(owned)
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


async def test_later_reliably_not_dispatched_delete_keeps_saved_query_and_retry_wait(
    environment,
):
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import (
        AttemptIntent,
        AttemptTarget,
        AttemptFinish,
        OperationKind,
    )
    from camctl.operations.validation import validate_outcome
    from camctl.outputs.cleanup_flow import (
        _not_dispatched,
        _not_dispatched_evidence,
        _query_retry_wait_step,
    )
    from camctl.outputs.cleanup_results import saved_cleanup_query
    from .test_cleanup_query_local_consumption import _world

    base, driver, action_id, item_id, _ = await _world(environment, "after", None)
    runtime = base.for_item(item_id)
    assert (await delete_source_file(runtime, item_id)).phase == "query_unknown"
    saved_before = saved_cleanup_query(runtime.owned.connection, item_id)
    # 新 DELETE 只有可靠未派发结果，不改变原查询的文件阶段或等待。
    intent = AttemptIntent(
        operation="delete",
        action_id=action_id,
        kind=OperationKind.DELETE_FILE,
        target=AttemptTarget(cleanup_item_id=item_id),
        query_purpose=None,
        config=runtime.delete_config,
        occurred_at=_NOW,
    )
    begun = runtime.outputs.begin_cleanup_attempt(
        intent, new_operation_key(), runtime.owned
    )
    assert begun.kind is DbOutcomeKind.COMPLETED and begun.value.ticket is not None
    ticket = begun.value.ticket
    actual = _not_dispatched("delete")
    saved = runtime.outputs.finish_cleanup_attempt(
        AttemptFinish(
            ticket,
            validate_outcome(
                ticket,
                actual.outcome,
                _not_dispatched_evidence(runtime.evidence, ticket),
            ),
            _NOW,
        ),
        new_operation_key(),
        runtime.owned,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved_cleanup_query(runtime.owned.connection, item_id) == saved_before
    assert _query_retry_wait_step(runtime, item_id).phase == "query_retry_wait"
    assert len(driver.deletes) == 1 and len(driver.queries) == 1


async def test_terminal_flow_running_attempt_is_work_and_cancellation_cannot_finish(
    environment,
):
    from camctl.bootstrap.application import query_work_facts
    from camctl.cancellation.settlement import TargetSettlement
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
        assert query_work_facts(owned.connection).required_settlements > 0
        settlement = TargetSettlement(
            owned,
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
    environment, operation, missing
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
        runtime = session_cleanup_assembly(
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
        )(owned)
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
        assert runtime.last_recovery_diagnostic == diagnostics[0]
        assert not driver.deletes and not driver.queries
    finally:
        owned.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_host_recovery_uses_independent_empty_local_file_evidence(
    environment, operation
):
    import json
    from camctl.capture.recovery import RecoveryBoundary
    from ..bootstrap.test_cleanup_flow import _seed_repaired_artifact

    # 来源在授票据前可靠固定为主机；恢复不读取当前相机能力。
    cfg = environment[0]
    owned, _, runtime, _, item_id = await _prepared(environment)
    output_id = owned.connection.execute(
        "SELECT output_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    _seed_repaired_artifact(Path(cfg.paths.state_db), cfg, output_id)
    owned, _, driver, item_id, ticket, horizon = await _orphan_intent_for_prepared(
        environment,
        (owned, environment[2], runtime, environment[3], item_id),
        operation,
    )
    try:
        runtime = session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            staging=Path(cfg.paths.staging),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW + 20,
            recovery_boundary=RecoveryBoundary.HOST_POWER_CYCLE,
            recovery_max_event_id=horizon,
        )(owned)
        runtime.allow_new_calls = False
        await advance_cleanup(runtime)
        raw = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone()[0]
        assert json.loads(raw)["settlement"] == {
            "basis": "assumed",
            "evidence": {"type": "local_file_recovery", "version": 1, "data": {}},
        }
        assert (
            Path(cfg.paths.staging) / "derived/900.jpg"
        ).read_bytes() == b"repaired-artifact"
    finally:
        owned.connection.close()


async def test_all_saved_local_handoffs_precede_any_current_port_resolution(
    environment,
):
    from camctl.devices.bindings import DeviceConfigurationError
    from .test_cleanup_query_save_recovery import _start_cleanup

    cfg = environment[0]
    owned, context, runtime, driver, first_item = await _prepared(environment)
    second_output = owned.connection.execute(
        "SELECT id FROM outputs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    _, second_item = _start_cleanup(owned, second_output, "3")
    runtime.outputs = _FaultOutputs("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
    assert (await delete_source_file(runtime, second_item)).phase == "result_rejected"
    owned.connection.close()
    fresh = context.open_connection()
    try:
        runtime = session_cleanup_assembly(
            devices=cfg.devices,
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
        )(fresh)
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
async def test_cancel_saved_unknown_respects_available_consumption_evidence(
    environment, same_session
):
    from camctl.contracts.workflow_errors import item_error_id
    from .test_cleanup_query_local_consumption import _world, _NoDeviceCalls
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel, _MatrixDriver
    from ..bootstrap.test_output_binding_changes import _registry

    cfg = environment[0]
    base, old_driver, action_id, item_id, _ = await _world(environment, "after", None)
    assert (
        await delete_source_file(base.for_item(item_id), item_id)
    ).phase == "query_unknown"
    consumed = base.consumed_queries
    _apply_cleanup_cancel(base.owned, action_id)
    base.owned.connection.close()
    fresh = environment[2].open_connection()
    driver = _MatrixDriver(fresh) if same_session else _NoDeviceCalls()
    if same_session:
        driver.matrix_enabled, driver.present = True, False
    try:
        factory = session_cleanup_assembly(
            devices=cfg.devices,
            drivers=_registry(driver),
            max_delete_attempts=3,
            max_query_attempts=3,
            occurred_at=lambda: _NOW,
            consumed_queries=consumed if same_session else {},
        )
        await advance_cleanup(factory(fresh))
        status, error = fresh.connection.execute(
            "SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)
        ).fetchone()
        assert (status, error) == (
            (4, None)
            if same_session
            else (6, item_error_id("cleanup_items", "delete_unconfirmed"))
        )
        assert len(old_driver.deletes) == len(old_driver.queries) == 1
        if same_session:
            assert len(driver.queries) == 1 and not driver.deletes
        assert fresh.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key=?",
            (f"exists/{item_id}",),
        ).fetchone() == ((2,) if same_session else (1,))
    finally:
        fresh.connection.close()


async def test_cancel_query_grant_reserves_unique_slot_before_dispatch_and_after_not_dispatched(
    environment,
):
    from camctl.outputs.cleanup_flow import (
        _begin_query_attempt,
        CleanupQueryConsumer,
        _not_dispatched,
        _not_dispatched_evidence,
    )
    from camctl.outputs.cleanup_results import cleanup_cancel_query_available
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.validation import validate_outcome
    from camctl.contracts.values import new_operation_key
    from .test_cleanup_query_local_consumption import _world
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel

    base, _, action_id, item_id, _ = await _world(environment, "after", None)
    runtime = base.for_item(item_id)
    assert (await delete_source_file(runtime, item_id)).phase == "query_unknown"
    _apply_cleanup_cancel(runtime.owned, action_id)
    assert cleanup_cancel_query_available(runtime.owned.connection, item_id)
    begun = _begin_query_attempt(
        runtime, item_id, action_id, CleanupQueryConsumer.CANCEL
    )
    assert begun.kind is DbOutcomeKind.COMPLETED and begun.value.ticket is not None
    ticket = begun.value.ticket
    assert not cleanup_cancel_query_available(runtime.owned.connection, item_id)
    assert cleanup_cancel_query_available(
        runtime.owned.connection, item_id, ticket=ticket
    )
    actual = _not_dispatched("query")
    saved = runtime.operations.finish_attempt(
        AttemptFinish(
            ticket,
            validate_outcome(
                ticket,
                actual.outcome,
                _not_dispatched_evidence(runtime.evidence, ticket),
            ),
            _NOW,
        ),
        new_operation_key(),
        runtime.owned,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert not cleanup_cancel_query_available(runtime.owned.connection, item_id)
    assert not cleanup_cancel_query_available(
        runtime.owned.connection, item_id, ticket=ticket
    )


async def test_discovery_pages_release_terminal_history_and_do_not_duplicate_calls(
    environment,
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
    batches = list(cleanup_item_batches(owned.connection, batch_size=2))
    assert [len(batch) for batch in batches] == [2, 2, 2, 1]
    assert [item for batch in batches for item in batch] == members
    runtime.discovery_batch_size = 2
    await advance_cleanup(runtime)
    assert len(driver.deletes) == 1 and not driver.queries
    assert not list(cleanup_item_batches(owned.connection, batch_size=2))
    before = tuple(owned.connection.iterdump())
    await advance_cleanup(runtime)
    assert tuple(owned.connection.iterdump()) == before


async def test_terminal_member_with_running_call_is_diagnosed_without_reopening(
    environment,
):
    from camctl.contracts.values import ConsistencyError

    owned, _, _, item_id, ticket, _ = await _orphan_intent(environment)
    try:
        owned.connection.execute(
            "UPDATE cleanup_items SET status=4,restriction_state=4,outcome=1,final_event_id=1 WHERE id=?",
            (item_id,),
        )
        before = tuple(owned.connection.iterdump())
        runtime = session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
        )(owned)
        with pytest.raises(ConsistencyError, match="终态清理成员仍有未收场物理尝试"):
            await advance_cleanup(runtime)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


async def test_cancel_waits_original_pending_application_before_member_save(
    environment,
):
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from camctl.persistence.repositories.outputs import OutputsRepository
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel

    owned, _, runtime, _, item_id = await _prepared(environment)
    runtime.outputs = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK)
    assert (
        await delete_source_file(runtime, item_id)
    ).phase == "delete_budget_rejected"
    action_id = owned.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()[0]
    _apply_cleanup_cancel(owned, action_id)
    before = tuple(owned.connection.iterdump())
    settlement = TargetSettlement(
        owned,
        OutputsRepository(),
        CancellationRepository(),
        lambda _: "unknown",
        lambda: _NOW,
        cleanup_pending=lambda *_: True,
    )
    assert not (await settlement.settle(action_id)).complete
    assert tuple(owned.connection.iterdump()) == before


async def test_pending_query_with_original_intent_is_saved_before_other_member_ports(
    environment,
):
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.bindings import DeviceConfigurationError
    from .test_cleanup_query_save_recovery import _start_cleanup, _QueryFinishPort

    cfg = environment[0]
    owned, context, runtime, driver, _ = await _prepared(environment)
    second_output = owned.connection.execute(
        "SELECT id FROM outputs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    _, second_item = _start_cleanup(owned, second_output, "3")
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, False
    runtime.operations = _QueryFinishPort(
        DbOutcomeKind.ROLLED_BACK, second_item, driver, runtime.retry_gate
    )
    with pytest.raises(ConsistencyError):
        await delete_source_file(runtime, second_item)
    assert second_item in runtime.pending_intents
    assert len(runtime.pending_queries) == 1
    original = next(iter(runtime.pending_queries.values()))
    owned.connection.close()
    fresh = context.open_connection()
    try:
        resumed = session_cleanup_assembly(
            devices=cfg.devices,
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
            pending_intents=runtime.pending_intents,
            pending_queries=runtime.pending_queries,
        )(fresh)
        with pytest.raises(DeviceConfigurationError):
            await advance_cleanup(resumed)
        assert fresh.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (second_item,)
        ).fetchone() == (4,)
        assert fresh.connection.execute(
            "SELECT operation_key FROM history_transactions WHERE operation_key=?",
            (str(original.key),),
        ).fetchone() == (str(original.key),)
        assert len(driver.deletes) == len(driver.queries) == 1
    finally:
        fresh.connection.close()


async def test_historical_binding_failure_after_saved_absence_keeps_file_handoff(
    environment,
):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    from camctl.persistence.repositories.outputs import (
        OutputsRepository,
        FailCleanupBinding,
    )
    from camctl.outputs.cleanup_flow import FinishCleanupAction
    from camctl.contracts.values import new_operation_key

    owned, context, runtime, _, item_id = await _prepared(environment)
    runtime.outputs = _FaultOutputs("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
    assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
    file_id, output_id, action_id = owned.connection.execute(
        "SELECT o.device_file_id,o.id,c.action_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id WHERE c.id=?",
        (item_id,),
    ).fetchone()
    result = OutputsRepository().fail_cleanup_binding(
        FailCleanupBinding(
            item_id,
            file_id,
            check_binding(runtime.binding_of(item_id), SimpleNamespace(devices={})),
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
        runtime = session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW + 3,
        )(fresh)
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


async def test_saved_result_wins_over_late_recovery_horizon_request(environment):
    from dataclasses import replace
    from camctl.operations.attempts import FinishDisposition
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.operations import OperationRepository

    owned, _, runtime, _, item_id = await _prepared(environment)
    runtime.outputs = _FaultOutputs("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
    assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
    pending = runtime.pending_deletes[item_id]
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().finish_attempt(
        replace(pending.finish, recovery_max_event_id=0), new_operation_key(), owned
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.disposition is FinishDisposition.ALREADY_ENDED
    assert tuple(owned.connection.iterdump()) == before


async def test_recovery_writer_rejects_running_intent_outside_fixed_horizon(
    environment,
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
async def test_old_recovery_requires_explicit_known_boundary(environment, boundary):
    from camctl.contracts.values import ConsistencyError
    from camctl.operations.recovery import RecoveryBlockedReason

    owned, _, driver, _, _, horizon = await _orphan_intent(environment)
    try:
        runtime = session_cleanup_assembly(
            devices={},
            drivers=_recovering_registry(driver, "delete"),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
            recovery_boundary=boundary,
            recovery_max_event_id=horizon,
        )(owned)
        before = tuple(owned.connection.iterdump())
        with pytest.raises(ConsistencyError, match="恢复收场"):
            await advance_cleanup(runtime)
        assert (
            runtime.last_recovery_diagnostic.reason
            is RecoveryBlockedReason.UNCONFIRMED_BOUNDARY
        )
        assert tuple(owned.connection.iterdump()) == before
        assert not driver.deletes and not driver.queries
    finally:
        owned.connection.close()
