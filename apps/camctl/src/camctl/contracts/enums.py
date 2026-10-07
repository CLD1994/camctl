"""从权威整数登记生成受约束枚举。

枚举成员只从包内 ``registry/enum-registry.json`` 生成，
不在生产代码或测试中复制完整编号清单；公共文本编码与
状态报告 Schema 的文本枚举互验，保证两个权威来源一致。
"""

from __future__ import annotations

import json
from enum import IntEnum
from functools import lru_cache
from typing import Any

from camctl.resources import resource_bytes

_REGISTRY_RESOURCE = "registry/enum-registry.json"
_SCHEMA_RESOURCE = "protocol/status-report.schema.json"

#: 具有公共文本表示的列及其在状态报告 Schema 中的定义名；
#: 公共文本 = 成员名称的小写形式，由 assert_public_contracts 与 Schema 互验。
PUBLIC_TEXT_CONTRACTS: dict[str, str] = {
    "actions.type": "action_type",
    "actions.status": "action_status",
    "plans.status": "plan_status",
}


class EnumRegistryError(ValueError):
    """整数登记缺失、结构非法或与公共 Schema 不一致。"""


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    """读取并缓存整数登记。"""
    try:
        registry = json.loads(resource_bytes(_REGISTRY_RESOURCE))
    except (OSError, ValueError) as error:
        raise EnumRegistryError(f"整数登记不可用: {_REGISTRY_RESOURCE}") from error
    for section in ("enums", "json_enums"):
        definitions = registry.get(section)
        if not isinstance(definitions, dict):
            raise EnumRegistryError(f"整数登记缺少 {section} 分区")
        for column, definition in definitions.items():
            members = definition.get("members")
            if not isinstance(members, dict) or not members:
                raise EnumRegistryError(f"整数登记 {column} 缺少成员")
            for name, code in members.items():
                if not isinstance(code, int) or isinstance(code, bool) or code <= 0:
                    raise EnumRegistryError(f"整数登记 {column} 成员 {name} 编号非法: {code!r}")
    return registry


@lru_cache(maxsize=None)
def enum_for(column: str) -> type[IntEnum]:
    """取得指定列的受约束枚举；列未登记时明确报错。"""
    registry = load_registry()
    for section in ("enums", "json_enums"):
        definition = registry[section].get(column)
        if definition is not None:
            return IntEnum(column, definition["members"])
    raise EnumRegistryError(f"列未在整数登记中定义: {column}")


def decode_member(column: str, value: int) -> IntEnum:
    """把数据库整数恢复为所属枚举成员；未知编号及其他枚举成员都拒绝。"""
    if isinstance(value, IntEnum):
        if not isinstance(value, enum_for(column)):
            raise EnumRegistryError(f"值属于其他枚举: {value!r} 不是 {column} 的成员")
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnumRegistryError(f"枚举编号必须是整数: {value!r}")
    try:
        return enum_for(column)(value)
    except ValueError as error:
        raise EnumRegistryError(f"{column} 未定义编号: {value}") from error


def encode_member(column: str, member: IntEnum) -> int:
    """把枚举成员写为数据库整数；成员不属于该列的枚举时拒绝。"""
    expected = enum_for(column)
    if not isinstance(member, expected):
        raise EnumRegistryError(f"成员不属于 {column}: {member!r}")
    return int(member)


def public_text(member: IntEnum) -> str:
    """生成公共协议文本字面量（成员名称小写）。"""
    if not isinstance(member, IntEnum):
        raise EnumRegistryError(f"预期枚举成员: {member!r}")
    return member.name.lower()


def _schema_enum_texts(schema: dict[str, Any], name: str) -> list[str]:
    node = schema["$defs"][name]
    if "enum" in node:
        return list(node["enum"])
    texts: list[str] = []
    for branch in node.get("anyOf", ()):
        if "$ref" in branch:
            target = branch["$ref"].rsplit("/", 1)[-1]
            texts.extend(_schema_enum_texts(schema, target))
        elif "enum" in branch:
            texts.extend(branch["enum"])
    return texts


def assert_public_contracts() -> None:
    """核对公共文本契约：登记成员的小写名称集合与 Schema 文本枚举完全相同。"""
    try:
        schema = json.loads(resource_bytes(_SCHEMA_RESOURCE))
    except (OSError, ValueError) as error:
        raise EnumRegistryError(f"状态报告 Schema 不可用: {_SCHEMA_RESOURCE}") from error
    for column, definition_name in PUBLIC_TEXT_CONTRACTS.items():
        members = enum_for(column)
        generated = sorted(public_text(member) for member in members)
        declared = sorted(_schema_enum_texts(schema, definition_name))
        if generated != declared:
            raise EnumRegistryError(
                f"{column} 的公共文本与 Schema {definition_name} 不一致: "
                f"登记生成 {generated}，Schema 声明 {declared}"
            )
