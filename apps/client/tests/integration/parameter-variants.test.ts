import { afterEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import type { Server } from "node:http";
import { tmpdir } from "node:os";
import { basename, join, resolve, sep } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import type {
  Draft,
  DraftContent,
  ExportedRequest,
  ParameterVariant,
} from "../../src/server/models";
import {
  initializePreviewMetadata,
  sameContent,
} from "../../src/shared/automatic-previews";
import { parseClientJson, stringifyJson } from "../../src/shared/json";
import { switchActionType } from "../../src/web/action-drafts";
import { editValue, removeAction, setValue } from "../../src/web/editing";
import { switchParameterType } from "../../src/web/parameter-drafts";
import { DraftSession } from "../../src/web/session";

const cleanup: Array<() => Promise<void>> = [];
afterEach(async () => {
  for (const close of cleanup.splice(0)) await close();
});

function catalog(previewSupported = true) {
  return {
    devices: [
      {
        device_id: "cam",
        driver_id: "test",
        actions: [
          {
            type: "camera_record",
            parameter_types: ["standard", "bitrate"].map((type) => ({
              type,
              name: type,
              description: type,
              preview_supported: previewSupported,
              schema: {
                $schema: "https://json-schema.org/draft/2020-12/schema",
                type: "object",
                required: ["type"],
                properties: { type: { const: type } },
                additionalProperties: true,
              },
            })),
          },
        ],
      },
    ],
  };
}

/** 每次只启动临时目录和随机端口；重启重新创建真实 Application 和 HTTP 服务。 */
async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "client-parameter-variants-"));
  writeFileSync(
    join(directory, "device-capabilities.json"),
    JSON.stringify(catalog()),
  );
  let app!: Application;
  let files!: Files;
  let server: Server | undefined;
  let base = "";
  const start = async () => {
    app = new Application(directory);
    files = new Files(app);
    server = await new Promise<Server>((resolveServer) => {
      const listening = createHttpApp(app, files).listen(0, "127.0.0.1", () =>
        resolveServer(listening),
      );
    });
    base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  };
  const stop = async () => {
    if (!server) return;
    await files.idle();
    const closing = server;
    server = undefined;
    await new Promise<void>((resolveClose, reject) =>
      closing.close((error) => (error ? reject(error) : resolveClose())),
    );
    app.store.close();
  };
  cleanup.push(async () => {
    await stop();
    const target = resolve(directory);
    if (
      !target.startsWith(resolve(tmpdir()) + sep) ||
      !basename(target).startsWith("client-parameter-variants-")
    )
      throw Error("临时测试目录超出清理范围");
    rmSync(target, { recursive: true, force: true });
  });
  await start();
  const request = async <T = unknown>(
    path: string,
    method = "GET",
    body?: unknown,
  ) => {
    const response = await fetch(base + path, {
      method,
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : stringifyJson(body),
    });
    const raw = await response.text();
    return { status: response.status, value: parseClientJson(raw) as T, raw };
  };
  const ok = async <T>(
    path: string,
    method = "GET",
    body?: unknown,
    status = 200,
  ) => {
    const response = await request<T>(path, method, body);
    expect(response.status, response.raw).toBe(status);
    return response.value;
  };
  const state = () => ok<ReturnType<Application["state"]>>("/api/state");
  const read = async (id: string) => {
    const draft = (await state()).drafts?.find((item) => item.id === id);
    expect(draft).toBeDefined();
    return draft!;
  };
  await ok("/api/initialize", "POST");
  return {
    get app() {
      return app;
    },
    request,
    ok,
    state,
    read,
    create: (content: DraftContent) =>
      ok<Draft>("/api/drafts", "POST", { content }, 201),
    save: (draft: Draft, content: DraftContent) =>
      ok<Draft>(`/api/drafts/${draft.id}`, "PUT", {
        revision: draft.revision,
        content,
      }),
    async export(draft: Draft, content = draft.content) {
      const capabilityVersion = (await state()).capabilities.version;
      return request<ExportedRequest & { code?: string }>(
        `/api/drafts/${draft.id}/export`,
        "POST",
        { revision: draft.revision, content, capabilityVersion },
      );
    },
    async restart() {
      await stop();
      await start();
      expect((await state()).startup.state).toBe("ready");
    },
    async reload(previewSupported: boolean) {
      writeFileSync(
        join(directory, "device-capabilities.json"),
        JSON.stringify(catalog(previewSupported)),
      );
      await ok("/api/capabilities/reload", "POST");
    },
  };
}

function capture(name: string, params?: string): string {
  return `{"name":${JSON.stringify(name)},"type":"camera_record","device_id":"cam","scheduled_at":"2026-10-10 01:00:00","policy":{"max_delay_ms":0}${params === undefined ? "" : `,"params":${params}`}}`;
}
function content(params?: string): DraftContent {
  return { text: `{"name":"参数计划","actions":[${capture("拍摄", params)}]}` };
}
const standardParams =
  '{"type":"standard","duration_s":60,"fine":1.0000000000000001,"nested":{"deep":1e-999},"extra":null}';
