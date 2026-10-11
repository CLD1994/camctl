import type { DraftContent } from "../server/models";
import { replaceObjectValue } from "../shared/object-variants";
import type { ParameterType } from "../shared/types";
import { SCHEMA_ANNOTATIONS } from "../shared/schema-nodes";
import { createValidator, isObject } from "../shared/validation";
import {
  cloneClientJson,
  originalNumberToken,
  rememberNumberToken,
} from "../shared/json";
import {
  parseDraft,
  pointer,
  pointerPath,
  valueAt,
  type Path,
} from "./editing";
import { hasNumericLoss, parameterOptions } from "./parameter-options";

export interface ObjectBranchMember {
  name: string;
  schema: Record<string, unknown>;
  required: boolean;
  choices?: unknown[];
}
export interface ObjectBranch {
  value: string;
  schema: Record<string, unknown>;
  members: ObjectBranchMember[];
}
export interface ObjectBranches {
  discriminator: string;
  schema: Record<string, unknown>;
  branches: ObjectBranch[];
}
const cache = new WeakMap<
  ParameterType,
  Map<string, ObjectBranches | undefined>
>();
const objectKeywords = new Set([
  "type",
  "properties",
  "required",
  "additionalProperties",
  ...SCHEMA_ANNOTATIONS,
]);
const scalarKeywords = new Set([
  "type",
  "const",
  "enum",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "multipleOf",
  "minLength",
  "maxLength",
  "pattern",
  "format",
  ...SCHEMA_ANNOTATIONS,
]);
const scalarTypes = new Set(["string", "number", "integer", "boolean", "null"]);
const scalar = (value: unknown) =>
  value === null || ["string", "number", "boolean"].includes(typeof value);

/** 仅把已证明独立的局部闭合对象分支转成控件，完整参数仍由原 Schema 校验。 */
export function objectBranches(
  parameter: ParameterType,
  name: string,
): ObjectBranches | undefined {
  let entries = cache.get(parameter);
  if (!entries) cache.set(parameter, (entries = new Map()));
  if (entries.has(name)) return entries.get(name);
  const result = buildBranches(parameter, name);
  entries.set(name, result);
  return result;
}
function buildBranches(
  parameter: ParameterType,
  name: string,
): ObjectBranches | undefined {
  if (parameterOptions(parameter).kind !== "independent") return undefined;
  const schema = isObject(parameter.schema.properties)
    ? parameter.schema.properties[name]
    : undefined;
  if (
    !isObject(schema) ||
    !Array.isArray(schema.oneOf) ||
    schema.oneOf.length < 2 ||
    (schema.type !== undefined && schema.type !== "object") ||
    Object.keys(schema).some(
      (key) =>
        !["oneOf", "type", ...SCHEMA_ANNOTATIONS].includes(
          key as (typeof SCHEMA_ANNOTATIONS)[number],
        ),
    ) ||
    hasNumericLoss(schema)
  )
    return undefined;
  const branches = schema.oneOf;
  if (
    !branches.every(
      (branch) =>
        isObject(branch) &&
        branch.type === "object" &&
        branch.additionalProperties === false &&
        isObject(branch.properties) &&
        Array.isArray(branch.required) &&
        branch.required.every(
          (key) =>
            typeof key === "string" &&
            Object.hasOwn(branch.properties as object, key),
        ) &&
        Object.keys(branch).every((key) => objectKeywords.has(key)) &&
        Object.values(branch.properties).every((field) => {
          if (
            !isObject(field) ||
            Object.keys(field).some((key) => !scalarKeywords.has(key))
          )
            return false;
          const types =
            field.type === undefined
              ? []
              : Array.isArray(field.type)
                ? field.type
                : [field.type];
          if (
            types.some(
              (type) => typeof type !== "string" || !scalarTypes.has(type),
            ) ||
            types.filter((type) => type !== "null").length > 1
          )
            return false;
          if (Object.hasOwn(field, "const") && !scalar(field.const))
            return false;
          if (
            Object.hasOwn(field, "enum") &&
            (!Array.isArray(field.enum) || !field.enum.every(scalar))
          )
            return false;
          return (
            Object.hasOwn(field, "const") ||
            Array.isArray(field.enum) ||
            types.length > 0
          );
        }),
    )
  )
    return undefined;
  const candidates = Object.keys(branches[0].properties as object).filter(
    (key) => {
      const values = branches.map((branch) => {
        const field = (branch.properties as Record<string, unknown>)[key];
        return (branch.required as unknown[]).includes(key) &&
          isObject(field) &&
          typeof field.const === "string"
          ? field.const
          : undefined;
      });
      return (
        values.every((value) => value !== undefined) &&
        new Set(values).size === branches.length
      );
    },
  );
  if (candidates.length !== 1) return undefined;
  const discriminator = candidates[0],
    result: ObjectBranch[] = [];
  for (const branch of branches) {
    const properties = branch.properties as Record<
      string,
      Record<string, unknown>
    >;
    const value = properties[discriminator].const as string;
    if (!createValidator().compile(properties[discriminator])(value))
      return undefined;
    const catalog = parameterOptions({ ...parameter, schema: branch });
    if (catalog.kind === "unavailable") return undefined;
    const members: ObjectBranchMember[] = [];
    for (const [key, field] of Object.entries(properties)) {
      if (key === discriminator) continue;
      const candidate = catalog.fields.find((entry) => entry.name === key);
      const finite =
        Object.hasOwn(field, "const") ||
        Array.isArray(field.enum) ||
        field.type === "boolean" ||
        field.type === "null" ||
        (Array.isArray(field.type) &&
          field.type.every((type) => type === "boolean" || type === "null"));
      if (finite && !candidate) return undefined;
      let choices: unknown[] | undefined;
      if (candidate) {
        const check = createValidator().compile(field);
        choices = [];
        candidate.values.forEach((item, index) => {
          const token = originalNumberToken(candidate.values, index, item);
          if (!check.call({ numberToken: token }, item)) return;
          choices!.push(cloneClientJson(item));
          rememberNumberToken(choices!, choices!.length - 1, token);
        });
      }
      members.push({
        name: key,
        schema: field,
        required: (branch.required as unknown[]).includes(key),
        ...(choices ? { choices } : {}),
      });
    }
    result.push({ value, schema: branch, members });
  }
  return { discriminator, schema, branches: result };
}

