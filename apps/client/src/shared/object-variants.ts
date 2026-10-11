import type {
  DraftContent,
  ObjectVariant,
  ObjectVariantMap,
} from "../server/models";
import { isObject } from "./validation";
import { decodeJsonPointer } from "./json-pointer";
import { inspectActionPending, parseDraft, pointer } from "./draft-plan";
import {
  cloneClientJson,
  exactJsonValueIdentity,
  originalNumberToken,
  parseClientJson,
  rememberNumberToken,
  stringifyJson,
} from "./json";

type Owner = Record<string, unknown>;
type Pending = NonNullable<DraftContent["pending"]>;
function location(params: unknown, path: readonly string[]) {
  if (!path.length || path[0] === "type")
    throw Error("对象分支路径不能是参数身份");
  let parent = params;
  for (const key of path.slice(0, -1)) {
    if (!isObject(parent) || !isObject(parent[key]))
      throw Error("对象分支祖先没有对应的对象");
    parent = parent[key];
  }
  if (!isObject(parent)) throw Error("对象分支没有对应的参数对象");
  return { parent, key: path.at(-1)! };
}
function wrapper(parent: Owner, key: string): Owner {
  const value: Owner = {};
  if (Object.hasOwn(parent, key)) {
    value.value = cloneClientJson(parent[key]);
    rememberNumberToken(
      value,
      "value",
      originalNumberToken(parent, key, parent[key]),
    );
  }
  return value;
}
export function readObjectVariant(variant: ObjectVariant): Owner {
  const value = parseClientJson(variant.valueText);
  if (!isObject(value) || Object.keys(value).some((key) => key !== "value"))
    throw Error("对象分支原文必须是仅含 value 的包装对象");
  return value;
}
function identity(owner: Owner, discriminator: string): string {
  if (!Object.hasOwn(owner, "value")) return "absent";
  const value = owner.value;
  if (!isObject(value))
    return (
      "nonobject:" +
      exactJsonValueIdentity(value, true, { parent: owner, key: "value" })
    );
  return (
    "object:" +
    exactJsonValueIdentity(
      value[discriminator],
      Object.hasOwn(value, discriminator),
      { parent: value, key: discriminator },
    )
  );
}
export function checkObjectVariants(
  value: unknown,
  params?: unknown,
  checkActive = false,
): asserts value is ObjectVariantMap {
  if (!isObject(value)) throw Error("对象分支资料必须是参数相对路径映射");
  const paths: string[][] = [];
  for (const [key, group] of Object.entries(value)) {
    const path = decodeJsonPointer(key);
    if (
      !path.length ||
      path[0] === "type" ||
      paths.some(
        (p) =>
          p.every((part, i) => path[i] === part) ||
          path.every((part, i) => p[i] === part),
      )
    )
      throw Error("对象分支路径无效或重叠");
    paths.push(path);
    if (
      !isObject(group) ||
      typeof group.discriminator !== "string" ||
      !group.discriminator.length ||
      !Array.isArray(group.variants) ||
      Object.keys(group).some((k) => !["discriminator", "variants"].includes(k))
    )
      throw Error("对象分支资料格式不正确");
    const ids = new Set<string>();
    for (const entry of group.variants) {
      if (
        !isObject(entry) ||
        typeof entry.valueText !== "string" ||
        !isObject(entry.pending) ||
        Object.keys(entry).some((k) => !["valueText", "pending"].includes(k))
      )
        throw Error("对象分支内容格式不正确");
      for (const [key, input] of Object.entries(entry.pending)) {
        const p = decodeJsonPointer(key);
        if (
          !p.length ||
          p[0] === group.discriminator ||
          !isObject(input) ||
          !["number", "json"].includes(String(input.kind)) ||
          typeof input.text !== "string"
        )
          throw Error("对象分支未完成输入归属不正确");
      }
      const id = identity(
        readObjectVariant(entry as unknown as ObjectVariant),
        group.discriminator,
      );
      if (ids.has(id)) throw Error("对象分支存在相同身份的多个内容");
      ids.add(id);
    }
    if (checkActive) {
      const { parent, key: field } = location(params, path);
      if (ids.has(identity(wrapper(parent, field), group.discriminator)))
        throw Error("当前对象分支不能同时存在停用权威内容");
    }
  }
}
export function checkObjectVariantLocations(
  content: DraftContent,
  actions: unknown[],
  active = true,
) {
  if (content.objectVariants !== undefined && !isObject(content.objectVariants))
    throw Error("对象分支资料必须是动作位置映射");
  for (const [index, map] of Object.entries(content.objectVariants ?? {})) {
    if (
      !/^(0|[1-9][0-9]*)$/.test(index) ||
      !Number.isSafeInteger(Number(index)) ||
      Number(index) >= actions.length ||
      !isObject(actions[Number(index)])
    )
      throw Error("对象分支资料没有对应的动作位置");
    checkObjectVariants(map, (actions[Number(index)] as Owner).params, active);
  }
}
function inspect(
  content: DraftContent,
  index: number,
  path: readonly string[],
) {
  const root = parseDraft(content),
    action = root.actions[index];
  if (!Number.isSafeInteger(index) || index < 0 || !isObject(action))
    throw Error("对象分支路径没有对应的动作对象");
  checkObjectVariantLocations(content, root.actions);
  const place = location(action.params, path),
    entries = inspectActionPending(content, root.actions);
  const relative = pointer([...path]),
    full = ["actions", String(index), "params", ...path];
  return {
    root,
    action,
    ...place,
    entries,
    relative,
    full,
    map: content.objectVariants?.[String(index)] ?? {},
  };
}
function prefix(a: readonly string[], b: readonly string[]) {
  return a.length <= b.length && a.every((part, i) => b[i] === part);
}
function blocksIdentity(
  path: string[],
  index: number,
  full: string[],
  discriminator: string,
) {
  return (
    prefix(path, full) ||
    prefix([...full, discriminator], path) ||
    (path[0] === "actions" &&
      path[1] === String(index) &&
      (path[2] === "device_id" ||
        path[2] === "type" ||
        (path[2] === "params" && path[3] === "type")))
  );
}
export function canSwitchObjectBranch(
  content: DraftContent,
  index: number,
  path: readonly string[],
  discriminator: string,
): boolean {
  try {
    const state = inspect(content, index, path),
      value = state.parent[state.key],
      group = state.map[state.relative];
    return (
      !!discriminator &&
      (!group || group.discriminator === discriminator) &&
      (!Object.hasOwn(state.parent, state.key) ||
        (isObject(value) &&
          (!Object.hasOwn(value, discriminator) ||
            typeof value[discriminator] === "string"))) &&
      !state.entries.some(({ path: p }) =>
        blocksIdentity(p, index, state.full, discriminator),
      )
    );
  } catch {
    return false;
  }
}
function withMap(content: DraftContent, index: number, map: ObjectVariantMap) {
  const all = { ...content.objectVariants };
  if (Object.keys(map).length) all[String(index)] = map;
  else delete all[String(index)];
  return all;
}
function write(parent: Owner, key: string, replacement: Owner) {
  delete parent[key];
  rememberNumberToken(parent, key);
  if (Object.hasOwn(replacement, "value")) {
    Object.defineProperty(parent, key, {
      value: cloneClientJson(replacement.value),
      writable: true,
      enumerable: true,
      configurable: true,
    });
    rememberNumberToken(
      parent,
      key,
      originalNumberToken(replacement, "value", replacement.value),
    );
  }
}
export function switchObjectBranch(
  content: DraftContent,
  index: number,
  path: readonly string[],
  discriminator: string,
  value: unknown,
  numberToken?: string,
): DraftContent {
  if (!canSwitchObjectBranch(content, index, path, discriminator))
    throw Error("对象分支存在未完成或无法归属的资料，暂不能切换");
  const state = inspect(content, index, path),
    current = wrapper(state.parent, state.key),
    target: Owner = { value: { [discriminator]: cloneClientJson(value) } };
  rememberNumberToken(target.value as Owner, discriminator, numberToken);
  const currentId = identity(current, discriminator),
    targetId = identity(target, discriminator);
  if (currentId === targetId) return content;
  const variants = state.map[state.relative]?.variants ?? [],
    restored = variants.find(
      (v) => identity(readObjectVariant(v), discriminator) === targetId,
    ),
    pending = { ...content.pending },
    relative: Pending = {};
  for (const { key, input, path: p } of state.entries)
    if (prefix(state.full, p)) {
      relative[pointer(p.slice(state.full.length))] = cloneClientJson(input);
      delete pending[key];
    }
  write(
    state.parent,
    state.key,
    restored ? readObjectVariant(restored) : target,
  );
  for (const [key, input] of Object.entries(restored?.pending ?? {}))
    pending[pointer(state.full) + key] = cloneClientJson(input);
  const map = {
    ...state.map,
    [state.relative]: {
      discriminator,
      variants: [
        ...variants.filter(
          (v) => identity(readObjectVariant(v), discriminator) !== targetId,
        ),
        { valueText: stringifyJson(current), pending: relative },
      ],
    },
  };
  return {
    ...content,
    text: stringifyJson(state.root, 2),
    pending,
    objectVariants: withMap(content, index, map),
  };
}
export interface ReplaceObjectOptions {
  omit?: boolean;
  numberToken?: string;
  resetBranches?: boolean;
  /** 首次整体编辑时，由调用者已证明的 Schema 判别字段建立归属。 */
  discriminator?: string;
}
export function replaceObjectValue(
  content: DraftContent,
  index: number,
  path: readonly string[],
  value: unknown,
  options: ReplaceObjectOptions = {},
): DraftContent {
  const state = inspect(content, index, path);
  if (
    state.entries.some(
      ({ path: p }) => p.length < state.full.length && prefix(p, state.full),
    )
  )
    throw Error("祖先未完成输入阻止替换对象");
  const current = wrapper(state.parent, state.key),
    target: Owner = options.omit ? {} : { value: cloneClientJson(value) },
    pending = { ...content.pending },
    relative: Pending = {},
    group =
      state.map[state.relative] ??
      (options.discriminator
        ? { discriminator: options.discriminator, variants: [] }
        : undefined),
    map = { ...state.map };
  rememberNumberToken(target, "value", options.numberToken);
  if (
    options.discriminator &&
    group &&
    group.discriminator !== options.discriminator
  )
    throw Error("对象分支判别字段与保存资料不一致");
  for (const { key, input, path: p } of state.entries)
    if (prefix(state.full, p)) {
      if (
        p.length > state.full.length &&
        (!group || p[state.full.length] !== group.discriminator)
      )
        relative[pointer(p.slice(state.full.length))] = cloneClientJson(input);
      delete pending[key];
    }
  if (options.resetBranches) {
    for (const key of Object.keys(map))
      if (prefix(path, decodeJsonPointer(key))) delete map[key];
  } else if (group) {
    const currentId = identity(current, group.discriminator),
      targetId = identity(target, group.discriminator);
    const variants = group.variants.filter((v) => {
      const id = identity(readObjectVariant(v), group.discriminator);
      return id !== currentId && id !== targetId;
    });
    if (currentId !== targetId)
      variants.push({ valueText: stringifyJson(current), pending: relative });
    map[state.relative] = { ...group, variants };
  }
  for (const key of Object.keys(map)) {
    const descendant = decodeJsonPointer(key);
    if (descendant.length > path.length && prefix(path, descendant))
      delete map[key];
  }
  write(state.parent, state.key, target);
  return {
    ...content,
    text: stringifyJson(state.root, 2),
    pending,
    objectVariants: withMap(content, index, map),
  };
}
