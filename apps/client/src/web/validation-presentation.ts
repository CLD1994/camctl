import type { Capabilities, Issue } from "../shared/types";
import { isCameraAction } from "../shared/actions";
import planSchema from "../../../../protocol/schemas/plan.schema.json";
import notificationSchema from "../../../../protocol/schemas/host-notification.schema.json";
import {
  builtinFields,
  sources,
  cleanupModes,
  targets,
} from "../shared/action-params";
import { isObject, schemaIssues } from "../shared/validation";
import { createProtocolValidator } from "../shared/protocol-validation";
import type { ValidateFunction } from "ajv";
import { pointer, pointerPath, type Path } from "./editing";

export interface IssuePresentation {
  target: string | null;
  location: string;
  message: string;
  /** 组合或对象结构只允许由 JSON 控件负责。 */
  json?: boolean;
  actionIndex?: number;
}

export type PresentedIssue = IssuePresentation & {
  issue: Issue;
  /** 固定协议投影已证明的责任身份；不由显示文案推断。 */
  responsibility?: string;
};

const names: Record<string, string> = {
  name: "名称",
  actions: "动作列表",
  type: "类型",
  device_id: "目标设备",
  scheduled_at: "执行时间",
  params: "动作参数",
  policy: "业务策略",
  max_delay_ms: "最大允许延迟",
  group: "动作组",
  source: "取回或清理来源",
  target: "取消目标",
  action_name: "来源动作名称",
  action_instance_id: "动作实例 ID",
  plan_instance_id: "计划实例 ID",
  request_id: "请求 ID",
  current_plan: "当前整个计划",
  output_ids: "产物 ID 列表",
  scope: "报告范围",
  after_report_id: "同步起点报告",
  position: "位置",
  filter: "取回筛选",
  purpose: "用途",
};
const hints: Record<string, string> = {
  schema_required: "缺少必填字段，请补齐输入。",
  schema_enum: "请选择规则允许的值。",
  schema_const: "固定值或参数组合不符合规则，请核对相关字段。",
  schema_type: "值的类型不符合规则，请修正输入。",
  schema_if: "参数组合不符合规则，请核对相关字段。",
  schema_additionalProperties: "包含不适用的字段，请通过 JSON 核对并修正。",
  schema_minimum: "数值小于允许的最小值。",
  schema_exclusiveMinimum: "数值必须大于规定的下限。",
  schema_maximum: "数值超过允许的最大值。",
  schema_exclusiveMaximum: "数值必须小于规定的上限。",
  schema_minItems: "列表项数量不足，请补齐输入。",
  schema_maxItems: "列表项数量超过限制。",
  schema_minLength: "文本长度不足，请补齐输入。",
  schema_maxLength: "文本长度超过限制。",
  schema_pattern: "文本格式不符合规则，请核对输入。",
  schema_format: "输入格式不符合规则。",
  schema_uniqueItems: "列表中存在重复项，请核对输入。",
  schema_oneOf: "字段组合不符合规则，请核对相关字段。",
  schema_anyOf: "字段组合不符合规则，请核对相关字段。",
  unfinished_input: "输入尚未完成，请修正保留的原文本。",
  invalid_json: "计划 JSON 无法解析，请修正原文本。",
  invalid_plan: "计划必须是包含动作列表的对象。",
};
const own = (values: Record<string, string>, key: string) =>
  Object.hasOwn(values, key) ? values[key] : undefined;