const bitrateParams = { type: "bitrate", bitrate_mbps: 70, duration_s: 10 };
function inactiveStandard(pending = false): DraftContent {
  const original = pending
    ? editValue(
        content(standardParams),
        ["actions", 0, "params", "duration_s"],
        "-",
        "number",
      )
    : content(standardParams);
  return setValue(
    switchParameterType(original, 0, "bitrate"),
    ["actions", 0, "params"],
    bitrateParams,
  );
}
function actions(input: DraftContent): Array<Record<string, any>> {
  return (
    parseClientJson(input.text) as { actions: Array<Record<string, any>> }
  ).actions;
}
function storedStandard(input: DraftContent, index = "0"): ParameterVariant {
  const entries = input.parameterVariants?.[index];
  expect(entries).toHaveLength(1);
  return entries![0];
}

it("停用完整参数经 HTTP 保存、SQLite 重开和服务重启后恢复原数字词元", async () => {
  const host = await setup();
  const original = content(standardParams);
  const initial = await host.create(original);
  const changed = inactiveStandard();
  const saved = await host.save(initial, changed);
  expect(saved.content.text).toBe(changed.text);
  expect(storedStandard(saved.content)).toEqual({
    paramsText: '{"params":' + standardParams + "}",
    pending: {},
  });
  const reopened = await host.read(saved.id);
  expect(reopened.content).toEqual(changed);
  expect(reopened.lastWrite?.input).toEqual(changed);
  await host.restart();
  const restarted = await host.read(saved.id);
  expect(restarted).toEqual(saved);
  const restored = switchParameterType(restarted.content, 0, "standard");
  expect(restored.text).toMatch(/"fine"\s*:\s*1\.0000000000000001/);
  expect(restored.text).toMatch(/"deep"\s*:\s*1e-999/);
  expect(actions(restored)[0]).toMatchObject({
    name: "拍摄",
    device_id: "cam",
    scheduled_at: "2026-10-10 01:00:00",
    policy: { max_delay_ms: 0 },
    params: { type: "standard", duration_s: 60, extra: null },
  });
  expect(restored.parameterVariants?.["0"]).toEqual([
    {
      paramsText:
        '{"params":{"type":"bitrate","bitrate_mbps":70,"duration_s":10}}',
      pending: {},
    },
  ]);
});

it.each([
  { token: "1e-999", projection: 0 },
  { token: "1.0000000000000001", projection: 1 },
])(
  "数值类型身份 $token 经真实保存和重启后按原数恢复",
  async ({ token, projection }) => {
    const host = await setup();
    const original = content(`{"type":${token},"nested":{"value":1e-999}}`);
    const switched = switchParameterType(original, 0, "bitrate");
    const draft = await host.create(switched);
    await host.restart();
    const actual = await host.read(draft.id);
    expect(actual.content.parameterVariants?.["0"]?.[0].paramsText).toContain(
      `"type":${token}`,
    );
    const restored = switchParameterType(actual.content, 0, projection, token);
    expect(restored.text).toMatch(
      new RegExp(
        `"type"\\s*:\\s*${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`,
      ),
    );
    expect(restored.text).toMatch(/"value"\s*:\s*1e-999/);
  },
);

it.each([undefined, "null", "[]", "1e-999"])(
  "未选择分支中的参数形状 %s 经保存和重启后保持",
  async (params) => {
    const host = await setup();
    const switched = switchParameterType(content(params), 0, "bitrate");
    const draft = await host.create(switched);
    await host.restart();
    const actual = await host.read(draft.id);
    expect(actual.content.parameterVariants?.["0"]?.[0].paramsText).toBe(
      params === undefined ? "{}" : `{"params":${params}}`,
    );
    const restored = switchParameterType(actual.content, 0, undefined);
    if (params === undefined)
      expect(actions(restored)[0]).not.toHaveProperty("params");
    else
      expect(restored.text).toMatch(
        new RegExp(
          `"params"\\s*:\\s*${params.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`,
        ),
      );
  },
);

it("停用成员输入不阻止当前导出，切回后原输入继续阻止导出", async () => {
  const host = await setup();
  const switched = inactiveStandard(true);
  let draft = await host.create(switched);
  expect(draft.content.pending ?? {}).toEqual({});
  expect(storedStandard(draft.content).pending).toEqual({
    "/duration_s": { kind: "number", text: "-" },
  });
  await host.restart();
  draft = await host.read(draft.id);
  const copy = await host.ok<Draft>(
    `/api/drafts/${draft.id}/copy`,
    "POST",
    undefined,
    201,
  );
  expect(copy.content.parameterVariants).toEqual(
    draft.content.parameterVariants,
  );
  const exported = await host.export(copy);
  expect(exported.status, exported.raw).toBe(200);
  expect(exported.value.body.actions).toEqual([
    {
      name: "拍摄",
      type: "camera_record",
      device_id: "cam",
      scheduled_at: "2026-10-10 01:00:00",
      policy: { max_delay_ms: 0 },
      params: bitrateParams,
    },
  ]);
  for (const field of [
    "parameterVariants",
    "actionVariants",
    "pending",
    "automaticPreviews",
  ])
    expect(exported.value.body).not.toHaveProperty(field);
  const downloaded = await host.ok<Record<string, unknown>>(
    `/api/requests/${exported.value.id}/download`,
  );
  expect(downloaded).toEqual(exported.value.body);
  const fromRequest = await host.ok<Draft>(
    `/api/requests/${exported.value.id}/copy`,
    "POST",
    undefined,
    201,
  );
  expect(fromRequest.content.parameterVariants).toBeUndefined();
  expect(fromRequest.content.pending).toBeUndefined();
  expect(actions(fromRequest.content)[0].params).toEqual(bitrateParams);

  draft = await host.save(
    draft,
    switchParameterType(draft.content, 0, "standard"),
  );
  expect(draft.content.pending).toEqual({
    "/actions/0/params/duration_s": { kind: "number", text: "-" },
  });
  const rejected = await host.export(draft);
  expect(rejected.status).toBe(400);
  expect(rejected.value.code).toBe("unfinished_input");
  expect(await host.read(draft.id)).toEqual(draft);
  expect((await host.state()).requests).toHaveLength(1);
});

