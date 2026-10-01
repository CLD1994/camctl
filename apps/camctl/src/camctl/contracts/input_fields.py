"""从普通列和互斥原始例外重建动作输入。"""
from __future__ import annotations

from typing import Any, Mapping
from functools import lru_cache

from camctl.contracts.enums import decode_member
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import create_validator, validation_errors
from camctl.contracts.values import ConsistencyError, format_utc_micros


@lru_cache(maxsize=1)
def _action_validator():
    return create_validator({"$schema":"https://json-schema.org/draft/2020-12/schema",
        "$ref":"plan.schema.json#/$defs/action"})


def reconstruct_action_input(row: Mapping[str, Any]) -> dict:
    """还原结构化原输入；双重来源或不可解释的事实属于状态错误。"""
    fields = row["input_fields_json"]
    if isinstance(fields, str):
        fields = parse_exact_json(fields)
    if not isinstance(fields, dict):
        raise ConsistencyError("动作原始例外必须是 JSON 对象")
    if "name" in fields or "type" in fields:
        raise ConsistencyError("动作名称和类型只能从普通列读取")
    try:
        literal = decode_member("actions.type", row["type"]).name.lower()
    except (TypeError, ValueError) as error:
        raise ConsistencyError("动作类型不可解释") from error
    result = {"name": row["name"], "type": literal, **fields}
    for column, key in (("device_id", "device_id"), ("scheduled_at", "scheduled_at"), ("group_name", "group")):
        value = row[column]
        if value is None:
            continue
        if key in fields:
            raise ConsistencyError(f"动作字段 {key} 同时存在有效列和原始例外")
        result[key] = format_utc_micros(value) if key == "scheduled_at" else value
    # 执行资格由首次保存的定义决定，后来进入终态也不能丢失原输入。
    if row.get("execution_spec_json") is not None:
        if any(key in fields for key in ("device_id", "scheduled_at", "group")):
            raise ConsistencyError("已受理动作仍携带普通字段的原始例外")
        if validation_errors(_action_validator(), result):
            raise ConsistencyError("已受理动作的原输入不符合第一版公共动作结构")
        if literal.startswith("camera_"):
            if row["max_delay_ms"] is None or fields["policy"]["max_delay_ms"] != row["max_delay_ms"]:
                raise ConsistencyError("已受理拍摄缺少原始策略或与生效延误上限矛盾")
    return result
