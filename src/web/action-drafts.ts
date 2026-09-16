import type { DraftContent, ActionVariant } from "../server/models";
import { DRAFT_COMMON_ACTION_FIELDS } from "../server/models";
import { isObject } from "../shared/validation";
import { parseDraft, pointer } from "./editing";
import { sameValue } from "./parameter-options";

const commonFields: readonly string[] = DRAFT_COMMON_ACTION_FIELDS;

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
  if (sameValue(action.type, type)) return content;
  const prefix = pointer(["actions", index]);
  const pending = { ...content.pending };
  const saved: ActionVariant = {
    ...(Object.hasOwn(action, "type") ? { type: action.type } : {}),
    fields: Object.fromEntries(
      Object.entries(action).filter(
        ([key]) => key !== "type" && !commonFields.includes(key),
      ),
    ),
    pending: {},
  };
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
  const variants = content.actionVariants?.[String(index)] ?? [];
  const restored = variants.find((item) => sameValue(item.type, type));
  root.actions[index] = {
    ...Object.fromEntries(
      Object.entries(action).filter(([key]) => commonFields.includes(key)),
    ),
    ...(type === undefined ? {} : { type }),
    ...structuredClone(restored?.fields ?? {}),
  };
  for (const [key, value] of Object.entries(restored?.pending ?? {}))
    pending[prefix + key] = structuredClone(value);
  const actionVariants = { ...content.actionVariants };
  const remaining = [
    ...variants.filter(
      (item) =>
        !sameValue(item.type, type) && !sameValue(item.type, action.type),
    ),
    ...(Object.keys(saved.fields).length || Object.keys(saved.pending).length
      ? [structuredClone(saved)]
      : []),
  ];
  if (remaining.length) actionVariants[index] = remaining;
  else delete actionVariants[index];
  return {
    ...content,
    text: JSON.stringify(root, null, 2),
    pending,
    actionVariants,
  };
}
