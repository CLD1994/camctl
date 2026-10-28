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

from camctl.persistence.models import db_failure

import asyncio
import json
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from camctl.contracts.enums import enum_for
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
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.owned_calls import owned_tool_call
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.scheduling import ExpireActionRequest, ExpireOutcome
from camctl.persistence.repositories.capture import FinishResidualBindingFailure, RecordingFailure
from camctl.persistence.transaction import row_facts
from camctl.scheduling.rules import WindowPhase, window_phase
from camctl.outputs.read_discovery import DEFAULT_BATCH_SIZE, ReadDiscoveryOwner
from camctl.persistence.repositories.operations import (
    _ATTEMPT_STATUS, _EFFECT_STATE,
)

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
        " ORDER BY da.id LIMIT 2",
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


async def _saved_binding_failure(runtime: Any, action: Mapping[str, Any]):
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    if (action.get("driver_id") != DRIVER_ID or action.get("type") != 1
            or action.get("status") != 1 or action.get("execution_started") != 0):
        return None
    from camctl.persistence.repositories.availability_wait_finish import saved_binding_check_failure
    return await runtime.database.read(lambda owned: saved_binding_check_failure(owned.connection, action["id"]),
                                       description="读取原检查绑定失败")


async def _original_binding_failure(runtime: Any, action: Mapping[str, Any]):
    """核对保存的设备及驱动；没有绑定端口的调用方提供匹配前提。"""
    from camctl.capture.handlers import _binding

    saved = await _saved_binding_failure(runtime, action)
    if saved is not None:
        return saved[0]
    if runtime.binding_check is None:
        return None
    failure = binding_failure_details(runtime.binding_check(_binding(action)))
    if failure is not None:
        return failure
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    if action.get("driver_id") != DRIVER_ID or action.get("type") != 1:
        return None
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.persistence.transaction import row_facts
    from camctl.persistence.repositories.physical_alias import physical_alias_conflict
    from camctl.persistence.repositories.availability_target import (logical_held_identity_mismatch,
        photo_check_identity_mismatch)
    current = runtime.fixed_camera_identity
    if not isinstance(current, CameraIdentity):
        raise ConsistencyError("当前 Hasselblad 配置缺少固定机身身份")
    def read(owned):
        found = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action["id"],)).fetchone()
        activity = None if found is None else row_facts(owned.connection, "device_activities", found[0])
        eligible = action["status"] == 1 and action["execution_started"] == 0
        mismatch = False
        if activity is None and eligible:
            old_mismatch = logical_held_identity_mismatch(owned.connection, action["device_id"], current)
            check_mismatch = photo_check_identity_mismatch(owned.connection, action, current)
            mismatch = old_mismatch or check_mismatch
        conflict = eligible and physical_alias_conflict(owned.connection, action["device_id"], current)
        return activity, mismatch, conflict
    activity, mismatch, conflict = await runtime.database.read(read, description="读取固定机身身份")
    if mismatch:
        return {"device_id": action["device_id"], "expected_driver_id": DRIVER_ID,
                "reason": "physical_identity_mismatch"}
    if activity is None:
        original = None
    else:
        try:
            original = CameraIdentity(**activity["expected_identity_json"])
        except (TypeError, ValueError, KeyError) as error:
            raise ConsistencyError("原活动缺少可靠固定机身身份") from error
        if activity["resource_key"] != original.resource_key:
            raise ConsistencyError("原活动资源键与固定机身身份不符")
        if current != original:
            return {"device_id": action["device_id"], "expected_driver_id": DRIVER_ID,
                    "reason": "physical_identity_mismatch"}
    if conflict:
        return {"device_id": action["device_id"], "expected_driver_id": DRIVER_ID,
                "reason": "physical_identity_conflict"}
    return None


