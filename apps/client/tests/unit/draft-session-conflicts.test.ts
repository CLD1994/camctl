import { expect, it, vi } from "vitest";
import { DraftSession } from "../../src/web/session";
import { ActionUiIdentity } from "../../src/web/action-ui-identity";
import { HttpError } from "../../src/web/api";
import { DraftCopyOperation } from "../../src/web/draft-copy";
import type { Draft, DraftContent } from "../../src/server/models";
import { record, creation } from "../helpers/draft-conflicts";
import { initializePreviewMetadata } from "../../src/shared/automatic-previews";
import type { CapabilityState } from "../../src/shared/automatic-previews";
import { loadCapabilities } from "../../src/shared/capabilities";
import {
  cloneClientJson,
  parseClientJson,
  stringifyJson,
} from "../../src/shared/json";

function task6Input(): DraftContent {
  return parseClientJson(
    '{"text":"{","pending":{"":{"kind":"json","text":"{"}},"actionVariants":{"0":[{"type":"camera_record","fields":{"amount":1.0000000000000001},"pending":{}}]},"parameterVariants":{"0":[{"paramsText":"{\\"params\\":{\\"amount\\":1e-999}}","pending":{}}]},"objectVariants":{"0":{"/exposure":{"discriminator":"mode","variants":[{"valueText":"{\\"value\\":{\\"mode\\":\\"manual\\"}}","pending":{}}]}}},"automaticPreviews":{"namespace":"source","next":1,"intent":"unset","actions":[{"id":"source-id"}]}}',
  ) as DraftContent;
}
it("Task6 源完整输入已保存但独立另存未知责任仍须离页提醒", async () => {
  const base = record(1, "base"),
    s = new DraftSession(
      base,
      { save: async () => base, read: async () => base },
      () => {},
    );
  const op = new DraftCopyOperation(
    base.id,
    0,
    task6Input(),
    {
      create: async () => {
        throw Error("lost");
      },
      read: async () => {
        throw Error("offline");
      },
    },
    "independent",
  );
  await op.advance();
  Reflect.get(s, "registerCopyRecovery").call(s, op);
  expect(s.saved).toBe(true);
  expect(s.copyPendingDecision).toBe(false);
  expect(s.pendingCopyRecoveries).toHaveLength(1);
  expect(s.needsLeaveWarning).toBe(true);
});
it("Task6 放弃已创建安排后清洁源不因历史created事实持续离页提醒", async () => {
  const base = record(1, "base"),
    s = new DraftSession(
      base,
      { save: async () => base, read: async () => base },
      () => {},
    );
  const transport = {
    create: async (id: string, input: DraftContent) => ({
      kind: "created" as const,
      creation: { ...creation(input), attemptId: id },
      target: { kind: "deleted" as const },
    }),
    read: async () => {
      throw Error("unused");
    },
  };
  const op = s.beginCopy(transport, "created-history");
  await op.advance();
  expect(s.needsLeaveWarning).toBe(true);
  s.abandonCopy(s.copyDecision);
  expect(op.result?.kind).toBe("created");
  expect(s.content).toEqual(base.content);
  expect(s.pendingCopyRecoveries).toHaveLength(0);
  expect(s.needsLeaveWarning).toBe(false);
});
it.each(["adopt", "replace"] as const)(
  "Task6 %s显示失败放弃只结束本地选择，输入和独立责任保持",
  async (kind) => {
    vi.useFakeTimers();
    try {
      const base = record(1, "base"),
        remote = record(2, "remote"),
        input = task6Input();
      let backend = cloneClientJson(remote);
      const save = vi.fn(
        async (_id: string, revision: number, content: DraftContent) => {
          backend = {
            ...remote,
            revision: revision + 1,
            content: cloneClientJson(content),
          };
          return backend;
        },
      );
      const s = new DraftSession(
        base,
        { save, read: async () => backend },
        () => {},
      );
      s.edit(input);
      s.observe(remote);
      const preparedEdit = s.prepareEdit(input);
      s.bindWholeView(() => {
        throw Error("view");
      });
      if (kind === "adopt")
        expect(() =>
          s.adoptSavedVersion(s.prepareConflictSelection()),
        ).toThrow();
      else
        await expect(
          s.replaceSavedVersion(s.prepareConflictSelection()),
        ).rejects.toThrow();
      const stale = s.selectionDecision!;
      expect(stale.kind).toBe(kind);
      expect(stale.canAbandon).toBe(true);
      expect(stale.written).toEqual(kind === "replace" ? backend : undefined);
      // 独立追加和删除核实不是此选择拥有的责任。
      s.appendLocked = true;
      s.deletionState = "unknown";
      s.abandonSelection(stale);
      expect(stringifyJson(s.content)).toBe(stringifyJson(input));
      expect(s.selectionDecision).toBeUndefined();
      expect(s.appendLocked).toBe(true);
      expect(s.deletionState).toBe("unknown");
      expect(s.hasUnfinishedResponsibilities).toBe(true);
      expect(() => s.commitEdit(preparedEdit)).toThrow();
      expect(() => s.abandonSelection(stale)).toThrow();
      await expect(s.retrySelection(stale)).rejects.toThrow();
      s.schedule();
      await vi.advanceTimersByTimeAsync(900);
      expect(save).toHaveBeenCalledTimes(kind === "replace" ? 1 : 0);
      expect(backend.content).toEqual(
        kind === "replace" ? input : remote.content,
      );
      expect(s.canSave).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  },
);
it.each(["adopt", "replace"] as const)(
  "Task6 %s显示失败重试不重发且完整共同接纳",
  async (kind) => {
    const remote = record(2, "remote"),
      input = task6Input();
    let backend = cloneClientJson(remote),
      commits = 0;
    const save = vi.fn(
      async (_id: string, revision: number, content: DraftContent) => {
        backend = {
          ...remote,
          revision: revision + 1,
          content: cloneClientJson(content),
        };
        return backend;
      },
    );
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => backend },
      () => {},
    );
    s.edit(input);
    s.observe(remote);
    s.bindWholeView(() => {
      throw Error("view");
    });
    if (kind === "adopt")
      expect(() => s.adoptSavedVersion(s.prepareConflictSelection())).toThrow();
    else
      await expect(
        s.replaceSavedVersion(s.prepareConflictSelection()),
      ).rejects.toThrow();
    s.bindWholeView(() => () => {
      commits++;
    });
    expect(await s.retrySelection(s.selectionDecision)).toBe("completed");
    expect(commits).toBe(1);
    expect(s.selectionDecision).toBeUndefined();
    expect(s.content).toEqual(backend.content);
    expect(s.confirmedDraft).toEqual(backend);
    expect(save).toHaveBeenCalledTimes(kind === "replace" ? 1 : 0);
  },
);
it.each(["unavailable", "changed", "terminal"] as const)(
  "Task6 采用异常资格%s后只返回新确认，旧采用零提交",
  async (change) => {
    const remote = record(2, "remote"),
      input = task6Input();
    const current =
      change === "changed"
        ? record(3, "changed")
        : change === "terminal"
          ? { ...record(3, "remote"), exportedRequestId: "1" }
          : remote;
    const s = new DraftSession(
      record(1, "base"),
      {
        save: async () => {
          throw Error("PUT");
        },
        read: async () => current,
      },
      () => {},
    );
    s.edit(input);
    s.observe(remote);
    s.bindWholeView(() => {
      throw Error("view");
    });
    expect(() => s.adoptSavedVersion(s.prepareConflictSelection())).toThrow();
    if (change === "unavailable") {
      s.observeTarget({ kind: "unavailable", message: "offline" });
      s.observe(remote);
    } else s.observe(current);
    let commits = 0;
    s.bindWholeView(() => () => {
      commits++;
    });
    expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
    expect(commits).toBe(0);
    expect(stringifyJson(s.content)).toBe(stringifyJson(input));
    expect(s.canSave).toBe(false);
  },
);
it("Task6 adopt prepare重入观察在首个commit前拒绝", () => {
  const remote = record(2, "remote"),
    input = task6Input();
  let commits = 0;
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw Error("PUT");
      },
      read: async () => remote,
    },
    () => {},
  );
  s.edit(input);
  s.observe(remote);
  s.bindWholeView(() => {
    s.observe(record(3, "changed"));
    return () => {
      commits++;
    };
  });
  expect(() => s.adoptSavedVersion(s.prepareConflictSelection())).toThrow();
  expect(commits).toBe(0);
  expect(stringifyJson(s.content)).toBe(stringifyJson(input));
  expect(s.selectionPendingDecision).toBe(true);
});
it("Task6 初始采用prepare中的旧重试不能结束正在执行的选择", async () => {
  const remote = record(2, "remote"),
    input = task6Input();
  let nested: Promise<unknown> | undefined,
    busy = false;
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw Error("PUT");
      },
      read: async () => remote,
    },
    () => {},
  );
  s.edit(input);
  s.observe(remote);
  s.bindWholeView(() => {
    const snapshot = s.selectionDecision!;
    busy = snapshot.requestRunning;
    nested = s.retrySelection(snapshot);
    void nested.catch(() => {});
    throw Error("view");
  });
  expect(() => s.adoptSavedVersion(s.prepareConflictSelection())).toThrow();
  expect(busy).toBe(true);
  await expect(nested).rejects.toThrow();
  expect(stringifyJson(s.content)).toBe(stringifyJson(input));
  expect(s.selectionPendingDecision).toBe(true);
});
it("Task6 放弃后的新采用具有新责任代次，旧异常不可复活", () => {
  const remote = record(2, "remote");
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw Error("PUT");
      },
      read: async () => remote,
    },
    () => {},
  );
  s.edit(task6Input());
  s.observe(remote);
  s.bindWholeView(() => {
    throw Error("view");
  });
  expect(() => s.adoptSavedVersion(s.prepareConflictSelection())).toThrow();
  const first = s.selectionDecision!;
  s.abandonSelection(first);
  expect(() => s.adoptSavedVersion(s.prepareConflictSelection())).toThrow();
  expect(s.selectionDecision!.generation).toBeGreaterThan(first.generation);
});
it("Task6 追加锁内恢复可靠观察结束读取诊断，保留追加责任和完整基线", () => {
  const base = record(1, "base");
  const s = new DraftSession(
    base,
    { save: async () => base, read: async () => base },
    () => {},
  );
  s.lockAppend();
  s.observeTarget({ kind: "unavailable", message: "offline" });
  expect(s.saved).toBe(false);
  s.observeTarget({ kind: "available", draft: base });
  expect(s.saved).toBe(true);
  expect(s.appendLocked).toBe(true);
  expect(s.content).toEqual(base.content);
  expect(s.confirmedDraft).toEqual(base);
});
it.each(["retry", "abandon"] as const)(
  "Task6 替换已写入后远端再变，%s保留当前事实且不重发",
  async (action) => {
    vi.useFakeTimers();
    try {
      const remote = record(2, "remote"),
        input = task6Input();
      const written = {
          ...remote,
          revision: 3,
          content: cloneClientJson(input),
        },
        current = record(4, "current");
      const save = vi.fn(async () => written);
      const s = new DraftSession(
        record(1, "base"),
        { save, read: async () => current },
        () => {},
      );
      s.edit(input);
      s.observe(remote);
      s.bindWholeView(() => {
        throw Error("view");
      });
      await expect(
        s.replaceSavedVersion(s.prepareConflictSelection()),
      ).rejects.toThrow();
      s.observe(current);
      s.bindWholeView(() => () => {});
      if (action === "retry") {
        expect(await s.retrySelection(s.selectionDecision)).toBe("completed");
        expect(s.content).toEqual(current.content);
        expect(s.confirmedDraft).toEqual(current);
        expect(s.saved).toBe(true);
      } else {
        s.abandonSelection(s.selectionDecision);
        expect(stringifyJson(s.content)).toBe(stringifyJson(input));
        expect(s.confirmedDraft).toEqual(written);
        expect(s.lastReliableDraft).toEqual(current);
        expect(s.canSave).toBe(false);
      }
      s.schedule();
      await vi.advanceTimersByTimeAsync(900);
      expect(save).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  },
);

it("Task6 核实普通保存未写入不产生第二次保存", async () => {
  vi.useFakeTimers();
  try {
    const base = record(1, "base"),
      local = record(1, "local").content;
    let readable = false;
    const save = vi.fn(async () => {
      throw Error("lost");
    });
    const s = new DraftSession(
      base,
      {
        save,
        read: async () => {
          if (!readable) throw Error("offline");
          return base;
        },
      },
      () => {},
    );
    s.edit(local);
    await expect(s.flush()).rejects.toThrow();
    readable = true;
    // 接口尚未接入时使用现有页面核实按钮所调用的 flush，证明可加载行为缺口。
    await s.verifySaveResult();
    expect(save).toHaveBeenCalledTimes(1);
    expect(s.content).toEqual(local);
    s.schedule();
    await vi.advanceTimersByTimeAsync(450);
    expect(save).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});

it("Task6 采用准备失败后观察变化不能在重试时共同提交旧采用", () => {
  const base = record(1, "base"),
    remote = record(2, "remote");
  const s = new DraftSession(
    base,
    {
      save: async () => {
        throw Error("unexpected PUT");
      },
      read: async () => remote,
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observe(remote);
  s.bindWholeView(() => {
    throw Error("view failed");
  });
  expect(() => s.adoptSavedVersion(s.prepareConflictSelection())).toThrow();
  s.observe(record(3, "new remote"));
  let commits = 0;
  s.bindWholeView(() => () => {
    commits++;
  });
  expect(() => s.retryWholeAcceptance(s.wholeAcceptanceSnapshot)).toThrow();
  expect(commits).toBe(0);
  expect(s.content).toEqual(record(1, "local").content);
  expect(s.lastReliableDraft).toEqual(record(3, "new remote"));
});

function rejectedPreviewRecords(intent: "enabled" | "disabled" = "disabled") {
  const capture = {
    name: "A",
    type: "camera_record",
    device_id: "cam",
    scheduled_at: "2026-10-11 10:00:00",
    params: { type: "photo" },
  };
  const automatic = {
    name: "A预览",
    type: "obtain_action_outputs",
    scheduled_at: capture.scheduled_at,
    params: {
      source: { action_name: "A" },
      filter: "preview",
      purpose: "auto_preview",
    },
  };
  const base = record(1, "base");
  base.content = initializePreviewMetadata(
    { text: JSON.stringify({ name: "base", actions: [capture, automatic] }) },
    intent,
    "n",
  );
  const local = {
    ...base.content,
    text: JSON.stringify({ name: "local", actions: [capture, automatic] }),
  };
  // 派生后的完整记录与本页输入独立列出，不从协调函数计算预期。
  const derived: Draft = {
    ...base,
    revision: 2,
    content: {
      text: JSON.stringify({ name: "base", actions: [capture] }, null, 2),
      automaticPreviews: {
        intent,
        namespace: "n",
        next: 2,
        actions: [{ id: "n:0" }],
      },
    },
  };
  const coordinated: DraftContent = {
    ...derived.content,
    text: JSON.stringify({ name: "local", actions: [capture] }, null, 2),
  };
  const capabilities = {
    active: { devices: [] },
    error: null,
    generation: 2,
    version: "v2",
  };
  return {
    base,
    local,
    derived,
    coordinated,
    capabilities,
    capture,
    automatic,
  };
}
function previewCapabilities(
  supported: boolean,
  generation: number,
): CapabilityState {
  return {
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
                  preview_supported: supported,
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
    generation,
    version: `v${generation}`,
  };
}
it("普通拒绝合法派生后重复当前B及纯版本推进不误报冲突", async () => {
  vi.useFakeTimers();
  try {
    const { base, local, derived, coordinated, capabilities } =
      rejectedPreviewRecords();
    const save = vi.fn(async () => {
      throw new HttpError("invalid_content", "rejected", [], 400);
    });
    const s = new DraftSession(
      base,
      {
        save,
        read: async () => derived,
        readObservation: async () => ({
          kind: "available",
          draft: derived,
          capabilities,
        }),
      },
      () => {},
    );
    s.edit(local);
    await expect(s.flush()).rejects.toThrow();
    expect(s.confirmedDraft).toEqual(derived);
    expect(s.content).toEqual(coordinated);
    for (const revision of [2, 3]) {
      s.observeTarget({
        kind: "available",
        draft: { ...derived, revision },
        capabilities,
      });
      expect(s.canSave).toBe(true);
      expect(s.confirmedDraft.revision).toBe(revision);
      expect(s.content).toEqual(coordinated);
      s.schedule();
      await vi.advanceTimersByTimeAsync(1350);
      expect(save).toHaveBeenCalledTimes(1);
    }
  } finally {
    vi.useRealTimers();
  }
});
it("普通拒绝派生后真实用户写回固定旧B仍为冲突，flush不能覆盖", async () => {
  const { base, local, derived, coordinated, capabilities } =
    rejectedPreviewRecords();
  const save = vi.fn(async () => {
    throw new HttpError("invalid_content", "rejected", [], 400);
  });
  const s = new DraftSession(
    base,
    {
      save,
      read: async () => derived,
      readObservation: async () => ({
        kind: "available",
        draft: derived,
        capabilities,
      }),
    },
    () => {},
  );
  s.edit(local);
  await expect(s.flush()).rejects.toThrow();
  const actual = {
    ...base,
    revision: 3,
    lastWrite: { revision: 3, input: base.content, content: base.content },
  };
  s.observeTarget({ kind: "available", draft: actual, capabilities });
  expect(s.canSave).toBe(false);
  expect(s.confirmedDraft).toEqual(derived);
  expect(s.content).toEqual(coordinated);
  expect(s.conflict).toEqual(actual);
  await expect(s.flush()).rejects.toThrow();
  expect(save).toHaveBeenCalledTimes(1);
});
it("普通拒绝首次不可用后轮询派生及重复观察使用当前B并保留显示", async () => {
  vi.useFakeTimers();
  try {
    const { base, local, derived, coordinated, capabilities } =
      rejectedPreviewRecords();
    const save = vi.fn(async () => {
      throw new HttpError("invalid_content", "rejected", [], 400);
    });
    const s = new DraftSession(
      base,
      {
        save,
        read: async () => {
          throw Error("offline");
        },
      },
      () => {},
    );
    s.edit(local);
    const identity = new ActionUiIdentity(s.content);
    s.bindWholeView((input) => identity.prepareWhole(input));
    await expect(s.flush()).rejects.toThrow();
    expect(s.saveStatus).toBe("unavailable");
    expect(s.confirmedDraft).toEqual(base);
    s.observeTarget({ kind: "available", draft: derived, capabilities });
    const keys = [...identity.keys];
    for (const revision of [2, 3, 3]) {
      s.observeTarget({
        kind: "available",
        draft: { ...derived, revision },
        capabilities,
      });
      expect(s.canSave).toBe(true);
      expect(s.content).toEqual(coordinated);
      expect(s.confirmedDraft.revision).toBe(revision);
      expect(identity.keys).toEqual(keys);
      expect(s.saveDiagnostic).toContain("未写入");
      s.schedule();
      await vi.advanceTimersByTimeAsync(900);
    }
    expect(save).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});
it("普通失败后的下一次能力派生沿当前B接纳且不授权旧输入重试", async () => {
  vi.useFakeTimers();
  try {
    const { base, local, derived, capture, automatic } =
      rejectedPreviewRecords("enabled");
    const noPreview = previewCapabilities(false, 2),
      preview = previewCapabilities(true, 3);
    const save = vi.fn(async () => {
      throw new HttpError("invalid_content", "rejected", [], 400);
    });
    const s = new DraftSession(
      base,
      {
        save,
        read: async () => derived,
        readObservation: async () => ({
          kind: "available",
          draft: derived,
          capabilities: noPreview,
        }),
      },
      () => {},
    );
    s.edit(local);
    await expect(s.flush()).rejects.toThrow();
    expect(s.confirmedDraft).toEqual(derived);
    const next: Draft = {
      ...derived,
      revision: 3,
      content: {
        text: JSON.stringify(
          { name: "base", actions: [capture, automatic] },
          null,
          2,
        ),
        automaticPreviews: {
          intent: "enabled",
          namespace: "n",
          next: 2,
          actions: [{ id: "n:0" }, { id: "n:0:preview", sourceId: "n:0" }],
        },
      },
    };
    const expected = {
      ...next.content,
      text: JSON.stringify(
        { name: "local", actions: [capture, automatic] },
        null,
        2,
      ),
    };
    s.observeTarget({ kind: "available", draft: next, capabilities: preview });
    s.observeTarget({ kind: "available", draft: next, capabilities: preview });
    expect(s.confirmedDraft).toEqual(next);
    expect(s.content).toEqual(expected);
    expect(s.canSave).toBe(true);
    expect(s.saveDiagnostic).toContain("未写入");
    s.schedule();
    await vi.advanceTimersByTimeAsync(1800);
    expect(save).toHaveBeenCalledTimes(1);
    expect(save.mock.calls[0]).toEqual(["d1", 1, local]);
  } finally {
    vi.useRealTimers();
  }
});
it("普通失败派生后仅私有资料不同的可靠R仍须完整选择", async () => {
  const { base, local, derived, coordinated, capabilities } =
    rejectedPreviewRecords();
  const save = vi.fn(async () => {
    throw new HttpError("invalid_content", "rejected", [], 400);
  });
  const s = new DraftSession(
    base,
    {
      save,
      read: async () => derived,
      readObservation: async () => ({
        kind: "available",
        draft: derived,
        capabilities,
      }),
    },
    () => {},
  );
  s.edit(local);
  await expect(s.flush()).rejects.toThrow();
  const content = {
    ...derived.content,
    pending: { "/name": { kind: "json" as const, text: "{" } },
  };
  const actual = {
    ...derived,
    revision: 3,
    content,
    lastWrite: { revision: 3, input: content, content },
  };
  s.observeTarget({ kind: "available", draft: actual, capabilities });
  expect(s.confirmedDraft).toEqual(derived);
  expect(s.content).toEqual(coordinated);
  expect(s.conflict).toEqual(actual);
  expect(s.canSave).toBe(false);
  await expect(s.flush()).rejects.toThrow();
  expect(save).toHaveBeenCalledTimes(1);
});
it("普通失败派生显示准备拒绝后重试接纳，重复R不制造冲突或PUT", async () => {
  vi.useFakeTimers();
  try {
    const { base, local, derived, coordinated, capabilities } =
      rejectedPreviewRecords();
    const save = vi.fn(async () => {
      throw new HttpError("invalid_content", "rejected", [], 400);
    });
    const s = new DraftSession(
      base,
      {
        save,
        read: async () => derived,
        readObservation: async () => ({
          kind: "available",
          draft: derived,
          capabilities,
        }),
      },
      () => {},
    );
    s.edit(local);
    const identity = new ActionUiIdentity(s.content),
      oldKeys = [...identity.keys];
    const release = s.bindWholeView(() => {
      throw Error("view");
    });
    await expect(s.flush()).rejects.toThrow();
    expect(s.saveStatus).toBe("accepting");
    expect(s.content).toEqual(local);
    expect(s.confirmedDraft).toEqual(base);
    expect(identity.keys).toEqual(oldKeys);
    await expect(s.flush()).rejects.toThrow();
    release();
    s.bindWholeView((input) => identity.prepareWhole(input));
    s.retryWholeAcceptance(s.wholeAcceptanceSnapshot);
    const keys = [...identity.keys];
    expect(s.confirmedDraft).toEqual(derived);
    expect(s.content).toEqual(coordinated);
    s.observeTarget({ kind: "available", draft: derived, capabilities });
    expect(s.canSave).toBe(true);
    expect(identity.keys).toEqual(keys);
    expect(s.saveDiagnostic).toContain("未写入");
    s.schedule();
    await vi.advanceTimersByTimeAsync(1800);
    expect(save).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});
it("普通失败派生后显式重试使用当前revision，再次拒绝不自动连发", async () => {
  vi.useFakeTimers();
  try {
    const { base, local, derived, coordinated, capabilities } =
      rejectedPreviewRecords();
    const save = vi.fn(async () => {
      throw new HttpError("invalid_content", "rejected", [], 400);
    });
    const s = new DraftSession(
      base,
      {
        save,
        read: async () => derived,
        readObservation: async () => ({
          kind: "available",
          draft: derived,
          capabilities,
        }),
      },
      () => {},
    );
    s.edit(local);
    await expect(s.flush()).rejects.toThrow();
    s.observeTarget({ kind: "available", draft: derived, capabilities });
    await expect(s.flush()).rejects.toThrow();
    expect(save).toHaveBeenNthCalledWith(2, "d1", 2, coordinated);
    s.schedule();
    await vi.advanceTimersByTimeAsync(1800);
    expect(save).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});
it("普通失败派生后新编辑449/450毫秒保存，重复R及schedule不重计时", async () => {
  vi.useFakeTimers();
  try {
    const { base, local, derived, coordinated, capabilities } =
      rejectedPreviewRecords();
    const save = vi.fn(
      async (_id: string, revision: number, content: DraftContent) => {
        if (save.mock.calls.length === 1)
          throw new HttpError("invalid_content", "rejected", [], 400);
        return { ...derived, revision: revision + 1, content };
      },
    );
    const s = new DraftSession(
      base,
      {
        save,
        read: async () => derived,
        readObservation: async () => ({
          kind: "available",
          draft: derived,
          capabilities,
        }),
      },
      () => {},
    );
    s.edit(local);
    await expect(s.flush()).rejects.toThrow();
    const edited = {
      ...coordinated,
      text: coordinated.text.replace('"local"', '"new"'),
    };
    s.edit(edited);
    s.schedule();
    await vi.advanceTimersByTimeAsync(200);
    s.observeTarget({ kind: "available", draft: derived, capabilities });
    s.schedule();
    await vi.advanceTimersByTimeAsync(249);
    expect(save).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(save).toHaveBeenNthCalledWith(2, "d1", 2, edited);
    expect(s.saved).toBe(true);
  } finally {
    vi.useRealTimers();
  }
});

it("替换409后R回到旧B也不未经确认发第二次PUT", async () => {
  vi.useFakeTimers();
  try {
    const save = vi.fn(
      async (_id: string, revision: number, content: DraftContent) => {
        if (revision === 2)
          throw new HttpError("revision_conflict", "rejected", [], 409);
        return { ...record(revision + 1, ""), content };
      },
    );
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => record(3, "base") },
      () => {},
    );
    s.edit(record(1, "local").content);
    s.observeTarget({ kind: "available", draft: record(2, "remote") });
    await expect(
      s.replaceSavedVersion(s.prepareConflictSelection()),
    ).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(1800);
    expect(save).toHaveBeenCalledTimes(1);
    expect(s.content).toEqual(record(1, "local").content);
    expect(s.canSave).toBe(false);
    expect(s.conflict?.revision).toBe(3);
  } finally {
    vi.useRealTimers();
  }
});
it("替换失败时L恰回旧B仍保留所选完整输入和显示键", async () => {
  const base = record(1, "base"),
    remote = record(2, "remote");
  base.content.text = '{"name":"base","actions":[{"name":"A"}]}';
  const s = new DraftSession(
    base,
    {
      save: async () => {
        throw new HttpError("revision_conflict", "rejected", [], 409);
      },
      read: async () => record(3, "other"),
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observeTarget({ kind: "available", draft: remote });
  s.edit(base.content);
  const identity = new ActionUiIdentity(s.content),
    keys = [...identity.keys];
  s.bindWholeView((input) => identity.prepareWhole(input));
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(s.content).toEqual(base.content);
  expect(s.confirmedDraft).toEqual(base);
  expect(identity.keys).toEqual(keys);
  expect(s.canSave).toBe(false);
});
it.each([
  ["rejected", false, "local", "base"],
  ["rejected", true, "local", "base"],
  ["rejected", false, "base", "other"],
  ["rejected", true, "base", "other"],
  ["not_written", false, "base", "other"],
  ["not_written", true, "base", "other"],
] as const)(
  "替换%s首次不可用%s的L=%s后续R=%s仍须新选择",
  async (outcome, unavailable, localName, actualName) => {
    vi.useFakeTimers();
    try {
      let readable = !unavailable;
      const selected = record(2, "remote");
      const save = vi.fn(async () => {
        if (outcome === "rejected")
          throw new HttpError("revision_conflict", "rejected", [], 409);
        throw Error("lost");
      });
      const s = new DraftSession(
        record(1, "base"),
        {
          save,
          read: async () => {
            if (!readable) throw Error("offline");
            return selected;
          },
        },
        () => {},
      );
      s.edit(record(1, "local").content);
      s.observeTarget({ kind: "available", draft: selected });
      if (localName === "base") s.edit(record(1, "base").content);
      const fixed = s.prepareConflictSelection();
      await expect(s.replaceSavedVersion(fixed)).rejects.toThrow();
      expect(s.content).toEqual(record(1, localName).content);
      if (unavailable) {
        expect(() => s.prepareConflictSelection()).toThrow();
        readable = true;
        if (outcome === "not_written")
          await expect(s.flush()).rejects.toThrow();
      }
      s.observeTarget({ kind: "available", draft: record(3, actualName) });
      expect(s.canEdit).toBe(false);
      expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
      await vi.advanceTimersByTimeAsync(1800);
      expect(s.content).toEqual(record(1, localName).content);
      expect(s.confirmedDraft).toEqual(record(1, "base"));
      expect(s.conflict).toEqual(record(3, actualName));
      expect(s.canSave).toBe(false);
      expect(s.canEdit).toBe(true);
      expect(save).toHaveBeenCalledTimes(1);
      expect(() => s.adoptSavedVersion(fixed)).toThrow();
      const updated = s.prepareConflictSelection();
      s.adoptSavedVersion(updated);
      expect(s.content).toEqual(record(3, actualName).content);
    } finally {
      vi.useRealTimers();
    }
  },
);
it.each(["rejected", "not_written"] as const)(
  "替换%s且仍为所选R时保留L并可重新明确替换",
  async (outcome) => {
    let calls = 0;
    const selected = record(2, "remote");
    const save = vi.fn(
      async (_id: string, revision: number, content: DraftContent) => {
        if (++calls === 1) {
          if (outcome === "rejected")
            throw new HttpError("invalid_content", "rejected", [], 400);
          throw Error("lost");
        }
        return { ...record(revision + 1, ""), content };
      },
    );
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => selected },
      () => {},
    );
    s.edit(record(1, "local").content);
    s.observeTarget({ kind: "available", draft: selected });
    await expect(
      s.replaceSavedVersion(s.prepareConflictSelection()),
    ).rejects.toThrow();
    expect(s.content).toEqual(record(1, "local").content);
    expect(s.canSave).toBe(false);
    expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
    await s.replaceSavedVersion(s.prepareConflictSelection());
    expect(save).toHaveBeenCalledTimes(2);
    expect(s.saved).toBe(true);
  },
);
it("被拒替换后的可靠R=L只确认当前已存在，不伪造替换成功", async () => {
  const save = vi.fn(async () => {
    throw new HttpError("revision_conflict", "rejected", [], 409);
  });
  const s = new DraftSession(
    record(1, "base"),
    { save, read: async () => record(3, "local") },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observeTarget({ kind: "available", draft: record(2, "remote") });
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(s.content).toEqual(record(3, "local").content);
  expect(s.canEdit).toBe(false);
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  expect(s.confirmedDraft).toEqual(record(1, "base"));
  expect(s.saveDiagnostic).toContain("未写入");
  s.adoptSavedVersion(s.prepareConflictSelection());
  expect(s.confirmedDraft).toEqual(record(3, "local"));
  expect(s.saved).toBe(true);
  expect(s.saveDiagnostic).toContain("未写入");
  expect(save).toHaveBeenCalledTimes(1);
});
it("普通可靠拒绝和重复B轮询不周期重发同一输入", async () => {
  vi.useFakeTimers();
  try {
    const save = vi.fn(async () => {
      throw new HttpError("invalid_content", "rejected", [], 400);
    });
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => record(1, "base") },
      () => {},
    );
    s.edit(record(1, "local").content);
    await expect(s.flush()).rejects.toThrow();
    for (let index = 0; index < 3; index++) {
      s.observeTarget({ kind: "available", draft: record(1, "base") });
      s.schedule();
      await vi.advanceTimersByTimeAsync(900);
    }
    expect(save).toHaveBeenCalledTimes(1);
    expect(s.saved).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});
it("普通失败显式重试只增加一次，失败后新编辑按449/450毫秒保存", async () => {
  vi.useFakeTimers();
  try {
    const save = vi.fn(async () => {
      throw new HttpError("invalid_content", "rejected", [], 400);
    });
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => record(1, "base") },
      () => {},
    );
    s.edit(record(1, "local").content);
    await expect(s.flush()).rejects.toThrow();
    await expect(s.flush()).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(900);
    expect(save).toHaveBeenCalledTimes(2);
    s.edit(record(1, "new").content);
    s.schedule();
    await vi.advanceTimersByTimeAsync(449);
    expect(save).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(save).toHaveBeenNthCalledWith(3, "d1", 1, record(1, "new").content);
    await vi.advanceTimersByTimeAsync(900);
    expect(save).toHaveBeenCalledTimes(3);
  } finally {
    vi.useRealTimers();
  }
});
it("普通在途保存失败后，已发生的新编辑仍取得自己的450毫秒资格", async () => {
  vi.useFakeTimers();
  try {
    const first = deferred<Draft>();
    const save = vi.fn(
      async (_id: string, revision: number, content: DraftContent) => {
        if (save.mock.calls.length === 1) return first.promise;
        return { ...record(revision + 1, ""), content };
      },
    );
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => record(1, "base") },
      () => {},
    );
    s.edit(record(1, "local").content);
    const flushing = s.flush();
    s.edit(record(1, "new").content);
    s.schedule();
    first.reject(new HttpError("invalid_content", "rejected", [], 400));
    await expect(flushing).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(450);
    expect(save).toHaveBeenNthCalledWith(2, "d1", 1, record(1, "new").content);
    expect(s.saved).toBe(true);
  } finally {
    vi.useRealTimers();
  }
});
it("迟到普通拒绝不从读回时刻重新计算期间新编辑的450毫秒", async () => {
  vi.useFakeTimers();
  try {
    const first = deferred<Draft>();
    const save = vi.fn(
      async (_id: string, revision: number, content: DraftContent) => {
        if (save.mock.calls.length === 1) return first.promise;
        return { ...record(revision + 1, ""), content };
      },
    );
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => record(1, "base") },
      () => {},
    );
    s.edit(record(1, "local").content);
    const flushing = s.flush();
    s.edit(record(1, "new").content);
    s.schedule();
    await vi.advanceTimersByTimeAsync(200);
    first.reject(new HttpError("invalid_content", "rejected", [], 400));
    await expect(flushing).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(249);
    expect(save).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(save).toHaveBeenNthCalledWith(2, "d1", 1, record(1, "new").content);
  } finally {
    vi.useRealTimers();
  }
});
it("未知保存的旧B读回不能证明未写入或自动结束旧责任", async () => {
  let actual = record(4, "remote");
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw Error("lost");
      },
      read: async () => actual,
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  await expect(s.flush()).rejects.toThrow();
  actual = record(1, "base");
  await expect(s.flush()).rejects.toThrow();
  s.observeTarget({ kind: "available", draft: record(5, "local") });
  expect(s.decision.kind).toBe("superseded_write");
  expect(s.saved).toBe(false);
  expect(s.saveDiagnostic).not.toContain("可靠确认未写入");
  s.adoptSavedVersion(s.prepareConflictSelection());
  expect(s.saved).toBe(true);
});
it("旧未知并存时被拒替换的R=L仍须明确结束旧责任", async () => {
  let calls = 0,
    actual = record(4, "remote");
  const save = vi.fn(async () => {
    if (++calls === 1) throw Error("lost");
    throw new HttpError("revision_conflict", "rejected", [], 409);
  });
  const s = new DraftSession(
    record(1, "base"),
    { save, read: async () => actual },
    () => {},
  );
  s.edit(record(1, "local").content);
  await expect(s.flush()).rejects.toThrow();
  actual = record(5, "local");
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(s.decision.kind).toBe("superseded_write");
  expect(s.saved).toBe(false);
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  s.adoptSavedVersion(s.prepareConflictSelection());
  expect(s.saved).toBe(true);
  expect(s.saveDiagnostic).toContain("旧保存结果不可确认");
  expect(s.saveDiagnostic).toContain("未写入");
  expect(save).toHaveBeenCalledTimes(2);
});
it("未知替换后较新可靠观察隔离请求时开放新选择但不消除责任", async () => {
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw Error("lost");
      },
      read: async () => {
        throw Error("offline");
      },
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observeTarget({ kind: "available", draft: record(2, "remote") });
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(s.canEdit).toBe(false);
  s.observeTarget({ kind: "available", draft: record(3, "other") });
  expect(s.canEdit).toBe(false);
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  expect(s.canEdit).toBe(true);
  expect(s.saved).toBe(false);
  s.adoptSavedVersion(s.prepareConflictSelection());
  expect(s.saved).toBe(true);
});

