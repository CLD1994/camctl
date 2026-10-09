import type { CameraActionType } from "./actions";
import type { ValidateFunction } from "ajv";
import type { ActionType, Issue } from "./types";
import planSchema from "../../../../protocol/schemas/plan.schema.json";
import { createProtocolValidator } from "./protocol-validation";
import { isCanonicalId, isUint, isObject } from "./validation";

export function isSyncBasis(
  report: { report_id: string; to_wm: number },
  coverage: unknown,
): boolean {
  return (
    isCanonicalId(report.report_id) &&
    isUint(report.to_wm) &&
    isUint(coverage) &&
    report.to_wm <= coverage
  );
}

export type Mode = {
  id:
    | "action_name"
    | "group"
    | "action_instance_id"
    | "plan_group"
    | "current_plan"
    | "plan_instance_id"
    | "request_id"
    | "output_ids";
  label: string;
  fields: Record<string, string>;
  constants?: Record<string, unknown>;
};
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
  {
    id: "current_plan",
    label: "当前整个计划",
    fields: {},
    constants: { current_plan: true },
  },
  {
    id: "plan_instance_id",
    label: "指定整个计划",
    fields: { plan_instance_id: "来源计划实例 ID" },
  },
];
export const cleanupModes: Mode[] = [
  { id: "output_ids", label: "精确产物列表", fields: {} },
  ...sources.filter((m) => m.id !== "group" && m.id !== "plan_group"),
];

/** 只识别引用的字段形状，值是否合法仍由协议校验判断。 */
export function referenceMode(
  reference: unknown,
  modes: readonly Mode[],
): Mode | undefined {
  if (!isObject(reference)) return undefined;
  return modes.find((m) => {
    const keys = [...Object.keys(m.fields), ...Object.keys(m.constants ?? {})];
    return (
      keys.length === Object.keys(reference).length &&
      keys.every((k) => Object.hasOwn(reference, k))
    );
  });
}
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

/** 非拍摄动作的参数契约直接编译自公共 plan.schema.json，不另维护字段清单。 */
const paramDefs = {
  motor_control: "motor_params",
  obtain_action_outputs: "obtain_params",
  delete_action_outputs: "delete_params",
  cancel_task: "cancel_params",
  report_status: "report_params",
} as const;

/** 读取参数对象的声明字段并集；组合合法性由完整 Schema 判断。 */
function declaredFields(schema: unknown, seen = new Set<string>()): string[] {
  if (!isObject(schema)) return [];
  const keys = isObject(schema.properties)
    ? Object.keys(schema.properties)
    : [];
  if (
    typeof schema.$ref === "string" &&
    schema.$ref.startsWith("#/$defs/") &&
    !seen.has(schema.$ref)
  ) {
    const name = schema.$ref.slice("#/$defs/".length);
    keys.push(
      ...declaredFields(
        (planSchema.$defs as Record<string, unknown>)[name],
        new Set([...seen, schema.$ref]),
      ),
    );
  }
  for (const keyword of ["oneOf", "anyOf", "allOf"])
    if (Array.isArray(schema[keyword]))
      for (const branch of schema[keyword])
        keys.push(...declaredFields(branch, seen));
  return [...new Set(keys)];
}
export const builtinFields = Object.fromEntries(
  Object.entries(paramDefs).map(([type, def]) => [
    type,
    declaredFields(planSchema.$defs[def]),
  ]),
) as unknown as Record<
  Exclude<ActionType, CameraActionType>,
  readonly string[]
>;

let compiledParams: Map<string, ValidateFunction> | undefined;
function paramValidators() {
  if (!compiledParams) {
    const ajv = createProtocolValidator();
    const defs = (planSchema as unknown as { $defs: object }).$defs;
    compiledParams = new Map(
      Object.values(paramDefs).map((def) => [
        def,
        ajv.compile({ $defs: defs, $ref: `#/$defs/${def}` }),
      ]),
    );
  }
  return compiledParams;
}

/** 非拍摄动作的静态参数契约；引用存在性由调用方已有完整资料判断。 */
export function validateBuiltinParams(
  type: Exclude<ActionType, CameraActionType>,
  params: unknown,
  present: boolean,
): Issue[] {
  if (type === "report_status" && !present)
    return [
      {
        path: "params",
        code: "invalid_params",
        message: "请选择完整或增量同步并填写报告参数",
      },
    ];
  const validate = paramValidators().get(paramDefs[type]);
  if (!validate || !validate(params))
    return [
      {
        path: "params",
        code: "invalid_params",
        message: "动作参数不符合公共参数契约",
      },
    ];
  return [];
}
