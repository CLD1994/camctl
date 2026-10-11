import { expect, it } from "vitest";
import { parseJson, stringifyJson } from "../../src/shared/json";
import { createValidator } from "../../src/shared/validation";

it.each([
  ["minimum", "1e-999", "0"],
  ["maximum", "9223372036854775", "9223372036854776"],
  ["exclusiveMinimum", "1.0000000000000001", "1"],
  ["exclusiveMaximum", "-1e-999", "0"],
] as const)("%s 的诊断参数保留原边界 %s", (keyword, bound, value) => {
  const validate = createValidator().compile(
    parseJson(
      `{"type":"object","properties":{"value":{"type":"number","${keyword}":${bound}}}}`,
    ) as object,
  );
  expect(validate(parseJson(`{"value":${value}}`))).toBe(false);
  expect(
    stringifyJson(
      validate.errors?.find((error) => error.keyword === keyword)?.params,
    ),
  ).toContain(`"limit":${bound}`);
});

it.each([
  ["maximum", "9223372036854775", "9223372036854775", true],
  ["maximum", "9223372036854775", "9223372036854776", false],
  ["minimum", "9223372036854776", "9223372036854775", false],
  ["minimum", "1e-999", "0", false],
  ["maximum", "-1e-999", "0", false],
  ["exclusiveMinimum", "1.0000000000000001", "1.0000000000000002", true],
  ["exclusiveMinimum", "1", "1.0000000000000001", true],
  ["exclusiveMaximum", "1.0000000000000001", "1", true],
  ["exclusiveMaximum", "1", "0.99999999999999999", true],
  ["maximum", "1e0", "1.0", true],
  ["minimum", "-0.0", "0e1000", true],
  ["minimum", "-2.0000000000000001", "-2", true],
  ["maximum", "-2.0000000000000001", "-2", false],
] as const)(
  "%s 精确比较边界 %s 与输入 %s",
  (keyword, bound, value, expected) => {
    const schema = parseJson(
      `{"type":"object","properties":{"value":{"type":"number","${keyword}":${bound}}}}`,
    ) as Record<string, unknown>;
    const before = stringifyJson(schema);
    const validate = createValidator().compile(schema);
    expect(validate(parseJson(`{"value":${value}}`))).toBe(expected);
    if (!expected)
      expect(validate.errors).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ keyword, instancePath: "/value" }),
        ]),
      );
    expect(stringifyJson(schema)).toBe(before);
  },
);

it.each([
  '{"type":"array","items":{"type":"number","maximum":9223372036854775}}',
  '{"type":"array","prefixItems":[{"$ref":"#/$defs/value"}],"$defs":{"value":{"type":"number","maximum":9223372036854775}}}',
])("数组与本地引用保留精确范围 %s", (text) => {
  const validate = createValidator().compile(parseJson(text) as object);
  expect(validate(parseJson("[9223372036854776]"))).toBe(false);
  expect(validate(parseJson("[9223372036854775]"))).toBe(true);
});

it("条件分支使用精确上界作判断", () => {
  const schema = parseJson(
    '{"type":"object","properties":{"value":{"type":"integer"},"flag":{"type":"boolean"}},"if":{"properties":{"value":{"maximum":9223372036854775}}},"then":{"properties":{"flag":{"const":true}}},"else":{"properties":{"flag":{"const":false}}}}',
  ) as object;
  const validate = createValidator().compile(schema);
  expect(validate(parseJson('{"value":9223372036854776,"flag":false}'))).toBe(
    true,
  );
  expect(validate(parseJson('{"value":9223372036854775,"flag":true}'))).toBe(
    true,
  );
});

it("根标量的显式数字上下文用于边界比较", () => {
  const validate = createValidator().compile(
    parseJson('{"type":"integer","maximum":9223372036854775}') as object,
  );
  expect(
    validate.call({ numberToken: "9223372036854775" }, 9223372036854776),
  ).toBe(true);
  expect(
    validate.call({ numberToken: "9223372036854776" }, 9223372036854776),
  ).toBe(false);
});
