"""公共 Schema 的加载与校验。

Schema 全部来自包内权威资源，相互引用按兄弟文件名解析；校验显
式采用 Draft 2020-12，不在受理时访问网络。输入文档先经精确解析
（int/Decimal），Schema 数值判断由 jsonschema 按精确类型执行。
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any, Iterator

import referencing
import referencing.jsonschema
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import extend
from referencing.exceptions import CannotDetermineSpecification, Unresolvable

from camctl.resources import ResourceError, resource_bytes
from camctl.contracts.json_values import JsonParseError, is_json_integer, is_multiple, parse_exact_json

__all__ = [
    "SchemaRuleError", "SchemaValidationError", "create_validator", "load_schema",
    "schema_registry", "validation_errors", "validate_document",
]

#: 打包的公共 Schema；键为资源名，注册表键为兄弟引用使用的文件名。
_SCHEMA_RESOURCES = (
    "protocol/plan.schema.json",
    "protocol/status-report.schema.json",
    "protocol/capabilities.schema.json",
)


class SchemaValidationError(ValueError):
    """文档不符合对应公共 Schema。"""


class SchemaRuleError(ValueError):
    """Schema、包资源或必需本地引用不可解释。"""


def _is_number(_checker, value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return False
    return not isinstance(value, Decimal) or value.is_finite()


def _multiple_of(validator, divisor: Any, value: Any, schema: Any) -> Iterator[ValidationError]:
    if not validator.is_type(value, "number"):
        return
    if not is_multiple(value, divisor):
        yield ValidationError(f"{value!r} 不是 {divisor!r} 的整数倍")


@lru_cache(maxsize=1)
def _precise_draft() -> type[Draft202012Validator]:
    checker = Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _checker, value: is_json_integer(value)
    ).redefine("number", _is_number)
    # 登记扩展类型，使元 Schema 和含显式 $schema 的引用也使用精确规则。
    return extend(
        Draft202012Validator, {"multipleOf": _multiple_of},
        type_checker=checker, version="camctl-precise-2020-12",
    )


def _check_schema(schema: Any) -> None:
    """检查定义及资源版本；普通子规则继承版本，实例数据不参与。"""
    try:
        _precise_draft().check_schema(schema)
    except SchemaError as error:
        raise SchemaRuleError(f"Schema 规则无效: {error}") from error

    specification = referencing.jsonschema.DRAFT202012
    required_version = Draft202012Validator.META_SCHEMA["$id"]
    pending = [(specification.create_resource(schema), True)]
    while pending:
        resource, is_root = pending.pop()
        contents = resource.contents
        independent = is_root or resource.id() is not None
        declares_version = isinstance(contents, dict) and "$schema" in contents
        if independent or declares_version:
            version = contents.get("$schema") if isinstance(contents, dict) else None
            if version != required_version:
                raise SchemaRuleError(
                    f"Schema 资源必须声明 {required_version}: {version!r}"
                )
        pending.extend((child, False) for child in resource.subresources())


def load_schema(name: str) -> Any:
    """精确读取登记的 Schema；不可用资源及非法 JSON 属于规则错误。"""
    if name not in _SCHEMA_RESOURCES:
        raise SchemaRuleError(f"未打包的公共 Schema: {name}")
    try:
        document = parse_exact_json(resource_bytes(name).decode("utf-8"))
        _check_schema(document)
    except (ResourceError, OSError, UnicodeDecodeError, JsonParseError, SchemaError) as error:
        raise SchemaRuleError(f"Schema 规则不可用: {name}: {error}") from error
    return document


@lru_cache(maxsize=1)
def _registry() -> referencing.Registry:
    resources: dict[str, referencing.jsonschema.Resource] = {}
    for name in _SCHEMA_RESOURCES:
        document = load_schema(name)
        try:
            resource = referencing.jsonschema.Resource.from_contents(
                document, default_specification=referencing.jsonschema.DRAFT202012
            )
        except CannotDetermineSpecification as error:
            raise SchemaRuleError(f"Schema 方言不可解释: {name}") from error
        # Schema 间以兄弟文件名引用（如 "status-report.schema.json#/..."）。
        basename = name.rsplit("/", 1)[1]
        resources[basename] = resource
    return referencing.Registry().with_resources(resources.items())


def schema_registry() -> referencing.Registry:
    """提供只包含登记资源的本地引用注册表，不执行网络检索。"""
    return _registry()


def create_validator(schema: Any, *, registry: referencing.Registry | None = None) -> Draft202012Validator:
    """使用统一精确数字规则构造校验器；规则错误与文档错误分开。"""
    _check_schema(schema)
    return _precise_draft()(schema, registry=schema_registry() if registry is None else registry)


def validation_errors(validator: Draft202012Validator, document: Any) -> list[ValidationError]:
    """取得文档校验错误；必需引用无法解析时报告规则错误。"""
    try:
        return sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    except Unresolvable as error:
        raise SchemaRuleError(f"Schema 本地引用无法解析: {error}") from error


@lru_cache(maxsize=len(_SCHEMA_RESOURCES))
def _validator(schema_name: str) -> Draft202012Validator:
    return create_validator(load_schema(schema_name))


def validate_document(schema_name: str, document: Any) -> None:
    """按指定公共 Schema 校验文档；不符合时抛出含定位的错误。"""
    validator = _validator(schema_name)
    errors = validation_errors(validator, document)
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path)
        where = f" 位于 {location}" if location else ""
        raise SchemaValidationError(
            f"文档不符合 {schema_name}{where}: {first.message}"
        )
