import {
  parseJson,
  parseClientJson,
  cloneClientJson,
  stringifyJson,
  rememberNumberToken,
} from "../shared/json";
import { isObject } from "../shared/validation";
import type { DraftContent, ExportedRequest } from "../server/models";
import type { ReportPlan } from "../shared/types";
import {
  appendContentAction,
  removeContentAction,
  renamePreviewSources,
} from "../shared/automatic-previews";
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
  return requireDraftRoot(parseJson(content.text));
}
function requireDraftRoot(
  value: unknown,
): EditObject & { actions: EditObject[] } {
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
/** JSON Pointer 只解码标准转义；无效路径不猜测对应字段。 */
export function pointerPath(value: string): Path {
  if (value === "") return [];
  if (!value.startsWith("/") || /~(?:[^01]|$)/.test(value))
    throw new Error("未完成输入路径不是有效的 JSON Pointer，请保留原文核对");
  return value
    .slice(1)
    .split("/")
    .map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
}
function prepareActionListChange(content: DraftContent, text?: string) {
  if (Object.hasOwn(content.pending ?? {}, ""))
    throw new Error("整个计划仍有未完成输入，请先修正该祖先输入");
  const root = parseClientJson(content.text);
  if (!isObject(root))
    throw new Error("当前计划正文不是可解释的对象，不能替换动作列表");
  const actions = text === undefined ? undefined : parseClientJson(text);
  if (text !== undefined && !Array.isArray(actions))
    throw new Error("动作列表修正必须是 JSON 数组，原输入已保留");
  return { root, actions };
}
/** UI 先验证资格，再取得确认；此检查不写入任何内容或资料。 */
export function checkActionListChange(
  content: DraftContent,
  text?: string,
): void {
  prepareActionListChange(content, text);
}
function replaceActionList(
  content: DraftContent,
  text: string | undefined,
  confirmed: boolean,
): DraftContent {
  const { root, actions } = prepareActionListChange(content, text);
  if (!confirmed)
    throw new Error(
      "替换或移除整组动作需要确认清除动作身份、自动预览和其他类型编辑资料",
    );
  if (text === undefined) delete root.actions;
  else root.actions = actions;
  const pending = Object.fromEntries(
    Object.entries(content.pending ?? {}).filter(([key]) => {
      try {
        return pointerPath(key)[0] !== "actions";
      } catch {
        return true;
      } // 未知路径保持原文，不扩大清理范围。
    }),
  );
  const {
    automaticPreviews: _preview,
    actionVariants: _variants,
    ...rest
  } = content;
  return { ...rest, text: stringifyJson(root, 2), pending };
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
  if (
    (content.automaticPreviews ||
      Object.keys(content.actionVariants ?? {}).length) &&
    !replaceVariants
  )
    throw new Error(
      "整份计划替换需要确认清除自动预览资料及其他动作类型的编辑内容",
    );
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
/** 数组位置只接受现存规范索引；对象成员始终保留原键。 */
function resolvePath(root: EditObject, path: Path): Path {
  let current: unknown = root;
  return path.map((part, depth) => {
    let key = part;
    if (Array.isArray(current)) {
      if (
        (typeof part === "string" && !/^(0|[1-9][0-9]*)$/.test(part)) ||
        !Number.isSafeInteger(Number(part)) ||
        Number(part) < 0 ||
        Number(part) >= current.length ||
        !Object.hasOwn(current, part)
      )
        throw new Error("数组路径没有对应的现存位置");
      key = Number(part);
      if (
        depth === 1 &&
        depth < path.length - 1 &&
        path[0] === "actions" &&
        !isObject(current[key])
      )
        throw new Error("动作路径没有对应的对象");
    }
    current =
      current !== null && typeof current === "object"
        ? (current as EditObject)[key]
        : undefined;
    return key;
  });
}
export function setValue(
  content: DraftContent,
  path: Path,
  value: unknown,
  omit = false,
  replace = false,
  numberToken?: string,
  confirmActionList = false,
): DraftContent {
  if (path.length === 1 && path[0] === "actions") {
    if (!omit && !Array.isArray(value)) {
      checkActionListChange(content);
      throw new Error("动作列表修正必须是 JSON 数组，原输入已保留");
    }
    return replaceActionList(
      content,
      omit ? undefined : stringifyJson(value),
      confirmActionList,
    );
  }
  if (!omit && !replace && pendingBlocks(content, path))
    throw new Error("此路径存在尚未解决的输入，请先逐项修正或明确省略");
  path = resolvePath(parseDraft(content), path);
  if (
    path.length === 3 &&
    path[0] === "actions" &&
    typeof path[1] === "number" &&
    path[2] === "name"
  )
    content = renamePreviewSources(content, path[1], omit ? undefined : value);
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
  confirmActionList = false,
): DraftContent {
  if (path.length === 1 && path[0] === "actions")
    return replaceActionList(content, text, confirmActionList);
  if (pendingBlocks(content, path))
    throw new Error("父级 JSON 无法表示未完成的子字段，请先修正具体路径");
  const unfinished = (current: DraftContent): DraftContent => ({
    ...current,
    pending: { ...current.pending, [pointer(path)]: { kind, text } },
  });
  let parsed: unknown;
  // 容器不可解析与已经确定的无效路径分开：前者只能保存原文，后者必须报错。
  try {
    parsed = parseJson(content.text);
  } catch {
    return unfinished(content);
  }
  const root = requireDraftRoot(parsed);
  path = resolvePath(root, path);
  let value: unknown;
  try {
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
    if (
      path.length === 3 &&
      path[0] === "actions" &&
      typeof path[1] === "number" &&
      path[2] === "name"
    )
      content = renamePreviewSources(content, path[1], undefined);
    return unfinished(content);
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
  return removeContentAction(content, index);
}
export function appendDraftAction(
  content: DraftContent,
  action: EditObject,
): DraftContent {
  return appendContentAction(content, action);
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