function pathOf(value: string): Path {
  if (value === "" || value.startsWith("/")) return pointerPath(value);
  const parts: Path = [];
  let rest = value;
  const head = /^[^.\[\]]+/.exec(rest);
  if (!head) throw new Error("路径无法解释");
  parts.push(head[0]);
  rest = rest.slice(head[0].length);
  while (rest) {
    const field = /^\.([^.\[\]]+)/.exec(rest),
      index = /^\[(0|[1-9]\d*)\]/.exec(rest);
    if (field) {
      parts.push(field[1]);
      rest = rest.slice(field[0].length);
    } else if (index) {
      parts.push(index[1]);
      rest = rest.slice(index[0].length);
    } else throw new Error("路径无法解释");
  }
  return parts;
}
function notation(parts: Path) {
  return parts
    .map((part, index) =>
      /^\d+$/.test(String(part)) ? `[${part}]` : `${index ? "." : ""}${part}`,
    )
    .join("");
}
function ambiguous(parts: Path, root: unknown, properties: unknown) {
  let current = root;
  for (let i = 0; i < parts.length; i++) {
    const remaining = notation(parts.slice(i));
    if (
      isObject(current) &&
      Object.keys(current).some(
        (key) =>
          /[.\[\]]/.test(key) &&
          (remaining === key ||
            remaining.startsWith(key + ".") ||
            remaining.startsWith(key + "[")),
      )
    )
      return true;
    current =
      isObject(current) || Array.isArray(current)
        ? current[parts[i] as keyof typeof current]
        : undefined;
  }
  const remaining = notation(parts.slice(3));
  return (
    parts[0] === "actions" &&
    parts[2] === "params" &&
    isObject(properties) &&
    Object.keys(properties).some(
      (key) =>
        /[.\[\]]/.test(key) &&
        (remaining === key ||
          remaining.startsWith(key + ".") ||
          remaining.startsWith(key + "[")),
    )
  );
}

function locateInput(
  path: string,
  plan?: unknown,
  capabilities?: Capabilities | null,
) {
  let parts: Path | null;
  try {
    parts = pathOf(path);
  } catch {
    parts = null;
  }
  const index =
    parts?.[0] === "actions" && /^(0|[1-9]\d*)$/.test(String(parts[1]))
      ? Number(parts[1])
      : undefined;
  const action =
    isObject(plan) && Array.isArray(plan.actions) && index !== undefined
      ? plan.actions[index]
      : undefined;
  const params =
    isObject(action) && isObject(action.params) ? action.params : undefined;
  const parameter =
    isObject(action) && params
      ? capabilities?.devices
          .find((device) => device.device_id === action.device_id)
          ?.actions.find((candidate) => candidate.type === action.type)
          ?.parameter_types.find((candidate) => candidate.type === params.type)
      : undefined;
  const properties = parameter?.schema.properties;
  const key = parts?.at(-1);
  let identified = parts?.length === 0;
  let field =
    key === undefined ? "计划" : (own(names, String(key)) ?? String(key));
  if (parts?.length === 1 && (key === "name" || key === "actions"))
    identified = true;
  if (
    index !== undefined &&
    isObject(action) &&
    ((parts?.length === 3 &&
      Object.hasOwn(planSchema.$defs.action.properties, String(key))) ||
      (parts?.length === 4 &&
        parts[2] === "policy" &&
        key === "max_delay_ms" &&
        (isCameraAction(action.type) ||
          action.type === notificationSchema.properties.type.const)) ||
      (parts?.length === 4 &&
        parts[2] === "params" &&
        key === "type" &&
        isCameraAction(action.type)))
  )
    identified = true;
  if (parts?.length === 1 && key === "name") field = "计划名称";
  if (parts?.length === 3 && key === "name") field = "动作名称";
  if (parts?.length === 3 && key === "type") field = "动作类型";
  if (parts?.[2] === "params" && parts[3] === "type") field = "参数类型";
  const builtin =
    isObject(action) && typeof action.type === "string"
      ? action.type === notificationSchema.properties.type.const
        ? Object.keys(notificationSchema.$defs.motor_params.properties)
        : Object.hasOwn(builtinFields, action.type)
          ? builtinFields[action.type as keyof typeof builtinFields]
          : undefined
      : undefined;
  if (
    parts?.[2] === "params" &&
    builtin?.includes(String(parts[3])) &&
    own(names, String(key)) !== undefined
  ) {
    if (parts.length === 4) identified = true;
    if (
      parts.length === 5 &&
      (parts[3] === "source" || parts[3] === "target")
    ) {
      const modes =
        parts[3] === "target"
          ? targets
          : action.type === "delete_action_outputs"
            ? cleanupModes
            : sources;
      identified = modes.some(
        (mode) =>
          Object.hasOwn(mode.fields, String(key)) ||
          Object.hasOwn(mode.constants ?? {}, String(key)),
      );
    }
  }
  const definition = isObject(properties) ? properties[String(key)] : undefined;
  if (
    parts?.[2] === "params" &&
    parts.length === 4 &&
    isObject(definition) &&
    typeof definition.title === "string"
  ) {
    field = definition.title;
    identified = definition.title.trim() !== "";
  }
  if (parts?.length === 2 && index !== undefined) {
    field = "动作内容";
    identified = action !== undefined;
  }
  const actionName =
    isObject(action) && typeof action.name === "string"
      ? `「${action.name}」`
      : "";
  const location =
    index !== undefined
      ? `动作 ${index + 1}${actionName} · ${field}`
      : parts === null
        ? `计划 JSON（${path}）`
        : field;
  if (parts && !path.startsWith("/") && ambiguous(parts, plan, properties))
    parts = null;
  return {
    target: parts === null ? null : pointer(parts),
    location,
    identified,
    actionIndex: index,
  };
}