export type ObjectBranchState =
  | { kind: "empty" }
  | { kind: "selected"; branch: ObjectBranch; index: number }
  | { kind: "raw"; reason?: "discriminator_changed" }
  | { kind: "blocked" };
/** 只读分类不修正旧值；可表达的非法标量交由普通字段明确修正。 */
export function objectBranchState(
  content: DraftContent,
  path: Path,
  catalog: ObjectBranches,
): ObjectBranchState {
  const root = parseDraft(content),
    value = valueAt(root, path),
    current = pointer(path);
  const identities = [
    pointer(path.slice(0, 2).concat("device_id")),
    pointer(path.slice(0, 2).concat("type")),
    pointer(path.slice(0, 3).concat("type")),
  ];
  let raw = false;
  for (const key of Object.keys(content.pending ?? {})) {
    try {
      pointerPath(key);
    } catch {
      return { kind: "blocked" };
    }
    if (
      (key !== current && current.startsWith(key + "/")) ||
      identities.some(
        (identity) =>
          identity === key ||
          identity.startsWith(key + "/") ||
          key.startsWith(identity + "/"),
      )
    )
      return { kind: "blocked" };
    if (
      key === current ||
      key ===
        `${current}/${catalog.discriminator.replace(/~/g, "~0").replace(/\//g, "~1")}`
    )
      raw = true;
  }
  const group =
    content.objectVariants?.[String(path[1])]?.[pointer(path.slice(3))];
  if (group && group.discriminator !== catalog.discriminator)
    return { kind: "raw", reason: "discriminator_changed" };
  if (raw) return { kind: "raw" };
  const childInputs = Object.keys(content.pending ?? {}).filter((key) =>
    key.startsWith(current + "/"),
  );
  if (
    (value === undefined ||
      (isObject(value) && Object.keys(value).length === 0)) &&
    !childInputs.length
  )
    return { kind: "empty" };
  if (!isObject(value)) return { kind: "raw" };
  const index = catalog.branches.findIndex(
    (branch) => branch.value === value[catalog.discriminator],
  );
  if (index < 0) return { kind: "raw" };
  const branch = catalog.branches[index],
    properties = branch.schema.properties as Record<string, unknown>;
  if (
    Object.keys(value).some(
      (key) => !Object.hasOwn(properties, key) || !scalar(value[key]),
    ) ||
    childInputs.some((key) => {
      const suffix = pointerPath(key).slice(path.length);
      return (
        suffix.length !== 1 ||
        !branch.members.some((field) => field.name === suffix[0])
      );
    })
  )
    return { kind: "raw" };
  return { kind: "selected", branch, index };
}

/** 整段有效 JSON 提供分支身份；未完成原文只保存 Field 已生成的 pending。 */
export function objectBranchJsonEdit(
  content: DraftContent,
  next: DraftContent,
  path: Path,
  discriminator: string,
): DraftContent {
  if (Object.hasOwn(next.pending ?? {}, pointer(path))) return next;
  const root = parseDraft(next),
    parent = valueAt(root, path.slice(0, -1)),
    key = path.at(-1)!,
    value = valueAt(root, path);
  return replaceObjectValue(
    content,
    Number(path[1]),
    path.slice(3).map(String),
    value,
    {
      discriminator,
      omit: !isObject(parent) || !Object.hasOwn(parent, key),
      numberToken: originalNumberToken(parent, key, value),
    },
  );
}
