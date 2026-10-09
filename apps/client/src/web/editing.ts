import {
  parseJson,
  cloneClientJson,
  stringifyJson,
  rememberNumberToken,
} from "../shared/json";
import { isObject } from "../shared/validation";
import type { DraftContent, ExportedRequest } from "../server/models";
import type { ReportPlan } from "../shared/types";
import {
  sources,
  cleanupModes,
  targets,
  type Mode,
} from "../shared/action-params";

export type Path = Array<string | number>;
export type EditObject = Record<string, any>;
export function parseDraft(
  content: DraftContent,
): EditObject & { actions: EditObject[] } {
  const value = parseJson(content.text);
  if (!isObject(value) || !Array.isArray(value.actions))
    throw new Error("计划必须是对象，并包含 actions 数组。请在 JSON 中修正。");
  return value as EditObject & { actions: EditObject[] };
}
export function pointer(path: Path): string {
  if (!path.length) return "";
  return (
    "/" +
    path
      .map((p) => String(p).replace(/~/g, "~0").replace(/\//g, "~1"))
      .join("/")
  );
}
export function pendingBlocks(content: DraftContent, path: Path): boolean {
  const current = pointer(path);
  return Object.keys(content.pending ?? {}).some(
    (key) =>
      key !== current &&
      (key.startsWith(current + "/") || current.startsWith(key + "/")),
  );
}
export function editPlanText(
  content: DraftContent,
  text: string,
  replaceVariants = false,
): DraftContent {
  if (Object.keys(content.pending ?? {}).length)
    throw new Error("请先修正或明确省略未完成输入，再编辑整份 JSON");
  if (Object.keys(content.actionVariants ?? {}).length && !replaceVariants)
    throw new Error("整份计划替换需要确认清除其他动作类型的编辑内容");
  return { text, pending: {} };
}
export function valueAt(value: unknown, path: Path): unknown {
  return path.reduce<unknown>(
    (v, k) =>
      v !== null && typeof v === "object" && Object.hasOwn(v, k)
        ? (v as EditObject)[k]
        : undefined,
    value,
  );
}
export function setValue(
  content: DraftContent,
  path: Path,
  value: unknown,
  omit = false,
  replace = false,
  numberToken?: string,
): DraftContent {
  if (!omit && !replace && pendingBlocks(content, path))
    throw new Error("此路径存在尚未解决的输入，请先逐项修正或明确省略");
  const root = parseDraft(content);
  let target: EditObject = root;
  for (const part of path.slice(0, -1)) {
    if (
      !Object.hasOwn(target, part) ||
      (!isObject(target[part]) && !Array.isArray(target[part]))
    )
      Object.defineProperty(target, part, {
        value: {},
        writable: true,
        enumerable: true,
        configurable: true,
      });
    target = target[part];
  }
  const key = path.at(-1)!;
  if (omit) delete target[key];
  else
    Object.defineProperty(target, key, {
      value: cloneClientJson(value),
      writable: true,
      enumerable: true,
      configurable: true,
    });
  rememberNumberToken(target, key, omit ? undefined : numberToken);
  const p = pointer(path);
  const pending = Object.fromEntries(
    Object.entries(content.pending ?? {}).filter(
      ([key]) => key !== p && !key.startsWith(p + "/"),
    ),
  );
  return { ...content, text: stringifyJson(root, 2), pending };
}
function requireCompleteParams(content: DraftContent, path: Path): void {
  const prefix = pointer(path);
  if (
    Object.keys(content.pending ?? {}).some(
      (k) =>
        k === prefix ||
        k.startsWith(prefix + "/") ||
        prefix.startsWith(k + "/"),
    )
  )
    throw new Error("请先修正或放弃未完成输入，再切换参数模式");
  const params = valueAt(parseDraft(content), path);
  if (params !== undefined && !isObject(params))
    throw new Error("参数原值不是对象，请通过 JSON 修正或明确重新填写");
}
/** 用户明确选择范围后才替换引用；其他字段与编辑资料保持原值。 */
export function changeBuiltinMode(
  content: DraftContent,
  path: Path,
  type: "obtain_action_outputs" | "delete_action_outputs" | "cancel_task",
  id: Mode["id"],
): DraftContent {
  requireCompleteParams(content, path);
  const modes =
    type === "delete_action_outputs"
      ? cleanupModes
      : type === "cancel_task"
        ? targets
        : sources;
  const mode = modes.find((m) => m.id === id);
  if (!mode) throw new Error("此动作不支持该界面模式");
  if (type === "delete_action_outputs" && id === "output_ids") {
    const next = setValue(content, [...path, "source"], undefined, true);
    return setValue(next, [...path, "output_ids"], []);
  }
  let next = setValue(
    content,
    [...path, type === "cancel_task" ? "target" : "source"],
    {
      ...Object.fromEntries(Object.keys(mode.fields).map((key) => [key, ""])),
      ...mode.constants,
    },
  );
  if (
    type === "delete_action_outputs" ||
    (type === "obtain_action_outputs" && id !== "action_instance_id")
  )
    next = setValue(next, [...path, "output_ids"], undefined, true);
  return next;
}
/** 精确列表与显式筛选的互斥转换由用户选择，不在显示参数时执行。 */
export function changeObtainSelection(
  content: DraftContent,
  path: Path,
  selection: "exact" | "default" | "preview" | "implicit",
): DraftContent {
  requireCompleteParams(content, path);
  if (selection === "exact") {
    const next = setValue(content, [...path, "filter"], undefined, true);
    return setValue(next, [...path, "output_ids"], []);
  }
  if (selection === "implicit")
    return setValue(content, [...path, "filter"], undefined, true);
  const next = setValue(content, [...path, "output_ids"], undefined, true);
  return setValue(next, [...path, "filter"], selection);
}
export function editValue(
  content: DraftContent,
  path: Path,
  text: string,
  kind: "number" | "json",
): DraftContent {
  if (pendingBlocks(content, path))
    throw new Error("父级 JSON 无法表示未完成的子字段，请先修正具体路径");
  let value: unknown;
  try {
    const root = parseDraft(content);
    const motor =
      path[0] === "actions" &&
      typeof path[1] === "number" &&
      root.actions[path[1]]?.type === "motor_control";
    const integerPaths =
      motor && (path[2] === "params" || path[2] === "policy")
        ? path.length === 3
          ? [[path[2] === "params" ? "position" : "max_delay_ms"]]
          : path.length === 4 &&
              path[3] === (path[2] === "params" ? "position" : "max_delay_ms")
            ? [[]]
            : []
        : [];
    value = parseJson(text, integerPaths);
    if (
      kind === "number" &&
      (typeof value !== "number" || !Number.isFinite(value))
    )
      throw new Error("数字尚未完成");
  } catch {
    return {
      ...content,
      pending: { ...content.pending, [pointer(path)]: { kind, text } },
    };
  }
  return setValue(
    content,
    path,
    value,
    false,
    false,
    typeof value === "number" ? text.trim() : undefined,
  );
}
export function removeAction(
  content: DraftContent,
  index: number,
): DraftContent {
  const root = parseDraft(content);
  root.actions.splice(index, 1);
  const pending: NonNullable<DraftContent["pending"]> = {};
  for (const [key, value] of Object.entries(content.pending ?? {})) {
    const match = /^\/actions\/(\d+)(\/.*)?$/.exec(key);
    if (!match) {
      pending[key] = value;
      continue;
    }
    const n = Number(match[1]);
    if (n === index) continue;
    pending[`/actions/${n > index ? n - 1 : n}${match[2] ?? ""}`] = value;
  }
  const actionVariants = Object.fromEntries(
    Object.entries(content.actionVariants ?? {})
      .filter(([key]) => Number(key) !== index)
      .map(([key, value]) => [
        String(Number(key) > index ? Number(key) - 1 : Number(key)),
        value,
      ]),
  );
  return {
    ...content,
    text: stringifyJson(root, 2),
    pending,
    ...(content.actionVariants ? { actionVariants } : {}),
  };
}
export function appendDraftAction(
  content: DraftContent,
  action: EditObject,
): DraftContent {
  const root = parseDraft(content);
  root.actions.push(cloneClientJson(action));
  return { ...content, text: stringifyJson(root, 2) };
}
export function resolveField(
  field: unknown,
  root: Record<string, unknown>,
  seen = new Set<string>(),
): Record<string, unknown> {
  if (!isObject(field)) return {};
  const { $ref, ...own } = field;
  if (typeof $ref !== "string") return own;
  if (!$ref.startsWith("#/") || seen.has($ref)) return own;
  const target = $ref
    .slice(2)
    .split("/")
    .reduce<unknown>(
      (value, key) =>
        isObject(value)
          ? value[key.replace(/~1/g, "/").replace(/~0/g, "~")]
          : undefined,
      root,
    );
  return { ...resolveField(target, root, new Set([...seen, $ref])), ...own };
}
export function localToUtc(input: string): string | undefined {
  if (!input) return undefined;
  const date = new Date(input);
  if (!Number.isFinite(date.getTime())) throw new Error("执行时间无效");
  return date
    .toISOString()
    .replace("T", " ")
    .replace(/\.000Z$/, "")
    .replace(/Z$/, "");
}
export function utcToLocal(input: unknown): string {
  if (typeof input !== "string") return "";
  const date = new Date(input.replace(" ", "T") + "Z");
  if (!Number.isFinite(date.getTime())) return "";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}T${p(date.getHours())}:${p(date.getMinutes())}:${p(date.getSeconds())}`;
}
export interface PlanRecord {
  id: string;
  request?: ExportedRequest;
  plan?: ReportPlan;
}
export function recordsFor(
  requests: ExportedRequest[],
  plans: ReportPlan[],
): PlanRecord[] {
  const records = new Map<string, PlanRecord>(
    requests.map((request) => [request.id, { id: request.id, request }]),
  );
  for (const plan of plans)
    records.set(plan.request_id, {
      ...records.get(plan.request_id),
      id: plan.request_id,
      plan,
    });
  return [...records.values()];
}
