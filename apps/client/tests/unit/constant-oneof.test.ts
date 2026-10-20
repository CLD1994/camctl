import { expect, it } from "vitest";
import { createValidator } from "../../src/shared/validation";
import { parseJson, stringifyJson } from "../../src/shared/json";

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
