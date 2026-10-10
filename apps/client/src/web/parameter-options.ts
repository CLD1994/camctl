import type { ParameterType } from "../shared/types";
import { createValidator, isObject } from "../shared/validation";
import { resolveField } from "./editing";
import { parseDraft, pointerPath, type Path } from "./editing";
import type { DraftContent } from "../server/models";
import { SCHEMA_ANNOTATIONS, visitSchemaNodes } from "../shared/schema-nodes";
import {
  cloneClientJson,
  exactJsonValueIdentity,
  originalNumberToken,
  rememberNumberToken,
  displayJsonValue,
} from "../shared/json";

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
  | { kind: "independent"; fields: ParameterField[] }
  | { kind: "unavailable"; reason: "open" | "unbounded" | "limit" | "numeric" };
const cache = new WeakMap<ParameterType, ParameterOptions>();

export function sameValue(a: unknown, b: unknown): boolean {
  if (a === undefined || b === undefined) return a === b;
  return exactJsonValueIdentity(a) === exactJsonValueIdentity(b);
}

/** 标量数字的原词元属于父容器；缺省也必须独立比较。 */
export function sameValueAt(
  a: object,
  aKey: string | number,
  b: object,
  bKey: string | number,
): boolean {
  return (
    exactJsonValueIdentity(Reflect.get(a, aKey), Object.hasOwn(a, aKey), {
      parent: a,
      key: aKey,
    }) ===
    exactJsonValueIdentity(Reflect.get(b, bKey), Object.hasOwn(b, bKey), {
      parent: b,
      key: bKey,
    })
  );
}

function filteredValues(
  values: unknown[],
  include: (value: unknown, index: number) => boolean,
): unknown[] {
  const result: unknown[] = [];
  values.forEach((value, index) => {
    if (!include(value, index)) return;
    result.push(cloneClientJson(value));
    rememberNumberToken(
      result,
      result.length - 1,
      originalNumberToken(values, index, value),
    );
  });
  return result;
}

// 候选域只需完整覆盖合法值；所有引用旁约束和组合规则由完整校验器取交集。
function domain(
  field: unknown,
  root: Record<string, unknown>,
  seen = new Set<string>(),
): unknown[] | undefined {
  if (!isObject(field)) return undefined;
  if (Object.hasOwn(field, "const")) {
    const values = [field.const];
    rememberNumberToken(
      values,
      0,
      originalNumberToken(field, "const", field.const),
    );
    return values;
  }
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
  const complete = buildOptions(parameter);
  const result =
    complete.kind === "unavailable" && complete.reason !== "numeric"
      ? (independentOptions(parameter) ?? complete)
      : complete;
  cache.set(parameter, result);
  return result;
}
function buildOptions(parameter: ParameterType): ParameterOptions {
  const schema = parameter.schema;
  const assertions = cloneClientJson(schema);
  visitSchemaNodes(assertions, (node) => {
    for (const keyword of SCHEMA_ANNOTATIONS) delete node[keyword];
  });
  // Ajv 的 enum、范围等使用 Number；不把存在舍入损失的规则用于生成候选。
  if (
    exactJsonValueIdentity(assertions) !==
    exactJsonValueIdentity(JSON.parse(JSON.stringify(assertions)))
  )
    return { kind: "unavailable", reason: "numeric" };
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
      ...field.values.map((value, index) => {
        const candidate = cloneClientJson(row);
        Object.defineProperty(candidate, field.name, {
          value: cloneClientJson(value),
          writable: true,
          configurable: true,
          enumerable: true,
        });
        rememberNumberToken(
          candidate,
          field.name,
          originalNumberToken(field.values, index, value),
        );
        return candidate;
      }),
    ]);
  }
  const validate = createValidator().compile(schema);
  return { kind: "finite", fields, rows: rows.filter((row) => validate(row)) };
}

