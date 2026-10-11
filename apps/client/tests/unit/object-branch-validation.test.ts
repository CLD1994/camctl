import { expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { DraftContent } from "../../src/server/models";
import type {
  Capabilities,
  Issue,
  ParameterType,
} from "../../src/shared/types";
import { parseDraft } from "../../src/web/editing";
import { stringifyJson } from "../../src/shared/json";
import { validatePlan } from "../../src/shared/plan";
import { presentIssues } from "../../src/web/validation-presentation";
import { SchemaFields } from "../../src/web/Editor";
import { ValidationProvider } from "../../src/web/Validation";

function parameter(
  field = "exposure",
  discriminator = "mode",
  member = "iso",
): ParameterType {
  return {
    type: "record",
    name: "录像",
    description: "",
    preview_supported: false,
    schema: {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      type: "object",
      additionalProperties: false,
      properties: {
        type: { const: "record" },
        resolution: { enum: ["4k"] },
        [field]: {
          oneOf: [
            {
              type: "object",
              additionalProperties: false,
              properties: {
                [discriminator]: { const: "manual" },
                [member]: { enum: [100, 800] },
              },
              required: [discriminator, member],
            },
            {
              type: "object",
              additionalProperties: false,
              properties: {
                [discriminator]: { const: "auto" },
                compensation_ev: { enum: [0, 0.7] },
              },
              required: [discriminator, "compensation_ev"],
            },
          ],
        },
      },
      required: ["type", "resolution", field],
    },
  };
}
function current(
  value: unknown,
  options: {
    missing?: boolean;
    pending?: DraftContent["pending"];
    resolution?: string;
    parameter?: ParameterType;
    field?: string;
  } = {},
) {
  const field = options.field ?? "exposure",
    p = options.parameter ?? parameter(field);
  const capabilities: Capabilities = {
    devices: [
      {
        device_id: "camera",
        driver_id: "test",
        actions: [{ type: "camera_record", parameter_types: [p] }],
      },
    ],
  };
  const content: DraftContent = {
    text: stringifyJson({
      name: "计划",
      actions: [
        {
          name: "录像",
          type: "camera_record",
          device_id: "camera",
          scheduled_at: "2026-10-11 00:00:00",
          policy: { max_delay_ms: 0 },
          params: {
            type: "record",
            resolution: options.resolution ?? "4k",
            ...(!options.missing ? { [field]: value } : {}),
          },
        },
      ],
    }),
    ...(options.pending ? { pending: options.pending } : {}),
  };
  const plan = parseDraft(content);
  const raw: Issue[] = [
    ...Object.keys(content.pending ?? {}).map((path) => ({
      path,
      code: "unfinished_input",
      message: "输入尚未完成",
    })),
    ...validatePlan(
      { ...plan, request_id: "1", created_at: "2026-10-11 00:00:00" },
      capabilities,
    ),
  ];
  const before = stringifyJson(raw);
  const shown = presentIssues(raw, plan, capabilities, content);
  expect(stringifyJson(raw)).toBe(before);
  return { raw, shown, content, capabilities, p };
}
it.each([
  [{ mode: "manual" }, "/actions/0/params/exposure/iso"],
  [{ mode: "manual", iso: 999 }, "/actions/0/params/exposure/iso"],
  [{ mode: "auto" }, "/actions/0/params/exposure/compensation_ev"],
  [
    { mode: "auto", compensation_ev: 99 },
    "/actions/0/params/exposure/compensation_ev",
  ],
])("可表达模式只呈现当前成员的真实错误 %j", (value, target) => {
  const { raw, shown } = current(value);
  expect(raw.length).toBeGreaterThan(1);
  expect(shown.map((item) => item.target)).toEqual([target]);
  expect(shown.every((item) => !item.json)).toBe(true);
});
it.each([true, false])(
  "对象缺省或空对象只要求选择模式 missing=%s",
  (missing) => {
    expect(current({}, { missing }).shown.map((item) => item.target)).toEqual([
      "/actions/0/params/exposure/mode",
    ]);
  },
);
it("当前对象合法时兄弟错误仍只由兄弟负责", () => {
  expect(
    current({ mode: "manual", iso: 800 }, { resolution: "bad" }).shown.map(
      (item) => item.target,
    ),
  ).toEqual(["/actions/0/params/resolution"]);
});
it.each([
  null,
  1,
  { iso: 800 },
  { mode: "unknown" },
  { mode: "manual", iso: 800, compensation_ev: 0 },
  { mode: "manual", iso: {} },
])("不可表达原值只定位本对象JSON %j", (value) => {
  const { shown } = current(value);
  expect(shown.length).toBeGreaterThan(0);
  expect(
    shown.every(
      (item) => item.target === "/actions/0/params/exposure" && item.json,
    ),
  ).toBe(true);
});
it.each(["/actions/0/params/exposure", "/actions/0/params/exposure/mode"])(
  "身份未完成原文不产生旧模式成员要求 %s",
  (path) => {
    const { shown } = current(
      { mode: "manual" },
      { pending: { [path]: { kind: "json", text: '"a' } } },
    );
    expect(
      shown.find((item) => item.issue.code === "unfinished_input")?.target,
    ).toBe(path);
    expect(
      shown
        .filter((item) => item.issue.schema)
        .every(
          (item) => item.target === "/actions/0/params/exposure" && item.json,
        ),
    ).toBe(true);
  },
);
it("成员未完成原文与保留正文错误分开呈现", () => {
  const path = "/actions/0/params/exposure/iso";
  const { shown, content, p } = current(
    { mode: "manual", iso: 999 },
    { pending: { [path]: { kind: "number", text: "1e" } } },
  );
  expect(shown.map((item) => item.target)).toEqual([path, path]);
  const html = renderToStaticMarkup(
    createElement(ValidationProvider, {
      issues: shown,
      children: createElement(SchemaFields, {
        parameter: p,
        content,
        path: ["actions", 0, "params"],
        change: () => {},
      }),
    }),
  );
  expect(html).toContain('class="current-input-error"');
  expect(html).toContain('class="retained-content-errors"');
});
it("特殊Pointer成员和另一判别名称按同一规则定位", () => {
  const p = parameter("capture/~", "kind", "gain/~");
  expect(
    current({ kind: "manual" }, { parameter: p, field: "capture/~" }).shown.map(
      (item) => item.target,
    ),
  ).toEqual(["/actions/0/params/capture~1~0/gain~1~0"]);
});
it("没有当前编辑上下文时保持原始诊断呈现边界", () => {
  const { raw, content, capabilities } = current({ mode: "manual" });
  expect(
    presentIssues(raw, parseDraft(content), capabilities).map(
      (item) => item.issue.code,
    ),
  ).toEqual(raw.map((item) => item.code));
});
it("不合格根组合保留完整Schema责任", () => {
  const p = parameter();
  p.schema.allOf = [{ required: ["resolution"] }];
  const { raw, shown } = current({ mode: "manual" }, { parameter: p });
  expect(shown.map((item) => item.issue.code)).toEqual(
    raw.map((item) => item.code),
  );
});
it.each([
  "/actions/0/params",
  "/actions/0/device_id",
  "/actions/0/type",
  "/actions/0/params/type",
])("祖先或身份未完成保持完整正文诊断 %s", (path) => {
  const { raw, shown } = current(
    { mode: "manual" },
    { pending: { [path]: { kind: "json", text: "{" } } },
  );
  expect(shown.map((item) => item.issue.code)).toEqual(
    raw.map((item) => item.code),
  );
});
it("对象原文未完成时结构诊断与当前原文分段", () => {
  const { shown, content, p } = current(1, {
    pending: { "/actions/0/params/exposure": { kind: "json", text: "auto" } },
  });
  const html = renderToStaticMarkup(
    createElement(ValidationProvider, {
      issues: shown,
      children: createElement(SchemaFields, {
        parameter: p,
        content,
        path: ["actions", 0, "params"],
        change: () => {},
      }),
    }),
  );
  expect(html).toContain('class="current-input-error"');
  expect(html).toContain('class="retained-content-errors"');
  expect(
    shown.some(
      (item) =>
        item.issue.code === "schema_type" && item.message.includes("对象"),
    ),
  ).toBe(true);
});
