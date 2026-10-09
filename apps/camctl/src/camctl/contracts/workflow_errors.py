"""公共业务错误登记的装载入口。

动作最终错误与逐明细错误的整数编号以 protocol 错误登记为唯一
权威来源；程序各处按名称取得编号，不另行维护编号清单。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping

from camctl.resources import ResourceError, resource_bytes
from camctl.contracts.json_values import JsonParseError, parse_exact_json
from camctl.contracts.schemas import (
    SchemaRuleError, create_validator, load_schema, schema_registry, validation_errors,
)
from referencing.jsonschema import DRAFT202012


@lru_cache(maxsize=1)
def _registry() -> dict:
    name = "protocol/workflow-codes.json"
    try:
        return parse_exact_json(resource_bytes(name).decode("utf-8"))
    except (ResourceError, OSError, UnicodeDecodeError, JsonParseError) as error:
        raise SchemaRuleError(f"公共错误登记资源不可解释: {name}: {error}") from error


@lru_cache(maxsize=1)
def action_error_ids() -> Mapping[str, int]:
    """公共登记中动作最终错误的名称到整数编号映射。"""
    return {
        name: int(spec["action_error_id"])
        for name, spec in _registry()["codes"].items()
        if "action_error_id" in spec
    }


@lru_cache(maxsize=1)
def item_error_ids(table: str) -> Mapping[str, int]:
    """一张明细表的公共错误名称到整数编号映射。"""
    return {
        name: int(spec["item_error_ids"][table])
        for name, spec in _registry()["codes"].items()
        if table in spec.get("item_error_ids", {})
    }


def action_error_id(name: str) -> int:
    """按公共名称取得动作错误编号；未知名称是装配错误。"""
    try:
        return action_error_ids()[name]
    except KeyError as error:
        raise ValueError(f"公共错误登记没有该动作错误: {name!r}") from error


def registered_error(name: str) -> dict:
    """按公共名称读取错误契约（阶段与详情结构）；未登记名称不可构造。"""
    try:
        return _registry()["codes"][name]
    except KeyError as error:
        raise ValueError(f"公共错误登记没有该错误: {name!r}") from error


def action_error_spec(error_id: int) -> dict:
    """读取已登记动作错误的完整契约，未登记编号不可解释。"""
    for spec in _registry()["codes"].values():
        if spec.get("action_error_id") == error_id and not isinstance(error_id, bool):
            return spec
    raise ValueError(f"公共错误登记没有该动作编号: {error_id!r}")


def registered_error_spec(registry_key: str, error_id: int) -> tuple[str, dict]:
    """按实际登记路径读取动作或明细错误，不复制错误清单。"""
    if isinstance(error_id, bool) or not isinstance(error_id, int):
        raise ValueError("已保存错误编号必须是整数")
    for name, spec in _registry()["codes"].items():
        value = spec
        for key in registry_key.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value == error_id:
            return name, spec
    raise ValueError(f"错误编号 {error_id} 未登记为 {registry_key}")


@lru_cache(maxsize=None)
def _details_validator(code: str):
    try:
        details_schema = _registry()["codes"][code]["details_schema"]
    except KeyError as error:
        raise ValueError(f"未登记的公共错误码: {code}") from error
    local = schema_registry().with_resource("workflow-codes.json", DRAFT202012.create_resource(_registry()))
    schema = {"$schema":"https://json-schema.org/draft/2020-12/schema", **details_schema}
    validated = create_validator(schema)
    return type(validated)(schema, registry=local)


def validate_error_details(code: str, details) -> None:
    """生产与消费都验证已登记结构；文案不参与分类。"""
    errors = validation_errors(_details_validator(code), details)
    if errors:
        raise ValueError(f"公共错误 {code} 的详情不符合登记结构: {errors[0].message}")


def validate_public_error(value: Mapping[str, Any]) -> None:
    """验证公共错误结构和已登记含义；完整未知驱动错误保持开放。"""
    if not isinstance(value, Mapping):
        raise ValueError("公共错误必须是对象")
    # Mapping 是保存端口的输入类型；校验副本不改变调用方持有的事实。
    document = dict(value)
    if isinstance(document.get("details"), Mapping):
        document["details"] = dict(document["details"])
    validator = create_validator({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "status-report.schema.json#/$defs/error",
    })
    errors = validation_errors(validator, document)
    if errors:
        raise ValueError(f"公共错误结构不符合契约: {errors[0].message}")
    code = document["code"]
    spec = _registry()["codes"].get(code)
    if spec is not None:
        if document["stage"] != spec["stage"]:
            raise ValueError(f"公共错误 {code} 的阶段不符合登记: {document['stage']!r}")
        validate_error_details(code, document["details"])


@lru_cache(maxsize=1)
def _public_json_registry():
    """将登记含义加入本地错误 Schema，沿原引用验证嵌套错误。"""
    report = load_schema("protocol/status-report.schema.json")
    rules = report["$defs"]["error"].setdefault("allOf", [])
    for code, spec in _registry()["codes"].items():
        pointer = code.replace("~", "~0").replace("/", "~1")
        rules.append({
            "if": {"properties": {"code": {"const": code}}, "required": ["code"]},
            "then": {"properties": {
                "stage": {"const": spec["stage"]},
                "details": {"$ref": f"workflow-codes.json#/codes/{pointer}/details_schema"},
            }},
        })
    registry = schema_registry().with_resources([
        ("status-report.schema.json", DRAFT202012.create_resource(report)),
        ("workflow-codes.json", DRAFT202012.create_resource(_registry())),
    ])
    # 派生规则也遵守公共 Schema 方言；实例错误不参与此检查。
    create_validator(report, registry=registry)
    for spec in _registry()["codes"].values():
        create_validator({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **spec["details_schema"],
        }, registry=registry)
    return registry


@lru_cache(maxsize=32)
def _public_json_validator(schema_pointer: str):
    return create_validator({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "status-report.schema.json" + schema_pointer,
    }, registry=_public_json_registry())


def validate_public_json(schema_pointer: str, document: Any) -> None:
    """验证已声明的公共 JSON 结构及其中明确引用的登记错误。"""
    errors = validation_errors(_public_json_validator(schema_pointer), document)
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path)
        where = f" 位于 {location}" if location else ""
        raise ValueError(f"公共字段不符合 {schema_pointer}{where}: {first.message}")


def item_error_id(table: str, name: str) -> int:
    """按公共名称取得明细错误编号；未知名称是装配错误。"""
    try:
        return item_error_ids(table)[name]
    except KeyError as error:
        raise ValueError(
            f"公共错误登记没有 {table} 的该项错误: {name!r}"
        ) from error
