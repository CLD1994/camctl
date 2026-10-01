"""累计确认（ACK）与同步结束规则。

单调累计业务水位：有效 ACK 的报告存在且覆盖水位严格提高时推进
累计身份；同水位 ACK 仍可结束合格同步但不覆盖累计报告身份。较
旧报告 ID 不代表旧水位，判断只依据保存的覆盖范围。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID, ObjectId

__all__ = [
    "AckDecision",
    "AckDisposition",
    "AckFacts",
    "AckInput",
    "AckReport",
    "SyncChanges",
    "SyncResponsibility",
    "decide_ack",
    "decide_sync_cancel",
    "qualifies_sync",
    "validate_watermark",
]


class AckDisposition(Enum):
    ABSORBED = "absorbed"
    VALID_NOT_ADVANCING = "valid_not_advancing"
    INVALID = "invalid"
    READ_ERROR = "read_error"


@dataclass(frozen=True)
class AckInput:
    """一次输入携带的 ACK：被确认的报告身份。"""

    report_id: int

    def __post_init__(self) -> None:
        ObjectId(self.report_id)


def validate_watermark(value: int, field: str) -> None:
    """报告和同步规则共用的精确业务水位检查。"""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
        raise ConsistencyError(f"{field} 必须是公共范围内的精确业务水位: {value!r}")


@dataclass(frozen=True)
class AckReport:
    """ACK 使用的已登记报告依据；文件处理状态不影响确认资格。"""

    report_id: int
    from_wm: int
    to_wm: int
    frozen_event_id: int

    def __post_init__(self) -> None:
        ObjectId(self.report_id)
        validate_watermark(self.from_wm, "from_wm")
        validate_watermark(self.to_wm, "to_wm")
        if self.from_wm > self.to_wm:
            raise ConsistencyError("报告左端不能晚于右端")
        if (isinstance(self.frozen_event_id, bool)
                or not isinstance(self.frozen_event_id, int)
                or not 0 <= self.frozen_event_id <= MAX_OBJECT_ID):
            raise ConsistencyError("报告冻结位置必须是非负精确事件编号")


@dataclass(frozen=True)
class AckFacts:
    """ACK 判定所需事实：当前累计与被引用报告的可靠覆盖。"""

    acknowledged_wm: int
    acknowledged_report_id: int | None
    report: AckReport | None = None
    read_failed: bool = False

    def __post_init__(self) -> None:
        validate_watermark(self.acknowledged_wm, "acknowledged_wm")
        if self.acknowledged_report_id is not None:
            ObjectId(self.acknowledged_report_id)
        elif self.acknowledged_wm != 0:
            raise ConsistencyError("非零累计确认位置必须保存报告身份")
        if self.report is not None and not isinstance(self.report, AckReport):
            raise ConsistencyError("ACK 必须使用有明确结构的报告依据")
        if type(self.read_failed) is not bool:
            raise ConsistencyError("报告读取结果必须是布尔值")


@dataclass(frozen=True)
class AckDecision:
    """ACK 判定结果：新累计值只在严格推进时给出。"""

    disposition: AckDisposition
    new_acknowledged_wm: int
    new_acknowledged_report_id: int | None


@dataclass(frozen=True)
class SyncResponsibility:
    """同步的固定起点、开始事务末位及所属动作。"""

    sync_id: int
    action_id: int
    from_wm: int
    started_boundary_event_id: int

    def __post_init__(self) -> None:
        ObjectId(self.sync_id)
        ObjectId(self.action_id)
        ObjectId(self.started_boundary_event_id)
        validate_watermark(self.from_wm, "from_wm")


@dataclass(frozen=True)
class SyncChanges:
    """取消同步后的实际变更范围。"""

    ended_sync_ids: tuple[int, ...]
    stopped_action_ids: tuple[int, ...]
    preserved_action_ids: tuple[int, ...] = ()


def decide_ack(ack: AckInput, facts: AckFacts) -> AckDecision:
    """判定一次 ACK 的吸收结果。

    读取失败与未知报告分别分类，不折叠为无效或缺失；有效 ACK 按
    覆盖水位决定是否推进累计身份。
    """
    if facts.read_failed:
        return AckDecision(
            disposition=AckDisposition.READ_ERROR,
            new_acknowledged_wm=facts.acknowledged_wm,
            new_acknowledged_report_id=facts.acknowledged_report_id,
        )
    if facts.report is None:
        return AckDecision(
            disposition=AckDisposition.INVALID,
            new_acknowledged_wm=facts.acknowledged_wm,
            new_acknowledged_report_id=facts.acknowledged_report_id,
        )
    if facts.report.report_id != ack.report_id:
        raise ConsistencyError("ACK 报告事实与查询身份不符")
    report_wm = facts.report.to_wm
    if report_wm > facts.acknowledged_wm:
        return AckDecision(
            disposition=AckDisposition.ABSORBED,
            new_acknowledged_wm=report_wm,
            new_acknowledged_report_id=ack.report_id,
        )
    return AckDecision(
        disposition=AckDisposition.VALID_NOT_ADVANCING,
        new_acknowledged_wm=facts.acknowledged_wm,
        new_acknowledged_report_id=facts.acknowledged_report_id,
    )


def qualifies_sync(report: AckReport, sync: SyncResponsibility) -> bool:
    """报告须覆盖固定起点，且其历史包含同步开始的完整事务。"""
    return (report.from_wm <= sync.from_wm
            and report.frozen_event_id >= sync.started_boundary_event_id)


def decide_sync_cancel(facts: Mapping[str, Any]) -> SyncChanges:
    """取消未结束同步责任的实际变更范围。

    未执行的报告动作可以停止；已开始与已成功的动作保持实际结
    果；共享的报告生成不在取消范围内。
    """
    ended = tuple(sorted(facts.get("cancelled_sync_ids", ())))
    stopped: list[int] = []
    preserved: list[int] = []
    for action_id, state in sorted(facts.get("report_actions", {}).items()):
        if state == "pending":
            stopped.append(action_id)
        else:
            preserved.append(action_id)
    return SyncChanges(
        ended_sync_ids=ended,
        stopped_action_ids=tuple(stopped),
        preserved_action_ids=tuple(preserved),
    )
