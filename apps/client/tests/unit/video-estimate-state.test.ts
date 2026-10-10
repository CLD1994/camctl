import { expect, it } from "vitest";
import type { DraftContent } from "../../src/server/models";
import type { CapabilityState } from "../../src/shared/automatic-previews";
import { estimateDraftAction } from "../../src/web/video-estimate-state";
import { capsFor, record } from "./video-estimate-fixtures";

const observed: CapabilityState = {
  active: capsFor(
    {
      bitrate_mbps: 130,
      duration: {
        method: "direct",
        seconds: { source: "parameter", path: "/duration_s" },
      },
    },
    {
      properties: {
        type: { const: "record" },
        duration_s: { type: "number", title: "录像时长" },
      },
    },
  ),
  error: null,
  generation: 1,
  version: "v1",
};
const content: DraftContent = {
  text: JSON.stringify({
    actions: [record({ duration_s: 60 }), record({ duration_s: 30 })],
  }),
};
const pending = (path: string): DraftContent => ({
  ...content,
  pending: { [path]: { kind: "json", text: "{" } },
});
// 删除任何祖先/选择/参数 pending 分支会使对应行错误地继续显示旧数值。
it.each([
  "",
  "/actions",
  "/actions/0",
  "/actions/0/device_id",
  "/actions/0/type",
  "/actions/0/params",
  "/actions/0/params/type",
  "/actions/0/params/unrelated",
  "/actions/0/params/a~1b",
  "/actions/0/params/01",
])("相关输入 %s 撤下数值并保留原文", (path) => {
  const draft = pending(path),
    before = JSON.stringify(draft);
  expect(estimateDraftAction(draft, 0, observed, "idle")).toMatchObject({
    kind: "unavailable",
    reason: "input_unfinished",
  });
  expect(JSON.stringify(draft)).toBe(before);
});
it.each([
  "/name",
  "/actions/0/scheduled_at",
  "/actions/0/policy",
  "/actions/0/params2",
  "/actions/1/params",
  "/other/01",
])("无关输入 %s 保持当前估算", (path) => {
  expect(estimateDraftAction(pending(path), 0, observed, "idle")).toMatchObject(
    { kind: "ready", sizeBytes: 975000000 },
  );
});
it.each([
  "/actions/01/params",
  "/actions/-/params",
  "/actions/2/params",
  "/actions/0/params/bad~2key",
  "actions",
  "/actions/0/policy/01/x",
])("无法解释范围 %s 暂停", (path) => {
  const draft = path.includes("policy")
    ? {
        ...pending(path),
        text: JSON.stringify({
          actions: [{ ...record({ duration_s: 60 }), policy: [] }],
        }),
      }
    : pending(path);
  expect(estimateDraftAction(draft, 0, observed, "idle")).toMatchObject({
    kind: "unavailable",
    reason: "pending_scope_unknown",
  });
});
it("相关输入优先于未知范围", () => {
  const draft = {
    ...content,
    pending: {
      ...pending("/actions/01").pending,
      ...pending("/actions/0/params").pending,
    },
  };
  expect(estimateDraftAction(draft, 0, observed, "idle")).toMatchObject({
    reason: "input_unfinished",
  });
});
it("能力不可用优先于输入未完成并保留诊断", () => {
  expect(
    estimateDraftAction(
      pending(""),
      0,
      { ...observed, active: null, error: "说明文件缺失" },
      "updating",
    ),
  ).toMatchObject({
    reason: "capabilities_unavailable",
    diagnostic: "说明文件缺失",
  });
});
it.each(["updating", "unconfirmed"] as const)(
  "重载阶段 %s 撤下数值",
  (phase) => {
    expect(estimateDraftAction(content, 0, observed, phase)).toMatchObject({
      reason: phase === "updating" ? "rules_updating" : "reload_unconfirmed",
    });
  },
);
it("已知非视频动作优先隐藏", () => {
  expect(
    estimateDraftAction(
      { text: '{"actions":[{"type":"camera_take_photo"}]}' },
      0,
      { ...observed, active: null },
      "updating",
    ),
  ).toEqual({ kind: "hidden" });
});
it.each(["{", '{"actions":null}'])(
  "无法解释正文保留并返回输入原因 %s",
  (text) => {
    const draft = { text };
    expect(estimateDraftAction(draft, 0, observed, "idle")).toMatchObject({
      reason: "input_unfinished",
    });
    expect(draft.text).toBe(text);
  },
);
it.each([-1, 2, 0.5])("不存在的动作位置 %s 无数值", (index) => {
  expect(estimateDraftAction(content, index, observed, "idle")).toMatchObject({
    reason: "selection_invalid",
  });
});
it("未选择动作类型不隐藏修正原因", () => {
  expect(
    estimateDraftAction({ text: '{"actions":[{}]}' }, 0, observed, "idle"),
  ).toMatchObject({ reason: "selection_invalid" });
});
it("沿用旧说明附警告且不改写输入", () => {
  expect(
    estimateDraftAction(
      content,
      0,
      { ...observed, error: "新文件无效" },
      "idle",
    ),
  ).toMatchObject({
    kind: "ready",
    warning: expect.stringMatching(/此前/),
    sizeBytes: 975000000,
  });
});
it("引用缺失提供Schema标题与实际路径", () => {
  expect(
    estimateDraftAction(
      { text: JSON.stringify({ actions: [record()] }) },
      0,
      observed,
      "idle",
    ),
  ).toMatchObject({
    reason: "value_missing",
    path: "/duration_s",
    label: "录像时长",
  });
});
it("完整参数错误优先于公式所需字段以外的问题", () => {
  expect(
    estimateDraftAction(
      {
        text: JSON.stringify({
          actions: [record({ duration_s: 60, extra: true })],
        }),
      },
      0,
      observed,
      "idle",
    ),
  ).toMatchObject({ reason: "params_invalid" });
});
it("未声明估算优先于非法参数", () => {
  expect(
    estimateDraftAction(
      { text: JSON.stringify({ actions: [record({ extra: true })] }) },
      0,
      { ...observed, active: capsFor() },
      "idle",
    ),
  ).toMatchObject({ reason: "not_provided" });
});
