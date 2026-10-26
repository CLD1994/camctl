"""后续拍摄按需结束旧活动的目录范围责任，不重开原拍摄结果。"""

from contextlib import closing

from camctl.capture.result_scans import PendingResultScan, SavedResultEntries
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import EvidenceContract
from camctl.operations.attempts import (
    AttemptIntent, AttemptTarget, BeginDisposition, OperationKind, RunOutcome, StaleRunFinish,
)
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.owned_calls import owned_tool_call
from camctl.logging_runtime.diagnostics import DiagnosticState, record_error
from camctl.persistence.models import DbOutcomeKind, db_failure


_NOT_DISPATCHED = EvidenceContract("output_scope_not_dispatched", 1, "result", frozenset())


def _blocked(runtime, trigger_id, activity_id, reason):
    state = runtime.preparation_cache.scope_diagnostics.setdefault(activity_id, DiagnosticState())
    record_error(ErrorValue("capture_result_unconfirmed", "execution",
        {"activity_id": str(activity_id), "reason": reason}),
        stage="capture.output_scope", context={"trigger_action_id": trigger_id}, state=state)


async def _trigger_ready(runtime, trigger_id):
    action = await runtime.action(trigger_id)
    return (action["status"] in (1, 2) and not action["cancel_requested"]
        and action["scheduled_at"] - runtime.prepare_lead_s * 1_000_000 <= runtime.wall_us()
        <= action["scheduled_at"] + action["max_delay_ms"] * 1000)


async def _write(runtime, callback, key, description):
    receipt = await runtime.database.write(callback, key=key, description=description)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"{description}未可靠完成: {receipt.error}", receipt)
    return receipt.value


async def _finish_incomplete(runtime, pending):
    from camctl.capture import handlers as common

    if pending.last_ref is not None:
        runtime.hold_call_result(pending.ticket, pending.last_outcome.outcome,
            occurred_at=pending.occurred_at, returned_ns=pending.returned_ns,
            result_listing=SavedResultEntries(runtime.capture, runtime.database, pending.last_ref))
        await common._finish_listing_result(runtime,
            await common._held_listing(runtime, pending.ticket), retry_wait=True)
    else:
        runtime.evidence = runtime.evidence.with_contract(_NOT_DISPATCHED)
        outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
            error=ErrorValue("scope_trigger_ended", "dispatch"),
            settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                EvidenceValue(_NOT_DISPATCHED.type, _NOT_DISPATCHED.version)))
        await runtime.finish(pending.ticket, outcome, retry_wait=True)
        pending.close()
        runtime.pending_result_scans.pop((pending.ticket.run_id, pending.ticket.attempt_id), None)


