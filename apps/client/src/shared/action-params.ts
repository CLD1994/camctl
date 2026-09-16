import type { CameraActionType } from "./actions";
import type { ActionType, Issue } from "./types";
import { isId, isName, isObject, isPositive, isUint } from "./validation";

export const builtinFields: Record<
  Exclude<ActionType, CameraActionType>,
  readonly string[]
> = {
  obtain_action_outputs: ["source", "output_ids"],
  delete_action_outputs: ["output_ids"],
  cancel_task: ["target"],
  report_status: ["scope", "after_report_id"],
};

export function isSyncBasis(
  report: { report_id: number; to_wm: number },
  coverage: unknown,
): boolean {
  return (
    isPositive(report.report_id) &&
    isUint(report.to_wm) &&
    isUint(coverage) &&
    report.to_wm <= coverage
  );
}

type Mode = { id: string; label: string; fields: Record<string, string> };
export const sources: Mode[] = [
  {
    id: "action_name",
    label: "本计划中的动作",
    fields: { action_name: "来源动作名称" },
  },
  { id: "group", label: "本计划中的组", fields: { group: "来源组" } },
  {
    id: "action_instance_id",
    label: "指定动作实例",
    fields: { action_instance_id: "来源动作实例 ID" },
  },
  {
    id: "plan_group",
    label: "指定计划实例中的组",
    fields: { plan_instance_id: "来源计划实例 ID", group: "来源组" },
  },
];
export const targets: Mode[] = [
  {
    id: "request_id",
    label: "原请求的整个计划",
    fields: { request_id: "目标请求 ID" },
  },
  {
    id: "plan_instance_id",
    label: "指定计划实例",
    fields: { plan_instance_id: "目标计划实例 ID" },
  },
  {
    id: "action_instance_id",
    label: "指定动作实例",
    fields: { action_instance_id: "目标动作实例 ID" },
  },
  {
    id: "plan_group",
    label: "指定计划实例中的组",
    fields: { plan_instance_id: "目标计划实例 ID", group: "目标组" },
  },
];

/** 非拍摄动作的静态参数契约；引用存在性由调用方已有完整资料判断。 */
export function validateBuiltinParams(
  type: Exclude<ActionType, CameraActionType>,
  params: unknown,
  present: boolean,
): Issue[] {
  const issues: Issue[] = [];
  const check = (ok: boolean, path: string, message: string) => {
    if (!ok) issues.push({ path, code: "invalid_params", message });
  };
  const fields = (
    value: Record<string, unknown>,
    allowed: readonly string[],
    path: string,
  ) => {
    for (const key of Object.keys(value))
      check(allowed.includes(key), `${path}.${key}`, "不接受此字段");
  };
  const ids = (value: unknown) => {
    check(
      Array.isArray(value) && value.length > 0,
      "params.output_ids",
      "必须填写非空产物 ID 数组",
    );
    if (Array.isArray(value)) {
      const seen = new Set<unknown>();
      value.forEach((id, i) => {
        check(
          isId(id) && !seen.has(id),
          `params.output_ids[${i}]`,
          "产物 ID 必须合法且不重复",
        );
        seen.add(id);
      });
    }
  };
  const reference = (
    value: unknown,
    combinations: string[][],
    path: string,
  ) => {
    if (!isObject(value)) {
      check(false, path, "引用必须是对象");
      return;
    }
    const keys = Object.keys(value);
    check(
      combinations.some(
        (combo) =>
          combo.length === keys.length && combo.every((k) => keys.includes(k)),
      ),
      path,
      "引用字段组合不合法",
    );
    for (const [key, v] of Object.entries(value))
      check(
        key === "group" || key === "action_name" ? isName(v) : isId(v),
        `${path}.${key}`,
        "引用值不合法",
      );
  };
  if (type === "report_status" && !present) return issues;
  if (!isObject(params))
    return [
      { path: "params", code: "invalid_params", message: "参数必须是对象" },
    ];
  switch (type) {
    case "report_status": {
      const keys = Object.keys(params);
      check(
        !keys.length ||
          (keys.length === 1 && params.scope === "full") ||
          (keys.length === 2 &&
            keys.includes("scope") &&
            keys.includes("after_report_id") &&
            params.scope === "since" &&
            isPositive(params.after_report_id)),
        "params",
        "报告范围参数组合不合法",
      );
      break;
    }
    case "delete_action_outputs":
      fields(params, builtinFields[type], "params");
      ids(params.output_ids);
      break;
    case "cancel_task":
      fields(params, builtinFields[type], "params");
      reference(
        params.target,
        targets.map((mode) => Object.keys(mode.fields)),
        "params.target",
      );
      break;
    case "obtain_action_outputs":
      fields(params, builtinFields[type], "params");
      reference(
        params.source,
        sources.map((mode) => Object.keys(mode.fields)),
        "params.source",
      );
      if (Object.hasOwn(params, "output_ids")) {
        ids(params.output_ids);
        check(
          !isObject(params.source) || !Object.hasOwn(params.source, "group"),
          "params.output_ids",
          "产物筛选只适用于动作级引用",
        );
      }
      break;
  }
  return issues;
}