it("停用外层动作分支的参数资料经 HTTP 保存和重启后共同恢复", async () => {
  const host = await setup();
  const inactive = inactiveStandard(true);
  const switched = setValue(
    switchActionType(inactive, 0, "report_status"),
    ["actions", 0, "params"],
    { scope: "full" },
  );
  const draft = await host.create(switched);
  expect(draft.content.parameterVariants?.["0"]).toBeUndefined();
  expect(draft.content.actionVariants?.["0"]?.[0].parameterVariants).toEqual(
    inactive.parameterVariants?.["0"],
  );
  await host.restart();
  const actual = await host.read(draft.id);
  expect(actual.content).toEqual(switched);
  const restoredAction = switchActionType(actual.content, 0, "camera_record");
  expect(restoredAction.parameterVariants?.["0"]).toEqual(
    inactive.parameterVariants?.["0"],
  );
  const restoredParams = switchParameterType(restoredAction, 0, "standard");
  expect(restoredParams.text).toMatch(/"fine"\s*:\s*1\.0000000000000001/);
  expect(restoredParams.pending).toEqual({
    "/actions/0/params/duration_s": { kind: "number", text: "-" },
  });
});

it("正文相同但参数资料不同的版本不能复用导出保存绑定", async () => {
  const host = await setup();
  const original = inactiveStandard();
  let draft = await host.create(original);
  const changed: DraftContent = {
    ...original,
    parameterVariants: {
      "0": [
        { paramsText: '{"params":{"type":"standard","fine":1}}', pending: {} },
      ],
    },
  };
  expect(changed.text).toBe(original.text);
  expect(sameContent(original, changed)).toBe(false);
  draft = await host.save(draft, changed);
  expect(draft.lastWrite).toEqual({
    revision: 2,
    input: changed,
    content: changed,
  });
  const rejected = await host.export(draft, original);
  expect(rejected.status).toBe(409);
  expect(rejected.value.code).toBe("content_conflict");
  expect(await host.read(draft.id)).toEqual(draft);
  expect((await host.state()).requests).toEqual([]);
});

it.each(["complete", "missing-variants"] as const)(
  "保存响应丢失后按真实服务的完整参数资料核实 %s",
  async (mode) => {
    const host = await setup();
    const draft = await host.create(content(JSON.stringify(bitrateParams)));
    const expected = inactiveStandard();
    const session = new DraftSession(
      draft,
      {
        async save(id, revision, input) {
          const stored =
            mode === "complete"
              ? input
              : { text: input.text, pending: input.pending };
          await host.ok<Draft>(`/api/drafts/${id}`, "PUT", {
            revision,
            content: stored,
          });
          throw Error("保存响应丢失");
        },
        read: (id) => host.read(id),
      },
      () => {},
    );
    session.edit(expected);
    if (mode === "complete") {
      await session.flush();
      expect(session.saved).toBe(true);
      expect(session.content).toEqual(expected);
      expect((await host.read(draft.id)).lastWrite?.input).toEqual(expected);
    } else {
      await expect(session.flush()).rejects.toThrow();
      expect(session.saved).toBe(false);
      expect(session.content).toEqual(expected);
      expect(session.conflict?.content.parameterVariants).toBeUndefined();
      expect(session.conflict?.content.text).toBe(expected.text);
      expect(
        (await host.read(draft.id)).content.parameterVariants,
      ).toBeUndefined();
    }
  },
);