// 这是独立编辑的受支持子集，不是能力 Schema 的合法关键词目录。
const independentRoot = new Set([
  "$schema",
  "type",
  "properties",
  "required",
  "additionalProperties",
  ...SCHEMA_ANNOTATIONS,
]);
const scalarAssertions = new Set([
  "type",
  "enum",
  "const",
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
function independentOptions(
  parameter: ParameterType,
): Extract<ParameterOptions, { kind: "independent" }> | undefined {
  const schema = parameter.schema;
  if (
    schema.type !== "object" ||
    schema.additionalProperties !== false ||
    !isObject(schema.properties) ||
    Object.keys(schema).some((key) => !independentRoot.has(key))
  )
    return undefined;
  const definitions = Object.entries(schema.properties);
  if (
    definitions.some(
      ([, field]) =>
        !isObject(field) ||
        Object.keys(field).some((key) => !scalarAssertions.has(key)) ||
        (field.type !== undefined &&
          !(Array.isArray(field.type) ? field.type : [field.type]).every(
            (type) => typeof type === "string" && scalarTypes.has(type),
          )),
    )
  )
    return undefined;
  const fields: ParameterField[] = [];
  for (const [name, field] of definitions) {
    const definition = field as Record<string, unknown>;
    const values = domain(definition, schema);
    if (
      !values ||
      values.length > 4096 ||
      values.some((value) => typeof value === "object" && value !== null)
    )
      continue;
    const validate = createValidator().compile(definition);
    fields.push({
      name,
      schema: definition,
      required:
        Array.isArray(schema.required) && schema.required.includes(name),
      values: filteredValues(values, (value, index) =>
        validate.call(
          { numberToken: originalNumberToken(values, index, value) },
          value,
        ),
      ),
    });
  }
  return { kind: "independent", fields };
}

/** 当前字段的依赖范围由目录分类确定，不能读取相关 pending 所覆盖的旧值。 */
export function parameterChoiceBlocked(
  content: DraftContent,
  path: Path,
  kind: "finite" | "independent",
): boolean {
  if (path.length !== 4 || path[0] !== "actions" || path[2] !== "params")
    return true;
  let root: ReturnType<typeof parseDraft>;
  try {
    root = parseDraft(content);
  } catch {
    return true;
  }
  const actionIndex = String(path[1]);
  if (
    !/^(0|[1-9][0-9]*)$/.test(actionIndex) ||
    !isObject(root.actions[Number(actionIndex)]) ||
    !isObject(root.actions[Number(actionIndex)].params)
  )
    return true;
  const fieldPath = path.map(String);
  const paramsPath = fieldPath.slice(0, 3);
  const identityPaths = [
    fieldPath.slice(0, 2).concat("device_id"),
    fieldPath.slice(0, 2).concat("type"),
    paramsPath.concat("type"),
  ];
  const prefix = (a: string[], b: string[]) =>
    a.length <= b.length && a.every((part, index) => part === b[index]);
  return Object.keys(content.pending ?? {}).some((key) => {
    let pending: string[];
    try {
      pending = pointerPath(key).map(String);
    } catch {
      return true;
    }
    if (
      pending[0] === "actions" &&
      pending.length > 1 &&
      (!/^(0|[1-9][0-9]*)$/.test(pending[1]) ||
        !isObject(root.actions[Number(pending[1])]))
    )
      return true;
    return (
      prefix(pending, fieldPath) ||
      prefix(fieldPath, pending) ||
      identityPaths.some(
        (identity) => prefix(pending, identity) || prefix(identity, pending),
      ) ||
      (kind === "finite" && prefix(paramsPath, pending))
    );
  });
}

export function compatibleValues(
  catalog: ParameterOptions,
  params: Record<string, unknown>,
  field: string,
): unknown[] {
  if (catalog.kind !== "finite") throw new Error("此规则无法推导联动选项");
  const rows = compatibleRows(catalog, params, field);
  const values = catalog.fields.find((entry) => entry.name === field)?.values;
  return values
    ? filteredValues(values, (_value, index) =>
        rows.some(
          (row) =>
            Object.hasOwn(row, field) && sameValueAt(row, field, values, index),
        ),
      )
    : [];
}

function compatibleRows(
  catalog: Extract<ParameterOptions, { kind: "finite" }>,
  params: Record<string, unknown>,
  field: string,
) {
  return catalog.rows.filter((row) =>
    Object.entries(params).every(
      ([key]) =>
        key === field ||
        (Object.hasOwn(row, key) && sameValueAt(row, key, params, key)),
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

export function optionLabel(
  value: unknown,
  parent?: unknown,
  key: string | number = "",
): string {
  if (typeof value === "string") return value === "" ? "空字符串" : value;
  if (typeof value === "boolean") return value ? "是" : "否";
  return displayJsonValue(parent, key, value);
}
