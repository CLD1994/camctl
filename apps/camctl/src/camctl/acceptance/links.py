"""完整本计划关联与执行定义的准备。

先按完整输入建立名称索引，再解析本计划内的来源引用；自动预览
冲突使全部冲突取回失败。跨计划来源（实例 ID 与组）不在受理时
查询；失败动作保留原字段，不修正成合法执行定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from camctl.acceptance.rules import ActionValidation, BodyDecision
from camctl.contracts.json_values import JsonValue
from camctl.contracts.values import to_utc_micros

__all__ = ["PlanIdentities", "PreparedAction", "PreparedPlan", "prepare_plan"]

_CAMERA_TYPES = frozenset({"camera_take_photo", "camera_record", "camera_timelapse"})
#: 公共 Schema 要求携带 scheduled_at 的动作类型（取消与报告不需要）。
_SCHEDULED_TYPES = _CAMERA_TYPES | {"obtain_action_outputs", "delete_action_outputs"}
_OBTAIN_TYPE = "obtain_action_outputs"


@dataclass(frozen=True)
class PlanIdentities:
    """本次受理分配的计划与动作身份（由原子事务分配）。"""

    plan_id: int
    action_ids: Mapping[str, int]


@dataclass(frozen=True)
class PreparedAction:
    """一个动作的完整受理准备结果。"""

    action_id: int
    input_index: int
    name: str
    action_type: str
    device_id: str | None
    group_name: str | None
    scheduled_at_micros: int | None
    raw_fields: JsonValue
    ok: bool
    failure_code: str | None
    failure_reason: str | None
    effective_params: JsonValue | None
    in_plan_dependencies: tuple[str, ...]
    auto_preview_source: str | None


@dataclass(frozen=True)
class PreparedPlan:
    """一次完整受理的计划成员、定义及关联。"""

    plan_id: int
    request_id: str
    created_at_micros: int
    name: str
    raw: JsonValue
    actions: tuple[PreparedAction, ...]

    @property
    def failed_actions(self) -> tuple[PreparedAction, ...]:
        return tuple(action for action in self.actions if not action.ok)


def prepare_plan(decision: BodyDecision, allocated: PlanIdentities) -> PreparedPlan:
    """从受理判定与已分配身份构造完整计划成员及关联。"""
    raw = decision.raw
    by_name = {action.name: action for action in decision.actions}
    conflicts = _auto_preview_conflicts(decision)

    prepared: list[PreparedAction] = []
    for index, validation in enumerate(decision.actions):
        prepared.append(
            _prepare_action(validation, index, by_name, conflicts, allocated)
        )
    return PreparedPlan(
        plan_id=allocated.plan_id,
        request_id=raw["request_id"],
        created_at_micros=to_utc_micros(raw["created_at"]),
        name=raw["name"],
        raw=raw,
        actions=tuple(prepared),
    )


def _auto_preview_conflicts(decision: BodyDecision) -> Mapping[str, tuple[str, ...]]:
    """按来源拍摄收集自动预览取回；同一来源多个时全部冲突。"""
    declared: dict[str, list[str]] = {}
    for action in decision.actions:
        if action.action_type != _OBTAIN_TYPE:
            continue
        params = action.raw.get("params", {})
        if params.get("purpose") != "auto_preview":
            continue
        source = params.get("source", {})
        source_name = source.get("action_name")
        if isinstance(source_name, str):
            declared.setdefault(source_name, []).append(action.name)
    return {
        source: tuple(names) for source, names in declared.items() if len(names) > 1
    }


def _prepare_action(
    validation: ActionValidation,
    index: int,
    by_name: Mapping[str, ActionValidation],
    conflicts: Mapping[str, tuple[str, ...]],
    allocated: PlanIdentities,
) -> PreparedAction:
    raw = validation.raw
    action_type = validation.action_type or raw.get("type")
    ok = validation.ok
    failure_code: str | None = None
    failure_reason: str | None = validation.failure
    group_name: str | None = None
    scheduled_micros: int | None = None
    dependencies: tuple[str, ...] = ()
    auto_source: str | None = None

    if action_type in _CAMERA_TYPES:
        group_name = raw.get("group")
    if action_type in _SCHEDULED_TYPES:
        scheduled_micros = to_utc_micros(raw["scheduled_at"])
    if action_type == _OBTAIN_TYPE:
        # 非拍摄动作携带 group 已由公共 Schema 整份拒绝（A2 层）。
        outcome = _resolve_obtain_sources(raw, by_name, conflicts)
        ok = ok and outcome.ok
        if not outcome.ok:
            failure_code = outcome.failure_code
            failure_reason = outcome.failure_reason
        dependencies = outcome.dependencies
        auto_source = outcome.auto_source

    return PreparedAction(
        action_id=allocated.action_ids[validation.name],
        input_index=index,
        name=validation.name,
        action_type=action_type,
        device_id=validation.device_id,
        group_name=group_name,
        scheduled_at_micros=scheduled_micros,
        raw_fields=raw,
        ok=ok,
        failure_code=failure_code,
        failure_reason=failure_reason,
        effective_params=validation.effective_params,
        in_plan_dependencies=dependencies,
        auto_preview_source=auto_source,
    )


@dataclass(frozen=True)
class _SourceOutcome:
    ok: bool
    failure_code: str | None
    failure_reason: str | None
    dependencies: tuple[str, ...]
    auto_source: str | None


def _resolve_obtain_sources(
    raw: JsonValue,
    by_name: Mapping[str, ActionValidation],
    conflicts: Mapping[str, tuple[str, ...]],
) -> _SourceOutcome:
    params = raw.get("params", {})
    source = params.get("source", {})
    purpose = params.get("purpose")
    if purpose == "auto_preview":
        source_name = source.get("action_name")
        conflict = conflicts.get(source_name)
        if conflict is not None:
            return _SourceOutcome(
                ok=False,
                failure_code="duplicate_auto_preview",
                failure_reason=(
                    f"来源拍摄 {source_name} 的自动预览取回冲突: {sorted(conflict)}"
                ),
                dependencies=(),
                auto_source=source_name,
            )
        target = by_name.get(source_name)
        if target is None or target.action_type not in _CAMERA_TYPES:
            return _SourceOutcome(
                ok=False,
                failure_code="invalid_source_reference",
                failure_reason=f"自动预览来源不是本计划的拍摄动作: {source_name!r}",
                dependencies=(),
                auto_source=source_name,
            )
        return _SourceOutcome(
            ok=True,
            failure_code=None,
            failure_reason=None,
            dependencies=(source_name,),
            auto_source=source_name,
        )
    if "action_name" in source:
        source_name = source["action_name"]
        target = by_name.get(source_name)
        if target is None or target.action_type not in _CAMERA_TYPES:
            return _SourceOutcome(
                ok=False,
                failure_code="invalid_source_reference",
                failure_reason=f"来源不是本计划的拍摄动作: {source_name!r}",
                dependencies=(),
                auto_source=None,
            )
        return _SourceOutcome(
            ok=True,
            failure_code=None,
            failure_reason=None,
            dependencies=(source_name,),
            auto_source=None,
        )
    # 实例 ID 与组引用是跨计划来源：不在受理时查询。
    return _SourceOutcome(
        ok=True, failure_code=None, failure_reason=None, dependencies=(), auto_source=None
    )