/** 未完成输入只采用已识别名称；技术路径由调用者另行提供诊断入口。 */
export function presentPendingInput(
  path: string,
  ordinal: number,
  plan?: unknown,
  capabilities?: Capabilities | null,
): string {
  try {
    pointerPath(path);
  } catch {
    return `未识别的输入 ${ordinal}`;
  }
  const result = locateInput(path, plan, capabilities);
  return result.identified ? result.location : `未识别的输入 ${ordinal}`;
}

/** 仅生成界面说明和可证明的路径，不修改校验事实或草稿。 */
export function presentIssue(
  issue: Issue,
  plan?: unknown,
  capabilities?: Capabilities | null,
): IssuePresentation {
  const { target, location, actionIndex } = locateInput(
    issue.pointer ?? issue.path,
    plan,
    capabilities,
  );
  let message =
    issue.code === "schema_minItems" && issue.path === "actions"
      ? "至少添加一个动作。"
      : (own(hints, issue.code) ??
        (issue.code.startsWith("schema_") ||
        !/[\u4e00-\u9fff]/.test(issue.message)
          ? "输入不符合规则，请核对填写内容。"
          : issue.message));
  const params = issue.schema?.params;
  if (issue.code === "schema_type" && typeof params?.type === "string")
    message = `需要${({ object: "对象", array: "列表", integer: "整数", number: "数值", string: "文本", boolean: "布尔值" } as Record<string, string>)[params.type] ?? params.type}。`;
  if (
    [
      "schema_minimum",
      "schema_maximum",
      "schema_exclusiveMinimum",
      "schema_exclusiveMaximum",
    ].includes(issue.code) &&
    typeof params?.limit === "number"
  )
    message = `${own(hints, issue.code)}限制为 ${params.limit}。`;
  if (issue.code === "schema_format" && typeof params?.format === "string")
    message = `输入不符合 ${params.format} 格式。`;
  return {
    target,
    location,
    message,
    actionIndex,
    ...(issue.code === "schema_oneOf" ||
    issue.code === "schema_anyOf" ||
    issue.code === "schema_if"
      ? { json: true }
      : {}),
  };
}

const protocol = createProtocolValidator();
const presentationValidators = new Map<string, ValidateFunction>();
const defs = planSchema.$defs as Record<string, unknown>;

/** 只解引用固定协议的已登记参数容器，叶子规则仍交给 Ajv。 */
function containerSchema(schema: unknown): Record<string, unknown> | undefined {
  if (!isObject(schema)) return undefined;
  if (typeof schema.$ref !== "string") return schema;
  if (schema.$ref.startsWith("#/$defs/"))
    return containerSchema(defs[schema.$ref.slice("#/$defs/".length)]);
  if (schema.$ref === "host-notification.schema.json#/$defs/motor_params")
    return notificationSchema.$defs.motor_params;
  return undefined;
}

