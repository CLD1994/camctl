import { it, expect, vi } from "vitest";
import * as sessionModule from "../../src/web/session";
import type { Draft } from "../../src/server/models";
const draft = (id: string): Draft => ({
  id,
  revision: 1,
  content: { text: '{"name":"计划","actions":[]}' },
  createdAt: "2026-01-01 00:00:00",
  updatedAt: "2026-01-01 00:00:00",
});
it("重载准备保存全部开放会话，包括非当前会话的未完成输入", async () => {
  const writes: string[] = [];
  const sessions = ["a", "b"].map(
    (id) =>
      new sessionModule.DraftSession(
        draft(id),
        {
          read: async () => draft(id),
          save: async (id, revision, content) => {
            writes.push(id);
            return { ...draft(id), revision: revision + 1, content };
          },
        },
        () => {},
      ),
  );
  sessions[0].edit({ text: "{" });
  sessions[1].edit({ text: '{"name":' });
  await sessionModule.prepareSessionsForReload(sessions, async () => {});
  expect(writes).toEqual(["a", "b"]);
  expect(sessions.every((s) => s.saved)).toBe(true);
});
it("保存结果未知时准备失败并指出阶段，输入不丢失", async () => {
  const s = new sessionModule.DraftSession(
    draft("a"),
    {
      read: async () => {
        throw Error("offline");
      },
      save: async () => {
        throw Error("lost");
      },
    },
    () => {},
  );
  s.edit({ text: "{" });
  await expect(
    sessionModule.prepareSessionsForReload([s], async () => {}),
  ).rejects.toThrow("尚未开始能力重载");
  expect(s.content.text).toBe("{");
  expect(s.saved).toBe(false);
});
it.each(["append", "delete", "export"])(
  "未核实的%s阻止能力重载",
  async (state) => {
    const s = new sessionModule.DraftSession(
      draft("a"),
      { read: async () => draft("a"), save: vi.fn() },
      () => {},
    );
    if (state === "append") s.lockAppend();
    else if (state === "delete") s.deletionState = "unknown";
    else {
      s.beginExport();
      s.exportUnknown();
    }
    await expect(
      sessionModule.prepareSessionsForReload([s], async () => {}),
    ).rejects.toThrow("尚未开始能力重载");
  },
);
it("导出核实没有同次能力观察时保持未知，后续一致观察可以恢复", async () => {
  const initial = draft("a");
  const actual = { ...initial, revision: 2 };
  const s = new sessionModule.DraftSession(
    initial,
    { read: async () => actual, save: vi.fn() },
    () => {},
  );
  s.beginExport();
  s.exportUnknown();
  await s.checkExport();
  expect(s.exportState).toBe("unknown");
  expect(s.error).toContain("能力");
  s.observe(actual, s.exportToken, {
    active: { devices: [] },
    error: null,
    generation: 2,
    version: "k2",
  });
  expect(s.exportState).toBe("editable");
  expect(s.revision).toBe(2);
});
