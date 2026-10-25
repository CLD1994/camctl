"""会话致命错误收场的录像应急停止与最终补记。

按[正在录像时的应急停止](../../architecture/session-errors.md#正在录像时的应急停止)
执行：非时钟致命错误退出时，对本进程已确认启动、身份与能力仍可
靠读取的录像立即有限尝试停止，随后逐个补记。目标发现依赖会话内
存的启动锚点与可靠读取；读取不可靠时不扩大停止范围。
"""

from contextlib import closing
from dataclasses import dataclass, field, replace
import json
from typing import Any, Callable, Mapping

from camctl.capture.recovery import (
    EmergencyBudget,
    EmergencyDecision,
    EmergencyFacts,
    EmergencyOutcome,
    EmergencyRecord,
    RecordStatus,
    emergency_stop,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.ports import ControlRequest, DeviceCallResult
from camctl.operations.result_format import error_document, result_document
from camctl.persistence.business import BusinessDatabase, settle_required
from camctl.persistence.models import DbOutcomeKind, db_outcome_for
from camctl.persistence.repositories.capture import CaptureRepository

_ACTION_TYPE = enum_for("actions.type")
_ACTIVITY_STATE = enum_for("device_activities.activity_state")
_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_EFFECT_STATE = enum_for("operation_attempts.effect_state")
_RUN_STATUS_SUCCEEDED = 3

#: 应急停止的默认限额；设备停止配置可按会话调整。
DEFAULT_MAX_STOP_ATTEMPTS = 3


@dataclass(frozen=True)
class EmergencyTarget:
    """一个可可靠识别的应急目标：动作、活动与既有停止确认。"""

    action_id: int
    activity_id: int
    device_id: str
    driver_id: str
    params: Mapping[str, Any]
    stop_observation: Mapping[str, Any] | None
    safe_repeat_stop: bool


@dataclass
class _Confirmed:
    confirmed: bool


class DriverStopAdapter:
    """把真实停止端口适配为应急端口并按统一结构收集每次尝试。"""

    def __init__(self, stopper, target: EmergencyTarget, timeout_s) -> None:
        self._stopper = stopper
        self._target = target
        self._timeout_s = timeout_s
        self.attempts: list[dict] = []
        self.stop_observation: Mapping[str, Any] | None = None

    async def stop(self) -> _Confirmed:
        response = await self._stopper.stop(ControlRequest(
            operation="stop",
            binding=DeviceBinding(self._target.device_id, self._target.driver_id),
            params=self._target.params,
            ticket=None,
            timeout_s=self._timeout_s,
        ))
        attempt, observation = _stop_attempt_document(
            response, self._target.activity_id)
        self.attempts.append(attempt)
        if observation is not None:
            self.stop_observation = observation
        return _Confirmed(confirmed=observation is not None)


def _stop_attempt_document(response: DeviceCallResult, activity_id: int):
    """编码一次停止尝试；确认观察必须指向本活动。"""
    from camctl.capture.handlers import _operation_outcome

    call, _ = _operation_outcome(response, "stop_confirmed",
                                 evidence_type="stop_returned")
    observation = next(
        ({"type": item.type, "version": item.version, "data": dict(item.data)}
         for item in response.observations if item.type == "stop_confirmed"),
        None,
    )
    if observation is not None and observation["data"].get("activity_id") not in (
            str(activity_id), None):
        # 观察指向其他活动时不是本目标的停止确认；仍按实际结果保存。
        observation = None
    return ({
        "status": int(_ATTEMPT_STATUS[call.status.name]),
        "effect_state": int(_EFFECT_STATE[call.effect.name]),
        "result_json": result_document(call),
        "error_json": error_document(call.error),
    }, observation)


def discover_targets(
        connection, anchors: Mapping[int, Any], session_key: str,
) -> list[EmergencyTarget]:
    """按会话启动锚点识别仍可可靠读取、尚未结束的在途录像。

    本会话已补记过应急流程的目标不再进入停止范围；同一会话对同
    一活动至多一条应急流程，后续错误不重新获得额度。
    """

    targets = []
    for action_id in sorted(anchors):
        with closing(connection.execute(
                "SELECT a.status, a.device_id, a.driver_id, a.effective_params_json,"
                " d.id, d.activity_state, d.stop_supported, d.safe_repeat_stop"
                " FROM actions a JOIN device_activities d ON d.action_id = a.id"
                " WHERE a.id = ? AND a.type = ?",
                (action_id, int(_ACTION_TYPE.CAMERA_RECORD)))) as cursor:
            row = cursor.fetchone()
        if row is None or row[0] != 2:
            continue
        (status, device_id, driver_id, params_json, activity_id, activity_state,
         stop_supported, safe_repeat_stop) = row
        if activity_state == int(_ACTIVITY_STATE.ENDED):
            # 已可靠确认停止的录像不重复发令，也不建立应急流程。
            continue
        with closing(connection.execute(
                "SELECT id FROM operation_runs WHERE responsibility_key = ?",
                (f"emergency/{session_key}/{int(activity_id)}",))) as cursor:
            if cursor.fetchone() is not None:
                continue
        targets.append(EmergencyTarget(
            action_id=action_id,
            activity_id=int(activity_id),
            device_id=device_id,
            driver_id=driver_id,
            params=json.loads(params_json),
            stop_observation=_saved_stop_confirmation(
                connection, action_id, int(activity_id)),
            safe_repeat_stop=bool(safe_repeat_stop) and bool(stop_supported),
        ))
    return targets


def _saved_stop_confirmation(connection, action_id: int, activity_id: int):
    """读取原停止责任最近成功尝试中指向本活动的停止确认观察。"""

    with closing(connection.execute(
            "SELECT a.result_json FROM operation_attempts a"
            " JOIN operation_runs r ON r.id = a.run_id"
            " WHERE r.responsibility_key = ? AND r.status = ?"
            " ORDER BY a.id DESC LIMIT 1",
            (f"stop/{action_id}", _RUN_STATUS_SUCCEEDED))) as cursor:
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    for observation in json.loads(row[0]).get("observations", ()):
        if (observation.get("type") == "stop_confirmed"
                and observation.get("data", {}).get("activity_id") in (
                    str(activity_id), None)):
            return dict(observation)
    return None


class _NoStopPort:
    """资格不成立时的占位端口；资格判定保证不会被调用。"""

    async def stop(self):  # pragma: no cover - 资格判定先于端口调用
        raise AssertionError("资格不成立时不发停止命令")


@dataclass
class EmergencySettlement:
    """一次会话错误收场的应急停止执行器。

    stop_config 按 device_id 提供本会话停止配置（max_attempts、
    timeout_s、retry_interval_s）；registry 提供 StopDriver；database
    为读取与补记提供共享业务队列。每个目标独立停止、补记并形成一条
    诊断；单目标失败不阻止其余目标。
    """

    session_key: str
    anchors: Mapping[int, Any]
    stop_config: Mapping[str, Mapping[str, Any]]
    registry: Any
    database: BusinessDatabase
    wall_us: Callable[[], int]
    repository: CaptureRepository = field(default_factory=CaptureRepository)
    entries: list[dict] = field(default_factory=list)

    async def settle(self) -> list[dict]:
        """执行全部可识别目标的应急停止；返回诊断条目。"""
        try:
            anchors = dict(self.anchors)
            targets = await self.database.read(lambda owned: discover_targets(
                owned.connection, anchors, self.session_key), description="读取应急目标")
        except Exception as error:
            return self._unreadable(error)
        for target in targets:
            await self._settle_one(target)
        return list(self.entries)

    def _unreadable(self, error: Exception) -> list[dict]:
        # 状态库不可读取时无法可靠确认目标身份；不用猜测扩大停止范围，
        # 以通用诊断表达未处理事实，不创建虚构的应急目标记录。
        self.entries.append({
            "outcome": "not_attempted", "attempts_used": 0,
            "record_status": "not_recorded",
            "error": f"目标身份读取失败，未发起停止: {error}"})
        return list(self.entries)

    async def _settle_one(self, target: EmergencyTarget) -> None:
        config = dict(self.stop_config.get(target.device_id) or {})
        maximum = config.get("max_attempts")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
            maximum = DEFAULT_MAX_STOP_ATTEMPTS
        entry: dict[str, Any] = {
            "action_instance_id": str(target.action_id),
            "device_id": target.device_id,
            "max_attempts": maximum,
            "attempts_used": 0,
        }
        self.entries.append(entry)
        stopper = self._stopper_of(target)
        facts = EmergencyFacts(
            process_can_handle=True,
            session_fatal_error=True,
            stop_confirmed=target.stop_observation is not None,
            ownership_confirmed=True,
            exclusive_eligibility=stopper is not None,
            driver_safe_repeat_stop=target.safe_repeat_stop,
            config_known=True,
            budget_available=True,
            stop_observation=target.stop_observation,
        )
        attempts: tuple[dict, ...] = ()
        if stopper is not None:
            adapter = DriverStopAdapter(
                stopper, target, config.get("timeout_s"))
            record = await emergency_stop(facts, EmergencyBudget(maximum), adapter)
            if (record.outcome is EmergencyOutcome.STOPPED
                    and adapter.stop_observation is not None):
                record = replace(record, stop_observation=adapter.stop_observation)
            attempts = tuple(adapter.attempts)
        else:
            record = await emergency_stop(facts, EmergencyBudget(maximum), _NoStopPort())
        entry["attempts_used"] = record.attempts_used
        entry["outcome"] = record.outcome.value
        entry["record_status"] = RecordStatus.NOT_RECORDED.value
        await self._record(target, record, attempts, config, entry)

    def _stopper_of(self, target: EmergencyTarget):
        if self.registry is None:
            return None
        registered = self.registry.entry(target.driver_id)
        if registered is None or not registered.declaration.stop_supported:
            return None
        return registered.driver

    async def _record(self, target: EmergencyTarget, record: EmergencyRecord,
                attempts: tuple[dict, ...], config: Mapping[str, Any],
                entry: dict[str, Any]) -> None:
        """原真实停止结果入队补记，诊断在事件循环接手实际回执。"""
        key, occurred_at = new_operation_key(), self.wall_us()
        async def save_and_apply():
            failure = None
            try:
                receipt = await self.database.write(lambda owned: self.repository.save_emergency(
                    session_key=self.session_key, action_id=target.action_id,
                    activity_id=target.activity_id, record=record, attempts=attempts,
                    occurred_at=occurred_at, key=key, owned=owned,
                    timeout_s=config.get("timeout_s"), retry_interval_s=config.get("retry_interval_s")),
                    key=key, description="补记原应急停止结果")
            except BaseException as error:
                receipt = db_outcome_for(error)
                if receipt is None:
                    if isinstance(error, Exception):
                        entry["error"] = f"补记失败: {error}"
                        return
                    raise
                failure = error
            if receipt.kind is DbOutcomeKind.COMPLETED:
                entry["record_status"] = RecordStatus.RECORDED.value
            elif receipt.kind is DbOutcomeKind.UNKNOWN:
                entry["record_status"] = RecordStatus.UNKNOWN.value
            if failure is not None:
                if isinstance(failure, Exception):
                    entry["error"] = f"补记交接失败: {failure}"
                else:
                    raise failure
        await settle_required(save_and_apply())
