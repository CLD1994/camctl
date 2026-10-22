"""分段来源显式未知项的唯一 QUERY v2 与原结果接手。"""

from dataclasses import dataclass

from camctl.capture.files import FilePresenceSave
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import item_error_id
from camctl.devices.bindings import DeviceBinding, binding_failure_details
from camctl.devices.ports import ControlRequest
from camctl.operations.attempts import (
    AttemptFinish,
    AttemptIntent,
    AttemptTarget,
    BeginDisposition,
    OperationKind,
    RunFinish,
    RunOutcome,
    StaleRunFinish,
)
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.owned_calls import owned_tool_call
from camctl.operations.recovery import (
    RecoveryBoundary,
    RecoveryBlockedReason,
    RecoveryDiagnostic,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.segment_queries import (
    ObserveQueriedOutput,
    SettleQueriedItem,
    _query_presence,
    observe_queried_output,
    settle_queried_item,
)
from camctl.persistence.repositories.segment_selections import is_segmented_source
from camctl.persistence.transaction import row_facts


@dataclass
class PendingSegmentQuery:
    item_id: int
    intent: AttemptIntent | None = None
    intent_key: str | None = None
    ticket: AttemptTicket | None = None
    dispatched: bool = False
    finish: AttemptFinish | None = None
    finish_key: str | None = None
    business: tuple | None = None


def _latest(connection, item_id):
    found = connection.execute(
        "SELECT r.id,r.status,r.attempts_used,r.retry_wait_required,t.id,t.attempt_no,"
        "t.status,t.result_event_id,t.intent_event_id FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key=?"
        " ORDER BY t.attempt_no DESC LIMIT 1",
        (f"obtain-exists/{item_id}",),
    ).fetchone()
    return found


def _target(connection, item_id):
    item = row_facts(connection, "obtain_items", item_id)
    selection = row_facts(connection, "obtain_source_selections", item["selection_id"])
    dependency = row_facts(
        connection, "action_dependencies", selection["dependency_id"]
    )
    output = row_facts(connection, "outputs", item["requested_output_id"])
    if (
        output is None
        or output["source_action_id"] != dependency["depends_on_action_id"]
    ):
        raise ConsistencyError("原显式分段取回项丢失了真实来源产物")
    file = (
        None
        if output["device_file_id"] is None
        else row_facts(connection, "device_files", output["device_file_id"])
    )
    observer = (
        None
        if file is None
        else row_facts(connection, "actions", file["observer_action_id"])
    )
    binding = (
        None
        if observer is None
        else DeviceBinding(observer["device_id"], observer["driver_id"])
    )
    return item, dependency, output, file, binding


def _save(runtime, pending, save, request):
    if pending.business is None:
        pending.business = (save, request, new_operation_key())
    original, request, key = pending.business
    if runtime.owned.connection.in_transaction:
        runtime.owned.connection.rollback()
    receipt = original(request, key, runtime.owned)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        if receipt.kind is DbOutcomeKind.ROLLED_BACK:
            pending.business = None
        raise ConsistencyError(f"原显式查询业务保存尚未可靠完成: {receipt.error}")
    pending.business = None


def _failed(runtime, pending, code, details):
    last = _latest(runtime.owned.connection, pending.item_id)
    if last is not None and last[1] in (1, 2):
        if last[6] == 1:
            raise ConsistencyError("原 QUERY 调用尚未结束，不能终结显式取回项")
        _save(
            runtime,
            pending,
            runtime.operations.finish_stale_runs,
            StaleRunFinish(
                (f"obtain-exists/{pending.item_id}",),
                RunOutcome.UNCONFIRMED,
                runtime.occurred_at(),
                ErrorValue(code, "source_read", details),
            ),
        )
    _save(
        runtime,
        pending,
        settle_queried_item,
        SettleQueriedItem(
            pending.item_id,
            runtime.occurred_at(),
            item_error_id("obtain_items", code),
            details,
        ),
    )


def _save_result(runtime, pending):
    if pending.finish is None:
        return
    if runtime.owned.connection.in_transaction:
        runtime.owned.connection.rollback()
    receipt = runtime.operations.finish_attempt(
        pending.finish, pending.finish_key, runtime.owned
    )
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"QUERY v2 原实际结果尚未可靠保存: {receipt.error}")
    pending.finish = None
    pending.intent = None
    pending.intent_key = None
    pending.ticket = None
    pending.dispatched = False


