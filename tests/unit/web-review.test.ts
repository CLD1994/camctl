import { expect, it, vi } from "vitest";
import {
  editValue,
  setValue,
  editPlanText,
  appendDraftAction,
  parseDraft,
} from "../../src/web/editing";
import { DraftSession, type DraftTransport } from "../../src/web/session";
import type { Draft } from "../../src/server/models";
const draft = (): Draft => ({
  id: "d",
  revision: 1,
  content: { text: "A0" },
  createdAt: "2026-01-01 00:00:00",
  updatedAt: "2026-01-01 00:00:00",
});
it("普通父JSON编辑不能用旧对象覆盖后代未完成输入", () => {
  const before = {
    text: JSON.stringify({
      name: "P",
      actions: [{ params: { count: 3, note: "old" } }],
    }),
    pending: {
      "/actions/0/params/count": { kind: "number" as const, text: "1e" },
    },
  };
  expect(() =>
    editValue(
      before,
      ["actions", 0, "params"],
      '{"count":3,"note":"changed"}',
      "json",
    ),
  ).toThrow();
  expect(before.pending["/actions/0/params/count"].text).toBe("1e");
});
it("明确省略只解除所删除子树的待完成输入", () => {
  const before = {
    text: JSON.stringify({
      name: "P",
      actions: [{ params: { count: 3 }, policy: { max_delay_ms: 0 } }],
    }),
    pending: {
      "/actions/0/params/count": { kind: "number" as const, text: "1e" },
      "/actions/0/policy/max_delay_ms": { kind: "number" as const, text: "-" },
    },
  };
  expect(
    setValue(before, ["actions", 0, "params"], undefined, true).pending,
  ).toEqual({
    "/actions/0/policy/max_delay_ms": { kind: "number", text: "-" },
  });
});
it.each([
  [true, true],
  [true, false],
  [false, true],
  [false, false],
])(
  "待定保存核实两次失败后恢复：已提交=%s，后续新输入=%s",
  async (committed, changed) => {
    let actual = draft(),
      reads = 0,
      calls = 0;
    const transport: DraftTransport = {
      save: vi.fn(async (_id, revision, content) => {
        calls++;
        if (calls === 1) {
          if (committed) actual = { ...actual, revision: 2, content };
          throw new Error("lost");
        }
        if (revision !== actual.revision) throw new Error("revision_conflict");
        actual = { ...actual, revision: revision + 1, content };
        return actual;
      }),
      read: async () => {
        if (++reads <= 2) throw new Error("offline");
        return actual;
      },
    };
    const session = new DraftSession(actual, transport, () => {});
    session.edit({ text: "A" });
    await expect(session.flush()).rejects.toThrow();
    if (changed) session.edit({ text: "B" });
    await expect(session.flush()).rejects.toThrow();
    expect(calls).toBe(1);
    await session.flush();
    expect(actual.content.text).toBe(changed ? "B" : "A");
    expect(session.content.text).toBe(changed ? "B" : "A");
    expect(session.saved).toBe(true);
    expect(session.revision).toBe(committed && changed ? 3 : 2);
  },
);
it("已导出观察不会覆盖尚未保存的额外输入", () => {
  const session = new DraftSession(
    draft(),
    { save: async () => draft(), read: async () => draft() },
    () => {},
  );
  session.edit({ text: "额外输入" });
  session.observe({ ...draft(), revision: 2, exportedRequestId: "r" });
  expect(session.exportedRequestId).toBe("r");
  expect(session.recoveryContent?.text).toBe("额外输入");
  expect(() => session.edit({ text: "其他" })).toThrow();
});
it("额外内容明确保存为新草稿后，后续导出观察不重复提供同一恢复副本", () => {
  const session = new DraftSession(
    draft(),
    { save: async () => draft(), read: async () => draft() },
    () => {},
  );
  session.edit({ text: "额外输入" });
  const actual = { ...draft(), exportedRequestId: "r" };
  session.observe(actual);
  expect(session.saved).toBe(false);
  session.completeRecovery();
  session.observe(actual);
  expect(session.recoveryContent).toBeUndefined();
  expect(session.saved).toBe(true);
});
it("无法解释的保存记录保留当前输入与实际记录，不采用其revision", async () => {
  const actual = { ...draft(), revision: 7, content: { text: "其他内容" } };
  let puts = 0;
  const session = new DraftSession(
    draft(),
    {
      save: async () => {
        puts++;
        throw Error("lost");
      },
      read: async () => actual,
    },
    () => {},
  );
  session.edit({ text: "A" });
  await expect(session.flush()).rejects.toThrow();
  session.edit({ text: "B" });
  await expect(session.flush()).rejects.toThrow();
  expect(session.content.text).toBe("B");
  expect(session.conflict).toEqual(actual);
  expect(session.revision).toBe(1);
  expect(puts).toBe(1);
});
it("导出未知期间锁定所有写入口，旧读取不能解除未知", async () => {
  const session = new DraftSession(
    draft(),
    { save: async () => draft(), read: async () => draft() },
    () => {},
  );
  session.beginExport();
  session.exportUnknown();
  session.observe(draft());
  expect(session.editable).toBe(false);
  expect(() => session.edit({ text: "B" })).toThrow();
  await expect(session.flush()).rejects.toThrow();
  await session.checkExport();
  expect(session.editable).toBe(true);
});
it("导出未知读到不同基线仍保持未知并展示实际记录", async () => {
  const actual = { ...draft(), revision: 4, content: { text: "different" } };
  const session = new DraftSession(
    draft(),
    { save: async () => draft(), read: async () => actual },
    () => {},
  );
  session.beginExport();
  session.exportUnknown();
  await session.checkExport();
  expect(session.editable).toBe(false);
  expect(session.conflict).toEqual(actual);
});
it("明确导出验证失败后恢复编辑，已确认导出不会被失败或旧观察撤销", () => {
  const session = new DraftSession(
    draft(),
    { save: async () => draft(), read: async () => draft() },
    () => {},
  );
  session.beginExport();
  session.exportFailed();
  expect(session.editable).toBe(true);
  session.beginExport();
  session.confirmExport("r");
  session.exportFailed();
  session.observe(draft(), session.exportToken);
  expect(session.editable).toBe(false);
  expect(session.exportedRequestId).toBe("r");
});
it("保存待定时发现已导出会保留新输入并停止写入", async () => {
  let puts = 0;
  const session = new DraftSession(
    draft(),
    {
      save: async () => {
        puts++;
        throw Error("lost");
      },
      read: async () => ({ ...draft(), exportedRequestId: "r" }),
    },
    () => {},
  );
  session.edit({ text: "B" });
  await expect(session.flush()).rejects.toThrow();
  expect(session.recoveryContent).toEqual({ text: "B" });
  expect(session.editable).toBe(false);
  await expect(session.flush()).rejects.toThrow();
  expect(puts).toBe(1);
});
it.each(
  (
    [
      ["actions", 0, "policy"],
      ["actions", 0, "params", "nested"],
      ["actions", 1, "params"],
    ] as const
  ).map((path) => [path]),
)("策略和嵌套JSON保留后代pending：%j", (path) => {
  const before = {
    text: JSON.stringify({
      name: "P",
      actions: [
        { policy: { max_delay_ms: 0 }, params: { nested: { a: 1 } } },
        { params: { count: 3 } },
      ],
    }),
    pending: {
      "/actions/0/policy/max_delay_ms": { kind: "number" as const, text: "-" },
      "/actions/0/params/nested/a": {
        kind: "json" as const,
        text: '{"x":1,"x":2}',
      },
      "/actions/1/params/count": { kind: "number" as const, text: "1e" },
    },
  };
  expect(() => editValue(before, [...path], "{}", "json")).toThrow();
  expect(() => editPlanText(before, before.text)).toThrow();
  const sibling = setValue(before, ["name"], "renamed");
  expect(sibling.pending).toEqual(before.pending);
  const added = appendDraftAction(sibling, {
    name: "新增",
    type: "report_status",
  });
  expect(added.pending).toEqual(before.pending);
  expect(parseDraft(added).actions).toHaveLength(3);
});
it("明确替换params仅清该子树，保留策略与其他动作pending", () => {
  const before = {
    text: JSON.stringify({
      actions: [{ params: { count: 3 }, policy: { max_delay_ms: 0 } }, {}],
    }),
    pending: {
      "/actions/0/params/count": { kind: "number" as const, text: "1e" },
      "/actions/0/policy/max_delay_ms": { kind: "number" as const, text: "-" },
      "/actions/1/params": { kind: "json" as const, text: "{" },
    },
  };
  const next = setValue(
    before,
    ["actions", 0, "params"],
    { type: "preset", count: "3" },
    false,
    true,
  );
  expect(parseDraft(next).actions[0].params).toEqual({
    type: "preset",
    count: "3",
  });
  expect(next.pending).toEqual({
    "/actions/0/policy/max_delay_ms": { kind: "number", text: "-" },
    "/actions/1/params": { kind: "json", text: "{" },
  });
});
