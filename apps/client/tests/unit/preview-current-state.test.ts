import { expect, it } from "vitest";
import { linked } from "../helpers/preview-renaming";
import { editValue, setValue, removeAction } from "../../src/web/editing";
import { switchActionType } from "../../src/web/action-drafts";
import {
  coordinatePreviews,
  type CapabilityState,
} from "../../src/shared/automatic-previews";
import { loadCapabilities } from "../../src/shared/capabilities";
const k = (support = true): CapabilityState => ({
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
  version: "k",
});
const waiting = () => setValue(linked(), ["actions", 0, "name"], "B");
it("其他拍摄改名消除重名后，共享协调恢复全部当前来源", () => {
  const result = coordinatePreviews(
    setValue(waiting(), ["actions", 1, "name"], "C"),
    k(),
  );
  expect(
    JSON.parse(result.content.text)
      .actions.slice(2)
      .map((a: any) => a.params.source.action_name),
  ).toEqual(["B", "C", "A"]);
  expect(result.content.automaticPreviews!.actions[2]).toMatchObject({
    id: "n:2",
    sourceId: "n:0",
    rename: { pending: false, actionName: "B" },
  });
  expect(coordinatePreviews(result.content, k()).content).toEqual(
    result.content,
  );
});
it("删除其他拍摄消除重名后，共享协调恢复剩余来源", () => {
  const result = coordinatePreviews(removeAction(waiting(), 1), k());
  expect(
    JSON.parse(result.content.text).actions.map((a: any) => a.name),
  ).toEqual(["B", "自动A", "手动"]);
  expect(
    JSON.parse(result.content.text).actions[1].params.source.action_name,
  ).toBe("B");
  expect(result.content.automaticPreviews!.actions[1]).toMatchObject({
    id: "n:2",
    sourceId: "n:0",
    rename: { pending: false },
  });
});
it.each(["switch", "pointer"])(
  "待恢复时明确非拍摄不受名称等待遮蔽 %s",
  (method) => {
    const c =
      method === "switch"
        ? switchActionType(waiting(), 0, "report_status")
        : editValue(
            waiting(),
            ["actions", "0", "type"],
            '"report_status"',
            "json",
          );
    const result = coordinatePreviews(c, k());
    expect(
      JSON.parse(result.content.text).actions.map((a: any) => a.name),
    ).toEqual(["B", "B", "自动B", "手动"]);
  },
);
it("已可靠不支持时名称等待不能阻止移除", () => {
  const result = coordinatePreviews(
    setValue(waiting(), ["actions", 1, "name"], "C"),
    k(false),
  );
  expect(
    JSON.parse(result.content.text).actions.map((a: any) => a.name),
  ).toEqual(["B", "C", "手动"]);
});
it.each([{ other: null }, { other: [] }, { other: 42 }])(
  "其他动作畸形仍保留目标的新未完成文本 $other",
  ({ other }) => {
    const c = linked(),
      root = JSON.parse(c.text);
    root.actions[1] = other;
    c.text = JSON.stringify(root);
    const result = editValue(
      c,
      ["actions", "0", "name"],
      '"unfinished',
      "json",
    );
    expect(result.text).toBe(c.text);
    expect(result.automaticPreviews).toEqual(c.automaticPreviews);
    expect(result.pending).toEqual({
      "/actions/0/name": { kind: "json", text: '"unfinished' },
    });
  },
);
it("整体JSON尚未完成时，容器未知仍保留新字段原文", () => {
  const c = waiting();
  c.text = '{"actions":[{"name":"A"}';
  const result = editValue(c, ["actions", "0", "name"], '"unfinished', "json");
  expect(result.text).toBe(c.text);
  expect(result.automaticPreviews).toEqual(c.automaticPreviews);
  expect(result.pending).toEqual({
    "/actions/0/name": { kind: "json", text: '"unfinished' },
  });
});
it.each(["手动", "自动B"])(
  "名称唯一性覆盖全部动作，冲突项改名或删除后恢复 %s",
  (name) => {
    for (const operation of ["rename", "remove"]) {
      const c = setValue(linked(), ["actions", 0, "name"], name),
        index = name === "手动" ? 4 : 3;
      const edited =
        operation === "rename"
          ? setValue(c, ["actions", index, "name"], "新名字")
          : removeAction(c, index);
      const result = coordinatePreviews(edited, k()).content;
      const auto = result.automaticPreviews!.actions.findIndex(
        (a) => a.id === "n:2",
      );
      expect(
        JSON.parse(result.text).actions[auto].params.source.action_name,
      ).toBe(name);
      expect(result.automaticPreviews!.actions[auto].rename!.pending).toBe(
        false,
      );
    }
  },
);
it.each(["error", "missing", "unknownType", "paramsPending", "typePending"])(
  "名称恢复与未知适用性分别判定 %s",
  (state) => {
    let c = setValue(waiting(), ["actions", 1, "name"], "C"),
      cap = k(false);
    if (state === "error") cap.error = "加载失败";
    if (state === "missing") cap.active = null;
    if (state === "unknownType")
      c = setValue(c, ["actions", 0, "type"], "future_camera");
    if (state === "paramsPending")
      c.pending = { "/actions/0/params": { kind: "json", text: "{" } };
    if (state === "typePending")
      c.pending = { "/actions/0/type": { kind: "json", text: '"future' } };
    const result = coordinatePreviews(c, cap),
      index = result.content.automaticPreviews!.actions.findIndex(
        (a) => a.id === "n:2",
      );
    expect(index).toBeGreaterThanOrEqual(0);
    expect(
      JSON.parse(result.content.text).actions[index].params.source.action_name,
    ).toBe("B");
    expect(
      result.content.automaticPreviews!.actions[index].rename!.pending,
    ).toBe(false);
    expect(result.issues.length).toBeGreaterThan(0);
    expect(result.content.pending ?? {}).toEqual(c.pending ?? {});
  },
);
it("只看当前参数选择的明确不支持，名称或时间pending不遮蔽它", () => {
  const cap = k(),
    parameter = cap.active!.devices[0].actions[0].parameter_types[0];
  cap.active!.devices[0].actions[0].parameter_types.push({
    ...parameter,
    type: "no_preview",
    preview_supported: false,
    schema: {
      ...parameter.schema,
      properties: { type: { const: "no_preview" } },
    },
  });
  const c = setValue(waiting(), ["actions", 0, "params", "type"], "no_preview");
  c.pending = {
    "/actions/0/name": { kind: "json", text: '"unfinished' },
    "/actions/0/scheduled_at": { kind: "json", text: '"unfinished' },
  };
  const result = coordinatePreviews(c, cap).content;
  expect(result.automaticPreviews!.actions.map((a) => a.id)).toEqual([
    "n:0",
    "n:1",
    "n:3",
    "n:4",
  ]);
  expect(result.pending).toEqual(c.pending);
});
it("名称pending不能从旧名字清除恢复等待", () => {
  const c = setValue(waiting(), ["actions", 1, "name"], "C");
  c.pending = { "/actions/0/name": { kind: "json", text: '"unfinished' } };
  const result = coordinatePreviews(c, k()).content;
  expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
    "A",
  );
  expect(result.automaticPreviews!.actions[2].rename!.pending).toBe(true);
});
it.each(["/actions/0/type", "/actions/0"])(
  "未知类型pending不能用旧非拍摄值删除 %s",
  (path) => {
    const c = switchActionType(waiting(), 0, "report_status");
    c.pending = { [path]: { kind: "json", text: "{" } };
    const result = coordinatePreviews(c, k(false)).content;
    expect(result.automaticPreviews!.actions.some((a) => a.id === "n:2")).toBe(
      true,
    );
  },
);
it.each([
  "/actions/2",
  "/actions/2/params/source",
  "/actions/2/params/purpose",
])("自动字段pending阻止恢复和能力清理 %s", (path) => {
  const c = setValue(waiting(), ["actions", 1, "name"], "C");
  c.pending = { [path]: { kind: "json", text: "{" } };
  const result = coordinatePreviews(c, k(false));
  expect(result.content).toEqual(c);
  expect(result.issues.length).toBeGreaterThan(0);
});
it.each(["projection", "duplicate", "missing"])(
  "来源证据不可靠时不执行恢复或不支持清理 %s",
  (fault) => {
    const c = setValue(waiting(), ["actions", 1, "name"], "C");
    if (fault === "projection") {
      const root = JSON.parse(c.text);
      root.actions[2].params.source.action_name = "外部";
      c.text = JSON.stringify(root);
    }
    if (fault === "duplicate") c.automaticPreviews!.actions[3].sourceId = "n:0";
    if (fault === "missing") c.automaticPreviews!.actions[0].id = "missing";
    expect(coordinatePreviews(c, k(false)).content).toEqual(c);
  },
);
it("删除明确不适用的自动项消除名称冲突后同次协调恢复，下一次幂等", () => {
  const cap = k(),
    c = setValue(linked(), ["actions", 0, "name"], "自动B");
  const changed = switchActionType(c, 1, "report_status");
  const result = coordinatePreviews(changed, cap).content;
  expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
    "自动B",
  );
  expect(coordinatePreviews(result, cap).content).toEqual(result);
});
it.each(["mismatch", "autoPending", "unresolvedBody"])(
  "关联未知时新pending续写保留全部原文及旧资料 %s",
  (state) => {
    const c = waiting();
    c.pending = { "/actions/0/name": { kind: "json", text: '"old' } };
    if (state === "mismatch") c.automaticPreviews!.actions.pop();
    if (state === "autoPending")
      c.pending["/actions/2/params/purpose"] = { kind: "json", text: '"man' };
    if (state === "unresolvedBody") c.text = '{"actions":';
    const result = editValue(
      c,
      ["actions", "0", "name"],
      '"new unfinished',
      "json",
    );
    expect(result.text).toBe(c.text);
    expect(result.automaticPreviews).toEqual(c.automaticPreviews);
    expect(result.pending).toEqual({
      ...c.pending,
      "/actions/0/name": { kind: "json", text: '"new unfinished' },
    });
  },
);
it.each([
  { text: '{"actions":{}}' },
  { text: '{"actions":[null]}' },
  { text: '{"actions":[]}' },
])("正文明确路径无效继续拒绝 $text", (c) => {
  expect(() =>
    editValue(c, ["actions", "0", "name"], '"unfinished', "json"),
  ).toThrow();
});
it("能力错误仍处理明确非拍摄，不借旧active删除其他自动项", () => {
  const c = switchActionType(waiting(), 0, "report_status"),
    cap = k(false);
  cap.error = "加载失败";
  const result = coordinatePreviews(c, cap).content;
  expect(result.automaticPreviews!.actions.map((a) => a.id)).toEqual([
    "n:0",
    "n:1",
    "n:3",
    "n:4",
  ]);
});
it("能力错误只恢复已证明名称，不生成或维护时间", async () => {
  const { appendContentAction } =
    await import("../../src/shared/automatic-previews");
  let c = setValue(waiting(), ["actions", 1, "name"], "C");
  c = setValue(c, ["actions", 0, "scheduled_at"], "2026-10-11 01:00:00");
  c = appendContentAction(c, {
    name: "D",
    type: "camera_record",
    device_id: "cam",
    scheduled_at: "2026-10-11 01:00:00",
    params: { type: "photo" },
  });
  const cap = k(false);
  cap.error = "加载失败";
  const result = coordinatePreviews(c, cap).content,
    actions = JSON.parse(result.text).actions;
  expect(actions).toHaveLength(6);
  expect(actions[2].params.source.action_name).toBe("B");
  expect(actions[2].scheduled_at).toBe("2026-10-10 01:00:00");
  expect(actions[3].params.source.action_name).toBe("C");
});
it.each(["/actions/0/device_id", "/actions/0/params/type"])(
  "选择字段pending不得依据旧目录投影清理 %s",
  (path) => {
    const c = setValue(waiting(), ["actions", 1, "name"], "C");
    c.pending = { [path]: { kind: "json", text: '"unfinished' } };
    const result = coordinatePreviews(c, k(false)).content;
    expect(result.automaticPreviews!.actions.some((a) => a.id === "n:2")).toBe(
      true,
    );
    expect(result.pending).toEqual(c.pending);
  },
);
it("有效目标的完整名称编辑不依赖其他动作结构可解释", () => {
  const c = linked(),
    root = JSON.parse(c.text);
  root.actions[1] = null;
  c.text = JSON.stringify(root);
  const result = setValue(c, ["actions", "0", "name"], "C");
  expect(JSON.parse(result.text).actions[0].name).toBe("C");
  expect(JSON.parse(result.text).actions[1]).toBeNull();
  expect(result.automaticPreviews).toEqual(c.automaticPreviews);
});
it("会话按同一次能力观察协调其他动作带来的恢复", async () => {
  const { DraftSession } = await import("../../src/web/session");
  const draft = {
    id: "d",
    revision: 1,
    createdAt: "now",
    updatedAt: "now",
    content: waiting(),
  };
  let stored = draft;
  const session = new DraftSession(
    draft,
    {
      save: async (_id, revision, content) =>
        (stored = { ...stored, revision: revision + 1, content }),
      read: async () => stored,
    },
    () => {},
  );
  session.observe(draft, undefined, k());
  session.edit(setValue(session.content, ["actions", 1, "name"], "C"));
  expect(
    JSON.parse(session.content.text).actions[2].params.source.action_name,
  ).toBe("B");
  await session.flush();
  expect(session.saved).toBe(true);
});
it.each([4, 3])(
  "其他动作改名制造冲突后再改原来源，可靠归属仍可恢复 %s",
  (index) => {
    const conflicted = setValue(linked(), ["actions", index, "name"], "A");
    const edited = setValue(conflicted, ["actions", 0, "name"], "C");
    const result = coordinatePreviews(edited, k()).content;
    expect(JSON.parse(result.text).actions[2].params.source.action_name).toBe(
      "C",
    );
    expect(result.automaticPreviews!.actions[2]).toMatchObject({
      id: "n:2",
      sourceId: "n:0",
    });
  },
);
