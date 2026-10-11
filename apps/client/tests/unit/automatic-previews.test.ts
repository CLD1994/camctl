import { expect, it } from "vitest";
import {
  coordinatePreviews,
  initializePreviewMetadata,
  setPreviewIntent,
  appendContentAction,
  copyDraftAction,
  createActionAllocation,
  sameContent,
} from "../../src/shared/automatic-previews";
import { loadCapabilities } from "../../src/shared/capabilities";
import { parseClientJson } from "../../src/shared/json";
import type { DraftContent, PreviewIntent } from "../../src/server/models";
import { DraftSession } from "../../src/web/session";
it("两个操作从同一版本新增不同动作，身份独立且重算固定", () => {
  const base = initializePreviewMetadata(
    { text: '{"name":"计划","actions":[]}' },
    "disabled",
    "base",
  );
  const a = createActionAllocation(),
    b = createActionAllocation();
  const action = {
    name: "报告",
    type: "report_status",
    params: { scope: "full" },
  };
  const first = appendContentAction(base, action, false, a);
  const second = appendContentAction(base, action, false, b);
  expect(first.automaticPreviews!.actions[0].id).toBe(a.id);
  expect(first.automaticPreviews!.next).toBe(base.automaticPreviews!.next);
  expect(first.automaticPreviews!.actions[0].id).not.toBe(
    second.automaticPreviews!.actions[0].id,
  );
  expect(sameContent(first, appendContentAction(base, action, false, a))).toBe(
    true,
  );
});
it("接纳旧完整内容后新增不会复用另一版本的新增身份", () => {
  const base = initializePreviewMetadata(
    { text: '{"actions":[]}' },
    "disabled",
    "base",
  );
  const first = appendContentAction(
    base,
    { name: "A" },
    false,
    createActionAllocation(),
  );
  const afterReplacement = appendContentAction(
    base,
    { name: "B" },
    false,
    createActionAllocation(),
  );
  expect(afterReplacement.automaticPreviews!.actions[0].id).not.toBe(
    first.automaticPreviews!.actions[0].id,
  );
});
it("同一基线的独立复制使用各自固定身份", () => {
  const base = initializePreviewMetadata(
    { text: '{"actions":[{"name":"A"}]}' },
    "disabled",
    "base",
  );
  const a = createActionAllocation(),
    b = createActionAllocation();
  const first = copyDraftAction(base, 0, a),
    second = copyDraftAction(base, 0, b);
  expect(first.automaticPreviews!.actions[1].id).toBe(a.id);
  expect(second.automaticPreviews!.actions[1].id).toBe(b.id);
  expect(sameContent(first, copyDraftAction(base, 0, a))).toBe(true);
});
it.each([undefined, { id: "" }, { id: "base:0" }, { id: 1 }])(
  "有元数据的新增拒绝缺少、重复或非法分配 %j 并保持输入",
  (allocation) => {
    const base = initializePreviewMetadata(
      { text: '{"actions":[{"name":"A"}]}' },
      "disabled",
      "base",
    );
    const before = structuredClone(base);
    expect(() =>
      appendContentAction(base, { name: "B" }, false, allocation as any),
    ).toThrow();
    expect(base).toEqual(before);
  },
);
it("旧草稿追加没有元数据，不被动建立身份资料", () => {
  const base = { text: '{"actions":[]}' };
  const result = appendContentAction(base, { name: "A" });
  expect(result.automaticPreviews).toBeUndefined();
  expect((parseClientJson(result.text) as any).actions).toEqual([
    { name: "A" },
  ]);
});
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
const contentWithBothCaches = (intent: PreviewIntent): DraftContent => ({
  text: JSON.stringify({
    name: "计划",
    actions: [
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
    ],
  }),
  automaticPreviews: {
    intent,
    namespace: "test",
    next: 2,
    actions: [{ id: "test:0" }, { id: "test:1", sourceId: "test:0" }],
  },
  parameterVariants: {
    "0": [
      {
        paramsText: '{"params":{"type":"spare","value":9007199254740993}}',
        pending: { "/value": { kind: "number", text: "9e" } },
      },
    ],
  },
  actionVariants: {
    "0": [
      {
        type: "camera_timelapse",
        fields: { params: { type: "inactive", value: 7 } },
        pending: { "/params/value": { kind: "number", text: "7e" } },
        parameterVariants: [
          {
            paramsText:
              '{"params":{"type":"other","value":1.0000000000000001}}',
            pending: { "/value": { kind: "number", text: "1e" } },
          },
        ],
      },
    ],
  },
});
for (const intent of ["disabled", "enabled"] as const) {
  it.each(["/actions/01/params/ghost", "/actions/0/policy/ghost~2"])(
    `预览协调在 ${intent} 删除前停止不能确定归属的输入 %s`,
    (path) => {
      const original = contentWithBothCaches(intent);
      original.pending = { [path]: { kind: "json", text: "{" } };
      const before = JSON.stringify(original);
      const result = coordinatePreviews(original, capabilities(false));
      expect(result.content).toBe(original);
      expect(JSON.stringify(result.content)).toBe(before);
      expect(JSON.stringify(original)).toBe(before);
      expect(result.issues.length).toBeGreaterThan(0);
      expect(result.issues.join(" ")).toContain(path);
    },
  );
  it(`预览协调在 ${intent} 可靠删除时按真实位置搬移两层缓存`, () => {
    const original = contentWithBothCaches(intent);
    const root = parseClientJson(original.text) as any;
    root.actions.push({ ...capture, name: "第二拍摄" });
    original.text = JSON.stringify(root);
    original.automaticPreviews!.actions.push({ id: "test:2" });
    original.automaticPreviews!.next = 3;
    original.parameterVariants = { "2": original.parameterVariants!["0"] };
    original.actionVariants = { "2": original.actionVariants!["0"] };
    original.pending = {
      "/actions/2/policy/a~1b~0c": { kind: "number", text: "7e" },
    };
    const before = JSON.stringify(original);
    const result = coordinatePreviews(original, capabilities(false));
    expect(result.issues).toEqual([]);
    expect((parseClientJson(result.content.text) as any).actions).toEqual([
      capture,
      { ...capture, name: "第二拍摄" },
    ]);
    expect(result.content.pending).toEqual({
      "/actions/1/policy/a~1b~0c": { kind: "number", text: "7e" },
    });
    expect(result.content.parameterVariants).toEqual({
      "1": original.parameterVariants["2"],
    });
    expect(result.content.actionVariants).toEqual({
      "1": original.actionVariants["2"],
    });
    expect(JSON.stringify(original)).toBe(before);
  });
}
it.each(["", "/actions"])(
  "预览协调对整个集合未完成输入保持原完整内容 %s",
  (path) => {
    const original = contentWithBothCaches("enabled");
    original.pending = { [path]: { kind: "json", text: "{" } };
    const before = JSON.stringify(original);
    const result = coordinatePreviews(original, capabilities(false));
    expect(result.content).toBe(original);
    expect(JSON.stringify(original)).toBe(before);
    expect(result.issues.length).toBeGreaterThan(0);
  },
);
it("预览协调对参数资料的双重权威保持原文并返回诊断", () => {
  const original = contentWithBothCaches("enabled");
  original.parameterVariants!["0"].push({
    paramsText: '{"params":{"type":"photo","other":7}}',
    pending: {},
  });
  const before = JSON.stringify(original);
  const result = coordinatePreviews(original, capabilities(false));
  expect(result.content).toBe(original);
  expect(JSON.stringify(original)).toBe(before);
  expect(result.issues.length).toBeGreaterThan(0);
});
it("会话观察与准备编辑共同保留停止协调的完整内容", () => {
  const original = contentWithBothCaches("disabled");
  original.pending = {
    "/actions/01/params/ghost": { kind: "json", text: "{" },
  };
  const draft = {
    id: "draft",
    revision: 1,
    createdAt: "2026-10-10 01:00:00",
    updatedAt: "2026-10-10 01:00:00",
    content: original,
  };
  const session = new DraftSession(
    draft,
    { read: async () => draft, save: async () => draft },
    () => {},
  );
  const before = JSON.stringify(original);
  session.observe(draft, undefined, capabilities(false));
  expect(session.content).toEqual(original);
  expect(session.saved).toBe(true);
  expect(session.prepareEdit(original).content).toEqual(original);
  session.coordinate(capabilities(false));
  expect(session.content).toEqual(original);
  expect(session.version).toBe(0);
  expect(JSON.stringify(original)).toBe(before);
});
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
  const appended = appendDraftAction(
    renamed,
    {
      name: "手动",
      type: "report_status",
    },
    createActionAllocation(),
  );
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
  const allocation = createActionAllocation();
  const copied = coordinatePreviews(
    copyDraftAction(original, 0, allocation),
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
  expect(copied.automaticPreviews!.actions[2]).toEqual({ id: allocation.id });
  expect(copied.automaticPreviews!.actions[3]).toEqual({
    id: `${allocation.id}:preview`,
    sourceId: allocation.id,
  });
  expect(
    sameContent(copied, coordinatePreviews(copied, capabilities()).content),
  ).toBe(true);
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
