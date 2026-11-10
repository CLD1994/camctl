"""受理创建时对动作和计划事实的业务规则。"""

from collections.abc import Mapping

from camctl.acceptance.definitions import read_action_spec
from camctl.contracts.workflow_errors import registered_error_spec, validate_error_details


def validate_admitted_action(values: Mapping) -> None:
    """校验一个新动作的已物化事实；不修改输入。"""
    read_action_spec(values)
    status = values.get("status")
    if status == 4:
        if values.get("execution_started") != 0 or values.get("error_code") is None:
            raise ValueError("受理失败动作必须 execution_started=0 且携带 admission 错误")
        if values.get("execution_spec_json") is not None:
            raise ValueError("受理失败动作不携带执行定义")
        try:
            code, spec = registered_error_spec("action_error_id", values["error_code"])
            if spec["stage"] != "admission":
                raise ValueError("首次受理失败必须使用受理阶段错误")
            validate_error_details(code, values["error_details_json"])
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError(str(error)) from error
    elif status == 1:
        if values.get("execution_spec_json") is None:
            raise ValueError("可受理动作必须携带执行定义")


def validate_initial_plan_status(status: object, action_statuses: tuple) -> None:
    """按创建时已物化的所属动作状态校验新计划状态。"""
    if not isinstance(action_statuses, tuple):
        raise TypeError("动作状态必须是已物化的元组")
    expected = 3 if action_statuses and set(action_statuses) == {4} else 1
    if status != expected:
        raise ValueError(f"计划状态与动作聚合不符: 期望 {expected}")
