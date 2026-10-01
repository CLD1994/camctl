"""完整本计划关联与执行定义的准备。

先按完整输入建立名称索引，再解析本计划内的来源引用；自动预览
冲突使全部冲突取回失败。跨计划来源（实例 ID 与组）不在受理时
查询；失败动作保留原字段，不修正成合法执行定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from camctl.acceptance.rules import ActionValidation, BodyDecision
from camctl.acceptance.definitions import build_action_spec
from camctl.acceptance.schema import RuleError, plan_fragment, schema_issues
from camctl.contracts.json_values import JsonValue
from camctl.contracts.values import to_utc_micros, format_utc_micros, ConsistencyError
from camctl.contracts.enums import enum_for
from camctl.outputs.sources import ActionFacts, SourceSpec, SourceResolution, ResolutionState, ResolveFailure, resolve_source

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
    max_delay_ms: int | None = None
    driver_id: str | None = None
    failure_details: JsonValue | None = None
    source_resolution_state: int | None = None
    source_plan_id: int | None = None
    auto_preview_source_id: int | None = None
    preview_support: bool | None = None
    source_parameter_type: str | None = None
    execution_spec: JsonValue | None = None


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
    lookup = _InputSources(decision, allocated)

    prepared: list[PreparedAction] = []
    for index, validation in enumerate(decision.actions):
        prepared.append(
            _prepare_action(validation, index, by_name, conflicts, allocated, lookup)
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
        params = action.raw.get("params")
        if not isinstance(params, dict):
            continue
        if params.get("purpose") != "auto_preview":
            continue
        source = params.get("source")
        if not isinstance(source, dict):
            continue
        source_name = source.get("action_name")
        target = next((candidate for candidate in decision.actions if candidate.name == source_name), None)
        if set(source) == {"action_name"} and target is not None and target.action_type in _CAMERA_TYPES:
            declared.setdefault(source_name, []).append(action.name)
    return {
        source: tuple(sorted(names)) for source, names in declared.items() if len(names) > 1
    }


def _prepare_action(
    validation: ActionValidation,
    index: int,
    by_name: Mapping[str, ActionValidation],
    conflicts: Mapping[str, tuple[str, ...]],
    allocated: PlanIdentities,
    lookup: _InputSources,
) -> PreparedAction:
    raw = validation.raw
    action_type = validation.action_type or raw.get("type")
    ok = validation.ok
    failure_code: str | None = validation.failure_code
    failure_reason: str | None = validation.failure
    group_name: str | None = None
    scheduled_micros: int | None = None
    dependencies: tuple[str, ...] = ()
    auto_source: str | None = None

    group_name = validation.group_name
    scheduled_micros = validation.scheduled_at_micros
    source_state = source_plan_id = None
    failure_details = validation.failure_details
    params = raw.get("params")
    source = params.get("source") if isinstance(params, dict) else None
    reliable_source = (isinstance(source, dict)
        and not schema_issues(plan_fragment("source"), source, f"actions[{index}].params.source")
        and all(not isinstance(source.get(key), str) or source[key] == source[key].strip() for key in ("action_name", "group"))
        and (action_type != "delete_action_outputs" or set(source) in ({"action_name"},{"action_instance_id"},{"plan_instance_id"},{"current_plan"})))
    if action_type in {_OBTAIN_TYPE, "delete_action_outputs"} and reliable_source:
        if "action_instance_id" in source or "plan_instance_id" in source:
            if ok:
                source_state = int(ResolutionState.PENDING)
        else:
            resolution = resolve_source(SourceSpec(**source), lookup)
            if resolution.state == ResolutionState.FIXED:
                if ok:
                    source_state = int(resolution.state)
                    source_plan_id = resolution.source_plan_id
                    dependencies = tuple(lookup.by_id[member_id].name for member_id in resolution.member_action_ids)
            else:
                key = "group" if "group" in source else "action_name"
                issue = {"field":f"actions[{index}].params.source.{key}", "reason":"reference", "value":source[key], "context":{"reason":resolution.failure.value}}
                if ok and resolution.failure == ResolveFailure.ACTION_NOT_FOUND:
                    failure_code = "source_action_not_found"
                    failure_details = {"field":f"actions[{index}].params.source.action_name", "value":source["action_name"]}
                else:
                    failure_code = "action_validation_failed"
                    previous = list(failure_details.get("issues", ())) if failure_details else []
                    failure_details = {"issues":[*previous, issue]}
                ok = False
                failure_reason = f"本计划来源无法解析: {source!r}（{resolution.failure.value}）"
    preview_support = source_parameter_type = auto_source_id = None
    if action_type == _OBTAIN_TYPE and isinstance(raw.get("params"), dict):
        params = raw["params"]
        source = params.get("source")
        if (params.get("purpose") == "auto_preview" and isinstance(source, dict)
                and set(source) == {"action_name"}
                and not schema_issues(plan_fragment("action/properties/name"), source["action_name"], f"actions[{index}].params.source.action_name")
                and source["action_name"] == source["action_name"].strip()):
            auto_source = source["action_name"]
            target = by_name.get(auto_source)
            additions = []
            if target is None:
                if ok:
                    failure_code = "source_action_not_found"
                    failure_details = {"field": f"actions[{index}].params.source.action_name", "value": auto_source}
                elif failure_code != "source_action_not_found":
                    additions.append({"field":f"actions[{index}].params.source.action_name", "reason":"reference", "value":auto_source})
                ok = False
                failure_reason = f"自动预览来源不存在: {auto_source}"
            elif target.action_type not in _CAMERA_TYPES:
                additions.append({"field":f"actions[{index}].params.source.action_name", "reason":"reference", "value":auto_source})
            else:
                auto_source_id = allocated.action_ids[target.name]
                source_parameter_type = target.parameter_type
                if target.parameter_definition is not None:
                    preview_support = target.parameter_definition.preview_supported
                    if not isinstance(preview_support, bool):
                        raise RuleError("来源参数类型缺少明确的预览支持声明")
                if source_parameter_type is None:
                    additions.append({"field":f"actions[{index}].params.source.action_name", "reason":"reference", "value":auto_source, "context":{"reason":"source_parameter_type_unavailable"}})
                if target.scheduled_at_micros is None or validation.scheduled_at_micros != target.scheduled_at_micros:
                    issue = {"field":f"actions[{index}].scheduled_at", "reason":"combination", "context":{"source_action_name":auto_source}}
                    if "scheduled_at" in raw:
                        issue["value"] = raw["scheduled_at"]
                    if target.scheduled_at_micros is not None:
                        issue["context"]["source_scheduled_at"] = format_utc_micros(target.scheduled_at_micros)
                    additions.append(issue)
                if preview_support is False:
                    if ok and not additions:
                        ok = False
                        failure_code = "preview_not_supported"
                        failure_details = {"source_action_name":auto_source, "parameter_type":source_parameter_type}
                        failure_reason = f"来源参数类型不支持预览: {source_parameter_type}"
                    else:
                        additions.append({"field":f"actions[{index}].params.source.action_name", "reason":"unsupported", "value":auto_source, "context":{"parameter_type":source_parameter_type, "preview_supported":False}})
            if additions:
                previous = list(failure_details.get("issues", ())) if failure_code == "action_validation_failed" else []
                for issue in additions:
                    if issue not in previous:
                        previous.append(issue)
                ok = False
                failure_code = "action_validation_failed"
                failure_details = {"issues":previous}
                failure_reason = "; ".join(issue["field"] + ": " + issue["reason"] for issue in previous)
            if auto_source in conflicts:
                ok = False
                extra_issues = list(failure_details.get("issues", ())) if failure_code == "action_validation_failed" else []
                if failure_code == "preview_not_supported":
                    extra_issues.append({"field":f"actions[{index}].params.source.action_name", "reason":"unsupported", "value":auto_source,
                        "context":{"parameter_type":source_parameter_type,"preview_supported":False}})
                failure_code = "duplicate_auto_preview"
                failure_details = {"source_action_name":auto_source, "obtain_action_names":list(conflicts[auto_source])}
                if extra_issues:
                    failure_details["issues"] = extra_issues
                failure_reason = f"来源拍摄 {auto_source} 的自动预览取回冲突: {conflicts[auto_source]}"
    if not ok:
        dependencies = ()
        source_state = source_plan_id = None

    return PreparedAction(
        action_id=allocated.action_ids[validation.name],
        input_index=index,
        name=validation.name,
        action_type=action_type,
        device_id=validation.device_id,
        group_name=group_name,
        scheduled_at_micros=scheduled_micros,
        raw_fields=validation.input_fields,
        ok=ok,
        failure_code=failure_code,
        failure_reason=failure_reason,
        effective_params=validation.effective_params,
        in_plan_dependencies=dependencies,
        auto_preview_source=auto_source,
        max_delay_ms=validation.max_delay_ms,
        driver_id=validation.driver_id,
        failure_details=failure_details,
        source_resolution_state=source_state, source_plan_id=source_plan_id,
        auto_preview_source_id=auto_source_id, preview_support=preview_support,
        source_parameter_type=source_parameter_type,
        execution_spec=build_action_spec(validation) if ok else None,
    )


class _InputSources:
    """完整本计划输入的来源查询适配；不能查询跨计划事实。"""

    def __init__(self, decision: BodyDecision, identities: PlanIdentities):
        self.plan_id = identities.plan_id
        self.by_id = {
            identities.action_ids[action.name]: ActionFacts(
                action_id=identities.action_ids[action.name], plan_id=self.plan_id,
                action_type=int(enum_for("actions.type")[action.action_type.upper()]),
                name=action.name, group_name=action.group_name,
            ) for action in decision.actions
        }

    def owner_plan_id(self) -> int:
        return self.plan_id

    def plan_exists(self, plan_id: int) -> bool:
        if plan_id != self.plan_id:
            raise ConsistencyError("受理不得查询跨计划来源")
        return True

    def action_by_id(self, action_id: int) -> ActionFacts | None:
        raise ConsistencyError("受理不得按实例查询来源")

    def plan_actions(self, plan_id: int) -> tuple[ActionFacts, ...]:
        if plan_id != self.plan_id:
            raise ConsistencyError("受理不得查询跨计划成员")
        return tuple(self.by_id[key] for key in sorted(self.by_id))
