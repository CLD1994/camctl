"""后续动作触发的录像残留收场。

既往动作的停止预算耗尽后，设备活动缺少结束与释放依据，执行事实
原样保留。后续仍有效且已取得执行资格的拍摄动作要求相机空闲时，
先按需查询设备状态：可靠确认空闲则保存观察继续触发动作；可靠确
认仍在录制且属于已结束动作留下的已知残留录像时，为触发动作建立
独立的有限收场流程（STOP_RESIDUAL），按声明能力停止录像，预算默
认 3 次包含第一次、单独计数可配置，同一目标录像同时只保持一个流
程；观察到其他尚未结束动作的正常录像时等待，不授权停止。触发动
作取消或启动窗口耗尽后，未发出停止的流程保存结束原因，已发出的
按已保存意图继续使用剩余次数收场。没有新的、需要相机空闲的有效
动作时，不为残留录像启动后台周期收场（camera-recovery.md#后续
动作触发的残留收场）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Mapping

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import binding_failure_details
from camctl.devices.ports import ControlRequest
from camctl.operations.attempts import (
    AttemptIntent,
    AttemptTarget,
    BeginDisposition,
    OperationKind,
    QueryPurpose,
    RunOutcome,
    StaleRunFinish,
)
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.scheduling import ExpireActionRequest
from camctl.persistence.repositories.capture import FinishResidualBindingFailure, RecordingFailure
from camctl.scheduling.rules import WindowPhase, window_phase
from camctl.persistence.repositories.operations import (
    _ATTEMPT_STATUS, _EFFECT_STATE,
)
from camctl.session.service import StateDbFailure

__all__ = [
    "ResidualCandidate",
    "pass_residual_gate",
    "residual_flow",
]

#: 触发动作的终态状态值（成功、失败、过期、取消）。
_TRIGGER_TERMINAL = (3, 4, 5, 6)

#: 查询与收场流程的整数登记（operation_runs.kind）。
_QUERY_KIND = OperationKind.QUERY_ACTIVITY
_RESIDUAL_KIND = OperationKind.STOP_RESIDUAL
_NO_TARGET = AttemptTarget()


@dataclass(frozen=True)
class ResidualCandidate:
    """一个已知残留录像的定位事实：活动、归属动作与停止能力。"""

    activity_id: int
    action_id: int
    stop_supported: int
    safe_repeat_stop: int

    @property
    def stop_capable(self) -> bool:
        """目标录像声明支持停止且允许安全重复停止。"""

        return self.stop_supported == 1 and self.safe_repeat_stop == 1


def residual_candidates(
    connection: Any, device_id: str
) -> tuple[ResidualCandidate, ...]:
    """读取设备上的残留候选：终态动作仍保持占用与执行中的活动。"""

    with closing(connection.execute(
        "SELECT da.id, da.action_id, da.stop_supported, da.safe_repeat_stop"
        " FROM device_activities da"
        " JOIN actions a ON a.id = da.action_id"
        " WHERE a.device_id = ? AND da.occupancy_state = 1"
        " AND da.activity_state = 2 AND a.status IN (3, 4, 5, 6)"
        " ORDER BY da.id",
        (device_id,),
    )) as cursor:
        return tuple(
            ResidualCandidate(
                activity_id=int(row[0]), action_id=int(row[1]),
                stop_supported=int(row[2]), safe_repeat_stop=int(row[3]))
            for row in cursor.fetchall())


def _winddown_key(trigger_id: int, activity_id: int) -> str:
    return f"followup/{trigger_id}/{activity_id}"


def _preflight_key(trigger_id: int) -> str:
    return f"query/preflight/{trigger_id}"


def _confirm_key(trigger_id: int, activity_id: int) -> str:
    return f"query/residual/{trigger_id}/{activity_id}"


def _run_row(connection: Any, responsibility: str):
    """读取责任流程行的主键、状态、累计次数与等待标志。"""

    with closing(connection.execute(
        "SELECT id, status, attempts_used, retry_wait_required"
        " FROM operation_runs WHERE responsibility_key = ?",
        (responsibility,),
    )) as cursor:
        return cursor.fetchone()


def _unfinished_winddown(connection: Any, activity_id: int):
    """目标录像未收口的收场流程行：主键、触发动作与累计次数。"""

    with closing(connection.execute(
        "SELECT id, action_id, attempts_used FROM operation_runs"
        " WHERE kind = 8 AND activity_id = ? AND status IN (1, 2)"
        " ORDER BY id",
        (activity_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1]), int(row[2])


def _succeeded_winddown(connection: Any, activity_id: int) -> bool:
    """目标录像是否已有成功终态的收场流程（可靠停止事实）。"""

    with closing(connection.execute(
        "SELECT 1 FROM operation_runs"
        " WHERE kind = 8 AND activity_id = ? AND status = 3",
        (activity_id,),
    )) as cursor:
        return cursor.fetchone() is not None


def _last_attempt(connection: Any, responsibility: str):
    """责任最近尝试的状态与效果；无尝试返回 None。"""

    with closing(connection.execute(
        "SELECT a.status, a.effect_state FROM operation_attempts a"
        " JOIN operation_runs r ON a.run_id = r.id"
        " WHERE r.responsibility_key = ? ORDER BY a.id DESC LIMIT 1",
        (responsibility,),
    )) as cursor:
        return cursor.fetchone()


def _last_observations(connection: Any, responsibility: str) -> tuple:
    """责任最近尝试保存的设备观察（结果事实内的序列化列表）。"""

    with closing(connection.execute(
        "SELECT a.result_json FROM operation_attempts a"
        " JOIN operation_runs r ON a.run_id = r.id"
        " WHERE r.responsibility_key = ? ORDER BY a.id DESC LIMIT 1",
        (responsibility,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], str):
        return ()
    result = json.loads(row[0])
    observations = result.get("observations")
    return tuple(observations) if isinstance(observations, list) else ()


def _trigger_idle_confirmed(connection: Any, trigger_id: int) -> bool:
    """触发动作的执行前检查是否已可靠确认设备空闲。

    取得空闲判定后执行前检查责任结束；重复检查沿用原结果，不新建
    预算（operation-fields.md#活动查询的责任范围）。
    """

    responsibility = _preflight_key(trigger_id)
    row = _run_row(connection, responsibility)
    if row is None or int(row[1]) != 3:
        return False
    attempt = _last_attempt(connection, responsibility)
    if attempt is None or int(attempt[0]) != int(_ATTEMPT_STATUS.SUCCEEDED):
        return False
    # 空闲判定=成功结束的检查且最近观察为空（携带活动观察表示设备
    # 正执行某个活动，不是空闲）。
    return not _last_observations(connection, responsibility)


def _observed_activity(response: Any) -> int | None:
    """从查询观察读取进行中活动的身份；空观察返回 None。"""

    for observation in response.observations:
        if observation.type != "activity_status":
            continue
        identity = observation.data.get("activity_id")
        try:
            return int(identity)
        except (TypeError, ValueError):
            return -1
    return None


def _original_binding_failure(runtime: Any, action: Mapping[str, Any]):
    """核对保存的设备及驱动；没有绑定端口的调用方提供匹配前提。"""
    from camctl.capture.handlers import _binding

    if runtime.binding_check is None:
        return None
    return binding_failure_details(runtime.binding_check(_binding(action)))


def _finish_original_binding_failure(
    runtime: Any, candidate: ResidualCandidate, details: Mapping[str, Any],
    trigger_id: int | None = None,
) -> bool:
    """原调用已结束时，结束固定残留责任及适用的新触发动作。"""
    with closing(runtime.owned.connection.execute(
        "SELECT responsibility_key FROM operation_runs WHERE activity_id = ?"
        " AND status IN (1, 2) AND (kind = 8 OR (kind = 6 AND query_purpose = 5))"
        " ORDER BY id", (candidate.activity_id,),
    )) as cursor:
        keys = tuple(row[0] for row in cursor.fetchall())
    if not keys:
        return False
    with closing(runtime.owned.connection.execute(
        "SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id = a.run_id"
        " WHERE r.activity_id = ? AND a.status = 1"
        " AND (r.kind = 8 OR (r.kind = 6 AND r.query_purpose = 5)) LIMIT 1",
        (candidate.activity_id,),
    )) as cursor:
        if cursor.fetchone() is not None:
            return False
    outcome = runtime.capture.finish_residual_binding_failure(
        FinishResidualBindingFailure(
            trigger_id, candidate.activity_id, runtime.wall_us(),
            RecordingFailure("device_binding_unavailable", details), keys),
        new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"原残留绑定失败事务未完成（{outcome.kind.value}）: {outcome.error}")
    return True


async def pass_residual_gate(runtime: Any, trigger: Mapping[str, Any]) -> bool:
    """首次控制前检查当前条件：True 继续争取启动，False 不派发。

    无残留候选或已有可靠空闲判定与成功收场时放行；确认已知残留则
    建立收场流程并发送第一次停止；其他有效活动占用、查询不可用或
    收场未完成时等待。尚未派发的 PENDING 或 RUNNING 动作均遵守
    当前窗口与取消规则，窗口耗尽时按已有过期事务保存终态。
    """

    from camctl.capture.handlers import _conclude_activity

    if trigger["status"] not in (1, 2) or trigger["cancel_requested"]:
        return False
    now = runtime.wall_us()
    phase = window_phase(runtime.window_of(trigger), now)
    if phase is WindowPhase.AFTER_WINDOW:
        outcome = runtime.scheduling.expire_action(
            ExpireActionRequest(
                action_id=trigger["id"], trusted_wall_now=now, occurred_at=now),
            new_operation_key(), runtime.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise RuntimeError(
                f"动作过期事务未完成（{outcome.kind.value}）: {outcome.error}")
        return False
    if phase is WindowPhase.BEFORE_START:
        return False
    if _original_binding_failure(runtime, trigger) is not None:
        # 普通动作先取得本地执行身份，再由其绑定失败事务结束。
        return True
    candidates = residual_candidates(
        runtime.owned.connection, trigger["device_id"])
    if not candidates:
        return True
    if len(candidates) > 1:
        raise RuntimeError(
            f"同设备出现多个残留候选: {trigger['device_id']} "
            + repr([c.activity_id for c in candidates]))
    candidate = candidates[0]
    flow = _unfinished_winddown(runtime.owned.connection, candidate.activity_id)
    if flow is None and _succeeded_winddown(runtime.owned.connection, candidate.activity_id):
        # 成功流程已存在而活动未收口：补齐收场后放行（中断恢复）。
        _conclude_activity(runtime, candidate.action_id)
        return not residual_candidates(
            runtime.owned.connection, trigger["device_id"])
    if flow is None and _trigger_idle_confirmed(runtime.owned.connection, trigger["id"]):
        return True
    details = _original_binding_failure(runtime, runtime.action(candidate.action_id))
    if flow is not None and details is not None:
        if trigger["status"] == 1:
            # ACTION_STARTED 是执行失败的前提，此处只放行本地开始。
            return True
        _finish_original_binding_failure(runtime, candidate, details, trigger["id"])
        return False
    if flow is not None:
        await _advance_winddown(runtime, candidate, flow)
        return False
    if details is not None:
        return False
    return await _preflight_check(runtime, trigger, candidate)


async def _preflight_check(
    runtime: Any, trigger: Mapping[str, Any], candidate: ResidualCandidate
) -> bool:
    """执行一次执行前检查并按观察分区；可靠空闲时返回 True。"""

    from camctl.capture.handlers import _binding, _operation_outcome

    if runtime.state_query is None:
        # 驱动未声明查询能力：不推测设备空闲，触发动作按自身窗口与
        # 取消规则收尾。
        return False
    responsibility = _preflight_key(trigger["id"])
    if runtime.retry_wait_remaining(
            responsibility,
            runtime.query_config.retry_interval_s) is not None:
        return False
    ticket = _begin_query_attempt(
        runtime, responsibility, trigger["id"], _NO_TARGET,
        QueryPurpose.BEFORE_EXECUTION)
    if ticket is None:
        return False
    response = await runtime.state_query.query_state(ControlRequest(
        operation="query", binding=_binding(trigger),
        params=trigger["effective_params_json"], ticket=ticket,
        timeout_s=runtime.query_config.timeout_s))
    outcome, _ = _operation_outcome(
        response, "activity_status", evidence_type="query_returned")
    observed = _observed_activity(response)
    if response.error is None and observed is None:
        # 可靠确认空闲：保存观察，执行前检查责任结束，触发动作继续。
        runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED)
        return True
    if response.error is None and observed == candidate.activity_id:
        # 确认目标残留仍在录制：检查责任结束，建立收场流程并首停。
        runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED)
        if candidate.stop_capable:
            await _stop_residual(runtime, trigger["id"], candidate)
        return False
    # 其他有效活动占用或调用错误：责任未满足，按间隔再次检查。
    runtime.finish(ticket, outcome, retry_wait=True)
    return False


def _begin_query_attempt(
    runtime: Any, responsibility: str, action_id: int, target: AttemptTarget,
    purpose: QueryPurpose,
):
    """提交查询意图并返回票据；预算耗尽或等待未到返回 None。"""

    intent = AttemptIntent(
        operation="query",
        action_id=action_id,
        kind=_QUERY_KIND,
        target=target,
        query_purpose=purpose,
        config=runtime.query_config,
        occurred_at=runtime.wall_us(),
    )
    outcome = runtime.operations.begin_attempt(
        intent, new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise RuntimeError(
            f"查询意图事务未完成（{outcome.kind.value}）: {outcome.error}")
    if outcome.value.disposition is not BeginDisposition.GRANTED:
        if outcome.value.reason == "budget_exhausted":
            finished = runtime.operations.finish_stale_runs(
                StaleRunFinish(
                    responsibility_keys=(responsibility,),
                    status=RunOutcome.UNCONFIRMED,
                    error=ErrorValue(code="result_unconfirmed", stage="device"),
                    occurred_at=runtime.wall_us()),
                new_operation_key(), runtime.owned)
            if finished.kind is not DbOutcomeKind.COMPLETED:
                raise ConsistencyError(f"查询预算耗尽的结果未保存: {finished.error}")
        return None
    return outcome.value.ticket


async def _advance_winddown(
    runtime: Any, candidate: ResidualCandidate,
    flow: tuple[int, int, int],
) -> None:
    """把未收口的收场流程推进一个可执行步骤。

    停止发出未确认且无调用错误时，先用确认查询核实：可靠空闲即按
    停止事实收场流程与活动；仍在录制、查询不可用或预算耗尽时按声
    明的安全重复停止能力继续停止。
    """

    run_id, trigger_id, _ = flow
    with closing(runtime.owned.connection.execute(
        "SELECT action_id, activity_id, status, attempts_used"
        " FROM operation_runs WHERE id = ? AND kind = 8", (run_id,),
    )) as cursor:
        current = cursor.fetchone()
    if current is None or current[:2] != (trigger_id, candidate.activity_id):
        raise ConsistencyError("残留收场流程与原触发动作或目标活动不符")
    if current[2] not in (1, 2):
        return
    responsibility = _winddown_key(trigger_id, candidate.activity_id)
    trigger = runtime.action(trigger_id)
    expired = window_phase(runtime.window_of(trigger), runtime.wall_us()) is WindowPhase.AFTER_WINDOW
    if current[3] == 0 and (trigger["cancel_requested"]
                           or trigger["status"] in _TRIGGER_TERMINAL or expired):
        result = runtime.operations.finish_stale_runs(
            StaleRunFinish(
                responsibility_keys=(responsibility,),
                status=(RunOutcome.EXPIRED if not trigger["cancel_requested"]
                        and (trigger["status"] == 5 or expired) else RunOutcome.CANCELED),
                occurred_at=runtime.wall_us()), new_operation_key(), runtime.owned)
        if result.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"未派发残留收场的结束事实未保存: {result.error}")
        return
    attempt = _last_attempt(runtime.owned.connection, responsibility)
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.RUNNING):
        return
    details = _original_binding_failure(runtime, runtime.action(candidate.action_id))
    if details is not None:
        _finish_original_binding_failure(runtime, candidate, details)
        return
    unconfirmed = (
        attempt is not None
        and int(attempt[0]) == int(_ATTEMPT_STATUS.SUCCEEDED)
        and int(attempt[1]) == int(_EFFECT_STATE.UNKNOWN))
    if unconfirmed and await _confirm_by_query(
            runtime, trigger_id, candidate):
        return
    if runtime.retry_wait_remaining(
            responsibility,
            runtime.residual_config.retry_interval_s) is not None:
        return
    await _stop_residual(runtime, trigger_id, candidate)


async def _stop_residual(
    runtime: Any, trigger_id: int, candidate: ResidualCandidate,
) -> None:
    """按收场预算发送一次停止调用并保存尝试结果。

    意图提交创建流程（首停即第一次尝试）；可靠确认结束流程并收场
    活动；错误或未确认保持流程执行中并建立重试等待；预算耗尽按
    recording_stop_failed 失败终态化，残留事实与占用原样保留。
    """

    from camctl.capture.handlers import (
        _binding, _conclude_activity, _operation_outcome,
    )

    responsibility = _winddown_key(trigger_id, candidate.activity_id)
    existing = _run_row(runtime.owned.connection, responsibility)
    if existing is None or existing[2] == 0:
        # 首次停止前重新核对当前触发资格；查询等待期间可能已经取消或超窗。
        trigger = runtime.action(trigger_id)
        if (trigger["cancel_requested"] or trigger["status"] in _TRIGGER_TERMINAL
                or window_phase(runtime.window_of(trigger), runtime.wall_us())
                is not WindowPhase.IN_WINDOW):
            return
    intent = AttemptIntent(
        operation="stop",
        action_id=trigger_id,
        kind=_RESIDUAL_KIND,
        target=AttemptTarget(activity_id=candidate.activity_id),
        query_purpose=None,
        config=runtime.residual_config,
        occurred_at=runtime.wall_us(),
    )
    begin = runtime.operations.begin_attempt(
        intent, new_operation_key(), runtime.owned)
    if begin.kind is not DbOutcomeKind.COMPLETED:
        raise RuntimeError(
            f"残留收场意图事务未完成（{begin.kind.value}）: {begin.error}")
    if begin.value.disposition is not BeginDisposition.GRANTED:
        if begin.value.reason == "budget_exhausted":
            _finish_winddown_exhausted(runtime, responsibility, candidate)
        return
    ticket = begin.value.ticket
    owner = runtime.action(candidate.action_id)
    response = await runtime.stopper.stop(ControlRequest(
        operation="stop_recording", binding=_binding(owner),
        params=owner["effective_params_json"], ticket=ticket,
        timeout_s=runtime.residual_config.timeout_s))
    outcome, confirmed = _operation_outcome(
        response, "stop_confirmed", evidence_type="stop_returned")
    if confirmed:
        runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED)
        _conclude_activity(runtime, candidate.action_id)
    else:
        runtime.finish(ticket, outcome, retry_wait=True)


def _finish_winddown_exhausted(
    runtime: Any, responsibility: str, candidate: ResidualCandidate,
) -> None:
    """停止预算耗尽的收场流程失败终态化。"""

    row = _run_row(runtime.owned.connection, responsibility)
    if row is None:
        return
    receipt = runtime.operations.finish_stale_runs(
        StaleRunFinish(
            responsibility_keys=(responsibility,),
            status=RunOutcome.FAILED,
            error=_stop_error(candidate, int(row[0])),
            occurred_at=runtime.wall_us()),
        new_operation_key(), runtime.owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _stop_error(candidate: ResidualCandidate, run_id: int):
    from camctl.operations.models import ErrorValue

    return ErrorValue(
        code="recording_stop_failed", stage="device_stop",
        details={"activity_id": str(candidate.activity_id),
                 "operation_run_id": str(run_id)})


async def _confirm_by_query(
    runtime: Any, trigger_id: int, candidate: ResidualCandidate,
) -> bool:
    """残留收场确认查询；本轮已由查询处理时返回 True。

    可靠空闲时按停止事实收场流程与活动并返回 True。查询端点未声
    明、预算耗尽、最近查询失败或仍在录制时不由查询收场（返回
    False 或 True 交由调用方按剩余预算继续停止）。
    """

    from camctl.capture.handlers import (
        _binding, _conclude_activity, _operation_outcome,
    )

    if runtime.state_query is None:
        return False
    responsibility = _confirm_key(trigger_id, candidate.activity_id)
    row = _run_row(runtime.owned.connection, responsibility)
    if row is not None and int(row[1]) not in (1, 2):
        # 查询责任已终态：继续停止路径。
        return False
    attempt = _last_attempt(runtime.owned.connection, responsibility)
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.RUNNING):
        return True
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.FAILED):
        # 查询调用失败：不反复查询，按重复停止能力继续停止。
        return False
    if runtime.retry_wait_remaining(
            responsibility,
            runtime.query_config.retry_interval_s) is not None:
        return True
    ticket = _begin_query_attempt(
        runtime, responsibility, trigger_id,
        AttemptTarget(activity_id=candidate.activity_id),
        QueryPurpose.RESIDUAL_STOP_CONFIRMATION)
    if ticket is None:
        return False
    owner = runtime.action(candidate.action_id)
    response = await runtime.state_query.query_state(ControlRequest(
        operation="query", binding=_binding(owner),
        params=owner["effective_params_json"], ticket=ticket,
        timeout_s=runtime.query_config.timeout_s))
    outcome, _ = _operation_outcome(
        response, "activity_status", evidence_type="query_returned")
    observed = _observed_activity(response)
    if response.error is None and observed is None:
        # 可靠确认空闲：目标录像已不再执行，查询责任结束，按停止事
        # 实收场流程与活动。
        runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED)
        receipt = runtime.operations.finish_stale_runs(
            StaleRunFinish(
                responsibility_keys=(
                    _winddown_key(trigger_id, candidate.activity_id),),
                status=RunOutcome.SUCCEEDED,
                occurred_at=runtime.wall_us()),
            new_operation_key(), runtime.owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        _conclude_activity(runtime, candidate.action_id)
        return True
    # 仍在录制、其他活动或调用错误：保存事实，继续停止路径。
    runtime.finish(ticket, outcome, retry_wait=True)
    return True


def residual_flow(capture_factory: Any, *, resume_media_results=None,
                   resume_file_observations=None, resume_read_results=None) -> Any:
    """构造无人驱动的残留收场推进流程。

    触发动作取消或启动窗口耗尽后，已建立的收场流程不再由执行前检
    查门驱动：未发出停止的流程保存结束原因，已发出的按已保存意图
    继续使用剩余次数收场（camera-recovery.md#触发动作结束时的收
    场）。执行前检查随触发资格结束；确认查询随原残留停止责任结束。
    """

    async def flow(context: Any) -> None:
        from camctl.persistence.repositories.operations import (
            OperationRepository,
        )

        owned = context.open_connection()
        try:
            try:
                if resume_read_results is not None:
                    resume_read_results(owned)
                if resume_file_observations is not None:
                    resume_file_observations(owned)
                if resume_media_results is not None:
                    await resume_media_results(owned)
            except (sqlite3.Error, ConsistencyError) as error:
                raise StateDbFailure(f"原文件事实未可靠保存: {error}") from error
            await _recover_old_attempts(owned, capture_factory)
            _settle_orphan_queries(context, owned, OperationRepository())
            with closing(owned.connection.execute(
                "SELECT id, action_id, activity_id, attempts_used"
                " FROM operation_runs WHERE kind = 8"
                " AND status IN (1, 2) ORDER BY id",
            )) as cursor:
                flows = [
                    (int(row[0]), int(row[1]), int(row[2]), int(row[3]))
                    for row in cursor.fetchall()]
            for (run_id, trigger_id, activity_id, used) in flows:
                with closing(owned.connection.execute(
                    "SELECT a.status, da.action_id, da.stop_supported,"
                    " da.safe_repeat_stop, a2.device_id"
                    " FROM operation_runs r"
                    " JOIN actions a ON a.id = r.action_id"
                    " JOIN device_activities da ON da.id = r.activity_id"
                    " JOIN actions a2 ON a2.id = da.action_id"
                    " WHERE r.id = ?", (run_id,),
                )) as cursor:
                    facts = cursor.fetchone()
                if facts is None:
                    raise ConsistencyError(f"残留收场关联事实缺失: {run_id}")
                candidate = ResidualCandidate(
                    activity_id=activity_id, action_id=int(facts[1]),
                    stop_supported=int(facts[2]), safe_repeat_stop=int(facts[3]))
                runtime = capture_factory(owned, facts[4])
                if runtime is None:
                    continue
                await _advance_winddown(
                    runtime, candidate, (run_id, trigger_id, used))
        finally:
            owned.connection.close()

    return flow


async def _recover_old_attempts(owned: Any, capture_factory: Any) -> None:
    """分批结束具有可靠旧边界的原调用，动作及流程终态保持。"""
    from camctl.capture.handlers import _RECOVERABLE_OPERATIONS
    from camctl.operations.models import AttemptTicket

    batch_size = 128
    last_id = 0
    kinds = tuple(_RECOVERABLE_OPERATIONS)
    marks = ",".join("?" for _ in kinds)
    while True:
        with closing(owned.connection.execute(
            "SELECT t.id, t.attempt_no, r.id, r.kind, r.activity_id, r.responsibility_key, a.device_id"
            " FROM operation_attempts t JOIN operation_runs r ON r.id = t.run_id"
            " LEFT JOIN actions a ON a.id = r.action_id"
            f" WHERE t.status = ? AND t.id > ? AND r.kind IN ({marks})"
            " ORDER BY t.id LIMIT ?",
            (int(_ATTEMPT_STATUS.RUNNING), last_id, *kinds, batch_size),
        )) as cursor:
            rows = cursor.fetchall()
        if not rows:
            return
        runtimes = {}
        for identity, attempt_no, run_id, kind, activity_id, responsibility, device_id in rows:
            last_id = identity
            if not isinstance(device_id, str) or not device_id:
                raise ConsistencyError("拍摄旧调用缺少所属设备身份")
            if device_id not in runtimes:
                runtimes[device_id] = capture_factory(owned, device_id)
            runtime = runtimes[device_id]
            if runtime is None:
                continue
            ticket = AttemptTicket(
                attempt_no, _RECOVERABLE_OPERATIONS[kind],
                None if activity_id is None else str(activity_id), responsibility, run_id)
            runtime.recover_attempt(ticket)
        if len(rows) < batch_size:
            return
        await asyncio.sleep(0)


def _settle_orphan_queries(context: Any, owned: Any, operations: Any) -> None:
    """按查询用途和原停止责任结算，不以触发者终态覆盖必要核实。"""

    with closing(owned.connection.execute(
        "SELECT r.responsibility_key, r.query_purpose, a.status, a.cancel_requested,"
        " stop.id, stop.status, stop.attempts_used FROM operation_runs r"
        " JOIN actions a ON a.id = r.action_id"
        " LEFT JOIN operation_runs stop ON stop.kind = 8"
        " AND stop.action_id = r.action_id AND stop.activity_id = r.activity_id"
        " WHERE r.kind = 6 AND r.status IN (1, 2)"
        " AND r.query_purpose IN (1, 5)",
    )) as cursor:
        rows = cursor.fetchall()
    for responsibility, purpose, status, canceled, stop_id, stop_status, used in rows:
        if purpose == 1:
            if not canceled and status not in _TRIGGER_TERMINAL:
                continue
            final = RunOutcome.CANCELED
        else:
            if stop_id is None:
                raise ConsistencyError(f"残留确认查询缺少原停止责任: {responsibility}")
            if stop_status in (1, 2):
                if used > 0 or (not canceled and status not in _TRIGGER_TERMINAL):
                    continue
                final = RunOutcome.CANCELED
            else:
                final = RunOutcome.SUCCEEDED if stop_status == 3 else RunOutcome.CANCELED
        outcome = operations.finish_stale_runs(
            StaleRunFinish(
                responsibility_keys=(responsibility,),
                status=final,
                occurred_at=context.clock.utc_micros()),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
