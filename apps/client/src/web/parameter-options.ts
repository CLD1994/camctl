import type { ParameterType } from "../shared/types";
import { createValidator, isObject } from "../shared/validation";
import { resolveField } from "./editing";
import { parseDraft, pointerPath, type Path } from "./editing";
import type { DraftContent } from "../server/models";
import { SCHEMA_ANNOTATIONS, visitSchemaNodes } from "../shared/schema-nodes";
import { isLocalScalarSchema } from "./scalar-schema";
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
type FiniteOptions = {
  kind: "finite";
  fields: ParameterField[];
  rows: Record<string, unknown>[];
};
type UnavailableOptions = {
  kind: "unavailable";
  reason: "open" | "unbounded" | "limit" | "numeric";
};
type IndependentOptions = {
  kind: "independent";
  fields: ParameterField[];
  names: string[];
  required: string[];
};
export type ParameterOptions =
  | FiniteOptions
  | IndependentOptions
  | UnavailableOptions
  | {
      kind: "grouped";
      // fields 只包含独立字段的有限选项；组候选只由 group.catalog 表达。
      fields: ParameterField[];
      independent: string[];
      independentRequired: string[];
      group: { members: string[]; catalog: FiniteOptions | UnavailableOptions };
    };
const cache = new WeakMap<ParameterType, ParameterOptions>();
const MAX_CANDIDATES = 4096;

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
    complete.kind === "unavailable"
      ? (groupedOptions(parameter) ?? independentOptions(parameter) ?? complete)
      : complete;
  cache.set(parameter, result);
  return result;
}
export function hasNumericLoss(schema: Record<string, unknown>): boolean {
  const assertions = cloneClientJson(schema);
  visitSchemaNodes(assertions, (node) => {
    for (const keyword of SCHEMA_ANNOTATIONS) delete node[keyword];
  });
  return (
    exactJsonValueIdentity(assertions) !==
    exactJsonValueIdentity(JSON.parse(JSON.stringify(assertions)))
  );
}
function buildOptions(
  parameter: ParameterType,
): FiniteOptions | UnavailableOptions {
  const schema = parameter.schema;
  // 规则错误必须原样交给能力加载层，不能被不可枚举状态隐藏。
  const validate = createValidator().compile(schema);
  // 完整目录与局部字段分别检查其全部断言，不能用舍入事实生成候选。
  if (hasNumericLoss(schema)) return { kind: "unavailable", reason: "numeric" };
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
    if (size > MAX_CANDIDATES) return { kind: "unavailable", reason: "limit" };
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
  const fields: ParameterField[] = [];
  for (const [name, definition] of Object.entries(schema.properties)) {
    if (!isLocalScalarSchema(definition) || hasNumericLoss(definition))
      continue;
    const values = domain(definition, schema);
    if (
      !values ||
      values.length > MAX_CANDIDATES ||
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
  return {
    kind: "independent",
    fields,
    names: Object.keys(schema.properties),
    required: Array.isArray(schema.required)
      ? (schema.required as string[])
      : [],
  };
}

const conditionKeywords = new Set([
  "if",
  "then",
  "else",
  ...SCHEMA_ANNOTATIONS,
]);
const leafKeywords = new Set([
  "type",
  "properties",
  "required",
  ...SCHEMA_ANNOTATIONS,
]);

function groupedOptions(
  parameter: ParameterType,
): Extract<ParameterOptions, { kind: "grouped" }> | undefined {
  const schema = parameter.schema;
  if (
    schema.type !== "object" ||
    schema.additionalProperties !== false ||
    !isObject(schema.properties) ||
    !Array.isArray(schema.required) ||
    !Array.isArray(schema.allOf) ||
    Object.keys(schema).some(
      (key) => key !== "allOf" && !independentRoot.has(key),
    ) ||
    schema.required.some(
      (name) =>
        typeof name !== "string" ||
        !Object.hasOwn(schema.properties as object, name),
    )
  )
    return undefined;
  const properties = schema.properties;
  const members = new Set<string>();
  const addMember = (name: string) => {
    if (!Object.hasOwn(properties, name)) return false;
    members.add(name);
    return true;
  };
  const leaf = (value: unknown): boolean => {
    if (typeof value === "boolean") return true;
    if (
      !isObject(value) ||
      Object.keys(value).some((key) => !leafKeywords.has(key)) ||
      (value.type !== undefined && value.type !== "object")
    )
      return false;
    if (
      value.required !== undefined &&
      (!Array.isArray(value.required) ||
        !value.required.every(
          (name) => typeof name === "string" && addMember(name),
        ))
    )
      return false;
    return (
      value.properties === undefined ||
      (isObject(value.properties) &&
        Object.entries(value.properties).every(
          ([name, rule]) => addMember(name) && isLocalScalarSchema(rule),
        ))
    );
  };
  if (
    !schema.allOf.every(
      (condition) =>
        isObject(condition) &&
        Object.hasOwn(condition, "if") &&
        Object.hasOwn(condition, "then") &&
        Object.keys(condition).every((key) => conditionKeywords.has(key)) &&
        leaf(condition.if) &&
        leaf(condition.then) &&
        (!Object.hasOwn(condition, "else") || leaf(condition.else)),
    ) ||
    members.size === 0
  )
    return undefined;
  // 根字段本身也必须是可解释的局部标量；无限域和精度损失只影响枚举资格。
  if ([...members].some((name) => !isLocalScalarSchema(properties[name])))
    return undefined;
  const names = Object.keys(properties);
  const groupMembers = names.filter((name) => members.has(name));
  const independent = names.filter((name) => !members.has(name));
  const project = (keys: string[]) =>
    Object.fromEntries(
      keys.map((name) => [name, cloneClientJson(properties[name])]),
    );
  const groupSchema = {
    $schema: schema.$schema,
    type: "object",
    additionalProperties: false,
    properties: project(groupMembers),
    required: schema.required.filter((name) => members.has(name as string)),
    allOf: cloneClientJson(schema.allOf),
  };
  const local = independentOptions({
    ...parameter,
    schema: {
      type: "object",
      additionalProperties: false,
      properties: project(independent),
      required: schema.required.filter((name) => !members.has(name as string)),
    },
  })!;
  return {
    kind: "grouped",
    fields: local.fields,
    independent,
    independentRequired: local.required,
    group: {
      members: groupMembers,
      catalog: buildOptions({ ...parameter, schema: groupSchema }),
    },
  };
}

/** 只有根结构证明条件不读取此字段时，才能采用其局部控件资格。 */
export function parameterFieldIndependent(
  catalog: ParameterOptions,
  field: string,
): boolean {
  return (
    (catalog.kind === "independent" && catalog.names.includes(field)) ||
    (catalog.kind === "grouped" && catalog.independent.includes(field))
  );
}

/** 查询返回当前字段实际覆盖的规则范围，局部组不能冒充完整目录。 */
export function parameterFieldOptions(
  catalog: ParameterOptions,
  field: string,
): FiniteOptions | IndependentOptions | UnavailableOptions {
  if (catalog.kind !== "grouped") return catalog;
  if (catalog.group.members.includes(field)) return catalog.group.catalog;
  if (catalog.independent.includes(field))
    return {
      kind: "independent",
      fields: catalog.fields,
      names: catalog.independent,
      required: catalog.independentRequired,
    };
  return { kind: "unavailable", reason: "unbounded" };
}

/** 当前字段的依赖范围由目录分类确定，不能读取相关 pending 所覆盖的旧值。 */
export function parameterChoiceBlocked(
  content: DraftContent,
  path: Path,
  scope: "finite" | "independent" | ParameterOptions,
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
  const kind =
    typeof scope === "string"
      ? scope
      : parameterFieldOptions(scope, String(path[3])).kind;
  const groupPaths =
    typeof scope !== "string" &&
    scope.kind === "grouped" &&
    scope.group.members.includes(String(path[3]))
      ? scope.group.members.map((name) => paramsPath.concat(name))
      : [];
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
      (groupPaths.length
        ? groupPaths.some(
            (member) => prefix(pending, member) || prefix(member, pending),
          )
        : kind === "finite" && prefix(paramsPath, pending))
    );
  });
}