describe("非法参数资料不产生部分写入", () => {
  it("顶层当前参数类型不能同时拥有停用同型内容", async () => {
    const host = await setup();
    const draft = host.app.createDraft(
      content('{"type":"standard","duration_s":60}'),
    );
    const bad = {
      ...draft.content,
      parameterVariants: {
        "0": [
          {
            paramsText: '{"params":{"type":"standard","duration_s":10}}',
            pending: {},
          },
        ],
      },
    };
    expect(() => host.app.saveDraft(draft.id, draft.revision, bad)).toThrowError(
      expect.objectContaining({ code: "invalid_content" }),
    );
    expect(host.app.draft(draft.id)).toEqual(draft);
    expect(() => host.app.createDraft(bad)).toThrowError(
      expect.objectContaining({ code: "invalid_content" }),
    );
    expect(host.app.store.all("drafts")).toEqual([draft]);
    expect(host.app.store.all("requests")).toEqual([]);
  });
  it("停用外层动作的当前参数类型不能同时拥有停用同型内容", async () => {
    const host = await setup();
    const draft = host.app.createDraft(content(JSON.stringify(bitrateParams)));
    const bad = {
      ...draft.content,
      actionVariants: {
        "0": [
          {
            type: "camera_configure",
            fields: {
              device_id: "cam",
              params: { type: "standard", duration_s: 60 },
            },
            pending: {},
            parameterVariants: [
              {
                paramsText: '{"params":{"type":"standard","duration_s":10}}',
                pending: {},
              },
            ],
          },
        ],
      },
    };
    expect(() => host.app.saveDraft(draft.id, draft.revision, bad)).toThrowError(
      expect.objectContaining({ code: "invalid_content" }),
    );
    expect(host.app.draft(draft.id)).toEqual(draft);
    expect(() => host.app.createDraft(bad)).toThrowError(
      expect.objectContaining({ code: "invalid_content" }),
    );
    expect(host.app.store.all("drafts")).toEqual([draft]);
    expect(host.app.store.all("requests")).toEqual([]);
  });
  const validEntries = [
    { paramsText: '{"params":{"type":"standard"}}', pending: {} },
  ];
  it.each([
    ["非对象映射", null],
    ["数组映射", []],
    ["非数字位置", { bad: validEntries }],
    ["空位置", { "": validEntries }],
    ["带前导零的位置", { "01": validEntries }],
    ["负数位置", { "-1": validEntries }],
    ["超出安全整数的位置", { "9007199254740992": validEntries }],
    ["没有对应动作的位置", { "1": validEntries }],
  ])("拒绝无法归属的顶层参数资料映射：%s", async (_name, parameterVariants) => {
    const host = await setup();
    const draft = await host.create(inactiveStandard());
    const bad = { ...draft.content, parameterVariants };
    const rejected = await host.request<{ code: string }>(
      `/api/drafts/${draft.id}`,
      "PUT",
      { revision: draft.revision, content: bad },
    );
    expect(rejected.status).toBe(400);
    expect(rejected.value.code).toBe("invalid_content");
    expect(await host.read(draft.id)).toEqual(draft);
    const creation = await host.request<{ code: string }>(
      "/api/drafts",
      "POST",
      { content: bad },
    );
    expect(creation.status).toBe(400);
    expect(creation.value.code).toBe("invalid_content");
    expect((await host.state()).drafts).toEqual([draft]);
  });
  const invalid: Array<[string, unknown]> = [
    ["非列表", null],
    ["缺少原文", [{ pending: {} }]],
    ["原文语法错误", [{ paramsText: "{", pending: {} }]],
    ["包装对象为数组", [{ paramsText: "[]", pending: {} }]],
    [
      "包装对象混入其他成员",
      [{ paramsText: '{"params":{"type":"standard"},"other":1}', pending: {} }],
    ],
    [
      "参数原文重复键",
      [
        {
          paramsText: '{"params":{"type":"standard","type":"bitrate"}}',
          pending: {},
        },
      ],
    ],
    [
      "整个参数的未完成输入",
      [
        {
          paramsText: '{"params":{"type":"standard"}}',
          pending: { "": { kind: "json", text: "{" } },
        },
      ],
    ],
    [
      "类型身份的未完成输入",
      [
        {
          paramsText: '{"params":{"type":"standard"}}',
          pending: { "/type": { kind: "json", text: '"' } },
        },
      ],
    ],
    [
      "无法解码的成员路径",
      [
        {
          paramsText: '{"params":{"type":"standard"}}',
          pending: { "/bad~2": { kind: "number", text: "-" } },
        },
      ],
    ],
    [
      "输入种类非法",
      [
        {
          paramsText: '{"params":{"type":"standard"}}',
          pending: { "/duration_s": { kind: "other", text: "-" } },
        },
      ],
    ],
    [
      "重复类型身份",
      [
        { paramsText: '{"params":{"type":"standard","one":1}}', pending: {} },
        { paramsText: '{"params":{"type":"standard","two":2}}', pending: {} },
      ],
    ],
  ];
  it.each(invalid)("拒绝顶层停用参数资料：%s", async (_name, variants) => {
    const host = await setup();
    const draft = await host.create(inactiveStandard());
    const bad = { ...draft.content, parameterVariants: { "0": variants } };
    const rejected = await host.request<{ code: string }>(
      `/api/drafts/${draft.id}`,
      "PUT",
      { revision: draft.revision, content: bad },
    );
    expect(rejected.status).toBe(400);
    expect(rejected.value.code).toBe("invalid_content");
    expect(await host.read(draft.id)).toEqual(draft);
    const creation = await host.request<{ code: string }>(
      "/api/drafts",
      "POST",
      { content: bad },
    );
    expect(creation.status).toBe(400);
    expect(creation.value.code).toBe("invalid_content");
    expect((await host.state()).drafts).toEqual([draft]);
    expect((await host.state()).requests).toEqual([]);
    await host.restart();
    expect(await host.read(draft.id)).toEqual(draft);
  });
  it.each(invalid)(
    "拒绝停用外层动作内的参数资料：%s",
    async (_name, variants) => {
      const host = await setup();
      const draft = await host.create(inactiveStandard());
      const bad = {
        ...draft.content,
        actionVariants: {
          "0": [
            {
              type: "camera_configure",
              fields: {},
              pending: {},
              parameterVariants: variants,
            },
          ],
        },
      };
      const rejected = await host.request<{ code: string }>(
        `/api/drafts/${draft.id}`,
        "PUT",
        { revision: draft.revision, content: bad },
      );
      expect(rejected.status).toBe(400);
      expect(rejected.value.code).toBe("invalid_content");
      expect(await host.read(draft.id)).toEqual(draft);
      expect((await host.state()).requests).toEqual([]);
    },
  );
});

