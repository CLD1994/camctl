import { expect, it } from "vitest";
import {
  parameterOptions,
  compatibleValues,
  optionLabel,
  parameterRequired,
} from "../../src/web/parameter-options";
import { DIALECT } from "../../src/shared/validation";
import type { ParameterType } from "../../src/shared/types";

const parameter = (): ParameterType => ({
  type: "demo",
  name: "测试录像",
  description: "组合测试",
  schema: {
    $schema: DIALECT,
    type: "object",
    additionalProperties: false,
    properties: {
      type: { const: "demo" },
      resolution: { type: "string", enum: ["4K", "1080p"] },
      fps: { $ref: "#/$defs/fps" },
    },
    required: ["type", "resolution", "fps"],
    $defs: { fps: { type: "integer", enum: [30, 60] } },
    if: {
      properties: { resolution: { const: "4K" } },
      required: ["resolution"],
    },
    then: { properties: { fps: { const: 30 } } },
  },
});
it("完整条件规则只生成三个合法组合", () => {
  const result = parameterOptions(parameter());
  expect(result.kind).toBe("finite");
  if (result.kind === "finite")
    expect(result.rows).toEqual([
      { type: "demo", resolution: "4K", fps: 30 },
      { type: "demo", resolution: "1080p", fps: 30 },
      { type: "demo", resolution: "1080p", fps: 60 },
    ]);
});
it.each([
  [{ type: "demo" }, "fps", [30, 60]],
  [{ type: "demo", resolution: "4K" }, "fps", [30]],
  [{ type: "demo", fps: 60 }, "resolution", ["1080p"]],
  [{ type: "demo", resolution: "4K", fps: 60 }, "resolution", ["1080p"]],
  [{ type: "demo", resolution: "4K", fps: 60 }, "fps", [30]],
  [{ type: "demo", fps: "30" }, "resolution", []],
  [{ type: "demo", extra: true }, "fps", []],
] as const)("保留其他已填值计算兼容选项 %j %s", (params, field, expected) => {
  const before = structuredClone(params);
  expect(
    compatibleValues(parameterOptions(parameter()), params, field),
  ).toEqual(expected);
  expect(params).toEqual(before);
});
it("可选参数的缺省、null、false、0和空字符串分别保存", () => {
  const p = parameter();
  p.schema = {
    $schema: DIALECT,
    type: "object",
    additionalProperties: false,
    properties: {
      type: { const: "demo" },
      value: { enum: [null, false, 0, ""] },
    },
    required: ["type"],
  };
  const result = parameterOptions(p);
  expect(result.kind).toBe("finite");
  if (result.kind === "finite")
    expect(result.rows).toEqual([
      { type: "demo" },
      { type: "demo", value: null },
      { type: "demo", value: false },
      { type: "demo", value: 0 },
      { type: "demo", value: "" },
    ]);
});
it("引用旁的约束仍按交集校验", () => {
  const p = parameter();
  (p.schema.properties as any).fps = { $ref: "#/$defs/fps", enum: [60, 120] };
  expect(
    compatibleValues(parameterOptions(p), { type: "demo" }, "fps"),
  ).toEqual([60]);
});
it("互斥分支和依赖约束使用完整规则", () => {
  const p = parameter();
  p.schema.oneOf = [
    { properties: { fps: { const: 30 } } },
    { properties: { resolution: { const: "1080p" } } },
  ];
  p.schema.dependentRequired = { fps: ["resolution"] };
  const result = parameterOptions(p);
  if (result.kind !== "finite") throw new Error("应为有限目录");
  expect(result.rows).toEqual([
    { type: "demo", resolution: "4K", fps: 30 },
    { type: "demo", resolution: "1080p", fps: 60 },
  ]);
});
it("合法规则但没有合法组合与不可枚举区分", () => {
  const p = parameter();
  p.schema.not = {};
  expect(parameterOptions(p)).toMatchObject({ kind: "finite", rows: [] });
});
it("不可枚举目录不被解释为没有兼容选项或非必填", () => {
  const p = parameter();
  delete p.schema.additionalProperties;
  const catalog = parameterOptions(p);
  expect(() => compatibleValues(catalog, { type: "demo" }, "fps")).toThrow();
  expect(() => parameterRequired(catalog, { type: "demo" }, "fps")).toThrow();
});
it.each(["open", "unbounded", "limit"])(
  "不能完整生成时明确返回%s",
  (reason) => {
    const p = parameter();
    if (reason === "open") delete p.schema.additionalProperties;
    if (reason === "unbounded")
      (p.schema.properties as any).free = { type: "string" };
    if (reason === "limit")
      (p.schema.properties as any).many = {
        enum: Array.from({ length: 1500 }, (_, i) => i),
      };
    expect(parameterOptions(p)).toMatchObject({ kind: "unavailable", reason });
  },
);
it("对象枚举按JSON值比较，不依赖对象键顺序", () => {
  const p = parameter();
  (p.schema.properties as any).config = { enum: [{ a: 1, b: 2 }] };
  expect(
    compatibleValues(
      parameterOptions(p),
      { type: "demo", config: { b: 2, a: 1 } },
      "fps",
    ),
  ).toEqual([30, 60]);
});
it("条件必填随其他选择变化，无法兼容时仍保留根必填要求", () => {
  const p = parameter();
  p.schema.required = ["type", "resolution"];
  p.schema.then = { required: ["fps"], properties: { fps: { const: 30 } } };
  const catalog = parameterOptions(p);
  expect(
    parameterRequired(catalog, { type: "demo", resolution: "4K" }, "fps"),
  ).toBe(true);
  expect(
    parameterRequired(catalog, { type: "demo", resolution: "1080p" }, "fps"),
  ).toBe(false);
  expect(
    parameterRequired(catalog, { type: "demo", fps: "invalid" }, "resolution"),
  ).toBe(true);
});
it.each([
  ["4K", "4K"],
  [30, "30"],
  [false, "否"],
  [null, "null"],
  ["", "空字符串"],
])("选项标签保持值含义 %j", (value, expected) => {
  expect(optionLabel(value)).toBe(expected);
});