export function compatibleValues(
  catalog: ParameterOptions,
  params: Record<string, unknown>,
  field: string,
): unknown[] {
  const scope = parameterFieldOptions(catalog, field);
  if (scope.kind === "unavailable") throw new Error("此规则无法推导联动选项");
  const values = scope.fields.find((entry) => entry.name === field)?.values;
  if (scope.kind === "independent")
    return values ? filteredValues(values, () => true) : [];
  const rows = compatibleRows(scope, params, field, catalog.kind === "grouped");
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
  catalog: FiniteOptions,
  params: Record<string, unknown>,
  field: string,
  local = false,
) {
  return catalog.rows.filter((row) =>
    Object.entries(params).every(
      ([key]) =>
        key === field ||
        (local && !catalog.fields.some((entry) => entry.name === key)) ||
        (Object.hasOwn(row, key) && sameValueAt(row, key, params, key)),
    ),
  );
}

export function parameterRequired(
  catalog: ParameterOptions,
  params: Record<string, unknown>,
  field: string,
): boolean {
  const scope = parameterFieldOptions(catalog, field);
  if (scope.kind === "unavailable") throw new Error("此规则无法推导条件必填性");
  if (scope.fields.find((f) => f.name === field)?.required) return true;
  if (scope.kind === "independent") return scope.required.includes(field);
  const rows = compatibleRows(scope, params, field, catalog.kind === "grouped");
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
