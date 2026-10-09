import type { DraftContent, ActionVariant } from "../server/models";
import { DRAFT_COMMON_ACTION_FIELDS } from "../server/models";
import { isObject } from "../shared/validation";
import { parseDraft, pointer } from "./editing";
import {
  parseClientJson,
  stringifyJson,
  cloneClientJson,
  originalNumberToken,
  rememberNumberToken,
  exactJsonIdentity,
} from "../shared/json";

const commonFields: readonly string[] = DRAFT_COMMON_ACTION_FIELDS;

/** 字段搬到新父对象时，同时迁移仍对应当前值的原数字事实。 */
function transferFields(
  target: object,
  source: Record<string, unknown>,
  keys: string[],
) {
  for (const key of keys) {
    Object.defineProperty(target, key, {
      value: cloneClientJson(source[key]),
      enumerable: true,
      configurable: true,
      writable: true,
    });
    rememberNumberToken(
      target,
      key,
      originalNumberToken(source, key, source[key]),
    );
  }
}

/** 类型资料以保存的 JSON 数学事实匹配，不能用 Number 投影选择候选。 */
function typeIdentity(owner: { type?: unknown }): string {
  const evidence = (
    value: unknown,
    parent: object,
    key: string | number,
  ): unknown => {
    const token = originalNumberToken(parent, key, value);
    if (token !== undefined)
      return (JSON as typeof JSON & { rawJSON(text: string): unknown }).rawJSON(
        token,
      );
    if (Array.isArray(value))
      return value.map((item, index) => evidence(item, value, index));
    if (isObject(value))
      return Object.fromEntries(
        Object.entries(value).map(([field, item]) => [
          field,
          evidence(item, value, field),
        ]),
      );
    return value;
  };
  return owner.type === undefined
    ? exactJsonIdentity(undefined, false)
    : exactJsonIdentity(evidence(owner.type, owner, "type"));
}

export function canSwitchActionType(
  content: DraftContent,
  index: number,
): boolean {
  const prefix = pointer(["actions", index]);
  return !Object.keys(content.pending ?? {}).some(
    (key) => key === prefix || prefix.startsWith(key + "/"),
  );
}

export function switchActionType(
  content: DraftContent,
  index: number,
  type: unknown,
): DraftContent {
  const root = parseDraft(content);
  const action = root.actions[index];
  if (!isObject(action)) throw new Error("动作必须是对象");
  if (!canSwitchActionType(content, index))
    throw new Error("整个动作存在未完成输入，暂不能切换类型");
  const currentIdentity = typeIdentity(action),
    targetIdentity = typeIdentity({ type });
  if (currentIdentity === targetIdentity) return content;
  const variants = content.actionVariants?.[String(index)] ?? [];
  const candidates = variants.filter(
    (item) => typeIdentity(item) === targetIdentity,
  );
  if (candidates.length > 1)
    throw Error("动作类型有多个相同事实的停用内容，无法确定恢复对象");
  const restored = candidates[0];
  const prefix = pointer(["actions", index]);
  const pending = { ...content.pending };
  const saved: ActionVariant = {
    fields: {},
    pending: {},
  };
  if (Object.hasOwn(action, "type")) transferFields(saved, action, ["type"]);
  transferFields(
    saved.fields,
    action,
    Object.keys(action).filter(
      (key) => key !== "type" && !commonFields.includes(key),
    ),
  );
  saved.fieldsText = stringifyJson(saved.fields);
  for (const [key, value] of Object.entries(pending)) {
    if (!key.startsWith(prefix + "/")) continue;
    const relative = key.slice(prefix.length);
    if (
      commonFields.some(
        (field) =>
          relative === `/${field}` || relative.startsWith(`/${field}/`),
      )
    )
      continue;
    saved.pending[relative] = value;
    delete pending[key];
  }
  const fields =
    restored?.fieldsText === undefined
      ? cloneClientJson(restored?.fields ?? {})
      : parseClientJson(restored.fieldsText);
  if (!isObject(fields)) throw Error("动作类型字段原文必须是对象");
  const current: Record<string, unknown> =
    type === undefined ? {} : { type: cloneClientJson(type) };
  transferFields(
    current,
    action,
    Object.keys(action).filter((key) => commonFields.includes(key)),
  );
  transferFields(current, fields, Object.keys(fields));
  root.actions[index] = current;
  for (const [key, value] of Object.entries(restored?.pending ?? {}))
    pending[prefix + key] = structuredClone(value);
  const actionVariants = { ...content.actionVariants };
  const remaining = [
    ...variants.filter(
      (item) =>
        typeIdentity(item) !== targetIdentity &&
        typeIdentity(item) !== currentIdentity,
    ),
    ...(Object.keys(saved.fields).length || Object.keys(saved.pending).length
      ? [cloneClientJson(saved)]
      : []),
  ];
  if (remaining.length) actionVariants[index] = remaining;
  else delete actionVariants[index];
  return {
    ...content,
    text: stringifyJson(root, 2),
    pending,
    actionVariants,
  };
}