it("替换可靠失败即消费旧确认，即使后续R完全不变也须重新确认", async () => {
  const selected = record(2, "remote");
  const save = vi.fn(async () => {
    throw new HttpError("invalid_content", "rejected", [], 400);
  });
  const s = new DraftSession(
    record(1, "base"),
    { save, read: async () => selected },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observeTarget({ kind: "available", draft: selected });
  const fixed = s.prepareConflictSelection();
  await expect(s.replaceSavedVersion(fixed)).rejects.toThrow();
  await expect(s.replaceSavedVersion(fixed)).rejects.toThrow();
  expect(save).toHaveBeenCalledTimes(1);
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  expect(() => s.prepareConflictSelection()).not.toThrow();
});
it.each(["rejected", "not_written"] as const)(
  "普通%s首次读回不可用后恢复B不恢复自动重试",
  async (outcome) => {
    vi.useFakeTimers();
    try {
      const save = vi.fn(async () => {
        if (outcome === "rejected")
          throw new HttpError("invalid_content", "rejected", [], 400);
        throw Error("lost");
      });
      const s = new DraftSession(
        record(1, "base"),
        {
          save,
          read: async () => {
            throw Error("offline");
          },
          readObservation: async () => ({
            kind: "unavailable",
            message: "offline",
          }),
        },
        () => {},
      );
      s.edit(record(1, "local").content);
      await expect(s.flush()).rejects.toThrow();
      // 普通观察不能证明未知请求未写入，只有本操作核实可以完成该责任。
      s.observeTarget({ kind: "available", draft: record(1, "base") });
      s.schedule();
      await vi.advanceTimersByTimeAsync(1800);
      expect(save).toHaveBeenCalledTimes(1);
      expect(s.saved).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  },
);
it("仅私有资料不同的普通拒绝后R仍为真正冲突", async () => {
  const base = record(1, "same"),
    local = record(1, "same"),
    actual = record(2, "same");
  local.content.pending = { "/name": { kind: "json", text: "{" } };
  actual.content.pending = { "/name": { kind: "json", text: "[" } };
  const save = vi.fn(async () => {
    throw new HttpError("revision_conflict", "rejected", [], 409);
  });
  const s = new DraftSession(
    base,
    { save, read: async () => actual },
    () => {},
  );
  s.edit(local.content);
  await expect(s.flush()).rejects.toThrow();
  expect(s.content).toEqual(local.content);
  expect(s.confirmedDraft).toEqual(base);
  expect(s.conflict).toEqual(actual);
  expect(s.canSave).toBe(false);
});
it.each(["exported", "deleted"] as const)(
  "替换被拒后%s终态保留选定完整L且不安排写回",
  async (terminal) => {
    vi.useFakeTimers();
    try {
      const save = vi.fn(async () => {
        throw new HttpError("revision_conflict", "rejected", [], 409);
      });
      const s = new DraftSession(
        record(1, "base"),
        {
          save,
          read: async () => record(2, "remote"),
          readObservation: async () =>
            terminal === "deleted"
              ? { kind: "deleted" }
              : {
                  kind: "available",
                  draft: { ...record(3, "remote"), exportedRequestId: "1" },
                },
        },
        () => {},
      );
      s.edit(record(1, "local").content);
      s.observeTarget({ kind: "available", draft: record(2, "remote") });
      await expect(
        s.replaceSavedVersion(s.prepareConflictSelection()),
      ).rejects.toThrow();
      await vi.advanceTimersByTimeAsync(1800);
      expect(s.content).toEqual(record(1, "local").content);
      expect(s.canEdit).toBe(false);
      expect(s.saveStatus).toBe(terminal);
      expect(save).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  },
);
it("替换拒绝后的旧观察与同版本矛盾不复活确认或改变L", async () => {
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw new HttpError("revision_conflict", "rejected", [], 409);
      },
      read: async () => record(3, "other"),
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observeTarget({ kind: "available", draft: record(2, "remote") });
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  const fixed = s.prepareConflictSelection();
  s.observeTarget({ kind: "available", draft: record(2, "remote") });
  expect(s.lastReliableDraft).toEqual(record(3, "other"));
  s.observeTarget({ kind: "available", draft: record(3, "base") });
  expect(s.saveStatus).toBe("invalid");
  expect(s.content).toEqual(record(1, "local").content);
  expect(() => s.adoptSavedVersion(fixed)).toThrow();
});
it("被拒替换的可靠R=L显示准备失败只等待本地接纳", async () => {
  const local = record(1, "local");
  local.content.text = '{"name":"local","actions":[{"name":"A"}]}';
  const actual = { ...local, revision: 3 };
  const save = vi.fn(async () => {
    throw new HttpError("revision_conflict", "rejected", [], 409);
  });
  const s = new DraftSession(
    record(1, "base"),
    { save, read: async () => actual },
    () => {},
  );
  s.edit(local.content);
  const identity = new ActionUiIdentity(s.content),
    keys = [...identity.keys];
  s.observeTarget({ kind: "available", draft: record(2, "remote") });
  const release = s.bindWholeView(() => {
    throw Error("view");
  });
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(s.selectionPendingDecision).toBe(true);
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  expect(() => s.adoptSavedVersion(s.prepareConflictSelection())).toThrow();
  expect(s.saveStatus).toBe("accepting");
  expect(s.content).toEqual(local.content);
  expect(s.confirmedDraft).toEqual(record(1, "base"));
  expect(identity.keys).toEqual(keys);
  expect(s.saved).toBe(false);
  await expect(s.flush()).rejects.toThrow();
  release();
  s.bindWholeView((input) => identity.prepareWhole(input));
  expect(await s.retrySelection(s.selectionDecision)).toBe("completed");
  expect(s.confirmedDraft).toEqual(actual);
  expect(s.saved).toBe(true);
  expect(s.saveDiagnostic).toContain("未写入");
  expect(save).toHaveBeenCalledTimes(1);
});

it("普通显式核实后新保存再次被拒绝也不取得周期重试资格", async () => {
  vi.useFakeTimers();
  try {
    let readable = false;
    const save = vi.fn(async () => {
      if (save.mock.calls.length === 1) throw Error("lost");
      throw new HttpError("invalid_content", "rejected", [], 400);
    });
    const s = new DraftSession(
      record(1, "base"),
      {
        save,
        read: async () => {
          if (!readable) throw Error("offline");
          return record(1, "base");
        },
      },
      () => {},
    );
    s.edit(record(1, "local").content);
    await expect(s.flush()).rejects.toThrow();
    readable = true;
    await expect(s.flush()).rejects.toThrow();
    s.observeTarget({ kind: "available", draft: record(1, "base") });
    s.schedule();
    await vi.advanceTimersByTimeAsync(1800);
    expect(save).toHaveBeenCalledTimes(2);
    expect(s.content).toEqual(record(1, "local").content);
    expect(s.saveStatus).toBe("dirty");
  } finally {
    vi.useRealTimers();
  }
});
it("旧未知和新未知替换分别保留责任直到可靠隔离后明确采用", async () => {
  let actual: Draft | undefined = record(4, "remote");
  const save = vi.fn(async () => {
    throw Error("lost");
  });
  const s = new DraftSession(
    record(1, "base"),
    {
      save,
      read: async () => {
        if (!actual) throw Error("offline");
        return actual;
      },
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  await expect(s.flush()).rejects.toThrow();
  const choice = s.prepareConflictSelection();
  actual = undefined;
  await expect(s.replaceSavedVersion(choice)).rejects.toThrow();
  expect(s.saveStatus).toBe("unknown");
  expect(s.canEdit).toBe(false);
  expect(s.saveDiagnostic).toContain("旧保存结果不可确认");
  s.observeTarget({ kind: "available", draft: record(6, "local") });
  expect(s.saveStatus).toBe("superseded_write");
  expect(s.saved).toBe(false);
  expect(s.canSave).toBe(false);
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  s.adoptSavedVersion(s.prepareConflictSelection());
  expect(s.saved).toBe(true);
  expect(save).toHaveBeenCalledTimes(2);
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function setup(remote = record(2, "remote")) {
  const save = vi.fn(
    async (_id: string, revision: number, content: DraftContent) => ({
      ...record(revision + 1, ""),
      content,
    }),
  );
  const session = new DraftSession(
    record(1, "base"),
    { save, read: async () => remote },
    () => {},
  );
  session.edit(record(1, "local").content);
  session.observeTarget({ kind: "available", draft: remote });
  return { session, save };
}
it("完整采用终止旧450毫秒计时器且保留远端全部资料", async () => {
  vi.useFakeTimers();
  try {
    const remote = record(2, "remote");
    remote.content.pending = { "/name": { kind: "json", text: "{" } };
    const { session, save } = setup(remote);
    session.schedule();
    expect(session.canEdit).toBe(true);
    expect(session.canSave).toBe(false);
    session.adoptSavedVersion(session.prepareConflictSelection());
    await vi.advanceTimersByTimeAsync(1000);
    expect(session.content).toEqual(remote.content);
    expect(session.confirmedDraft).toEqual(remote);
    expect(session.saved).toBe(true);
    expect(session.content.automaticPreviews).toBeUndefined();
    expect(save).not.toHaveBeenCalled();
  } finally {
    vi.useRealTimers();
  }
});
it("纯版本推进保留本页修改并使用实际revision保存", async () => {
  const { session, save } = setup(record(5, "base"));
  expect(session.canSave).toBe(true);
  await session.flush();
  expect(save).toHaveBeenCalledWith("d1", 5, record(1, "local").content);
  expect(session.confirmedDraft.revision).toBe(6);
});
it.each(["edit", "remote", "unavailable", "exported", "deleted"] as const)(
  "%s使完整选择快照失效，旧确认不得执行",
  (change) => {
    const { session, save } = setup();
    const selection = session.prepareConflictSelection();
    if (change === "edit") session.edit(record(1, "new").content);
    if (change === "remote")
      session.observeTarget({ kind: "available", draft: record(3, "new") });
    if (change === "unavailable") {
      session.observeTarget({ kind: "unavailable", message: "offline" });
      session.observeTarget({ kind: "available", draft: record(2, "remote") });
    }
    if (change === "exported")
      session.observeTarget({
        kind: "available",
        draft: { ...record(3, "remote"), exportedRequestId: "1" },
      });
    if (change === "deleted") session.observeTarget({ kind: "deleted" });
    expect(() => session.adoptSavedVersion(selection)).toThrow();
    expect(save).not.toHaveBeenCalled();
    expect(session.content).toEqual(
      record(1, change === "edit" ? "new" : "local").content,
    );
  },
);
it("明确替换以所选R为条件基线，在途禁止编辑与再次选择", async () => {
  const { session } = setup(),
    pending = deferred<Draft>();
  const save = vi.fn(async () => pending.promise);
  const remote = record(2, "remote");
  const s = new DraftSession(
    record(1, "base"),
    { save, read: async () => remote },
    () => {},
  );
  s.edit(session.content);
  s.observeTarget({ kind: "available", draft: remote });
  const snapshot = s.prepareConflictSelection();
  const replacing = s.replaceSavedVersion(snapshot);
  expect(s.canEdit).toBe(false);
  expect(() => s.edit(record(1, "late").content)).toThrow();
  expect(() => s.adoptSavedVersion(snapshot)).toThrow();
  pending.resolve({
    ...record(3, ""),
    content: snapshot.local,
    updatedAt: "2026-10-11 10:01:00",
  });
  await replacing;
  expect(save).toHaveBeenCalledWith("d1", 2, snapshot.local);
  expect(s.saved).toBe(true);
  expect(s.confirmedDraft.updatedAt).toBe("2026-10-11 10:01:00");
});
it("替换409重展示最新版本，不抬高revision重发", async () => {
  const save = vi.fn(async () => {
    throw new HttpError("revision_conflict", "stale", [], 409);
  });
  const s = new DraftSession(
    record(1, "base"),
    { save, read: async () => record(3, "other") },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observeTarget({ kind: "available", draft: record(2, "remote") });
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(s.conflict?.revision).toBe(3);
  expect(s.canEdit).toBe(false);
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  expect(s.canEdit).toBe(true);
  expect(s.canSave).toBe(false);
  expect(save).toHaveBeenCalledTimes(1);
});
it.each(["local", "remote"])(
  "旧未知保存被较新%s隔离后仍须明确采用",
  async (name) => {
    const save = vi.fn(async () => {
      throw Error("lost");
    });
    const remote = record(4, name);
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => remote },
      () => {},
    );
    s.edit(record(1, "local").content);
    await expect(s.flush()).rejects.toThrow();
    expect(s.saved).toBe(false);
    expect(s.canSave).toBe(false);
    expect(s.decision.kind).toBe("superseded_write");
    s.adoptSavedVersion(s.prepareConflictSelection());
    await s.flush();
    expect(s.saved).toBe(true);
    expect(s.content).toEqual(remote.content);
    expect(s.saveDiagnostic).toContain("不可确认");
    expect(save).toHaveBeenCalledTimes(1);
  },
);
it("普通观察相等不能认领固定保存，具体核实才接纳", async () => {
  const pending = deferred<Draft>(),
    remote = record(2, "local");
  const s = new DraftSession(
    record(1, "base"),
    { save: async () => pending.promise, read: async () => remote },
    () => {},
  );
  s.edit(remote.content);
  const flushing = s.flush();
  s.observeTarget({ kind: "available", draft: remote });
  expect(s.revision).toBe(1);
  expect(s.saved).toBe(false);
  expect(() => s.prepareConflictSelection()).toThrow();
  pending.resolve(remote);
  await flushing;
  expect(s.saved).toBe(true);
});
it("后端已保存而显示准备失败时只重试共同接纳", async () => {
  const save = vi.fn(
    async (_id: string, revision: number, content: DraftContent) => ({
      ...record(revision + 1, ""),
      content,
    }),
  );
  const s = new DraftSession(
    record(1, "base"),
    {
      save,
      read: async () => {
        throw Error("must not read");
      },
    },
    () => {},
  );
  const old = s.content;
  s.edit(record(1, "local").content);
  const local = s.content;
  const release = s.bindWholeView(() => {
    throw Error("view");
  });
  await expect(s.flush()).rejects.toThrow();
  expect(s.content).toBe(local);
  expect(s.revision).toBe(1);
  expect(s.canEdit).toBe(false);
  release();
  s.retryWholeAcceptance(s.wholeAcceptanceSnapshot);
  expect(s.saved).toBe(true);
  expect(s.content).not.toEqual(old);
  expect(save).toHaveBeenCalledTimes(1);
});
it.each(["export", "delete", "append"])(
  "终态观察不解除%s独立核实责任",
  (operation) => {
    const s = new DraftSession(
      record(1, "base"),
      {
        save: async () => record(1, "base"),
        read: async () => record(1, "base"),
      },
      () => {},
    );
    if (operation === "export") {
      s.beginExport();
      s.exportUnknown();
    }
    if (operation === "delete") s.deletionState = "unknown";
    if (operation === "append") s.lockAppend();
    s.observeTarget({ kind: "deleted" });
    if (operation === "export") expect(s.exportState).toBe("unknown");
    if (operation === "delete") expect(s.deletionState).toBe("unknown");
    if (operation === "append") expect(s.appendLocked).toBe(true);
    expect(s.canEdit).toBe(false);
  },
);
it("450毫秒自动保存与连续修改重计时", async () => {
  vi.useFakeTimers();
  try {
    const save = vi.fn(
      async (_id: string, revision: number, content: DraftContent) => ({
        ...record(revision + 1, ""),
        content,
      }),
    );
    const s = new DraftSession(
      record(1, "base"),
      { save, read: async () => record(1, "base") },
      () => {},
    );
    s.edit(record(1, "one").content);
    s.schedule();
    await vi.advanceTimersByTimeAsync(449);
    expect(save).not.toHaveBeenCalled();
    s.edit(record(1, "two").content);
    s.schedule();
    await vi.advanceTimersByTimeAsync(449);
    expect(save).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(save).toHaveBeenCalledTimes(1);
    expect(s.content).toEqual(record(1, "two").content);
  } finally {
    vi.useRealTimers();
  }
});
it.each([true, false])(
  "整份远端接纳%s身份只保留基线可证明的动作",
  (metadata) => {
    const base = record(1, "base");
    base.content = { text: '{"actions":[{"name":"A"}]}' };
    if (metadata)
      base.content = initializePreviewMetadata(base.content, "unset", "n");
    const local: DraftContent = {
      ...base.content,
      text: '{"actions":[{"name":"A"},{"name":"local"}]}',
    };
    const remote: Draft = {
      ...base,
      revision: 2,
      content: {
        ...local,
        text: '{"actions":[{"name":"A"},{"name":"remote"}]}',
      },
    };
    if (metadata) {
      local.automaticPreviews = {
        ...base.content.automaticPreviews!,
        actions: [
          ...base.content.automaticPreviews!.actions,
          { id: "colliding" },
        ],
        next: 2,
      };
      remote.content.automaticPreviews = structuredClone(
        local.automaticPreviews,
      );
    }
    const s = new DraftSession(
      base,
      { save: async () => remote, read: async () => remote },
      () => {},
    );
    s.edit(local);
    const identity = new ActionUiIdentity(s.content),
      keys = [...identity.keys];
    s.bindWholeView((value) => identity.prepareWhole(value));
    s.observeTarget({ kind: "available", draft: remote });
    s.adoptSavedVersion(s.prepareConflictSelection());
    if (metadata) expect(identity.keys[0]).toBe(keys[0]);
    else expect(keys).not.toContain(identity.keys[0]);
    expect(keys).not.toContain(identity.keys[1]);
  },
);
it("重复可靠观察不重复重建无身份动作键", () => {
  const base = {
    ...record(1, ""),
    content: { text: '{"actions":[{"name":"A"}]}' },
  };
  const s = new DraftSession(
    base,
    { save: async () => base, read: async () => base },
    () => {},
  );
  const identity = new ActionUiIdentity(s.content),
    keys = [...identity.keys];
  s.bindWholeView((value) => identity.prepareWhole(value));
  s.observeTarget({ kind: "available", draft: base });
  s.observeTarget({ kind: "available", draft: base });
  expect(identity.keys).toEqual(keys);
  expect(s.version).toBe(0);
});
it("未知替换可以具体核实同次成功，不重新PUT", async () => {
  let readable = false;
  const actual = record(3, "local");
  const save = vi.fn(async () => {
    throw Error("lost");
  });
  const s = new DraftSession(
    record(1, "base"),
    {
      save,
      read: async () => {
        if (!readable) throw Error("offline");
        return actual;
      },
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observeTarget({ kind: "available", draft: record(2, "remote") });
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(s.canEdit).toBe(false);
  readable = true;
  expect(await s.retrySelection(s.selectionDecision)).toBe("completed");
  expect(s.canEdit).toBe(true);
  expect(s.saved).toBe(true);
  expect(save).toHaveBeenCalledTimes(1);
});
it("公开选择对象被篡改时私有快照拒绝接纳", () => {
  const { session } = setup();
  const fixed = session.prepareConflictSelection();
  fixed.remote.content.text = record(2, "injected").content.text;
  expect(() => session.adoptSavedVersion(fixed)).toThrow();
  expect(session.content).toEqual(record(1, "local").content);
});
it("较新普通观察先到时，旧保存成功回执不能回退当前事实", async () => {
  const pending = deferred<Draft>();
  const save = vi.fn(async () => pending.promise);
  const s = new DraftSession(
    record(1, "base"),
    { save, read: async () => record(4, "remote") },
    () => {},
  );
  s.edit(record(1, "local").content);
  const flushing = s.flush();
  s.observeTarget({ kind: "available", draft: record(4, "remote") });
  pending.resolve(record(2, "local"));
  await flushing;
  expect(s.confirmedDraft.revision).toBe(4);
  expect(s.content).toEqual(record(4, "remote").content);
  expect(save).toHaveBeenCalledTimes(1);
});
it("未知替换核实为未写入后不能绕过新选择自动重发", async () => {
  let available = false;
  const save = vi.fn(async () => {
    throw Error("lost");
  });
  const s = new DraftSession(
    record(1, "base"),
    {
      save,
      read: async () => {
        if (!available) throw Error("offline");
        return record(2, "remote");
      },
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  s.observeTarget({ kind: "available", draft: record(2, "remote") });
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  available = true;
  await expect(s.flush()).rejects.toThrow();
  expect(await s.retrySelection(s.selectionDecision)).toBe("confirmation");
  expect(s.canEdit).toBe(true);
  expect(s.canSave).toBe(false);
  expect(save).toHaveBeenCalledTimes(1);
});
it("后端成功但视图等待期间的新观察在重试后仍有效", async () => {
  const save = vi.fn(async () => record(2, "local"));
  const s = new DraftSession(
    record(1, "base"),
    { save, read: async () => record(4, "remote") },
    () => {},
  );
  s.edit(record(1, "local").content);
  const release = s.bindWholeView(() => {
    throw Error("view");
  });
  await expect(s.flush()).rejects.toThrow();
  s.observeTarget({ kind: "available", draft: record(4, "remote") });
  release();
  s.retryWholeAcceptance(s.wholeAcceptanceSnapshot);
  expect(s.confirmedDraft.revision).toBe(4);
  expect(s.content).toEqual(record(4, "remote").content);
  expect(save).toHaveBeenCalledTimes(1);
});
it("可靠拒绝后的读取故障同时保留拒绝事实和读取诊断", async () => {
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw new HttpError("invalid_content", "rejected", [], 400);
      },
      read: async () => {
        throw Error("offline");
      },
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  await expect(s.flush()).rejects.toThrow();
  expect(s.saveDiagnostic).toContain("未写入");
  expect(s.error).toContain("offline");
  expect(s.error).toContain("rejected");
  expect(s.decision.kind).toBe("unavailable");
  expect(s.canSave).toBe(false);
});
it("普通导出终态观察不能把本会话未知导出说成已完成保存", () => {
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => record(1, "base"),
      read: async () => record(1, "base"),
    },
    () => {},
  );
  s.beginExport();
  s.exportUnknown();
  s.observeTarget({
    kind: "available",
    draft: { ...record(2, "base"), exportedRequestId: "1" },
  });
  expect(s.exportState).toBe("unknown");
  expect(s.saved).toBe(false);
});
it("无身份异长远端集合完整采用后全部重建局部键", () => {
  const base = {
    ...record(1, ""),
    content: { text: '{"actions":[{"name":"A"},{"name":"B"}]}' },
  };
  const remote = {
    ...base,
    revision: 2,
    content: { text: '{"actions":[{"name":"X"}]}' },
  };
  const s = new DraftSession(
    base,
    { save: async () => remote, read: async () => remote },
    () => {},
  );
  s.edit({ text: '{"actions":[{"name":"local"},{"name":"B"}]}' });
  const identity = new ActionUiIdentity(s.content),
    keys = [...identity.keys];
  s.bindWholeView((transition) => identity.prepareWhole(transition));
  s.observeTarget({ kind: "available", draft: remote });
  s.adoptSavedVersion(s.prepareConflictSelection());
  expect(identity.keys).toHaveLength(1);
  expect(keys).not.toContain(identity.keys[0]);
  expect(s.saved).toBe(true);
});
it("冲突重新可靠回到完整基线时恢复450毫秒普通保存", async () => {
  vi.useFakeTimers();
  try {
    const { session, save } = setup();
    session.observeTarget({ kind: "available", draft: record(3, "base") });
    await vi.advanceTimersByTimeAsync(449);
    expect(save).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(save).toHaveBeenCalledWith("d1", 3, record(1, "local").content);
    expect(session.saved).toBe(true);
  } finally {
    vi.useRealTimers();
  }
});
it("读取失效后的本页修正不清除读取诊断", () => {
  const { session } = setup();
  session.observeTarget({ kind: "unavailable", message: "offline" });
  session.edit(record(1, "corrected").content);
  expect(session.error).toContain("offline");
  expect(session.canSave).toBe(false);
});
it("旧未知保存后的新替换被拒绝时仍保留旧结果不可确认事实", async () => {
  let attempts = 0;
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        if (++attempts === 1) throw Error("lost");
        throw new HttpError("revision_conflict", "rejected", [], 409);
      },
      read: async () => record(4, "remote"),
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  await expect(s.flush()).rejects.toThrow();
  await expect(
    s.replaceSavedVersion(s.prepareConflictSelection()),
  ).rejects.toThrow();
  expect(s.saveDiagnostic).toContain("旧保存结果不可确认");
  expect(s.saveDiagnostic).toContain("未写入");
});
it("具体核实证明成功时不留下已被取代且不可确认的旧保存诊断", async () => {
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw Error("lost");
      },
      read: async () => record(2, "local"),
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  await s.flush();
  expect(s.saved).toBe(true);
  expect(s.saveDiagnostic).toBe("");
});
it("保存阶段由固定责任表达，不依赖错误文案", async () => {
  const { session } = setup();
  expect(session.saveStatus).toBe("conflict");
  session.error = "translated";
  expect(session.saveStatus).toBe("conflict");
  session.adoptSavedVersion(session.prepareConflictSelection());
  expect(session.saveStatus).toBe("saved");
  const pending = deferred<Draft>();
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => pending.promise,
      read: async () => {
        throw Error("offline");
      },
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  const flushing = s.flush();
  expect(s.saveStatus).toBe("saving");
  pending.reject(Error("lost"));
  await expect(flushing).rejects.toThrow();
  expect(s.saveStatus).toBe("unknown");
  expect(s.targetObservation.kind).toBe("unavailable");
});
it("对外目标观察副本不能改变已确认的选择依据", () => {
  const { session } = setup();
  const observation = session.targetObservation;
  if (observation.kind === "available")
    observation.draft.content.text = "injected";
  const fixed = session.prepareConflictSelection();
  session.adoptSavedVersion(fixed);
  expect(session.content).toEqual(record(2, "remote").content);
});
it("本页能力协调也先准备完整动作显示，拒绝时不部分提交", () => {
  const base = record(1, "base");
  base.content = initializePreviewMetadata(
    {
      text: '{"actions":[{"name":"A","type":"camera_record"},{"name":"auto","type":"obtain_action_outputs","params":{"purpose":"auto_preview","source":{"action_name":"A"}}}]}',
    },
    "disabled",
    "n",
  );
  const s = new DraftSession(
    base,
    { save: async () => base, read: async () => base },
    () => {},
  );
  const old = s.content;
  const release = s.bindWholeView(() => {
    throw Error("view");
  });
  expect(() =>
    s.coordinate({
      active: { devices: [] },
      error: null,
      generation: 2,
      version: "v2",
    }),
  ).toThrow();
  expect(s.content).toBe(old);
  expect(s.revision).toBe(1);
  release();
  s.retryWholeAcceptance(s.wholeAcceptanceSnapshot);
  expect(
    JSON.parse(s.content.text).actions.map(
      (action: { name: string }) => action.name,
    ),
  ).toEqual(["A"]);
  expect(s.saved).toBe(false);
});
it("固定保存待定期间较旧R不能撤销最高可靠观察", async () => {
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw Error("lost");
      },
      read: async () => record(4, "remote"),
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  await expect(s.flush()).rejects.toThrow();
  const selection = s.prepareConflictSelection(),
    epoch = s.observationEpoch;
  s.observeTarget({ kind: "available", draft: record(3, "older") });
  expect(s.targetObservation).toEqual({
    kind: "available",
    draft: record(4, "remote"),
  });
  expect(s.observationEpoch).toBe(epoch);
  s.adoptSavedVersion(selection);
  expect(s.confirmedDraft.revision).toBe(4);
});
it("读取失效后较旧可靠记录不能恢复原选择资格", async () => {
  const s = new DraftSession(
    record(1, "base"),
    {
      save: async () => {
        throw Error("lost");
      },
      read: async () => record(4, "remote"),
    },
    () => {},
  );
  s.edit(record(1, "local").content);
  await expect(s.flush()).rejects.toThrow();
  const selection = s.prepareConflictSelection();
  s.observeTarget({ kind: "unavailable", message: "offline" });
  s.observeTarget({ kind: "available", draft: record(3, "older") });
  expect(s.targetObservation.kind).toBe("unavailable");
  expect(() => s.prepareConflictSelection()).toThrow();
  s.observeTarget({ kind: "available", draft: record(4, "remote") });
  expect(() => s.adoptSavedVersion(selection)).toThrow();
});
