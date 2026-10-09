import { ACTION_TYPES, isCameraAction } from "./actions";
export { ACTION_TYPES } from "./actions";
import type {
  ActionType,
  Capabilities,
  Issue,
  ValidationContext,
} from "./types";
import { validateParams } from "./capabilities";
import { validateBuiltinParams, isSyncBasis } from "./action-params";
import { createProtocolValidator } from "./protocol-validation";
import { isName, isObject, isTimestamp, schemaIssues } from "./validation";

const validate = createProtocolValidator().getSchema("plan.schema.json")!;

export function validatePlan(
  plan: unknown,
  capabilities: Capabilities | null,
  context: ValidationContext = {},
): Issue[] {
  const issues: Issue[] = validate(plan) ? [] : schemaIssues(validate.errors);
  const issue = (path: string, code: string, message: string) => {
    issues.push({ path, code, message });
  };
  if (!isObject(plan)) return issues;
  if (!isTimestamp(plan.created_at))
    issue("created_at", "invalid_value", "创建时间必须是真实的秒级 UTC 时间");
  if (!Array.isArray(plan.actions)) return issues;
  const actions = plan.actions;
  const names = new Map<string, Record<string, unknown>[]>();
  for (const action of actions) {
    if (!isObject(action) || typeof action.name !== "string") continue;
    const matches = names.get(action.name) ?? [];
    matches.push(action);
    names.set(action.name, matches);
  }
  const cameras = actions.filter((a) => isObject(a) && isCameraAction(a.type));
  const automated = new Map<string, number[]>();
  actions.forEach((action, index) => {
    if (!isObject(action)) return;
    const path = `actions[${index}]`;
    if (typeof action.name === "string" && names.get(action.name)!.length > 1)
      issue(`${path}.name`, "duplicate_name", "动作名称重复");
    if (
      Object.hasOwn(action, "scheduled_at") &&
      !isTimestamp(action.scheduled_at)
    )
      issue(
        `${path}.scheduled_at`,
        "invalid_value",
        "执行时间必须是真实的秒级 UTC 时间",
      );
    if (!ACTION_TYPES.includes(action.type as ActionType)) return;
    const type = action.type as ActionType;
    if (isCameraAction(type)) {
      issues.push(
        ...validateParams(
          typeof action.device_id === "string" ? action.device_id : "",
          type,
          action.params,
          capabilities,
        ).map((i) => ({ ...i, path: `${path}.${i.path}` })),
      );
      return;
    }
    // 参数摘要与公共 Schema 一致；业务引用不能因另一参数错误被提前跳过。
    issues.push(
      ...validateBuiltinParams(
        type,
        action.params,
        Object.hasOwn(action, "params"),
      ).map((i) => ({ ...i, path: `${path}.${i.path}` })),
    );
    if (!isObject(action.params)) return;
    const params = action.params;
    const p = `${path}.params`;
    if (
      type === "report_status" &&
      params.scope === "since" &&
      !context.reports?.some(
        (r) =>
          r.report_id === params.after_report_id &&
          isSyncBasis(r, context.coverage),
      )
    )
      issue(
        `${p}.after_report_id`,
        "sync_basis_unavailable",
        "同步起点没有已保存且完整覆盖的报告依据",
      );
    if (type !== "obtain_action_outputs" && type !== "delete_action_outputs")
      return;
    if (!isObject(params.source)) return;
    const source = params.source;
    const keys = Object.keys(source);
    let sourceAction: Record<string, unknown> | undefined;
    if (keys.length === 1 && isName(source.action_name)) {
      const matches = names.get(source.action_name);
      sourceAction =
        matches?.length === 1 && isCameraAction(matches[0].type)
          ? matches[0]
          : undefined;
      if (!sourceAction)
        issue(
          `${p}.source.action_name`,
          "source_not_found",
          "本计划内没有唯一的同名产物来源动作",
        );
    } else if (keys.length === 1 && source.current_plan === true) {
      if (!cameras.length)
        issue(
          `${p}.source.current_plan`,
          "source_not_found",
          "本计划中没有产物来源动作",
        );
    } else if (
      type === "obtain_action_outputs" &&
      keys.length === 1 &&
      isName(source.group)
    ) {
      if (!cameras.some((a) => a.group === source.group))
        issue(
          `${p}.source.group`,
          "source_not_found",
          "本计划组中没有产物来源动作",
        );
    }
    if (
      type !== "obtain_action_outputs" ||
      params.purpose !== "auto_preview" ||
      !sourceAction
    )
      return;
    const sourceName = source.action_name as string;
    const declarations = automated.get(sourceName) ?? [];
    declarations.push(index);
    automated.set(sourceName, declarations);
    const parameterType = isObject(sourceAction.params)
      ? sourceAction.params.type
      : undefined;
    const parameter = capabilities?.devices
      .find((d) => d.device_id === sourceAction.device_id)
      ?.actions.find((a) => a.type === sourceAction.type)
      ?.parameter_types.find((p) => p.type === parameterType);
    if (!parameter)
      issue(
        `${p}.source.action_name`,
        "preview_support_unavailable",
        "无法从有效设备与参数类型确认来源的预览支持",
      );
    else if (!parameter.preview_supported)
      issue(
        `${p}.source.action_name`,
        "preview_not_supported",
        "来源参数类型不支持预览",
      );
    if (action.scheduled_at !== sourceAction.scheduled_at)
      issue(
        `${path}.scheduled_at`,
        "auto_preview_time_mismatch",
        "自动预览取回时间必须与来源拍摄相同",
      );
  });
  for (const [source, indices] of automated) {
    if (indices.length < 2) continue;
    const conflicts = indices
      .map((i) => JSON.stringify(actions[i].name))
      .join("、");
    for (const index of indices)
      issue(
        `actions[${index}].params.source.action_name`,
        "duplicate_auto_preview",
        `来源拍摄 ${JSON.stringify(source)} 存在多个自动预览取回：${conflicts}`,
      );
  }
  return issues;
}
