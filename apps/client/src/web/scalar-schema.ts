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
    Object.keys(value).every((key) => assertions.has(key)) &&
    (value.type === undefined ||
      (Array.isArray(value.type) ? value.type : [value.type]).every(
        (type) => typeof type === "string" && types.has(type),
      )) &&
    (!Object.hasOwn(value, "const") || scalar(value.const)) &&
    (!Object.hasOwn(value, "enum") ||
      (Array.isArray(value.enum) && value.enum.every(scalar)))
  );
}
