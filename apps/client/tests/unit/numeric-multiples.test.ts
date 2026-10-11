import { expect, it } from "vitest";
import { parseJson, stringifyJson } from "../../src/shared/json";
import { createValidator, schemaIssues } from "../../src/shared/validation";
import { parameterOptions } from "../../src/web/parameter-options";
import type { ParameterType } from "../../src/shared/types";

it.each([
  ["0.1", "0.3", true],
  ["0.1", "-0.3", true],
  ["0.1", "0", true],
  ["0.1", "0.30000000000000001", false],
  ["0.1", "0.29999999999999999", false],
  ["0.1", "1e-999", false],
  ["0.5", "1.5", true],
  ["0.6", "3", true],
  ["0.03", "1", false],
  ["0.08", "1", false],
  ["0.08", "2", true],
  ["0.04", "1", true],
  ["2.5", "0.5", false],
  ["5", "9223372036854775", true],
  ["1e-999", "0", true],
  ["1e-1000", "1e-999", true],
  ["1e-999999999999999999999999", "1e-999999999999999999999998", true],
] as const)("步进 %s 对输入 %s 按精确数学值校验", (step, value, expected) => {
  const schema = parseJson(
    `{"type":"object","properties":{"value":{"type":"number","multipleOf":${step}}}}`,
  ) as object;
  const before = stringifyJson(schema);
  const validate = createValidator().compile(schema);
  expect(validate(parseJson(`{"value":${value}}`))).toBe(expected);
  if (!expected)
    expect(validate.errors).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          keyword: "multipleOf",
          instancePath: "/value",
        }),
      ]),
    );
  expect(stringifyJson(schema)).toBe(before);
});

it.each(["0", "-0.1", "-1e-999"])("非法步进 %s 仍作为规则错误拒绝", (step) => {
  expect(() =>
    createValidator().compile(
      parseJson(`{"type":"number","multipleOf":${step}}`) as object,
    ),
  ).toThrow();
});

it.each(["finite", "independent"] as const)(
  "%s 目录保留合法小数步进选项",
  (kind) => {
    const schema = parseJson(
      `{"type":"object","additionalProperties":false,"properties":{"type":{"const":"test"},"choice":{"type":"number","enum":[0.3,0.4],"multipleOf":0.1}${kind === "independent" ? ',"other":{"type":"object"}' : ""}},"required":["type","choice"]}`,
    ) as Record<string, unknown>;
    const parameter: ParameterType = {
      type: "test",
      name: "测试",
      description: "",
      preview_supported: false,
      schema,
    };
    const options = parameterOptions(parameter);
    expect(options.kind).toBe(kind);
    if (options.kind !== "unavailable")
      expect(
        options.fields.find((field) => field.name === "choice")?.values,
      ).toEqual([0.3, 0.4]);
    if (options.kind === "finite") expect(options.rows).toHaveLength(2);
  },
);

it.each([
  '{"type":"array","items":{"type":"number","multipleOf":0.1}}',
  '{"type":"array","prefixItems":[{"$ref":"#/$defs/value"}],"$defs":{"value":{"type":"number","multipleOf":0.1}}}',
])("数组与引用的数值步进按原词元判定 %s", (text) => {
  const validate = createValidator().compile(parseJson(text) as object);
  expect(validate(parseJson("[0.3]"))).toBe(true);
  expect(validate(parseJson("[0.3000000000000000000001]"))).toBe(false);
});

it("根标量的显式原数字用于精确步进", () => {
  const validate = createValidator().compile({
    type: "number",
    multipleOf: 0.1,
  });
  expect(validate.call({ numberToken: "0.3" }, 0.3)).toBe(true);
  expect(validate.call({ numberToken: "0.3000000000000000000001" }, 0.3)).toBe(
    false,
  );
});

it("条件步进使用实际原数值选择分支", () => {
  const schema = parseJson(
    '{"type":"object","properties":{"value":{"type":"number"},"flag":{"type":"boolean"}},"if":{"properties":{"value":{"multipleOf":0.1}}},"then":{"properties":{"flag":{"const":true}}},"else":{"properties":{"flag":{"const":false}}}}',
  ) as object;
  const validate = createValidator().compile(schema);
  expect(validate(parseJson('{"value":0.3,"flag":true}'))).toBe(true);
  expect(
    validate(parseJson('{"value":0.3000000000000000000001,"flag":false}')),
  ).toBe(true);
});

it("步进诊断经规范化与序列化保留极小原除数", () => {
  const validate = createValidator().compile(
    parseJson(
      '{"type":"object","properties":{"value":{"type":"number","multipleOf":3e-999}}}',
    ) as object,
  );
  expect(validate(parseJson('{"value":1e-999}'))).toBe(false);
  const text = stringifyJson(schemaIssues(validate.errors));
  expect(text).toMatch(/"multipleOf":3e-999\s*[,}]/);
  expect(stringifyJson(parseJson(text))).toBe(text);
});
