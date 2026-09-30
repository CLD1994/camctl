"""公共结构、动作范围及错误优先级的分层校验。

公共结构错误与不支持的动作类型、名称重复按整份拒绝；动作自身
的语义错误（设备不存在、时间非法、参数规则不符）按本动作失败
保存。结果按动作名称组织，不依赖数组顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from camctl.acceptance.schema import (
    BodySchemaError,
    RuleError,
    validate_plan_structure,
    validate_precise,
)
from camctl.acceptance.ports import ParameterDefinition, StaticActionCatalog
from camctl.contracts.json_values import JsonValue
from camctl.contracts.values import ValueFormatError, to_utc_micros

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
    names: set[str] = set()
    for action in actions:
        name = action["name"]
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
    for action in actions:
        validated.append(_validate_action(action, catalog))
    return BodyDecision(
        is_whole_rejection=False,
        actions=tuple(validated),
        raw=raw,
    )


def _validate_action(action: JsonValue, catalog: StaticActionCatalog) -> ActionValidation:
    name = action["name"]
    action_type = action["type"]
    device_id = action.get("device_id")
    failure: str | None = None
    effective: JsonValue | None = None

    if action_type in _CAMERA_TYPES:
        if not catalog.device_exists(device_id):
            failure = f"设备不存在: {device_id!r}"
        else:
            scheduled = action.get("scheduled_at")
            try:
                to_utc_micros(scheduled)
            except ValueFormatError as error:
                failure = f"计划时间非法: {error}"
            if failure is None:
                definition = catalog.parameter_definition(device_id, action_type)
                if definition is None:
                    raise RuleError(
                        f"目录声明支持 {device_id}/{action_type} 但缺少参数定义"
                    )
                params = action.get("params", {})
                validation = validate_capture_params(params, definition)
                if validation.ok:
                    effective = validation.effective_params
                else:
                    failure = f"参数不符合规则: {validation.failure}"

    return ActionValidation(
        name=name,
        raw=action,
        ok=failure is None,
        failure=failure,
        effective_params=effective,
        device_id=device_id,
        action_type=action_type,
    )


def validate_capture_params(
    raw: JsonValue, definition: ParameterDefinition
) -> ActionValidation:
    """按驱动参数定义校验并补齐默认值；原输入保持不变。"""
    if not isinstance(raw, dict):
        return ActionValidation(
            name="",
            raw=raw,
            ok=False,
            failure="参数必须是对象",
        )
    effective: dict[str, Any] = dict(definition.defaults)
    effective.update(raw)
    try:
        validate_precise(definition.schema, effective, label="参数")
    except BodySchemaError as error:
        return ActionValidation(name="", raw=raw, ok=False, failure=str(error))
    return ActionValidation(
        name="", raw=raw, ok=True, effective_params=effective
    )
