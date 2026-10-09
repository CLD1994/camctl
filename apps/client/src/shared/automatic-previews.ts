import type {
  DraftContent,
  PreviewIntent,
  PreviewMetadata,
} from "../server/models";
import type { Capabilities } from "./types";
import { cloneClientJson, parseJson, stringifyJson } from "./json";
import { isObject, isName, isTimestamp } from "./validation";
import { isCameraAction, ACTION_TYPES } from "./actions";
import { validateParams } from "./capabilities";

export interface CapabilityState {
  active: Capabilities | null;
  error: string | null;
  generation: number;
  version?: string;
}
export const sameContent = (a: DraftContent, b: DraftContent) =>
  a.text === b.text &&
  stringifyJson(a.pending ?? {}) === stringifyJson(b.pending ?? {}) &&
  stringifyJson(a.actionVariants ?? {}) ===
    stringifyJson(b.actionVariants ?? {}) &&
  stringifyJson(a.automaticPreviews ?? null) ===
    stringifyJson(b.automaticPreviews ?? null);
export const previewIntent = (c: DraftContent): PreviewIntent =>
  c.automaticPreviews?.intent ?? "unset";
const automatic = (a: unknown) =>
  isObject(a) &&
  a.type === "obtain_action_outputs" &&
  isObject(a.params) &&
  a.params.purpose === "auto_preview";
