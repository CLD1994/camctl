import { expect, it, vi } from "vitest";
import {
  FollowOperation,
  classifyAppend,
  type FollowTransport,
} from "../../src/web/followup";
import type { Draft } from "../../src/server/models";
import { HttpError } from "../../src/web/api";
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
    text: JSON.stringify({
      name: "原计划",
      actions: [{ ...action, name: "同步 2" }],
    }),
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
  expect(tx.append).toHaveBeenLastCalledWith("fixed", 1, action);
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
