import type { DraftContent, ParameterVariant } from "../server/models";
import { isObject } from "./validation";
import { decodeJsonPointer } from "./json-pointer";
import { inspectActionPending } from "./draft-plan";
import {
  cloneClientJson,
  exactJsonValueIdentity,
  originalNumberToken,
  parseClientJson,
  parseJson,
  rememberNumberToken,
  stringifyJson,
} from "./json";

type Pending = NonNullable<DraftContent["pending"]>;
type ParamsOwner = Record<string, unknown>;

/** 搬移标量时必须同时搬移原父容器拥有的数字词元。 */
function transferParams(target: ParamsOwner, source: ParamsOwner) {
  if (!Object.hasOwn(source, "params")) return;
  Object.defineProperty(target, "params", {
    value: cloneClientJson(source.params),
    writable: true,
    configurable: true,
    enumerable: true,
  });
  rememberNumberToken(
    target,
    "params",
    originalNumberToken(source, "params", source.params),
  );
}

function identity(owner: ParamsOwner): string {
  const params = owner.params;
  return isObject(params)
    ? exactJsonValueIdentity(params.type, Object.hasOwn(params, "type"), {
        parent: params,
        key: "type",
      })
    : exactJsonValueIdentity(undefined, false);
}

export function readParameterVariant(variant: ParameterVariant): ParamsOwner {
  const owner = parseClientJson(variant.paramsText);
  if (!isObject(owner) || Object.keys(owner).some((key) => key !== "params"))
    throw new Error("参数类型原文必须是仅含 params 的包装对象");
  return owner;
}

/** 当前正文与停用列表不能为同一类型同时保存两个权威内容。 */
export function checkActiveParameterVariant(
  variants: ParameterVariant[],
  owner: ParamsOwner,
) {
  if (
    variants.some(
      (variant) => identity(readParameterVariant(variant)) === identity(owner),
    )
  )
    throw Error("当前参数类型不能同时存在停用权威内容");
}

/** 不按当前设备校验停用内容，只验证唯一归属及权威资料格式。 */
export function checkParameterVariants(
  value: unknown,
): asserts value is ParameterVariant[] {
  if (!Array.isArray(value)) throw new Error("参数类型编辑资料必须是列表");
  const identities = new Set<string>();
  for (const entry of value) {
    if (
      !isObject(entry) ||
      typeof entry.paramsText !== "string" ||
      !isObject(entry.pending) ||
      Object.keys(entry).some((key) => !["paramsText", "pending"].includes(key))
    )
      throw new Error("参数类型编辑资料格式不正确");
    for (const [key, input] of Object.entries(entry.pending)) {
      const path = decodeJsonPointer(key);
      if (
        !path.length ||
        path[0] === "type" ||
        !isObject(input) ||
        !["number", "json"].includes(String(input.kind)) ||
        typeof input.text !== "string"
      )
        throw new Error("参数类型未完成输入不能归属到参数成员");
    }
    const id = identity(
      readParameterVariant(entry as unknown as ParameterVariant),
    );
    if (identities.has(id)) throw new Error("参数类型有多个相同事实的停用内容");
    identities.add(id);
  }
}

function inspect(content: DraftContent, index: number) {
  const root = parseJson(content.text);
  if (
    !isObject(root) ||
    !Array.isArray(root.actions) ||
    !Number.isSafeInteger(index) ||
    index < 0 ||
    index >= root.actions.length ||
    !isObject(root.actions[index])
  )
    throw new Error("参数类型路径没有对应的动作对象");
  checkParameterVariantLocations(content, root.actions);
  const variants = content.parameterVariants?.[String(index)] ?? [];
  checkParameterVariants(variants);
  const inputs = inspectActionPending(content, root.actions).map(
    ({ key, input, path }) => ({ key, value: input, path }),
  );
  return { root, action: root.actions[index] as ParamsOwner, variants, inputs };
}

function isParams(path: string[], index: number) {
  return (
    path[0] === "actions" && path[1] === String(index) && path[2] === "params"
  );
}

/** 位置变更前统一核对新资料，不能把无法归属的记录重排或删除。 */
export function checkParameterVariantLocations(
  content: DraftContent,
  actions: unknown[],
  moving = false,
) {
  if (
    content.parameterVariants !== undefined &&
    !isObject(content.parameterVariants)
  )
    throw Error("参数类型资料必须是动作位置映射");
  for (const [key, variants] of Object.entries(
    content.parameterVariants ?? {},
  )) {
    if (
      !/^(0|[1-9][0-9]*)$/.test(key) ||
      !Number.isSafeInteger(Number(key)) ||
      Number(key) >= actions.length ||
      !isObject(actions[Number(key)])
    )
      throw Error("参数类型资料没有对应的动作位置");
    checkParameterVariants(variants);
    if (moving)
      checkActiveParameterVariant(
        variants,
        actions[Number(key)] as ParamsOwner,
      );
  }
  for (const [key, variants] of Object.entries(content.actionVariants ?? {})) {
    if (!Array.isArray(variants)) throw Error("动作类型资料必须是列表");
    for (const variant of variants)
      if (variant.parameterVariants !== undefined) {
        if (
          !/^(0|[1-9][0-9]*)$/.test(key) ||
          !Number.isSafeInteger(Number(key)) ||
          Number(key) >= actions.length
        )
          throw Error("动作分支的参数类型资料没有对应的位置");
        checkParameterVariants(variant.parameterVariants);
        if (moving) {
          const fields =
            variant.fieldsText === undefined
              ? variant.fields
              : parseClientJson(variant.fieldsText);
          if (!isObject(fields)) throw Error("动作类型字段原文必须是对象");
          checkActiveParameterVariant(variant.parameterVariants, fields);
        }
      }
  }
}