it("复制动作保留独立参数资料，修改副本不改变来源动作", async () => {
  const host = await setup();
  let draft = await host.create(inactiveStandard(true));
  draft = await host.ok<Draft>(
    `/api/drafts/${draft.id}/actions/0/copy`,
    "POST",
    { revision: draft.revision },
  );
  expect(actions(draft.content).map((action) => action.name)).toEqual([
    "拍摄",
    "拍摄 2",
  ]);
  expect(draft.content.parameterVariants?.["1"]).toEqual(
    draft.content.parameterVariants?.["0"],
  );
  const edited = editValue(
    switchParameterType(draft.content, 1, "standard"),
    ["actions", 1, "params", "fine"],
    "8",
    "number",
  );
  draft = await host.save(draft, edited);
  expect(actions(draft.content)[1].params.fine).toBe(8);
  expect(draft.content.pending).toEqual({
    "/actions/1/params/duration_s": { kind: "number", text: "-" },
  });
  expect(storedStandard(draft.content).paramsText).toContain(
    '"fine":1.0000000000000001',
  );
  await host.restart();
  const restored = switchParameterType(
    (await host.read(draft.id)).content,
    0,
    "standard",
  );
  expect(restored.text).toMatch(/"fine"\s*:\s*1\.0000000000000001/);
  expect(actions(restored)[1].params.fine).toBe(8);
  expect(restored.pending).toEqual({
    "/actions/0/params/duration_s": { kind: "number", text: "-" },
    "/actions/1/params/duration_s": { kind: "number", text: "-" },
  });
});

it("自动预览移除及动作删除后参数资料仍归属原动作", async () => {
  const host = await setup();
  let draft = await host.create(
    initializePreviewMetadata(inactiveStandard(), "enabled", "parameter-order"),
  );
  expect(actions(draft.content).map((action) => action.name)).toEqual([
    "拍摄",
    "拍摄预览",
  ]);
  const second = parseClientJson(
    capture(
      "第二拍摄",
      '{"type":"standard","label":"第二动作","fine":1.0000000000000001}',
    ),
  ) as Record<string, unknown>;
  draft = await host.ok<Draft>(`/api/drafts/${draft.id}/actions`, "POST", {
    revision: draft.revision,
    action: second,
  });
  expect(actions(draft.content).map((action) => action.name)).toEqual([
    "拍摄",
    "拍摄预览",
    "第二拍摄",
    "第二拍摄预览",
  ]);
  const pendingSecond = editValue(
    draft.content,
    ["actions", 2, "params", "fine"],
    "-",
    "number",
  );
  draft = await host.save(
    draft,
    switchParameterType(pendingSecond, 2, "bitrate"),
  );
  const secondCache = draft.content.parameterVariants?.["2"];
  expect(secondCache?.[0].pending).toEqual({
    "/fine": { kind: "number", text: "-" },
  });
  await host.reload(false);
  draft = await host.read(draft.id);
  expect(actions(draft.content).map((action) => action.name)).toEqual([
    "拍摄",
    "第二拍摄",
  ]);
  expect(Object.keys(draft.content.parameterVariants ?? {})).toEqual([
    "0",
    "1",
  ]);
  expect(draft.content.parameterVariants?.["1"]).toEqual(secondCache);
  draft = await host.save(draft, removeAction(draft.content, 0));
  expect(Object.keys(draft.content.parameterVariants ?? {})).toEqual(["0"]);
  expect(draft.content.parameterVariants?.["0"]).toEqual(secondCache);
  await host.restart();
  const restored = switchParameterType(
    (await host.read(draft.id)).content,
    0,
    "standard",
  );
  expect(actions(restored).map((action) => action.name)).toEqual(["第二拍摄"]);
  expect(actions(restored)[0].params.label).toBe("第二动作");
  expect(restored.text).toMatch(/"fine"\s*:\s*1\.0000000000000001/);
  expect(restored.pending).toEqual({
    "/actions/0/params/fine": { kind: "number", text: "-" },
  });
});

/** 身份回归只使用真实 Application 和 SQLite，不启动 HTTP 服务。 */
function setupIdentityApplication() {
  const directory = mkdtempSync(join(tmpdir(), "client-parameter-identity-"));
  let app = new Application(directory);
  app.store.initialize();
  cleanup.push(async () => {
    app.store.close();
    const target = resolve(directory);
    if (
      !target.startsWith(resolve(tmpdir()) + sep) ||
      !basename(target).startsWith("client-parameter-identity-")
    )
      throw Error("身份回归临时目录超出清理范围");
    rmSync(target, { recursive: true, force: true });
  });
  return {
    get app() {
      return app;
    },
    reopen() {
      app.store.close();
      app = new Application(directory);
      expect(app.state().startup.state).toBe("ready");
      return app;
    },
  };
}

function saveIdentityAndReopen(
  host: ReturnType<typeof setupIdentityApplication>,
  draft: Draft,
  edited: DraftContent,
): Draft {
  const saved = host.app.saveDraft(draft.id, draft.revision, edited);
  const actual = host.reopen().draft(saved.id);
  expect(actual).toEqual(saved);
  return actual;
}

function outerIdentityContent(
  typeText: string,
  paramsText: string,
): DraftContent {
  const value = content(paramsText);
  return {
    text: value.text.replace('"type":"camera_record"', `"type":${typeText}`),
  };
}

const descendantIdentities = [
  { label: "对象成员", a: { code: "A" }, b: { code: "B" }, path: ["code"] },
  { label: "数组成员", a: ["A"], b: ["B"], path: [0] },
  {
    label: "嵌套数组成员",
    a: { codes: ["A"] },
    b: { codes: ["B"] },
    path: ["codes", 0],
  },
];

