import type { ParameterType } from "../shared/types";
import { createValidator, isObject } from "../shared/validation";
import { resolveField } from "./editing";

export interface ParameterField {
  name: string;
  schema: Record<string, unknown>;
  values: unknown[];
  required: boolean;
}
export type ParameterOptions =
  | {
      kind: "finite";
      fields: ParameterField[];
      rows: Record<string, unknown>[];
    }
  | { kind: "unavailable"; reason: "open" | "unbounded" | "limit" };
const cache = new WeakMap<ParameterType, ParameterOptions>();

export function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b))
    return (
      a.length === b.length && a.every((value, i) => sameValue(value, b[i]))
    );
  if (isObject(a) && isObject(b))
    return (
      Object.keys(a).length === Object.keys(b).length &&
      Object.keys(a).every(
        (key) => Object.hasOwn(b, key) && sameValue(a[key], b[key]),
      )
    );
  return false;
}

// 候选域只需完整覆盖合法值；所有引用旁约束和组合规则由完整校验器取交集。
function domain(
  field: unknown,
  root: Record<string, unknown>,
  seen = new Set<string>(),
): unknown[] | undefined {
  if (!isObject(field)) return undefined;
  if (Object.hasOwn(field, "const")) return [field.const];
  if (Array.isArray(field.enum)) return field.enum;
  if (field.type === "boolean") return [true, false];
  if (
    Array.isArray(field.type) &&
    field.type.every((t) => t === "boolean" || t === "null")
  )
    return [
      ...(field.type.includes("boolean") ? [true, false] : []),
      ...(field.type.includes("null") ? [null] : []),
    ];
  if (field.type === "null") return [null];
  if (
    typeof field.$ref !== "string" ||
    !field.$ref.startsWith("#/") ||
    seen.has(field.$ref)
  )
    return undefined;
  let target: unknown = root;
  for (const token of field.$ref.slice(2).split("/")) {
    const key = token.replace(/~1/g, "/").replace(/~0/g, "~");
    if (
      !isObject(target) ||
      !Object.hasOwn(target, key) ||
      (Object.hasOwn(target, "$id") && target !== root)
    )
      return undefined;
    target = target[key];
  }
  if (isObject(target) && Object.hasOwn(target, "$id")) return undefined;
  return domain(target, root, new Set([...seen, field.$ref]));
}

export function parameterOptions(parameter: ParameterType): ParameterOptions {
  const prior = cache.get(parameter);
  if (prior) return prior;
  const result = buildOptions(parameter);
  cache.set(parameter, result);
  return result;
}
function buildOptions(parameter: ParameterType): ParameterOptions {
  const schema = parameter.schema;
  // patternProperties 可以开放未列出的键，无法证明下面的候选目录完整。
  if (
    schema.additionalProperties !== false ||
    schema.patternProperties !== undefined ||
    !isObject(schema.properties)
  )
    return { kind: "unavailable", reason: "open" };
  const fields: ParameterField[] = [];
  let size = 1;
  for (const [name, definition] of Object.entries(schema.properties)) {
    const values = domain(definition, schema);
    if (!values) return { kind: "unavailable", reason: "unbounded" };
    const required =
      Array.isArray(schema.required) && schema.required.includes(name);
    size *= values.length + (required ? 0 : 1);
    if (size > 4096) return { kind: "unavailable", reason: "limit" };
    fields.push({
      name,
      schema: resolveField(definition, schema),
      values,
      required,
    });
  }
  let rows: Record<string, unknown>[] = [{}];
  for (const field of fields) {
    rows = rows.flatMap((row) => [
      ...(!field.required ? [row] : []),
      ...field.values.map((value) => ({ ...row, [field.name]: value })),
    ]);
  }
  const validate = createValidator().compile(schema);
  return { kind: "finite", fields, rows: rows.filter((row) => validate(row)) };
}

export function compatibleValues(
  catalog: ParameterOptions,
  params: Record<string, unknown>,
  field: string,
): unknown[] {
  if (catalog.kind !== "finite") throw new Error("此规则无法推导联动选项");
  const rows = compatibleRows(catalog, params, field);
  return (
    catalog.fields
      .find((f) => f.name === field)
      ?.values.filter((value) =>
        rows.some(
          (row) => Object.hasOwn(row, field) && sameValue(row[field], value),
        ),
      ) ?? []
  );
}

function compatibleRows(
  catalog: Extract<ParameterOptions, { kind: "finite" }>,
  params: Record<string, unknown>,
  field: string,
) {
  return catalog.rows.filter((row) =>
    Object.entries(params).every(
      ([key, value]) =>
        key === field ||
        (Object.hasOwn(row, key) && sameValue(row[key], value)),
    ),
  );
}

export function parameterRequired(
  catalog: ParameterOptions,
  params: Record<string, unknown>,
  field: string,
): boolean {
  if (catalog.kind !== "finite") throw new Error("此规则无法推导条件必填性");
  if (catalog.fields.find((f) => f.name === field)?.required) return true;
  const rows = compatibleRows(catalog, params, field);
  return rows.length > 0 && rows.every((row) => Object.hasOwn(row, field));
}

export function optionLabel(value: unknown): string {
  if (typeof value === "string") return value === "" ? "空字符串" : value;
  if (typeof value === "boolean") return value ? "是" : "否";
  return JSON.stringify(value);
}
