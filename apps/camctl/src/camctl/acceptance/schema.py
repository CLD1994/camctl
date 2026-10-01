"""精确数字的 Schema 校验适配（Draft 2020-12，本地引用）。

整数识别与倍数判断不经过二进制浮点：integer 按数学值判定（排
除布尔），multipleOf 用 Fraction 判断。Schema 或本地引用无效属
规则处理错误，与用户参数错误分开表达。
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from camctl.contracts.schemas import (
    SchemaRuleError, create_validator, load_schema, validation_errors,
)

__all__ = ["BodySchemaError", "RuleError", "precise_validator_for", "validate_precise"]

_PLAN_SCHEMA_RESOURCE = "protocol/plan.schema.json"


class RuleError(ValueError):
    """Schema 规则自身无效（Schema 非法或缺本地引用）。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"规则处理错误: {message}")


class BodySchemaError(ValueError):
    """输入文档不符合对应 Schema（用户参数错误）。"""


def precise_validator_for(schema: Any) -> Draft202012Validator:
    """按精确数字适配构造校验器；规则无效抛 RuleError。"""
    try:
        return create_validator(schema)
    except SchemaRuleError as error:
        raise RuleError(f"Schema 规则无效: {error}") from error


def validate_precise(schema: Any, document: Any, *, label: str = "输入") -> None:
    """校验文档；不符合时抛 BodySchemaError，规则无效抛 RuleError。"""
    validator = precise_validator_for(schema)
    try:
        errors = validation_errors(validator, document)
    except SchemaRuleError as error:
        raise RuleError(str(error)) from error
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path)
        where = f" 位于 {location}" if location else ""
        raise BodySchemaError(f"{label}不符合公共结构{where}: {first.message}")


def plan_schema() -> Any:
    """公共计划 Schema（精确校验使用）。"""
    try:
        return load_schema(_PLAN_SCHEMA_RESOURCE)
    except SchemaRuleError as error:
        raise RuleError(str(error)) from error


def validate_plan_structure(document: Any) -> None:
    validate_precise(plan_fragment("plan_structure"), document, label="计划正文")


def plan_fragment(name: str) -> dict:
    """通过本地公共资源定位受理片段，保留其引用基址。"""
    return {"$schema": Draft202012Validator.META_SCHEMA["$id"],
            "$ref": f"plan.schema.json#/$defs/{name}"}


def schema_issues(schema: Any, document: Any, prefix: str) -> tuple[dict, ...]:
    """把 Schema 的已确定错误转换为公共机器字段，不解析错误文案。"""
    import re

    try:
        errors = validation_errors(precise_validator_for(schema), document)
    except SchemaRuleError as error:
        raise RuleError(str(error)) from error
    issues: list[dict] = []
    for error in errors:
        path = prefix
        for part in error.absolute_path:
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        if error.validator == "required":
            additions = [
                {"field": f"{path}.{key}", "reason": "required"}
                for key in error.validator_value if key not in error.instance
            ]
        elif error.validator == "additionalProperties" and error.validator_value is False:
            properties = error.schema.get("properties", {})
            patterns = error.schema.get("patternProperties", {})
            additions = [
                {"field": f"{path}.{key}", "reason": "unsupported", "value": value}
                for key, value in error.instance.items()
                if key not in properties and not any(re.search(pattern, key) for pattern in patterns)
            ]
        else:
            reason = (
                "type" if error.validator == "type" else
                "unsupported" if error.validator in {"enum", "const"} else
                "combination" if error.validator in {"oneOf", "anyOf", "not", "dependentRequired"} else "range"
            )
            additions = [{"field": path, "reason": reason, "value": error.instance}]
        for issue in additions:
            if issue not in issues:
                issues.append(issue)
    return tuple(issues)