def _consume(runtime, pending, last):
    item, dependency, output, file, binding = _target(
        runtime.owned.connection, pending.item_id
    )
    owner = row_facts(runtime.owned.connection, "actions", dependency["action_id"])
    if item["status"] != 1 or owner["status"] != 2 or owner["cancel_requested"]:
        if last[1] in (1, 2):
            _save(
                runtime,
                pending,
                runtime.operations.finish_stale_runs,
                StaleRunFinish(
                    (f"obtain-exists/{pending.item_id}",),
                    RunOutcome.CANCELED,
                    runtime.occurred_at(),
                ),
            )
        return True
    unavailable = {2: "restricted", 3: "cleaned", 4: "missing"}.get(
        output["availability"]
    )
    if unavailable is not None:
        _failed(
            runtime,
            pending,
            "output_unavailable",
            {"output_id": str(output["id"]), "availability": unavailable},
        )
        return True
    present, _, _, _, _ = _query_presence(
        runtime.owned.connection, pending.item_id, last[7]
    )
    if present is None:
        if last[1] not in (1, 2):
            _failed(
                runtime,
                pending,
                "source_file_unconfirmed",
                {"output_id": str(output["id"])},
            )
            return True
        return False
    if file["presence_state"] != (2 if present else 3):
        _save(
            runtime,
            pending,
            CaptureRepository().save_file_presence,
            FilePresenceSave(file["id"], 2 if present else 3, runtime.occurred_at()),
        )
    if output["availability"] == 5:
        _save(
            runtime,
            pending,
            observe_queried_output,
            ObserveQueriedOutput(
                pending.item_id, last[7], present, runtime.occurred_at()
            ),
        )
    _save(
        runtime,
        pending,
        settle_queried_item,
        SettleQueriedItem(
            pending.item_id, runtime.occurred_at(), result_event_id=last[7]
        ),
    )
    return True


def _recover(runtime, pending, last, binding):
    reason = None
    if runtime.recovery_boundary not in (
        RecoveryBoundary.HOST_LOCAL_SETTLED,
        RecoveryBoundary.HOST_POWER_CYCLE,
    ):
        reason = RecoveryBlockedReason.UNCONFIRMED_BOUNDARY
    elif runtime.recovery_max_event_id is None:
        reason = RecoveryBlockedReason.MISSING_HORIZON
    elif last[8] > runtime.recovery_max_event_id:
        reason = RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON
    elif runtime.recovery_evidence_for is None:
        reason = RecoveryBlockedReason.MISSING_EVIDENCE_LOOKUP
    evidence = (
        None if reason is not None else runtime.recovery_evidence_for(binding, "query")
    )
    if reason is None and evidence is None:
        reason = RecoveryBlockedReason.EVIDENCE_UNAVAILABLE
    if reason is not None:
        diagnostic = RecoveryDiagnostic(reason, last[0], last[5])
        runtime.last_recovery_diagnostic = diagnostic
        callback = runtime.on_recovery_diagnostic
        if callback is not None:
            callback(diagnostic)
        raise ConsistencyError(f"原 QUERY v2 缺少合法恢复依据: {reason.value}")
    ticket = AttemptTicket(
        last[5],
        "query",
        str(pending.item_id),
        f"obtain-exists/{pending.item_id}",
        last[0],
    )
    actual = CallOutcome(
        status=AttemptStatus.UNKNOWN,
        error=ErrorValue("source_file_unconfirmed", "source_read", {}),
        effect=EffectState.UNKNOWN,
        settlement=Settlement(
            SettlementBasis.ASSUMED, EvidenceValue("adb_foreground_recovery", 1, {})
        ),
    )
    pending.finish = AttemptFinish(
        ticket,
        validate_outcome(ticket, actual, evidence),
        runtime.occurred_at(),
        retry_wait=True,
        recovery_max_event_id=runtime.recovery_max_event_id,
    )
    pending.finish_key = new_operation_key()
    _save_result(runtime, pending)
    return True


