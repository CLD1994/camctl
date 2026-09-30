"""冻结报告的不可变模型与完整性验证。

报告字段内容直接消费 K4.project_public；本模块只定义冻结身份、
完整历史边界、覆盖水位与入选范围的完整性，不复制字段计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY

__all__ = ["FrozenReport", "validate_frozen_report"]

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