describe("身份后代与路径归属的真实保存重开回归", () => {
  it.each(descendantIdentities)(
    "参数身份$label修正回 A 后保存重开恢复 value=7，再返回 B 恢复独立内容",
    ({ a, b, path }) => {
      const host = setupIdentityApplication();
      let draft = host.app.createDraft(
        content(JSON.stringify({ type: a, value: 7 })),
      );
      draft = saveIdentityAndReopen(
        host,
        draft,
        setValue(
          switchParameterType(draft.content, 0, b),
          ["actions", 0, "params", "value"],
          11,
        ),
      );
      draft = saveIdentityAndReopen(
        host,
        draft,
        setValue(draft.content, ["actions", 0, "params", "type", ...path], "A"),
      );
      expect(actions(draft.content)[0].params).toEqual({ type: a, value: 7 });
      expect(draft.content.parameterVariants?.["0"]).toHaveLength(1);
      expect(
        parseClientJson(draft.content.parameterVariants!["0"][0].paramsText),
      ).toEqual({ params: { type: b, value: 11 } });
      draft = saveIdentityAndReopen(
        host,
        draft,
        switchParameterType(draft.content, 0, b),
      );
      expect(actions(draft.content)[0].params).toEqual({ type: b, value: 11 });
    },
  );

  it.each(descendantIdentities.slice(0, 2))(
    "外层身份$label修正回 A 后保存重开，经第三外层往返仍恢复 P/Q 参数缓存",
    ({ a, b, path }) => {
      const host = setupIdentityApplication();
      let draft = host.app.createDraft(
        outerIdentityContent(JSON.stringify(a), '{"type":"P","value":7}'),
      );
      draft = saveIdentityAndReopen(
        host,
        draft,
        setValue(
          switchParameterType(draft.content, 0, "Q"),
          ["actions", 0, "params", "value"],
          9,
        ),
      );
      draft = saveIdentityAndReopen(
        host,
        draft,
        switchActionType(draft.content, 0, b),
      );
      draft = saveIdentityAndReopen(
        host,
        draft,
        setValue(draft.content, ["actions", 0, "type", ...path], "A"),
      );
      expect(actions(draft.content)[0].type).toEqual(a);
      expect(actions(draft.content)[0].params).toEqual({ type: "Q", value: 9 });
      expect(draft.content.parameterVariants?.["0"]).toEqual([
        { paramsText: '{"params":{"type":"P","value":7}}', pending: {} },
      ]);
      draft = saveIdentityAndReopen(
        host,
        draft,
        switchActionType(draft.content, 0, { code: "third" }),
      );
      draft = saveIdentityAndReopen(
        host,
        draft,
        switchActionType(draft.content, 0, a),
      );
      expect(actions(draft.content)[0].params).toEqual({ type: "Q", value: 9 });
      draft = saveIdentityAndReopen(
        host,
        draft,
        switchParameterType(draft.content, 0, "P"),
      );
      expect(actions(draft.content)[0].params).toEqual({ type: "P", value: 7 });
      draft = saveIdentityAndReopen(
        host,
        draft,
        switchParameterType(draft.content, 0, "Q"),
      );
      expect(actions(draft.content)[0].params).toEqual({ type: "Q", value: 9 });
    },
  );

  for (const scope of ["parameter", "action"] as const) {
    it.each([
      { token: "1e-999", projection: 0 },
      { token: "1.0000000000000001", projection: 1 },
    ])(
      `${scope} 身份后代恢复原数 $token 后保存重开保留原分支及数值词元`,
      ({ token, projection }) => {
        const host = setupIdentityApplication();
        const original =
          scope === "parameter"
            ? content(`{"type":{"code":${token}},"value":7}`)
            : outerIdentityContent(
                `{"code":${token}}`,
                '{"type":"P","value":7}',
              );
        let draft = host.app.createDraft(original);
        if (scope === "action")
          draft = saveIdentityAndReopen(
            host,
            draft,
            setValue(
              switchParameterType(draft.content, 0, "Q"),
              ["actions", 0, "params", "value"],
              9,
            ),
          );
        draft = saveIdentityAndReopen(
          host,
          draft,
          scope === "parameter"
            ? switchParameterType(draft.content, 0, { code: projection })
            : switchActionType(draft.content, 0, { code: projection }),
        );
        const path =
          scope === "parameter"
            ? ["actions", 0, "params", "type", "code"]
            : ["actions", 0, "type", "code"];
        draft = saveIdentityAndReopen(
          host,
          draft,
          editValue(draft.content, path, token, "number"),
        );
        expect(draft.content.text).toMatch(
          new RegExp(
            `"code"\\s*:\\s*${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`,
          ),
        );
        if (scope === "parameter")
          expect(actions(draft.content)[0].params.value).toBe(7);
        else {
          expect(actions(draft.content)[0].params).toEqual({
            type: "Q",
            value: 9,
          });
          draft = saveIdentityAndReopen(
            host,
            draft,
            switchParameterType(draft.content, 0, "P"),
          );
          expect(actions(draft.content)[0].params).toEqual({
            type: "P",
            value: 7,
          });
        }
      },
    );
  }

  for (const scope of ["parameter", "action"] as const) {
    const setupPendingIdentity = () => {
      const host = setupIdentityApplication();
      const original =
        scope === "parameter"
          ? content('{"type":{"code":"A","extra":1},"value":7}')
          : outerIdentityContent(
              '{"code":"A","extra":1}',
              '{"type":"P","value":7}',
            );
      let draft = host.app.createDraft(original);
      if (scope === "action")
        draft = saveIdentityAndReopen(
          host,
          draft,
          setValue(
            switchParameterType(draft.content, 0, "Q"),
            ["actions", 0, "params", "value"],
            9,
          ),
        );
      draft = saveIdentityAndReopen(
        host,
        draft,
        scope === "parameter"
          ? switchParameterType(draft.content, 0, { code: "B", extra: 1 })
          : switchActionType(draft.content, 0, { code: "B", extra: 1 }),
      );
      const path =
        scope === "parameter"
          ? ["actions", 0, "params", "type", "code"]
          : ["actions", 0, "type", "code"];
      return { host, draft, path };
    };
    it(`${scope} 身份后代自身输入完成后保存重开消费原文并恢复原内容`, () => {
      const initial = setupPendingIdentity();
      let draft = saveIdentityAndReopen(
        initial.host,
        initial.draft,
        editValue(initial.draft.content, initial.path, '"A', "json"),
      );
      expect(draft.content.pending?.["/" + initial.path.join("/")]).toEqual({
        kind: "json",
        text: '"A',
      });
      draft = saveIdentityAndReopen(
        initial.host,
        draft,
        editValue(draft.content, initial.path, '"A"', "json"),
      );
      expect(draft.content.pending ?? {}).toEqual({});
      if (scope === "parameter")
        expect(actions(draft.content)[0].params).toEqual({
          type: { code: "A", extra: 1 },
          value: 7,
        });
      else {
        expect(actions(draft.content)[0].type).toEqual({ code: "A", extra: 1 });
        expect(actions(draft.content)[0].params).toEqual({
          type: "Q",
          value: 9,
        });
        draft = saveIdentityAndReopen(
          initial.host,
          draft,
          switchParameterType(draft.content, 0, "P"),
        );
        expect(actions(draft.content)[0].params).toEqual({
          type: "P",
          value: 7,
        });
      }
    });
    it(`${scope} 身份兄弟输入未完成时拒绝后代修正，保存重开保持完整原状态`, () => {
      const initial = setupPendingIdentity();
      const siblingPath = [...initial.path.slice(0, -1), "extra"];
      const draft = saveIdentityAndReopen(
        initial.host,
        initial.draft,
        editValue(initial.draft.content, siblingPath, "-", "number"),
      );
      const before = parseClientJson(stringifyJson(draft.content));
      expect(() =>
        editValue(draft.content, initial.path, '"A"', "json"),
      ).toThrow();
      expect(draft.content).toEqual(before);
      expect(initial.host.app.draft(draft.id)).toEqual(draft);
      expect(initial.host.reopen().draft(draft.id)).toEqual(draft);
    });
  }

  for (const operation of ["copy", "remove", "action-type"] as const) {
    it.each(["/actions/01/params/ghost", "/actions/0/params/ghost~2"])(
      `${operation} 不搬移无法归属的原文 %s，真实草稿保存重开保持原状态`,
      (pendingPath) => {
        const host = setupIdentityApplication();
        const original: DraftContent = {
          text: `{"name":"路径计划","actions":[${capture("第一", '{"type":{"code":"A"},"value":7}')},${capture("第二", '{"type":{"code":"A"},"value":11}')} ]}`,
        };
        let draft = host.app.createDraft(
          switchParameterType(
            switchParameterType(original, 0, { code: "B" }),
            1,
            { code: "B" },
          ),
        );
        draft = saveIdentityAndReopen(host, draft, {
          ...draft.content,
          pending: { [pendingPath]: { kind: "json", text: "{" } },
        });
        const before = parseClientJson(stringifyJson(draft.content));
        expect(() => {
          if (operation === "copy")
            host.app.copyAction(draft.id, draft.revision, 0);
          else if (operation === "remove") removeAction(draft.content, 0);
          else switchActionType(draft.content, 0, "report_status");
        }).toThrow();
        expect(draft.content).toEqual(before);
        expect(host.app.draft(draft.id)).toEqual(draft);
        expect(host.reopen().draft(draft.id)).toEqual(draft);
      },
    );
  }
});

