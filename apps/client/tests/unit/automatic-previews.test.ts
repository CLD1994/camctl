import { expect, it } from "vitest";
import {
  coordinatePreviews,
  initializePreviewMetadata,
  setPreviewIntent,
} from "../../src/shared/automatic-previews";
import { loadCapabilities } from "../../src/shared/capabilities";
import { parseClientJson } from "../../src/shared/json";
const capture = {
  name: "拍摄",
  type: "camera_record",
  device_id: "cam",
  scheduled_at: "2026-10-10 01:00:00",
  params: { type: "photo" },
  policy: { max_delay_ms: 0 },
};
const capabilities = (support = true) => ({
  active: loadCapabilities({
    devices: [
      {
        device_id: "cam",
        driver_id: "test",
        actions: [
          {
            type: "camera_record",
            parameter_types: [
              {
                type: "photo",
                name: "照片",
                description: "照片",
                preview_supported: support,
                schema: {
                  $schema: "https://json-schema.org/draft/2020-12/schema",
                  type: "object",
                  required: ["type"],
                  properties: { type: { const: "photo" } },
                  additionalProperties: false,
                },
              },
            ],
          },
        ],
      },
    ],
  }),
  error: null,
  generation: 1,
  version: "catalog-1",
});
const content = () =>
  initializePreviewMetadata(
    { text: JSON.stringify({ name: "计划", actions: [capture] }) },
    "enabled",
    "test",
  );
it("开启时生成唯一取回并且重复协调保持完整版本", () => {
  const first = coordinatePreviews(content(), capabilities());
  expect((parseClientJson(first.content.text) as any).actions).toEqual([
    capture,
    {
      name: "拍摄预览",
      type: "obtain_action_outputs",
      scheduled_at: capture.scheduled_at,
      params: {
        source: { action_name: "拍摄" },
        filter: "preview",
        purpose: "auto_preview",
      },
    },
  ]);
  expect(coordinatePreviews(first.content, capabilities()).content).toEqual(
    first.content,
  );
});
it("尚未设置时不从显式动作猜测开关", () => {
  const c = { text: JSON.stringify({ name: "计划", actions: [capture] }) };
  expect(coordinatePreviews(c, capabilities()).content).toEqual(c);
});
it.each(["pending", "error", "invalid"])(
  "不能可靠判断时保持原文与关联 %s",
  (kind) => {
    const c = coordinatePreviews(content(), capabilities()).content;
    const original =
      kind === "pending"
        ? {
            ...c,
            pending: {
              "/actions/0/params": { kind: "json" as const, text: "{" },
            },
          }
        : kind === "invalid"
          ? { ...c, text: "{" }
          : c;
    expect(
      coordinatePreviews(
        original,
        kind === "error"
          ? { ...capabilities(false), error: "加载失败" }
          : capabilities(false),
      ).content,
    ).toEqual(original);
  },
);
it("明确不支持时移除自动取回", () => {
  const c = coordinatePreviews(content(), capabilities()).content;
  expect(
    (
      parseClientJson(
        coordinatePreviews(c, capabilities(false)).content.text,
      ) as any
    ).actions,
  ).toEqual([capture]);
});
it("重复自动关联保留原文和诊断", () => {
  const c = coordinatePreviews(content(), capabilities()).content;
  const root = parseClientJson(c.text) as any;
  root.actions.push({ ...root.actions[1], name: "重复" });
  const original = initializePreviewMetadata(
    { text: JSON.stringify(root) },
    "enabled",
    "dup",
  );
  const result = coordinatePreviews(original, capabilities());
  expect(result.content).toEqual(original);
  expect(result.issues.length).toBeGreaterThan(0);
});
it("关闭只移除明确自动用途", () => {
  const c = coordinatePreviews(content(), capabilities()).content;
  const result = setPreviewIntent(c, "disabled", "unused", capabilities());
  expect((parseClientJson(result.content.text) as any).actions).toEqual([
    capture,
  ]);
  expect(result.content.automaticPreviews?.intent).toBe("disabled");
});

it("改名跟随稳定来源身份，删除拍摄同步移除派生并搬移资料", async () => {
  const { setValue, removeAction, appendDraftAction } =
    await import("../../src/web/editing");
  const original = coordinatePreviews(content(), capabilities()).content;
  const renamed = coordinatePreviews(
    setValue(original, ["actions", 0, "name"], "新拍摄"),
    capabilities(),
  ).content;
  expect(renamed.automaticPreviews!.actions[1].id).toBe(
    original.automaticPreviews!.actions[1].id,
  );
  expect(
    (parseClientJson(renamed.text) as any).actions[1].params.source.action_name,
  ).toBe("新拍摄");
  const appended = appendDraftAction(renamed, {
    name: "手动",
    type: "report_status",
  });
  const withInputs = {
    ...appended,
    pending: { "/actions/2/params": { kind: "json" as const, text: "{" } },
    actionVariants: {
      "2": [{ type: "camera_record", fields: {}, pending: {} }],
    },
  };
  const removed = removeAction(withInputs, 0);
  expect((parseClientJson(removed.text) as any).actions).toEqual([
    { name: "手动", type: "report_status" },
  ]);
  expect(removed.pending).toEqual({
    "/actions/0/params": { kind: "json", text: "{" },
  });
  expect(removed.actionVariants).toHaveProperty("0");
  expect(removed.automaticPreviews!.actions).toHaveLength(1);
});
it("复制拍摄生成独立身份和预览，不复制来源的自动关联", async () => {
  const { copyDraftAction } =
    await import("../../src/shared/automatic-previews");
  const original = coordinatePreviews(content(), capabilities()).content;
  const copied = coordinatePreviews(
    copyDraftAction(original, 0),
    capabilities(),
  ).content;
  const actions = (parseClientJson(copied.text) as any).actions;
  expect(actions.map((a: any) => a.name)).toEqual([
    "拍摄",
    "拍摄预览",
    "拍摄 2",
    "拍摄 2预览",
  ]);
  expect(new Set(copied.automaticPreviews!.actions.map((a) => a.id)).size).toBe(
    4,
  );
});
it("另一个动作的业务错误不阻止有效拍摄生成，参数非法不得猜支持", () => {
  const original = initializePreviewMetadata(
    {
      text: JSON.stringify({
        name: "计划",
        actions: [
          capture,
          {
            name: "坏",
            type: "camera_record",
            device_id: "missing",
            params: { type: "photo" },
          },
        ],
      }),
    },
    "enabled",
    "test",
  );
  const result = coordinatePreviews(original, capabilities());
  expect(
    (parseClientJson(result.content.text) as any).actions.map(
      (a: any) => a.name,
    ),
  ).toEqual(["拍摄", "坏", "拍摄预览"]);
  expect(result.issues).toHaveLength(1);
});

