import { parseJson } from "../shared/json";
import { isObject } from "../shared/validation";
import type { DraftContent, ExportedRequest } from "../server/models";
import type { ReportPlan } from "../shared/types";

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
      value: structuredClone(value),
      writable: true,
      enumerable: true,
      configurable: true,
    });
  const p = pointer(path);
  const pending = Object.fromEntries(
    Object.entries(content.pending ?? {}).filter(
      ([key]) => key !== p && !key.startsWith(p + "/"),
    ),
  );
  return { ...content, text: JSON.stringify(root, null, 2), pending };
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
    value = parseJson(text);
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
  return setValue(content, path, value);
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
    text: JSON.stringify(root, null, 2),
    pending,
    ...(content.actionVariants ? { actionVariants } : {}),
  };
}
export function appendDraftAction(
  content: DraftContent,
  action: EditObject,
): DraftContent {
  const root = parseDraft(content);
  root.actions.push(structuredClone(action));
  return { ...content, text: JSON.stringify(root, null, 2) };
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
