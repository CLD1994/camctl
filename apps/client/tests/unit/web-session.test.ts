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

it("完整版本比较包括预览三态和动作身份", () => {
  const a = {
    text: "{}",
    automaticPreviews: {
      intent: "enabled" as const,
      namespace: "x",
      next: 0,
      actions: [],
    },
  };
  expect(
    sameContent(a, {
      ...a,
      automaticPreviews: { ...a.automaticPreviews, intent: "disabled" },
    }),
  ).toBe(false);
});
it("观察后端派生版本保留本地未保存正文与资料并更新保存基线", async () => {
  const { coordinatePreviews, initializePreviewMetadata } =
    await import("../../src/shared/automatic-previews");
  const capabilities = {
    active: { devices: [] },
    error: null,
    generation: 2,
    version: "second",
  };
  const original = {
    ...draft(),
    content: initializePreviewMetadata(
      {
        text: JSON.stringify({
          name: "原名",
          actions: [
            {
              name: "自动",
              type: "obtain_action_outputs",
              params: { purpose: "auto_preview" },
            },
          ],
        }),
      },
      "disabled",
      "x",
    ),
  };
  const session = new DraftSession(
    original,
    {
      save: async (_id, revision, content) => ({
        ...original,
        revision: revision + 1,
        content,
      }),
      read: async () => original,
    },
    () => {},
  );
  const local = {
    ...original.content,
    text: original.content.text.replace("原名", "本地新名"),
    pending: { "/name": { kind: "json" as const, text: "{" } },
  };
  session.edit(local);
  session.observe(
    {
      ...original,
      revision: 2,
      content: coordinatePreviews(original.content, capabilities).content,
    },
    undefined,
    capabilities,
  );
  expect(session.revision).toBe(2);
  expect(session.content.text).toContain("本地新名");
  expect(session.content.pending).toEqual(local.pending);
  await session.flush();
  expect(session.revision).toBe(3);
});
it("未知保存后能力派生不以协调相等猜提交，原始写入依据才能证明提交", async () => {
  const initial = draft(),
    input = { text: '{"new":true}' };
  let actual = {
    ...initial,
    revision: 3,
    content: { text: '{"derived":true}' },
    lastWrite: { revision: 2, input, content: { text: '{"derived":true}' } },
  };
  const session = new DraftSession(
    initial,
    {
      save: async () => {
        throw Error("响应丢失");
      },
      read: async () => actual,
    },
    () => {},
  );
  session.edit(input);
  await session.flush();
  expect(session.revision).toBe(3);
  expect(session.content).toEqual(actual.content);
  expect(session.saved).toBe(true);
  const other = new DraftSession(
    initial,
    {
      save: async () => {
        throw Error("响应丢失");
      },
      read: async () => ({
        ...actual,
        lastWrite: { ...actual.lastWrite, input: initial.content },
      }),
    },
    () => {},
  );
  other.edit(input);
  await expect(other.flush()).rejects.toThrow();
  expect(other.content).toEqual(input);
  expect(other.saved).toBe(false);
});

it("导出未知遇到仅能力派生的新版本可证实未导出并保留新输入", async () => {
  const { initializePreviewMetadata, coordinatePreviews } =
    await import("../../src/shared/automatic-previews");
  const k = {
    active: { devices: [] },
    error: null,
    generation: 2,
    version: "v2",
  };
  const original = {
    ...draft(),
    content: initializePreviewMetadata(
      {
        text: JSON.stringify({
          name: "原",
          actions: [
            {
              name: "自动",
              type: "obtain_action_outputs",
              params: { purpose: "auto_preview" },
            },
          ],
        }),
      },
      "disabled",
      "ns",
    ),
  };
  const session = new DraftSession(
    original,
    { save: async () => original, read: async () => original },
    () => {},
  );
  session.beginExport();
  session.exportUnknown();
  const actual = {
    ...original,
    revision: 2,
    content: coordinatePreviews(original.content, k).content,
  };
  session.observe(actual, session.exportToken, k);
  expect(session.exportState).toBe("editable");
  expect(session.revision).toBe(2);
  expect(session.content).toEqual(actual.content);
});
it("未核实保存的观察不能抬高基线或消除差异", async () => {
  const initial = draft(),
    pending = deferred<Draft>();
  const session = new DraftSession(
    initial,
    {
      save: async () => pending.promise,
      read: async () => {
        throw Error("offline");
      },
    },
    () => {},
  );
  session.edit({ text: '{"local":true}' });
  const flushing = session.flush();
  session.observe(
    { ...initial, revision: 8, content: { text: '{"remote":true}' } },
    undefined,
    { active: { devices: [] }, error: null, generation: 2, version: "v2" },
  );
  expect(session.revision).toBe(1);
  expect(session.content.text).toContain("local");
  pending.resolve({ ...initial, revision: 8 });
  await expect(flushing).rejects.toThrow();
  expect(session.saved).toBe(false);
});
