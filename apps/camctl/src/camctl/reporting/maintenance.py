"""报告失败维护与时限。

文件或 worker 失败后保留责任并等待新触发：重扫与时间经过不构
成重试；新业务变化、显式同步或后续正常 run 才重新生成。报告失
败与状态库失败严格区分：前者保留普通业务继续，后者停止可靠执
行。规定报告首次失败触发日志副本（L5），副本失败不递归。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "MaintenanceLimits",
    "ReportMaintenanceContext",
    "ReportMaintenanceOutcome",
    "ReportMaintenanceResult",
    "maintain_reports",
]


class MaintenanceLimits:
    """报告维护的工程时限初值；运行内固定，联调再校准。"""

    __slots__ = ("startup_seconds", "total_seconds", "stop_grace_seconds")

    def __init__(
        self, startup_seconds: float, total_seconds: float, stop_grace_seconds: float
    ) -> None:
        for name, value in (
            ("startup_seconds", startup_seconds),
            ("total_seconds", total_seconds),
            ("stop_grace_seconds", stop_grace_seconds),
        ):
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} 必须是有限正秒数: {value!r}")
        self.startup_seconds = startup_seconds
        self.total_seconds = total_seconds
        self.stop_grace_seconds = stop_grace_seconds

    @classmethod
    def defaults(cls) -> "MaintenanceLimits":
        return cls(startup_seconds=10.0, total_seconds=300.0, stop_grace_seconds=5.0)


class ReportMaintenanceOutcome(enum.Enum):
    NO_PENDING_REPORT = "no_pending_report"
    PUBLISHED = "published"
    GENERATION_FAILED = "generation_failed"
    WAITING_NEW_TRIGGER = "waiting_new_trigger"


class ReportPorts(Protocol):
    """维护所需端口：变化观察、生成与发布。"""

    def has_new_changes(self) -> bool: ...

    async def generate(self, job: Any) -> bytes: ...

    async def publish(self, report_id: int, payload: bytes) -> bool: ...


@dataclass(frozen=True)
class ReportMaintenanceContext:
    """一次维护机会的上下文。"""

    limits: MaintenanceLimits
    ports: ReportPorts
    previous_attempt_failed: bool = False


@dataclass(frozen=True)
class ReportMaintenanceResult:
    outcome: ReportMaintenanceOutcome


async def maintain_reports(
    context: ReportMaintenanceContext,
) -> ReportMaintenanceResult:
    """处理一次报告维护机会。

    上次尝试失败且无新触发时等待（不重试）；有新变化时生成并
    发布；生成失败保留责任（首次失败触发 L5 日志副本，由装配
    侧接入），发布失败同样等待新触发。
    """
    if context.previous_attempt_failed and not context.ports.has_new_changes():
        return ReportMaintenanceResult(ReportMaintenanceOutcome.WAITING_NEW_TRIGGER)
    if not context.ports.has_new_changes():
        return ReportMaintenanceResult(ReportMaintenanceOutcome.NO_PENDING_REPORT)
    try:
        payload = await context.ports.generate(job=None)
    except Exception:
        return ReportMaintenanceResult(ReportMaintenanceOutcome.GENERATION_FAILED)
    published = await context.ports.publish(report_id=0, payload=payload)
    if not published:
        return ReportMaintenanceResult(ReportMaintenanceOutcome.GENERATION_FAILED)
    return ReportMaintenanceResult(ReportMaintenanceOutcome.PUBLISHED)
