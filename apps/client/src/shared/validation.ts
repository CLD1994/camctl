import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import type { ErrorObject } from "ajv";
import type { Issue } from "./types";

export const DIALECT = "https://json-schema.org/draft/2020-12/schema";
export const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
export const isId = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value) &&
  !/[\r\n]/.test(value);
export const isUint = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
export const isPositive = (value: unknown): value is number =>
  isUint(value) && value > 0;
export const isName = (value: unknown): value is string =>
  typeof value === "string" &&
  [...value].length >= 1 &&
  [...value].length <= 128 &&
  !/^[\p{White_Space}\uFEFF]|[\p{White_Space}\uFEFF]$/u.test(value) &&
  !/[\u0000-\u001f\u007f-\u009f]/.test(value);
export function isTimestamp(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$/.test(value) ||
    /[\r\n]/.test(value)
  )
    return false;
  const [year, month, day, hour, minute, second] = value
    .slice(0, 19)
    .split(/[- :]/)
    .map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  return (
    year >= 1 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <=
      [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1] &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59
  );
}
export function createValidator() {
  const ajv = new Ajv2020({
    strict: true,
    strictSchema: false,
    strictRequired: false,
    strictTypes: false,
    strictTuples: false,
    allowMatchingProperties: true,
    allErrors: true,
    coerceTypes: false,
    removeAdditional: false,
    useDefaults: false,
  });
  addFormats(ajv);
  return ajv;
}
export function schemaIssues(
  errors: ErrorObject[] | null | undefined,
  prefix = "",
): Issue[] {
  return (errors ?? []).map((error) => {
    const segments = error.instancePath
      .split("/")
      .slice(1)
      .map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));
    const missing =
      error.keyword === "required"
        ? error.params.missingProperty
        : error.keyword === "additionalProperties"
          ? error.params.additionalProperty
          : undefined;
    if (typeof missing === "string") segments.push(missing);
    const path = segments.reduce(
      (p, s) => (/^\d+$/.test(s) ? `${p}[${s}]` : p ? `${p}.${s}` : s),
      prefix,
    );
    return {
      path,
      code: `schema_${error.keyword}`,
      message: `${path || "输入"}：${error.message ?? "不符合规则"}`,
    };
  });
}
