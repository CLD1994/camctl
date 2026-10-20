import { expect, it } from "vitest";
import { createValidator, DIALECT } from "../../src/shared/validation";
import { parseJson, stringifyJson } from "../../src/shared/json";
import {
  loadCapabilities,
  validateParams,
} from "../../src/shared/capabilities";
import { parameterOptions } from "../../src/web/parameter-options";
import { objectBranches } from "../../src/web/object-branches";

const branches = () =>
  Array.from({ length: 256 }, (_, index) => ({
    const: `v${index}`,
    title: `标题 ${index}`,
  }));
it("大型纯常量 oneOf 保持原规则及其他局部断言的交集", () => {
  const schema = {
    type: "string",
    oneOf: branches(),
    enum: ["v0", "outside"],
    allOf: [{ minLength: 2 }],
  };
  const before = stringifyJson(schema);
  const check = createValidator().compile(schema);
  expect(check("v0")).toBe(true);
  expect(check("v1")).toBe(false);
  expect(check("outside")).toBe(false);
  expect(stringifyJson(schema)).toBe(before);
});
it("大型 oneOf 中重复常量不能通过恰好一个分支", () => {
  const options = branches();
  options[1].const = "v0";
  const check = createValidator().compile({ type: "string", oneOf: options });
  expect(check("v0")).toBe(false);
  expect(check("v2")).toBe(true);
  expect(check("v1")).toBe(false);
});
it("大型 oneOf 全部身份重复时仍无合法值", () => {
  const check = createValidator().compile({
    oneOf: Array.from({ length: 256 }, () => ({ const: 1 })),
  });
  expect(check(1)).toBe(false);
  expect(check(2)).toBe(false);
});
it("大型命名常量编译保持相同 Number 投影下的独立精确身份", () => {
  const options = branches() as unknown[];
  options[0] = parseJson('{"const":1.0000000000000001,"title":"细微差值"}');
  options[1] = { const: 1, title: "整数" };
  const check = createValidator().compile({
    type: "object",
    properties: { value: { oneOf: options } },
    required: ["value"],
  });
  expect(check(parseJson('{"value":1.0000000000000001}'))).toBe(true);
  expect(check({ value: 1 })).toBe(true);
  expect(check(parseJson('{"value":1.0000000000000002}'))).toBe(false);
});
it("分支中的额外断言仍参加完整 oneOf 校验", () => {
  const options = branches().map((branch, index) => ({
    ...branch,
    ...(index === 0 ? { pattern: "^other" } : {}),
  }));
  const check = createValidator().compile({ type: "string", oneOf: options });
  expect(check("v0")).toBe(false);
  expect(check("v1")).toBe(true);
});
it("纯常量编译前检查原 Schema 定义，不隐藏非法说明元数据", () => {
  const options: unknown[] = branches();
  options[0] = { const: "v0", title: 5 };
  expect(() => createValidator().compile({ oneOf: options })).toThrow();
});

it("同一大型常量 Schema 重复编译保持 Ajv 的缓存和资源身份", () => {
  const schema = { $id: "urn:test:titled-choice", oneOf: branches() };
  const validator = createValidator();
  const first = validator.compile(schema);
  expect(validator.compile(schema)).toBe(first);
  expect(first("v0")).toBe(true);
});

it("指向常量分支本身的本地引用仍能找到原 Schema 节点", () => {
  const schema = {
    type: "object",
    $defs: { choice: { oneOf: branches() } },
    properties: {
      value: { $ref: "#/$defs/choice" },
      specific: { $ref: "#/$defs/choice/oneOf/0" },
    },
    required: ["value", "specific"],
  };
  const check = createValidator().compile(schema);
  expect(check({ value: "v1", specific: "v0" })).toBe(true);
  expect(check({ value: "v1", specific: "v1" })).toBe(false);
});

