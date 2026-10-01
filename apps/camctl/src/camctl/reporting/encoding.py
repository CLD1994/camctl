"""固定报告排版、精确数字及有容量上限的字节输出。

公开字段由共享投影计算，实体层级由同一字段登记决定。此处只
编码传入的固定事实；冻结历史的分页恢复由生成调用方负责。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from camctl.contracts.public_projection import (
    OMIT, ProjectionInput, project_public, projection_structure,
)
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import ObjectId, parse_object_id

__all__ = ["ReportDocument", "encode_number", "encode_report", "iter_report_chunks"]

_SCHEMA = "protocol/status-report.schema.json"
_DEFAULT_BUFFER_SIZE = 64 * 1024
EntityEntry = tuple[str, int, tuple[str, tuple[int, ...]]]


@dataclass(frozen=True)
class ReportDocument:
    """传入编码器的报告身份与入选根实体；不承担历史读取。"""

    report_id: str
    from_wm: int
    to_wm: int
    plans: tuple[EntityEntry, ...] = ()
    diagnostics: tuple[EntityEntry, ...] = ()


def encode_number(value: int | Decimal) -> bytes:
    """按报告数值分区输出统一精确写法，不受 Decimal 上下文影响。"""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError("报告数值必须是整数或 Decimal")
    number = Decimal(value) if isinstance(value, int) else value
    if not number.is_finite():
        raise ValueError("报告数值必须有限")
    sign, digits, exponent = number.as_tuple()
    coefficient = "".join(str(digit) for digit in digits)
    shortened = coefficient.rstrip("0")
    if not shortened:
        return b"0"
    exponent += len(coefficient) - len(shortened)
    adjusted = len(shortened) + exponent - 1
    if -6 <= adjusted < 21:
        point = len(shortened) + exponent
        if point <= 0:
            text = "0." + "0" * -point + shortened
        elif point >= len(shortened):
            text = shortened + "0" * (point - len(shortened))
        else:
            text = shortened[:point] + "." + shortened[point:]
    else:
        text = shortened[0]
        if len(shortened) > 1:
            text += "." + shortened[1:]
        text += "e" + str(adjusted)
    return (("-" if sign else "") + text).encode("ascii")


def _string(value: str) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _iter_value(value: Any) -> Iterator[bytes]:
    if value is None:
        yield b"null"
    elif value is True:
        yield b"true"
    elif value is False:
        yield b"false"
    elif isinstance(value, (int, Decimal)):
        yield encode_number(value)
    elif isinstance(value, str):
        yield _string(value)
    elif isinstance(value, dict):
        yield from _iter_object(value)
    elif isinstance(value, list):
        yield b"["
        for position, member in enumerate(value):
            if position:
                yield b","
            yield from _iter_value(member)
        yield b"]"
    else:
        raise ValueError(f"报告字段不是合法精确 JSON 值: {type(value).__name__}")


def _iter_object(value: dict, entity: str | None = None) -> Iterator[bytes]:
    if any(not isinstance(key, str) for key in value):
        raise ValueError("报告对象字段名必须是字符串")
    children = () if entity is None else projection_structure(entity).entity_fields
    child_names = {name for name, _ in children}
    yield b"{"
    wrote = False
    for name in sorted(value.keys() - child_names):
        if wrote:
            yield b","
        yield _string(name)
        yield b":"
        yield from _iter_value(value[name])
        wrote = True
    for name, child_entity in children:
        if name not in value:
            continue
        members = value[name]
        if not isinstance(members, list):
            raise ValueError(f"实体集合 {name} 必须是数组")
        if not members:
            continue
        identity = projection_structure(child_entity).identity_field
        if identity is None:
            raise ValueError(f"实体 {child_entity} 缺少登记身份字段")
        ordered = sorted(members, key=lambda member: parse_object_id(member[identity]))
        previous = None
        if wrote:
            yield b","
        yield _string(name)
        yield b":["
        for position, member in enumerate(ordered):
            current = parse_object_id(member[identity])
            if current == previous:
                raise ValueError(f"实体集合 {name} 的身份重复: {current}")
            previous = current
            if position:
                yield b","
            yield from _iter_object(member, child_entity)
        yield b"]"
        wrote = True
    yield b"}"


def _projected_entities(entries: tuple[EntityEntry, ...], entity: str, facts: Mapping) -> Iterator[dict]:
    previous = None
    for source_entity, row_id, selected in sorted(entries, key=lambda entry: ObjectId(entry[1])):
        if source_entity != entity:
            raise ValueError(f"集合要求 {entity}，实际为 {source_entity}")
        if row_id == previous:
            raise ValueError(f"实体 {entity} 的身份重复: {row_id}")
        previous = row_id
        selection = {selected[0]: selected[1]} if selected[0] else {}
        fragment = project_public(ProjectionInput(entity, row_id, facts, selection))
        if fragment is not OMIT:
            yield fragment


def _iter_document(document: ReportDocument, facts: Mapping) -> Iterator[bytes]:
    metadata = {"report_id": document.report_id, "from_wm": document.from_wm, "to_wm": document.to_wm}
    parse_object_id(document.report_id)
    validate_document(_SCHEMA, metadata)
    collections = {"plans": document.plans, "plan_file_diagnostics": document.diagnostics}
    yield b"{"
    for position, name in enumerate(sorted(metadata)):
        if position:
            yield b","
        yield _string(name)
        yield b":"
        yield from _iter_value(metadata[name])
    for name, entity in projection_structure("report").entity_fields:
        opened = False
        for fragment in _projected_entities(collections[name], entity, facts):
            # 单个根实体与真实公共结构组合校验；不会建立整份报告列表。
            validate_document(_SCHEMA, {**metadata, name: [fragment]})
            if opened:
                yield b","
            else:
                yield b"," + _string(name) + b":["
                opened = True
            yield from _iter_object(fragment, entity)
        if opened:
            yield b"]"
    yield b"}\n"


def iter_report_chunks(document: ReportDocument, facts: Mapping, *, buffer_size: int) -> Iterator[bytes]:
    """输出不超过容量的片段；后续投影失败时此前字节仍是未完成报告。"""
    if isinstance(buffer_size, bool) or not isinstance(buffer_size, int) or buffer_size < 1:
        raise ValueError("报告字节缓冲容量必须是正整数")
    pending = bytearray()
    for token in _iter_document(document, facts):
        offset = 0
        while offset < len(token):
            length = min(buffer_size - len(pending), len(token) - offset)
            pending.extend(token[offset:offset + length])
            offset += length
            if len(pending) == buffer_size:
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


def encode_report(document: ReportDocument, facts: Mapping) -> bytes:
    """完整编码的便利入口；文件生成使用 iter_report_chunks 逐段写出。"""
    return b"".join(iter_report_chunks(document, facts, buffer_size=_DEFAULT_BUFFER_SIZE))