@owned_tool_call
async def advance_ended_scope(runtime, trigger):
    """仅在有效准备时段核实已结束的旧范围；返回是否可以继续准备。"""
    from camctl.capture import handlers as common

    if not await _trigger_ready(runtime, trigger["id"]):
        return False

    def candidate(owned):
        with closing(owned.connection.execute(
            "SELECT d.id,d.action_id FROM device_activities d JOIN actions a ON a.id=d.action_id"
            " WHERE a.device_id=? AND a.id<>? AND a.status IN(3,4,5,6)"
            " AND d.activity_state=3 AND d.occupancy_state=1 AND d.ownership_mode=2"
            " AND d.baseline_state=3 AND d.result_set_state<>3"
            " AND d.output_set_finalized_event_id IS NULL ORDER BY d.id LIMIT 1",
            (trigger["device_id"], trigger["id"]))) as cursor:
            return cursor.fetchone()

    found = await runtime.database.read(candidate, description="读取已结束相机的范围责任")
    if found is None:
        return True
    activity_id, owner_id = found
    if runtime.results is None:
        return False
    from camctl.capture.residual import _original_binding_failure
    if await _original_binding_failure(runtime, await runtime.action(owner_id)) is not None:
        return False
    responsibility = f"scope/{activity_id}"
    await runtime.resume_start_results(owner_id)

    def read_run(owned):
        with closing(owned.connection.execute(
                "SELECT id,status FROM operation_runs WHERE responsibility_key=?",
                (responsibility,))) as cursor:
            return cursor.fetchone()

    run = await runtime.database.read(read_run, description="读取原范围核实阶段")
    if run is not None and run[1] not in (1, 2):
        _blocked(runtime, trigger["id"], activity_id, "scope_attempts_ended")
        return False
    ticket = await common._original_ticket(runtime, responsibility, "result")
    last_attempt = await runtime.last_attempt(responsibility)
    listing = None
    pending = None
    if ticket is not None and last_attempt is not None:
        if last_attempt[0] == 1:
            listing = await common._held_listing(runtime, ticket)
            pending = runtime.pending_result_scans.get((ticket.run_id, ticket.attempt_id))
            if listing is None and pending is None:
                evidence = await runtime._attempt_recovery_evidence(ticket)
                if evidence is None:
                    return False
                last = await runtime.database.read(lambda owned: runtime.capture.read_last_result_page(ticket, owned))
                if last is not None and common._result_page_returned(last):
                    common._hold_recovered_result_page(runtime, last, evidence)
                    listing = await common._held_listing(runtime, ticket)
                else:
                    outcome = common._recovered_call_outcome(evidence.type)
                    await runtime.finish(ticket, outcome, retry_wait=True)
                    return False
        else:
            def waiting(owned):
                return owned.connection.execute(
                    "SELECT retry_wait_required FROM operation_runs WHERE id=?",
                    (ticket.run_id,)).fetchone()[0]
            if not await runtime.database.read(waiting):
                listing = await common._saved_result_listing(runtime, owner_id,
                    responsibility=responsibility)
    if listing is None and pending is None:
        if await runtime.retry_wait_remaining(responsibility,
                runtime.check_config.retry_interval_s, maximum=runtime.check_config.max_attempts) is not None:
            return False
        if not await _trigger_ready(runtime, trigger["id"]):
            return False
        intent = AttemptIntent("result", owner_id, OperationKind.RESOLVE_OUTPUT_SCOPE,
            AttemptTarget(activity_id=activity_id), None, runtime.check_config, runtime.wall_us())
        key = new_operation_key()
        begun = await _write(runtime, lambda owned: runtime.operations.begin_attempt(intent, key, owned),
            key, "保存独立范围核实意图")
        if begun.disposition is not BeginDisposition.GRANTED:
            if begun.reason == "budget_exhausted":
                key = new_operation_key()
                finish = StaleRunFinish((responsibility,), RunOutcome.FAILED, runtime.wall_us(),
                    ErrorValue("capture_result_unconfirmed", "execution",
                        {"activity_id": str(activity_id), "reason": "output_scope_unconfirmed"}))
                await _write(runtime, lambda owned: runtime.operations.finish_stale_runs(finish, key, owned),
                    key, "结束有限范围核实责任")
                _blocked(runtime, trigger["id"], activity_id, "scope_attempts_exhausted")
            return False
        ticket = begun.ticket
        source = await common._result_source(runtime, ticket)
        scope = await runtime.database.read(lambda owned: common.row_facts(
            owned.connection, "device_activities", activity_id)["output_scope_json"])
        action = await runtime.action(owner_id)
        pending = PendingResultScan(ticket, scope, runtime.check_config.timeout_s,
            completion_context=source.context,
            video_result_mode=action["execution_spec_json"].get("video_result_mode"), scope_only=True)
        runtime.pending_result_scans[(ticket.run_id, ticket.attempt_id)] = pending
    if listing is None:
        listing = await common._continue_result_scan(runtime, pending,
            read_allowed=lambda: _trigger_ready(runtime, trigger["id"]))
        if listing.phase is common.ListingPhase.IN_FLIGHT:
            if not await _trigger_ready(runtime, trigger["id"]):
                await _finish_incomplete(runtime, pending)
            return False
    await common._register_listing(runtime, owner_id, listing)
    if listing.outcome is not None and listing.outcome.error is not None:
        await common._finish_listing_result(runtime, listing, retry_wait=True)
        return False
    if not common._listing_finalized(listing):
        await common._finish_listing_result(runtime, listing, retry_wait=True)
        return False
    await common._finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED,
        finalize_output_set=True)
    if listing.already_saved:
        key = new_operation_key()
        finish = StaleRunFinish((responsibility,), RunOutcome.SUCCEEDED, runtime.wall_us())
        await _write(runtime, lambda owned: runtime.operations.finish_stale_runs(finish, key, owned),
            key, "结束已保存的范围核实责任")
    from camctl.capture.segment_completion import release_segment_occupancy

    await release_segment_occupancy(runtime, owner_id)
    return True
