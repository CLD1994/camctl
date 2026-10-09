import { expect, it, vi } from "vitest";
import {
  FollowOperation,
  classifyAppend,
  type FollowTransport,
} from "../../src/web/followup";
import type { Draft } from "../../src/server/models";
import { HttpError } from "../../src/web/api";
import {
  initializePreviewMetadata,
  appendContentAction,
  coordinatePreviews,
} from "../../src/shared/automatic-previews";
const action = {
  name: "同步",
  type: "report_status",
  params: { scope: "full" },
};
const baseline = (): Draft => ({
  id: "fixed",
  revision: 1,
  content: {
    text: JSON.stringify({ name: "原计划", actions: [] }),
    pending: { "/name": { kind: "json", text: "{" } },
  },
  createdAt: "t",
  updatedAt: "t",
});
const appended = (): Draft => ({
  ...baseline(),
  revision: 2,
  content: {
    ...baseline().content,
    text: JSON.stringify(
      {
        name: "原计划",
        actions: [action],
      },
      null,
      2,
    ),
  },
});
it.each(["appended", "baseline", "conflict"] as const)(
  "追加核实按完整记录分类：%s",
  (kind) => {
    const actual =
      kind === "appended"
        ? appended()
        : kind === "baseline"
          ? baseline()
          : { ...appended(), revision: 3 };
    expect(classifyAppend(baseline(), action, actual)).toBe(kind);
  },
);
it.each(["pending", "variants", "prefix", "action", "id", "exported"])(
  "追加结果不接受不同%s",
  (difference) => {
    const actual = appended();
    if (difference === "pending") actual.content.pending = {};
    if (difference === "variants")
      actual.content.actionVariants = {
        "0": [
          { type: "camera_record", fields: { device_id: "cam" }, pending: {} },
        ],
      };
    if (difference === "prefix")
      actual.content.text = JSON.stringify({
        name: "different",
        actions: [action],
      });
    if (difference === "action")
      actual.content.text = JSON.stringify({
        name: "原计划",
        actions: [
          { ...action, params: { scope: "since", after_report_id: 1 } },
        ],
      });
    if (difference === "id") actual.id = "other";
    if (difference === "exported") actual.exportedRequestId = "r";
    expect(classifyAppend(baseline(), action, actual)).toBe("conflict");
  },
);
function transport(overrides: Partial<FollowTransport> = {}): FollowTransport {
  return {
    create: vi.fn(async () => baseline()),
    prepare: vi.fn(async () => baseline()),
    append: vi.fn(async () => appended()),
    read: vi.fn(async () => appended()),
    ...overrides,
  };
}
it.each([false, true])(
  "固定追加attempt并只在可靠成功后发布转换：响应丢失%s",
  async (lost) => {
    const tx = transport({
      append: vi.fn(async () => {
        expect(op.attempt).toEqual({
          origin: baseline(),
          baselineChanges: [],
          baseline: baseline(),
          action,
          index: 0,
          appended: appended().content,
          expected: {
            content: appended().content,
            capabilityVersion: undefined,
          },
        });
        expect(op.transition).toBeUndefined();
        if (lost) throw Error("lost");
        return appended();
      }),
    });
    const op = new FollowOperation("fixed", action, tx);
    await op.advance();
    expect(op.transition).toEqual({
      ...op.attempt,
      actual: appended(),
      capabilities: undefined,
    });
    const copy = op.transition!;
    copy.actual.content.text = "mutated";
    expect(op.transition!.actual.content).toEqual(appended().content);
    await op.advance();
    expect(tx.append).toHaveBeenCalledTimes(1);
  },
);
it("结果已提交但接纳失败仅保留实际结果，不再追加", async () => {
  const tx = transport(),
    op = new FollowOperation("fixed", action, tx);
  await op.advance();
  op.acceptanceFailed(Error("映射不完整"));
  expect(op.phase).toBe("acceptance_failed");
  expect(op.result).toEqual(appended());
  await op.advance();
  expect(tx.append).toHaveBeenCalledTimes(1);
  expect(op.transition!.actual).toEqual(appended());
});
it("未知追加持续读取失败时保留固定目标动作和基线，不重发", async () => {
  const tx = transport({
    append: vi.fn(async () => {
      throw Error("lost");
    }),
    read: vi.fn(async () => {
      throw Error("offline");
    }),
  });
  const op = new FollowOperation("new", action, tx);
  await op.advance();
  await op.advance();
  expect(op.phase).toBe("unknown");
  expect(op.targetId).toBe("fixed");
  expect(op.baseline).toEqual(baseline());
  expect(tx.create).toHaveBeenCalledTimes(1);
  expect(tx.append).toHaveBeenCalledTimes(1);
  tx.read = vi.fn(async () => appended());
  await op.advance();
  expect(op.phase).toBe("done");
  expect(tx.append).toHaveBeenCalledTimes(1);
});
it("已确认未追加只在明确重试时对同目标再次追加", async () => {
  const tx = transport({
    append: vi
      .fn()
      .mockRejectedValueOnce(Error("lost"))
      .mockResolvedValue(appended()),
    read: vi.fn(async () => baseline()),
  });
  const op = new FollowOperation("new", action, tx);
  await op.advance();
  expect(op.phase).toBe("not_appended");
  expect(tx.append).toHaveBeenCalledTimes(1);
  await op.advance();
  expect(op.phase).toBe("done");
  expect(tx.create).toHaveBeenCalledTimes(1);
  expect(tx.append).toHaveBeenLastCalledWith("fixed", 1, action, {
    content: appended().content,
    capabilityVersion: undefined,
  });
});
it("基线读取失败后仍使用已知目标", async () => {
  const tx = transport({
    prepare: vi
      .fn()
      .mockRejectedValueOnce(Error("offline"))
      .mockResolvedValue(baseline()),
  });
  const op = new FollowOperation("new", action, tx);
  await op.advance();
  expect(op.phase).toBe("target");
  await op.advance();
  expect(op.phase).toBe("done");
  expect(tx.create).toHaveBeenCalledTimes(1);
});
it("创建结果未知不会重建或按内容认领", async () => {
  const tx = transport({
    create: vi.fn(async () => {
      throw new HttpError("result_unconfirmed", "lost");
    }),
  });
  const op = new FollowOperation("new", action, tx);
  await op.advance();
  await op.advance();
  expect(op.phase).toBe("creation_unknown");
  expect(op.targetId).toBeUndefined();
  expect(tx.create).toHaveBeenCalledTimes(1);
  expect(tx.append).not.toHaveBeenCalled();
});
it("明确创建失败允许重新开始创建", async () => {
  const tx = transport({
    create: vi
      .fn()
      .mockRejectedValueOnce(new HttpError("invalid", "bad", [], 400))
      .mockResolvedValue(baseline()),
  });
  const op = new FollowOperation("new", action, tx);
  await op.advance();
  expect(op.phase).toBe("new");
  await op.advance();
  expect(op.phase).toBe("done");
});
it("未知结果读到不同内容时保留实际记录并禁止重发", async () => {
  const actual = { ...appended(), revision: 4 };
  const tx = transport({
    append: vi.fn(async () => {
      throw Error("lost");
    }),
    read: vi.fn(async () => actual),
  });
  const op = new FollowOperation("fixed", action, tx);
  await op.advance();
  await op.advance();
  expect(op.phase).toBe("conflict");
  expect(op.actual).toEqual(actual);
  expect(tx.append).toHaveBeenCalledTimes(1);
});
it("准备目标基线期间重复调用不会并发追加", async () => {
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const tx = transport({
    prepare: vi.fn(async () => {
      await held;
      return baseline();
    }),
  });
  const op = new FollowOperation("fixed", action, tx);
  const first = op.advance(),
    second = op.advance();
  release();
  await Promise.all([first, second]);
  expect(tx.append).toHaveBeenCalledTimes(1);
  expect(op.phase).toBe("done");
});

