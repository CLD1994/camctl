"""固定报告排版、精确数字及有容量上限的字节输出。

公开字段由共享投影计算，实体层级由同一字段登记决定。此处只
编码传入的固定事实；冻结历史的分页恢复由生成调用方负责。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
from typing import Any

from camctl.contracts.public_projection import (
    OMIT, ProjectionInput, project_public, projection_structure,
)
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import ObjectId, parse_object_id

__all__ = [
    "ReportDocument", "ReportStream", "encode_number", "encode_report", "iter_report_chunks",
]

_SCHEMA = "protocol/status-report.schema.json"
_DEFAULT_BUFFER_SIZE = 64 * 1024
EntityEntry = tuple[str, int, Mapping[str, Mapping[int, Any]]]


@dataclass(frozen=True)
class ReportDocument:
    """传入编码器的报告身份与入选根实体；不承担历史读取。

    每个根实体的第三项是其入选子树，与公共投影的逐层入选结构
    一致：{子实体名: {子实体 ID: 后代入选子树}}。
    """

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
        fragment = project_public(ProjectionInput(entity, row_id, facts, dict(selected)))
        if fragment is not OMIT:
            yield fragment


@dataclass
class _Frame:
    kind: str
    entity: str | None = None
    name: str | None = None
    wrote: bool = False
    last_field: str | None = None
    child_position: int = -1
    count: int = 0
    last_id: int = 0
    expected_id: int | None = None
    identity_seen: bool = False


def _emission(method):
    """一次供给必须耗尽；失败或中途关闭的字节只能作为未完成前缀。"""
    @wraps(method)
    def emit(self, *args, **kwargs):
        if self._failed or self._finished:
            raise ValueError('报告字节流已失败或已收尾')
        if self._busy:
            self._failed = True
            raise ValueError('上次报告字节供给尚未耗尽')
        self._busy = True
        try:
            for token in method(self, *args, **kwargs):
                if self._failed:
                    raise ValueError('报告字节供给期间文档已失败')
                yield token
                if self._failed:
                    raise ValueError('报告字节供给期间文档已失败')
        except BaseException:
            self._failed = True
            raise
        finally:
            self._busy = False
    return emit


class ReportStream:
    """一份报告的字节状态：有限 section 或逐层字段/数组/对象供给。

    每次返回的迭代器须耗尽后再供给下一步，失败即终止文档。逐层
    模式只保留当前嵌套帧，不收集成员；实体子集合按登记顺序输出，
    空集合省略，普通数组保留。field/item 的值须由调用者保证有限，
    增长关系用节点 API 逐项供给。业务 Schema 和回执由调用方校验。
    """

    def __init__(self, document: ReportDocument) -> None:
        metadata = {
            "report_id": document.report_id,
            "from_wm": document.from_wm,
            "to_wm": document.to_wm,
        }
        parse_object_id(document.report_id)
        validate_document(_SCHEMA, metadata)
        self._metadata = metadata
        self._collections = projection_structure("report").entity_fields
        self._position = -1
        self._opened = False
        self._finished = False
        self._started = False
        self._failed = False
        self._busy = False
        self._mode = None
        self._frames: list[_Frame] = []

    @_emission
    def open(self) -> Iterator[bytes]:
        """文档起始：身份与覆盖元数据，首个集合由第一个片段打开。"""
        if self._started:
            raise ValueError("报告字节流已打开或已收尾")
        self._started = True
        yield b"{"
        for index, name in enumerate(sorted(self._metadata)):
            if index:
                yield b","
            yield _string(name)
            yield b":"
            yield from _iter_value(self._metadata[name])

    @_emission
    def section(self, collection: str, fragment: Mapping) -> Iterator[bytes]:
        """供给一个根实体片段；集合只能沿登记顺序推进。"""
        self._choose_mode('section')
        entity = self._entity_of(collection)
        index = self._collection_index(collection)
        if index < self._position:
            raise ValueError(f"报告集合 {collection!r} 的供给顺序回退")
        if index > self._position:
            if self._opened:
                yield b"]"
            self._position = index
            self._opened = False
            self._section_id = 0
        # 单个根实体与真实公共结构组合校验；不建立整份报告列表。
        validate_document(_SCHEMA, {**self._metadata, collection: [fragment]})
        identity = parse_object_id(fragment[projection_structure(entity).identity_field])
        if identity <= self._section_id:
            raise ValueError('报告实体身份重复或回退')
        self._section_id = identity
        if self._opened:
            yield b","
        else:
            yield b"," + _string(collection) + b":["
            self._opened = True
        yield from _iter_object(fragment, entity)

    def _choose_mode(self, mode):
        if not self._started:
            raise ValueError('报告字节流尚未打开')
        if self._mode not in (None, mode):
            raise ValueError('整片段与逐层供给不能混用')
        if self._mode is None:
            self._mode = mode
            if mode == 'nodes':
                self._frames.append(_Frame('object', entity='report', wrote=True))

    def _node(self, kind):
        self._choose_mode('nodes')
        frame = self._frames[-1]
        if frame.kind != kind:
            raise ValueError(f'当前报告节点不是 {kind}')
        return frame

    @staticmethod
    def _children(frame):
        return () if frame.entity is None else projection_structure(frame.entity).entity_fields

    def _field_prefix(self, name):
        frame = self._node('object')
        if not isinstance(name, str):
            raise ValueError('报告字段名必须是字符串')
        if frame.entity == 'report' or any(name == child for child, _ in self._children(frame)):
            raise ValueError('实体子集合必须逐层打开数组')
        if frame.child_position >= 0 or (frame.last_field is not None and name <= frame.last_field):
            raise ValueError('报告字段重复、倒序或位于实体子集合之后')
        frame.last_field = name
        if frame.wrote:
            yield b','
        yield _string(name)
        yield b':'
        frame.wrote = True

    def _item_prefix(self, identity=None):
        frame = self._node('array')
        if frame.entity is not None:
            current = ObjectId(identity)
            if current <= frame.last_id:
                raise ValueError('报告实体身份重复或回退')
            frame.last_id = current
            if not frame.count:
                parent = self._frames[-2]
                if parent.wrote:
                    yield b','
                yield _string(frame.name)
                yield b':['
                parent.wrote = True
        elif identity is not None:
            raise ValueError('普通数组成员不接受实体身份')
        if frame.count:
            yield b','
        frame.count += 1

    @_emission
    def field(self, name: str, value: Any) -> Iterator[bytes]:
        """写当前对象的一个有限自身值；增长字段由逐层节点供给。"""
        frame = self._node('object')
        if frame.expected_id is not None and name == projection_structure(frame.entity).identity_field:
            if parse_object_id(value) != frame.expected_id:
                raise ValueError('实体身份字段与供给身份不符')
            frame.identity_seen = True
        yield from self._field_prefix(name)
        yield from _iter_value(value)

    @_emission
    def open_array(self, field: str | None = None) -> Iterator[bytes]:
        """实体子集合延迟到首项才开字段；普通数组包括空数组均写出。"""
        self._choose_mode('nodes')
        parent = self._frames[-1]
        if parent.kind == 'object':
            for index, (name, entity) in enumerate(self._children(parent)):
                if name == field:
                    if index <= parent.child_position:
                        raise ValueError('报告实体子集合重复或倒序')
                    parent.child_position = index
                    self._frames.append(_Frame('array', entity=entity, name=name))
                    return
            yield from self._field_prefix(field)
        else:
            if field is not None or parent.entity is not None:
                raise ValueError('数组成员不能指定字段或替代实体对象')
            yield from self._item_prefix()
        yield b'['
        self._frames.append(_Frame('array'))

    @_emission
    def open_object(self, field: str | None = None, *, identity: int | None = None) -> Iterator[bytes]:
        """打开自身普通对象或一个实体/普通数组项，不接收完整片段。"""
        self._choose_mode('nodes')
        parent = self._frames[-1]
        entity = None
        if parent.kind == 'object':
            if identity is not None:
                raise ValueError('普通对象字段不接受实体身份')
            yield from self._field_prefix(field)
        else:
            if field is not None:
                raise ValueError('数组成员不能指定字段')
            entity = parent.entity
            if entity is not None and projection_structure(entity).identity_field is None:
                raise ValueError('报告实体缺少登记身份字段')
            yield from self._item_prefix(identity)
        yield b'{'
        self._frames.append(_Frame('object', entity=entity, expected_id=identity))

    @_emission
    def item(self, value: Any) -> Iterator[bytes]:
        """供给普通数组的有限值；实体项须用 open_object 逐字段写。"""
        if self._node('array').entity is not None:
            raise ValueError('实体数组不接受整片段成员')
        yield from self._item_prefix()
        yield from _iter_value(value)

    @_emission
    def close_object(self) -> Iterator[bytes]:
        frame = self._node('object')
        if len(self._frames) == 1:
            raise ValueError('报告根对象须用 finish 收尾')
        if frame.expected_id is not None and not frame.identity_seen:
            raise ValueError('实体身份字段尚未写出')
        self._frames.pop()
        yield b'}'

    @_emission
    def close_array(self) -> Iterator[bytes]:
        frame = self._node('array')
        self._frames.pop()
        if frame.entity is None or frame.count:
            yield b']'

    @_emission
    def finish(self) -> Iterator[bytes]:
        """关闭当前集合并收尾；此后不能再供给片段。"""
        if not self._started:
            raise ValueError('报告字节流尚未打开')
        if self._mode == 'nodes' and len(self._frames) != 1:
            raise ValueError('报告仍有未关闭的对象或数组')
        self._finished = True
        if self._opened:
            yield b"]"
        yield b"}\n"
        self._frames.clear()

    def _collection_index(self, collection: str) -> int:
        for index, (name, _) in enumerate(self._collections):
            if name == collection:
                return index
        raise ValueError(f"未登记的报告集合: {collection!r}")

    def _entity_of(self, collection: str) -> str:
        for name, entity in self._collections:
            if name == collection:
                return entity
        raise ValueError(f"未登记的报告集合: {collection!r}")


def _iter_document(document: ReportDocument, facts: Mapping) -> Iterator[bytes]:
    stream = ReportStream(document)
    yield from stream.open()
    collections = {"plans": document.plans, "plan_file_diagnostics": document.diagnostics}
    for name, entity in stream._collections:
        for fragment in _projected_entities(collections[name], entity, facts):
            yield from stream.section(name, fragment)
    yield from stream.finish()


def chunk_bytes(tokens: Iterator[bytes], *, buffer_size: int) -> Iterator[bytes]:
    """把字节流按固定容量分块；后续投影失败时此前字节仍是未完成报告。"""
    if isinstance(buffer_size, bool) or not isinstance(buffer_size, int) or buffer_size < 1:
        raise ValueError("报告字节缓冲容量必须是正整数")
    pending = bytearray()
    for token in tokens:
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


def iter_report_chunks(document: ReportDocument, facts: Mapping, *, buffer_size: int) -> Iterator[bytes]:
    """输出不超过容量的片段；后续投影失败时此前字节仍是未完成报告。"""
    return chunk_bytes(_iter_document(document, facts), buffer_size=buffer_size)


def encode_report(document: ReportDocument, facts: Mapping) -> bytes:
    """完整编码的便利入口；文件生成使用 iter_report_chunks 逐段写出。"""
    return b"".join(iter_report_chunks(document, facts, buffer_size=_DEFAULT_BUFFER_SIZE))