function plan(content: DraftContent) {
  const root = parseJson(content.text);
  if (
    !isObject(root) ||
    !Array.isArray(root.actions) ||
    !root.actions.every(isObject)
  )
    throw Error("动作列表无法可靠解释");
  return root as Record<string, unknown> & {
    actions: Record<string, unknown>[];
  };
}
export function validPreviewMetadata(value: unknown): value is PreviewMetadata {
  return (
    isObject(value) &&
    ["enabled", "disabled", "unset"].includes(String(value.intent)) &&
    typeof value.namespace === "string" &&
    value.namespace.length > 0 &&
    Number.isSafeInteger(value.next) &&
    Number(value.next) >= 0 &&
    Array.isArray(value.actions) &&
    value.actions.every(
      (a) =>
        isObject(a) &&
        typeof a.id === "string" &&
        a.id.length > 0 &&
        (a.sourceId === undefined || typeof a.sourceId === "string"),
    ) &&
    new Set(value.actions.map((a) => a.id)).size === value.actions.length
  );
}
function reliableLinks(
  content: DraftContent,
  root: ReturnType<typeof plan>,
): Map<string, number> | undefined {
  const metadata = content.automaticPreviews;
  if (
    !metadata ||
    !validPreviewMetadata(metadata) ||
    metadata.actions.length !== root.actions.length
  )
    return undefined;
  const names = root.actions.map((a) => a.name),
    byId = new Map(metadata.actions.map((entry, i) => [entry.id, i])),
    links = new Map<string, number>();
  for (let i = 0; i < root.actions.length; i++) {
    const a = root.actions[i],
      entry = metadata.actions[i];
    if (!automatic(a)) {
      if (entry.sourceId) return undefined;
      continue;
    }
    const prefix = "/actions/" + i;
    if (
      Object.keys(content.pending ?? {}).some(
        (p) => p === prefix || p.startsWith(prefix + "/"),
      )
    )
      return undefined;
    const source = entry.sourceId ? byId.get(entry.sourceId) : undefined;
    if (
      source === undefined ||
      names.filter((n) => n === root.actions[source].name).length !== 1 ||
      !isObject(a.params) ||
      !isObject(a.params.source) ||
      Object.keys(a.params.source).length !== 1 ||
      a.params.source.action_name !== root.actions[source].name ||
      links.has(entry.sourceId!)
    )
      return undefined;
    links.set(entry.sourceId!, i);
  }
  return links;
}
/** 身份只在明确创建、复制或选择开关时分配；读取旧资料保持缺省。 */
export function initializePreviewMetadata(
  content: DraftContent,
  intent: PreviewIntent,
  namespace: string,
): DraftContent {
  let actions: Record<string, unknown>[];
  try {
    actions = plan(content).actions;
  } catch {
    return {
      ...content,
      automaticPreviews: { intent, namespace, next: 0, actions: [] },
    };
  }
  const entries: PreviewMetadata["actions"] = actions.map((_, i) => ({
    id: `${namespace}:${i}`,
  }));
  const names = new Map<unknown, number[]>();
  actions.forEach((a, i) =>
    names.set(a.name, [...(names.get(a.name) ?? []), i]),
  );
  actions.forEach((a, i) => {
    if (automatic(a) && isObject(a.params) && isObject(a.params.source)) {
      const matches = names.get(a.params.source.action_name);
      if (matches?.length === 1 && isCameraAction(actions[matches[0]].type))
        entries[i].sourceId = entries[matches[0]].id;
    }
  });
  return {
    ...content,
    automaticPreviews: {
      intent,
      namespace,
      next: actions.length,
      actions: entries,
    },
  };
}
/** 删除使用同一位置映射搬移全部编辑资料。 */
export function removeDraftActions(
  content: DraftContent,
  removed: ReadonlySet<number>,
): DraftContent {
  const root = plan(content),
    map = new Map<number, number>();
  const kept: Record<string, unknown>[] = [];
  root.actions.forEach((a, i) => {
    if (!removed.has(i)) {
      map.set(i, kept.length);
      kept.push(a);
    }
  });
  const pending: NonNullable<DraftContent["pending"]> = {};
  for (const [key, value] of Object.entries(content.pending ?? {})) {
    const match = /^\/actions\/(\d+)(\/.*)?$/.exec(key);
    if (!match) pending[key] = value;
    else if (map.has(Number(match[1])))
      pending[`/actions/${map.get(Number(match[1]))}${match[2] ?? ""}`] = value;
  }
  root.actions = kept;
  return {
    ...content,
    text: stringifyJson(root, 2),
    ...(content.pending ? { pending } : {}),
    ...(content.actionVariants
      ? {
          actionVariants: Object.fromEntries(
            Object.entries(content.actionVariants)
              .filter(([i]) => map.has(Number(i)))
              .map(([i, v]) => [String(map.get(Number(i))), v]),
          ),
        }
      : {}),
    ...(content.automaticPreviews
      ? {
          automaticPreviews: {
            ...content.automaticPreviews,
            actions: content.automaticPreviews.actions.filter(
              (_, i) => !removed.has(i),
            ),
          },
        }
      : {}),
  };
}
export function coordinatePreviews(
  content: DraftContent,
  k: CapabilityState,
): { content: DraftContent; issues: string[] } {
  const issues: string[] = [];
  if (previewIntent(content) === "unset") return { content, issues };
  let root: ReturnType<typeof plan>;
  try {
    root = plan(content);
  } catch {
    return { content, issues: ["正文尚不能可靠解释"] };
  }
  const metadata = content.automaticPreviews!;
  if (
    !validPreviewMetadata(metadata) ||
    metadata.actions.length !== root.actions.length
  )
    return { content, issues: ["动作身份与正文不对应"] };
  if (
    Object.keys(content.pending ?? {}).some((p) => p === "" || p === "/actions")
  )
    return { content, issues: ["动作列表仍有未完成输入"] };
  if (
    root.actions.some(
      (a, i) =>
        automatic(a) &&
        Object.keys(content.pending ?? {}).some(
          (p) => p === `/actions/${i}` || p.startsWith(`/actions/${i}/`),
        ),
    )
  )
    return { content, issues: ["自动取回仍有未完成输入，关联待核实"] };
  if (metadata.intent === "disabled") {
    const removed = new Set(
      root.actions.flatMap((a, i) => (automatic(a) ? [i] : [])),
    );
    return {
      content: removed.size ? removeDraftActions(content, removed) : content,
      issues,
    };
  }
  const names = root.actions.map((a) => a.name);

  const links = reliableLinks(content, root);
  if (!links)
    return { content, issues: ["自动取回来源缺失、重复或与资料矛盾"] };
  if (k.error || !k.active)
    return {
      content,
      issues: root.actions.some((a) => isCameraAction(a.type) || automatic(a))
        ? ["能力说明不可可靠取得"]
        : [],
    };
  const next = cloneClientJson(content),
    working = plan(next),
    meta = next.automaticPreviews!,
    removed = new Set<number>();
  let changed = false;
  for (let i = 0; i < root.actions.length; i++) {
    const a = root.actions[i],
      entry = metadata.actions[i];
    if (automatic(a)) continue;
    const autoIndex = links.get(entry.id);
    const prefix = `/actions/${i}`;
    if (
      Object.keys(content.pending ?? {}).some(
        (p) =>
          p === prefix ||
          p.startsWith(prefix + "/") ||
          prefix.startsWith(p + "/"),
      )
    ) {
      issues.push(`动作 ${a.name} 尚有未完成输入`);
      continue;
    }
    if (!isCameraAction(a.type)) {
      if (
        autoIndex !== undefined &&
        ACTION_TYPES.some((type) => type === a.type)
      ) {
        removed.add(autoIndex);
        changed = true;
      }
      continue;
    }
    if (
      typeof a.device_id !== "string" ||
      validateParams(a.device_id, a.type, a.params, k.active).length
    ) {
      issues.push(`拍摄 ${a.name} 的设备或参数尚未有效`);
      continue;
    }
    if (
      !isTimestamp(a.scheduled_at) ||
      !isName(a.name) ||
      names.filter((n) => n === a.name).length !== 1
    ) {
      issues.push(`拍摄 ${a.name} 的时间或名称尚未有效`);
      continue;
    }
    const parameter = k.active.devices
      .find((d) => d.device_id === a.device_id)!
      .actions.find((x) => x.type === a.type)!
      .parameter_types.find(
        (p) => p.type === (a.params as Record<string, unknown>).type,
      )!;
    if (!parameter.preview_supported) {
      if (autoIndex !== undefined) {
        removed.add(autoIndex);
        changed = true;
      }
      continue;
    }
    if (autoIndex !== undefined) {
      const auto = working.actions[autoIndex];
      if (auto.scheduled_at !== a.scheduled_at) {
        auto.scheduled_at = a.scheduled_at;
        changed = true;
      }
      continue;
    }
    let suffix = 1,
      name = "";
    do {
      const tail = suffix === 1 ? "预览" : `预览 ${suffix}`;
      name =
        [...String(a.name)].slice(0, 128 - [...tail].length).join("") + tail;
      suffix++;
    } while (names.includes(name));
    names.push(name);
    working.actions.push({
      name,
      type: "obtain_action_outputs",
      scheduled_at: a.scheduled_at,
      params: {
        source: { action_name: a.name },
        filter: "preview",
        purpose: "auto_preview",
      },
    });
    meta.actions.push({ id: `${entry.id}:preview`, sourceId: entry.id });
    changed = true;
  }
  if (!changed) return { content, issues };
  next.text = stringifyJson(working, 2);
  return {
    content: removed.size ? removeDraftActions(next, removed) : next,
    issues,
  };
}
export function setPreviewIntent(
  content: DraftContent,
  intent: PreviewIntent,
  namespace: string,
  k: CapabilityState,
) {
  const next = content.automaticPreviews
    ? {
        ...content,
        automaticPreviews: { ...content.automaticPreviews, intent },
      }
    : initializePreviewMetadata(content, intent, namespace);
  return coordinatePreviews(next, k);
}
export function copyDraftContent(
  content: DraftContent,
  namespace: string,
): DraftContent {
  const next = cloneClientJson(content),
    old = next.automaticPreviews;
  if (!old) return next;
  const ids = new Map(old.actions.map((a, i) => [a.id, `${namespace}:${i}`]));
  next.automaticPreviews = {
    ...old,
    namespace,
    next: old.actions.length,
    actions: old.actions.map((a) => ({
      id: ids.get(a.id)!,
      ...(a.sourceId
        ? { sourceId: ids.get(a.sourceId) ?? `${namespace}:missing` }
        : {}),
    })),
  };
  return next;
}

