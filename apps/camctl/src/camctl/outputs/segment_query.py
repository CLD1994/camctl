"""分段来源显式未知项的唯一 QUERY v2 与原结果接手。"""

from contextlib import closing

from camctl.persistence.models import db_failure
from dataclasses import dataclass

from camctl.capture.files import FilePresenceSave
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import item_error_id
from camctl.devices.bindings import DeviceBinding, binding_failure_details
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
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
from camctl.persistence.business import settle_required
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
    with closing(connection.execute(
        "SELECT r.id,r.status,r.attempts_used,r.retry_wait_required,t.id,t.attempt_no,"
        "t.status,t.result_event_id,t.intent_event_id FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key=?"
        " ORDER BY t.attempt_no DESC LIMIT 1",
        (f"obtain-exists/{item_id}",),
    )) as cursor:
        return cursor.fetchone()


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


async def _read_facts(runtime, pending, *, ticket=None):
    item_id = pending.item_id
    intent = pending.intent

    def read(owned):
        connection = owned.connection
        item, dependency, output, file, binding = _target(connection, item_id)
        owner = row_facts(connection, "actions", dependency["action_id"])
        last = _latest(connection, item_id)
        if ticket is not None and (
            ticket.operation != "query"
            or ticket.target_id != str(item_id)
            or ticket.responsibility_key != f"obtain-exists/{item_id}"
            or intent is None
            or intent.action_id != owner["id"]
            or last is None
            or (ticket.run_id, ticket.attempt_id) != (last[0], last[5])
        ):
            raise ConsistencyError("原 QUERY 未派发票据与当前取回责任不符")
        present = None
        if (item["status"] == 1 and owner["status"] == 2 and not owner["cancel_requested"]
                and output["availability"] not in (2, 3, 4)
                and last is not None and last[6] != 1):
            present, _, _, _, _ = _query_presence(connection, pending.item_id, last[7])
        return item, dependency, output, file, binding, owner, last, present

    return await runtime.database.read(read, description="显式分段查询目标事实读取")


async def _prepare_dispatch(runtime, pending):
    """按原票据复核派发资格；本地未派发结果沿原尝试可靠保存。"""
    facts = await _read_facts(runtime, pending, ticket=pending.ticket)
    item, _dependency, _output, _file, _binding, owner, last, _present = facts
    if last[6] != 1:
        return None
    if (item["status"] == 1 and owner["status"] == 2
            and not owner["cancel_requested"] and last[1] in (1, 2)):
        return facts
    outcome = CallOutcome(
        status=AttemptStatus.FAILED,
        effect=EffectState.NO_EFFECT,
        error=ErrorValue("canceled_before_dispatch" if owner["cancel_requested"]
                         else "query_no_longer_required", "source_read"),
        settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
            EvidenceValue("dispatch_prevented", 1, {})),
    )
    evidence = EvidenceRegistry((
        EvidenceContract("dispatch_prevented", 1, "query", frozenset()),
    ))
    pending.finish = AttemptFinish(
        pending.ticket, validate_outcome(pending.ticket, outcome, evidence),
        runtime.occurred_at(), run_finish=RunFinish(RunOutcome.CANCELED))
    pending.finish_key = new_operation_key()
    await _save_result(runtime, pending)
    return None


async def _save(runtime, pending, save, request):
    if pending.business is None:
        pending.business = (save, request, new_operation_key())
    original, request, key = pending.business

    async def save_and_apply():
        receipt = await runtime.database.write(
            lambda owned: original(request, key, owned), key=key,
            description="显式分段查询业务保存")
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            if receipt.kind is DbOutcomeKind.ROLLED_BACK:
                pending.business = None
            raise db_failure(ConsistencyError, f"原显式查询业务保存尚未可靠完成: {receipt.error}", receipt)
        pending.business = None

    await settle_required(save_and_apply())


async def _failed(runtime, pending, code, details):
    last = await runtime.database.read(
        lambda owned: _latest(owned.connection, pending.item_id),
        description="显式分段查询最近尝试读取")
    if last is not None and last[1] in (1, 2):
        if last[6] == 1:
            raise ConsistencyError("原 QUERY 调用尚未结束，不能终结显式取回项")
        await _save(
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
    await _save(
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


async def _save_result(runtime, pending):
    if pending.finish is None:
        return
    request, key = pending.finish, pending.finish_key

    async def save_and_apply():
        receipt = await runtime.database.write(
            lambda owned: runtime.operations.finish_attempt(request, key, owned),
            key=key, description="QUERY v2原实际结果保存")
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"QUERY v2 原实际结果尚未可靠保存: {receipt.error}", receipt)
        pending.finish = None
        pending.finish_key = None
        pending.intent = None
        pending.intent_key = None
        pending.ticket = None
        pending.dispatched = False

    await settle_required(save_and_apply())


async def _begin(runtime, pending):
    request, key = pending.intent, pending.intent_key

    async def save_and_apply():
        receipt = await runtime.database.write(
            lambda owned: runtime.operations.begin_attempt(request, key, owned),
            key=key, description="QUERY v2原意图保存")
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"QUERY v2 原意图尚未可靠保存: {receipt.error}", receipt)
        if receipt.value.disposition is BeginDisposition.GRANTED:
            pending.ticket = receipt.value.ticket
        return receipt.value

    return await settle_required(save_and_apply())


