"""取回模块拥有的固定选择方式。"""
from enum import IntEnum
from camctl.contracts.json_values import is_json_integer


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