function actionClause(action: Record<string, unknown>) {
  return planSchema.$defs.action.allOf.find((clause) => {
    const definition = clause.if.properties.type;
    return "const" in definition
      ? definition.const === action.type
      : isCameraAction(action.type);
  });
}

function projectedIssue(target: string, code: string, message: string): Issue {
  return { path: target, pointer: target, code, message };
}

/** 固定引用仅按完整字段形状选择；坏值不能改变已选引用模式。 */
function referenceBranch(schema: Record<string, unknown>, value: unknown) {
  const alternatives = schema.oneOf;
  if (!Array.isArray(alternatives) || !isObject(value)) return undefined;
  const keys = Object.keys(value);
  const matching = alternatives.filter((candidate) => {
    if (
      !isObject(candidate) ||
      !isObject(candidate.properties) ||
      !Array.isArray(candidate.required)
    )
      return false;
    const required = candidate.required;
    return (
      keys.length === required.length &&
      keys.every((key) => required.includes(key))
    );
  });
  return matching.length === 1
    ? (matching[0] as Record<string, unknown>)
    : undefined;
}

function validatePresentation(
  schema: Record<string, unknown>,
  value: unknown,
  prefix: string,
): Issue[] {
  const key = JSON.stringify(schema);
  let check = presentationValidators.get(key);
  if (!check) {
    check = protocol.compile({ $defs: planSchema.$defs, ...schema });
    presentationValidators.set(key, check);
  }
  return check(value) ? [] : schemaIssues(check.errors, prefix, prefix);
}

/** 只提取固定公共分支对已有字段的共同合法域，不继承候选的必填或排他条件。 */
function independentProperties(candidates: Record<string, unknown>[]) {
  const definitions = new Map<string, Record<string, unknown>[]>();
  for (const candidate of candidates) {
    if (!isObject(candidate.properties)) continue;
    for (const [key, definition] of Object.entries(candidate.properties)) {
      const schema = containerSchema(definition);
      if (schema)
        definitions.set(key, [...(definitions.get(key) ?? []), schema]);
    }
  }
  return Object.fromEntries(
    [...definitions].map(([key, alternatives]) => {
      const unique = [
        ...new Map(
          alternatives.map((schema) => [JSON.stringify(schema), schema]),
        ).values(),
      ];
      if (unique.length === 1) return [key, unique[0]];
      if (key === "source" || key === "target") {
        const branches = unique.flatMap((schema) =>
          Array.isArray(schema.oneOf)
            ? schema.oneOf.filter(isObject)
            : [schema],
        );
        return [
          key,
          {
            oneOf: [
              ...new Map(
                branches.map((schema) => [JSON.stringify(schema), schema]),
              ).values(),
            ],
          },
        ];
      }
      if (
        unique.every(
          (schema) =>
            Object.hasOwn(schema, "const") || Array.isArray(schema.enum),
        )
      ) {
        const allowed = unique.flatMap((schema) =>
          Array.isArray(schema.enum) ? schema.enum : [schema.const],
        );
        return [
          key,
          {
            enum: [
              ...new Map(
                allowed.map((value) => [JSON.stringify(value), value]),
              ).values(),
            ],
          },
        ];
      }
      // 无法证明字段合法域时，该字段仍由已保留的组合责任和完整原始诊断负责。
      return [key, {}];
    }),
  );
}

