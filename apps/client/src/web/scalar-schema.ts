import { exactJsonValueIdentity } from "../shared/json";
import { SCHEMA_ANNOTATIONS } from "../shared/schema-nodes";
import { isObject } from "../shared/validation";

// 局部控件能完整解释的标量断言子集，不是能力 Schema 的合法关键词目录。
const assertions = new Set([
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
const types = new Set(["string", "number", "integer", "boolean", "null"]);
const scalar = (value: unknown) =>
  value === null || ["string", "number", "boolean"].includes(typeof value);

export function isLocalScalarSchema(
  value: unknown,
): value is Record<string, unknown> {
  return (
    isObject(value) &&
    Object.keys(value).every((key) => assertions.has(key) || key === "oneOf") &&
    (!Object.hasOwn(value, "oneOf") || namedConstants(value)) &&
    (value.type === undefined ||
      (Array.isArray(value.type) ? value.type : [value.type]).every(
        (type) => typeof type === "string" && types.has(type),
      )) &&
    (!Object.hasOwn(value, "const") || scalar(value.const)) &&
    (!Object.hasOwn(value, "enum") ||
      (Array.isArray(value.enum) && value.enum.every(scalar)))
  );
}

/** oneOf 的每个分支必须是同类型、互异且可解释的常量声明。 */
function namedConstants(schema: Record<string, unknown>): boolean {
  if (
    !["string", "number", "integer"].includes(schema.type as string) ||
    !Array.isArray(schema.oneOf) ||
    schema.oneOf.length === 0
  )
    return false;
  const identities = new Set<string>();
  return schema.oneOf.every((branch) => {
    if (
      !isObject(branch) ||
      !Object.hasOwn(branch, "const") ||
      Object.keys(branch).some((key) => !assertions.has(key)) ||
      !isLocalScalarSchema(branch)
    )
      return false;
    const value = branch.const;
    if (
      schema.type === "string"
        ? typeof value !== "string"
        : typeof value !== "number" ||
          !Number.isFinite(value) ||
          (schema.type === "integer" && !Number.isInteger(value))
    )
      return false;
    const identity = exactJsonValueIdentity(value, true, {
      parent: branch,
      key: "const",
    });
    if (identities.has(identity)) return false;
    identities.add(identity);
    return true;
  });
}
