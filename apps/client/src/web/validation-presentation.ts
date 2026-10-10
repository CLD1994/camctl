import type { Capabilities, Issue } from "../shared/types";
import { isObject } from "../shared/validation";
import { pointer, pointerPath, type Path } from "./editing";

export interface IssuePresentation {
  target: string | null;
  location: string;
  message: string;
}

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

/** 仅生成界面说明和可证明的路径，不修改校验事实或草稿。 */
export function presentIssue(
  issue: Issue,
  plan?: unknown,
  capabilities?: Capabilities | null,
): IssuePresentation {
  let parts: Path | null;
  try {
    parts = pathOf(issue.path);
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
  let field =
    key === undefined ? "计划" : (own(names, String(key)) ?? String(key));
  if (parts?.length === 1 && key === "name") field = "计划名称";
  if (parts?.length === 3 && key === "name") field = "动作名称";
  if (parts?.length === 3 && key === "type") field = "动作类型";
  if (parts?.[2] === "params" && parts[3] === "type") field = "参数类型";
  const definition = isObject(properties) ? properties[String(key)] : undefined;
  if (
    parts?.[2] === "params" &&
    parts.length === 4 &&
    isObject(definition) &&
    typeof definition.title === "string"
  )
    field = definition.title;
  if (parts?.length === 2 && index !== undefined) field = "动作内容";
  const actionName =
    isObject(action) && typeof action.name === "string"
      ? `「${action.name}」`
      : "";
  const location =
    index !== undefined
      ? `动作 ${index + 1}${actionName} · ${field}`
      : parts === null
        ? `计划 JSON（${issue.path}）`
        : field;
  if (
    parts &&
    !issue.path.startsWith("/") &&
    ambiguous(parts, plan, properties)
  )
    parts = null;
  const message =
    issue.code === "schema_minItems" && issue.path === "actions"
      ? "至少添加一个动作。"
      : (own(hints, issue.code) ??
        (issue.code.startsWith("schema_") ||
        !/[\u4e00-\u9fff]/.test(issue.message)
          ? "输入不符合规则，请核对填写内容。"
          : issue.message));
  return { target: parts === null ? null : pointer(parts), location, message };
}