/** 自动预览协调回归复用能力目录与直接 Application/SQLite 生命周期。 */
function setupPreviewCoordinationApplication(previewSupported: boolean) {
  const host = setupIdentityApplication();
  writeFileSync(
    join(host.app.store.directory, "device-capabilities.json"),
    JSON.stringify(catalog(previewSupported)),
  );
  const loaded = host.app.reloadCapabilities();
  expect(loaded.error).toBeNull();
  expect(loaded.active).toEqual(catalog(previewSupported));
  return host;
}

function previewCoordinationContent(
  intent: "enabled" | "disabled",
  pendingPath: string,
): DraftContent {
  const original = inactiveStandard(true);
  const automatic = {
    name: "拍摄预览",
    type: "obtain_action_outputs",
    scheduled_at: "2026-10-10 01:00:00",
    params: {
      source: { action_name: "拍摄" },
      filter: "preview",
      purpose: "auto_preview",
    },
  };
  return initializePreviewMetadata(
    {
      text: `{"name":"参数计划","actions":[{"name":"同步","type":"report_status","params":{"scope":"full"}},${capture("拍摄", JSON.stringify(bitrateParams))},${JSON.stringify(automatic)}]}`,
      pending: { [pendingPath]: { kind: "json", text: "{" } },
      parameterVariants: { "1": original.parameterVariants!["0"] },
      actionVariants: {
        "1": [
          {
            type: "camera_configure",
            fields: { device_id: "cam", params: { type: "Q", value: 9 } },
            pending: { "/params/value": { kind: "number", text: "-" } },
            parameterVariants: [
              {
                paramsText:
                  '{"params":{"type":"P","value":7,"nested":{"deep":1e-999}}}',
                pending: { "/value": { kind: "number", text: "-" } },
              },
            ],
          },
        ],
      },
    },
    intent,
    "coordination-pause",
  );
}

