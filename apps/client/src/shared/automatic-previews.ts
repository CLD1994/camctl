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
type PreviewPlan = Record<string, unknown> & {
  actions: Record<string, unknown>[];
};
/** 仅解析/结构不满足时不可用；不可用绝不表示空的可靠关联图。 */
function inspectPlan(content: DraftContent): PreviewPlan | undefined {
  let root: unknown;
  try {
    root = parseJson(content.text);
  } catch {
    return undefined;
  }
  return isObject(root) &&
    Array.isArray(root.actions) &&
    root.actions.every(isObject)
    ? (root as PreviewPlan)
    : undefined;
}
function plan(content: DraftContent): PreviewPlan {
  const root = inspectPlan(content);
  if (!root) throw Error("动作列表无法可靠解释");
  return root;
}
function hasPending(content: DraftContent, path: string): boolean {
  return Object.keys(content.pending ?? {}).some(
    (p) => p === path || p.startsWith(path + "/") || path.startsWith(p + "/"),
  );
}
type Applicability = "unneeded" | "supported" | "unknown";
/** 类型、设备和参数只使用各自未被 pending 遮盖的当前事实。 */
function applicability(
  content: DraftContent,
  action: Record<string, unknown>,
  index: number,
  k: CapabilityState,
): Applicability {
  const prefix = `/actions/${index}`;
  if (hasPending(content, prefix + "/type")) return "unknown";
  if (!isCameraAction(action.type))
    return ACTION_TYPES.some((type) => type === action.type)
      ? "unneeded"
      : "unknown";
  if (
    hasPending(content, prefix + "/device_id") ||
    hasPending(content, prefix + "/params") ||
    k.error ||
    !k.active ||
    typeof action.device_id !== "string" ||
    validateParams(action.device_id, action.type, action.params, k.active)
      .length
  )
    return "unknown";
  const parameter = k.active.devices
    .find((d) => d.device_id === action.device_id)!
    .actions.find((a) => a.type === action.type)!
    .parameter_types.find(
      (p) => p.type === (action.params as Record<string, unknown>).type,
    )!;
  return parameter.preview_supported ? "supported" : "unneeded";
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
        (a.sourceId === undefined || typeof a.sourceId === "string") &&
        (a.rename === undefined ||
          (typeof a.sourceId === "string" &&
            isObject(a.rename) &&
            typeof a.rename.sourceId === "string" &&
            typeof a.rename.automaticId === "string" &&
            isName(a.rename.actionName) &&
            typeof a.rename.pending === "boolean")),
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
  if (
    Object.keys(content.pending ?? {}).some((p) => p === "" || p === "/actions")
  )
    return undefined;
  const waiting = new Set(
    metadata.actions.filter((a) => a.rename?.pending).map((a) => a.sourceId),
  );
  for (let i = 0; i < root.actions.length; i++) {
    const a = root.actions[i],
      entry = metadata.actions[i];
    if (!automatic(a)) {
      if (entry.sourceId || entry.rename) return undefined;
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
      !isObject(a.params) ||
      !isObject(a.params.source) ||
      Object.keys(a.params.source).length !== 1 ||
      !isName(a.params.source.action_name) ||
      (entry.rename
        ? entry.rename.sourceId !== entry.sourceId ||
          entry.rename.automaticId !== entry.id ||
          a.params.source.action_name !== entry.rename.actionName ||
          (!entry.rename.pending &&
            a.params.source.action_name !== root.actions[source].name)
        : a.params.source.action_name !== root.actions[source].name) ||
      (!entry.rename?.pending &&
        names.some(
          (n, j) =>
            j !== source &&
            n === root.actions[source].name &&
            (!entry.rename || !waiting.has(metadata.actions[j].id)),
        )) ||
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
  const root = inspectPlan(content);
  if (!root) return { content, issues: ["正文尚不能可靠解释"] };
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
  const links = reliableLinks(content, root);
  if (!links)
    return { content, issues: ["自动取回来源缺失、重复或与资料矛盾"] };
  const next = cloneClientJson(content),
    working = plan(next),
    meta = next.automaticPreviews!,
    removed = new Set<number>(),
    states = new Map<number, Applicability>();
  // 先判定明确不适用项，再用最终保留的名称集合处理所有已证明关联。
  root.actions.forEach((a, i) => {
    if (automatic(a)) return;
    const state = applicability(content, a, i, k);
    states.set(i, state);
    const autoIndex = links.get(metadata.actions[i].id);
    if (state === "unneeded" && autoIndex !== undefined) removed.add(autoIndex);
  });
  const names = root.actions
    .filter((_, i) => !removed.has(i))
    .map((a) => a.name);
  let changed = removed.size > 0;
  for (let i = 0; i < root.actions.length; i++) {
    const a = root.actions[i],
      entry = metadata.actions[i];
    if (automatic(a)) continue;
    const autoIndex = links.get(entry.id),
      state = states.get(i)!,
      prefix = `/actions/${i}`;
    if (state === "unneeded") continue;
    const nameReady =
      !hasPending(content, prefix + "/name") &&
      isName(a.name) &&
      names.filter((n) => n === a.name).length === 1;
    if (
      autoIndex !== undefined &&
      nameReady &&
      meta.actions[autoIndex].rename?.pending
    ) {
      const proof = meta.actions[autoIndex].rename!;
      (
        working.actions[autoIndex].params as {
          source: { action_name: unknown };
        }
      ).source.action_name = a.name;
      proof.actionName = a.name as string;
      proof.pending = false;
      changed = true;
    }
    if (!nameReady) issues.push(`动作 ${a.name} 的名称尚未有效`);
    if (state === "unknown") {
      issues.push(`动作 ${a.name} 的类型、设备、参数或能力尚不能确认`);
      continue;
    }
    if (
      !nameReady ||
      hasPending(content, prefix) ||
      !isTimestamp(a.scheduled_at)
    ) {
      issues.push(`拍摄 ${a.name} 的时间或输入尚未完成`);
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
      ...a,
      ...(a.rename
        ? {
            rename: {
              ...a.rename,
              sourceId: ids.get(a.rename.sourceId) ?? `${namespace}:missing`,
              automaticId:
                ids.get(a.rename.automaticId) ?? `${namespace}:missing`,
            },
          }
        : {}),
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
/** 局部名称编辑先核对保存投影；暂时非法时保留原归属，恢复后原子更新。 */
export function renamePreviewSources(
  content: DraftContent,
  index: number,
  name: unknown,
): DraftContent {
  if (previewIntent(content) !== "enabled") return content;
  const root = inspectPlan(content);
  if (!root) return content;
  const links = reliableLinks(content, root);
  if (!links || !root.actions[index]) return content;
  const next = cloneClientJson(content),
    meta = next.automaticPreviews!;
  // 首次局部编辑只从严格旧图建立依据；已有依据已由 reliableLinks 核实。
  for (const [sourceId, i] of links) {
    const entry = meta.actions[i];
    if (!entry.rename)
      entry.rename = {
        sourceId,
        automaticId: entry.id,
        actionName: (
          root.actions[i].params as { source: { action_name: string } }
        ).source.action_name,
        pending: false,
      };
  }
  // 任一动作的局部名称编辑都可能令已有来源重名；先保存整张旧图的归属。
  const candidateNames = root.actions.map((a, i) =>
    i === index ? name : a.name,
  );
  for (const [sourceId, i] of links) {
    const source = meta.actions.findIndex((entry) => entry.id === sourceId);
    const candidate = candidateNames[source];
    if (
      !isName(candidate) ||
      candidateNames.filter((n) => n === candidate).length !== 1
    )
      meta.actions[i].rename!.pending = true;
  }
  const autoIndex = links.get(meta.actions[index].id);
  if (autoIndex === undefined) return next;
  const proof = meta.actions[autoIndex].rename!;
  if (
    !isName(name) ||
    root.actions.some((a, i) => i !== index && a.name === name)
  ) {
    proof.pending = true;
  } else {
    (
      root.actions[autoIndex].params as { source: { action_name: string } }
    ).source.action_name = name;
    proof.actionName = name;
    proof.pending = false;
  }
  next.text = stringifyJson(root, 2);
  return next;
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
        isObject(a.params.source)
      )
        removed.add(i);
    });
  }
  return removeDraftActions(content, removed);
}
