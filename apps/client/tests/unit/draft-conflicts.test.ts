import { expect, it } from "vitest";
import { record } from "../helpers/draft-conflicts";
import { cloneClientJson, parseClientJson } from "../../src/shared/json";
import type { CapabilityState } from "../../src/shared/automatic-previews";
import { loadCapabilities } from "../../src/shared/capabilities";
import type { Draft, DraftContent } from "../../src/server/models";
import {
  classifyDraftObservation,
  selectionMatches,
  type DraftObservation,
  type PendingDraftOperation,
} from "../../src/web/draft-conflicts";

const none: PendingDraftOperation = { kind: "none" };
const available = (draft: Draft): DraftObservation => ({
  kind: "available",
  draft,
});
it.each([
  ["base", "remote", "remote"],
  ["local", "base", "same_baseline"],
  ["local", "local", "saved_content"],
  ["local", "remote", "conflict"],
  ["base", "base", "saved_content"],
] as const)("L=%s、R=%s 的完整分类为 %s", (l, r, expected) => {
  expect(
    classifyDraftObservation(
      record(1, "base"),
      record(1, l).content,
      available(record(2, r)),
      none,
    ).kind,
  ).toBe(expected);
});
it("本页编辑后恢复 B，远端更新仍可以完整同步", () => {
  expect(
    classifyDraftObservation(
      record(1, "base"),
      record(1, "base").content,
      available(record(2, "remote")),
      none,
    ).kind,
  ).toBe("remote");
});
it.each([false, true])(
  "同版本不同内容恰等于 L=%s 仍是记录异常",
  (equalLocal) => {
    expect(
      classifyDraftObservation(
        record(2, "base"),
        record(2, equalLocal ? "remote" : "local").content,
        available(record(2, "remote")),
        none,
      ).kind,
    ).toBe("invalid");
  },
);
it.each([
  ["身份不同", { ...record(2, "remote"), id: "other" }],
  [
    "私有资料结构错误",
    { ...record(2, "remote"), content: { text: "{", pending: [] } },
  ],
  ["版本不是正整数", { ...record(2, "remote"), revision: 2.5 }],
  [
    "创建时间改变",
    { ...record(2, "remote"), createdAt: "2026-10-11 09:00:00" },
  ],
])("%s 的记录不能提高基线", (_name, draft) => {
  expect(
    classifyDraftObservation(
      record(1, "base"),
      record(1, "local").content,
      available(draft as Draft),
      none,
    ).kind,
  ).toBe("invalid");
});
it("较旧版本即使内容或导出标记不同也忽略", () => {
  expect(
    classifyDraftObservation(
      record(3, "base"),
      record(1, "local").content,
      available({ ...record(2, "remote"), exportedRequestId: "42" }),
      none,
    ).kind,
  ).toBe("ignore");
});
it("读取不可用不提供新选择资格", () => {
  expect(
    classifyDraftObservation(
      record(1, "base"),
      record(1, "local").content,
      { kind: "unavailable", message: "offline" },
      none,
    ).kind,
  ).toBe("unavailable");
});
it.each([
  none,
  { kind: "other" },
  { kind: "save", revision: 1, requestEnded: false },
] as PendingDraftOperation[])(
  "可靠删除在待定操作 %j 前分类，但不核实操作结果",
  (pending) => {
    expect(
      classifyDraftObservation(
        record(1, "base"),
        record(1, "local").content,
        { kind: "deleted" },
        pending,
      ).kind,
    ).toBe("deleted");
  },
);
it.each([
  none,
  { kind: "other" },
  { kind: "save", revision: 1, requestEnded: false },
] as PendingDraftOperation[])("可靠导出在待定操作 %j 前分类", (pending) => {
  expect(
    classifyDraftObservation(
      record(1, "base"),
      record(1, "local").content,
      available({ ...record(2, "base"), exportedRequestId: "42" }),
      pending,
    ).kind,
  ).toBe("exported");
});
it.each([
  { kind: "other" },
  { kind: "save", revision: 1, requestEnded: false },
  { kind: "save", revision: 1, requestEnded: true },
] as PendingDraftOperation[])(
  "待定操作 %j 未取得高于提交基线的事实时继续核实",
  (pending) => {
    expect(
      classifyDraftObservation(
        record(1, "base"),
        record(1, "local").content,
        available(record(1, "base")),
        pending,
      ).kind,
    ).toBe("blocked");
  },
);
it.each(["local", "remote", "base"])(
  "请求已结束且更高版本为 %s，旧写入不得被认领",
  (name) => {
    expect(
      classifyDraftObservation(
        record(1, "base"),
        record(1, "local").content,
        available(record(3, name)),
        { kind: "save", revision: 1, requestEnded: true },
      ).kind,
    ).toBe("superseded_write");
  },
);
const capabilities: CapabilityState = {
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
                preview_supported: true,
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
  version: "k1",
};
const capture = {
  name: "拍摄",
  type: "camera_record",
  device_id: "cam",
  scheduled_at: "2026-10-11 10:00:00",
  params: { type: "photo" },
};
const previewBase = (): Draft => ({
  ...record(2, "base"),
  content: {
    text: JSON.stringify({ name: "base", actions: [capture] }, null, 2),
    automaticPreviews: {
      namespace: "n",
      next: 1,
      intent: "enabled",
      actions: [{ id: "n:0" }],
    },
  },
});
// 此结果独立列出协调契约要求的自动动作与稳定身份，不从协调函数计算预期。
const previewDerived = (): DraftContent => ({
  text: JSON.stringify(
    {
      name: "base",
      actions: [
        capture,
        {
          name: "拍摄预览",
          type: "obtain_action_outputs",
          scheduled_at: "2026-10-11 10:00:00",
          params: {
            source: { action_name: "拍摄" },
            filter: "preview",
            purpose: "auto_preview",
          },
        },
      ],
    },
    null,
    2,
  ),
  automaticPreviews: {
    namespace: "n",
    next: 1,
    intent: "enabled",
    actions: [{ id: "n:0" }, { id: "n:0:preview", sourceId: "n:0" }],
  },
});
it("同次能力派生优先于一般差异", () => {
  const base = previewBase();
  const content = previewDerived();
  expect(
    classifyDraftObservation(
      base,
      record(1, "local").content,
      {
        kind: "available",
        draft: { ...base, revision: 3, content },
        capabilities,
      },
      none,
    ).kind,
  ).toBe("derived");
});
it("没有同次能力时不能认定派生", () => {
  const base = previewBase();
  expect(
    classifyDraftObservation(
      base,
      record(1, "local").content,
      available({ ...base, revision: 3, content: previewDerived() }),
      none,
    ).kind,
  ).toBe("conflict");
});
it("较新用户写入不能因内容恰为能力派生结果而消失", () => {
  const base = previewBase(),
    input = record(1, "user").content;
  const content = previewDerived();
  expect(
    classifyDraftObservation(
      base,
      record(1, "local").content,
      {
        kind: "available",
        draft: {
          ...base,
          revision: 3,
          content,
          lastWrite: { revision: 3, input, content },
        },
        capabilities,
      },
      none,
    ).kind,
  ).toBe("conflict");
});
it("基线既有写入依据改变不能冒充能力派生", () => {
  const base = previewBase();
  base.lastWrite = { revision: 2, input: base.content, content: base.content };
  const actual = {
    ...base,
    revision: 3,
    content: previewDerived(),
    lastWrite: { ...base.lastWrite, input: record(1, "another").content },
  };
  expect(
    classifyDraftObservation(
      base,
      record(1, "local").content,
      { kind: "available", draft: actual, capabilities },
      none,
    ).kind,
  ).toBe("conflict");
});
const snapshot = () => ({
  draftId: "d1",
  localVersion: 1,
  local: record(1, "local").content,
  remote: record(2, "remote"),
  observationEpoch: 4,
});
it("相同完整观察允许确认", () => {
  const s = snapshot();
  expect(
    selectionMatches(
      s,
      "d1",
      1,
      cloneClientJson(s.local),
      available(cloneClientJson(s.remote)),
      4,
    ),
  ).toBe(true);
});
it.each([
  "id",
  "localVersion",
  "local",
  "remoteVersion",
  "remote",
  "epoch",
  "exported",
  "deleted",
  "unavailable",
])("%s 改变撤销确认", (change) => {
  const s = snapshot();
  let observed: DraftObservation = available(cloneClientJson(s.remote));
  if (observed.kind === "available") {
    if (change === "remoteVersion") observed.draft.revision++;
    if (change === "remote")
      observed.draft.content.pending = { "": { kind: "json", text: "{" } };
    if (change === "exported") observed.draft.exportedRequestId = "42";
  }
  if (change === "deleted") observed = { kind: "deleted" };
  if (change === "unavailable")
    observed = { kind: "unavailable", message: "offline" };
  const local = cloneClientJson(s.local);
  if (change === "local")
    local.automaticPreviews = {
      namespace: "n",
      next: 0,
      intent: "unset",
      actions: [],
    };
  expect(
    selectionMatches(
      s,
      change === "id" ? "other" : "d1",
      change === "localVersion" ? 2 : 1,
      local,
      observed,
      change === "epoch" ? 5 : 4,
    ),
  ).toBe(false);
});
it("读取恢复为相同 R 后，旧确认仍已经撤销", () => {
  const s = snapshot();
  expect(
    selectionMatches(
      s,
      "d1",
      1,
      s.local,
      { kind: "unavailable", message: "offline" },
      5,
    ),
  ).toBe(false);
  expect(selectionMatches(s, "d1", 1, s.local, available(s.remote), 5)).toBe(
    false,
  );
});
it.each([
  ["1e-999", "0"],
  ["1.0000000000000001", "1"],
])("私有数字 %s 与 %s 不共享确认资格", (a, b) => {
  const content = (token: string): DraftContent =>
    parseClientJson(
      `{"text":"{","actionVariants":{"0":[{"type":"camera_record","fields":{"amount":${token}},"pending":{}}]}}`,
    ) as DraftContent;
  const s = snapshot();
  s.local = content(a);
  expect(selectionMatches(s, "d1", 1, content(b), available(s.remote), 4)).toBe(
    false,
  );
  expect(
    classifyDraftObservation(
      { ...record(1, "base"), content: content(a) },
      content(a),
      available({ ...record(2, "remote"), content: content(b) }),
      none,
    ).kind,
  ).toBe("remote");
});
it.each([
  ["pending", { pending: { "": { kind: "json", text: "-" } } }],
  [
    "actionVariants",
    {
      actionVariants: {
        "0": [
          { type: "camera_record", fields: { device_id: "cam" }, pending: {} },
        ],
      },
    },
  ],
  [
    "parameterVariants",
    {
      parameterVariants: {
        "0": [{ paramsText: '{"params":{"type":"photo"}}', pending: {} }],
      },
    },
  ],
  [
    "objectVariants",
    {
      objectVariants: {
        "0": {
          "/exposure": {
            discriminator: "mode",
            variants: [
              { valueText: '{"value":{"mode":"manual"}}', pending: {} },
            ],
          },
        },
      },
    },
  ],
  [
    "automaticPreviews",
    {
      automaticPreviews: {
        namespace: "n",
        next: 0,
        intent: "unset",
        actions: [],
      },
    },
  ],
] as Array<[string, Partial<DraftContent>]>)(
  "正文相同而 %s 不同仍形成完整冲突并撤销确认",
  (_name, fields) => {
    const base = { ...record(1, "base"), content: { text: "{" } };
    const local = {
      text: "{",
      pending: { "": { kind: "json" as const, text: "local" } },
    };
    const remote = { ...base, revision: 2, content: { text: "{", ...fields } };
    expect(
      classifyDraftObservation(base, local, available(remote), none).kind,
    ).toBe("conflict");
    const s = {
      draftId: "d1",
      localVersion: 1,
      local: base.content,
      remote: base,
      observationEpoch: 4,
    };
    expect(
      selectionMatches(s, "d1", 1, remote.content, available(base), 4),
    ).toBe(false);
    expect(
      selectionMatches(
        s,
        "d1",
        1,
        base.content,
        available({ ...remote, revision: 1 }),
        4,
      ),
    ).toBe(false);
  },
);
it("未发生派生的纯版本推进保留实际基线分类", () => {
  const base = record(1, "base");
  expect(
    classifyDraftObservation(
      base,
      record(1, "local").content,
      { kind: "available", draft: { ...base, revision: 2 }, capabilities },
      none,
    ).kind,
  ).toBe("same_baseline");
});
it("能力故障不能借旧能力把新增预览当作派生事实", () => {
  const base = previewBase();
  expect(
    classifyDraftObservation(
      base,
      record(1, "local").content,
      {
        kind: "available",
        draft: { ...base, revision: 3, content: previewDerived() },
        capabilities: { ...capabilities, error: "failed" },
      },
      none,
    ).kind,
  ).toBe("conflict");
});
it("相同派生结果在保存请求仍在途时不提高基线", () => {
  const base = previewBase();
  expect(
    classifyDraftObservation(
      base,
      record(1, "local").content,
      {
        kind: "available",
        draft: { ...base, revision: 3, content: previewDerived() },
        capabilities,
      },
      { kind: "save", revision: 2, requestEnded: false },
    ).kind,
  ).toBe("blocked");
});
it("确认快照自身已导出也不提供资格", () => {
  const s = snapshot();
  s.remote.exportedRequestId = "42";
  expect(
    selectionMatches(s, "d1", 1, s.local, available(record(2, "remote")), 4),
  ).toBe(false);
});
it("确认快照的远端结构损坏不能由相同正文豁免", () => {
  const s = snapshot();
  s.remote.content.pending = [] as any;
  expect(
    selectionMatches(s, "d1", 1, s.local, available(record(2, "remote")), 4),
  ).toBe(false);
});