function assertedBranches(size = 4096) {
  return Array.from({ length: size }, (_, index) => ({
    const: String(index),
    title: `档位 ${index}`,
    type: "string",
  }));
}
function assertedParameter(size: number) {
  return {
    type: "asserted_photo",
    name: "局部断言目录",
    description: "通用断言边界验证",
    preview_supported: false,
    schema: {
      $schema: DIALECT,
      type: "object",
      additionalProperties: false,
      properties: {
        type: { const: "asserted_photo" },
        value: { type: "string", oneOf: assertedBranches(size) },
      },
      required: ["type", "value"],
    },
  };
}
function assertedCapabilities(size: number) {
  return {
    devices: [
      {
        device_id: "camera",
        driver_id: "test",
        actions: [
          {
            type: "camera_take_photo",
            parameter_types: [assertedParameter(size)],
          },
        ],
      },
    ],
  };
}
it.each([4096, 4097])("附带局部 type 的 %s 常量分支可完整编译", (size) => {
  const parameter = assertedParameter(size);
  const check = createValidator().compile(parameter.schema);
  expect(check({ type: "asserted_photo", value: String(size - 1) })).toBe(true);
  expect(check({ type: "asserted_photo", value: "outside" })).toBe(false);
});
it.each([4096, 4097])(
  "真实 loadCapabilities 接受 %s 个局部断言常量并维持候选边界",
  (size) => {
    const capabilities = loadCapabilities(assertedCapabilities(size));
    const parameter = capabilities.devices[0].actions[0].parameter_types[0];
    expect(
      validateParams(
        "camera",
        "camera_take_photo",
        { type: "asserted_photo", value: String(size - 1) },
        capabilities,
      ),
    ).toEqual([]);
    const catalog = parameterOptions(parameter);
    expect(catalog.kind).toBe(size === 4096 ? "finite" : "independent");
    if (catalog.kind !== "unavailable")
      expect(
        catalog.fields.find((field) => field.name === "value")?.values.length,
      ).toBe(size === 4096 ? 4096 : undefined);
  },
);
it("大型常量目录逐分支执行整个局部标量断言子集", () => {
  const cases = [
    ['{"const":"typed","type":"string"}', '"typed"', true],
    ['{"const":"wrong-type","type":"number"}', '"wrong-type"', false],
    ['{"const":10,"minimum":10,"maximum":10}', "10", true],
    ['{"const":11,"minimum":12}', "11", false],
    ['{"const":12,"maximum":11}', "12", false],
    ['{"const":13,"exclusiveMinimum":12,"exclusiveMaximum":14}', "13", true],
    ['{"const":14,"exclusiveMinimum":14}', "14", false],
    ['{"const":15,"exclusiveMaximum":15}', "15", false],
    ['{"const":0.3,"multipleOf":0.1}', "0.3", true],
    [
      '{"const":0.30000000000000001,"multipleOf":0.1}',
      "0.30000000000000001",
      false,
    ],
    ['{"const":"ab","minLength":2,"maxLength":2}', '"ab"', true],
    ['{"const":"a","minLength":2}', '"a"', false],
    ['{"const":"abc","maxLength":2}', '"abc"', false],
    ['{"const":"yes-pattern","pattern":"^yes"}', '"yes-pattern"', true],
    ['{"const":"no-pattern","pattern":"^yes"}', '"no-pattern"', false],
    ['{"const":"2026-10-21","format":"date"}', '"2026-10-21"', true],
    ['{"const":"not-a-date","format":"date"}', '"not-a-date"', false],
    ['{"const":"enum-in","enum":["enum-in"]}', '"enum-in"', true],
    ['{"const":"enum-out","enum":["other"]}', '"enum-out"', false],
  ] as const;
  const options = cases.map(([branch]) => parseJson(branch));
  options.push(
    ...Array.from({ length: 4096 - options.length }, (_, index) => ({
      const: `padding-${index}`,
      type: "string",
    })),
  );
  const schema = {
    type: "object",
    properties: { value: { oneOf: options } },
    required: ["value"],
  };
  const original = stringifyJson(schema);
  const check = createValidator().compile(schema);
  for (const [, value, expected] of cases)
    expect(check(parseJson(`{"value":${value}}`)), value).toBe(expected);
  expect(stringifyJson(schema)).toBe(original);
});
it("重复精确身份按实际满足自身断言的零一多个分支计数", () => {
  const options = [
    '{"const":"zero","type":"number"}',
    '{"const":"zero","pattern":"^other"}',
    '{"const":"one","type":"string"}',
    '{"const":"one","minLength":10}',
    '{"const":"multi","type":"string"}',
    '{"const":"multi","enum":["multi"]}',
    '{"const":1.0,"minimum":2}',
    '{"const":1e0,"minimum":1}',
    '{"const":2.0,"minimum":2}',
    '{"const":2e0,"maximum":2}',
    '{"const":1.0000000000000001,"exclusiveMinimum":1.00000000000000005}',
    '{"const":1.0000000000000002,"maximum":1.00000000000000015}',
    '{"const":1.0000000000000003,"type":"integer"}',
  ].map((text) => parseJson(text));
  options.push(
    ...Array.from({ length: 4096 - options.length }, (_, index) => ({
      const: `padding-${index}`,
      type: "string",
    })),
  );
  const check = createValidator().compile({
    type: "object",
    properties: { value: { oneOf: options } },
    required: ["value"],
  });
  for (const [value, expected] of [
    ['"zero"', false],
    ['"one"', true],
    ['"multi"', false],
    ["1", true],
    ["2", false],
    ["1.0000000000000001", true],
    ["1.0000000000000002", false],
    ["1.0000000000000003", false],
  ] as const)
    expect(check(parseJson(`{"value":${value}}`)), value).toBe(expected);
});
it("局部断言大目录继续与原 enum/allOf 取交集", () => {
  const schema = {
    type: "string",
    oneOf: assertedBranches(),
    enum: ["2", "3", "outside"],
    allOf: [{ not: { const: "3" } }],
  };
  const check = createValidator().compile(schema);
  expect(check("2")).toBe(true);
  for (const value of ["3", "4", "outside"]) expect(check(value)).toBe(false);
});
it.each([
  { minimum: "bad" },
  { minLength: -1 },
  { type: "invalid" },
  { format: 4 },
])("局部断言压缩不隐藏非法原 Schema %j", (rule) => {
  const options: unknown[] = assertedBranches();
  options[0] = { const: "0", ...rule };
  expect(() => createValidator().compile({ oneOf: options })).toThrow();
});
it.each([4096, 4097])(
  "对象分支成员的 %s 档局部断言同时遵守完整校验和控件资格",
  (size) => {
    const parameter = assertedParameter(size);
    parameter.schema.properties = {
      type: { const: "asserted_photo" },
      setting: {
        oneOf: [
          {
            type: "object",
            additionalProperties: false,
            properties: {
              mode: { const: "manual" },
              value: { type: "string", oneOf: assertedBranches(size) },
            },
            required: ["mode", "value"],
          },
          {
            type: "object",
            additionalProperties: false,
            properties: { mode: { const: "auto" } },
            required: ["mode"],
          },
        ],
      },
    } as any;
    parameter.schema.required = ["type", "setting"];
    const check = createValidator().compile(parameter.schema);
    expect(
      check({
        type: "asserted_photo",
        setting: { mode: "manual", value: String(size - 1) },
      }),
    ).toBe(true);
    const branches = objectBranches(parameter, "setting");
    expect(branches?.branches[0].members[0].choices?.length).toBe(
      size === 4096 ? 4096 : undefined,
    );
  },
);

