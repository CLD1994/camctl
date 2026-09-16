import { expect, it, vi } from "vitest";
import {
  DraftSession,
  sameContent,
  type DraftTransport,
} from "../../src/web/session";
import type { Draft, DraftContent } from "../../src/server/models";
const draft = (): Draft => ({
  id: "d1",
  revision: 1,
  content: { text: "{}" },
  createdAt: "2026-01-01 00:00:00",
  updatedAt: "2026-01-01 00:00:00",
});
it("当前文本相同时其他类型内容的变化仍需保存", () => {
  const first: DraftContent = {
    text: "{}",
    actionVariants: {
      "0": [{ type: "camera_record", fields: { device_id: "a" }, pending: {} }],
    },
  };
  const next = structuredClone(first);
  next.actionVariants!["0"][0].fields.device_id = "b";
  expect(sameContent(first, next)).toBe(false);
  expect(sameContent(first, structuredClone(first))).toBe(true);
});
it.each([true, false])(
  "保存响应丢失时核实类型内容是否完整：%s",
  async (complete) => {
    const content: DraftContent = {
      text: "{}",
      actionVariants: {
        "0": [
          {
            type: "camera_record",
            fields: { device_id: "cam" },
            pending: { "/params": { kind: "json", text: "{" } },
          },
        ],
      },
    };
    let actual = draft();
    const session = new DraftSession(
      draft(),
      {
        save: async (_id, revision, value) => {
          actual = {
            ...draft(),
            revision: revision + 1,
            content: complete ? structuredClone(value) : { text: value.text },
          };
          throw new Error("响应丢失");
        },
        read: async () => actual,
      },
      () => {},
    );
    session.edit(content);
    if (complete) await session.flush();
    else await expect(session.flush()).rejects.toThrow();
    expect(session.saved).toBe(complete);
    expect(session.content).toEqual(content);
    if (!complete) expect(session.conflict).toEqual(actual);
  },
);
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

it("删除等待自动保存完成并锁定后续编辑", async () => {
  const saving = deferred<Draft>(),
    removed = deferred<void>();
  const session = new DraftSession(
    draft(),
    { save: async () => saving.promise, read: async () => draft() },
    () => {},
  );
  session.edit({ text: "{" });
  let deletedRevision = 0;
  const operation = session.delete(
    async (revision) => {
      deletedRevision = revision;
      await removed.promise;
    },
    async () => undefined,
  );
  expect(deletedRevision).toBe(0);
  saving.resolve({ ...draft(), revision: 2, content: { text: "{" } });
  await vi.waitFor(() => expect(session.deletionState).toBe("deleting"));
  expect(session.editable).toBe(false);
  expect(() => session.edit({ text: "resurrect" })).toThrow();
  expect(deletedRevision).toBe(2);
  removed.resolve();
  await operation;
  expect(session.deletionState).toBe("deleted");
});
it.each(["missing", "present", "unavailable"] as const)(
  "删除响应丢失核实为 %s",
  async (result) => {
    const session = new DraftSession(
      draft(),
      { save: async () => draft(), read: async () => draft() },
      () => {},
    );
    const operation = session.delete(
      async () => {
        throw new Error("network");
      },
      async () => {
        if (result === "unavailable") throw new Error("offline");
        return result === "present" ? draft() : undefined;
      },
    );
    if (result === "missing") await operation;
    else await expect(operation).rejects.toThrow();
    expect(session.deletionState).toBe(
      result === "missing"
        ? "deleted"
        : result === "present"
          ? "idle"
          : "unknown",
    );
    expect(session.editable).toBe(result === "present");
    expect(session.content).toEqual(draft().content);
  },
);
it("删除未知状态仅在可靠核实不存在后结束", async () => {
  const session = new DraftSession(
    draft(),
    { save: async () => draft(), read: async () => draft() },
    () => {},
  );
  await expect(
    session.delete(
      async () => {
        throw new Error("network");
      },
      async () => {
        throw new Error("offline");
      },
    ),
  ).rejects.toThrow();
  await session.checkDeletion(async () => undefined);
  expect(session.deletionState).toBe("deleted");
});
