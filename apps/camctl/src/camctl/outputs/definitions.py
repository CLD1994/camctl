"""取回模块拥有的固定选择方式。"""
from enum import IntEnum
from camctl.contracts.json_values import is_json_integer
from camctl.contracts.values import parse_object_id


class SelectionMode(IntEnum):
    DEFAULT = 1
    PREVIEW = 2
    EXPLICIT_IDS = 3


def build_obtain_spec(params: dict) -> dict:
    if "output_ids" in params:
        mode = SelectionMode.EXPLICIT_IDS
    elif params.get("filter", "default") == "preview":
        mode = SelectionMode.PREVIEW
    else:
        mode = SelectionMode.DEFAULT
    return {"selection_mode":int(mode)}


def validate_obtain_spec(spec) -> dict:
    if not isinstance(spec, dict) or set(spec) != {"selection_mode"}:
        raise ValueError("取回定义必须只保存 selection_mode")
    value = spec["selection_mode"]
    if not is_json_integer(value):
        raise ValueError("取回选择方式必须是 JSON 整数")
    return {"selection_mode":int(SelectionMode(int(value)))}


def read_selection_request(spec, input_fields) -> tuple[SelectionMode, tuple[int, ...]]:
    """执行定义提供选择方式，原输入提供精确请求的身份与顺序。"""
    mode = SelectionMode(validate_obtain_spec(spec)["selection_mode"])
    if not isinstance(input_fields, dict) or not isinstance(input_fields.get("params"), dict):
        raise ValueError("取回缺少原参数对象")
    params = input_fields["params"]
    if "filter" in params and ("output_ids" in params or params["filter"] not in ("default", "preview")):
        raise ValueError("原筛选方式无效或与精确列表同时存在")
    if int(mode) != build_obtain_spec(params)["selection_mode"]:
        raise ValueError("取回固定选择方式与原参数矛盾")
    if mode is not SelectionMode.EXPLICIT_IDS:
        return mode, ()
    raw_ids = params.get("output_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("精确取回缺少非空原产物列表")
    identities = tuple(parse_object_id(raw) for raw in raw_ids)
    if len(set(identities)) != len(identities):
        raise ValueError("精确取回原产物列表不得重复")
    return mode, identities