function snapshot(action: ParamsOwner, pending: Pending): ParameterVariant {
  const owner: ParamsOwner = {};
  transferParams(owner, action);
  return {
    paramsText: stringifyJson(owner),
    pending: cloneClientJson(pending),
  };
}

function withVariants(
  content: DraftContent,
  index: number,
  variants: ParameterVariant[],
) {
  const all = { ...content.parameterVariants };
  if (variants.length)
    Object.defineProperty(all, String(index), {
      value: variants,
      writable: true,
      enumerable: true,
      configurable: true,
    });
  else delete all[String(index)];
  return all;
}

export function canSwitchParameterType(
  content: DraftContent,
  index: number,
): boolean {
  try {
    const { action, variants, inputs } = inspect(content, index);
    checkActiveParameterVariant(variants, action);
    return !inputs.some(
      ({ path }) =>
        !path.length ||
        (path[0] === "actions" &&
          (path.length === 1 ||
            (path[1] === String(index) &&
              (path.length === 2 ||
                path[2] === "type" ||
                (path[2] === "params" &&
                  (path.length === 3 || path[3] === "type")))))),
    );
  } catch {
    return false;
  }
}

/** 只选择身份：恢复目标内容，首次目标只建立 type，成员原文随离开分支保存。 */
export function switchParameterType(
  content: DraftContent,
  index: number,
  type: unknown,
  numberToken?: string,
): DraftContent {
  const { root, action, variants, inputs } = inspect(content, index);
  if (!canSwitchParameterType(content, index))
    throw new Error("参数类型存在未完成或无法归属的编辑资料，暂不能切换");
  const target: ParamsOwner =
    type === undefined ? {} : { params: { type: cloneClientJson(type) } };
  if (isObject(target.params))
    rememberNumberToken(target.params, "type", numberToken);
  const currentId = identity(action),
    targetId = identity(target);
  if (currentId === targetId) return content;
  const restored = variants.find(
    (v) => identity(readParameterVariant(v)) === targetId,
  );
  const relative: Pending = {},
    pending = { ...content.pending };
  for (const { key, value, path } of inputs)
    if (isParams(path, index) && path.length > 3) {
      relative[
        "/" +
          path
            .slice(3)
            .map((part) => part.replace(/~/g, "~0").replace(/\//g, "~1"))
            .join("/")
      ] = cloneClientJson(value);
      delete pending[key];
    }
  const saved = snapshot(action, relative),
    replacement = restored ? readParameterVariant(restored) : target;
  delete action.params;
  transferParams(action, replacement);
  for (const [key, input] of Object.entries(restored?.pending ?? {}))
    pending[`/actions/${index}/params${key}`] = cloneClientJson(input);
  const remaining = [
    ...variants.filter((v) => identity(readParameterVariant(v)) !== targetId),
    saved,
  ];
  return {
    ...content,
    text: stringifyJson(root, 2),
    pending,
    parameterVariants: withVariants(content, index, remaining),
  };
}

/** 整体显式内容优先；旧完整类型保存，目标停用副本取出且不覆盖提供的值。 */
export function replaceParameterValue(
  content: DraftContent,
  index: number,
  value: unknown,
  omit = false,
  numberToken?: string,
): DraftContent {
  const { root, action, variants, inputs } = inspect(content, index);
  if (
    inputs.some(
      ({ path }) =>
        !path.length ||
        (path[0] === "actions" &&
          (path.length === 1 ||
            (path[1] === String(index) && path.length === 2))),
    )
  )
    throw new Error("祖先未完成输入阻止替换参数");
  const replacement: ParamsOwner = omit
    ? {}
    : { params: cloneClientJson(value) };
  rememberNumberToken(replacement, "params", numberToken);
  const currentId = identity(action),
    targetId = identity(replacement);
  if (currentId !== targetId) checkActiveParameterVariant(variants, action);
  const relative: Pending = {},
    pending = { ...content.pending };
  for (const { key, value: input, path } of inputs)
    if (isParams(path, index)) {
      if (path.length > 3 && path[3] !== "type")
        relative[
          "/" +
            path
              .slice(3)
              .map((part) => part.replace(/~/g, "~0").replace(/\//g, "~1"))
              .join("/")
        ] = cloneClientJson(input);
      delete pending[key];
    }
  let remaining = variants.filter(
    (v) =>
      identity(readParameterVariant(v)) !== targetId &&
      identity(readParameterVariant(v)) !== currentId,
  );
  if (currentId !== targetId)
    remaining = [...remaining, snapshot(action, relative)];
  delete action.params;
  transferParams(action, replacement);
  return {
    ...content,
    text: stringifyJson(root, 2),
    pending,
    parameterVariants: withVariants(content, index, remaining),
  };
}