@owned_tool_call
async def _advance_one(runtime, pending):
    _save_result(runtime, pending)
    if pending.business is not None:
        _save(runtime, pending, *pending.business[:2])
    item, dependency, output, file, binding = _target(
        runtime.owned.connection, pending.item_id
    )
    owner = row_facts(runtime.owned.connection, "actions", dependency["action_id"])
    if item["status"] != 1 or owner["status"] != 2 or owner["cancel_requested"]:
        return True
    last = _latest(runtime.owned.connection, pending.item_id)
    if last is not None:
        if last[6] == 1:
            if not _recover(runtime, pending, last, binding):
                return False
            last = _latest(runtime.owned.connection, pending.item_id)
        if _consume(runtime, pending, last):
            return True
    if file is None:
        _failed(
            runtime,
            pending,
            "source_file_unconfirmed",
            {"output_id": str(output["id"])},
        )
        return True
    binding_result = (
        None if runtime.binding_check is None else runtime.binding_check(binding)
    )
    details = (
        None if binding_result is None else binding_failure_details(binding_result)
    )
    if details is not None:
        _failed(runtime, pending, "device_binding_unavailable", details)
        return True
    assembly = runtime.devices.get(binding.device_id)
    if (
        assembly is None
        or assembly.query_driver is None
        or assembly.query_config is None
    ):
        _failed(
            runtime,
            pending,
            "source_file_unconfirmed",
            {"output_id": str(output["id"])},
        )
        return True
    config = assembly.query_config
    if last is not None:
        remaining = runtime.retry_gate.remaining(
            f"obtain-exists/{pending.item_id}",
            attempts_used=last[2],
            retry_wait_required=bool(last[3]),
            max_attempts_used=config.max_attempts,
            interval_s=config.retry_interval_s,
            now_ns=runtime.monotonic_ns(),
        )
        if remaining is not None:
            return False
    if pending.intent is None:
        pending.intent = AttemptIntent(
            "query",
            owner["id"],
            OperationKind.CHECK_FILE_EXISTS,
            AttemptTarget(obtain_item_id=pending.item_id),
            None,
            config,
            runtime.occurred_at(),
        )
        pending.intent_key = new_operation_key()
    receipt = runtime.operations.begin_attempt(
        pending.intent, pending.intent_key, runtime.owned
    )
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"QUERY v2 原意图尚未可靠保存: {receipt.error}")
    if receipt.value.disposition is not BeginDisposition.GRANTED:
        _failed(
            runtime,
            pending,
            "source_file_unconfirmed",
            {"output_id": str(output["id"])},
        )
        return True
    pending.ticket = receipt.value.ticket
    if pending.dispatched:
        return False
    pending.dispatched = True
    actual = await assembly.query_driver.query_file(
        ControlRequest(
            "query",
            binding,
            {
                "obtain_item_id": str(pending.item_id),
                "file_id": str(file["id"]),
                "identity_key": file["identity_key"],
                "locator": file["locator_json"],
            },
            ticket=pending.ticket,
            timeout_s=config.timeout_s,
        )
    )
    if actual.outcome is None:
        raise ConsistencyError("QUERY v2 端口必须交回原完整 CallOutcome")
    validated = validate_outcome(pending.ticket, actual.outcome, assembly.evidence)
    presence = next(
        (
            value.data["present"]
            for value in actual.observations
            if value.type == "file_presence" and value.version == 2
        ),
        None,
    )
    error = actual.outcome.error or ErrorValue(
        "source_file_unconfirmed", "source_read", {"output_id": str(output["id"])}
    )
    exhausted = pending.ticket.attempt_id >= config.max_attempts
    end = (
        RunFinish(RunOutcome.SUCCEEDED)
        if presence is not None
        else RunFinish(RunOutcome.UNCONFIRMED, error)
        if exhausted
        else None
    )
    pending.finish = AttemptFinish(
        pending.ticket,
        validated,
        runtime.occurred_at(),
        retry_wait=end is None,
        run_finish=end,
    )
    pending.finish_key = new_operation_key()
    _save_result(runtime, pending)
    pending.intent = None
    pending.ticket = None
    pending.dispatched = False
    return _consume(
        runtime, pending, _latest(runtime.owned.connection, pending.item_id)
    )


async def advance_segment_queries(runtime, now):
    """分页发现原显式项；原返回先于新发现和新设备调用处理。"""
    for identity, pending in tuple(runtime.pending_segment_queries.items()):
        if await _advance_one(runtime, pending):
            del runtime.pending_segment_queries[identity]
    page = runtime.discovery.rows(
        runtime.owned.connection,
        "segment_queries",
        now,
        "SELECT i.id,d.depends_on_action_id FROM obtain_items i"
        " JOIN obtain_source_selections s ON s.id=i.selection_id"
        " JOIN action_dependencies d ON d.id=s.dependency_id JOIN actions a ON a.id=d.action_id"
        " WHERE i.status=1 AND i.basis=5 AND a.status=2 AND a.cancel_requested=0",
        identity="i.id",
    )
    for identity, source in page.items:
        if identity in runtime.pending_segment_queries or not is_segmented_source(
            runtime.owned.connection, source
        ):
            continue
        pending = PendingSegmentQuery(identity)
        runtime.pending_segment_queries[identity] = pending
        if await _advance_one(runtime, pending):
            del runtime.pending_segment_queries[identity]
