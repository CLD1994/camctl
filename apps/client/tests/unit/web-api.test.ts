import { parseClientJson, stringifyJson } from "../../src/shared/json";
import { afterEach, expect, it, vi } from "vitest";
import {
  api,
  createDraftCopy,
  readDraftCreation,
  readDraft,
  observeDraft,
  readDraftObservation,
  type ClientState,
} from "../../src/web/api";
import { record } from "../helpers/draft-conflicts";

afterEach(() => vi.unstubAllGlobals());
it.each([
  ["集合缺失", { startup: { state: "ready" } }],
  ["启动异常", { startup: { state: "faulted" }, drafts: [record(1, "base")] }],
  [
    "重复目标",
    {
      startup: { state: "ready" },
      drafts: [record(1, "base"), record(2, "remote")],
    },
  ],
  [
    "其他记录结构损坏",
    {
      startup: { state: "ready" },
      drafts: [record(1, "base"), { id: "other" }],
    },
  ],
])("读回拒绝%s，不能证明目标存在", async (_name, state) => {
  await expect(
    readDraft("d1", async () => state as ClientState),
  ).rejects.toThrow();
});
it.each([
  null,
  {},
  { drafts: [] },
  { startup: { state: "ready" } },
  { startup: { state: "faulted" }, drafts: [] },
  {
    startup: { state: "ready" },
    drafts: [record(1, "base"), record(2, "remote")],
  },
  {
    startup: { state: "ready" },
    drafts: [{ ...record(1, "base"), content: { text: "{", pending: [] } }],
  },
  {
    startup: { state: "ready" },
    drafts: [{ ...record(1, "base"), exportedRequestId: "42" }],
  },
  { startup: { state: "ready" }, drafts: [{ id: "other" }] },
])("不完整集合 %j 不能证明删除", (state) => {
  expect(observeDraft("absent", state as ClientState).kind).toBe("unavailable");
});
it.each([true, false])("可靠完整集合允许判定存在=%s", (exists) => {
  const capabilities = { active: null, error: null, generation: 1 };
  const draft = record(1, "base");
  const result = observeDraft("d1", {
    startup: { state: "ready" },
    drafts: exists ? [draft] : [],
    capabilities,
  } as unknown as ClientState);
  expect(result).toEqual(
    exists ? { kind: "available", draft, capabilities } : { kind: "deleted" },
  );
});
it.each(["fetch", "parse"])(
  "%s 失败产生 unavailable 观察，不证明删除",
  async (failure) => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => {
        if (failure === "fetch") throw Error("offline");
        return new Response("{");
      }),
    );
    const result = await readDraftObservation("d1", () =>
      api<ClientState>("/state"),
    );
    expect(result.kind).toBe("unavailable");
    if (result.kind === "unavailable")
      expect(result.message.length).toBeGreaterThan(0);
  },
);
it("API 响应恢复草稿的非法原文，不提前执行公共计划校验", async () => {
  const value = {
    drafts: [
      {
        content: {
          text: '{"name":"\ud800"}',
          pending: { "/actions/0/params": { kind: "json", text: '"\udc00' } },
        },
      },
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(
      async () => new Response(JSON.stringify(value), { status: 200 }),
    ),
  );
  expect(await api("/state")).toEqual(value);
});

it("API 实际请求体与响应保留非整数词元", async () => {
  const input = parseClientJson('{"params":{"value":1.0000000000000001}}');
  const fetcher = vi.fn<typeof fetch>(async (_url, options) => {
    expect(options?.body).toContain("1.0000000000000001");
    return new Response(String(options?.body));
  });
  vi.stubGlobal("fetch", fetcher);
  const output = await api("/presets", "POST", input);
  expect(stringifyJson(output)).toContain("1.0000000000000001");
});
const creationInput = {
  text: "{",
  pending: { "": { kind: "json", text: "{" } },
};
const createdDraft = {
  id: "copy-id",
  revision: 1,
  content: creationInput,
  createdAt: "2026-10-11 00:00:00",
  updatedAt: "2026-10-11 00:00:00",
};
const creationReply = {
  kind: "created",
  creation: {
    attemptId: "attempt/1",
    input: creationInput,
    created: createdDraft,
  },
  target: { kind: "editable", draft: createdDraft },
};
for (const method of ["GET", "PUT"] as const) {
  it.each([
    [
      "初始版本带导出标记",
      { kind: "exported", draft: { ...createdDraft, exportedRequestId: "42" } },
    ],
    [
      "初始版本内容变化",
      { kind: "editable", draft: { ...createdDraft, content: { text: "[]" } } },
    ],
    ["导出类别缺少标记", { kind: "exported", draft: createdDraft }],
  ])(`另存 ${method} 拒绝不可靠生命周期：%s`, async (_name, target) => {
    const reply = { ...creationReply, target };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (_url, options) => {
        expect(options?.method).toBe(method);
        if (method === "PUT")
          expect(parseClientJson(String(options?.body))).toEqual({
            content: creationInput,
          });
        return new Response(JSON.stringify(reply));
      }),
    );
    await expect(
      method === "PUT"
        ? createDraftCopy("attempt/1", creationInput as any)
        : readDraftCreation("attempt/1"),
    ).rejects.toMatchObject({ code: "result_unconfirmed" });
  });
  it.each([
    ["初始可编辑", { kind: "editable", draft: createdDraft }],
    [
      "后续可编辑",
      { kind: "editable", draft: { ...createdDraft, revision: 2 } },
    ],
    [
      "后续已导出",
      {
        kind: "exported",
        draft: { ...createdDraft, revision: 2, exportedRequestId: "42" },
      },
    ],
    ["目标已删除", { kind: "deleted" }],
  ])(`另存 ${method} 接纳可靠生命周期：%s`, async (_name, target) => {
    const reply = { ...creationReply, target };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (_url, options) => {
        expect(options?.method).toBe(method);
        if (method === "PUT")
          expect(parseClientJson(String(options?.body))).toEqual({
            content: creationInput,
          });
        return new Response(JSON.stringify(reply));
      }),
    );
    expect(
      await (method === "PUT"
        ? createDraftCopy("attempt/1", creationInput as any)
        : readDraftCreation("attempt/1")),
    ).toEqual(reply);
  });
}
it("可靠的缺失创建事实保留显式未创建身份", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(
      async () =>
        new Response('{"kind":"not_created","attemptId":"attempt/1"}'),
    ),
  );
  expect(await readDraftCreation("attempt/1")).toEqual({
    kind: "not_created",
    attemptId: "attempt/1",
  });
});
it("PUT 回执的原输入不同不能认领已创建结果", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(
      async () =>
        new Response(
          JSON.stringify({
            ...creationReply,
            creation: { ...creationReply.creation, input: { text: "[]" } },
          }),
        ),
    ),
  );
  await expect(
    createDraftCopy("attempt/1", creationInput as any),
  ).rejects.toMatchObject({ code: "result_unconfirmed" });
});
it("核实回执恢复全部类型、对象、预览与未完成资料", async () => {
  const input = {
    text: "{",
    pending: { "": { kind: "json", text: "{" } },
    actionVariants: {
      "0": [
        { type: "camera_record", fields: { device_id: "cam" }, pending: {} },
      ],
    },
    parameterVariants: {
      "0": [
        {
          paramsText: '{"params":{"type":"photo","amount":1.0000000000000001}}',
          pending: { "/amount": { kind: "number", text: "-" } },
        },
      ],
    },
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
    automaticPreviews: {
      namespace: "source",
      next: 1,
      intent: "unset",
      actions: [{ id: "source-id" }],
    },
  };
  const draft = { ...createdDraft, content: input };
  const reply = {
    kind: "created",
    creation: { attemptId: "attempt/1", input, created: draft },
    target: { kind: "editable", draft },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async () => new Response(stringifyJson(reply))),
  );
  expect(await readDraftCreation("attempt/1")).toEqual(reply);
});
it.each(["PUT", "GET"])(
  "另存 %s 网络响应未知保留 result_unconfirmed",
  async (method) => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => {
        throw Error("连接中断");
      }),
    );
    await expect(
      method === "PUT"
        ? createDraftCopy("attempt/1", creationInput as any)
        : readDraftCreation("attempt/1"),
    ).rejects.toMatchObject({ code: "result_unconfirmed" });
  },
);
it.each([
  "",
  "{",
  "null",
  "{}",
  '{"kind":"not_created"}',
  '{"kind":"not_created","attemptId":"other"}',
  '{"kind":"created"}',
  JSON.stringify({ ...creationReply, target: { kind: "editable" } }),
  JSON.stringify({
    ...creationReply,
    creation: { ...creationReply.creation, created: { id: "copy-id" } },
  }),
])("不完整创建响应 %s 不能认定创建或未创建", async (body) => {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async () => new Response(body)),
  );
  await expect(readDraftCreation("attempt/1")).rejects.toMatchObject({
    code: "result_unconfirmed",
  });
});
it.each(["editable", "exported", "deleted"])(
  "另存核实完整解析 %s 当前目标与全部输入资料",
  async (kind) => {
    const target =
      kind === "deleted"
        ? { kind }
        : {
            kind,
            draft: {
              ...createdDraft,
              revision: 2,
              ...(kind === "exported" ? { exportedRequestId: "42" } : {}),
            },
          };
    const reply = { ...creationReply, target };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(reply))),
    );
    expect(await readDraftCreation("attempt/1")).toEqual(reply);
  },
);
it("另存 PUT 编码固定身份并发送和解析完整数字词元", async () => {
  const input = parseClientJson(
    '{"text":"{","actionVariants":{"0":[{"type":"camera_record","fields":{"params":{"amount":1.0000000000000001}},"pending":{}}]}}',
  );
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async (url, options) => {
      expect(url).toBe("/api/draft-creations/attempt%2F1");
      expect(options?.method).toBe("PUT");
      expect(String(options?.body)).toContain("1.0000000000000001");
      const draft = { ...createdDraft, content: input };
      return new Response(
        stringifyJson({
          kind: "created",
          creation: { attemptId: "attempt/1", input, created: draft },
          target: { kind: "editable", draft },
        }),
      );
    }),
  );
  expect(
    stringifyJson(await createDraftCopy("attempt/1", input as any)),
  ).toContain("1.0000000000000001");
});
it("另存完整故障回执保留 HTTP 错误码和状态", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(
      async () =>
        new Response('{"code":"creation_conflict","message":"固定输入冲突"}', {
          status: 409,
        }),
    ),
  );
  await expect(
    createDraftCopy("attempt/1", creationInput as any),
  ).rejects.toMatchObject({ code: "creation_conflict", status: 409 });
});
it.each(["input", "created", "target"])(
  "完整封套中的 %s 私有结构损坏不能认定可靠结果",
  async (location) => {
    const reply = structuredClone(creationReply);
    const invalid = { text: "{", pending: [] };
    if (location === "input") reply.creation.input = invalid as any;
    if (location === "created") reply.creation.created.content = invalid as any;
    if (location === "target") reply.target.draft.content = invalid as any;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => new Response(JSON.stringify(reply))),
    );
    await expect(readDraftCreation("attempt/1")).rejects.toMatchObject({
      code: "result_unconfirmed",
    });
  },
);
it.each([
  "null",
  "{}",
  '{"code":"data_fault"}',
  '{"code":0,"message":"数据故障"}',
])("另存不完整故障响应 %s 保留未确认状态", async (body) => {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(async () => new Response(body, { status: 503 })),
  );
  await expect(readDraftCreation("attempt/1")).rejects.toMatchObject({
    code: "result_unconfirmed",
  });
});
