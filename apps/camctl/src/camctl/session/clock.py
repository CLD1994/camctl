"""受理后时钟资格与受限执行。

墙钟检查在会话锁、状态库验证及适用的受理之后进行：可信历史下
界只在检查通过后建立或推进，每次 run 至多一次；复检等待不占用
写事务。时钟异常进入有限安全收场模式，不启动新的普通定时动作。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from camctl.contracts.clock import ClockPort
from camctl.contracts.values import OperationKey
from camctl.history.events import RowChange, RowImage
from camctl.history.validators import EventValidationError, register_guard
from camctl.persistence.transaction import CommandPlan, commit_operation

__all__ = [
    "ClockCheck",
    "ClockCheckInput",
    "ClockError",
    "ExecutionMode",
    "check_clock",
    "enter_execution",
    "register_clock_guard",
]


class ClockError(ValueError):
    """时钟检查输入或状态矛盾。"""


class ClockBecameUntrusted(RuntimeError):
    """运行中的最终资格检查发现墙钟不可信，须进入有限安全收场。"""


class ClockTrustStatus(Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class ExecutionMode(Enum):
    NORMAL = "normal"
    RESTRICTED = "restricted"
    FATAL = "fatal"


@dataclass(frozen=True)
class ClockCheckInput:
    """一次启动时钟检查的输入：已提交下界与部署的最小可信日期。"""

    lower_bound_micros: int | None
    min_plausible_micros: int
    recheck_delay_s: float
    recheck_count: int


@dataclass(frozen=True)
class ClockCheck:
    """一次读数的判定结果。

    new_lower_bound_micros 为 None 表示不需要保存下界变更（读数
    不可信，或 T <= 已有下界）。
    """

    status: ClockTrustStatus
    reading_micros: int
    lower_bound_before: int | None
    new_lower_bound_micros: int | None

    @property
    def trusted(self) -> bool:
        return self.status is ClockTrustStatus.TRUSTED

    @property
    def needs_bound_update(self) -> bool:
        return self.new_lower_bound_micros is not None


def check_clock(read_input: ClockCheckInput, clock: ClockPort) -> ClockCheck:
    """判定一次墙钟读数是否可信及需要的下界变更。

    读数不早于部署的最小可信日期即视为可信；可信且严格晚于已有
    下界（或尚无下界）时取得新下界。受理阶段未经校验的读数不调
    用本函数，不能借本函数推进下界。
    """
    reading = clock.utc_micros()
    if reading < read_input.min_plausible_micros:
        return ClockCheck(
            status=ClockTrustStatus.UNTRUSTED,
            reading_micros=reading,
            lower_bound_before=read_input.lower_bound_micros,
            new_lower_bound_micros=None,
        )
    bound = read_input.lower_bound_micros
    if bound is None or reading > bound:
        new_bound: int | None = reading
    else:
        new_bound = None
    return ClockCheck(
        status=ClockTrustStatus.TRUSTED,
        reading_micros=reading,
        lower_bound_before=bound,
        new_lower_bound_micros=new_bound,
    )


async def enter_execution(check: ClockCheck, context) -> ExecutionMode:
    """按检查结果进入普通或受限执行。

    检查通过且需要下界变更时，在进入普通执行前以短事务保存权威
    历史及投影；读数不可信时进入有限安全收场模式，不取得普通接
    纳资格。复检等待由调用方在事务外完成。
    """
    if not check.trusted:
        return ExecutionMode.RESTRICTED
    if check.needs_bound_update:
        outcome = context.repository.update_lower_bound(
            check, context.operation_key, context.owned
        )
        if outcome.kind.value != "completed":
            return ExecutionMode.FATAL
    return ExecutionMode.NORMAL


async def check_with_recheck(
    read_input: ClockCheckInput, clock: ClockPort
) -> ClockCheck:
    """带复检等待的完整启动检查：等待在写事务之外。"""
    check = check_clock(read_input, clock)
    attempts = 0
    while not check.trusted and attempts < read_input.recheck_count:
        await asyncio.sleep(read_input.recheck_delay_s)
        attempts += 1
        check = check_clock(read_input, clock)
    return check


def register_clock_guard() -> None:
    """注册可信时间下界事件的正式守卫。"""
    register_guard("clock", _clock_guard)


def _clock_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "runtime_state":
            continue
        before = row.before.values
        after = row.after.values
        old_bound = before.get("trusted_time_lower_bound")
        new_bound = after.get("trusted_time_lower_bound")
        if old_bound is None:
            if new_bound is None:
                raise EventValidationError("首次建立必须写入可信时间下界")
        elif new_bound is None or new_bound <= old_bound:
            raise EventValidationError("可信时间下界只严格提高")
        if after.get("trusted_time_event_id") != event.event_id:
            raise EventValidationError("下界依据必须引用本事件")
