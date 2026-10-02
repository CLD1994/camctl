"""可逆历史状态恢复。

正向按原事件顺序应用，逆向按相反顺序恢复前值；应用时核对
行值连续性，只使用原事实，不重新执行预算、时钟或副作用判断。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.history.validators import ValidatedEvent


class ReplayError(ValueError):
    """事件序列与镜像状态不连续，或恢复范围不完整。"""


@dataclass(frozen=True)
class EntityImage:
    """一个历史对象在某一位置的自身字段、自身成员与累计计数。

    rows 键为 (表名, 行 ID)，值为该行业务列值；跨线程共享时
    由调用方取得独立所有权。
    """

    entity_type: int
    entity_id: int
    exists: bool
    rows: Mapping[tuple[str, int], Mapping[str, Any]]
    last_event_id: int
    change_count: int


@dataclass(frozen=True)
class RestoreSeed:
    """恢复起点：初始状态、可靠快照或绑定 C 的当前投影。"""

    image: EntityImage
    boundary: HistoryBoundary


def _primary_table(entity_type: int) -> str:
    for spec in load_enum_registry()["history_objects"].values():
        if spec["id"] == entity_type:
            return spec["table"]
    raise ReplayError(f"未知历史对象编号: {entity_type}")


def _own_rows(event: ValidatedEvent, entity: tuple[int, int]):
    return (
        row
        for row in event.envelope.rows
        if event.row_owners.get((row.table, row.row_id)) == entity
    )


def _require_values(key, current: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for column, value in expected.items():
        if column not in current:
            raise ReplayError(f"行 {key} 缺少事件要求核对的列 {column}")
        if not json_equal(current[column], value):
            raise ReplayError(f"行 {key} 的 {column} 当前值与事件要求值不一致")


def apply_forward(image: EntityImage, event: ValidatedEvent) -> EntityImage:
    """把一条事件应用到对象镜像；只应用属于该对象的行。

    更新要求镜像中该行的对应列值与事件 before 值一致，
    创建要求行尚不存在；不一致说明事件序列缺失或错序。
    """
    entity = (image.entity_type, image.entity_id)
    rows = dict(image.rows)
    touched = False
    for row in _own_rows(event, entity):
        touched = True
        key = (row.table, row.row_id)
        if not row.before.exists:
            if key in rows:
                raise ReplayError(f"行 {key} 已存在，无法再次创建")
            rows[key] = dict(row.after.values)
        else:
            if key not in rows:
                raise ReplayError(f"行 {key} 不存在，无法更新")
            current = rows[key]
            _require_values(key, current, row.before.values)
            merged = dict(current)
            merged.update(row.after.values)
            rows[key] = merged
    primary = (_primary_table(image.entity_type), image.entity_id)
    exists = primary in rows
    return replace(
        image,
        rows=rows,
        exists=exists,
        last_event_id=event.envelope.event_id,
        change_count=image.change_count + (1 if entity in event.references else 0),
    ) if touched or entity in event.references else replace(
        image, last_event_id=event.envelope.event_id
    )


def apply_reverse(image: EntityImage, event: ValidatedEvent) -> EntityImage:
    """按相反顺序恢复一条事件作用前的对象镜像。

    更新恢复 before 值（恢复列覆盖镜像对应列），创建则移除该行；
    更新要求镜像中该行与事件 after 值一致。
    """
    entity = (image.entity_type, image.entity_id)
    rows = dict(image.rows)
    touched = False
    for row in _own_rows(event, entity):
        touched = True
        key = (row.table, row.row_id)
        if not row.before.exists:
            if key not in rows:
                raise ReplayError(f"行 {key} 不存在，无法逆向移除")
            if not json_equal(dict(rows[key]), dict(row.after.values)):
                raise ReplayError(f"行 {key} 的完整业务值与创建后的事实不一致")
            del rows[key]
        else:
            if key not in rows:
                raise ReplayError(f"行 {key} 不存在，无法逆向更新")
            current = rows[key]
            _require_values(key, current, row.after.values)
            merged = dict(current)
            merged.update(row.before.values)
            rows[key] = merged
    primary = (_primary_table(image.entity_type), image.entity_id)
    exists = primary in rows
    return replace(
        image,
        rows=rows,
        exists=exists,
        last_event_id=event.envelope.event_id,
        change_count=image.change_count - (1 if entity in event.references else 0),
    ) if touched or entity in event.references else replace(
        image, last_event_id=event.envelope.event_id
    )


def restore(
    seed: RestoreSeed,
    events: Iterable[ValidatedEvent],
    target: HistoryBoundary,
) -> EntityImage:
    """把镜像恢复到完整历史边界 target。

    seed 边界早于 target 时正向应用其间事件，晚于 target 时
    逆向恢复；事件按位置连续应用，缺失中间事件时因行值或
    存在性不连续而拒绝。
    """
    if target.txn_id == 0 and target.last_event_id == 0:
        if seed.boundary.txn_id == 0:
            return seed.image
        ordered = sorted(events, key=lambda event: event.envelope.event_id, reverse=True)
        image = seed.image
        for event in ordered:
            if event.envelope.event_id <= 0:
                continue
            image = apply_reverse(image, event)
        if image.change_count != 0 or image.exists:
            raise ReplayError("逆向到初始化边界后镜像必须为空")
        return image

    if seed.boundary.last_event_id <= target.last_event_id:
        ordered = sorted(events, key=lambda event: event.envelope.event_id)
        image = seed.image
        for event in ordered:
            if event.envelope.event_id > target.last_event_id:
                continue
            if event.envelope.event_id <= seed.boundary.last_event_id and seed.boundary.last_event_id > 0:
                continue
            image = apply_forward(image, event)
        return image

    ordered = sorted(events, key=lambda event: event.envelope.event_id, reverse=True)
    image = seed.image
    for event in ordered:
        if event.envelope.event_id <= target.last_event_id:
            continue
        image = apply_reverse(image, event)
    return image
