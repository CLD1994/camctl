"""累计确认（ACK）与同步结束规则。

单调累计业务水位：有效 ACK 的报告存在且覆盖水位严格提高时推进
累计身份；同水位 ACK 仍可结束合格同步但不覆盖累计报告身份。较
旧报告 ID 不代表旧水位，判断只依据保存的覆盖范围。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "AckDecision",
    "AckDisposition",
    "AckFacts",
    "AckInput",
    "SyncChanges",
    "SyncResponsibility",
    "decide_ack",
    "decide_sync_cancel",
    "qualifies_sync",
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


@dataclass(frozen=True)
class AckFacts:
    """ACK 判定所需事实：当前累计与被引用报告的可靠覆盖。"""

    acknowledged_wm: int
    acknowledged_report_id: int | None
    report: Any | None = None
    report_known: bool = True
    read_failed: bool = False


@dataclass(frozen=True)
class AckDecision:
    """ACK 判定结果：新累计值只在严格推进时给出。"""

    disposition: AckDisposition
    new_acknowledged_wm: int
    new_acknowledged_report_id: int | None


@dataclass(frozen=True)
class SyncResponsibility:
    """一条显式同步责任：覆盖区间与所属动作。"""

    sync_id: int
    action_id: int
    from_wm: int
    to_wm: int


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
    if not facts.report_known or facts.report is None:
        return AckDecision(
            disposition=AckDisposition.INVALID,
            new_acknowledged_wm=facts.acknowledged_wm,
            new_acknowledged_report_id=facts.acknowledged_report_id,
        )
    report_wm = int(facts.report["to_wm"])
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


def qualifies_sync(report: Any, sync: SyncResponsibility) -> bool:
    """报告覆盖范围是否完整满足同步责任。"""
    from_wm = int(report["from_wm"])
    to_wm = int(report["to_wm"])
    return from_wm <= sync.from_wm and to_wm >= sync.to_wm


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
