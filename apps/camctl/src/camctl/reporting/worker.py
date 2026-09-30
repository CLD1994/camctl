"""报告生成进程生命周期。

结果与退出是两个维度：结果已可靠到达则生成成功，仅残留文件不
构成成功；停止/超时先确认实际退出，才允许清理与复用；迟到的状
态库错误只记录，不改写已确认结果。进程 spawn 与父死亡保护在
集成侧（真实子进程）验证。
"""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from typing import Awaitable, Callable

__all__ = [
    "GenerationJob",
    "GenerationOutcome",
    "GenerationResult",
    "WorkerSettlement",
    "WorkerStopReason",
    "decide_generation_outcome",
    "settle_worker",
]


@dataclass(frozen=True)
class GenerationJob:
    """一次生成任务：报告身份与预期负载。"""

    report_id: int
    payload: bytes


@dataclass(frozen=True)
class GenerationResult:
    """生成结束的机器结果。"""

    job: GenerationJob
    outcome: "GenerationOutcome"
    payload: bytes | None = None


class GenerationOutcome(enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    STOPPED = "stopped"
    TIMEOUT = "timeout"


class WorkerStopReason(enum.Enum):
    SESSION_CLOSING = "session_closing"
    TIMEOUT = "timeout"


@dataclass
class WorkerSettlement:
    """worker 收场事实：结果、清理与迟到错误。"""

    outcome: GenerationOutcome
    stop_reason: WorkerStopReason | None
    cleaned: bool = False
    late_error: BaseException | None = None

    @property
    def is_success(self) -> bool:
        return self.outcome is GenerationOutcome.SUCCESS

    def record_late_error(self, error: BaseException) -> None:
        """迟到错误只记录诊断，不改写已确认结果。"""
        self.late_error = error


def decide_generation_outcome(
    *,
    result: bytes | None,
    stop_reason: WorkerStopReason | None,
    exit_failed: bool,
) -> GenerationOutcome:
    """按结果、停止与退出事实判定生成结果分区。"""
    if result is not None and not exit_failed:
        return GenerationOutcome.SUCCESS
    if stop_reason is WorkerStopReason.TIMEOUT:
        return GenerationOutcome.TIMEOUT
    if stop_reason is not None:
        return GenerationOutcome.STOPPED
    return GenerationOutcome.FAILED


async def settle_worker(
    *,
    outcome: GenerationOutcome,
    stop_reason: WorkerStopReason | None,
    confirm_exit: Callable[[], Awaitable[None]],
    cleanup: Callable[[], Awaitable[None]],
) -> WorkerSettlement:
    """确认实际退出后清理临时资源；清理不先于退出确认。"""
    settlement = WorkerSettlement(outcome=outcome, stop_reason=stop_reason)
    await confirm_exit()
    await cleanup()
    settlement.cleaned = True
    return settlement


async def generate(job: GenerationJob, worker_process) -> GenerationResult:
    """驱动一次生成并等待实际结束（worker_process 为进程端口）。"""
    result_bytes = await asyncio.to_thread(worker_process.run, job)
    outcome = decide_generation_outcome(
        result=result_bytes, stop_reason=None, exit_failed=False
    )
    return GenerationResult(job=job, outcome=outcome, payload=result_bytes)


async def stop_worker(reason: WorkerStopReason) -> WorkerSettlement:
    """请求停止：返回待确认的收场（退出确认由 settle_worker 完成）。"""
    return WorkerSettlement(outcome=GenerationOutcome.STOPPED, stop_reason=reason)
