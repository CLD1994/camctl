"""静态参数规则的完整性：版本、类型关联及自包含引用。"""
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012
from copy import deepcopy

from camctl.acceptance.schema import RuleError
from camctl.contracts.schemas import create_validator, SchemaRuleError


def validate_parameter_schema(parameter_type: str, schema: dict) -> None:
    """复用 JSON Schema 库的资源遍历与引用解析，检查全部分支。"""
    try:
        if (not isinstance(schema, dict) or schema.get("type") != "object"
                or "type" not in schema.get("required", ())
                or schema.get("properties", {}).get("type", {}).get("const") != parameter_type):
            raise RuleError("参数 Schema 必须明确约束选定 type 的完整对象")
        root = DRAFT202012.create_resource(schema)
        base = root.id() or "urn:camctl:parameter-schema"
        registry = Registry().with_resource(base, root).crawl()
        create_validator(schema, registry=registry)
        pending = [(root, registry.resolver(base))]
        while pending:
            resource, resolver = pending.pop()
            contents = resource.contents
            if isinstance(contents, dict):
                for keyword in ("$ref", "$dynamicRef"):
                    if keyword in contents:
                        resolver.lookup(contents[keyword])
            pending.extend((child, resolver.in_subresource(child)) for child in resource.subresources())
    except (SchemaRuleError, Unresolvable, TypeError, AttributeError) as error:
        raise RuleError(f"参数 Schema 无法完整解释: {error}") from error


def schema_with_defaults(schema: dict, defaults) -> dict:
    """只重建 Schema 说明，不修改 const 等实例数据或默认应用规则。"""
    copied = deepcopy(schema)
    pending = [DRAFT202012.create_resource(copied)]
    while pending:
        resource = pending.pop()
        if isinstance(resource.contents, dict):
            resource.contents.pop("default", None)
        pending.extend(resource.subresources())
    copied["default"] = deepcopy(dict(defaults))
    for field, field_schema in copied.get("properties", {}).items():
        if field in defaults and isinstance(field_schema, dict):
            field_schema["default"] = deepcopy(defaults[field])
    return copied