/** 分支处理仅限公共内置动作；任意设备 Schema 不经过此投影。 */
function builtinProjection(
  action: Record<string, unknown>,
  index: number,
): PresentedIssue[] | undefined {
  const clause = actionClause(action);
  const schema = clause && containerSchema(clause.then.properties.params);
  if (!schema || isCameraAction(action.type)) return undefined;
  const prefix = `/actions/${index}/params`;
  const value =
    action.params === undefined && !Object.hasOwn(action, "params")
      ? {}
      : action.params;
  const selection: Array<{
    issue: Issue;
    message: string;
    json: boolean;
    responsibility: string;
  }> = [];
  const select = (target: string, message: string, json = false) => {
    selection.push({
      issue: projectedIssue(target, "schema_oneOf", message),
      message,
      json,
      responsibility: `fixed-selection:${action.type}:${target}`,
    });
  };
  let selected = isObject(value) ? schema : { type: "object" };
  let automaticPurpose = false;
  if (Array.isArray(schema.oneOf) && isObject(value)) {
    const candidates = schema.oneOf.filter(isObject);
    if (action.type === "report_status") {
      const chosen = candidates.filter(
        (branch) =>
          isObject(branch.properties) &&
          isObject(branch.properties.scope) &&
          branch.properties.scope.const === value.scope,
      );
      if (chosen.length === 1) selected = chosen[0];
      else {
        select(
          `${prefix}/scope`,
          Object.hasOwn(value, "scope")
            ? "请选择规则允许的报告范围。"
            : "请选择报告范围。",
        );
        selected = {
          type: "object",
          properties: Object.fromEntries(
            candidates.flatMap((branch) =>
              Object.keys(branch.properties as object).map((key) => [key, {}]),
            ),
          ),
          additionalProperties: false,
        };
      }
    } else if (action.type === "delete_action_outputs") {
      const keys = ["source", "output_ids"].filter((key) =>
        Object.hasOwn(value, key),
      );
      if (keys.length === 1)
        selected = candidates.find(
          (branch) =>
            isObject(branch.properties) &&
            Object.hasOwn(branch.properties, keys[0]),
        )!;
      else {
        select(
          keys.length ? prefix : `${prefix}/source`,
          keys.length
            ? "清理来源与产物列表不能同时填写，请通过参数 JSON 选择一种范围。"
            : "请选择清理范围。",
          keys.length > 0,
        );
        selected = {
          type: "object",
          properties: independentProperties(candidates),
          additionalProperties: false,
        };
      }
    } else if (action.type === "obtain_action_outputs") {
      const auto = candidates.find(
        (branch) =>
          Array.isArray(branch.required) && branch.required.includes("purpose"),
      )!;
      const automatic =
        isObject(auto.properties) &&
        isObject(auto.properties.purpose) &&
        value.purpose === auto.properties.purpose.const;
      automaticPurpose = automatic;
      const manualCandidates = candidates.filter((branch) => branch !== auto);
      const manual =
        !Object.hasOwn(value, "purpose") ||
        manualCandidates.some(
          (branch) =>
            isObject(branch.properties) &&
            isObject(branch.properties.purpose) &&
            branch.properties.purpose.const === value.purpose,
        );
      const exact = Object.hasOwn(value, "output_ids");
      if (automatic) {
        // 用途已确定；额外方式冲突不能改变当前 source/filter 的真实契约。
        selected = auto;
        if (exact) {
          select(
            prefix,
            "自动预览用途与精确产物列表不能同时填写，请通过参数 JSON 核对用途和产物列表。",
            true,
          );
          selected = {
            ...auto,
            properties: {
              ...(auto.properties as object),
              // 已填写列表保留公共限制，但不成为自动用途的必填字段。
              output_ids: independentProperties(candidates).output_ids,
            },
          };
        }
      } else if (manual && !(exact && Object.hasOwn(value, "filter")))
        selected = manualCandidates.find(
          (branch) =>
            isObject(branch.properties) &&
            Object.hasOwn(branch.properties, "output_ids") === exact,
        )!;
      else {
        const eligible = manual ? manualCandidates : candidates;
        select(
          prefix,
          manual
            ? "取回筛选与精确产物列表不能同时填写，请通过参数 JSON 选择一种方式。"
            : "取回用途或字段组合不符合规则，请通过参数 JSON 核对用途、来源、筛选和产物列表。",
          true,
        );
        selected = {
          type: "object",
          properties: independentProperties(eligible),
          // 只保留所有当前候选共有的必填责任，不带入专属成员。
          required: Array.isArray(eligible[0].required)
            ? eligible[0].required.filter((key) =>
                eligible.every(
                  (branch) =>
                    Array.isArray(branch.required) &&
                    branch.required.includes(key),
                ),
              )
            : [],
          additionalProperties: false,
        };
      }
    }
  }
  // 只有当前已选参数分支拥有的 source/target 才检查引用形状。
  if (isObject(selected.properties) && isObject(value)) {
    const properties = { ...selected.properties };
    for (const key of ["source", "target"]) {
      const referenceSchema = containerSchema(properties[key]);
      if (!referenceSchema || !Object.hasOwn(value, key)) continue;
      if (!Array.isArray(referenceSchema.oneOf)) {
        if (
          automaticPurpose &&
          key === "source" &&
          isObject(value[key]) &&
          isObject(referenceSchema.properties) &&
          Object.keys(value[key]).some(
            (field) =>
              !Object.hasOwn(referenceSchema.properties as object, field),
          )
        ) {
          select(
            `${prefix}/${key}`,
            "自动预览来源必须使用本计划的动作名称，请通过参数 JSON 修正来源组合。",
            true,
          );
          // 不适用的来源形状由组合负责，已填写的当前名称仍保留叶子约束。
          properties[key] = {
            type: "object",
            properties: referenceSchema.properties,
          };
        }
        continue;
      }
      const branch = referenceBranch(referenceSchema, value[key]);
      if (branch) properties[key] = branch;
      else if (!isObject(value[key])) properties[key] = { type: "object" };
      else {
        const empty = Object.keys(value[key]).length === 0;
        select(
          `${prefix}/${key}`,
          empty
            ? `请选择${key === "target" ? "取消目标" : "来源"}。`
            : "引用字段组合不符合规则，请通过参数 JSON 核对并选择一种来源或目标。",
          !empty,
        );
        properties[key] = { type: "object" };
      }
    }
    selected = { ...selected, properties };
  }
  const errors = validatePresentation(selected, value, prefix);
  return [
    ...errors.map((issue) => {
      const missingReference =
        issue.code === "schema_required" &&
        ["source", "target"].find(
          (key) =>
            issue.pointer === `${prefix}/${key}` &&
            isObject(value) &&
            !Object.hasOwn(value, key),
        );
      return {
        issue,
        ...presentIssue(issue),
        ...(missingReference
          ? {
              message: `请选择${missingReference === "target" ? "取消目标" : action.type === "delete_action_outputs" ? "清理范围" : "来源"}。`,
              responsibility: `fixed-reference-selection:${action.type}:${issue.pointer}`,
            }
          : {}),
        ...(issue.code === "schema_type" &&
        (issue.pointer === prefix ||
          ["source", "target"].some(
            (key) =>
              issue.pointer === `${prefix}/${key}` &&
              isObject(value) &&
              Object.hasOwn(value, key) &&
              !isObject(value[key]),
          ))
          ? { json: true }
          : {}),
      };
    }),
    ...selection.map(({ issue, message, json, responsibility }) => ({
      ...presentIssue(issue),
      issue,
      message,
      json,
      responsibility,
    })),
  ];
}

