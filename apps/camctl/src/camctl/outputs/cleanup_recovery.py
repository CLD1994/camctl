"""旧清理调用沿原票据恢复；当前会话拥有者和原保存结果优先。"""

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.recovery import (
    RecoveryBoundary,
    RecoveryBlockedReason,
    RecoveryDiagnostic,
    RecoveryFacts,
    recover_attempt,
)
from camctl.outputs.cleanup_results import running_cleanup_attempt


def recover_cleanup_call(runtime, item_id):
    """恢复只形成原尝试的 UNKNOWN 结果，保存和消费沿原统一路径。"""
    if (
        item_id in runtime.pending_intents
        or item_id in runtime.pending_deletes
        or any(value.item_id == item_id for value in runtime.pending_queries.values())
    ):
        return
    running = running_cleanup_attempt(runtime.owned.connection, item_id)
    if running is None:
        return
    ticket, intent_event = running
    reason = None
    if runtime.recovery_boundary not in (
        RecoveryBoundary.HOST_LOCAL_SETTLED,
        RecoveryBoundary.HOST_POWER_CYCLE,
    ):
        reason = RecoveryBlockedReason.UNCONFIRMED_BOUNDARY
    elif runtime.recovery_max_event_id is None:
        reason = RecoveryBlockedReason.MISSING_HORIZON
    elif intent_event > runtime.recovery_max_event_id:
        reason = RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON
    elif runtime.recovery_evidence_for is None:
        reason = RecoveryBlockedReason.MISSING_EVIDENCE_LOOKUP
    evidence = None
    if reason is None:
        evidence = runtime.recovery_evidence_for(
            runtime.binding_of(item_id), ticket.operation
        )
        if evidence is None:
            reason = RecoveryBlockedReason.EVIDENCE_UNAVAILABLE
    if reason is not None:
        diagnostic = RecoveryDiagnostic(reason, ticket.run_id, ticket.attempt_id)
        runtime.last_recovery_diagnostic = diagnostic
        if runtime.on_recovery_diagnostic is not None:
            runtime.on_recovery_diagnostic(diagnostic)
        raise ConsistencyError(f"原清理调用缺少合法恢复收场依据: {reason.value}")
    decision = recover_attempt(RecoveryFacts(True, False, True))
    if not decision.save_basis or decision.redispatch:
        raise ConsistencyError("原清理恢复判定不能结束原调用")
    kind = (
        "local_file_recovery"
        if runtime.binding_of(item_id) is None
        else "adb_foreground_recovery"
    )
    actual = DeviceCallResult.from_outcome(
        CallOutcome(
            status=AttemptStatus.UNKNOWN,
            effect=EffectState.UNKNOWN,
            error=ErrorValue("result_not_saved", "recovery"),
            settlement=Settlement(SettlementBasis.ASSUMED, EvidenceValue(kind, 1, {})),
        )
    )
    from camctl.outputs.cleanup_flow import (
        PendingCleanupDelete,
        PendingCleanupQuery,
        CleanupQueryConsumer,
    )

    if ticket.operation == "delete":
        pending = PendingCleanupDelete(
            runtime.owned.metadata.instance_id,
            item_id,
            ticket,
            actual,
            evidence,
            None,
            None,
            new_operation_key(),
            recovery_max_event_id=runtime.recovery_max_event_id,
        )
        runtime.pending_deletes[item_id] = pending
    else:
        from camctl.outputs.cleanup_flow import (
            _cancel_requested,
            _item_action,
            _latest_possible_delete,
        )

        connection = runtime.owned.connection
        if _cancel_requested(connection, _item_action(connection, item_id)):
            consumer = CleanupQueryConsumer.CANCEL
        else:
            output_id = connection.execute(
                "SELECT output_id FROM cleanup_items WHERE id=?", (item_id,)
            ).fetchone()[0]
            consumer = (
                CleanupQueryConsumer.AFTER_DELETE
                if _latest_possible_delete(connection, output_id)
                else CleanupQueryConsumer.BEFORE_DELETE
            )
        runtime.pending_queries[(ticket.run_id, ticket.attempt_id)] = (
            PendingCleanupQuery(
                runtime.owned.metadata.instance_id,
                item_id,
                consumer,
                ticket,
                actual,
                evidence,
                None,
                None,
                new_operation_key(),
                recovery_max_event_id=runtime.recovery_max_event_id,
            )
        )
    runtime.last_recovery_diagnostic = None
