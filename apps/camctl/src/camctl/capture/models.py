"""拍摄专属定义（第一版：拍照、录像、延时摄影）。

定义由首次受理事实构建并只读：动作类型、精确目标时长（毫秒）
与完成声明。公共管理模型设备无关；专属参数仅本模块解释。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from camctl.contracts.values import (
    MAX_OBJECT_ID,
    ObjectId,
    UtcMicros,
    seconds_to_duration_ms,
)
from camctl.contracts.json_values import is_json_integer
from camctl.contracts.enums import enum_for
from camctl.contracts.workflow_errors import validate_public_error
from camctl.devices.tasks import CaptureTask, CompletionMode, EndControl, StartReturn

__all__ = [
    "ActivityCapabilities",
    "ActivityConcludeSave",
    "ActivityObservationSave",
    "ActivityReleaseSave",
    "CaptureCompletion",
    "CaptureDefinition",
    "CaptureInput",
    "CaptureResultStatus",
    "ResultSetPhase",
    "ResultSetSave",
    "WaitCompletedSave",
    "activity_capabilities",
    "validate_capture_result",
]


@dataclass(frozen=True)
class ActivityCapabilities:
    """设备活动登记时固定的实际能力（第一次建立活动时使用）。"""

    state_query_supported: int
    stop_supported: int
    safe_repeat_stop: int
    start_return_meaning: int
    completion_mode: int
    ownership_mode: int
    output_scope_json: dict


def activity_capabilities(
    action_type: str, spec: Mapping[str, Any]
) -> ActivityCapabilities:
    """从动作类型与首次固定执行定义推导设备活动能力。

    单张拍摄以成功返回为完成依据；录像必须支持停止，完成由停止
    与产物集合判定；延时摄影沿用受理时固定任务契约中的同一能力
    声明。归属声明与范围来自固定定义，实际基准由执行历史保存。
    """
    if action_type == "camera_take_photo":
        validate_capture_spec(action_type, spec)
        return ActivityCapabilities(
            state_query_supported=0, stop_supported=0, safe_repeat_stop=0,
            start_return_meaning=int(StartReturn.COMPLETED),
            completion_mode=int(CompletionMode.DEVICE_EVIDENCE),
            ownership_mode=1, output_scope_json={})
    if action_type == "camera_record":
        validated = validate_capture_spec(action_type, spec)
        return ActivityCapabilities(
            state_query_supported=0, stop_supported=1, safe_repeat_stop=1,
            start_return_meaning=int(StartReturn.STARTED),
            completion_mode=int(CompletionMode.TIME_AND_OUTPUTS),
            ownership_mode=validated.get("ownership_mode", 1),
            output_scope_json=deepcopy(validated.get("output_scope", {})))
    if action_type == "camera_timelapse":
        validated = validate_capture_spec(action_type, spec)
        stop = 1 if validated["stop_supported"] else 0
        return ActivityCapabilities(
            state_query_supported=0, stop_supported=stop,
            safe_repeat_stop=stop,
            start_return_meaning=validated["start_return_meaning"],
            completion_mode=validated["completion_mode"],
            ownership_mode=validated.get("ownership_mode", 1),
            output_scope_json=deepcopy(validated.get("output_scope", {})))
    raise ValueError(f"非拍摄动作不推导设备活动能力: {action_type!r}")

_CAMERA_TYPES = frozenset(
    {"camera_take_photo", "camera_record", "camera_timelapse"}
)


CaptureCompletion = CompletionMode


@dataclass(frozen=True)
class CaptureInput:
    """已校验的动作输入：类型与生效参数（首次受理事实）。"""

    action_type: str
    effective_params: Mapping[str, Any]
    task: CaptureTask | None = None

    def __post_init__(self) -> None:
        if self.action_type not in _CAMERA_TYPES:
            raise ValueError(f"非拍摄动作不构造拍摄定义: {self.action_type!r}")
        object.__setattr__(self, "effective_params", MappingProxyType(dict(self.effective_params)))


@dataclass(frozen=True)
class CaptureDefinition:
    """一次拍摄动作的只读执行定义。

    target_duration_ms 仅录像/延时需要（正整数毫秒，精确换算）；
    定义由首次受理构建，驱动默认值变化不影响旧动作。
    """

    action_type: str
    target_duration_ms: int | None
    completion: CaptureCompletion

    @classmethod
    def build(cls, source: CaptureInput) -> "CaptureDefinition":
        """从生效参数构造定义；参数不完整或不精确时拒绝。"""
        spec = build_capture_spec(source.action_type, source.task)
        return cls(
            action_type=source.action_type,
            target_duration_ms=spec.get("target_duration_ms"),
            completion=CaptureCompletion(spec["completion_mode"]) if source.action_type == "camera_timelapse" else CaptureCompletion.DEVICE_EVIDENCE,
        )


def _integer(value, minimum=0):
    if not is_json_integer(value) or not minimum <= value <= MAX_OBJECT_ID:
        raise ValueError(f"执行定义数值必须是 {minimum}～{MAX_OBJECT_ID} 的 JSON 整数")
    return int(value)


_OWNERSHIP_FIELDS = frozenset({"ownership_mode", "output_scope"})


def _ownership(spec: dict) -> dict:
    """归属字段成对出现；范围必须是非空精确 JSON 对象。"""
    if not _OWNERSHIP_FIELDS.intersection(spec):
        return {}
    if not _OWNERSHIP_FIELDS <= set(spec):
        raise ValueError("归属方式和输出范围必须共同声明")
    mode = enum_for("device_activities.ownership_mode")(_integer(spec["ownership_mode"], 1))
    scope = spec["output_scope"]
    if not isinstance(scope, dict) or not scope:
        raise ValueError("输出范围必须是非空 JSON 对象")

    def check(value):
        if value is None or isinstance(value, (bool, int)):
            return
        if isinstance(value, str):
            value.encode("utf-8")
            return
        if isinstance(value, Decimal) and value.is_finite():
            return
        if isinstance(value, list):
            for item in value:
                check(item)
            return
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            for key, item in value.items():
                check(key)
                check(item)
            return
        raise ValueError("输出范围包含非 JSON 值")

    check(scope)
    return {"ownership_mode": int(mode), "output_scope": deepcopy(scope)}


def validate_capture_spec(action_type: str, spec: Any) -> dict:
    """验证已保存的拍摄结构；不读取参数默认值或设备配置。"""
    if not isinstance(spec, dict):
        raise ValueError("拍摄执行定义必须是 JSON 对象")
    if action_type == "camera_take_photo":
        if spec:
            raise ValueError("拍照执行定义必须是空对象")
        return {}
    if action_type == "camera_record":
        ownership = _ownership(spec)
        if set(spec) != {"target_duration_ms", *ownership}:
            raise ValueError("录像定义必须保存目标时长及适用的归属声明")
        return {"target_duration_ms": _integer(spec["target_duration_ms"], 1), **ownership}
    if action_type != "camera_timelapse":
        raise ValueError("动作类型不是拍摄动作")
    required = {"duration_based", "wait_after_send", "end_control", "stop_supported", "start_return_meaning", "completion_mode"}
    if not required <= set(spec):
        raise ValueError("延时定义缺少固定任务依据")
    for key in ("duration_based", "wait_after_send", "stop_supported"):
        if not isinstance(spec[key], bool):
            raise ValueError(f"{key} 必须是 JSON 布尔值")
    ownership = _ownership(spec)
    result = {**spec, **ownership}
    end = EndControl(_integer(spec["end_control"], 1))
    start = StartReturn(_integer(spec["start_return_meaning"], 1))
    completion = CompletionMode(_integer(spec["completion_mode"], 1))
    result.update(end_control=int(end), start_return_meaning=int(start), completion_mode=int(completion))
    expected = set(required) | set(ownership)
    if spec["duration_based"]:
        expected.add("target_duration_ms")
    if spec["wait_after_send"]:
        expected.add("result_wait_margin_ms")
        if not spec["duration_based"] or end is not EndControl.DEVICE or start is not StartReturn.SENT:
            raise ValueError("发送后等待必须具有设备自行结束的固定时长任务和 SENT 返回")
    if set(spec) != expected:
        raise ValueError("延时定义条件字段缺失或不适用")
    if spec["duration_based"]:
        result["target_duration_ms"] = _integer(spec["target_duration_ms"], 1)
    if spec["wait_after_send"]:
        result["result_wait_margin_ms"] = _integer(spec["result_wait_margin_ms"])
    if end is EndControl.HOST_TIMER and (not spec["stop_supported"] or not spec["duration_based"] or completion is not CompletionMode.DEVICE_EVIDENCE or start is StartReturn.COMPLETED):
        raise ValueError("主机按时停止的任务缺少可用停止与完成契约")
    if completion is CompletionMode.TIME_AND_OUTPUTS and not spec["duration_based"]:
        raise ValueError("时间与产物完成方式必须是有限时长任务")
    return result


def build_capture_spec(action_type: str, task: CaptureTask | None) -> dict:
    """所属处理模块将驱动任务事实转为首次固定定义。"""
    if action_type == "camera_take_photo":
        return validate_capture_spec(action_type, {})
    if not isinstance(task, CaptureTask) or task.action_type != action_type:
        raise ValueError("驱动未提供对应动作的固定拍摄任务")
    ownership = {}
    if task.ownership_mode is not None or task.output_scope is not None:
        ownership = {"ownership_mode": task.ownership_mode, "output_scope": task.output_scope}
    if action_type == "camera_record":
        if task.stop_supported is not True:
            raise ValueError("录像任务必须明确具备 stop_supported")
        return validate_capture_spec(action_type, {"target_duration_ms": seconds_to_duration_ms(task.target_duration_s), **ownership})
    spec = {"duration_based": task.duration_based, "wait_after_send": task.wait_after_send,
            "end_control":task.end_control, "stop_supported":task.stop_supported,
            "start_return_meaning":task.start_return_meaning, "completion_mode":task.completion_mode,
            **ownership}
    if task.target_duration_s is not None:
        spec["target_duration_ms"] = seconds_to_duration_ms(task.target_duration_s)
    if task.result_wait_margin_s is not None:
        spec["result_wait_margin_ms"] = seconds_to_duration_ms(task.result_wait_margin_s)
    return validate_capture_spec(action_type, spec)


@dataclass(frozen=True)
class ActivityObservationSave:
    """一次设备活动观察的保存输入（DEVICE_OBSERVED.OBSERVE）。

    发送与启动时刻只能从空值一次保存；活动结束不经本命令补造。
    至少携带一项新事实。
    """

    action_id: int
    occurred_at: int
    sent_at: int | None = None
    started_at: int | None = None
    dispatch_state: int | None = None
    activity_state: int | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)
        for name in ("sent_at", "started_at"):
            value = getattr(self, name)
            if value is not None:
                UtcMicros(value)
        if (self.sent_at is None and self.started_at is None
                and self.dispatch_state is None and self.activity_state is None):
            raise ValueError("活动观察必须携带至少一项事实")
        if self.activity_state == 3:
            raise ValueError("活动结束须由可靠停止事实承载，不经观察补造")


@dataclass(frozen=True)
class ActivityReleaseSave:
    """一次占用释放申请的输入（DEVICE_OBSERVED.RELEASE）。

    释放判定由事务按统一占用规则完成；命令只携带活动身份与事实
    时刻。
    """

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


@dataclass(frozen=True)
class ActivityConcludeSave:
    """一次活动收场申请的输入（结束观察与占用释放同事务）。

    活动必须已被观察到进行中；结束的可靠停止事实是 start 责任
    的成功终态流程行，由事务装载核验。
    """

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class ResultSetPhase(Enum):
    """结果集合核实的分支，与 RESULT_SET_CONFIRMED 的分支一一对应。"""

    COMPLETE = "complete"
    UNSATISFIED = "unsatisfied"
    UNCONFIRMED = "unconfirmed"
    BEGIN = "begin"


class CaptureResultStatus(str, Enum):
    """采集阶段的内部结果状态，不从动作终态推定。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    UNCONFIRMED = "unconfirmed"


