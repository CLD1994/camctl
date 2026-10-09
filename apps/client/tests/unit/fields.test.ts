import { expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Field, JsonField } from "../../src/web/Fields";
import { editValue, setValue } from "../../src/web/editing";
import { createValidator } from "../../src/shared/validation";
import { parseJson } from "../../src/shared/json";
const token = "1.0000000000000001";
const content = (value = token) => ({
  text: `{"value":${value},"actions":[]}`,
});
const path = ["value"];
const props = { name: "value", required: true, path, change: () => {} };
it.each([
  { oneOf: [{ type: "integer" }, { type: "number" }] },
  {},
  { type: "number", const: 1 },
])("Field JSON 分支显示父容器原词元 %#", (schema) => {
  expect(
    renderToStaticMarkup(
      createElement(Field, {
        ...props,
        schema,
        content: content(),
        jsonChoices: true,
      }),
    ),
  ).toContain(`>${token}</textarea>`);
});
it.each([token, "-1e-400", '{"nested":' + token + "}", "[" + token + "]"])(
  "JsonField 保留根片段和内部词元 %s",
  (value) => {
    expect(
      renderToStaticMarkup(
        createElement(JsonField, {
          ...props,
          label: "值",
          content: content(value),
        }),
      ),
    ).toContain(value.includes(token) ? token : value);
  },
);
it.each(["1.0", "1e0", "1", "1.5", "true", "null"])(
  "JsonField 显示等价数值或原 JSON 类型 %s",
  (value) => {
    expect(
      renderToStaticMarkup(
        createElement(JsonField, {
          ...props,
          label: "值",
          content: content(value),
        }),
      ),
    ).toContain(`>${JSON.stringify(JSON.parse(value))}</textarea>`);
  },
);
it("JsonField 字符串仍使用 JSON 编码", () => {
  expect(
    renderToStaticMarkup(
      createElement(JsonField, {
        ...props,
        label: "值",
        content: content('"abc"'),
      }),
    ),
  ).toContain("&quot;abc&quot;</textarea>");
});
it("字段原值提示显示真实数值", () => {
  expect(
    renderToStaticMarkup(
      createElement(Field, {
        ...props,
        schema: { type: "string" },
        content: content(),
      }),
    ),
  ).toContain(token);
});
it("JSON 空白编辑保留非整数，显式同值覆盖清除旧词元", () => {
  const check = createValidator().compile({
    type: "object",
    properties: { value: { not: { type: "integer" } } },
  });
  const edited = editValue(content(), path, ` ${token} `, "json");
  expect(check(parseJson(edited.text))).toBe(true);
  expect(check(parseJson(setValue(edited, path, 1).text))).toBe(false);
});

it("数字数组编辑其他项保留证据，整段删除搬移以新原文索引为准", () => {
  const initial = content(`[0,${token},-1e-400]`);
  const unrelated = setValue(initial, ["value", 0], 2);
  expect(unrelated.text).toContain(token);
  expect(unrelated.text).toContain("-1e-400");
  const moved = editValue(unrelated, ["value"], `[${token},-1e-400]`, "json");
  const check = createValidator().compile({
    type: "object",
    properties: {
      value: { type: "array", items: { not: { type: "integer" } } },
    },
  });
  expect(check(parseJson(moved.text))).toBe(true);
  expect(check(parseJson(setValue(moved, ["value", 0], 1).text))).toBe(false);
});
