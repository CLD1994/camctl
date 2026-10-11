import { expect, it, vi } from "vitest";
import { DraftCopyOperation } from "../../src/web/draft-copy";
import { creation } from "../helpers/draft-conflicts";
import type {
  DraftContent,
  DraftCreationResult,
} from "../../src/server/models";
import {
  cloneClientJson,
  parseClientJson,
  stringifyJson,
} from "../../src/shared/json";
const input: DraftContent = {
  text: "{",
  pending: { "": { kind: "json", text: "{" } },
};
function success(
  content = input,
): Extract<DraftCreationResult, { kind: "created" }> {
  const fact = creation(content);
  return {
    kind: "created",
    creation: fact,
    target: { kind: "editable", draft: cloneClientJson(fact.created) },
  };
}
function operation(
  create: () => Promise<DraftCreationResult>,
  read = create,
  content = input,
) {
  return new DraftCopyOperation(
    "source",
    4,
    content,
    { create, read },
    "copy-1",
  );
}
it("目标删除后的同次核实结束并保留未转交输入", async () => {
  const fact = creation(input),
    result: DraftCreationResult = {
      kind: "created",
      creation: fact,
      target: { kind: "deleted" },
    };
  const create = vi.fn(async () => {
      throw Error("lost");
    }),
    read = vi.fn(async () => result);
  const op = operation(create, read);
  await op.advance();
  await op.verify();
  await op.verify();
  expect(op.phase).toBe("created_untransferred");
  expect(op.input).toEqual(input);
  expect(op.result).toEqual(result);
  expect(create).toHaveBeenCalledTimes(1);
  expect(read).toHaveBeenCalledTimes(1);
});
it("未创建只按固定身份和输入重试", async () => {
  const create = vi
    .fn()
    .mockResolvedValueOnce({ kind: "not_created", attemptId: "copy-1" })
    .mockResolvedValueOnce(success());
  const op = operation(create);
  await op.advance();
  expect(op.phase).toBe("not_created");
  await op.advance();
  expect(op.phase).toBe("created_untransferred");
  expect(create.mock.calls).toEqual([
    ["copy-1", input],
    ["copy-1", input],
  ]);
});
it("请求在途不能核实或开始第二次创建", async () => {
  let resolve!: (v: DraftCreationResult) => void;
  const create = vi.fn(
      () => new Promise<DraftCreationResult>((r) => (resolve = r)),
    ),
    read = vi.fn(async () => success());
  const op = operation(create, read),
    pending = op.advance();
  await op.advance();
  await op.verify();
  expect(op.phase).toBe("submitting");
  expect(create).toHaveBeenCalledTimes(1);
  expect(read).not.toHaveBeenCalled();
  resolve(success());
  await pending;
  expect(op.phase).toBe("created_untransferred");
});
it("核实失败保留未知责任并能恢复", async () => {
  const read = vi
    .fn()
    .mockRejectedValueOnce(Error("offline"))
    .mockResolvedValueOnce(success());
  const op = operation(async () => {
    throw Error("lost");
  }, read);
  await op.advance();
  await op.verify();
  expect(op.phase).toBe("unknown");
  await op.verify();
  expect(op.phase).toBe("created_untransferred");
});
it.each(["attempt", "input", "target", "revision", "terminal", "structure"])(
  "拒绝不可靠创建事实：%s",
  async (kind) => {
    const bad = success();
    if (kind === "attempt") bad.creation.attemptId = "other";
    if (kind === "input") bad.creation.input = { text: "different" };
    if (bad.target.kind !== "deleted") {
      if (kind === "target") bad.target.draft.id = "other";
      if (kind === "revision") bad.target.draft.revision = 0;
      if (kind === "terminal") bad.target.kind = "exported";
      if (kind === "structure") (bad.target.draft.content as any).pending = [];
    }
    const op = operation(async () => bad);
    await op.advance();
    expect(op.phase).toBe("invalid");
    expect(op.result).toBeUndefined();
    expect(op.input).toEqual(input);
  },
);
it("固定依据和公开副本不能被调用者或 transport 改写", async () => {
  const original = cloneClientJson(input),
    result = success(original);
  const op = operation(
    async () => result,
    async () => result,
    original,
  );
  original.text = "caller";
  op.input.text = "getter";
  await op.advance();
  expect(op.phase).toBe("created_untransferred");
  result.creation.input.text = "transport";
  op.result!.kind === "created" &&
    (op.result!.creation.input.text = "getter result");
  expect(op.input.text).toBe("{");
  expect(op.result!.kind === "created" && op.result!.creation.input.text).toBe(
    "{",
  );
});
it("目标后续编辑用实际完整记录并保留首次创建快照", async () => {
  const result = success();
  if (result.target.kind !== "deleted")
    result.target.draft = {
      ...result.target.draft,
      revision: 3,
      content: { text: "later" },
    };
  const op = operation(async () => result);
  await op.advance();
  expect(op.result).toEqual(result);
  expect(op.input).toEqual(input);
  op.completeTransfer();
  expect(op.phase).toBe("transferred");
  await op.advance();
  expect(op.phase).toBe("transferred");
});
it("已创建事实不能降成未创建或改写首次快照", async () => {
  const read = vi
    .fn()
    .mockResolvedValueOnce({ kind: "not_created", attemptId: "copy-1" });
  const op = operation(async () => success(), read);
  await op.advance();
  await op.verify();
  expect(op.phase).toBe("invalid");
  expect(op.result?.kind).toBe("created");
});
it("私有数字词元参与固定输入复制", async () => {
  const content = parseClientJson(
    '{"text":"{","pending":{"":{"kind":"json","text":"{"}},"actionVariants":{"0":[{"type":"old","fields":{"x":1.0000000000000001},"fieldsText":"{\\"x\\":1.0000000000000001}","pending":{}}]}}',
  ) as DraftContent;
  const op = operation(async () => success(content), undefined, content);
  await op.advance();
  expect(op.phase).toBe("created_untransferred");
  expect(stringifyJson(op.input)).toContain("1.0000000000000001");
});
it("首次创建快照改变时保留原事实并拒绝认领", async () => {
  const changed = success();
  changed.creation.created.content = { text: "rewritten" };
  if (changed.target.kind !== "deleted")
    changed.target.draft = { ...changed.creation.created, revision: 2 };
  const op = operation(
    async () => success(),
    async () => changed,
  );
  await op.advance();
  await op.verify();
  expect(op.phase).toBe("invalid");
  expect(op.result).toEqual(success());
});
it("较旧当前目标不会覆盖已核实的新版本", async () => {
  const current = success();
  if (current.target.kind !== "deleted") current.target.draft.revision = 3;
  const op = operation(
    async () => current,
    async () => success(),
  );
  await op.advance();
  await op.verify();
  expect(op.phase).toBe("invalid");
  expect(op.result).toEqual(current);
});
it("已导出目标结束核实但禁止转交和同次重建", async () => {
  const terminal = success();
  terminal.target = {
    kind: "exported",
    draft: {
      ...terminal.creation.created,
      revision: 2,
      exportedRequestId: "42",
    },
  };
  const create = vi.fn(async () => terminal),
    read = vi.fn(async () => terminal),
    op = operation(create, read);
  await op.advance();
  await op.verify();
  await op.advance();
  expect(op.phase).toBe("created_untransferred");
  expect(op.locksSource).toBe(true);
  expect(() => op.completeTransfer()).toThrow();
  expect(create).toHaveBeenCalledTimes(1);
  expect(read).not.toHaveBeenCalled();
});
it("未知和结构异常不能放弃固定操作或解除源锁", async () => {
  const op = operation(
    async () => {
      throw Error("lost");
    },
    async () => ({ kind: "not_created", attemptId: "wrong" }),
  );
  await op.advance();
  expect(() => op.abandon()).toThrow();
  await op.verify();
  expect(op.phase).toBe("invalid");
  expect(op.locksSource).toBe(true);
  expect(() => op.abandon()).toThrow();
});
it("操作完成准备先检查资格且不改变事实，提交仅同步应用阶段", async () => {
  const changed = vi.fn(),
    fact = success(),
    op = new DraftCopyOperation(
      "source",
      4,
      input,
      { create: async () => fact, read: async () => fact },
      "copy-1",
      changed,
    );
  await op.advance();
  const notifications = changed.mock.calls.length,
    commit = op.prepareTransferCompletion();
  expect(op.phase).toBe("created_untransferred");
  expect(op.result).toEqual(fact);
  expect(changed).toHaveBeenCalledTimes(notifications);
  expect(commit()).toBeUndefined();
  expect(op.phase).toBe("transferred");
  expect(op.result).toEqual(fact);
  expect(op.input).toEqual(input);
  expect(changed).toHaveBeenCalledTimes(notifications);
  op.completeTransfer();
  expect(op.phase).toBe("transferred");
});
it.each([
  "new",
  "unknown",
  "invalid",
  "not_created",
  "deleted",
  "exported",
  "abandoned",
  "transferred",
] as const)("%s 操作不能准备新的完成应用", async (phase) => {
  const terminal = success();
  if (phase === "deleted" || phase === "abandoned")
    terminal.target = { kind: "deleted" };
  if (phase === "exported")
    terminal.target = {
      kind: "exported",
      draft: {
        ...terminal.creation.created,
        revision: 2,
        exportedRequestId: "42",
      },
    };
  const op = operation(async () => {
    if (phase === "unknown") throw Error("lost");
    if (phase === "invalid") return { kind: "not_created", attemptId: "wrong" };
    if (phase === "not_created")
      return { kind: "not_created", attemptId: "copy-1" };
    return terminal;
  });
  if (phase !== "new") await op.advance();
  if (phase === "abandoned") op.abandon();
  if (phase === "transferred") op.completeTransfer();
  const before = {
    phase: op.phase,
    result: op.result,
    abandoned: op.abandoned,
  };
  expect(() => op.prepareTransferCompletion()).toThrow();
  expect({
    phase: op.phase,
    result: op.result,
    abandoned: op.abandoned,
  }).toEqual(before);
  expect(op.input).toEqual(input);
});
it("核实在途时拒绝完成准备，可靠结束后才获得资格", async () => {
  let resolve!: (value: DraftCreationResult) => void;
  const op = operation(
    async () => success(),
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  await op.advance();
  const verification = op.verify();
  expect(op.phase).toBe("submitting");
  expect(() => op.prepareTransferCompletion()).toThrow();
  expect(op.result).toEqual(success());
  resolve(success());
  await verification;
  const commit = op.prepareTransferCompletion();
  expect(op.phase).toBe("created_untransferred");
  commit();
  expect(op.phase).toBe("transferred");
});
const privateParts: Partial<DraftContent>[] = [
  { pending: { "": { kind: "json", text: "{" } } },
  {
    actionVariants: {
      "0": [
        { type: "camera_record", fields: { device_id: "cam" }, pending: {} },
      ],
    },
  },
  {
    parameterVariants: {
      "0": [
        {
          paramsText: '{"params":{"type":"photo","amount":1.0000000000000001}}',
          pending: { "/amount": { kind: "number", text: "-" } },
        },
      ],
    },
  },
  {
    objectVariants: {
      "0": {
        "/exposure": {
          discriminator: "mode",
          variants: [
            {
              valueText: '{"value":{"mode":"manual","iso":1e-999}}',
              pending: {},
            },
          ],
        },
      },
    },
  },
  {
    automaticPreviews: {
      namespace: "source",
      next: 1,
      intent: "unset",
      actions: [{ id: "source-id" }],
    },
  },
];
it.each([...privateParts, Object.assign({}, ...privateParts)])(
  "完整固定输入保留私有资料 %j",
  async (part) => {
    const content: DraftContent = { text: "{", ...part };
    const op = operation(async () => success(content), undefined, content);
    await op.advance();
    expect(op.phase).toBe("created_untransferred");
    expect(stringifyJson(op.input)).toBe(stringifyJson(content));
  },
);
