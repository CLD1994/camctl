"""拍摄专属定义（第一版：拍照、录像、延时摄影）。

定义由首次受理事实构建并只读：动作类型、精确目标时长（毫秒）
与完成声明。公共管理模型设备无关；专属参数仅本模块解释。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from camctl.contracts.values import (
    MAX_OBJECT_ID,
    ObjectId,
    UtcMicros,
    seconds_to_duration_ms,
)
from camctl.contracts.json_values import is_json_integer
from camctl.devices.tasks import CaptureTask, CompletionMode, EndControl, StartReturn

__all__ = [
    "ActivityCapabilities",
    "ActivityObservationSave",
    "CaptureCompletion",
    "CaptureDefinition",
    "CaptureInput",
    "activity_capabilities",
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
    声明。第一版驱动无中途状态查询，输出范围均为任务独立范围。
    """
    if action_type == "camera_take_photo":
        validate_capture_spec(action_type, spec)
        return ActivityCapabilities(
            state_query_supported=0, stop_supported=0, safe_repeat_stop=0,
            start_return_meaning=int(StartReturn.COMPLETED),
            completion_mode=int(CompletionMode.DEVICE_EVIDENCE),
            ownership_mode=1, output_scope_json={})
    if action_type == "camera_record":
        validate_capture_spec(action_type, spec)
        return ActivityCapabilities(
            state_query_supported=0, stop_supported=1, safe_repeat_stop=1,
            start_return_meaning=int(StartReturn.STARTED),
            completion_mode=int(CompletionMode.TIME_AND_OUTPUTS),
            ownership_mode=1, output_scope_json={})
    if action_type == "camera_timelapse":
        validated = validate_capture_spec(action_type, spec)
        stop = 1 if validated["stop_supported"] else 0
        return ActivityCapabilities(
            state_query_supported=0, stop_supported=stop,
            safe_repeat_stop=stop,
            start_return_meaning=validated["start_return_meaning"],
            completion_mode=validated["completion_mode"],
            ownership_mode=1, output_scope_json={})
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


def validate_capture_spec(action_type: str, spec: Any) -> dict:
    """验证已保存的拍摄结构；不读取参数默认值或设备配置。"""
    if not isinstance(spec, dict):
        raise ValueError("拍摄执行定义必须是 JSON 对象")
    if action_type == "camera_take_photo":
        if spec:
            raise ValueError("拍照执行定义必须是空对象")
        return {}
    if action_type == "camera_record":
        if set(spec) != {"target_duration_ms"}:
            raise ValueError("录像定义必须只保存 target_duration_ms")
        return {"target_duration_ms": _integer(spec["target_duration_ms"], 1)}
    if action_type != "camera_timelapse":
        raise ValueError("动作类型不是拍摄动作")
    required = {"duration_based", "wait_after_send", "end_control", "stop_supported", "start_return_meaning", "completion_mode"}
    if not required <= set(spec):
        raise ValueError("延时定义缺少固定任务依据")
    for key in ("duration_based", "wait_after_send", "stop_supported"):
        if not isinstance(spec[key], bool):
            raise ValueError(f"{key} 必须是 JSON 布尔值")
    result = dict(spec)
    end = EndControl(_integer(spec["end_control"], 1))
    start = StartReturn(_integer(spec["start_return_meaning"], 1))
    completion = CompletionMode(_integer(spec["completion_mode"], 1))
    result.update(end_control=int(end), start_return_meaning=int(start), completion_mode=int(completion))
    expected = set(required)
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
    if action_type == "camera_record":
        if task.stop_supported is not True:
            raise ValueError("录像任务必须明确具备 stop_supported")
        return validate_capture_spec(action_type, {"target_duration_ms": seconds_to_duration_ms(task.target_duration_s)})
    spec = {"duration_based": task.duration_based, "wait_after_send": task.wait_after_send,
            "end_control":task.end_control, "stop_supported":task.stop_supported,
            "start_return_meaning":task.start_return_meaning, "completion_mode":task.completion_mode}
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
