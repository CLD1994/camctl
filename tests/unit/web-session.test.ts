import { expect, it, vi } from "vitest";
import { DraftSession, type DraftTransport } from "../../src/web/session";
import type { Draft, DraftContent } from "../../src/server/models";
const draft = (): Draft => ({
  id: "d1",
  revision: 1,
  content: { text: "{}" },
  createdAt: "2026-01-01 00:00:00",
  updatedAt: "2026-01-01 00:00:00",
});
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}
it("旧保存回执不覆盖新输入，后续写入使用新的revision", async () => {
  let saved = draft();
  const first = deferred<Draft>();
  const transport: DraftTransport = {
    save: vi.fn(async (_id, revision, content) => {
      if (revision === 1) return first.promise;
      saved = { ...saved, revision: revision + 1, content };
      return saved;
    }),
    read: async () => saved,
  };
  const session = new DraftSession(saved, transport, () => {});
  session.edit({ text: '{"name":"A"}' });
  const flushing = session.flush();
  session.edit({ text: '{"name":"B"}' });
  first.resolve({ ...saved, revision: 2, content: { text: '{"name":"A"}' } });
  await flushing;
  expect(session.content.text).toBe('{"name":"B"}');
  expect(session.saved).toBe(true);
  expect(session.revision).toBe(3);
  expect(transport.save).toHaveBeenNthCalledWith(2, "d1", 2, {
    text: '{"name":"B"}',
  });
});
it("保存响应丢失后按同一草稿已保存内容核实", async () => {
  let saved = draft();
  const transport: DraftTransport = {
    save: async (_id, revision, content) => {
      saved = { ...saved, revision: revision + 1, content };
      throw new TypeError("network");
    },
    read: async () => saved,
  };
  const session = new DraftSession(saved, transport, () => {});
  session.edit({ text: "{" });
  await session.flush();
  expect(session.saved).toBe(true);
  expect(session.revision).toBe(2);
});
it("保存失败保留当前输入与未保存状态", async () => {
  const transport: DraftTransport = {
    save: async () => {
      throw new Error("disk");
    },
    read: async () => draft(),
  };
  const session = new DraftSession(draft(), transport, () => {});
  session.edit({ text: "{" });
  await expect(session.flush()).rejects.toThrow();
  expect(session.content.text).toBe("{");
  expect(session.saved).toBe(false);
  expect(session.error).toBeTruthy();
});
