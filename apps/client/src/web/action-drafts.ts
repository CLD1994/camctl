import type { DraftContent, ActionVariant } from "../server/models";
import { DRAFT_COMMON_ACTION_FIELDS } from "../server/models";
import { isObject } from "../shared/validation";
import {
  checkParameterVariants,
  checkParameterVariantLocations,
  checkActiveParameterVariant,
  replaceParameterValue,
} from "../shared/parameter-variants";
import {
  parseDraft,
  pointer,
  inspectActionPending,
} from "../shared/draft-plan";
import { decodeJsonPointer } from "../shared/json-pointer";
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
  try {
    const root = parseDraft(content);
    if (
      !Number.isSafeInteger(index) ||
      index < 0 ||
      !isObject(root.actions[index])
    )
      return false;
    return !inspectActionPending(content, root.actions).some(
      ({ path, actionIndex }) =>
        !path.length ||
        (path[0] === "actions" && path.length === 1) ||
        (actionIndex === index && path.length === 2),
    );
  } catch {
    return false;
  }
}

export function switchActionType(
  content: DraftContent,
  index: number,
  type: unknown,
  numberToken?: string,
): DraftContent {
  return switchActionBranch(content, index, type, numberToken);
}
function switchActionBranch(
  content: DraftContent,
  index: number,
  type: unknown,
  numberToken?: string,
  explicitTarget = false,
): DraftContent {
  const root = parseDraft(content);
  const action = root.actions[index];
  if (!isObject(action)) throw new Error("动作必须是对象");
  const inputs = inspectActionPending(content, root.actions);
  if (!canSwitchActionType(content, index))
    throw new Error("整个动作存在未完成输入，暂不能切换类型");
  const currentIdentity = typeIdentity(action),
    target = { type: cloneClientJson(type) };
  rememberNumberToken(target, "type", numberToken);
  const targetIdentity = typeIdentity(target);
  if (currentIdentity === targetIdentity) return content;
  const variants = content.actionVariants?.[String(index)] ?? [];
  const parameters = content.parameterVariants?.[String(index)] ?? [];
  checkParameterVariantLocations(content, root.actions);
  checkParameterVariants(parameters);
  checkActiveParameterVariant(parameters, action);
  if (variants.some((variant) => typeIdentity(variant) === currentIdentity))
    throw Error("当前动作类型不能同时存在停用权威内容");
  for (const variant of variants)
    if (variant.parameterVariants !== undefined) {
      checkParameterVariants(variant.parameterVariants);
      const fields =
        variant.fieldsText === undefined
          ? variant.fields
          : parseClientJson(variant.fieldsText);
      if (!isObject(fields)) throw Error("动作类型字段原文必须是对象");
      if (!explicitTarget || typeIdentity(variant) !== targetIdentity)
        checkActiveParameterVariant(variant.parameterVariants, fields);
    }
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
  if (parameters.length) saved.parameterVariants = cloneClientJson(parameters);
  if (Object.hasOwn(action, "type")) transferFields(saved, action, ["type"]);
  transferFields(
    saved.fields,
    action,
    Object.keys(action).filter(
      (key) => key !== "type" && !commonFields.includes(key),
    ),
  );
  saved.fieldsText = stringifyJson(saved.fields);
  for (const { key, input, path, actionIndex } of inputs) {
    if (
      actionIndex !== index ||
      path.length <= 2 ||
      commonFields.includes(path[2])
    )
      continue;
    saved.pending[pointer(path.slice(2))] = cloneClientJson(input);
    delete pending[key];
  }
  const fields =
    restored?.fieldsText === undefined
      ? cloneClientJson(restored?.fields ?? {})
      : parseClientJson(restored.fieldsText);
  if (!isObject(fields)) throw Error("动作类型字段原文必须是对象");
  const current: Record<string, unknown> =
    type === undefined ? {} : { type: cloneClientJson(type) };
  rememberNumberToken(current, "type", numberToken);
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
    ...(Object.keys(saved.fields).length ||
    Object.keys(saved.pending).length ||
    saved.parameterVariants?.length
      ? [cloneClientJson(saved)]
      : []),
  ];
  if (remaining.length) actionVariants[index] = remaining;
  else delete actionVariants[index];
  const parameterVariants = { ...content.parameterVariants };
  if (restored?.parameterVariants?.length)
    parameterVariants[index] = cloneClientJson(restored.parameterVariants);
  else delete parameterVariants[index];
  return {
    ...content,
    text: stringifyJson(root, 2),
    pending,
    actionVariants,
    parameterVariants,
  };
}