it("追加必须匹配发送前完整预期，不能接受任意返回名称或身份", async () => {
  const wrong = {
    ...appended(),
    content: {
      ...appended().content,
      text: JSON.stringify(
        { name: "原计划", actions: [{ ...action, name: "任意名称" }] },
        null,
        2,
      ),
    },
  };
  expect(classifyAppend(baseline(), action, wrong)).toBe("conflict");
  const { initializePreviewMetadata } =
    await import("../../src/shared/automatic-previews");
  const base = {
    ...baseline(),
    content: initializePreviewMetadata(baseline().content, "enabled", "n"),
  };
  const actual = {
    ...base,
    revision: 2,
    content: {
      ...base.content,
      text: JSON.stringify({ name: "原计划", actions: [action] }, null, 2),
      automaticPreviews: {
        ...base.content.automaticPreviews!,
        next: 1,
        actions: [{ id: "wrong" }],
      },
    },
  };
  expect(classifyAppend(base, action, actual)).toBe("conflict");
});
it("核实未追加后保留追加锁，重试不要求再次进入可编辑保存入口", async () => {
  const tx = transport({
    prepare: vi
      .fn()
      .mockResolvedValueOnce(baseline())
      .mockRejectedValue(Error("追加锁仍持有")),
    append: vi
      .fn()
      .mockRejectedValueOnce(Error("lost"))
      .mockResolvedValue(appended()),
    read: async () => baseline(),
  });
  const op = new FollowOperation("fixed", action, tx);
  await op.advance();
  expect(op.phase).toBe("not_appended");
  await op.advance();
  expect(op.phase).toBe("done");
});
it.each(["unknown", "conflict", "not_appended"])(
  "%s不发布成功转换",
  async (phase) => {
    const tx = transport({
      append: async () => {
        throw Error("lost");
      },
      read: async () => {
        if (phase === "unknown") throw Error("offline");
        return phase === "conflict"
          ? { ...appended(), revision: 7 }
          : baseline();
      },
    });
    const op = new FollowOperation("fixed", action, tx);
    await op.advance();
    expect(op.phase).toBe(phase);
    expect(op.transition).toBeUndefined();
  },
);
it("资料长度缺项在形成expected前拒绝且不发送追加", async () => {
  const base = baseline();
  base.content.text =
    '{"actions":[{"name":"A","type":"report_status","params":{"scope":"full"}}]}';
  base.content = initializePreviewMetadata(base.content, "disabled", "n");
  base.content.automaticPreviews!.actions.pop();
  const before = structuredClone(base),
    tx = transport({ prepare: async () => base });
  const op = new FollowOperation("fixed", action, tx);
  await op.advance();
  expect(op.phase).toBe("target");
  expect(op.error).toContain("身份");
  expect(op.expected).toBeUndefined();
  expect(op.attempt).toBeUndefined();
  expect(op.transition).toBeUndefined();
  expect(op.result).toBeUndefined();
  expect(tx.append).not.toHaveBeenCalled();
  expect(base).toEqual(before);
});
it("transport改变发送副本不能改变固定attempt", async () => {
  const tx = transport({
    append: async (_id, _rev, sentAction, expected) => {
      sentAction.name = "被改写";
      expected!.content.text = "被改写";
      return appended();
    },
  });
  const op = new FollowOperation("fixed", action, tx);
  await op.advance();
  expect(op.phase).toBe("done");
  expect(op.attempt!.expected.content).toEqual(appended().content);
  expect(op.attempt!.action).toEqual(action);
});
it("not_appended重试形成新attempt，固定此前已证明的baseline派生链", async () => {
  const base = {
    ...baseline(),
    content: initializePreviewMetadata(baseline().content, "disabled", "n"),
  };
  const capabilities = {
    active: null,
    error: null,
    generation: 2,
    version: "v2",
  };
  const derived = {
    ...base,
    revision: 2,
    content: coordinatePreviews(base.content, capabilities).content,
  };
  const actual = {
    ...derived,
    revision: 3,
    content: appendContentAction(derived.content, action, true),
  };
  const tx = transport({
    capabilities: () => capabilities,
    prepare: async () => base,
    append: vi
      .fn()
      .mockRejectedValueOnce(Error("lost"))
      .mockResolvedValue(actual),
    read: async () => derived,
  });
  const op = new FollowOperation("fixed", action, tx);
  await op.advance();
  const first = op.attempt!;
  expect(op.phase).toBe("not_appended");
  expect(op.transition).toBeUndefined();
  await op.advance();
  expect(op.phase).toBe("done");
  expect(first.baseline.revision).toBe(1);
  expect(op.attempt!.baseline.revision).toBe(2);
  expect(op.attempt!.origin).toEqual(base);
  expect(op.attempt!.baselineChanges).toEqual([
    { actual: derived, capabilities },
  ]);
  expect(op.transition!.actual).toEqual(actual);
});
