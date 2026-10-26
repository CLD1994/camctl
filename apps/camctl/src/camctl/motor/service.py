"""电机动作的单次发送流程；数据库许可与管道效果分开保存。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, new_operation_key
from camctl.logging_runtime.diagnostics import DiagnosticState, record_error
from camctl.operations.models import ErrorValue
from camctl.contracts.workflow_errors import registered_error
from camctl.motor.models import (
    FinishSendRequest, MotorFinalKind, PrepareOutcome, PrepareSendRequest, SendPermit,
)
from camctl.motor.notification import WriteKind, encode_motor_notification
from camctl.motor.rules import MotorDecision, MotorFacts, decide_motor, owns_send_permit
from camctl.persistence.business import BusinessDatabase, settle_required
from camctl.persistence.models import DbOutcomeKind, db_failure, db_outcome_errors, db_outcome_for
from camctl.persistence.repositories.scheduling import ObserveWindowRequest
from camctl.scheduling.rules import LaunchWindow, expiration_reason
from camctl.session.clock import ClockBecameUntrusted, check_clock
from camctl.session.service import StateDbFailure


@dataclass
class MotorRuntime:
    database: BusinessDatabase
    repository: Any
    writer: Any
    clock: Any
    clock_policy: Callable
    #: 当前会话仍能证明未进入 write 的许可；进入 write 前即移除。
    permits: dict[int, SendPermit]


@dataclass
class PendingMotorSave:
    """本次原保存与核实共用身份，两个实际错误分别持有诊断状态。"""

    method: str
    request: Any
    key: OperationKey
    write_diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)
    verification_diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


def _has_actual_error(error) -> bool:
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        elif isinstance(current, Exception):
            return True
        pending.extend(value for value in (current.__cause__, current.__context__) if value is not None)
    return False


def _database_failure(pending, result, step, *, verification=False):
    detail = ("包含实际错误" if result.kind is DbOutcomeKind.COMPLETED else "未可靠完成")
    failure = db_failure(StateDbFailure,
        f"电机{step}{detail}（{result.kind.value}）: {result.error}", result)
    if any(_has_actual_error(error) for error in db_outcome_errors(result)):
        record_error(failure, stage="motor.verification" if verification else "motor.save",
                     context={"action_id": pending.request.action_id,
                              "method": pending.method, "operation_key": pending.key},
                     state=pending.verification_diagnostic if verification else pending.write_diagnostic,
                     details={"database_outcome": result.kind, "database_stage": result.stage})
    return failure


def _completed(result, step: str, pending, *, verification=False):
    if result.kind is not DbOutcomeKind.COMPLETED:
        raise _database_failure(pending, result, step, verification=verification)
    if db_outcome_errors(result):
        _database_failure(pending, result, step, verification=verification)
    return result.value


async def _save(runtime: MotorRuntime, method: str, request, step: str, *, apply=None):
    pending = PendingMotorSave(method, request, new_operation_key())

    async def save_and_apply():
        try:
            result = await runtime.database.write(
                lambda owned: getattr(runtime.repository, method)(pending.request, pending.key, owned),
                key=pending.key, description=f"电机{step}")
        except BaseException as error:
            actual = db_outcome_for(error)
            if actual is not None and any(_has_actual_error(value) for value in db_outcome_errors(actual)):
                _database_failure(pending, actual, step)
                record_error(error, stage="motor.save", state=pending.write_diagnostic)
            raise
        verified = result.kind is DbOutcomeKind.UNKNOWN
        if verified:
            # 原提交实际错误在核实前冻结，后续回执不能覆盖原诊断。
            _database_failure(pending, result, step)
            try:
                result = await runtime.database.read(
                    lambda owned: runtime.repository.verify_operation(pending.request, pending.key, owned),
                    description=f"电机{step}原操作核实")
            except Exception as error:
                failure = StateDbFailure(f"电机{step}的原操作核实失败: {error}")
                failure.__cause__ = error
                record_error(failure, stage="motor.verification",
                             context={"action_id": pending.request.action_id,
                                      "method": pending.method, "operation_key": pending.key},
                             state=pending.verification_diagnostic)
                raise failure
        value = _completed(result, step, pending, verification=verified)
        if apply is not None:
            apply(value)
        return value

    return await settle_required(save_and_apply())


def _trusted_now(runtime: MotorRuntime, clock_input) -> int:
    checked = check_clock(clock_input, runtime.clock)
    if not checked.trusted:
        raise ClockBecameUntrusted("电机发送前的墙钟检查未通过")
    return checked.reading_micros


async def _read_send_basis(runtime: MotorRuntime, action_id: int):
    owns_permit = action_id in runtime.permits

    def read(owned):
        facts = runtime.repository.read_facts(action_id, owned)
        if (facts.action["status"] in (3, 4, 5, 6)
                or (facts.notification is not None and not owns_permit)):
            return facts, None
        return facts, runtime.clock_policy(owned.connection)

    return await runtime.database.read(
        read, description="电机发送资格读取")


def _grant_permit(runtime: MotorRuntime, action_id: int, prepared) -> None:
    if prepared.outcome is PrepareOutcome.GRANTED:
        if prepared.permit is None:
            raise ConsistencyError("电机意图授予结果缺少本地许可")
        runtime.permits[action_id] = prepared.permit


def _decision(facts, runtime: MotorRuntime, now: int) -> MotorDecision:
    action = facts.action
    permit = runtime.permits.get(action["id"])
    if action["status"] in (3, 4, 5, 6):
        return MotorDecision.KEEP_TERMINAL
    if permit is not None and not owns_send_permit(facts, permit):
        raise ConsistencyError("电机本地许可与持久化发送意图不符")
    window = LaunchWindow(action["scheduled_at"],
        action["scheduled_at"] + action["max_delay_ms"] * 1000)
    return decide_motor(MotorFacts(
        action["status"] in (3, 4, 5, 6), facts.notification is not None,
        permit is not None, now,
        runtime.writer.available, window))


async def _finish(runtime: MotorRuntime, request: FinishSendRequest, *, notification=None) -> None:
    code = {
        MotorFinalKind.FAILED: "motor_notification_failed",
        MotorFinalKind.CHANNEL_UNAVAILABLE: "motor_channel_unavailable",
        MotorFinalKind.UNCONFIRMED: "motor_notification_unconfirmed",
    }.get(request.kind)
    if code is not None:
        details = {}
        if request.kind is not MotorFinalKind.UNCONFIRMED:
            details["reason"] = request.reason
        if request.written_bytes is not None:
            details["written_bytes"] = request.written_bytes
        if request.errno is not None:
            details["errno"] = request.errno
        stage = registered_error(code)["stage"]
        record_error(ErrorValue(code, stage, details), stage=stage,
                     context={"action_id": request.action_id},
                     details={"send_result": request, "notification": notification})
    await _save(runtime, "finish_send", request, "结果事务",
                apply=lambda _value: runtime.permits.pop(request.action_id, None))


async def advance_motor(action_id: int, runtime: MotorRuntime) -> MotorDecision:
    """推进一个可靠动作；最终资格交付到实际发送之间没有协程让出点。

    只有当前流程首次取得许可才可能写管道。提交未知时用新连接核实
    原操作；意图核实不产生许可，结果核实不再次执行 write。无法
    可靠核实则停止会话，后续依据持久化终态或未知意图恢复。
    """
    facts, clock_input = await _read_send_basis(runtime, action_id)
    if facts.action["status"] in (3, 4, 5, 6):
        runtime.permits.pop(action_id, None)
        return MotorDecision.KEEP_TERMINAL
    if facts.notification is not None and action_id not in runtime.permits:
        # 恢复未知不依据当前窗口或取消事实补造未发送结论。
        await _finish(runtime, FinishSendRequest(action_id, runtime.clock.utc_micros(),
                                          MotorFinalKind.UNCONFIRMED))
        return MotorDecision.RECOVER_UNKNOWN

    fields = facts.action["input_fields_json"]
    if isinstance(fields, str):
        fields = parse_exact_json(fields)
    message = encode_motor_notification(ObjectId(action_id), fields["params"]["position"])
    now = _trusted_now(runtime, clock_input)
    decision = _decision(facts, runtime, now)
    if decision in (MotorDecision.PREPARE, MotorDecision.CHANNEL_UNAVAILABLE):
        await _save(runtime, "observe_window",
              ObserveWindowRequest(action_id, now, now), "窗口观察事务")
    if decision is MotorDecision.PREPARE:
        prepared = await _save(runtime, "prepare_send",
                         PrepareSendRequest(action_id, now, now), "意图事务",
                         apply=lambda value: _grant_permit(runtime, action_id, value))
        if prepared.outcome is not PrepareOutcome.GRANTED:
            # 新事实由下一轮读取；ALREADY 永远不转换为本次发送许可。
            return MotorDecision.WAIT
        facts, clock_input = await _read_send_basis(runtime, action_id)
        if facts.action["status"] in (3, 4, 5, 6):
            runtime.permits.pop(action_id, None)
            return MotorDecision.KEEP_TERMINAL
        now = _trusted_now(runtime, clock_input)
        decision = _decision(facts, runtime, now)

    if decision is MotorDecision.KEEP_TERMINAL:
        runtime.permits.pop(action_id, None)
        return decision
    permit = runtime.permits.get(action_id)
    if decision is MotorDecision.WAIT:
        return decision
    if decision is MotorDecision.EXPIRE:
        await _finish(runtime, FinishSendRequest(action_id, now,
            MotorFinalKind.EXPIRED, permit=permit,
            expiration_reason=expiration_reason(facts.action["first_window_observed_at"])))
    elif decision is MotorDecision.CHANNEL_UNAVAILABLE:
        unavailable = runtime.writer.unavailability
        await _finish(runtime, FinishSendRequest(action_id, now,
            MotorFinalKind.CHANNEL_UNAVAILABLE, permit=permit,
            reason=unavailable.reason, errno=unavailable.errno), notification=unavailable)
    elif decision is MotorDecision.SEND:
        # 从这里起不能再证明“未开始发送”，即使写入或结果保存抛异常。
        runtime.permits.pop(action_id)
        result = runtime.writer.send(message)
        if result.kind is WriteKind.UNAVAILABLE:
            kind = MotorFinalKind.CHANNEL_UNAVAILABLE
            request = FinishSendRequest(action_id, now, kind, permit=permit,
                                        reason=result.reason, errno=result.errno)
        else:
            kind = (MotorFinalKind.WRITTEN if result.kind is WriteKind.WRITTEN
                    else MotorFinalKind.FAILED)
            request = FinishSendRequest(action_id, now, kind, permit=permit,
                written_bytes=result.written_bytes, errno=result.errno, reason=result.reason)
        await _finish(runtime, request, notification=result)
    else:
        raise ConsistencyError(f"电机流程出现不可解释决定: {decision}")
    return decision
