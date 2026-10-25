"""拍摄设置准备、原保存申请及会话凭据的实际拥有者。"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass, field, replace
from typing import Any

from camctl.capture.baseline import BaselinePreparation, PreparationPhase
from camctl.capture.models import ActivityReleaseSave
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.capture_preparation import CAPTURE_ACTION_TYPES, capture_operation
from camctl.devices.ports import CapturePreparationDriver, ControlRequest, DeviceCallResult
from camctl.devices.evidence import EvidenceRegistry
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, OperationKind,
    RunFinish, RunOutcome,
)
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.owned_calls import owned_tool_call
from camctl.operations.validation import validate_outcome
from camctl.persistence.business import SharedSave, settle_required
from camctl.persistence.models import DbOutcomeKind, db_outcome_for
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.transaction import row_facts

_PREPARE_KIND = int(enum_for("operation_runs.kind").PREPARE_CAPTURE)


@dataclass
class PendingPreparation:
    action_id: int
    activity_id: int
    session_key: str
    intent: AttemptIntent | None
    evidence: EvidenceRegistry
    driver: CapturePreparationDriver | None
    intent_key: OperationKey = field(default_factory=new_operation_key)
    result_key: OperationKey = field(default_factory=new_operation_key)
    ticket: AttemptTicket | None = None
    task: asyncio.Task | None = None
    actual: DeviceCallResult | None = None
    returned_at: int | None = None
    finish: AttemptFinish | None = None
    saved: bool = False
    successful: bool = False
    refused: str | None = None
    entered: bool = False
    intent_save: SharedSave = field(default_factory=SharedSave)
    result_save: SharedSave = field(default_factory=SharedSave)
    canceled_before_dispatch_at: int | None = None
    wakeup_pending: bool = False


@dataclass
class RuntimePreparationCache:
    """仅在本会话共享；实际返回和原键不依赖某轮 Runtime 的寿命。"""

    owners: dict[int, PendingPreparation] = field(default_factory=dict)
    reservations: dict[int, tuple[Any, Any]] = field(default_factory=dict)
    endings: dict[int, tuple[str, Any, Any]] = field(default_factory=dict)
    request_saves: dict[OperationKey, SharedSave] = field(default_factory=dict)
    changed_actions: set[int] = field(default_factory=set)

    def take_wakeup(self, action_id):
        """合并本动作尚未交给调度的实际返回和首次保存应用。"""
        owner = self.owners.get(action_id)
        changed = action_id in self.changed_actions
        self.changed_actions.discard(action_id)
        if owner is not None and owner.wakeup_pending:
            owner.wakeup_pending = False
            changed = True
        return changed

    def pending(self, action_id):
        owner = self.owners.get(action_id)
        return (action_id in self.reservations or action_id in self.endings
                or owner is not None and (not owner.saved
                    or owner.task is not None and not owner.task.done()))


def _completed(receipt, message):
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"{message}，原完整申请仍持有: {receipt.error}")
    return receipt.value


async def _save_owner(owner, database, operations, *, retry=False):
    saving_intent = owner.ticket is None

    async def save_and_apply():
        notification_errors = []

        async def save(callback, *, key):
            try:
                return await database.write(callback, key=key)
            except BaseException as error:
                receipt = db_outcome_for(error)
                if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                    raise
                notification_errors.append(error)
                return receipt

        if owner.refused is not None:
            return
        if owner.ticket is None:
            result = _completed(await save(lambda owned: operations.begin_attempt(owner.intent, owner.intent_key, owned), key=owner.intent_key),
                                "设置准备意图未可靠保存")
            if result.ticket is None:
                owner.refused = result.reason
                owner.saved = True
                if notification_errors:
                    raise notification_errors[0]
                return
            owner.ticket = result.ticket
            owner.wakeup_pending = True
        if saving_intent:
            if notification_errors:
                raise notification_errors[0]
            return
        if owner.canceled_before_dispatch_at is not None and not owner.entered and owner.actual is None:
            owner.actual = _not_dispatched_preparation()
            owner.returned_at = owner.canceled_before_dispatch_at
        if owner.actual is not None and owner.finish is None:
            actual = owner.actual
            if actual.outcome is None:
                raise ConsistencyError("设置准备没有交回完整的实际控制结果")
            if actual.diagnostics_check is not None:
                actual.diagnostics_check(actual.outcome)
            validated = validate_outcome(owner.ticket, actual.outcome, owner.evidence)
            owner.successful = (actual.outcome.status is AttemptStatus.SUCCEEDED
                                and actual.outcome.effect is EffectState.CONFIRMED
                                and actual.outcome.error is None
                                and any(item.type == "capture_preparation_completed"
                                        and item.data.get("activity_id") == str(owner.activity_id)
                                        for item in actual.observations))
            end = (RunFinish(RunOutcome.SUCCEEDED) if owner.successful else
                   RunFinish(RunOutcome.UNCONFIRMED if actual.outcome.status is AttemptStatus.UNKNOWN
                             else RunOutcome.FAILED,
                             actual.outcome.error or ErrorValue("device_error", "device")))
            owner.finish = AttemptFinish(owner.ticket, validated, owner.returned_at, run_finish=end)
        if owner.finish is not None and not owner.saved:
            _completed(await save(lambda owned: operations.finish_attempt(owner.finish, owner.result_key, owned), key=owner.result_key),
                       "设置准备结果未可靠保存")
            owner.saved = True
            owner.wakeup_pending = True
        if notification_errors:
            raise notification_errors[0]
    if (owner.ticket is not None and owner.actual is None and owner.finish is None
            and owner.canceled_before_dispatch_at is None):
        return
    saving = owner.intent_save if saving_intent else owner.result_save
    if retry:
        saving.retry_failed()
    result = await saving.run(save_and_apply)
    if saving_intent and owner.ticket is not None and owner.canceled_before_dispatch_at is not None:
        return await _save_owner(owner, database, operations, retry=retry)
    return result


async def _save_cached_request(database, cache, action_id, *, scheduling, capture, ending=False, retry=False):
    requests = cache.endings if ending else cache.reservations
    original = requests.get(action_id)
    if original is None:
        return
    if ending:
        kind, request, key = original
        method = capture.release_occupancy if kind == "release" else getattr(scheduling, kind)
        message = "原准备释放未可靠保存" if kind == "release" else "原准备收场未可靠保存"
    else:
        request, key = original
        kind, method, message = None, scheduling.reserve_preparation, "原准备占用未可靠保存"

    async def save_and_apply():
        notification_error = None
        try:
            receipt = await database.write(lambda owned: method(request, key, owned), key=key)
        except BaseException as error:
            receipt = db_outcome_for(error)
            if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                raise
            notification_error = error
        value = _completed(receipt, message)
        if kind == "release" and value.outcome.value == "rejected":
            raise ConsistencyError(f"准备释放条件未成立: {value.reason}")
        del requests[action_id]
        if ending or getattr(getattr(value, "outcome", None), "value", None) != "rejected":
            cache.changed_actions.add(action_id)
        if notification_error is not None:
            raise notification_error
        return value

    saving = cache.request_saves.setdefault(key, SharedSave())
    if retry:
        saving.retry_failed()
    try:
        return await saving.run(save_and_apply)
    finally:
        if requests.get(action_id) is not original and cache.request_saves.get(key) is saving:
            del cache.request_saves[key]


async def resume_preparation_saves(database, cache, *, operations=None, scheduling=None, capture=None):
    """只核原申请；不读取时钟、不取得设备资格、不重新设置。"""
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.scheduling import SchedulingRepository

    operations = OperationRepository() if operations is None else operations
    scheduling = SchedulingRepository() if scheduling is None else scheduling
    capture = CaptureRepository() if capture is None else capture

    async def save_and_apply():
        for owner in tuple(cache.owners.values()):
            await _save_owner(owner, database, operations, retry=True)
        for action_id in tuple(cache.reservations):
            await _save_cached_request(database, cache, action_id,
                scheduling=scheduling, capture=capture, retry=True)
        for action_id in tuple(cache.endings):
            await _save_cached_request(database, cache, action_id,
                scheduling=scheduling, capture=capture, ending=True, retry=True)
    return await settle_required(save_and_apply())


async def preparation_dispatch_reason(runtime, action_id):
    """设置允许提前；每次真实设置前核取消、原占用和窗口上界。"""
    from camctl.persistence.repositories.capture_facts import load_start_facts

    def read(owned):
        connection = owned.connection
        action = row_facts(connection, "actions", action_id)
        if action is None:
            raise LookupError(f"动作不存在: {action_id}")
        if action["status"] in (3, 4, 5, 6) or action["cancel_requested"]:
            return action, None, False
        with closing(connection.execute(
                "SELECT id FROM device_activities WHERE action_id=?", (action_id,))) as cursor:
            found = cursor.fetchone()
        activity = None if found is None else row_facts(
            connection, "device_activities", found[0])
        if activity is None or activity["occupancy_state"] != 1:
            return action, activity, False
        with closing(connection.execute(
                "SELECT a.status,a.effect_state,e.occurred_at FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id=r.id"
                " JOIN history_events e ON e.id=a.intent_event_id"
                " WHERE r.responsibility_key=? ORDER BY a.id DESC LIMIT 1",
                (f"start/{action_id}",))) as cursor:
            attempt = cursor.fetchone()
        if attempt is None:
            return action, activity, False
        if action["type"] == int(enum_for("actions.type").CAMERA_TAKE_PHOTO):
            return action, activity, True
        facts = load_start_facts(connection, action)
        start_precedes = (not facts.not_started
            or facts.run is not None and facts.run["status"] not in (1, 2)
            or facts.attempts_used >= runtime.start_config.max_attempts)
        return action, activity, start_precedes

    action, activity, start_precedes = await runtime.database.read(
        read, description="读取设置准备派发资格")
    if action["status"] in (3, 4, 5, 6):
        return "terminal"
    if action["cancel_requested"]:
        return "canceled"
    if runtime.wall_us() > action["scheduled_at"] + action["max_delay_ms"] * 1000:
        return "window_ended"
    if activity is None or activity["occupancy_state"] != 1:
        return "occupancy_released"
    if start_precedes:
        return "already_dispatched"
    return None


async def _activity(runtime, action_id):
    def read(owned, parameters=(action_id,)):
        with closing(owned.connection.execute("SELECT id FROM device_activities WHERE action_id=?", parameters)) as cursor:
            return cursor.fetchone()
    found = await runtime.database.read(read, description="读取拍摄直接事实")
    return None if found is None else (await runtime.database.read(lambda owned, inputs=("device_activities", found[0]), options=dict(): row_facts(owned.connection, inputs[0], inputs[1], **options), description="读取拍摄直接事实"))


async def _start_precedes_preparation(runtime, action):
    if (await runtime.last_attempt(f"start/{action['id']}")) is None:
        return False
    if action["type"] == int(enum_for("actions.type").CAMERA_TAKE_PHOTO):
        return True
    from camctl.persistence.repositories.capture_facts import load_start_facts
    facts = (await runtime.database.read(lambda owned, inputs=(action,), options=dict(): load_start_facts(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    return (not facts.not_started or facts.run is not None and facts.run["status"] not in (1, 2)
            or facts.attempts_used >= runtime.start_config.max_attempts)


def _request(runtime, action, *, ticket=None, prepared_by=None):
    return ControlRequest(
        capture_operation(action["type"]),
        DeviceBinding(action["device_id"], action["driver_id"]), action["effective_params_json"],
        ticket=ticket, timeout_s=runtime.start_config.timeout_s, prepared_by=prepared_by,
        dispatch_check=lambda: preparation_dispatch_reason(runtime, action["id"]),
        start_return_meaning=action["execution_spec_json"].get("start_return_meaning"),
    )


def prepared_by(runtime, action):
    owner = runtime.preparation_cache.owners.get(action["id"])
    if (owner is None or owner.session_key != runtime.session_key or not owner.saved
            or not owner.successful or owner.driver is not runtime.preparation_driver):
        return None
    request = _request(runtime, action, ticket=owner.ticket, prepared_by=owner.ticket)
    return owner.ticket if runtime.preparation_driver.prepared_capture_matches(request) else None


@owned_tool_call
async def _call_prepare(owner, runtime, request):
    owner.entered = True
    owner.actual = await runtime.preparation_driver.prepare_capture(request)
    if not isinstance(owner.actual, DeviceCallResult):
        raise ConsistencyError("设置准备没有交回实际控制结果")
    owner.wakeup_pending = True
    owner.returned_at = runtime.wall_us()
    (await _save_owner(owner, runtime.database, runtime.operations))


def _not_dispatched_preparation():
    return DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        error=ErrorValue("canceled", "dispatch"),
        settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                              EvidenceValue("dispatch_prevented", 1))))


async def _save_unentered_preparation(owner, runtime):
    if owner.entered or owner.actual is not None:
        return
    if owner.canceled_before_dispatch_at is None:
        owner.canceled_before_dispatch_at = runtime.wall_us()
    if owner.ticket is None:
        return
    await _save_owner(owner, runtime.database, runtime.operations)


async def _recover_old_preparation(runtime, action_id):
    """旧意图只恢复原责任；没有真实返回时不声称设置成功。"""
    def read(owned, parameters=(action_id, _PREPARE_KIND)):
        with closing(owned.connection.execute("SELECT r.id,r.responsibility_key,r.activity_id,t.attempt_no,t.status"
        " FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE r.action_id=? AND r.kind=? AND r.status IN (1,2) ORDER BY r.id,t.attempt_no", parameters)) as cursor:
            return cursor.fetchall()
    rows = await runtime.database.read(read, description="读取拍摄直接事实")
    for run_id, key, activity_id, attempt_no, status in rows:
        local = runtime.preparation_cache.owners.get(action_id)
        if local is not None and local.ticket is not None and local.ticket.run_id == run_id:
            if not local.saved:
                return False
            continue
        ticket = AttemptTicket(attempt_no, "control", str(activity_id), key, run_id)
        evidence = (await runtime._attempt_recovery_evidence(ticket))
        if evidence is None:
            return False
        from camctl.capture.handlers import _recovered_call_outcome
        actual = _recovered_call_outcome()
        owner = PendingPreparation(action_id, activity_id, "", None, evidence, None, ticket=ticket)
        owner.finish = AttemptFinish(ticket, validate_outcome(ticket, actual, evidence), runtime.wall_us(),
            run_finish=RunFinish(RunOutcome.UNCONFIRMED, actual.error),
            recovery_max_event_id=runtime.recovery_max_event_id)
        runtime.preparation_cache.owners[action_id] = owner
        (await _save_owner(owner, runtime.database, runtime.operations))
    return True


async def _end(runtime, action_id, kind, request):
    cache = runtime.preparation_cache
    if action_id not in cache.endings:
        cache.endings[action_id] = kind, request, new_operation_key()
    (await resume_preparation_saves(runtime.database, cache, operations=runtime.operations,
                             scheduling=runtime.scheduling, capture=runtime.capture))


async def _fail_preparation(runtime, action_id, reason, error=None):
    action = (await runtime.action(action_id))
    if action["status"] == 2:
        from camctl.capture.handlers import StartCloseRequest
        from camctl.outputs.catalog import OutputCatalogFacts
        from camctl.operations.attempts import StaleRunFinish
        from camctl.persistence.repositories.capture import FinishCapture, RecordingFailure
        activity = (await _activity(runtime, action_id))
        now = runtime.wall_us()
        details = {"activity_id": str(activity["id"]), "reason": reason}
        finish = FinishCapture(action_id, (), OutputCatalogFacts(action_id, True), now,
                               RecordingFailure("capture_failed", details), baseline_error=error)
        if action["execution_spec_json"].get("result_policy") is not None:
            from camctl.capture.segment_completion import (
                _save, finish_segmented_baseline_failure, finish_segmented_preparation_failure,
                resume_segment_writes,
            )
            (await resume_segment_writes(runtime.database, runtime.pending_segment_writes, action_id))
            if reason == "baseline_read_failed":
                (await finish_segmented_baseline_failure(runtime, action_id))
            else:
                if (await runtime.last_attempt(f"start/{action_id}")) is not None:
                    (await _save(runtime, action_id, runtime.operations, "finish_stale_runs",
                        StaleRunFinish((f"start/{action_id}",), RunOutcome.FAILED, now,
                                       ErrorValue("capture_failed", "execution", details))))
                (await finish_segmented_preparation_failure(runtime, action_id))
        elif (await runtime.last_attempt(f"start/{action_id}")) is None:
            (await runtime.save_capture_completion(finish))
        else:
            (await runtime.save_capture_completion(StartCloseRequest(
                StaleRunFinish((f"start/{action_id}",), RunOutcome.FAILED, now,
                               ErrorValue("capture_failed", "execution", details)), finish)))
    else:
        from camctl.persistence.repositories.scheduling import FailPreparationRequest
        (await _end(runtime, action_id, "fail_preparation", FailPreparationRequest(
            action_id, runtime.wall_us(), reason)))
    (await settle_preparation(runtime, action_id))


async def settle_preparation(runtime, action_id):
    """实际调用与原保存收场后才关闭目录缓存并释放准备占用。"""
    cache = runtime.preparation_cache
    (await resume_preparation_saves(runtime.database, cache, operations=runtime.operations,
                             scheduling=runtime.scheduling, capture=runtime.capture))
    if cache.pending(action_id) or not (await _recover_old_preparation(runtime, action_id)):
        return False
    pending = runtime.pending_baselines.get(action_id)
    if pending is not None:
        if pending.in_call:
            return False
        await _resume_baseline_progress(runtime, action_id)
        pending = runtime.pending_baselines.get(action_id)
        if pending is not None:
            if pending.read_owner is not None or pending.saves.pending is not None:
                return False
    action = (await runtime.action(action_id))
    if action["status"] not in (3, 4, 5, 6):
        if action["cancel_requested"]:
            from camctl.capture.handlers import _finish_canceled_capture
            (await _finish_canceled_capture(runtime, action_id, unstarted=True))
            cache.changed_actions.add(action_id)
        if (await runtime.action(action_id))["status"] not in (3, 4, 5, 6):
            return False
    if runtime.pending_baselines.get(action_id) is not None:
        await _resume_baseline_progress(runtime, action_id)
        if runtime.pending_baselines.get(action_id) is not None:
            return False
    activity = (await _activity(runtime, action_id))
    if activity is not None and activity["occupancy_state"] == 1 and activity["dispatch_state"] in (1, 4):
        (await _end(runtime, action_id, "release", ActivityReleaseSave(
            action_id, runtime.wall_us(), preparation_resolved=True)))
    owner = cache.owners.pop(action_id, None)
    if owner is not None and owner.wakeup_pending:
        cache.changed_actions.add(action_id)
    return True


async def _resume_baseline_progress(runtime, action_id):
    pending = runtime.pending_baselines.get(action_id)
    resumed = await runtime.resume_baselines(action_id)
    changed = resumed is not None and resumed.wakeup_required
    if pending is not None and pending.wakeup_pending:
        pending.wakeup_pending = False
        changed = True
    if changed:
        runtime.preparation_cache.changed_actions.add(action_id)


async def recover_preparation(runtime, action_id, *, on_progress=None):
    result = await _recover_preparation(runtime, action_id)
    if on_progress is not None and runtime.preparation_cache.take_wakeup(action_id):
        on_progress()
    return result


async def _recover_preparation(runtime, action_id):
    """受限会话只核原保存和旧调用；未终态准备继续保留原活动。"""
    cache = runtime.preparation_cache
    (await resume_preparation_saves(runtime.database, cache, operations=runtime.operations,
                             scheduling=runtime.scheduling, capture=runtime.capture))
    if cache.pending(action_id) or not (await _recover_old_preparation(runtime, action_id)):
        return False
    action = (await runtime.action(action_id))
    if action["status"] in (3, 4, 5, 6) or action["cancel_requested"]:
        return (await settle_preparation(runtime, action_id))
    return True


async def _abort_if_required(runtime, action):
    action_id = action["id"]
    if action["status"] in (3, 4, 5, 6) or action["cancel_requested"]:
        (await settle_preparation(runtime, action_id))
        return BaselinePreparation(PreparationPhase.ABORTED, wakeup_required=False)
    now = runtime.wall_us()
    if now > action["scheduled_at"] + action["max_delay_ms"] * 1000:
        from camctl.persistence.repositories.scheduling import ExpireActionRequest
        (await _end(runtime, action_id, "expire_action", ExpireActionRequest(action_id, now, now)))
        (await settle_preparation(runtime, action_id))
        return BaselinePreparation(PreparationPhase.ABORTED, wakeup_required=False)
    return None


async def advance_preparation(runtime, action_id):
    result = await _advance_preparation(runtime, action_id)
    changed = runtime.preparation_cache.take_wakeup(action_id)
    return replace(result, wakeup_required=True) if changed and not result.wakeup_required else result


async def _advance_preparation(runtime, action_id):
    """推进一次完整准备；READY 后交回调度器等待计划时间。"""
    cache = runtime.preparation_cache
    (await resume_preparation_saves(runtime.database, cache, operations=runtime.operations,
                             scheduling=runtime.scheduling, capture=runtime.capture))
    action = (await runtime.action(action_id))
    if action["type"] not in CAPTURE_ACTION_TYPES:
        return BaselinePreparation(PreparationPhase.READY, wakeup_required=False)
    if action["status"] == 2 and action["execution_spec_json"].get("result_policy") is not None:
        from camctl.capture.segment_completion import resume_segmented_preparation_failure
        if (await resume_segmented_preparation_failure(runtime, action)):
            (await settle_preparation(runtime, action_id))
            return BaselinePreparation(PreparationPhase.ABORTED)
    if (await _start_precedes_preparation(runtime, action)):
        return BaselinePreparation(PreparationPhase.READY, wakeup_required=False)
    if not (await _recover_old_preparation(runtime, action_id)):
        return BaselinePreparation(PreparationPhase.WAITING, wakeup_required=False)
    aborted = (await _abort_if_required(runtime, action))
    if aborted is not None:
        return aborted
    now = runtime.wall_us()
    if now < action["scheduled_at"] - runtime.prepare_lead_s * 1_000_000:
        return BaselinePreparation(PreparationPhase.WAITING, wakeup_required=False)
    activity = (await _activity(runtime, action_id))
    if activity is None:
        from camctl.persistence.repositories.scheduling import ReservePreparationRequest
        if action_id not in cache.reservations:
            request = ReservePreparationRequest(action_id, now, now, runtime.prepare_lead_s)
            cache.reservations[action_id] = request, new_operation_key()
        value = await _save_cached_request(runtime.database, cache, action_id,
            scheduling=runtime.scheduling, capture=runtime.capture)
        if value.outcome.value == "rejected":
            return BaselinePreparation(PreparationPhase.WAITING, wakeup_required=False)
        activity = (await _activity(runtime, action_id))
    if activity is None:
        raise ConsistencyError("准备占用已可靠取得但原活动缺失")
    if prepared_by(runtime, action) is None:
        owner = cache.owners.get(action_id)
        if owner is None or owner.session_key != runtime.session_key:
            intent = AttemptIntent("control", action_id, OperationKind.PREPARE_CAPTURE,
                AttemptTarget(activity_id=activity["id"]), None,
                AttemptConfig(1, runtime.start_config.timeout_s), runtime.wall_us(),
                session_key=runtime.session_key)
            owner = PendingPreparation(action_id, activity["id"], runtime.session_key,
                                       intent, runtime.evidence, runtime.preparation_driver)
            cache.owners[action_id] = owner
            try:
                await _save_owner(owner, runtime.database, runtime.operations)
            except BaseException as interrupted:
                if (isinstance(interrupted, asyncio.CancelledError)
                        or isinstance(interrupted.__cause__, asyncio.CancelledError)):
                    try:
                        await _save_unentered_preparation(owner, runtime)
                    except BaseException as error:
                        raise interrupted from error
                raise
        if owner.task is None and owner.actual is None and owner.finish is None:
            if owner.refused is not None:
                cache.owners.pop(action_id)
                return BaselinePreparation(PreparationPhase.WAITING, wakeup_required=False)
            owner.task = asyncio.create_task(_call_prepare(owner, runtime,
                _request(runtime, action, ticket=owner.ticket)))
            try:
                await _wait_preparation_task(owner.task)
            except asyncio.CancelledError as interrupted:
                try:
                    await _save_unentered_preparation(owner, runtime)
                except BaseException as error:
                    raise interrupted from error
                raise
        if not owner.saved:
            return BaselinePreparation(PreparationPhase.WAITING, wakeup_required=False)
        aborted = (await _abort_if_required(runtime, (await runtime.action(action_id))))
        if aborted is not None:
            return aborted
        if not owner.successful:
            (await _fail_preparation(runtime, action_id, "device_failed"))
            return BaselinePreparation(PreparationPhase.FAILED)
        if prepared_by(runtime, action) is None:
            raise ConsistencyError("本会话设置成功但驱动未保留匹配准备凭据")
    reason = (await preparation_dispatch_reason(runtime, action_id))
    if reason in ("terminal", "canceled", "window_ended"):
        aborted = (await _abort_if_required(runtime, (await runtime.action(action_id))))
        return aborted if aborted is not None else BaselinePreparation(PreparationPhase.WAITING, wakeup_required=False)
    if reason is not None:
        return BaselinePreparation(PreparationPhase.WAITING, wakeup_required=False)
    baseline = await runtime.prepare_baseline(action_id)
    if baseline.phase not in (PreparationPhase.PENDING, PreparationPhase.WAITING):
        aborted = (await _abort_if_required(runtime, (await runtime.action(action_id))))
        if aborted is not None:
            return aborted
    if baseline.phase is PreparationPhase.FAILED:
        (await _fail_preparation(runtime, action_id, "baseline_read_failed", baseline.error))
    elif baseline.phase is PreparationPhase.ABORTED:
        raise ConsistencyError("目录准备已中止，但原动作没有取消、终态或窗口结束依据")
    return baseline


async def discard_terminal_preparation(runtime, action_id):
    cache = runtime.preparation_cache
    if cache is not None and (await runtime.action(action_id))["status"] in (3, 4, 5, 6) and not cache.pending(action_id):
        changed = cache.take_wakeup(action_id)
        cache.owners.pop(action_id, None)
        return changed
    return False


async def _wait_preparation_task(task):
    """重复取消只中断实际拥有者一次，连接等实际保存交付后才关闭。"""
    interrupted = None
    actual_error = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if interrupted is None:
                interrupted = error
                if not task.done():
                    task.cancel()
            if task.done():
                try:
                    task.result()
                except BaseException as failure:
                    actual_error = failure
                break
        except BaseException as error:
            actual_error = error
            break
    if interrupted is not None:
        cause = (actual_error.__cause__ if isinstance(actual_error, asyncio.CancelledError)
                 else actual_error)
        raise interrupted from cause
    if actual_error is not None:
        raise actual_error