it("能力加载仍拒绝未登记的 format，不把它当作用户值错误", () => {
  const capabilities = assertedCapabilities(4096);
  (
    capabilities.devices[0].actions[0].parameter_types[0].schema.properties
      .value.oneOf[0] as any
  ).format = "unsupported";
  expect(() => loadCapabilities(capabilities)).toThrow(/unsupported/);
});
it("纯常量中的布尔、null、对象和数组保持原精确 JSON 身份", () => {
  const options: unknown[] = [
    { const: true, type: "boolean" },
    { const: null, type: "null" },
    parseJson('{"const":{"nested":1.0000000000000001}}'),
    parseJson('{"const":[0.30000000000000001]}'),
  ];
  options.push(
    ...Array.from({ length: 4096 - options.length }, (_, index) => ({
      const: `padding-${index}`,
      type: "string",
    })),
  );
  const check = createValidator().compile({
    type: "object",
    properties: { value: { oneOf: options } },
    required: ["value"],
  });
  for (const value of [
    "true",
    "null",
    '{"nested":1.0000000000000001}',
    "[0.30000000000000001]",
  ])
    expect(check(parseJson(`{"value":${value}}`))).toBe(true);
  for (const value of ["false", '{"nested":1}', "[0.3]"])
    expect(check(parseJson(`{"value":${value}}`))).toBe(false);
});
