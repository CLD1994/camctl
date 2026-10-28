"""设备可用性检查的会话级协调。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.logging_runtime.diagnostics import DiagnosticState, record_availability_error, record_call_outcome
from camctl.operations.attempts import (
    AttemptFinish, AttemptIntent, AttemptTarget, OperationKind, RetryWaitGate,
    RunFinish, RunOutcome,
)
from camctl.operations.availability_owner import AvailabilityAttemptOwner
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.business import SharedSave
from camctl.persistence.models import DbOutcomeKind, db_failure, db_outcome_for
from camctl.persistence.repositories.operations import OperationRepository


class AvailabilityProgress(Enum):
    READY = "ready"
    WAIT = "wait"
    FAILED = "failed"


class _DispatchCancelled(asyncio.CancelledError):
    def __init__(self, outcome):
        super().__init__("设备检查在派发前取消")
        self.result = outcome


@dataclass
class _Write:
    request: Any
    database: Any
    callback: Any
    key: Any = field(default_factory=new_operation_key)
    save: SharedSave = field(default_factory=SharedSave)


def availability_result_saver(*, ticket, request, config, evidence, binding, check_id,
                              database, write, finish_availability, finish_attempt,
                              occurred_at, monotonic_ns, retry_gate):
    """持有一次检查结果的固定保存申请及诊断责任。"""
    finish_holder = {}

    async def save(actual, diagnostic):
        if not isinstance(actual, CallOutcome) and not actual.settled:
            error = next((call.error for call in actual.calls if call.error is not None), None)
            if error is None:
                error = ErrorValue("device_error", "availability", {"reason": actual.reason})
            record_availability_error(error, ticket=ticket, diagnostics=actual.tool_diagnostics(),
                context={"action_id": request.action_id},
                details={"reason": actual.reason, "calls": actual.calls}, state=diagnostic)
            raise ConsistencyError("设备检查实际收场尚未确认")
        device_result = None if isinstance(actual, CallOutcome) else actual.to_device_result(str(check_id))
        outcome = actual if device_result is None else device_result.outcome
        diagnostic_error = None if device_result is None else device_result.diagnostic_error
        if outcome.error is not None or diagnostic_error is not None:
            record_call_outcome(outcome, ticket=ticket, run_error=diagnostic_error,
                context={"action_id": request.action_id}, state=diagnostic, include_tool_stdout=True)
        if "finish" not in finish_holder:
            validated = validate_outcome(ticket, outcome, evidence)
            if outcome.status is AttemptStatus.SUCCEEDED:
                finish = AttemptFinish(ticket, validated, occurred_at(), run_finish=RunFinish(RunOutcome.SUCCEEDED))
            else:
                action = await database.read(lambda owned: _action_facts(owned, request.action_id))
                if action["cancel_requested"] or action["status"] == 6:
                    finish = AttemptFinish(ticket, validated, occurred_at(), run_finish=RunFinish(RunOutcome.CANCELED))
                elif request.trigger_copy_id is None and (action["status"] == 5 or
                        occurred_at() > action["scheduled_at"] + action["max_delay_ms"] * 1000):
                    finish = AttemptFinish(ticket, validated, occurred_at(), run_finish=RunFinish(RunOutcome.EXPIRED))
                elif ticket.attempt_id >= config.max_attempts:
                    error = ErrorValue("source_device_unavailable" if request.trigger_copy_id else "device_availability_unconfirmed",
                        "source_read" if request.trigger_copy_id else "execution", {"device_id": binding.device_id,
                        "operation_run_id": str(ticket.run_id), "attempts_used": ticket.attempt_id,
                        "max_attempts": config.max_attempts})
                    finish = AttemptFinish(ticket, validated, occurred_at(), run_finish=RunFinish(RunOutcome.FAILED, error))
                else:
                    finish = AttemptFinish(ticket, validated, occurred_at(), retry_wait=True)
            finish_holder["finish"] = finish
        finish = finish_holder["finish"]
        record_call_outcome(outcome, ticket=ticket,
            run_error=diagnostic_error or (None if finish.run_finish is None else finish.run_finish.error),
            context={"action_id": request.action_id}, state=diagnostic, include_tool_stdout=True)
        callback = finish_availability if outcome.status is AttemptStatus.SUCCEEDED else finish_attempt
        await write(database, (request.action_id, request.trigger_copy_id, "result", ticket.run_id, ticket.attempt_id),
                    finish, callback)
        if finish.retry_wait:
            retry_gate.established(ticket.responsibility_key, monotonic_ns())

    return save


class AvailabilityCoordinator:
    """拍摄及设备读取共享固定检查、原调用和未交付事务。"""

    def __init__(self, *, monotonic_ns, occurred_at, recovery_boundary,
                 recovery_max_event_id, wake=None):
        self.monotonic_ns = monotonic_ns
        self.occurred_at = occurred_at
        self.recovery_boundary = recovery_boundary
        self.recovery_max_event_id = recovery_max_event_id
        self.wake = wake
        self.operations = OperationRepository()
        self.retry_gate = RetryWaitGate()
        self.writes: dict[tuple, _Write] = {}
        self.calls: dict[int, AvailabilityAttemptOwner] = {}
        self.tasks: dict[tuple, asyncio.Task] = {}
        self.results: dict[tuple, Any] = {}
        self.recovery_diagnostics: dict[int, DiagnosticState] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._stopping = False
        self._deadlines: dict[int, int] = {}

    async def _write(self, database, identity, request, callback):
        pending = self.writes.get(identity)
        if pending is None:
            pending = _Write(request, database, callback)
            self.writes[identity] = pending

        async def save():
            try:
                receipt = await pending.database.write(lambda owned: pending.callback(pending.request, pending.key, owned),
                                               key=pending.key)
            except BaseException as error:
                receipt = db_outcome_for(error)
                if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                    raise
                raise
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, "设备检查事务未可靠完成", receipt)
            return receipt.value

        pending.save.retry_failed()
        result = await pending.save.run(save)
        self.writes.pop(identity, None)
        return result

    def _notify(self):
        if self.wake is not None:
            from camctl.scheduling.notifications import WakeReason
            self.wake.mark_changed(WakeReason.ACTIVITY_FINISHED)

    def next_wakeup_at(self, now, margin_s):
        if not self._deadlines:
            return None
        return now + max(0, min(self._deadlines.values()) - self.monotonic_ns()) / 1_000_000_000

    def required_settlements(self):
        return (len(self.tasks) + len(self.writes) + sum(not owner.saved for owner in self.calls.values())
                + sum(isinstance(result, BaseException) for result in self.results.values()))

    def pending(self, action_id):
        return (any(key[0] == action_id for key in self.tasks)
                or any(key[0] == action_id for key in self.writes)
                or any(getattr(owner, "action_id", None) == action_id and not owner.saved
                       for owner in self.calls.values()))

    def cancel_action(self, action_id):
        for owner in self.calls.values():
            if getattr(owner, "action_id", None) == action_id:
                owner.stop()

    def stop_new_work(self):
        self._stopping = True
        for owner in self.calls.values():
            owner.stop()

    def check_completed(self):
        for identity, result in tuple(self.results.items()):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                del self.results[identity]
                raise result

    async def settle(self):
        failures = []
        for identity, task in tuple(self.tasks.items()):
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if not task.done():
                    raise
            except Exception as error:
                failures.append(error)
            finally:
                if task.done():
                    self.tasks.pop(identity, None)
        for owner in tuple(self.calls.values()):
            try:
                await owner.settle()
            except Exception as error:
                failures.append(error)
        for identity, pending in tuple(self.writes.items()):
            try:
                await self._write(pending.database, identity, pending.request, pending.callback)
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("设备检查责任收场失败", failures)

    async def request(self, database, request, config, driver, evidence, binding):
        """安排检查推进，长工具调用不阻塞本地文件或其他设备。"""
        identity = (request.action_id, request.trigger_copy_id)
        if identity in self.results:
            result = self.results.pop(identity)
            if isinstance(result, BaseException):
                raise result
            return result
        task = self.tasks.get(identity)
        if task is not None:
            return AvailabilityProgress.WAIT
        if self._stopping:
            return AvailabilityProgress.WAIT
        task = asyncio.create_task(self.advance(database, request, config, driver, evidence, binding))
        self.tasks[identity] = task
        def completed(actual):
            self.tasks.pop(identity, None)
            try:
                result = actual.result()
            except asyncio.CancelledError:
                self._notify()
                return
            except BaseException as error:
                result = error
            self.results[identity] = result
            if result is not AvailabilityProgress.WAIT:
                self._notify()
        task.add_done_callback(completed)
        await asyncio.sleep(0)
        return AvailabilityProgress.WAIT

    async def advance(self, database, request, config, driver, evidence, binding):
        """推进已具普通资格的触发工作；READY 后由原调度重新判断。"""
        from camctl.persistence.repositories.availability_create import CreateDisposition, ensure_check
        from camctl.persistence.repositories.availability_release import ResolveDisposition, finish_if_resolved

        identity = (request.action_id, request.trigger_copy_id)
        lock = self._locks.setdefault(identity, asyncio.Lock())
        async with lock:
            if self._stopping:
                return AvailabilityProgress.WAIT
            existing = await database.read(lambda owned: _open_check(owned, request))
            if existing is None:
                created = await self._write(database, (*identity, "create"), request, ensure_check)
                if created.disposition is CreateDisposition.NOT_REQUIRED:
                    return AvailabilityProgress.READY
                if created.disposition in (CreateDisposition.WAIT, CreateDisposition.IDENTITY_MISMATCH):
                    return AvailabilityProgress.WAIT
                check_id, run_id = created.check_id, created.run_id
            else:
                check_id, run_id, original_copy_id = existing
                request = replace(request, trigger_copy_id=original_copy_id)
            owner = self.calls.get(run_id)
            if owner is not None:
                if not owner.saved:
                    await owner.advance()
                if owner.saved:
                    self.calls.pop(run_id, None)
                    self._notify()
            resolved = await self._write(database, (*identity, "resolve"),
                (check_id, self.occurred_at()),
                lambda args, key, owned: finish_if_resolved(*args, key, owned))
            if resolved is ResolveDisposition.SUCCEEDED:
                self._deadlines.pop(check_id, None)
                return AvailabilityProgress.READY
            run = await database.read(lambda owned: _run_facts(owned, run_id))
            if resolved is ResolveDisposition.TERMINAL:
                self._deadlines.pop(check_id, None)
                if run["status"] == 3:
                    return AvailabilityProgress.READY
                if run["status"] in (4, 6):
                    await self._deliver_failure(database, identity, check_id)
                return AvailabilityProgress.FAILED
            if run["running_attempt"] is not None:
                await self._recover_attempt(database, run, check_id, request, evidence, binding, config)
                return AvailabilityProgress.WAIT
            from camctl.persistence.repositories.availability_wait_finish import (
                WaitingFinishDisposition, finish_waiting_availability,
            )
            now = self.occurred_at()
            waiting = await self._write(database, (*identity, "finish-wait"),
                (check_id, now, now, config.max_attempts),
                lambda args, key, owned: finish_waiting_availability(*args, key, owned))
            if waiting in (WaitingFinishDisposition.FINISHED, WaitingFinishDisposition.TERMINAL,
                           WaitingFinishDisposition.RESOLVED):
                self._notify()
                return AvailabilityProgress.WAIT
            if run["attempts_used"] >= config.max_attempts:
                return AvailabilityProgress.WAIT
            if run["retry_wait_required"]:
                now_ns = self.monotonic_ns()
                remaining = self.retry_gate.remaining(f"availability/{check_id}",
                    attempts_used=run["attempts_used"], retry_wait_required=True,
                    max_attempts_used=config.max_attempts, interval_s=config.retry_interval_s, now_ns=now_ns)
                if remaining is not None:
                    target = now_ns + int(remaining * 1_000_000_000)
                    if self._deadlines.get(check_id) != target:
                        self._deadlines[check_id] = target
                        self._notify()
                    return AvailabilityProgress.WAIT
                from camctl.persistence.repositories.availability_retry import complete_availability_retry_wait
                await self._write(database, (*identity, "retry"),
                    (run_id, run["last_attempt_id"], self.occurred_at()),
                    lambda args, key, owned: complete_availability_retry_wait(*args, key, owned))
                self.retry_gate.cleared(f"availability/{check_id}")
                self._deadlines.pop(check_id, None)
            if self._stopping:
                return AvailabilityProgress.WAIT
            now = self.occurred_at()
            current = replace(request, occurred_at=now, trusted_wall_now=now)
            intent = AttemptIntent("availability", request.action_id, OperationKind.CHECK_DEVICE_AVAILABILITY,
                                   AttemptTarget(availability_check_id=check_id), None, config, now)
            begun = await self._write(database, (*identity, "intent"), (intent, current),
                lambda args, key, owned: self.operations.begin_availability_attempt(*args, key, owned))
            if begun.ticket is None:
                return AvailabilityProgress.WAIT
            owner = self._attempt_owner(database, begun.ticket, current, config, driver, evidence, binding, check_id)
            owner.action_id = request.action_id
            self.calls[run_id] = owner
            await owner.advance()
            if owner.saved:
                self.calls.pop(run_id, None)
                self._notify()
            return AvailabilityProgress.WAIT

    async def _deliver_failure(self, database, identity, check_id):
        from camctl.persistence.repositories.availability_failure import (
            FailureDisposition, fail_photo_availability, fail_read_availability,
        )
        action_id, copy_id = identity
        now = self.occurred_at()
        if copy_id is None:
            request = (check_id, now, now)
            callback = lambda args, key, owned: fail_photo_availability(*args, key, owned)
        else:
            request = (check_id, copy_id, now)
            callback = lambda args, key, owned: fail_read_availability(*args, key, owned)
        result = await self._write(database, (*identity, "deliver", check_id), request, callback)
        if result is FailureDisposition.FAILED:
            self._notify()

    def _attempt_owner(self, database, ticket, request, config, driver, evidence, binding, check_id):
        from camctl.devices.drivers.hasselblad.driver import AvailabilityCancelled
        from camctl.persistence.repositories.availability_intent import recheck_availability_dispatch
        from camctl.persistence.repositories.availability_release import finish_availability

        async def invoke():
            if self._stopping:
                return CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                    error=ErrorValue("device_error", "availability", {"reason": "session_stopping"}),
                    settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                                          EvidenceValue("availability_not_dispatched", 1, {})))
            now = self.occurred_at()
            current = replace(request, occurred_at=now, trusted_wall_now=now)
            try:
                decision = await database.read(lambda owned: recheck_availability_dispatch(owned.connection, ticket, current))
            except asyncio.CancelledError as error:
                outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                    error=ErrorValue("canceled", "dispatch"),
                    settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                                          EvidenceValue("availability_not_dispatched", 1, {})))
                raise _DispatchCancelled(outcome) from error
            if not decision.allowed:
                return CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                    error=ErrorValue("device_error", "availability", {"reason": decision.reason}),
                    settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                                          EvidenceValue("availability_not_dispatched", 1, {})))
            result = await driver.check_availability(binding,
                expected_serial=request.identity.serial, expected_product_id=request.identity.product_id,
                timeout_s=config.timeout_s)
            return result

        save = availability_result_saver(ticket=ticket, request=request, config=config,
            evidence=evidence, binding=binding, check_id=check_id, database=database, write=self._write,
            finish_availability=finish_availability, finish_attempt=self.operations.finish_attempt,
            occurred_at=self.occurred_at, monotonic_ns=self.monotonic_ns, retry_gate=self.retry_gate)

        return AvailabilityAttemptOwner(ticket, invoke, save,
            lambda error: error.result if isinstance(error, (AvailabilityCancelled, _DispatchCancelled)) else None)

    async def _recover_attempt(self, database, run, check_id, request, evidence, binding, config):
        from camctl.operations.recovery import RecoveryBoundary
        horizon = self.recovery_max_event_id() if callable(self.recovery_max_event_id) else self.recovery_max_event_id
        if (self.recovery_boundary not in (RecoveryBoundary.HOST_LOCAL_SETTLED, RecoveryBoundary.HOST_POWER_CYCLE)
                or horizon is None or run["intent_event_id"] is None or run["intent_event_id"] > horizon):
            return
        ticket = AttemptTicket(run["attempts_used"], "availability", str(check_id),
                               run["responsibility_key"], run["id"])
        outcome = CallOutcome(status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
            error=ErrorValue("result_not_saved", "recovery"),
            settlement=Settlement(SettlementBasis.ASSUMED, EvidenceValue("local_availability_recovery", 1, {})))
        validated = validate_outcome(ticket, outcome, evidence)
        active = run["status"] in (1, 2)
        finish = AttemptFinish(ticket, validated, self.occurred_at(), retry_wait=active,
                               recovery_max_event_id=horizon)
        diagnostic = self.recovery_diagnostics.setdefault(run["id"], DiagnosticState())
        record_call_outcome(outcome, ticket=ticket,
                            context={"action_id": request.action_id}, state=diagnostic)
        await self._write(database, (request.action_id, request.trigger_copy_id, "recover", run["id"]),
                          finish, self.operations.finish_attempt)
        if active:
            self.retry_gate.established(ticket.responsibility_key, self.monotonic_ns())
            action = await database.read(lambda owned: _action_facts(owned, request.action_id))
            now = self.occurred_at()
            terminal_trigger = (action["cancel_requested"] or action["status"] in (5, 6)
                or request.trigger_copy_id is None and now > action["scheduled_at"] + action["max_delay_ms"] * 1000)
            if config is not None or terminal_trigger:
                from camctl.persistence.repositories.availability_wait_finish import (
                    WaitingFinishDisposition, finish_waiting_availability,
                )
                maximum = config.max_attempts if config is not None else run["max_attempts_used"]
                result = await self._write(database, (request.action_id, request.trigger_copy_id, "finish-wait"),
                    (check_id, now, now, maximum),
                    lambda args, key, owned: finish_waiting_availability(*args, key, owned))
                if result in (WaitingFinishDisposition.FINISHED, WaitingFinishDisposition.TERMINAL):
                    self.retry_gate.cleared(ticket.responsibility_key)
        self._notify()


def _run_facts(owned, run_id):
    from camctl.persistence.transaction import row_facts
    run = row_facts(owned.connection, "operation_runs", run_id)
    if run is None:
        raise ConsistencyError("设备检查流程不存在")
    row = owned.connection.execute(
        "SELECT id,status,result_event_id,intent_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
        (run_id,)).fetchone()
    run["last_attempt_id"] = None if row is None else row[0]
    run["running_attempt"] = None if row is None or row[1] != 1 and row[2] is not None else row[0]
    run["intent_event_id"] = None if row is None else row[3]
    return run


def _open_check(owned, request):
    rows = owned.connection.execute(
        "SELECT c.id,r.id,c.trigger_copy_id FROM device_availability_checks c"
        " JOIN operation_runs r ON r.availability_check_id=c.id"
        " WHERE c.action_id=? AND c.resource_key=? AND r.status IN (1,2) ORDER BY c.id LIMIT 2",
        (request.action_id, request.identity.resource_key),
    ).fetchall()
    if len(rows) > 1:
        raise ConsistencyError("同一动作与机身存在多个开放检查")
    return None if not rows else tuple(rows[0])


def _action_facts(owned, action_id):
    from camctl.persistence.transaction import row_facts
    action = row_facts(owned.connection, "actions", action_id)
    if action is None:
        raise ConsistencyError("设备检查触发动作不存在")
    return action