#: 结论分支要求的采集状态（operation-fields.md#设备活动字段）。
_PHASE_CAPTURE_STATUS = {
    ResultSetPhase.COMPLETE: CaptureResultStatus.COMPLETED,
    ResultSetPhase.UNSATISFIED: CaptureResultStatus.FAILED,
    ResultSetPhase.UNCONFIRMED: CaptureResultStatus.UNCONFIRMED,
}


def validate_capture_result(
    value, *, phase: ResultSetPhase | None = None,
) -> None:
    """核实原采集结果的状态、错误与精确数值，不修改输入。

    可选次数和秒数未知时省略；显式空值不能替代未知成员。
    指定结果集合结论时，采集状态还须与该分区一致。
    """
    if not isinstance(value, Mapping):
        raise ValueError(f"采集结果必须是对象: {value!r}")
    raw_status = value.get("status")
    if not isinstance(raw_status, str):
        raise ValueError(f"采集结果缺少合法状态: {raw_status!r}")
    try:
        status = CaptureResultStatus(raw_status)
    except ValueError as error:
        raise ValueError(f"采集结果状态未登记: {raw_status!r}") from error
    if phase is not None:
        if not isinstance(phase, ResultSetPhase) or phase is ResultSetPhase.BEGIN:
            raise ValueError(f"采集结果只适用于结果集合结论: {phase!r}")
        expected = _PHASE_CAPTURE_STATUS[phase]
        if status is not expected:
            raise ValueError(
                f"采集结果的状态与结论不符: 期望 {expected.value!r}, 实际 {raw_status!r}")
    needs_error = status in (CaptureResultStatus.FAILED, CaptureResultStatus.UNCONFIRMED)
    if "error" in value:
        error = value["error"]
        if not needs_error:
            raise ValueError(f"采集结果的状态 {status.value!r} 不携带错误成员")
        if not isinstance(error, Mapping):
            raise ValueError(f"采集结果的错误必须是对象: {error!r}")
        validate_public_error(error)
    elif needs_error:
        raise ValueError(f"采集结果的状态 {status.value!r} 必须携带错误成员")
    members = set(value) - {"status", "error", "captured_count", "elapsed_s"}
    if members:
        raise ValueError(f"采集结果携带未知成员: {members!r}")
    if "captured_count" in value:
        count = value["captured_count"]
        if not is_json_integer(count) or not 0 <= count <= 9007199254740991:
            raise ValueError(f"采集结果的采集次数必须是安全整数: {count!r}")
    if "elapsed_s" in value:
        elapsed = value["elapsed_s"]
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, Decimal)):
            raise ValueError(f"采集结果的时长必须是精确数字: {elapsed!r}")
        if (isinstance(elapsed, Decimal) and not elapsed.is_finite()) or elapsed < 0:
            raise ValueError(f"采集结果的时长必须有限非负: {elapsed!r}")


