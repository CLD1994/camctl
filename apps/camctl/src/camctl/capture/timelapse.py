"""延时摄影等待安排与跨重启恢复。

预计检查时间 = 发送成功日期时间 + 目标采集时长 + 驱动必要余量 +
部署额外等待；持久化或日志延迟不改变该安排。本次运行用单调钟
等待；重启以原发送日期时间计算剩余等待并采用本次额外等待。等
待计划不制造设备完成观察；原生完成后返回与已保存等待完成事实
直接进入产物核实；主机负责结束时目标时长安排停止操作。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CaptureWaitConfig",
    "ClockReading",
    "EndControl",
    "StartReturn",
    "TimelapseState",
    "WaitKind",
    "WaitPlan",
    "plan_capture_wait",
]

#: 毫秒到微秒与纳秒的换算。
_MS_TO_US = 1_000
_MS_TO_NS = 1_000_000


class StartReturn(Enum):
    """启动成功响应的含义（首次受理固定）。"""

    SENT = "sent"
    STARTED = "started"
    COMPLETED = "completed"


class EndControl(Enum):
    """任务的结束责任。"""

    DEVICE = "device"
    HOST = "host"


class WaitKind(Enum):
    """等待安排的分区。"""

    WAIT_THEN_CHECK = "wait_then_check"
    RESUME_WAIT = "resume_wait"
    CHECK_NOW = "check_now"
    VERIFY_FILES_NOW = "verify_files_now"
    HOST_CONTROL_STOP = "host_control_stop"
    CLOCK_UNAVAILABLE = "clock_unavailable"
    UNRESOLVABLE = "unresolvable"


@dataclass(frozen=True)
class CaptureWaitConfig:
    """等待安排的配置输入。

    driver_margin_ms 是驱动必要余量，首次受理确定后不变；extra_wait_ms
    是部署额外等待，本次运行采用实际值。部署只能增加等待，不能用
    负值抵消驱动要求。
    """

    target_duration_ms: int
    driver_margin_ms: int
    extra_wait_ms: int = 0

    def __post_init__(self) -> None:
        for name in ("target_duration_ms", "driver_margin_ms", "extra_wait_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数毫秒: {value!r}")
        if self.target_duration_ms <= 0:
            raise ValueError("目标采集时长必须为正")


@dataclass(frozen=True)
class TimelapseState:
    """等待判定的已保存事实。"""

    clock_trusted: bool
    start_return: StartReturn
    end_control: EndControl
    sent_at_utc: int | None
    anchor_monotonic_ns: int | None
    restart: bool = False
    wait_completed_fact_saved: bool = False
    device_completion_evidence: bool = False


@dataclass(frozen=True)
class ClockReading:
    """本次判定的时钟读数：可信墙钟（微秒）与会话单调钟（纳秒）。"""

    utc_us: int
    monotonic_ns: int


@dataclass(frozen=True)
class WaitPlan:
    """等待安排结果。

    device_state_is_observed_ended 恒为 False：等待与到期不构成设备
    完成观察，完成证据只来自可靠事实。
    """

    kind: WaitKind
    check_at_utc: int | None = None
    monotonic_deadline_ns: int | None = None
    remaining_ms: int | None = None
    device_state_is_observed_ended: bool = False


def plan_capture_wait(
    state: TimelapseState, config: CaptureWaitConfig, now: ClockReading
) -> WaitPlan:
    """按已保存事实与本次配置安排延时摄影的等待。

    预计检查时间只由发送时点与配置组成，本判定的墙钟读数不参与
    其计算；重启用原发送日期时间计算剩余并换算为本次单调钟等待。
    """
    if not state.clock_trusted:
        return WaitPlan(kind=WaitKind.CLOCK_UNAVAILABLE)
    if state.wait_completed_fact_saved or state.device_completion_evidence:
        return WaitPlan(kind=WaitKind.VERIFY_FILES_NOW)
    if state.start_return is StartReturn.COMPLETED:
        return WaitPlan(kind=WaitKind.VERIFY_FILES_NOW)
    if state.end_control is EndControl.HOST:
        anchor = state.anchor_monotonic_ns
        if anchor is None:
            return WaitPlan(kind=WaitKind.UNRESOLVABLE)
        return WaitPlan(
            kind=WaitKind.HOST_CONTROL_STOP,
            monotonic_deadline_ns=anchor + config.target_duration_ms * _MS_TO_NS,
        )
    sent = state.sent_at_utc
    if sent is None:
        return WaitPlan(kind=WaitKind.UNRESOLVABLE)
    total_ms = (
        config.target_duration_ms + config.driver_margin_ms + config.extra_wait_ms
    )
    check_at_utc = sent + total_ms * _MS_TO_US
    if state.restart and state.anchor_monotonic_ns is None:
        remaining_ms = (check_at_utc - now.utc_us) // _MS_TO_US
        if remaining_ms <= 0:
            return WaitPlan(kind=WaitKind.CHECK_NOW, check_at_utc=check_at_utc)
        return WaitPlan(
            kind=WaitKind.RESUME_WAIT,
            check_at_utc=check_at_utc,
            remaining_ms=remaining_ms,
            monotonic_deadline_ns=now.monotonic_ns + remaining_ms * _MS_TO_NS,
        )
    anchor = state.anchor_monotonic_ns
    if anchor is None:
        return WaitPlan(kind=WaitKind.UNRESOLVABLE)
    return WaitPlan(
        kind=WaitKind.WAIT_THEN_CHECK,
        check_at_utc=check_at_utc,
        monotonic_deadline_ns=anchor + total_ms * _MS_TO_NS,
    )