async def _consume(runtime, pending, facts):
    item, dependency, output, file, binding, owner, last, present = facts
    if item["status"] != 1 or owner["status"] != 2 or owner["cancel_requested"]:
        if last[1] in (1, 2):
            await _save(
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
        await _failed(
            runtime,
            pending,
            "output_unavailable",
            {"output_id": str(output["id"]), "availability": unavailable},
        )
        return True
    if present is None:
        if last[1] not in (1, 2):
            await _failed(
                runtime,
                pending,
                "source_file_unconfirmed",
                {"output_id": str(output["id"])},
            )
            return True
        return False
    if file["presence_state"] != (2 if present else 3):
        await _save(
            runtime,
            pending,
            CaptureRepository().save_file_presence,
            FilePresenceSave(file["id"], 2 if present else 3, runtime.occurred_at()),
        )
    if output["availability"] == 5:
        await _save(
            runtime,
            pending,
            observe_queried_output,
            ObserveQueriedOutput(
                pending.item_id, last[7], present, runtime.occurred_at()
            ),
        )
    await _save(
        runtime,
        pending,
        settle_queried_item,
        SettleQueriedItem(
            pending.item_id, runtime.occurred_at(), result_event_id=last[7]
        ),
    )
    return True


async def _recover(runtime, pending, last, binding):
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
    await _save_result(runtime, pending)
    return True


@owned_tool_call
async def _advance_one(runtime, pending):
    await _save_result(runtime, pending)
    if pending.business is not None:
        await _save(runtime, pending, *pending.business[:2])
    facts = await _read_facts(runtime, pending)
    item, dependency, output, file, binding, owner, last, _present = facts
    local_unsent = pending.intent is not None and not pending.dispatched
    if ((item["status"] != 1 or owner["status"] != 2 or owner["cancel_requested"])
            and not (local_unsent and last is not None and last[6] == 1)):
        return True
    owns_unsent = False
    if last is not None:
        if last[6] == 1:
            if pending.intent is not None and not pending.dispatched:
                # 同一流程仍保留原意图，能证明尚未进入实际调用。
                if pending.ticket is None:
                    begun = await _begin(runtime, pending)
                    if begun.disposition is not BeginDisposition.GRANTED:
                        raise ConsistencyError("原 QUERY 未派发意图无法核实其本地票据")
                owns_unsent = (pending.ticket.run_id == last[0]
                               and pending.ticket.attempt_id == last[5])
                if not owns_unsent:
                    raise ConsistencyError("原 QUERY 本地未派发票据与持久化尝试不符")
                facts = await _prepare_dispatch(runtime, pending)
                if facts is None:
                    return True
                item, dependency, output, file, binding, owner, last, _present = facts
            else:
                if not await _recover(runtime, pending, last, binding):
                    return False
                facts = await _read_facts(runtime, pending)
                item, dependency, output, file, binding, owner, last, _present = facts
        if not owns_unsent and await _consume(runtime, pending, facts):
            return True
    if file is None:
        await _failed(
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
        await _failed(runtime, pending, "device_binding_unavailable", details)
        return True
    assembly = runtime.devices.get(binding.device_id)
    if (
        assembly is None
        or assembly.query_driver is None
        or assembly.query_config is None
    ):
        await _failed(
            runtime,
            pending,
            "source_file_unconfirmed",
            {"output_id": str(output["id"])},
        )
        return True
    config = assembly.query_config
    if last is not None and not owns_unsent:
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
    if not owns_unsent:
        begun = await _begin(runtime, pending)
        if begun.disposition is not BeginDisposition.GRANTED:
            await _failed(
                runtime,
                pending,
                "source_file_unconfirmed",
                {"output_id": str(output["id"])},
            )
            return True
        facts = await _prepare_dispatch(runtime, pending)
        if facts is None:
            return True
        item, dependency, output, file, binding, owner, last, _present = facts
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
    await _save_result(runtime, pending)
    pending.intent = None
    pending.ticket = None
    pending.dispatched = False
    facts = await _read_facts(runtime, pending)
    return await _consume(runtime, pending, facts)


async def advance_segment_queries(runtime, now):
    """分页发现原显式项；原返回先于新发现和新设备调用处理。"""
    for identity, pending in tuple(runtime.pending_segment_queries.items()):
        if await _advance_one(runtime, pending):
            del runtime.pending_segment_queries[identity]
    page = await runtime.discovery.rows(
        runtime.database,
        "segment_queries",
        now,
        "SELECT i.id,d.depends_on_action_id FROM obtain_items i"
        " JOIN obtain_source_selections s ON s.id=i.selection_id"
        " JOIN action_dependencies d ON d.id=s.dependency_id JOIN actions a ON a.id=d.action_id"
        " WHERE i.status=1 AND i.basis=5 AND a.status=2 AND a.cancel_requested=0",
        identity="i.id",
    )
    candidates = tuple((identity, source) for identity, source in page.items
                       if identity not in runtime.pending_segment_queries)
    if not candidates:
        return
    segmented = await runtime.database.read(
        lambda owned: tuple(identity for identity, source in candidates
                            if is_segmented_source(owned.connection, source)),
        description="显式分段查询来源资格读取")
    for identity in segmented:
        pending = PendingSegmentQuery(identity)
        runtime.pending_segment_queries[identity] = pending
        if await _advance_one(runtime, pending):
            del runtime.pending_segment_queries[identity]
