"""组织所属动作模块的固定定义；读取只使用首次保存的事实。"""
from copy import deepcopy
from collections.abc import Mapping

from camctl.acceptance.schema import RuleError
from camctl.capture.models import build_capture_spec, validate_capture_spec
from camctl.contracts.enums import decode_member
from camctl.contracts.input_fields import reconstruct_action_input
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import action_error_spec
from camctl.outputs.definitions import build_obtain_spec, validate_obtain_spec


def build_action_spec(validation) -> dict:
    """仅在动作全部校验通过后调用；驱动不连接设备。"""
    literal = validation.action_type
    try:
        if literal.startswith("camera_"):
            task = None
            if literal != "camera_take_photo":
                factory = validation.parameter_definition.task_factory
                if not callable(factory):
                    raise ValueError("已支持的拍摄参数类型未提供任务定义")
                task = factory(deepcopy(validation.effective_params))
            return build_capture_spec(literal, task)
        if literal == "obtain_action_outputs":
            return validate_obtain_spec(build_obtain_spec(validation.raw["params"]))
        if literal in {"delete_action_outputs", "cancel_task", "report_status"}:
            return {}
        raise ValueError("没有对应动作的执行定义")
    except (TypeError, ValueError, AttributeError) as error:
        raise RuleError(f"动作 {validation.name} 的固定执行定义无效: {error}") from error


def read_action_spec(row: Mapping) -> dict | None:
    """校验当前或历史动作行；SQL NULL 只适用于首次受理失败。"""
    try:
        literal = decode_member("actions.type", row["type"]).name.lower()
        original = reconstruct_action_input(row)
        spec = row["execution_spec_json"]
        if spec is None:
            error = action_error_spec(row["error_code"])
            if row["status"] != 4 or error["stage"] != "admission" or row["execution_started"] != 0:
                raise ValueError("SQL NULL 执行定义与首次受理事实矛盾")
            if row["effective_params_json"] is not None or row["driver_id"] is not None:
                raise ValueError("受理失败仍保存生效参数或驱动绑定")
            return None
        if row["error_code"] is not None and action_error_spec(row["error_code"])["stage"] == "admission":
            raise ValueError("受理失败动作存在有效执行定义")
        if isinstance(spec, str):
            spec = parse_exact_json(spec)
        if literal.startswith("camera_"):
            effective = row["effective_params_json"]
            if isinstance(effective, str):
                effective = parse_exact_json(effective)
            if (not isinstance(effective, dict) or not isinstance(effective.get("type"), str)
                    or not effective["type"] or not isinstance(row["driver_id"], str) or not row["driver_id"]):
                raise ValueError("已受理拍摄缺少生效参数或驱动绑定")
            if original["params"].get("type") != effective["type"]:
                raise ValueError("原始参数类型与生效参数类型矛盾")
            return validate_capture_spec(literal, spec)
        if row["effective_params_json"] is not None or row["driver_id"] is not None:
            raise ValueError("非拍摄动作携带生效驱动参数或绑定")
        if literal == "obtain_action_outputs":
            validated = validate_obtain_spec(spec)
            if validated != build_obtain_spec(original["params"]):
                raise ValueError("取回选择方式与首次原输入矛盾")
            return validated
        if literal in {"delete_action_outputs", "cancel_task", "report_status"} and spec == {}:
            return {}
        raise ValueError("执行定义与所属动作类型不符")
    except (KeyError, ValueError, TypeError) as error:
        raise ConsistencyError(f"持久化动作执行定义不可解释: {error}") from error
