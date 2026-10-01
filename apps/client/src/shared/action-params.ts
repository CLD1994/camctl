import type { CameraActionType } from "./actions";
import type { ValidateFunction } from "ajv";
import type { ActionType, Issue } from "./types";
import planSchema from "../../../../protocol/schemas/plan.schema.json";
import statusReportSchema from "../../../../protocol/schemas/status-report.schema.json";
import {
  createValidator,
  isCanonicalId,
  isUint,
  schemaIssues,
} from "./validation";

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

/** 非拍摄动作的参数契约直接编译自公共 plan.schema.json，不另维护字段清单。 */
const paramDefs = {
  obtain_action_outputs: "obtain_params",
  delete_action_outputs: "delete_params",
  cancel_task: "cancel_params",
  report_status: "report_params",
} as const;

let compiledParams: Map<string, ValidateFunction> | undefined;
function paramValidators() {
  if (!compiledParams) {
    const ajv = createValidator();
    ajv.addSchema(
      statusReportSchema as unknown as object,
      "status-report.schema.json",
    );
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