it("生成名称满足协议长度且不覆盖已有用户名称", () => {
  const c = initializePreviewMetadata(
    {
      text: JSON.stringify({
        name: "计划",
        actions: [
          { ...capture, name: "拍".repeat(128) },
          { name: "拍摄预览", type: "report_status" },
        ],
      }),
    },
    "enabled",
    "long",
  );
  const result = coordinatePreviews(c, capabilities());
  const actions = (parseClientJson(result.content.text) as any).actions;
  expect([...actions[2].name].length).toBeLessThanOrEqual(128);
  expect(actions[1].name).toBe("拍摄预览");
});
it("无效年份不能据已解析旧值生成自动动作", () => {
  const c = initializePreviewMetadata(
    {
      text: JSON.stringify({
        name: "计划",
        actions: [{ ...capture, scheduled_at: "0000-01-01 00:00:00" }],
      }),
    },
    "enabled",
    "zero",
  );
  expect(coordinatePreviews(c, capabilities()).content).toEqual(c);
});

it("明确选择内置类型才移除取回，未知类型保留关联待核实", () => {
  const original = coordinatePreviews(content(), capabilities()).content;
  const parsed = parseClientJson(original.text) as any;
  parsed.actions[0].type = "future_camera";
  const unknown = { ...original, text: JSON.stringify(parsed) };
  expect(coordinatePreviews(unknown, capabilities()).content).toEqual(unknown);
  parsed.actions[0].type = "report_status";
  const changed = coordinatePreviews(
    { ...original, text: JSON.stringify(parsed) },
    capabilities(),
  ).content;
  expect((parseClientJson(changed.text) as any).actions).toHaveLength(1);
});
it("外部自动关联引用非拍摄动作时保持原文和诊断", () => {
  const original = initializePreviewMetadata(
    {
      text: JSON.stringify({
        name: "计划",
        actions: [
          { name: "报告", type: "report_status" },
          {
            name: "自动",
            type: "obtain_action_outputs",
            params: {
              source: { action_name: "报告" },
              filter: "preview",
              purpose: "auto_preview",
            },
          },
        ],
      }),
    },
    "enabled",
    "bad-source",
  );
  const result = coordinatePreviews(original, capabilities());
  expect(result.content).toEqual(original);
  expect(result.issues.length).toBeGreaterThan(0);
});

it("不相关动作缺少名称不会掩盖可确定的拍摄支持", () => {
  const c = initializePreviewMetadata(
    {
      text: JSON.stringify({
        name: "计划",
        actions: [capture, { type: "report_status" }],
      }),
    },
    "enabled",
    "independent",
  );
  expect(
    (parseClientJson(coordinatePreviews(c, capabilities()).content.text) as any)
      .actions,
  ).toHaveLength(3);
});
it("删除自动项保留根与未修改动作的原数字事实", async () => {
  const { setPreviewIntent } =
    await import("../../src/shared/automatic-previews");
  const c = coordinatePreviews(content(), capabilities()).content;
  const withNumber = {
    ...c,
    text: c.text.replace(
      '"name": "计划",',
      '"name": "计划", "extra":1.0000000000000001,',
    ),
  };
  const result = setPreviewIntent(
    withNumber,
    "disabled",
    "unused",
    capabilities(),
  ).content;
  expect(result.text).toContain("1.0000000000000001");
});
it("自动用途存在局部未完成输入时不能用旧值删除或改写", () => {
  const c = coordinatePreviews(content(), capabilities()).content;
  const pending = {
    ...c,
    pending: {
      "/actions/1/params/purpose": { kind: "json" as const, text: '"man' },
    },
  };
  expect(coordinatePreviews(pending, capabilities(false)).content).toEqual(
    pending,
  );
});

it("重复自动关联不因拍摄改名而猜测修复", async () => {
  const { setValue } = await import("../../src/web/editing");
  const c = coordinatePreviews(content(), capabilities()).content,
    root = parseClientJson(c.text) as any;
  root.actions.push({ ...root.actions[1], name: "重复" });
  const original = initializePreviewMetadata(
    { text: JSON.stringify(root) },
    "enabled",
    "duplicate",
  );
  const changed = setValue(original, ["actions", 0, "name"], "新名称");
  expect(
    (parseClientJson(changed.text) as any).actions
      .slice(1)
      .map((a: any) => a.params.source.action_name),
  ).toEqual(["拍摄", "拍摄"]);
});
