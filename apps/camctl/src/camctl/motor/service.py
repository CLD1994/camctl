"""电机动作的单次发送流程；数据库许可与管道效果分开保存。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId, new_operation_key
from camctl.motor.models import (
    FinishSendRequest, MotorFinalKind, PrepareOutcome, PrepareSendRequest, SendPermit,
)
from camctl.motor.notification import WriteKind, encode_motor_notification
from camctl.motor.rules import MotorDecision, MotorFacts, decide_motor, owns_send_permit
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.scheduling import ObserveWindowRequest
from camctl.scheduling.rules import LaunchWindow, expiration_reason
from camctl.session.clock import ClockBecameUntrusted, check_clock
from camctl.session.service import StateDbFailure


@dataclass
class MotorRuntime:
    owned: Any
    repository: Any
    writer: Any
    clock: Any
    clock_policy: Callable
    #: 当前会话仍能证明未进入 write 的许可；进入 write 前即移除。
    permits: dict[int, SendPermit]
    open_connection: Callable | None = None


def _completed(result, step: str):
    if result.kind is not DbOutcomeKind.COMPLETED:
        raise StateDbFailure(f"电机{step}未可靠完成（{result.kind.value}）: {result.error}")
    return result.value


def _save(runtime: MotorRuntime, method: str, request, step: str):
    key = new_operation_key()
    result = getattr(runtime.repository, method)(request, key, runtime.owned)
    if result.kind is DbOutcomeKind.UNKNOWN:
        if runtime.open_connection is None:
            raise StateDbFailure(f"电机{step}结果未知且没有重新核实连接")
        old, runtime.owned = runtime.owned, None
        try:
            old.connection.close()
            runtime.owned = runtime.open_connection()
            if (runtime.owned.connection is old.connection
                    or runtime.owned.metadata.instance_id != old.metadata.instance_id):
                raise ConsistencyError("重新核实连接不属于原状态库")
            result = runtime.repository.verify_operation(request, key, runtime.owned)
        except Exception as error:
            raise StateDbFailure(f"电机{step}的原操作核实失败: {error}") from error
    return _completed(result, step)


def _trusted_now(runtime: MotorRuntime) -> int:
    checked = check_clock(runtime.clock_policy(runtime.owned.connection), runtime.clock)
    if not checked.trusted:
        raise ClockBecameUntrusted("电机发送前的墙钟检查未通过")
    return checked.reading_micros


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


def _finish(runtime: MotorRuntime, request: FinishSendRequest) -> None:
    _save(runtime, "finish_send", request, "结果事务")
    runtime.permits.pop(request.action_id, None)


def advance_motor(action_id: int, runtime: MotorRuntime) -> MotorDecision:
    """推进一个可靠动作；许可保存到实际 write 之间没有协程让出点。

    只有当前流程首次取得许可才可能写管道。提交未知时用新连接核实
    原操作；意图核实不产生许可，结果核实不再次执行 write。无法
    可靠核实则停止会话，后续依据持久化终态或未知意图恢复。
    """
    facts = runtime.repository.read_facts(action_id, runtime.owned)
    if facts.action["status"] in (3, 4, 5, 6):
        runtime.permits.pop(action_id, None)
        return MotorDecision.KEEP_TERMINAL
    if facts.notification is not None and action_id not in runtime.permits:
        # 恢复未知不依据当前窗口或取消事实补造未发送结论。
        _finish(runtime, FinishSendRequest(action_id, runtime.clock.utc_micros(),
                                          MotorFinalKind.UNCONFIRMED))
        return MotorDecision.RECOVER_UNKNOWN

    fields = facts.action["input_fields_json"]
    if isinstance(fields, str):
        fields = parse_exact_json(fields)
    message = encode_motor_notification(ObjectId(action_id), fields["params"]["position"])
    now = _trusted_now(runtime)
    decision = _decision(facts, runtime, now)
    if decision in (MotorDecision.PREPARE, MotorDecision.CHANNEL_UNAVAILABLE):
        _save(runtime, "observe_window",
              ObserveWindowRequest(action_id, now, now), "窗口观察事务")
    if decision is MotorDecision.PREPARE:
        prepared = _save(runtime, "prepare_send",
                         PrepareSendRequest(action_id, now, now), "意图事务")
        if prepared.outcome is not PrepareOutcome.GRANTED:
            # 新事实由下一轮读取；ALREADY 永远不转换为本次发送许可。
            return MotorDecision.WAIT
        if prepared.permit is None:
            raise ConsistencyError("电机意图授予结果缺少本地许可")
        runtime.permits[action_id] = prepared.permit
        facts = runtime.repository.read_facts(action_id, runtime.owned)
        now = _trusted_now(runtime)
        decision = _decision(facts, runtime, now)

    if decision is MotorDecision.KEEP_TERMINAL:
        runtime.permits.pop(action_id, None)
        return decision
    permit = runtime.permits.get(action_id)
    if decision is MotorDecision.WAIT:
        return decision
    if decision is MotorDecision.EXPIRE:
        _finish(runtime, FinishSendRequest(action_id, now,
            MotorFinalKind.EXPIRED, permit=permit,
            expiration_reason=expiration_reason(facts.action["first_window_observed_at"])))
    elif decision is MotorDecision.CHANNEL_UNAVAILABLE:
        unavailable = runtime.writer.unavailability
        _finish(runtime, FinishSendRequest(action_id, now,
            MotorFinalKind.CHANNEL_UNAVAILABLE, permit=permit,
            reason=unavailable.reason, errno=unavailable.errno))
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
        _finish(runtime, request)
    else:
        raise ConsistencyError(f"电机流程出现不可解释决定: {decision}")
    return decision
