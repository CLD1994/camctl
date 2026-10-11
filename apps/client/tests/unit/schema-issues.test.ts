import { it, expect } from "vitest";
import type { ErrorObject } from "ajv";
import { parseJson, stringifyJson } from "../../src/shared/json";
import { schemaIssues } from "../../src/shared/validation";

it.each([
  ["minimum", "1e-999"],
  ["maximum", "9223372036854775"],
  ["exclusiveMinimum", "1.0000000000000001"],
  ["exclusiveMaximum", "-1e-999"],
] as const)("%s 规范化诊断保留边界 %s", (keyword, bound) => {
  const errors = parseJson(
    `[{"instancePath":"/value","schemaPath":"#/properties/value/${keyword}","keyword":"${keyword}","params":{"limit":${bound}},"message":"technical"}]`,
  ) as unknown as ErrorObject[];
  const before = stringifyJson(errors);
  const issue = schemaIssues(errors)[0];
  expect(stringifyJson(issue.schema?.params)).toContain(`"limit":${bound}`);
  expect(issue.pointer).toBe("/value");
  expect(issue.code).toBe(`schema_${keyword}`);
  expect(stringifyJson(errors)).toBe(before);
});
