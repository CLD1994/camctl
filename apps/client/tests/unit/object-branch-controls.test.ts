import { expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SchemaFields } from "../../src/web/Editor";
import type { DraftContent } from "../../src/server/models";
import type { ParameterType } from "../../src/shared/types";
import { editValue, parseDraft } from "../../src/web/editing";
import { switchObjectBranch } from "../../src/shared/object-variants";
import { objectBranchJsonEdit } from "../../src/web/object-branches";
import { replaceObjectValue } from "../../src/shared/object-variants";

function parameter(): ParameterType {
  return {
    type: "record",
    name: "录像",
    description: "",
    preview_supported: false,
    schema: {
      type: "object",
      additionalProperties: false,
      properties: {
        type: { const: "record" },
        duration_s: { type: "integer", minimum: 1 },
        exposure: {
          oneOf: [
            {
              type: "object",
              additionalProperties: false,
              properties: {
                mode: { const: "manual" },
                iso: { enum: [100, 800] },
              },
              required: ["mode", "iso"],
            },
            {
              type: "object",
              additionalProperties: false,
              properties: {
                mode: { const: "auto" },
                compensation_ev: { enum: [-0.7, 0, 0.7] },
              },
              required: ["mode", "compensation_ev"],
            },
          ],
        },
      },
      required: ["type", "duration_s", "exposure"],
    },
  };
}
function render(
  value = '{"mode":"manual","iso":800}',
  p = parameter(),
  pending?: DraftContent["pending"],
) {
  return renderToStaticMarkup(
    createElement(SchemaFields, {
      parameter: p,
      path: ["actions", 0, "params"],
      change: () => {},
      content: {
        text: `{"actions":[{"type":"camera_record","device_id":"camera","params":{"type":"record","duration_s":10${value === "missing" ? "" : `,"exposure":${value}`}}}]}`,
        ...(pending ? { pending } : {}),
      },
    }),
  );
}
it("合格对象回显判别字段且只提供当前分支成员", () => {
  const html = render();
  expect(html).toContain('aria-label="mode (mode)"');
  expect(html).toContain('aria-label="iso (iso)"');
  expect(html).not.toContain('aria-label="compensation_ev (compensation_ev)"');
  expect(html).not.toContain('aria-label="exposure (exposure)"');
});
it("首次曝光整段JSON替换后回切恢复导入的完整模式", () => {
  const content: DraftContent = {
    text: '{"actions":[{"type":"camera_record","device_id":"camera","params":{"type":"record","duration_s":10,"exposure":{"mode":"manual","iso":800}}}]}',
  };
  const path = ["actions", 0, "params", "exposure"];
  const next = editValue(
    content,
    path,
    '{"mode":"auto","compensation_ev":0.7}',
    "json",
  );
  const updated = objectBranchJsonEdit(content, next, path, "mode");
  const restored = switchObjectBranch(
    updated,
    0,
    ["exposure"],
    "mode",
    "manual",
  );
  expect((parseDraft(restored).actions[0].params as any).exposure).toEqual({
    mode: "manual",
    iso: 800,
  });
});
it("曝光整段JSON未完成时保持Field的原文且不建立旧投影分支", () => {
  const content: DraftContent = {
    text: '{"actions":[{"type":"camera_record","device_id":"camera","params":{"type":"record","exposure":{"mode":"manual","iso":800}}}]}',
  };
  const path = ["actions", 0, "params", "exposure"];
  const next = editValue(content, path, '{"mode":"auto",', "json");
  const updated = objectBranchJsonEdit(content, next, path, "mode");
  expect(updated).toBe(next);
  expect(updated.pending?.["/actions/0/params/exposure"]?.text).toBe(
    '{"mode":"auto",',
  );
  expect(updated.objectVariants).toBeUndefined();
});
it.each([{}, { kind: "manual", iso: 800 }])(
  "目录改变判别字段时正文%j与旧资料通过重填入口呈现",
  (replacement) => {
    const content: DraftContent = {
      text: '{"actions":[{"type":"camera_record","device_id":"camera","params":{"type":"record","exposure":{"mode":"manual","iso":800}}}]}',
    };
    const switched = switchObjectBranch(
      content,
      0,
      ["exposure"],
      "mode",
      "auto",
    );
    const empty = replaceObjectValue(switched, 0, ["exposure"], replacement);
    const p = parameter();
    for (const branch of (p.schema.properties as any).exposure.oneOf) {
      branch.properties.kind = branch.properties.mode;
      delete branch.properties.mode;
      branch.required = branch.required.map((key: string) =>
        key === "mode" ? "kind" : key,
      );
    }
    const html = renderToStaticMarkup(
      createElement(SchemaFields, {
        parameter: p,
        content: empty,
        path: ["actions", 0, "params"],
        change: () => {},
      }),
    );
    expect(html).toContain('aria-label="重新填写exposure"');
    expect(html).not.toContain('aria-label="kind (kind)"');
  },
);
it("判别资料冲突时身份未完成仍优先阻止重填", () => {
  const content: DraftContent = {
    text: '{"actions":[{"type":"camera_record","device_id":"camera","params":{"type":"record","exposure":{}}}]}',
    objectVariants: {
      "0": { "/exposure": { discriminator: "mode", variants: [] } },
    },
    pending: { "/actions/0/device_id": { kind: "json", text: '"cam' } },
  };
  const p = parameter();
  for (const branch of (p.schema.properties as any).exposure.oneOf) {
    branch.properties.kind = branch.properties.mode;
    delete branch.properties.mode;
    branch.required = branch.required.map((key: string) =>
      key === "mode" ? "kind" : key,
    );
  }
  const html = renderToStaticMarkup(
    createElement(SchemaFields, {
      parameter: p,
      content,
      path: ["actions", 0, "params"],
      change: () => {},
    }),
  );
  expect(html).toContain('aria-label="重新填写exposure" disabled');
  expect(html).not.toContain('aria-label="kind (kind)"');
});
it.each(["missing", "{}"])(
  "缺省或空对象 %s 不选择默认模式或首个成员",
  (value) => {
    const html = render(value);
    expect(html).toContain('aria-label="mode (mode)"');
    expect(html).toContain('data-value=""');
    expect(html).not.toContain('aria-label="iso (iso)"');
  },
);
it("自动分支没有额外成员时只展示模式", () => {
  const p = parameter();
  const branch = (p.schema.properties as any).exposure.oneOf[1];
  delete branch.properties.compensation_ev;
  branch.required = ["mode"];
  const html = render('{"mode":"auto"}', p);
  expect(html).toContain('aria-label="mode (mode)"');
  expect(html).not.toContain('aria-label="iso (iso)"');
  expect(html).not.toContain('aria-label="compensation_ev (compensation_ev)"');
});
it.each(['{"mode":"manual"}', '{"mode":"manual","iso":999}'])(
  "可表达但未合法的成员 %s 保留级联修正入口",
  (value) => {
    const html = render(value);
    expect(html).toContain('aria-label="iso (iso)"');
    expect(html).not.toContain('aria-label="重新填写exposure"');
    if (value.includes("999")) {
      expect(html).toContain("999");
      expect(html).toContain('data-value="invalid"');
    }
  },
);
it.each([
  "1",
  "null",
  '"manual"',
  '{"mode":"unknown"}',
  '{"mode":"manual","iso":800,"compensation_ev":0}',
  '{"mode":"manual","iso":{"value":800}}',
])("不可表达原对象 %s 保留JSON并有明确重填入口", (value) => {
  const html = render(value);
  expect(html).toContain('aria-label="exposure (exposure)"');
  expect(html).toContain('aria-label="重新填写exposure"');
  expect(html).not.toContain('aria-label="iso (iso)"');
});
it("子成员未完成原文保持当前分支可见", () => {
  const html = render(undefined, parameter(), {
    "/actions/0/params/exposure/iso": { kind: "number", text: "1e" },
  });
  expect(html).toContain('aria-label="mode (mode)"');
  expect(html).toContain('aria-label="iso (iso)"');
  expect(html).toContain("1e");
});
it.each(["/actions/0/params/exposure", "/actions/0/params/exposure/mode"])(
  "对象或判别未完成输入 %s 不读取旧模式",
  (path) => {
    const html = render(undefined, parameter(), {
      [path]: { kind: "json", text: '{"mode":' },
    });
    expect(html).toContain('aria-label="重新填写exposure"');
    expect(html).not.toContain('aria-label="iso (iso)"');
  },
);
it("根跨字段组合无法由现有目录证明时保留JSON", () => {
  const p = parameter();
  p.schema.allOf = [
    {
      if: { properties: { duration_s: { const: 10 } } },
      then: { required: ["exposure"] },
    },
  ];
  const html = render(undefined, p);
  expect(html).toContain('aria-label="exposure (exposure)"');
  expect(html).not.toContain('aria-label="mode (mode)"');
});
it("成员数字按原数学值保留待修正而非匹配舍入值", () => {
  const html = render('{"mode":"auto","compensation_ev":1e-999}');
  expect(html).toContain('aria-label="compensation_ev (compensation_ev)"');
  expect(html).toContain("1e-999");
  expect(html).toContain('data-value="invalid"');
});
it("资格无法证明的局部分支保持高级JSON", () => {
  const p = parameter();
  (p.schema.properties as any).exposure.oneOf[0].allOf = [
    { required: ["iso"] },
  ];
  expect(render(undefined, p)).not.toContain('aria-label="mode (mode)"');
});
it("普通标量成员保持输入控件，不要求有限枚举", () => {
  const p = parameter();
  (p.schema.properties as any).exposure.oneOf[0].properties.iso = {
    type: "integer",
    minimum: 1,
  };
  expect(render(undefined, p)).toContain('inputMode="decimal"');
  expect(render(undefined, p)).toContain('aria-label="iso (iso)"');
});
it.each([
  (field: any) => {
    field.oneOf[1].properties.mode.const = "manual";
  },
  (field: any) => {
    delete field.oneOf[0].additionalProperties;
  },
  (field: any) => {
    field.oneOf[0].properties.iso = { $ref: "#/properties/duration_s" };
  },
  (field: any) => {
    field.oneOf[0].properties.iso = { enum: [{ value: 800 }] };
  },
  (field: any) => {
    field.oneOf[0].properties.iso = { enum: [800], dependentSchemas: {} };
  },
  (field: any) => {
    field.oneOf[0].properties.other = { const: "a" };
    field.oneOf[0].required.push("other");
    field.oneOf[1].properties.other = { const: "b" };
    field.oneOf[1].required.push("other");
  },
])("无法证明唯一模式和成员控件的规则保留JSON %#", (mutate) => {
  const p = parameter();
  mutate((p.schema.properties as any).exposure);
  const html = render(undefined, p);
  expect(html).toContain('aria-label="exposure (exposure)"');
  expect(html).not.toContain('aria-label="mode (mode)"');
});
it("同一动作的设备身份尚未完成时不回显旧模式", () => {
  const html = render(undefined, parameter(), {
    "/actions/0/device_id": { kind: "json", text: '"cam' },
  });
  expect(html).not.toContain('aria-label="mode (mode)"');
  expect(html).toContain('aria-label="重新填写exposure" disabled');
});