/** 单动作全文采用提供的内容；分支资料归属仍由外层与参数类型转换保持。 */
export function replaceActionValue(
  content: DraftContent,
  index: number,
  value: unknown,
  omit = false,
  numberToken?: string,
): DraftContent {
  const root = parseDraft(content),
    old = root.actions[index];
  if (!Number.isSafeInteger(index) || index < 0 || index >= root.actions.length)
    throw Error("动作路径没有对应的位置");
  const path = ["actions", String(index)];
  const entries = Object.entries(content.pending ?? {}).map(([key, input]) => ({
    key,
    input,
    path: decodeJsonPointer(key),
  }));
  if (
    entries.some(
      ({ path: p }) => p.length < 2 && p.every((part, i) => part === path[i]),
    )
  )
    throw Error("祖先未完成输入阻止替换动作");
  const pending = Object.fromEntries(
    entries
      .filter(
        ({ path: p }) =>
          !(p.length === 2 && p.every((part, i) => part === path[i])),
      )
      .map(({ key, input }) => [key, input]),
  );
  let next: DraftContent = { ...content, pending };
  if (!isObject(old)) {
    if (content.parameterVariants?.[String(index)]?.length)
      throw Error("非对象动作的参数资料无法确定归属");
    if (isObject(value) && !omit) {
      const candidates = (content.actionVariants?.[String(index)] ?? []).filter(
        (v) => typeIdentity(v) === typeIdentity(value),
      );
      if (candidates.length > 1) throw Error("动作类型停用内容不唯一");
      const restored = candidates[0];
      const parameters = restored?.parameterVariants ?? [];
      checkParameterVariants(parameters);
      const fields =
        restored?.fieldsText === undefined
          ? cloneClientJson(restored?.fields ?? {})
          : parseClientJson(restored.fieldsText);
      if (!isObject(fields)) throw Error("动作类型字段原文必须是对象");
      const baseline: Record<string, unknown> = {};
      if (Object.hasOwn(value, "type"))
        transferFields(baseline, value, ["type"]);
      transferFields(baseline, fields, Object.keys(fields));
      const actionVariants = { ...content.actionVariants };
      if (actionVariants[index])
        actionVariants[index] = actionVariants[index].filter(
          (v) => typeIdentity(v) !== typeIdentity(value),
        );
      root.actions[index] = baseline;
      const restoredPending = Object.fromEntries(
        entries
          .filter(
            ({ path: p }) => !(p[0] === "actions" && p[1] === String(index)),
          )
          .map(({ key, input }) => [key, input]),
      );
      for (const [key, input] of Object.entries(restored?.pending ?? {}))
        restoredPending[`/actions/${index}${key}`] = cloneClientJson(input);
      next = {
        ...next,
        text: stringifyJson(root, 2),
        pending: restoredPending,
        actionVariants,
        parameterVariants: {
          ...next.parameterVariants,
          [index]: cloneClientJson(parameters),
        },
      };
      next = replaceParameterValue(
        next,
        index,
        value.params,
        !Object.hasOwn(value, "params"),
        originalNumberToken(value, "params", value.params),
      );
    }
  } else if (isObject(value) && !omit) {
    // 原动作自己的原文是输入；成员原文随离开分支由已有转换保存。
    next = switchActionBranch(
      next,
      index,
      value.type,
      originalNumberToken(value, "type", value.type),
      true,
    );
    const current = parseDraft(next).actions[index];
    if (isObject(current))
      next = replaceParameterValue(
        next,
        index,
        value.params,
        !Object.hasOwn(value, "params"),
        originalNumberToken(value, "params", value.params),
      );
  } else {
    const variants = next.actionVariants?.[String(index)] ?? [];
    for (const variant of variants)
      if (variant.parameterVariants !== undefined)
        checkParameterVariants(variant.parameterVariants);
    const parameters = next.parameterVariants?.[String(index)] ?? [];
    checkParameterVariants(parameters);
    checkActiveParameterVariant(parameters, old);
    if (variants.filter((v) => typeIdentity(v) === typeIdentity(old)).length)
      throw Error("当前动作分支有重复权威资料");
    const fields: Record<string, unknown> = {};
    transferFields(
      fields,
      old,
      Object.keys(old).filter(
        (key) => key !== "type" && !commonFields.includes(key),
      ),
    );
    const saved: ActionVariant = {
      fields,
      fieldsText: stringifyJson(fields),
      pending: {},
    };
    if (Object.hasOwn(old, "type")) transferFields(saved, old, ["type"]);
    if (parameters.length)
      saved.parameterVariants = cloneClientJson(parameters);
    for (const { key, input, path: p } of entries)
      if (
        p.length > 2 &&
        p[0] === "actions" &&
        p[1] === String(index) &&
        !commonFields.includes(p[2])
      )
        saved.pending[pointer(p.slice(2))] = cloneClientJson(input);
    next = {
      ...next,
      actionVariants: { ...next.actionVariants, [index]: [...variants, saved] },
    };
    const parameterVariants = { ...next.parameterVariants };
    delete parameterVariants[index];
    next.parameterVariants = parameterVariants;
  }
  const updated = parseDraft(next);
  if (!omit && isObject(value) && next.actionVariants?.[String(index)]) {
    const actionVariants = { ...next.actionVariants };
    actionVariants[index] = actionVariants[index].filter(
      (variant) => typeIdentity(variant) !== typeIdentity(value),
    );
    if (!actionVariants[index].length) delete actionVariants[index];
    next = { ...next, actionVariants };
  }
  if (omit) delete updated.actions[index];
  else {
    Object.defineProperty(updated.actions, index, {
      value: cloneClientJson(value),
      writable: true,
      enumerable: true,
      configurable: true,
    });
    rememberNumberToken(updated.actions, index, numberToken);
  }
  const finalPending = Object.fromEntries(
    Object.entries(next.pending ?? {}).filter(([key]) => {
      const p = decodeJsonPointer(key);
      return !(p[0] === "actions" && p[1] === String(index));
    }),
  );
  return { ...next, text: stringifyJson(updated, 2), pending: finalPending };
}