function expectUnfinishedPreviewExport(
  host: ReturnType<typeof setupIdentityApplication>,
  draft: Draft,
) {
  expect(() =>
    host.app.exportDraft(
      draft.id,
      draft.revision,
      draft.content,
      host.app.capabilities.version,
    ),
  ).toThrowError(expect.objectContaining({ code: "unfinished_input" }));
  expect(host.app.draft(draft.id)).toEqual(draft);
  expect(host.app.store.all("requests")).toEqual([]);
}

describe("预览协调暂停的真实保存重开回归", () => {
  for (const intent of ["disabled", "enabled"] as const) {
    it.each(["/actions/01/params/ghost", "/actions/0/params/ghost~2"])(
      `${intent} 需要移除预览但路径 %s 未确定时创建并重开完整草稿`,
      (pendingPath) => {
        const host = setupPreviewCoordinationApplication(false);
        const input = previewCoordinationContent(intent, pendingPath);
        const before = parseClientJson(stringifyJson(input));
        const draft = host.app.createDraft(input);
        const namespace = draft.content.automaticPreviews!.namespace;
        expect(namespace).not.toBe(input.automaticPreviews!.namespace);
        expect(draft.content).toEqual({
          ...input,
          automaticPreviews: {
            intent,
            namespace,
            next: 3,
            actions: [
              { id: `${namespace}:0` },
              { id: `${namespace}:1` },
              { id: `${namespace}:2`, sourceId: `${namespace}:1` },
            ],
          },
        });
        expect(input).toEqual(before);
        expect(actions(draft.content).map((action) => action.name)).toEqual([
          "同步",
          "拍摄",
          "拍摄预览",
        ]);
        expect(host.reopen().draft(draft.id)).toEqual(draft);
        expect(host.app.capabilities.error).toBeNull();
        expectUnfinishedPreviewExport(host, draft);
      },
    );
  }

  it.each(["/actions/01/params/ghost", "/actions/0/params/ghost~2"])(
    "关闭预览但路径 %s 未确定时保存并重开完整原文和两层缓存",
    (pendingPath) => {
      const host = setupPreviewCoordinationApplication(true);
      const initial = previewCoordinationContent("enabled", pendingPath);
      const draft = host.app.createDraft({ ...initial, pending: {} });
      const input: DraftContent = {
        ...draft.content,
        pending: initial.pending,
        automaticPreviews: {
          ...draft.content.automaticPreviews!,
          intent: "disabled",
        },
      };
      const before = parseClientJson(stringifyJson(input));
      const saved = saveIdentityAndReopen(host, draft, input);
      expect(saved.content).toEqual(input);
      expect(saved.lastWrite).toEqual({
        revision: draft.revision + 1,
        input,
        content: input,
      });
      expect(input).toEqual(before);
      expect(actions(saved.content).map((action) => action.name)).toEqual([
        "同步",
        "拍摄",
        "拍摄预览",
      ]);
      expectUnfinishedPreviewExport(host, saved);
    },
  );

  it.each(["/actions/01/params/ghost", "/actions/0/params/ghost~2"])(
    "启用预览且路径 %s 未确定时能力刷新仍有效，暂停移除并保存重开完整内容",
    (pendingPath) => {
      const host = setupPreviewCoordinationApplication(true);
      const initial = previewCoordinationContent("enabled", pendingPath);
      let draft = host.app.createDraft({ ...initial, pending: {} });
      draft = host.app.saveDraft(draft.id, draft.revision, {
        ...draft.content,
        pending: initial.pending,
      });
      const before = parseClientJson(stringifyJson(draft));
      const oldCapabilityVersion = host.app.capabilities.version;
      writeFileSync(
        join(host.app.store.directory, "device-capabilities.json"),
        JSON.stringify(catalog(false)),
      );
      const loaded = host.app.reloadCapabilities();
      expect(loaded.error).toBeNull();
      expect(loaded.active).toEqual(catalog(false));
      expect(loaded.version).not.toBe(oldCapabilityVersion);
      expect(host.app.draft(draft.id)).toEqual(before);
      expect(host.reopen().draft(draft.id)).toEqual(before);
      expect(host.app.capabilities.error).toBeNull();
      expect(host.app.capabilities.active).toEqual(catalog(false));
      draft = saveIdentityAndReopen(host, draft, draft.content);
      expect(draft.content).toEqual((before as Draft).content);
      expect(draft.content.automaticPreviews?.intent).toBe("enabled");
      expect(actions(draft.content).map((action) => action.name)).toEqual([
        "同步",
        "拍摄",
        "拍摄预览",
      ]);
      expectUnfinishedPreviewExport(host, draft);
    },
  );
});
