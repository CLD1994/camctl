import { expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SchemaFields } from "../../src/web/Editor";
import type { DraftContent } from "../../src/server/models";
import type { ParameterType } from "../../src/shared/types";
import { editValue, parseDraft, setValue } from "../../src/web/editing";
import { switchObjectBranch } from "../../src/shared/object-variants";
import { objectBranchJsonEdit } from "../../src/web/object-branches";
import { replaceObjectValue } from "../../src/shared/object-variants";
import { linkedParameter } from "../fixtures/linked-parameters";

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
it("时间条件共存时仍可独立编辑曝光且Auto不带入EV", () => {
  const html = render('{"mode":"manual","iso":800}', linkedParameter());
  expect(html).toContain('aria-label="mode (mode)"');
  expect(html).toContain('aria-label="iso (iso)"');
  expect(html).not.toContain('aria-label="exposure (exposure)"');
  const auto = render('{"mode":"auto"}', linkedParameter());
  expect(auto).toContain('aria-label="mode (mode)"');
  expect(auto).not.toContain('aria-label="iso (iso)"');
  expect(auto).not.toContain('aria-label="compensation_ev (compensation_ev)"');
});
it("时间组pending不阻止独立曝光，曝光成员pending保留其原文", () => {
  const html = render('{"mode":"manual","iso":800}', linkedParameter(), {
    "/actions/0/params/interval_s": { kind: "number", text: "1e" },
    "/actions/0/params/exposure/iso": { kind: "number", text: "8e" },
  });
  expect(html).toContain('aria-label="mode (mode)"');
  expect(html).toContain("8e");
});
it.each(["/actions/99/name", "/actions/00/params/exposure/iso", "invalid"])(
  "无法归属的pending %s暂停曝光全部选择并保留JSON入口",
  (path) => {
    const html = render('{"mode":"manual","iso":800}', linkedParameter(), {
      [path]: { kind: "number", text: "1e" },
    });
    expect(html).not.toContain('aria-label="mode (mode)"');
    expect(html).not.toContain('aria-label="iso (iso)"');
    expect(html).toContain('aria-label="exposure (exposure)"');
    expect(html).toContain('aria-label="重新填写exposure" disabled');
  },
);
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

it("incomplete white balance does not disable exposure choices", async () => {
  const { titledParameter } = await import("../fixtures/titled-parameters");
  const p = titledParameter();
  const html = render('{"mode":"M","iso":100}', p, {
    "/actions/0/params/white_balance/temperature_k": {
      kind: "number",
      text: "5e",
    },
  });
  expect(html).toContain('aria-label="曝光模式 (mode)"');
  expect(html).toContain('aria-label="快门 (shutter_s)"');
  expect(html).toContain('aria-label="光圈 (aperture)"');
  expect(html).not.toContain('aria-label="曝光设置 (exposure)"');
});
it("object branches use titles and preserve per-branch drafts", async () => {
  const { titledParameter } = await import("../fixtures/titled-parameters");
  const { objectBranches } = await import("../../src/web/object-branches");
  const catalog = objectBranches(titledParameter(), "exposure");
  expect(catalog?.branches.map((branch) => (branch as any).label)).toEqual([
    "手动曝光",
    "光圈优先",
    "快门优先",
    "程序曝光",
  ]);
  const initial = {
    text: '{"actions":[{"type":"camera_take_photo","params":{"type":"titled_photo","exposure":{"mode":"M","shutter_s":"1/1500","extra":7}}}]}',
  };
  const switched = switchObjectBranch(initial, 0, ["exposure"], "mode", "P");
  const restored = switchObjectBranch(switched, 0, ["exposure"], "mode", "M");
  expect((parseDraft(restored).actions[0].params as any).exposure).toEqual({
    mode: "M",
    shutter_s: "1/1500",
    extra: 7,
  });
  expect(render('{"mode":"M","extra":7}', titledParameter())).toContain(
    'aria-label="重新填写曝光设置"',
  );
});

it("换设备只改变当前校验依据，模式内容与未完成输入继续随原字段恢复", async () => {
  const { titledParameter } = await import("../fixtures/titled-parameters");
  const initial: DraftContent = {
    text: '{"actions":[{"type":"camera_take_photo","device_id":"first","params":{"type":"titled_photo","exposure":{"mode":"M","iso":100,"shutter_s":"1/1500"}}}]}',
    pending: {
      "/actions/0/params/exposure/aperture": { kind: "number", text: "4e" },
    },
  };
  const switched = switchObjectBranch(initial, 0, ["exposure"], "mode", "A");
  const changed = setValue(switched, ["actions", 0, "device_id"], "second");
  expect(changed.objectVariants).toEqual(switched.objectVariants);
  const restored = switchObjectBranch(changed, 0, ["exposure"], "mode", "M");
  expect((parseDraft(restored).actions[0].params as any).exposure).toEqual({
    mode: "M",
    iso: 100,
    shutter_s: "1/1500",
  });
  expect(restored.pending?.["/actions/0/params/exposure/aperture"]?.text).toBe(
    "4e",
  );
  const parameter = titledParameter();
  (parameter.schema.properties as any).exposure.oneOf[0].properties.iso.enum = [
    64,
  ];
  const html = renderToStaticMarkup(
    createElement(SchemaFields, {
      parameter,
      content: restored,
      path: ["actions", 0, "params"],
      change: () => {
        throw new Error("查看不能修改");
      },
    }),
  );
  expect(html).toContain("100");
  expect(html).toContain('data-value="invalid"');
  expect(html).toContain("4e");
});
it("对象模式缺少标题时回退机器值，同名标题仍区分模式身份", async () => {
  const { titledParameter } = await import("../fixtures/titled-parameters");
  const { objectBranches } = await import("../../src/web/object-branches");
  const parameter = titledParameter();
  const branches = (parameter.schema.properties as any).exposure.oneOf;
  branches[0].title = branches[1].title = "同名";
  delete branches[2].title;
  branches[3].title = " ";
  expect(
    objectBranches(parameter, "exposure")?.branches.map(
      (branch) => branch.label,
    ),
  ).toEqual(["同名（M）", "同名（A）", "S", "P"]);
});
