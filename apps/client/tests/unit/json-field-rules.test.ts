import { createElement as h } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { JsonFieldRules } from "../../src/web/JsonFieldRules";
import { parseJson, stringifyJson } from "../../src/shared/json";

const render = (schema: Record<string, unknown>) =>
  renderToStaticMarkup(h(JsonFieldRules, { schema }));
const facts = (html: string) => html.split("<details")[0];

it.each([true, false])(
  "布尔 Schema %s 保留完整原值且不派生对象字段",
  (schema) => {
    const html = renderToStaticMarkup(h(JsonFieldRules, { schema }));
    expect(html).toContain(`<pre>${schema}</pre>`);
    expect(facts(html)).not.toContain("<table");
    expect(facts(html)).not.toContain('class="json-rule-branch"');
    expect(html).not.toContain("<pre>{}</pre>");
  },
);

it("说明页普通标量只展示完整原规则，不提示填写 JSON", () => {
  const schema = parseJson(
    '{"type":"integer","maximum":9223372036854775.807}',
  ) as Record<string, unknown>;
  const html = renderToStaticMarkup(
    h(JsonFieldRules, { schema, showEditingHint: false }),
  );
  expect(facts(html)).not.toContain("<p>");
  expect(facts(html)).not.toContain("<table");
  expect(html).toContain("<details");
  expect(html).toContain("<pre>");
  expect(html).toContain("9223372036854775.807");
});

it("说明页对象保留直接事实、限定说明和完整原规则", () => {
  const schema = { type: "object", properties: { mode: { const: "auto" } } };
  const html = renderToStaticMarkup(
    h(JsonFieldRules, { schema, showEditingHint: false }),
  );
  expect(facts(html)).toContain("<table");
  expect(facts(html)).toContain("<code>mode</code>");
  expect(facts(html)).toContain("auto");
  expect(facts(html)).toContain("<p>");
  expect(html).toContain("<details");
});

// 若组件只展示原始 JSON，直接声明的属性、必填标识和局部约束就不会出现在说明表中。
it("直接对象的字段事实来自传入 Schema", () => {
  const html = facts(
    render({
      type: "object",
      required: ["mode", "gain"],
      properties: {
        mode: { type: "string", const: "manual" },
        gain: { type: "number", enum: [1, 2.5], minimum: 1, maximum: 2.5 },
        meter: { type: "string", enum: ["center", "spot"] },
      },
    }),
  );
  for (const field of ["mode", "gain", "meter"])
    expect(html).toContain(`<code>${field}</code>`);
  for (const value of [
    "manual",
    "center",
    "spot",
    "number",
    "string",
    "1",
    "2.5",
  ])
    expect(html).toContain(value);
  expect(html.match(/class="required"/g)).toHaveLength(2);
  expect(html).toContain("<table");
});

// 删除一个分支或把 Schema 内容换成固定的相机字段清单时，此用例失败。
it("直接对象 oneOf 分别展示所有分支的声明", () => {
  const html = facts(
    render({
      oneOf: [
        {
          type: "object",
          required: ["mode", "gain"],
          properties: {
            mode: { const: "manual" },
            gain: { type: "number", minimum: 1 },
          },
        },
        {
          type: "object",
          required: ["mode", "bias"],
          properties: {
            mode: { const: "auto" },
            bias: { type: "integer", enum: [-1, 0, 1] },
          },
        },
      ],
    }),
  );
  expect(html.match(/class="json-rule-branch"/g)).toHaveLength(2);
  expect(html).toContain("oneOf");
  const branches = html.split('class="json-rule-branch"').slice(1);
  expect(branches[0]).toContain("gain");
  expect(branches[0]).not.toContain("bias");
  expect(branches[1]).toContain("bias");
  expect(branches[1]).not.toContain("gain");
  for (const value of ["manual", "auto", "gain", "bias", "integer", "-1"])
    expect(html).toContain(value);
  expect(html.match(/class="required"/g)).toHaveLength(4);
});

it.each([
  {
    $ref: "#/$defs/manual",
    $defs: {
      manual: { type: "object", properties: { secret: { const: "hidden" } } },
    },
  },
  { allOf: [{ type: "object", properties: { secret: { const: "hidden" } } }] },
  { anyOf: [{ type: "object", properties: { secret: { const: "hidden" } } }] },
  {
    oneOf: [
      { type: "object", properties: { mode: { const: "manual" } } },
      { $ref: "#/$defs/auto" },
    ],
  },
])("引用或未覆盖组合仅提供原字段规则 %j", (schema) => {
  const html = render(schema);
  expect(facts(html)).not.toContain("<table");
  expect(facts(html)).not.toContain('class="json-rule-branch"');
  expect(html).toContain("<details");
  expect(html).toContain("<pre>");
  expect(html).toContain(
    schema.$ref ? "#/$defs/manual" : schema.oneOf ? "#/$defs/auto" : "hidden",
  );
});

// 直接数值未带父容器或 JSON 数据先被普通 stringify 舍入时，说明表会丢失这些事实。
it("字段 const enum type 与严格边界保留原 JSON 数值和结构", () => {
  const schema = parseJson(`{"type":"object","properties":{
    "gain":{"type":["number","null"],"minimum":1e-999,"maximum":9223372036854775.807,"exclusiveMinimum":1.0000000000000001,"exclusiveMaximum":9223372036854775.803},
    "choice":{"enum":[1e-999,1.0000000000000001,"1e-999"]},
    "fixed":{"const":{"nested":[9223372036854775.807,1e-999]}},
    "flag":{"enum":[true,false,null]}
  }}`) as Record<string, unknown>;
  const html = facts(render(schema));
  const gain = html
    .split("<tr>")
    .find((row) => row.includes("<code>gain</code>"))!;
  for (const token of [
    "1e-999",
    "1.0000000000000001",
    "9223372036854775.807",
    "9223372036854775.803",
  ])
    expect(gain).toContain(token);
  for (const json of [
    "[&quot;number&quot;,&quot;null&quot;]",
    "[true,false,null]",
    "{&quot;nested&quot;:[9223372036854775.807,1e-999]}",
    "&quot;1e-999&quot;",
  ])
    expect(html).toContain(json);
});

it("查看说明不改输入 Schema 或把 default 和 examples 转成字段内容", () => {
  const schema = parseJson(
    '{"type":"object","properties":{"gain":{"type":"number","default":1.0000000000000001,"examples":[1e-999]}}}',
  ) as Record<string, unknown>;
  const before = stringifyJson(schema);
  const html = render(schema);
  expect(stringifyJson(schema)).toBe(before);
  expect(facts(html)).not.toContain("1.0000000000000001");
  expect(facts(html)).not.toContain("1e-999");
  expect(html).toContain("1.0000000000000001");
  expect(html).toContain("1e-999");
  expect(html).not.toMatch(/<(input|textarea|select)\b/);
});

it("命名标量与对象分支说明消费同源标题并保留机器值", () => {
  const html = facts(
    render({
      oneOf: [
        {
          type: "object",
          title: "手动曝光",
          properties: {
            aperture: {
              type: "number",
              oneOf: [
                { const: 4.8, title: "f/4.8" },
                { const: 8, title: "f/8" },
              ],
            },
          },
        },
      ],
    }),
  );
  expect(html).toContain("手动曝光");
  expect(html).toContain("f/4.8");
  expect(html).toContain("f/8");
});