def _evidence_object(name: str, value) -> None:
    """采集判定依据的结构：方法与实际观察，未知成员不接受。"""
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} 必须是对象: {value!r}")
    method = value.get("method")
    if not isinstance(method, str) or not method:
        raise ValueError(f"{name} 的方法必须是非空文本: {method!r}")
    if not isinstance(value.get("observation"), Mapping):
        raise ValueError(f"{name} 必须携带实际观察对象")
    members = set(value) - {
        "method", "observation", "wait_completed_event_id", "attempt_id"}
    if members:
        raise ValueError(f"{name} 携带未知成员: {sorted(members)}")


@dataclass(frozen=True)
class WaitCompletedSave:
    """等待完成事实的保存输入（CAPTURE_WAIT_CHANGED 完成分支）。

    已安排的等待到期后保存一次完成事实；预计检查时间与发送事
    实由事务核对，命令只携带活动身份与事实时刻。
    """

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


@dataclass(frozen=True)
class ResultSetSave:
    """一次结果集合核实事实的保存输入（RESULT_SET_CONFIRMED）。

    结论分支必须携带驱动结果规则标识与结构化依据；evidence 提
    供采集判定依据（时间与产物或设备证据），UNSATISFIED 的已知
    失败同样必须携带依据；BEGIN 只推进核实状态。
    """

    action_id: int
    occurred_at: int
    phase: ResultSetPhase
    contract: str | None = None
    observation: Mapping[str, Any] | None = None
    capture: Mapping[str, Any] | None = None
    evidence: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)
        if not isinstance(self.phase, ResultSetPhase):
            raise ValueError(f"核实分支必须是登记分区: {self.phase!r}")
        if self.phase is ResultSetPhase.BEGIN:
            for name in ("contract", "observation", "capture", "evidence", "error"):
                if getattr(self, name) is not None:
                    raise ValueError(f"开始分支不携带{name}")
            return
        if not isinstance(self.contract, str) or not self.contract:
            raise ValueError("结论分支必须携带驱动结果规则标识")
        if not isinstance(self.observation, Mapping):
            raise ValueError("结论分支必须携带结构化依据")
        if self.capture is not None:
            validate_capture_result(self.capture, phase=self.phase)
        if self.phase is ResultSetPhase.COMPLETE:
            _evidence_object("采集判定依据", self.evidence)
            if self.error is not None:
                raise ValueError("满足结论不携带核实错误")
        elif self.phase is ResultSetPhase.UNSATISFIED:
            _evidence_object("采集判定依据", self.evidence)
            if self.error is not None and not isinstance(self.error, Mapping):
                raise ValueError("核实错误必须是对象")
        else:
            if self.evidence is not None:
                raise ValueError("无法确认分支不判定采集结果")
            if self.error is not None and not isinstance(self.error, Mapping):
                raise ValueError("核实错误必须是对象")
        if self.error is not None:
            validate_public_error(self.error)


@dataclass(frozen=True)
class ResultRunClose:
    """录像活动核实流程收场申请的输入（不携带集合结论）。

    录像活动不适用结果集合核实，采集判定列保持为空；预算耗尽时
    仅把 results 责任流程按无法确认收场，动作结果由调用方按所属
    拍摄规则另行保存。
    """

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)

