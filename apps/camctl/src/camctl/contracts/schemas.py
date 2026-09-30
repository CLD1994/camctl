"""公共 Schema 的加载与校验。

Schema 全部来自包内权威资源，相互引用按兄弟文件名解析；校验显
式采用 Draft 2020-12，不在受理时访问网络。输入文档先经精确解析
（int/Decimal），Schema 数值判断由 jsonschema 按精确类型执行。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import referencing
import referencing.jsonschema
from jsonschema import Draft202012Validator

from camctl.bootstrap.resources import resource_bytes

__all__ = ["SchemaValidationError", "validate_document"]

#: 打包的公共 Schema；键为资源名，注册表键为兄弟引用使用的文件名。
_SCHEMA_RESOURCES = (
    "protocol/plan.schema.json",
    "protocol/status-report.schema.json",
    "protocol/capabilities.schema.json",
)


class SchemaValidationError(ValueError):
    """文档不符合对应公共 Schema。"""


@lru_cache(maxsize=1)
def _registry() -> referencing.Registry:
    resources: dict[str, referencing.jsonschema.Resource] = {}
    for name in _SCHEMA_RESOURCES:
        document = json.loads(resource_bytes(name))
        resource = referencing.jsonschema.Resource.from_contents(document)
        # Schema 间以兄弟文件名引用（如 "status-report.schema.json#/..."）。
        basename = name.rsplit("/", 1)[1]
        resources[basename] = resource
    return referencing.Registry().with_resources(resources.items())


@lru_cache(maxsize=len(_SCHEMA_RESOURCES))
def _validator(schema_name: str) -> Draft202012Validator:
    if schema_name not in _SCHEMA_RESOURCES:
        raise SchemaValidationError(f"未打包的公共 Schema: {schema_name}")
    document = json.loads(resource_bytes(schema_name))
    return Draft202012Validator(document, registry=_registry())


def validate_document(schema_name: str, document: Any) -> None:
    """按指定公共 Schema 校验文档；不符合时抛出含定位的错误。"""
    validator = _validator(schema_name)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path)
        where = f" 位于 {location}" if location else ""
        raise SchemaValidationError(
            f"文档不符合 {schema_name}{where}: {first.message}"
        )
