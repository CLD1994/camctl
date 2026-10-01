"""版本化历史事件信封与登记访问。

事件类型、分支、行权限与状态模型全部来自包内权威登记
（event-transitions.json）；本模块不复制编号清单。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from camctl.bootstrap.resources import resource_bytes

_EVENT_REGISTRY_RESOURCE = "registry/event-transitions.json"

#: 权威结构资源；事务内核与事件校验共同使用同一份 SQL。
SCHEMA_RESOURCES = (
    "sql/core.sql",
    "sql/workflows.sql",
    "sql/files.sql",
    "sql/operations.sql",
    "sql/reports.sql",
    "sql/history.sql",
)


class HistoryEventError(ValueError):
    """事件登记不可用或事件信封结构非法。"""


@dataclass(frozen=True)
class RowImage:
    """行在某一时刻的存在性与发生变化（或创建时全部）的业务列值。"""

    exists: bool
    values: Mapping[str, Any]


@dataclass(frozen=True)
class RowChange:
    """一个事件内对同一物理行的一次变更。"""

    table: str
    row_id: int
    before: RowImage
    after: RowImage


@dataclass(frozen=True)
class EventEnvelope:
    """一条权威历史事件（正文版本 1）。"""

    event_id: int
    transaction_id: int
    event_type: int
    event_version: int
    occurred_at: int
    clock_status: int
    change_seq: int | None
    reason: int
    evidence: Mapping[str, Any]
    rows: tuple[RowChange, ...]


@lru_cache(maxsize=1)
def foreign_key_targets() -> dict[tuple[str, str], str]:
    """从权威 SQL 收集 (表, 列) -> 被引用表 的单列外键指向。"""
    targets: dict[tuple[str, str], str] = {}
    for name in SCHEMA_RESOURCES:
        sql = resource_bytes(name).decode("utf-8")
        for block in re.finditer(r"CREATE TABLE\s+(\w+)\s*\((.*?)\)\s*STRICT", sql, re.DOTALL):
            table, body = block.group(1), block.group(2)
            for match in re.finditer(
                r"(\w+)\s+INTEGER[^,)]*?REFERENCES\s+(\w+)\(id\)", body
            ):
                targets[(table, match.group(1))] = match.group(2)
    return targets


@lru_cache(maxsize=1)
def load_event_registry() -> dict[str, Any]:
    try:
        registry = json.loads(resource_bytes(_EVENT_REGISTRY_RESOURCE))
    except (OSError, ValueError) as error:
        raise HistoryEventError("事件转换登记不可用") from error
    if registry.get("format_version") != 1:
        raise HistoryEventError("事件转换登记版本不受支持")
    return registry


def event_type_name(type_id: int) -> str:
    """按整数编号取得事件类型名；未知编号报错。"""
    for name, spec in load_event_registry()["events"].items():
        if spec["id"] == type_id:
            return name
    raise HistoryEventError(f"未知事件类型: {type_id}")


def branch_of(type_id: int, reason: int) -> tuple[str, dict[str, Any]]:
    """按事件类型与分支编号取得 (分支名, 分支规格)。"""
    name = event_type_name(type_id)
    for branch_name, branch in load_event_registry()["events"][name]["branches"].items():
        if branch["reason"] == reason:
            return branch_name, branch
    raise HistoryEventError(f"事件 {name} 没有分支编号 {reason}")


def business_columns(table: str) -> frozenset[str]:
    """该业务投影表全部业务列（不含主键与派生历史元数据）。"""
    spec = load_event_registry()["tables"].get(table)
    if spec is None:
        raise HistoryEventError(f"未知业务投影表: {table}")
    return frozenset(spec["immutable"] + spec["mutable"] + spec["write_once"])


def changeable_columns(table: str) -> frozenset[str]:
    """允许出现在更新 values 中的列（可变列与一次写列）。"""
    spec = load_event_registry()["tables"][table]
    return frozenset(spec["mutable"] + spec["write_once"])
