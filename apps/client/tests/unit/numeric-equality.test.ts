import { expect, it } from "vitest";
import { parseJson, stringifyJson } from "../../src/shared/json";
import { createValidator, schemaIssues } from "../../src/shared/validation";
import { hasNumericLoss } from "../../src/web/parameter-options";

// 这些字面量分别证明数学等值、近邻差异和 JSON 类型边界；不从身份函数派生预期。
for (const keyword of ["enum", "const"] as const) {
  const rule = (value: string) =>
    `"${keyword}":${keyword === "enum" ? `[${value}]` : value}`;

  it.each([
    ["0.5", "0.5", true],
    ["0.5", "0.50", true],
    ["0.5", "5e-1", true],
    ["-0.0", "0e1000", true],
    ["0.5", "0.50000000000000001", false],
    ["0.5", "0.49999999999999999", false],
    ["1", "1.0000000000000001", false],
    ["0", "1e-999", false],
    ["1e-999", "0", false],
    ["0.50000000000000001", "0.5", false],
    ["0.50000000000000001", "0.50000000000000001", true],
    ["0.5", '"0.5"', false],
    ["1", "true", false],
    ["0", "false", false],
    ["0", "null", false],
    ['"1"', '"1"', true],
    ["true", "true", true],
    ["false", "false", true],
    ["null", "null", true],
  ] as const)(
    `${keyword} 属性允许 %s 时输入 %s 的结论为 %s`,
    (allowed, value, expected) => {
      const schema = parseJson(
        `{"properties":{"value":{${rule(allowed)}}}}`,
      ) as object;
      const data = parseJson(`{"value":${value}}`);
      const beforeSchema = stringifyJson(schema);
      const beforeData = stringifyJson(data);
      const validate = createValidator().compile(schema);
      expect(validate(data)).toBe(expected);
      if (!expected)
        expect(schemaIssues(validate.errors)).toEqual(
          expect.arrayContaining([
            expect.objectContaining({
              code: `schema_${keyword}`,
              pointer: "/value",
            }),
          ]),
        );
      expect(stringifyJson(schema)).toBe(beforeSchema);
      expect(stringifyJson(data)).toBe(beforeData);
    },
  );

  it.each([
    ['{"a":0.5,"b":[1,null,true]}', '{"b":[1.0,null,true],"a":5e-1}', true],
    ['{"a":0.5}', '{"a":0.50000000000000001}', false],
    ['{"a":[{"b":1}]}', '{"a":[{"b":1.0000000000000001}]}', false],
    ["[0.5,1]", "[1,0.5]", false],
    ["[0.5,1]", "[0.50,1e0]", true],
    ["[0.5]", "[0.5,1]", false],
    ['{"a":null}', "{}", false],
    ['{"a":1}', '{"a":1,"b":2}', false],
  ] as const)(
    `${keyword} 根复合值按 JSON 身份比较 %s 与 %s`,
    (allowed, input, expected) => {
      const schema = parseJson(`{${rule(allowed)}}`) as object;
      const data = parseJson(input);
      const beforeSchema = stringifyJson(schema);
      const beforeData = stringifyJson(data);
      const validate = createValidator().compile(schema);
      expect(validate(data)).toBe(expected);
      expect(stringifyJson(schema)).toBe(beforeSchema);
      expect(stringifyJson(data)).toBe(beforeData);
    },
  );

  it.each([
    ["0.5", "0.50", true],
    ["0.5", "0.50000000000000001", false],
    ["1", "1.0000000000000001", false],
    ["0", "1e-999", false],
    ["1e-999", "1e-999", true],
  ] as const)(
    `${keyword} 根标量显式上下文：%s 与 %s`,
    (allowed, token, expected) => {
      const validate = createValidator().compile(
        parseJson(`{${rule(allowed)}}`) as object,
      );
      expect(validate.call({ numberToken: token }, Number(token))).toBe(
        expected,
      );
    },
  );

  it.each([false, true])(
    `${keyword} 数组成员的本地引用=%s 保留词元`,
    (reference) => {
      const schema = parseJson(
        `{"type":"array","items":${reference ? '{"$ref":"#/$defs/value"}' : `{${rule("0.5")}}`},"$defs":{"value":{${rule("0.5")}}}}`,
      ) as object;
      const validate = createValidator().compile(schema);
      expect(validate(parseJson("[5e-1]"))).toBe(true);
      expect(validate(parseJson("[0.50000000000000001]"))).toBe(false);
      expect(validate.errors).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ keyword, instancePath: "/0" }),
        ]),
      );
    },
  );

  it(`${keyword} 条件按原数值选择 then 与 else`, () => {
    const schema = parseJson(
      `{"if":{"properties":{"value":{${rule("0.5")}}},"required":["value"]},"then":{"properties":{"flag":{"const":true}}},"else":{"properties":{"flag":{"const":false}}}}`,
    ) as object;
    const validate = createValidator().compile(schema);
    expect(validate(parseJson('{"value":0.50,"flag":true}'))).toBe(true);
    expect(
      validate(parseJson('{"value":0.50000000000000001,"flag":false}')),
    ).toBe(true);
    expect(
      validate(parseJson('{"value":0.50000000000000001,"flag":true}')),
    ).toBe(false);
    expect(validate(parseJson('{"flag":false}'))).toBe(true);
  });

  it.each(["1e-999", '{"x":[0.50000000000000001]}'])(
    `${keyword} 诊断保留精确允许值 %s`,
    (allowed) => {
      const validate = createValidator().compile(
        parseJson(`{"properties":{"value":{${rule(allowed)}}}}`) as object,
      );
      expect(validate({ value: null })).toBe(false);
      const issues = schemaIssues(validate.errors);
      expect(issues[0]).toMatchObject({
        code: `schema_${keyword}`,
        schema: { keyword, schemaPath: `#/properties/value/${keyword}` },
      });
      const params = stringifyJson(issues[0].schema!.params);
      expect(params).toBe(
        keyword === "enum"
          ? `{"allowedValues":[${allowed}]}`
          : `{"allowedValue":${allowed}}`,
      );
      expect(stringifyJson(parseJson(params))).toBe(params);
    },
  );

  it(`${keyword} 的高精度规则仍受原控件资格限制`, () => {
    const schema = parseJson(`{${rule("0.50000000000000001")}}`) as Record<
      string,
      unknown
    >;
    expect(hasNumericLoss(schema)).toBe(true);
  });
}

it("混合枚举匹配任一原值，不接受其舍入投影", () => {
  const validate = createValidator().compile(
    parseJson('{"enum":[0.50000000000000001,1,"0.5",null]}') as object,
  );
  expect(validate.call({ numberToken: "0.5" }, 0.5)).toBe(false);
  expect(validate.call({ numberToken: "0.50000000000000001" }, 0.5)).toBe(true);
  expect(validate(1)).toBe(true);
  expect(validate("0.5")).toBe(true);
  expect(validate(null)).toBe(true);
  expect(validate(true)).toBe(false);
});

it("精确关键字仍拒绝空枚举规则", () => {
  expect(() => createValidator().compile({ enum: [] })).toThrow();
});