async def _finish_original_binding_failure(
    runtime: Any, candidate: ResidualCandidate, details: Mapping[str, Any],
    trigger_id: int | None = None,
) -> bool:
    """原调用已结束时，结束固定残留责任及适用的新触发动作。"""
    def read(owned, parameters=(candidate.activity_id,)):
        with closing(owned.connection.execute("SELECT responsibility_key FROM operation_runs WHERE activity_id = ?"
        " AND status IN (1, 2) AND (kind = 8 OR (kind = 6 AND query_purpose = 5))"
        " ORDER BY id", parameters)) as cursor:
            return tuple(row[0] for row in cursor.fetchall())
    keys = await runtime.database.read(read, description="读取拍摄直接事实")
    if not keys:
        return False
    running = await runtime.database.read(lambda owned: owned.connection.execute(
        "SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id = a.run_id"
        " WHERE r.activity_id = ? AND a.status = 1"
        " AND (r.kind = 8 OR (r.kind = 6 AND r.query_purpose = 5)) LIMIT 1",
        (candidate.activity_id,)).fetchone())
    if running is not None:
        return False
    _save_inputs = (FinishResidualBindingFailure(
            trigger_id, candidate.activity_id, runtime.wall_us(),
            RecordingFailure("device_binding_unavailable", details), keys), new_operation_key())
    _save_options = dict()
    outcome = (await runtime.database.write(lambda owned: runtime.capture.finish_residual_binding_failure(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"原残留绑定失败事务未完成（{outcome.kind.value}）: {outcome.error}")
    return True


async def pass_residual_gate(runtime: Any, trigger: Mapping[str, Any], *, on_progress=None) -> bool:
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
        _save_inputs = (ExpireActionRequest(
                action_id=trigger["id"], trusted_wall_now=now, occurred_at=now), new_operation_key())
        _save_options = dict()
        outcome = (await runtime.database.write(lambda owned: runtime.scheduling.expire_action(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise RuntimeError(
                f"动作过期事务未完成（{outcome.kind.value}）: {outcome.error}")
        if on_progress is not None and outcome.value.outcome is ExpireOutcome.EXPIRED:
            on_progress()
        return False
    if phase is WindowPhase.BEFORE_START:
        return False
    if (await _original_binding_failure(runtime, trigger)) is not None:
        # 普通动作先取得本地执行身份，再由其绑定失败事务结束。
        return True
    from camctl.capture.software_recovery import pass_software_recovery_gate
    recovery = await pass_software_recovery_gate(runtime, trigger, on_progress=on_progress)
    if recovery is not None:
        if recovery:
            return True
        # 未配置的驱动仍沿原 ACTIVE 查询/停止链处理。
        if getattr(runtime, "software_recovery_driver", None) is not None:
            return False
    candidates = (await runtime.database.read(lambda owned, inputs=(trigger["device_id"],), options=dict(): residual_candidates(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    if not candidates:
        return True
    if len(candidates) > 1:
        raise RuntimeError(
            f"同设备出现多个残留候选: {trigger['device_id']} "
            + repr([c.activity_id for c in candidates]))
    candidate = candidates[0]
    flow = (await runtime.database.read(lambda owned, inputs=(candidate.activity_id,), options=dict(): _unfinished_winddown(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    if flow is None and (await runtime.database.read(lambda owned, inputs=(candidate.activity_id,), options=dict(): _succeeded_winddown(owned.connection, inputs[0], **options), description="读取拍摄直接事实")):
        # 成功流程已存在而活动未收口：补齐收场后放行（中断恢复）。
        (await _conclude_activity(runtime, candidate.action_id))
        if on_progress is not None:
            on_progress()
        return not (await runtime.database.read(lambda owned, inputs=(trigger["device_id"],), options=dict(): residual_candidates(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    if flow is None and (await runtime.database.read(lambda owned, inputs=(trigger["id"],), options=dict(): _trigger_idle_confirmed(owned.connection, inputs[0], **options), description="读取拍摄直接事实")):
        return True
    details = (await _original_binding_failure(runtime, (await runtime.action(candidate.action_id))))
    if flow is not None and details is not None:
        if trigger["status"] == 1:
            # ACTION_STARTED 是执行失败的前提，此处只放行本地开始。
            return True
        changed = await _finish_original_binding_failure(runtime, candidate, details, trigger["id"])
        if changed and on_progress is not None:
            on_progress()
        return False
    if flow is not None:
        await _advance_winddown(runtime, candidate, flow, on_progress=on_progress)
        return False
    if details is not None:
        return False
    return await _preflight_check(runtime, trigger, candidate, on_progress=on_progress)


@owned_tool_call
async def _preflight_check(
    runtime: Any, trigger: Mapping[str, Any], candidate: ResidualCandidate, *, on_progress=None,
) -> bool:
    """执行一次执行前检查并按观察分区；可靠空闲时返回 True。"""

    from camctl.capture.handlers import _binding, _operation_outcome

    if runtime.state_query is None:
        # 驱动未声明查询能力：不推测设备空闲，触发动作按自身窗口与
        # 取消规则收尾。
        return False
    responsibility = _preflight_key(trigger["id"])
    if (await runtime.retry_wait_remaining(
            responsibility,
            runtime.query_config.retry_interval_s)) is not None:
        return False
    ticket = (await _begin_query_attempt(
        runtime, responsibility, trigger["id"], _NO_TARGET,
        QueryPurpose.BEFORE_EXECUTION, on_progress=on_progress))
    if ticket is None:
        return False
    response = await runtime.state_query.query_state(ControlRequest(
        operation="query", binding=_binding(trigger),
        params=trigger["effective_params_json"], ticket=ticket,
        timeout_s=runtime.query_config.timeout_s))
    if on_progress is not None:
        on_progress()
    outcome, _ = _operation_outcome(
        response, "activity_status", evidence_type="query_returned")
    observed = _observed_activity(response)
    if response.error is None and observed is None:
        # 可靠确认空闲：保存观察，执行前检查责任结束，触发动作继续。
        (await runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED))
        return True
    if response.error is None and observed == candidate.activity_id:
        # 确认目标残留仍在录制：检查责任结束，建立收场流程并首停。
        (await runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED))
        if candidate.stop_capable:
            await _stop_residual(runtime, trigger["id"], candidate, on_progress=on_progress)
        return False
    # 其他有效活动占用或调用错误：责任未满足，按间隔再次检查。
    (await runtime.finish(ticket, outcome, retry_wait=True))
    return False


async def _begin_query_attempt(
    runtime: Any, responsibility: str, action_id: int, target: AttemptTarget,
    purpose: QueryPurpose, *, on_progress=None,
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
    _save_inputs = (intent, new_operation_key())
    _save_options = dict()
    outcome = (await runtime.database.write(lambda owned: runtime.operations.begin_attempt(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise RuntimeError(
            f"查询意图事务未完成（{outcome.kind.value}）: {outcome.error}")
    if outcome.value.disposition is not BeginDisposition.GRANTED:
        if outcome.value.reason == "budget_exhausted":
            _save_inputs = (StaleRunFinish(
                    responsibility_keys=(responsibility,),
                    status=RunOutcome.UNCONFIRMED,
                    error=ErrorValue(code="result_unconfirmed", stage="device"),
                    occurred_at=runtime.wall_us()), new_operation_key())
            _save_options = dict()
            finished = (await runtime.database.write(lambda owned: runtime.operations.finish_stale_runs(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
            if finished.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, f"查询预算耗尽的结果未保存: {finished.error}", finished)
            if on_progress is not None:
                on_progress()
        return None
    if on_progress is not None:
        on_progress()
    return outcome.value.ticket


async def _advance_winddown(
    runtime: Any, candidate: ResidualCandidate,
    flow: tuple[int, int, int], *, on_progress=None,
) -> None:
    """把未收口的收场流程推进一个可执行步骤。

    停止发出未确认且无调用错误时，先用确认查询核实：可靠空闲即按
    停止事实收场流程与活动；仍在录制、查询不可用或预算耗尽时按声
    明的安全重复停止能力继续停止。
    """

    run_id, trigger_id, _ = flow
    def read(owned, parameters=(run_id,)):
        with closing(owned.connection.execute("SELECT action_id, activity_id, status, attempts_used"
        " FROM operation_runs WHERE id = ? AND kind = 8", parameters)) as cursor:
            return cursor.fetchone()
    current = await runtime.database.read(read, description="读取拍摄直接事实")
    if current is None or current[:2] != (trigger_id, candidate.activity_id):
        raise ConsistencyError("残留收场流程与原触发动作或目标活动不符")
    if current[2] not in (1, 2):
        return
    responsibility = _winddown_key(trigger_id, candidate.activity_id)
    trigger = (await runtime.action(trigger_id))
    expired = window_phase(runtime.window_of(trigger), runtime.wall_us()) is WindowPhase.AFTER_WINDOW
    if current[3] == 0 and (trigger["cancel_requested"]
                           or trigger["status"] in _TRIGGER_TERMINAL or expired):
        _save_inputs = (StaleRunFinish(
                responsibility_keys=(responsibility,),
                status=(RunOutcome.EXPIRED if not trigger["cancel_requested"]
                        and (trigger["status"] == 5 or expired) else RunOutcome.CANCELED),
                occurred_at=runtime.wall_us()), new_operation_key())
        _save_options = dict()
        result = (await runtime.database.write(lambda owned: runtime.operations.finish_stale_runs(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
        if result.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError, f"未派发残留收场的结束事实未保存: {result.error}", result)
        if on_progress is not None:
            on_progress()
        return
    attempt = (await runtime.database.read(lambda owned, inputs=(responsibility,), options=dict(): _last_attempt(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.RUNNING):
        return
    details = (await _original_binding_failure(runtime, (await runtime.action(candidate.action_id))))
    if details is not None:
        changed = await _finish_original_binding_failure(runtime, candidate, details)
        if changed and on_progress is not None:
            on_progress()
        return
    unconfirmed = (
        attempt is not None
        and int(attempt[0]) == int(_ATTEMPT_STATUS.SUCCEEDED)
        and int(attempt[1]) == int(_EFFECT_STATE.UNKNOWN))
    if unconfirmed and await _confirm_by_query(
            runtime, trigger_id, candidate, on_progress=on_progress):
        return
    if (await runtime.retry_wait_remaining(
            responsibility,
            runtime.residual_config.retry_interval_s)) is not None:
        return
    await _stop_residual(runtime, trigger_id, candidate, on_progress=on_progress)


@owned_tool_call
async def _stop_residual(
    runtime: Any, trigger_id: int, candidate: ResidualCandidate, *, on_progress=None,
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
    existing = (await runtime.database.read(lambda owned, inputs=(responsibility,), options=dict(): _run_row(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    first_stop = existing is None or existing[2] == 0
    if first_stop:
        # 首次停止前重新核对当前触发资格；查询等待期间可能已经取消或超窗。
        trigger = (await runtime.action(trigger_id))
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
    _save_inputs = (intent, new_operation_key())
    _save_options = dict()
    begin = (await runtime.database.write(lambda owned: runtime.operations.begin_attempt(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    if begin.kind is not DbOutcomeKind.COMPLETED:
        raise RuntimeError(
            f"残留收场意图事务未完成（{begin.kind.value}）: {begin.error}")
    if begin.value.disposition is not BeginDisposition.GRANTED:
        if begin.value.reason == "budget_exhausted":
            (await _finish_winddown_exhausted(runtime, responsibility, candidate))
            if on_progress is not None:
                on_progress()
        return
    if on_progress is not None:
        on_progress()
    ticket = begin.value.ticket
    def read_dispatch_facts(owned):
        return (row_facts(owned.connection, "actions", candidate.action_id),
            row_facts(owned.connection, "actions", trigger_id) if first_stop else None)

    owner, trigger = await runtime.database.read(read_dispatch_facts,
        description="读取残留停止派发资格")
    if first_stop:
        expired = window_phase(runtime.window_of(trigger), runtime.wall_us()) is WindowPhase.AFTER_WINDOW
        if trigger["cancel_requested"] or trigger["status"] in _TRIGGER_TERMINAL or expired:
            reason = "window_ended" if not trigger["cancel_requested"] and (trigger["status"] == 5 or expired) else "canceled"
            prevented = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                error=ErrorValue(reason, "dispatch"),
                settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                    EvidenceValue("dispatch_prevented", 1, {})))
            await runtime.finish(ticket, prevented,
                end_run=RunOutcome.EXPIRED if reason == "window_ended" else RunOutcome.CANCELED)
            return
    from camctl.logging_runtime.events import measure_stage

    with measure_stage("capture.residual_stop", action_id=candidate.action_id,
            trigger_action_id=trigger_id, device_id=owner["device_id"],
            activity_id=candidate.activity_id, run_id=ticket.run_id,
            attempt_id=ticket.attempt_id):
        response = await runtime.stopper.stop(ControlRequest(
            operation="stop_recording", binding=_binding(owner),
            params=owner["effective_params_json"], ticket=ticket,
            timeout_s=runtime.residual_config.timeout_s))
    outcome, confirmed = _operation_outcome(
        response, "stop_confirmed", evidence_type="stop_returned")
    if confirmed:
        # 可靠确认的残留停止：尝试结果、流程成功与活动收场同事务。
        (await runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED,
                       residual_conclude=candidate.action_id))
        # 范围限制等收场拒绝已随同事务表达；重入按幂等补足收场。
        (await _conclude_activity(runtime, candidate.action_id))
    else:
        (await runtime.finish(ticket, outcome, retry_wait=True))


async def _finish_winddown_exhausted(
    runtime: Any, responsibility: str, candidate: ResidualCandidate,
) -> None:
    """停止预算耗尽的收场流程失败终态化。"""

    row = (await runtime.database.read(lambda owned, inputs=(responsibility,), options=dict(): _run_row(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    if row is None:
        return
    _save_inputs = (StaleRunFinish(
            responsibility_keys=(responsibility,),
            status=RunOutcome.FAILED,
            error=_stop_error(candidate, int(row[0])),
            occurred_at=runtime.wall_us()), new_operation_key())
    _save_options = dict()
    receipt = (await runtime.database.write(lambda owned: runtime.operations.finish_stale_runs(*_save_inputs, owned, **_save_options), key=_save_inputs[-1], description="保存拍摄业务事实"))
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


def _stop_error(candidate: ResidualCandidate, run_id: int):
    from camctl.operations.models import ErrorValue

    return ErrorValue(
        code="recording_stop_failed", stage="device_stop",
        details={"activity_id": str(candidate.activity_id),
                 "operation_run_id": str(run_id)})


@owned_tool_call
async def _confirm_by_query(
    runtime: Any, trigger_id: int, candidate: ResidualCandidate, *, on_progress=None,
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
    row = (await runtime.database.read(lambda owned, inputs=(responsibility,), options=dict(): _run_row(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    if row is not None and int(row[1]) not in (1, 2):
        # 查询责任已终态：继续停止路径。
        return False
    attempt = (await runtime.database.read(lambda owned, inputs=(responsibility,), options=dict(): _last_attempt(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.RUNNING):
        return True
    if attempt is not None and int(attempt[0]) == int(_ATTEMPT_STATUS.FAILED):
        # 查询调用失败：不反复查询，按重复停止能力继续停止。
        return False
    if (await runtime.retry_wait_remaining(
            responsibility,
            runtime.query_config.retry_interval_s)) is not None:
        return True
    ticket = (await _begin_query_attempt(
        runtime, responsibility, trigger_id,
        AttemptTarget(activity_id=candidate.activity_id),
        QueryPurpose.RESIDUAL_STOP_CONFIRMATION, on_progress=on_progress))
    if ticket is None:
        return False
    owner = (await runtime.action(candidate.action_id))
    response = await runtime.state_query.query_state(ControlRequest(
        operation="query", binding=_binding(owner),
        params=owner["effective_params_json"], ticket=ticket,
        timeout_s=runtime.query_config.timeout_s))
    if on_progress is not None:
        on_progress()
    outcome, _ = _operation_outcome(
        response, "activity_status", evidence_type="query_returned")
    observed = _observed_activity(response)
    if response.error is None and observed is None:
        # 可靠确认空闲：目标录像已不再执行，查询结果、残留停止结束
        # 与活动收场同事务保存。
        (await runtime.finish(ticket, outcome, end_run=RunOutcome.SUCCEEDED,
                       query_conclude=candidate.action_id,
                       query_conclude_stop_keys=(
                           _winddown_key(trigger_id, candidate.activity_id),)))
        return True
    # 仍在录制、其他活动或调用错误：保存事实，继续停止路径。
    (await runtime.finish(ticket, outcome, retry_wait=True))
    return True


def residual_flow(
    capture_factory: Any, *,
    resume_actual_file_facts: Callable[[Any], Awaitable[None]] | None = None,
    owns_device: Callable[[str], bool] | None = None,
    read_discovery_batch_size=DEFAULT_BATCH_SIZE,
) -> Any:
    """构造无人驱动的残留收场推进流程。

    触发动作取消或启动窗口耗尽后，已建立的收场流程不再由执行前检
    查门驱动：未发出停止的流程保存结束原因，已发出的按已保存意图
    继续使用剩余次数收场（camera-recovery.md#触发动作结束时的收
    场）。执行前检查随触发资格结束；确认查询随原残留停止责任结束。
    """

    discovery = ReadDiscoveryOwner(read_discovery_batch_size)

    async def flow(context: Any) -> None:
        from camctl.persistence.repositories.operations import (
            OperationRepository,
        )

        database = context.database
        if resume_actual_file_facts is not None:
            await resume_actual_file_facts(database)
        now = context.clock.utc_micros()
        await discovery.begin_round(database, now)
        await _recover_old_attempts(database, capture_factory, owns_device=owns_device, discovery=discovery, now=now)
        await _settle_orphan_queries(context, database, OperationRepository(), discovery=discovery, now=now)
        if database.metadata.format_version >= 3:
            from camctl.capture.software_recovery import advance_recovery
            recovery_page = await discovery.rows(database, "software_recovery", now,
                "SELECT r.id,r.activity_id,a.device_id FROM software_recoveries r"
                " JOIN actions a ON a.id=r.action_id JOIN device_activities d ON d.id=r.activity_id"
                " WHERE d.occupancy_state=1 AND r.phase NOT IN (?,?)",
                (int(enum_for("software_recoveries.phase").ISOLATED),
                 int(enum_for("software_recoveries.phase").ABANDONED)), identity="r.id")
            for _, activity_id, device_id in recovery_page.items:
                if owns_device is not None and owns_device(device_id):
                    continue
                runtime = await capture_factory(database, device_id)
                if runtime is not None:
                    await advance_recovery(runtime, activity_id)

        page = await discovery.rows(database, "residual", now,
            "SELECT id, action_id, activity_id, attempts_used"
            " FROM operation_runs WHERE kind = 8"
            " AND status IN (1, 2)")
        flows = page.items
        for (run_id, trigger_id, activity_id, used) in flows:
            def read(owned):
                with closing(owned.connection.execute(
                    "SELECT a.status,da.action_id,da.stop_supported,da.safe_repeat_stop,a2.device_id"
                    " FROM operation_runs r JOIN actions a ON a.id=r.action_id"
                    " JOIN device_activities da ON da.id=r.activity_id"
                    " JOIN actions a2 ON a2.id=da.action_id WHERE r.id=?", (run_id,))) as cursor:
                    return cursor.fetchone()
            facts = await database.read(read)
            if facts is None:
                raise ConsistencyError(f"残留收场关联事实缺失: {run_id}")
            if owns_device is not None and owns_device(facts[4]):
                continue
            candidate = ResidualCandidate(
                activity_id=activity_id, action_id=int(facts[1]),
                stop_supported=int(facts[2]), safe_repeat_stop=int(facts[3]))
            runtime = await capture_factory(database, facts[4])
            if runtime is None:
                continue
            await _advance_winddown(
                runtime, candidate, (run_id, trigger_id, used))
        if discovery.pending and getattr(context, "wake", None) is not None:
            from camctl.scheduling.notifications import WakeReason
            context.wake.mark_changed(WakeReason.READ_OPPORTUNITY)
    return flow


async def _recover_old_attempts(database: Any, capture_factory: Any, *, owns_device=None,
                                discovery=None, now=0) -> None:
    """每轮结束一批具有可靠旧边界的原调用，沿用原身份及预算。"""
    from camctl.capture.handlers import _RECOVERABLE_OPERATIONS
    from camctl.operations.models import AttemptTicket

    discovery = discovery if discovery is not None else ReadDiscoveryOwner()
    kinds = tuple(_RECOVERABLE_OPERATIONS)
    marks = ",".join("?" for _ in kinds)
    page = await discovery.rows(database, "attempts", now,
        "SELECT t.id, t.attempt_no, r.id, r.kind, r.activity_id, r.responsibility_key, a.device_id"
        " FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " LEFT JOIN actions a ON a.id=r.action_id"
        f" WHERE t.status=? AND r.kind IN ({marks})",
        (int(_ATTEMPT_STATUS.RUNNING), *kinds), identity="t.id")
    rows = page.items
    runtimes = {}
    for identity, attempt_no, run_id, kind, activity_id, responsibility, device_id in rows:
        if not isinstance(device_id, str) or not device_id:
            raise ConsistencyError("拍摄旧调用缺少所属设备身份")
        if owns_device is not None and owns_device(device_id):
            continue
        if device_id not in runtimes:
            runtimes[device_id] = await capture_factory(database, device_id)
        runtime = runtimes[device_id]
        if runtime is None:
            continue
        ticket = AttemptTicket(
            attempt_no, _RECOVERABLE_OPERATIONS[kind],
            None if activity_id is None else str(activity_id), responsibility, run_id)
        # 查询责任的后续尝试沿重试间隔继续；恢复结果不带等待会
        # 使下一次查询被"先建立重试等待"守卫拒绝。
        (await runtime.recover_attempt(
            ticket, retry_wait=ticket.operation == "query"))


async def _settle_orphan_queries(context: Any, database: Any, operations: Any, *, discovery=None, now=0) -> None:
    """按查询用途和原停止责任结算，不以触发者终态覆盖必要核实。

    普通停止核实与活动核实随所属动作和停止责任收场：原停止已按
    可靠依据成功时查询按同一依据收 SUCCEEDED，不为形式完整追加
    查询；动作取消或终态后不再承担的核实责任按 CANCELED 收场。
    """

    discovery = discovery if discovery is not None else ReadDiscoveryOwner()
    page = await discovery.rows(database, "queries", now,
        "SELECT r.id,r.responsibility_key, r.query_purpose, a.status, a.cancel_requested,"
        " stop.id, stop.status, stop.attempts_used,"
        " nstop.status, act.activity_state FROM operation_runs r"
        " JOIN actions a ON a.id = r.action_id"
        " LEFT JOIN operation_runs stop ON stop.kind = 8"
        " AND stop.action_id = r.action_id AND stop.activity_id = r.activity_id"
        " LEFT JOIN operation_runs nstop ON nstop.kind = 2"
        " AND nstop.responsibility_key = 'stop/' || r.action_id"
        " AND nstop.activity_id = r.activity_id"
        " LEFT JOIN device_activities act ON act.id = r.activity_id"
        " WHERE r.kind = 6 AND r.status IN (1, 2)"
        " AND r.query_purpose IN (1, 3, 4, 5)", identity="r.id")
    for (_, responsibility, purpose, status, canceled, stop_id, stop_status, used,
         nstop_status, activity_state) in page.items:
        if purpose == 1:
            if not canceled and status not in _TRIGGER_TERMINAL:
                continue
            final = RunOutcome.CANCELED
        elif purpose in (3, 4):
            # 普通核实：停止或活动已按可靠依据成功即同依据收成功；
            # 动作不再承担核实责任时按取消收场；责任仍须继续时保持。
            succeeded = (nstop_status == 3 or stop_status == 3
                         or activity_state == 3)
            if succeeded:
                final = RunOutcome.SUCCEEDED
            elif canceled or status in _TRIGGER_TERMINAL:
                final = RunOutcome.CANCELED
            else:
                continue
        else:
            if stop_id is None:
                raise ConsistencyError(f"残留确认查询缺少原停止责任: {responsibility}")
            if stop_status in (1, 2):
                if used > 0 or (not canceled and status not in _TRIGGER_TERMINAL):
                    continue
                final = RunOutcome.CANCELED
            else:
                final = RunOutcome.SUCCEEDED if stop_status == 3 else RunOutcome.CANCELED
        request = StaleRunFinish(responsibility_keys=(responsibility,), status=final,
            occurred_at=context.clock.utc_micros())
        key = new_operation_key()
        outcome = await database.write(lambda owned: operations.finish_stale_runs(request, key, owned), key=key)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
