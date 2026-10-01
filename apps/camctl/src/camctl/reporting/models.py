"""冻结报告的不可变模型与完整性验证。

报告字段内容直接消费 K4.project_public；本模块只定义冻结身份、
完整历史边界、覆盖水位与入选范围的完整性，不复制字段计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Tuple

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.reporting.ack import validate_watermark

__all__ = [
    "FrozenReport", "ReportDecision", "ReportDecisionKind", "ReportOpportunity",
    "ReportSelection", "validate_frozen_report",
]

#: 入选范围的对象类型（历史对象登记中 report_target 为真者）。
_SCOPE_TYPES = frozenset(
    spec["id"]
    for spec in load_enum_registry()["history_objects"].values()
    if spec.get("report_target")
)


@dataclass(frozen=True)
class FrozenReport:
    """一份不可变逻辑报告：身份、冻结依据、覆盖范围与入选对象。"""

    report_id: int
    boundary: HistoryBoundary | None
    from_wm: int
    to_wm: int
    format_version: int
    scope: Tuple[Tuple[str, Tuple[int, ...]], ...] = ()


class ReportDecisionKind(Enum):
    GENERATE = "generate"
    REUSE = "reuse"
    SKIP = "skip"


@dataclass(frozen=True)
class ReportOpportunity:
    """同一完整边界处的累计 ACK、业务终点与全部有效同步要求。"""

    boundary: HistoryBoundary
    latest_change_wm: int
    acknowledged_wm: int
    sync_from_wm: int | None = None
    sync_started_boundary_event_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, HistoryBoundary):
            raise ConsistencyError("报告机会必须使用完整历史边界")
        validate_watermark(self.latest_change_wm, "latest_change_wm")
        validate_watermark(self.acknowledged_wm, "acknowledged_wm")
        if self.acknowledged_wm > self.latest_change_wm:
            raise ConsistencyError("累计确认位置不能晚于冻结业务终点")
        if (self.sync_from_wm is None) != (self.sync_started_boundary_event_id is None):
            raise ConsistencyError("同步起点与开始历史必须共同存在或共同省略")
        if self.sync_from_wm is not None:
            validate_watermark(self.sync_from_wm, "sync_from_wm")
            ObjectId(self.sync_started_boundary_event_id)
            if self.sync_from_wm > self.latest_change_wm:
                raise ConsistencyError("同步起点不能晚于冻结业务终点")
            if self.sync_started_boundary_event_id > self.boundary.last_event_id:
                raise ConsistencyError("冻结历史必须包含所有有效同步的开始")


@dataclass(frozen=True)
class ReportDecision:
    kind: ReportDecisionKind
    from_wm: int = 0
    to_wm: int = 0
    reused_report_id: int | None = None


@dataclass(frozen=True)
class ReportSelection:
    """仓储的实际选择；复用仅证明内容依据足够，不证明文件已发布。"""

    kind: ReportDecisionKind
    report: FrozenReport | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReportDecisionKind):
            raise ConsistencyError("报告选择必须使用已定义的分支")
        if self.kind is ReportDecisionKind.SKIP:
            if self.report is not None:
                raise ConsistencyError("跳过报告时不能提供报告依据")
        elif not isinstance(self.report, FrozenReport):
            raise ConsistencyError("生成或复用必须提供实际报告依据")


def validate_frozen_report(report: FrozenReport) -> None:
    """核对冻结依据的完整性：完整 H、固定范围与生成元数据。

    合法初始边界（无任何报告目标）允许空范围；其他边界缺少范围
    或对象身份非法均拒绝。
    """
    if report.format_version != 1:
        raise ValueError(f"报告格式版本不受支持: {report.format_version}")
    if report.report_id is None or report.report_id < 1:
        raise ValueError("报告身份必须是正整数")
    if report.boundary is None:
        raise ValueError("冻结报告缺少完整历史边界 H")
    if report.boundary != INITIAL_BOUNDARY:
        if report.boundary.txn_id < 1 or report.boundary.last_event_id < 1:
            raise ValueError(f"冻结边界不完整: {report.boundary}")
    if report.from_wm < 0 or report.to_wm < report.from_wm:
        raise ValueError(
            f"覆盖水位范围不合法: from={report.from_wm}, to={report.to_wm}"
        )
    for entity_name, ids in report.scope:
        objects = load_enum_registry()["history_objects"]
        if entity_name not in objects or not objects[entity_name].get("report_target"):
            raise ValueError(f"范围包含非报告对象类型: {entity_name!r}")
        for entity_id in ids:
            if not isinstance(entity_id, int) or isinstance(entity_id, bool) or entity_id < 1:
                raise ValueError(f"{entity_name} 的对象身份非法: {entity_id!r}")
