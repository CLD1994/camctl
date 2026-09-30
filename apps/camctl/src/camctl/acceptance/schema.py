"""精确数字的 Schema 校验适配（Draft 2020-12，本地引用）。

整数识别与倍数判断不经过二进制浮点：integer 按数学值判定（排
除布尔），multipleOf 用 Fraction 判断。Schema 或本地引用无效属
规则处理错误，与用户参数错误分开表达。
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.validators import extend

from camctl.contracts.json_values import is_json_integer, is_multiple

__all__ = ["BodySchemaError", "RuleError", "precise_validator_for", "validate_precise"]

_PLAN_SCHEMA_RESOURCE = "protocol/plan.schema.json"


class RuleError(ValueError):
    """Schema 规则自身无效（Schema 非法或缺本地引用）。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"规则处理错误: {message}")


class BodySchemaError(ValueError):
    """输入文档不符合对应 Schema（用户参数错误）。"""


def _is_integer(_checker, instance: Any) -> bool:
    if isinstance(instance, bool):
        return False
    return isinstance(instance, (int, Decimal)) and is_json_integer(instance)


def _is_number(_checker, instance: Any) -> bool:
    if isinstance(instance, bool) or not isinstance(instance, (int, Decimal)):
        return False
    if isinstance(instance, Decimal):
        return instance.is_finite()
    return True


def _multiple_of(validator, db: Any, instance: Any, schema: Any) -> list:
    from jsonschema.exceptions import ValidationError

    if not _is_number(None, instance):
        return []
    try:
        divisor = Decimal(str(db)) if not isinstance(db, Decimal) else db
    except Exception:  # pragma: no cover - 规则本身异常
        return [ValidationError(f"multipleOf 规则值不合法: {db!r}")]
    if not is_multiple(instance, divisor):
        return [ValidationError(f"{instance!r} 不是 {db!r} 的整数倍")]
    return []


@lru_cache(maxsize=8)
def _precise_draft() -> type:
    from jsonschema import validators

    type_checker = Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, instance: _is_integer(checker, instance)
    ).redefine("number", lambda checker, instance: _is_number(checker, instance))
    return extend(Draft202012Validator, {"multipleOf": _multiple_of}, type_checker=type_checker)


def precise_validator_for(schema: Any) -> Draft202012Validator:
    """按精确数字适配构造校验器；规则无效抛 RuleError。"""
    from camctl.contracts.schemas import SchemaValidationError

    try:
        registry = _plan_registry()
        validator_class = _precise_draft()
        validator_class.check_schema(schema)
        return validator_class(schema, registry=registry)
    except Exception as error:
        raise RuleError(f"Schema 规则无效: {error}") from error


@lru_cache(maxsize=1)
def _plan_registry():
    from camctl.contracts.schemas import _registry

    return _registry()


def validate_precise(schema: Any, document: Any, *, label: str = "输入") -> None:
    """校验文档；不符合时抛 BodySchemaError，规则无效抛 RuleError。"""
    validator = precise_validator_for(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path)
        where = f" 位于 {location}" if location else ""
        raise BodySchemaError(f"{label}不符合公共结构{where}: {first.message}")


def plan_schema() -> Any:
    """公共计划 Schema（精确校验使用）。"""
    import json

    from camctl.bootstrap.resources import resource_bytes

    return json.loads(resource_bytes(_PLAN_SCHEMA_RESOURCE))


def validate_plan_structure(document: Any) -> None:
    validate_precise(plan_schema(), document, label="计划正文")