export function appendContentAction(
  content: DraftContent,
  action: Record<string, unknown>,
  uniqueName = false,
): DraftContent {
  const root = plan(content),
    copy = cloneClientJson(action),
    next = cloneClientJson(content),
    meta = next.automaticPreviews;
  if (uniqueName) {
    const names = new Set(root.actions.map((a) => a.name));
    const base = String(copy.name ?? "后续动作");
    let name = base,
      n = 2;
    while (names.has(name)) name = `${base} ${n++}`;
    copy.name = name;
  }
  root.actions.push(copy);
  if (meta) {
    if (meta.actions.length !== root.actions.length - 1)
      throw Error("动作身份与正文不对应");
    meta.actions.push({ id: `${meta.namespace}:${meta.next++}` });
  }
  next.text = stringifyJson(root, 2);
  return next;
}
export function copyDraftAction(
  content: DraftContent,
  index: number,
): DraftContent {
  const root = plan(content),
    action = root.actions[index];
  if (!action || automatic(action)) throw Error("请选择普通动作复制");
  const next = appendContentAction(content, action, true),
    newIndex = root.actions.length;
  const prefix = `/actions/${index}`;
  if (content.actionVariants?.[String(index)])
    next.actionVariants = {
      ...next.actionVariants,
      [String(newIndex)]: cloneClientJson(
        content.actionVariants[String(index)],
      ),
    };
  for (const [key, value] of Object.entries(content.pending ?? {}))
    if (key === prefix || key.startsWith(prefix + "/"))
      next.pending = {
        ...next.pending,
        [`/actions/${newIndex}${key.slice(prefix.length)}`]:
          cloneClientJson(value),
      };
  return next;
}
/** 显式局部改名使用原关联核实后更新引用，不按新名称猜配。 */
export function renamePreviewSources(
  content: DraftContent,
  index: number,
  name: unknown,
): DraftContent {
  if (previewIntent(content) !== "enabled" || typeof name !== "string")
    return content;
  const root = plan(content),
    meta = content.automaticPreviews!;
  if (!reliableLinks(content, root) || !root.actions[index]) return content;
  const id = meta.actions[index].id,
    oldName = root.actions[index].name;
  meta.actions.forEach((entry, i) => {
    const a = root.actions[i];
    if (
      entry.sourceId === id &&
      automatic(a) &&
      isObject(a.params) &&
      isObject(a.params.source) &&
      a.params.source.action_name === oldName
    )
      a.params.source.action_name = name;
  });
  return { ...content, text: stringifyJson(root, 2) };
}
export function removeContentAction(
  content: DraftContent,
  index: number,
): DraftContent {
  const removed = new Set([index]),
    root = plan(content),
    meta = content.automaticPreviews;
  if (
    previewIntent(content) === "enabled" &&
    meta &&
    reliableLinks(content, root)
  ) {
    const id = meta.actions[index]?.id;
    meta.actions.forEach((entry, i) => {
      const a = root.actions[i];
      if (
        entry.sourceId === id &&
        automatic(a) &&
        isObject(a.params) &&
        isObject(a.params.source) &&
        a.params.source.action_name === root.actions[index]?.name
      )
        removed.add(i);
    });
  }
  return removeDraftActions(content, removed);
}