/** 当前输入的唯一呈现集合；原始机器诊断由调用者完整保留。 */
export function presentIssues(
  issues: readonly Issue[],
  plan?: unknown,
  capabilities?: Capabilities | null,
): PresentedIssue[] {
  const result: PresentedIssue[] = [];
  const handled = new Set<Issue>();
  if (isObject(plan) && Array.isArray(plan.actions))
    plan.actions.forEach((action, index) => {
      if (!isObject(action)) return;
      const prefix = `/actions/${index}`;
      const rawParams = issues.filter(
        (issue) =>
          issue.schema &&
          (issue.pointer === `${prefix}/params` ||
            issue.pointer?.startsWith(`${prefix}/params/`)),
      );
      const projection = rawParams.length
        ? builtinProjection(action, index)
        : undefined;
      if (projection) {
        rawParams.forEach((issue) => handled.add(issue));
        issues
          .filter(
            (issue) =>
              issue.code === "invalid_params" &&
              issue.path === `actions[${index}].params`,
          )
          .forEach((issue) => handled.add(issue));
        result.push(
          ...projection.map((item) => ({
            ...item,
            ...presentIssue(item.issue, plan, capabilities),
            message: item.message,
            json: item.json,
          })),
        );
      }
      // 公共 action 与当前已登记动作契约都声明同一容器必须为对象。
      // 这两处结构断言的同责归并有固定协议依据，不适用于设备独立规则。
      const currentClause = actionClause(action);
      for (const key of ["params", "policy"] as const) {
        if (
          !currentClause ||
          !Object.hasOwn(action, key) ||
          isObject(action[key]) ||
          containerSchema(currentClause.then.properties[key])?.type !== "object"
        )
          continue;
        const clauseIndex =
          planSchema.$defs.action.allOf.indexOf(currentClause);
        const aliases = new Set([
          "#/type",
          `#/properties/${key}/type`,
          `#/allOf/${clauseIndex}/then/properties/${key}/type`,
        ]);
        const structural = issues.filter(
          (issue) =>
            !handled.has(issue) &&
            issue.pointer === `${prefix}/${key}` &&
            issue.code === "schema_type" &&
            issue.schema?.params.type === "object" &&
            aliases.has(issue.schema.schemaPath),
        );
        if (structural.length) {
          structural.forEach((issue) => handled.add(issue));
          result.push({
            issue: structural[0],
            ...presentIssue(structural[0], plan, capabilities),
            json: true,
            responsibility: `fixed-object:${prefix}/${key}`,
          });
        }
      }
      for (const issue of issues) {
        if (!issue.schema || issue.pointer === undefined) continue;
        if (
          issue.code === "schema_required" &&
          issue.pointer === `${prefix}/policy` &&
          !Object.hasOwn(action, "policy")
        ) {
          const clause = actionClause(action);
          const policy =
            clause && containerSchema(clause.then.properties.policy);
          if (policy && Array.isArray(policy.required)) {
            handled.add(issue);
            for (const key of policy.required) {
              const child = { ...issue, pointer: `${prefix}/policy/${key}` };
              result.push({
                issue: child,
                ...presentIssue(child, plan, capabilities),
              });
            }
          }
        }
        if (
          isCameraAction(action.type) &&
          issue.code === "schema_required" &&
          issue.pointer === `${prefix}/params` &&
          !Object.hasOwn(action, "params")
        ) {
          const child = { ...issue, pointer: `${prefix}/params/type` };
          handled.add(issue);
          issues
            .filter(
              (other) =>
                other.code === "params_invalid" &&
                other.path === `actions[${index}].params`,
            )
            .forEach((other) => handled.add(other));
          result.push({
            issue: child,
            ...presentIssue(child, plan, capabilities),
          });
        }
        if (
          issue.code === "schema_if" &&
          issue.schema.params.failingKeyword === "then" &&
          issue.schema.instancePath === prefix
        ) {
          const match = /^#\/allOf\/(\d+)\/if$/.exec(issue.schema.schemaPath);
          const clause =
            match && planSchema.$defs.action.allOf[Number(match[1])];
          if (clause && clause === actionClause(action)) handled.add(issue);
        }
      }
    });
  for (const issue of issues)
    if (!handled.has(issue)) {
      const shown = presentIssue(issue, plan, capabilities);
      // 同一字段已经明确缺失或结构错误时，当前参数资格摘要不新增责任。
      if (
        (issue.code === "params_invalid" ||
          issue.code === "parameter_type_not_supported" ||
          issue.code === "sync_basis_unavailable") &&
        [
          ...result,
          ...issues
            .filter((other) => !handled.has(other) && other.schema)
            .map((other) => ({
              issue: other,
              ...presentIssue(other, plan, capabilities),
            })),
        ].some(
          (other) =>
            other.target === shown.target &&
            ["schema_required", "schema_type"].includes(other.issue.code),
        )
      )
        continue;
      result.push({ issue, ...shown });
    }
  const seen = new Set<string>();
  return result.filter((item) => {
    if (!item.responsibility && !item.issue.schema) return true;
    const key = JSON.stringify([
      item.responsibility ?? item.issue.schema?.schemaPath,
      item.issue.schema?.instancePath,
      item.target,
      item.issue.code,
      item.issue.schema?.params,
      item.message,
      item.json,
    ]);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
