"""公共结构、动作范围及错误优先级的分层校验。

公共结构错误与不支持的动作类型、名称重复按整份拒绝；动作自身
的语义错误（设备不存在、时间非法、参数规则不符）按本动作失败
保存。结果按动作名称组织，不依赖数组顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from camctl.acceptance.schema import (
    BodySchemaError,
    RuleError,
    validate_plan_structure,
    validate_precise,
    plan_fragment, schema_issues,
)
from camctl.acceptance.ports import ParameterDefinition, StaticActionCatalog
from camctl.contracts.json_values import JsonValue
from camctl.contracts.values import ObjectId, ValueFormatError, ValueTypeError, to_utc_micros
from camctl.devices.parameter_schemas import validate_parameter_schema

__all__ = ["ActionValidation", "BodyDecision", "validate_capture_params", "validate_new_body"]

_CAMERA_TYPES = frozenset({"camera_take_photo", "camera_record", "camera_timelapse"})


@dataclass(frozen=True)
class ActionValidation:
    """一个动作的受理校验结果。

    ok 为假时 failure 说明本动作失败原因；原输入与生效参数分别
    保留（默认值补齐后的 effective_params 仅供执行定义使用）。
    """

    name: str
    raw: JsonValue
    ok: bool
    failure: str | None = None
    effective_params: JsonValue | None = None
    device_id: str | None = None
    action_type: str | None = None
    scheduled_at_micros: int | None = None
    group_name: str | None = None
    max_delay_ms: int | None = None
    input_fields: JsonValue | None = None
    failure_code: str | None = None
    failure_details: JsonValue | None = None
    driver_id: str | None = None
    parameter_type: str | None = None
    parameter_definition: ParameterDefinition | None = None


@dataclass(frozen=True)
class BodyDecision:
    """一次新正文的受理判定。

    is_whole_rejection 为真时不进入逐动作受理；否则 actions 携带
    全部动作（含本动作失败）的完整结果。
    """

    is_whole_rejection: bool
    rejection_reasons: tuple[str, ...] = ()
    actions: tuple[ActionValidation, ...] = ()
    raw: JsonValue | None = None


def validate_new_body(raw: JsonValue, catalog: StaticActionCatalog) -> BodyDecision:
    """分层校验一份新计划正文。

    先检查公共结构与完整动作名称，再对支持动作校验自身字段；
    整份拒绝优先于单动作失败。
    """
    try:
        validate_plan_structure(raw)
    except BodySchemaError as error:
        return BodyDecision(is_whole_rejection=True, rejection_reasons=(str(error),), raw=raw)
    except RuleError:
        raise

    # 公共时间字段的实际有效性（模式之外）按语义整份校验。
    try:
        to_utc_micros(raw["created_at"])
    except ValueFormatError as error:
        return BodyDecision(
            is_whole_rejection=True,
            rejection_reasons=(f"计划创建时间非法: {error}",),
            raw=raw,
        )

    actions = raw["actions"]
    rejection_reasons: list[str] = []
    if raw["name"] != raw["name"].strip():
        rejection_reasons.append("计划名称含首尾空白")
    names: set[str] = set()
    for action in actions:
        name = action["name"]
        if name != name.strip():
            rejection_reasons.append(f"动作名称含首尾空白: {name!r}")
        if name in names:
            rejection_reasons.append(f"动作名称重复: {name}")
        names.add(name)
        action_type = action["type"]
        if action_type not in catalog.action_types():
            rejection_reasons.append(f"不支持的动作类型: {action_type}")

    if rejection_reasons:
        return BodyDecision(
            is_whole_rejection=True,
            rejection_reasons=tuple(rejection_reasons),
            raw=raw,
        )

    validated: list[ActionValidation] = []
    for index, action in enumerate(actions):
        validated.append(_validate_action(action, catalog, index))
    return BodyDecision(
        is_whole_rejection=False,
        actions=tuple(validated),
        raw=raw,
    )


def _validate_action(action: JsonValue, catalog: StaticActionCatalog, index: int) -> ActionValidation:
    prefix = f"actions[{index}]"
    name, action_type = action["name"], action["type"]
    issues = list(schema_issues(plan_fragment("action"), action, prefix))
    input_fields = {key: value for key, value in action.items() if key not in {"name", "type"}}
    device_id = scheduled = group = max_delay = None
    effective = None
    driver_id = None
    definition = None
    parameter_type = None

    params = action.get("params")
    if isinstance(params, dict):
        for owner in ("source", "target"):
            reference = params.get(owner)
            if isinstance(reference, dict):
                for field in ("action_name", "group"):
                    candidate = reference.get(field)
                    if isinstance(candidate, str) and candidate != candidate.strip():
                        issues.append({"field":f"{prefix}.params.{owner}.{field}", "reason":"range", "value":candidate})

    if "device_id" in action and action_type in _CAMERA_TYPES:
        raw_device = action["device_id"]
        if not schema_issues(plan_fragment("action/properties/device_id"), raw_device, prefix + ".device_id"):
            if catalog.device_exists(raw_device):
                device_id = raw_device
                del input_fields["device_id"]
            else:
                issues.append({"field": prefix + ".device_id", "reason": "reference", "value": raw_device})
    if "scheduled_at" in action:
        try:
            scheduled = to_utc_micros(action["scheduled_at"])
        except (ValueTypeError, ValueFormatError):
            issue = {"field": prefix + ".scheduled_at", "reason": "type" if not isinstance(action["scheduled_at"], str) else "range", "value": action["scheduled_at"]}
            if issue not in issues:
                issues.append(issue)
        else:
            del input_fields["scheduled_at"]
    if "group" in action and action_type != "obtain_action_outputs":
        candidate = action["group"]
        if not schema_issues(plan_fragment("action/properties/group"), candidate, prefix + ".group"):
            if candidate == candidate.strip():
                group = candidate
                del input_fields["group"]
            else:
                issues.append({"field": prefix + ".group", "reason": "range", "value": candidate})
    if action_type == "motor_control" and "policy" in action:
        if not schema_issues(plan_fragment("time_window_policy"), action["policy"], prefix + ".policy"):
            max_delay = int(action["policy"]["max_delay_ms"])
    if action_type in _CAMERA_TYPES:
        if "policy" in action and not schema_issues(plan_fragment("camera_policy"), action["policy"], prefix + ".policy"):
            max_delay = int(action["policy"]["max_delay_ms"])
        if device_id is not None:
            params = action.get("params")
            if not catalog.device_supports(device_id, action_type):
                issues.append({"field": prefix + ".type", "reason": "unsupported", "value": action_type})
            elif isinstance(params, dict) and isinstance(params.get("type"), str) and params["type"]:
                definition = catalog.parameter_definition(device_id, action_type, params["type"])
                if definition is None:
                    issues.append({"field": prefix + ".params.type", "reason": "unsupported", "value": params["type"]})
                else:
                    validate_parameter_schema(params["type"], definition.schema)
                    parameter_type = params["type"]
                    parameter_result = validate_capture_params(params, definition)
                    if parameter_result.ok:
                        effective = parameter_result.effective_params
                    else:
                        for issue in parameter_result.failure_details["issues"]:
                            rewritten = {**issue, "field": prefix + ".params" + issue["field"][6:]}
                            if rewritten not in issues:
                                issues.append(rewritten)
        if not issues:
            driver_id = catalog.driver_id(device_id)
            if not isinstance(driver_id, str) or not driver_id:
                raise RuleError("合法拍摄动作缺少驱动绑定")
    return ActionValidation(
        name=name, raw=action, ok=not issues,
        failure="; ".join(issue["field"] + ": " + issue["reason"] for issue in issues) or None,
        effective_params=effective if not issues else None,
        device_id=device_id, action_type=action_type,
        scheduled_at_micros=scheduled, group_name=group, max_delay_ms=max_delay,
        input_fields=input_fields,
        failure_code="action_validation_failed" if issues else None,
        failure_details={"issues": issues} if issues else None,
        driver_id=driver_id,
        parameter_type=parameter_type, parameter_definition=definition,
    )


def validate_capture_params(
    raw: JsonValue, definition: ParameterDefinition
) -> ActionValidation:
    """按驱动参数定义校验并补齐默认值；原输入保持不变。"""
    issues = schema_issues(definition.schema, raw, "params")
    if issues:
        return ActionValidation(name="", raw=raw, ok=False,
                                failure="; ".join(i["field"] + ": " + i["reason"] for i in issues),
                                failure_details={"issues": list(issues)})
    if not isinstance(raw, dict):
        raise RuleError("拍摄参数 Schema 必须约束完整对象")
    from camctl.devices.catalog import apply_defaults

    effective = apply_defaults(raw, definition.defaults)
    if schema_issues(definition.schema, effective, "params"):
        raise RuleError("驱动默认值使合法原参数不再符合参数规则")
    return ActionValidation(name="", raw=raw, ok=True, effective_params=effective)



#: 动作终态编号（succeeded/failed/expired/canceled）。
_TERMINAL_ACTION_STATUSES = frozenset({3, 4, 5, 6})


class PlanState(Enum):
    """父计划的运行状态。"""

    PENDING = 1
    RUNNING = 2
    COMPLETED = 3


@dataclass(frozen=True)
class ActionManagement:
    """派生父状态所需的动作事实：持久化状态及是否实际开始。"""

    status: int
    execution_started: int


def derive_plan_state(actions: Sequence[ActionManagement]) -> PlanState:
    """按全部动作的持久化事实派生父计划状态。

    全部动作终态才 COMPLETED；存在实际开始过（execution_started=1）
    的动作且尚有非终态时为 RUNNING；未执行而取消的动作不构成开始
    事实，与剩余 pending 一起保持 PENDING。
    """
    entries = list(actions)
    if entries and all(action.status in _TERMINAL_ACTION_STATUSES for action in entries):
        return PlanState.COMPLETED
    if any(action.execution_started == 1 for action in entries):
        return PlanState.RUNNING
    return PlanState.PENDING


@dataclass(frozen=True)
class RequestIdentityDecision:
    """完整解析后的请求身份；非法值只形成业务诊断。"""

    request_id: ObjectId | None
    error: dict | None


def extract_request_identity(document: JsonValue) -> RequestIdentityDecision:
    from camctl.contracts.json_values import MISSING, json_field
    from camctl.contracts.values import parse_object_id, ValueTypeError, ValueRangeError

    if not isinstance(document, dict):
        return RequestIdentityDecision(None, {
            "stage": "admission", "code": "plan_body_rejected",
            "details": {"field": "", "reason": "type", "value": document},
        })
    raw = json_field(document, "request_id")
    details = {"field": "request_id"}
    if raw is MISSING:
        details["reason"] = "required"
    else:
        try:
            return RequestIdentityDecision(parse_object_id(raw), None)
        except ValueTypeError:
            details["reason"] = "type"
        except ValueFormatError:
            details["reason"] = "format"
        except ValueRangeError:
            details["reason"] = "range"
        details["value"] = raw
    return RequestIdentityDecision(None, {
        "stage": "admission", "code": "invalid_request_id", "details": details,
    })
