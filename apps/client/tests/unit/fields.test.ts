import { expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Field, JsonField } from "../../src/web/Fields";
import { SchemaFields } from "../../src/web/Editor";
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
  [{ exclusiveMinimum: 0 }, "大于 0"],
  [{ exclusiveMaximum: 70 }, "小于 70"],
])("数字字段说明呈现严格边界 %j", (bounds, expected) => {
  const html = renderToStaticMarkup(
    createElement(Field, {
      ...props,
      schema: { type: "number", ...bounds },
      content: content("70"),
    }),
  );
  expect(html).toContain(expected);
});
it("严格数字边界显示原词元而非Number投影", () => {
  const schema = parseJson(
    '{"type":"number","exclusiveMinimum":1.0000000000000001,"exclusiveMaximum":2.0000000000000001}',
  ) as Record<string, unknown>;
  const html = renderToStaticMarkup(
    createElement(Field, { ...props, schema, content: content("2") }),
  );
  expect(html).toContain("大于 1.0000000000000001");
  expect(html).toContain("小于 2.0000000000000001");
});
it("普通文本字段不把数值断言说成文本必须满足的边界", () => {
  const html = renderToStaticMarkup(
    createElement(Field, {
      ...props,
      schema: { type: "string", exclusiveMinimum: 0 },
      content: content('"文本"'),
    }),
  );
  expect(html).not.toContain("必须大于");
});
it("独立空候选说明字段规则无允许值，不指向其他参数冲突", () => {
  const html = renderToStaticMarkup(
    createElement(Field, {
      ...props,
      schema: {},
      choices: [],
      content: content("1"),
      choiceBasis: "independent",
    }),
  );
  expect(html).toContain("没有允许值");
  expect(html).not.toContain("清空冲突字段");
  expect(html).toContain("设备说明");
});
it.each(["1e-999", token])(
  "选择控件将原数学值 %s 显示为待修正而非舍入选项",
  (value) => {
    const html = renderToStaticMarkup(
      createElement(Field, {
        ...props,
        schema: {},
        choices: [0, 1],
        content: content(value),
      }),
    );
    expect(html).toContain('data-value="invalid"');
    expect(html).toContain(value);
  },
);
it("缺省选择不写入首项或 Schema 默认值", () => {
  const html = renderToStaticMarkup(
    createElement(Field, {
      ...props,
      schema: { default: "a" },
      choices: ["a", "b"],
      content: { text: '{"actions":[]}' },
    }),
  );
  expect(html).toContain('data-value=""');
  expect(html).not.toContain('data-value="0"');
});
it("字段自身 pending 暂停选择且显示保留的原文", () => {
  const html = renderToStaticMarkup(
    createElement(Field, {
      ...props,
      schema: {},
      choices: [0, 1],
      content: {
        ...content("1"),
        pending: { "/value": { kind: "number", text: "1e" } },
      },
    }),
  );
  expect(html).toContain("disabled");
  expect(html).toContain('data-value="invalid"');
  expect(html).toContain("1e");
});
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

function schemaFieldsMarkup(schema: Record<string, unknown>) {
  return renderToStaticMarkup(
    createElement(SchemaFields, {
      parameter: {
        type: "reference_record",
        name: "参考码率录像",
        description: "",
        preview_supported: false,
        schema,
      },
      content: {
        text: '{"actions":[{"params":{"type":"reference_record","bitrate_mbps":2}}]}',
      },
      path: ["actions", 0, "params"],
      change: () => {},
    }),
  );
}
it.each([false, true])(
  "真实表单说明解析保持直接及引用边界词元：ref=%s",
  (reference) => {
    const schema = parseJson(
      reference
        ? '{"type":"object","$defs":{"rate":{"type":"number","exclusiveMinimum":1.0000000000000001,"exclusiveMaximum":9.0000000000000001}},"properties":{"type":{"const":"reference_record"},"bitrate_mbps":{"$ref":"#/$defs/rate","exclusiveMaximum":2.0000000000000001}}}'
        : '{"type":"object","properties":{"type":{"const":"reference_record"},"bitrate_mbps":{"type":"number","exclusiveMinimum":1.0000000000000001,"exclusiveMaximum":2.0000000000000001}}}',
    ) as Record<string, unknown>;
    const html = schemaFieldsMarkup(schema);
    expect(html).toContain("大于 1.0000000000000001");
    expect(html).toContain("小于 2.0000000000000001");
    expect(html).not.toContain("小于 9.0000000000000001");
  },
);
it("引用旁裸值覆盖同Number投影时不恢复目标旧边界词元", () => {
  const schema = parseJson(
    '{"type":"object","$defs":{"rate":{"type":"number","exclusiveMaximum":2.0000000000000001}},"properties":{"type":{"const":"reference_record"}}}',
  ) as any;
  schema.properties.bitrate_mbps = {
    $ref: "#/$defs/rate",
    exclusiveMaximum: 2,
  };
  const html = schemaFieldsMarkup(schema);
  expect(html).toContain("必须小于 2。");
  expect(html).not.toContain("小于 2.0000000000000001");
});
