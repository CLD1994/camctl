import { afterEach, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Server } from "node:http";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { loadCapabilities } from "../../src/shared/capabilities";
import {
  parseJson,
  parseClientJson,
  stringifyJson,
  cloneClientJson,
} from "../../src/shared/json";
import { validatePlan } from "../../src/shared/plan";
import { validateParams } from "../../src/shared/capabilities";
import { createValidator } from "../../src/shared/validation";
import { setValue } from "../../src/web/editing";
import type {
  Draft,
  DraftContent,
  ExportedRequest,
  Preset,
} from "../../src/server/models";
import { reportInput, mappedReport } from "./fixtures";
import { switchActionType } from "../../src/web/action-drafts";
import { DraftSession } from "../../src/web/session";
import {
  copyDraftAction,
  coordinatePreviews,
  initializePreviewMetadata,
  previewIntent,
} from "../../src/shared/automatic-previews";

const clean: Array<() => void | Promise<void>> = [];
afterEach(async () => {
  vi.restoreAllMocks();
  for (const f of clean.splice(0).reverse()) await f();
});
const time = "2026-10-10 00:00:00";
const rounded = "1.0000000000000001";
function setup(rule: object = { type: "integer" }) {
  const dir = mkdtempSync(join(tmpdir(), "camctl-input-"));
  clean.push(() => rmSync(dir, { recursive: true, force: true }));
  const app = new Application(dir, { next: () => 9223372036854775807n });
  app.store.initialize();
  clean.push(() => app.store.close());
  app.capabilities.active = loadCapabilities({
    devices: [
      {
        device_id: "cam",
        driver_id: "demo",
        actions: [
          {
            type: "camera_record",
            parameter_types: [
              {
                type: "fixed",
                name: "固定",
                description: "测试",
                preview_supported: true,
                schema: {
                  $schema: "https://json-schema.org/draft/2020-12/schema",
                  type: "object",
                  required: ["type"],
                  properties: {
                    type: { const: "fixed" },
                    value: rule,
                    note: { type: "string" },
                  },
                  additionalProperties: false,
                },
              },
            ],
          },
        ],
      },
    ],
  });
  app.capabilities.error = null;
  return app;
}
function content(token = "0") {
  return {
    text: `{"name":"计划","actions":[{"name":"拍摄","type":"camera_record","device_id":"cam","scheduled_at":"${time}","params":{"type":"fixed","value":${token}},"policy":{"max_delay_ms":0}}]}`,
  };
}
it.each([
  content(rounded),
  { text: '{"name":"\\ud800","actions":[]}' },
  {
    text: JSON.stringify({
      name: "计划",
      actions: [
        {
          name: "清理",
          type: "delete_action_outputs",
          scheduled_at: time,
          params: { source: { action_name: "无" } },
        },
      ],
    }),
  },
  {
    text: JSON.stringify({
      name: "计划",
      actions: [
        JSON.parse(content().text).actions[0],
        ...["甲", "乙"].map((name) => ({
          name,
          type: "obtain_action_outputs",
          scheduled_at: time,
          params: {
            source: { action_name: "拍摄" },
            filter: "preview",
            purpose: "auto_preview",
          },
        })),
      ],
    }),
  },
])("后端拒绝非法完整输入且不落原请求 %#", (value) => {
  const app = setup();
  const draft = app.createDraft(value);
  expect(() => app.validateContent(value)).toThrow();
  expect(() => app.exportDraft(draft.id, 1, value)).toThrow();
  expect(app.store.all("requests")).toEqual([]);
  expect(app.draft(draft.id)).toEqual(draft);
});
it("后端追加保留已有非法整数的原词元", () => {
  const app = setup();
  const draft = app.createDraft(content(rounded));
  const appended = app.appendAction(draft.id, 1, {
    name: "报告",
    type: "report_status",
    params: { scope: "full" },
  });
  expect(appended.content.text).toContain(rounded);
  expect(() =>
    app.exportDraft(draft.id, appended.revision, appended.content),
  ).toThrow();
  expect(app.store.all("requests")).toEqual([]);
});
it("合法整数表示和最大请求身份可以下载", () => {
  const app = setup();
  const value = content("1e0");
  const draft = app.createDraft(value);
  const exported = app.exportDraft(draft.id, 1, value);
  expect(exported.id).toBe("9223372036854775807");
  expect(app.downloadRequest(exported.id).request_id).toBe(
    "9223372036854775807",
  );
});
it("类型原文保存重启后切回仍阻止非法整数导出", () => {
  const app = setup();
  const switched = switchActionType(content(rounded), 0, "report_status");
  const draft = app.createDraft(switched);
  app.store.close();
  const reopened = new Application(app.store.directory);
  clean.push(() => reopened.store.close());
  reopened.capabilities.active = app.capabilities.active;
  const restored = switchActionType(
    reopened.draft(draft.id).content,
    0,
    "camera_record",
  );
  expect(restored.text).toContain(rounded);
  expect(() => reopened.exportDraft(draft.id, 1, restored)).toThrow();
  expect(reopened.store.all("requests")).toEqual([]);
});
it.each(["{", '{"params":{"type":"different"}}', '{"name":"越界"}', "[]"])(
  "类型原文无效或与字段投影矛盾时拒绝 %s",
  (fieldsText) => {
    const app = setup();
    expect(() =>
      app.createDraft({
        ...content(),
        actionVariants: {
          "0": [
            {
              type: "camera_record",
              fields: { params: { type: "fixed" } },
              fieldsText,
              pending: {},
            },
          ],
        },
      }),
    ).toThrow();
  },
);
it("没有原文的旧类型资料仍可以恢复", () => {
  const app = setup();
  const draft = app.createDraft({
    text: '{"name":"计划","actions":[{"name":"拍摄","type":"report_status"}]}',
    actionVariants: {
      "0": [
        {
          type: "camera_record",
          fields: { params: { type: "fixed", value: 1 } },
          pending: {},
        },
      ],
    },
  });
  expect(
    JSON.parse(
      switchActionType(app.draft(draft.id).content, 0, "camera_record").text,
    ).actions[0].params.value,
  ).toBe(1);
});
it("负零原文与 JSON 保存后的零投影兼容", () => {
  const app = setup();
  const switched = switchActionType(content("-0"), 0, "report_status");
  const transported = JSON.parse(JSON.stringify(switched));
  expect(() => app.createDraft(transported)).not.toThrow();
});
it("私有草稿正文与未完成输入中的代理码元可保存重开，完整导出仍拒绝", () => {
  const app = setup();
  const value = {
    text: '{"name":"\ud800","actions":[]}',
    pending: {
      "/actions/0/params": { kind: "json" as const, text: '"\udc00' },
    },
  };
  const draft = app.createDraft(value);
  expect(app.draft(draft.id).content).toEqual(value);
  expect(app.store.all("drafts")).toEqual([draft]);
  app.store.close();
  const reopened = new Application(app.store.directory);
  clean.push(() => reopened.store.close());
  expect(reopened.draft(draft.id).content).toEqual(value);
  expect(() => reopened.exportDraft(draft.id, 1, value)).toThrowError(
    expect.objectContaining({ code: "unfinished_input" }),
  );
  expect(() => reopened.validateContent({ text: value.text })).toThrowError(
    expect.objectContaining({ code: "invalid_json" }),
  );
  expect(reopened.store.all("requests")).toEqual([]);
});
it("非当前类型资料允许业务字符串非法，恢复后公共计划拒绝", () => {
  const app = setup();
  const fields = { params: { type: "fixed", invalid: "\ud800" } };
  const fieldsText = JSON.stringify(fields);
  const draft = app.createDraft({
    text: '{"name":"计划","actions":[{"name":"拍摄","type":"report_status"}]}',
    actionVariants: {
      "0": [{ type: "camera_record", fields, fieldsText, pending: {} }],
    },
  });
  const recovered = app.draft(draft.id);
  const restored = switchActionType(recovered.content, 0, "camera_record");
  expect(JSON.parse(restored.text).actions[0].params.invalid).toBe("\ud800");
  expect(() => app.validateContent(restored)).toThrowError(
    expect.objectContaining({ code: "invalid_json" }),
  );
});
it("HTTP 接受包含非法编辑原文的私有草稿并完整返回状态", async () => {
  const app = setup();
  const files = new Files(app);
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(app, files).listen(0, "127.0.0.1", () =>
      resolve(s),
    );
  });
  clean.push(async () => {
    await files.idle();
    await new Promise<void>((resolve, reject) =>
      server.close((e) => (e ? reject(e) : resolve())),
    );
  });
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const value = {
    text: '{"name":"\ud800","actions":[]}',
    pending: { "/actions": { kind: "json", text: "\udc00" } },
  };
  const response = await fetch(`${base}/api/drafts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: value }),
  });
  expect(response.status).toBe(201);
  const draft = await response.json();
  expect(draft.content).toEqual(value);
  const state = await (await fetch(`${base}/api/state`)).json();
  expect(state.drafts[0].content).toEqual(value);
  const exported = await fetch(`${base}/api/drafts/${draft.id}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      content: value,
      revision: 1,
      capabilityVersion: app.capabilities.version,
    }),
  });
  expect(exported.status).toBe(400);
  expect(app.store.all("requests")).toEqual([]);
});
it("直接 HTTP 预设入口按原词元验证整数，合法表示可保存", async () => {
  const app = setup();
  const files = new Files(app);
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(app, files).listen(0, "127.0.0.1", () =>
      resolve(s),
    );
  });
  clean.push(async () => {
    await files.idle();
    await new Promise<void>((resolve, reject) =>
      server.close((e) => (e ? reject(e) : resolve())),
    );
  });
  const url = `http://127.0.0.1:${(server.address() as { port: number }).port}/api/presets`;
  const send = (token: string) =>
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: `{"name":"预设","deviceId":"cam","actionType":"camera_record","params":{"type":"fixed","value":${token}}}`,
    });
  expect((await send(rounded)).status).toBe(400);
  expect(app.store.all("presets")).toEqual([]);
  const invalidString = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: "预设",
      deviceId: "cam",
      actionType: "camera_record",
      params: { type: "fixed", note: "\ud800" },
    }),
  });
  expect(invalidString.status).toBe(400);
  expect(app.store.all("presets")).toEqual([]);
  expect((await send("1.0")).status).toBe(201);
});

async function http(app: Application) {
  const files = new Files(app);
  const server = createHttpApp(app, files).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  clean.push(async () => {
    await files.idle();
    await new Promise<void>((resolve, reject) =>
      server.close((e) => (e ? reject(e) : resolve())),
    );
  });
  return `http://127.0.0.1:${(server.address() as { port: number }).port}/api`;
}
function reopen(app: Application) {
  app.store.close();
  const next = new Application(app.store.directory, { next: () => 2n });
  next.capabilities = { ...app.capabilities };
  clean.push(() => next.store.close());
  return next;
}
const nonintegerRules = [
  { not: { type: "integer" } },
  { oneOf: [{ type: "integer" }, { type: "number" }] },
  { if: { type: "integer" }, then: false, else: { type: "number" } },
  { type: "number" },
  { type: ["integer", "number"] },
  { anyOf: [{ type: "integer" }, { type: "number" }] },
];
function noninteger(params: unknown, app: Application) {
  expect(
    validateParams("cam", "camera_record", params, app.capabilities.active),
  ).toEqual([]);
  const check = createValidator().compile({
    type: "object",
    properties: { value: { not: { type: "integer" } } },
  });
  expect(check(params)).toBe(true);
  expect(typeof (params as { value: unknown }).value).toBe("number");
}
it.each(
  nonintegerRules.flatMap((rule) =>
    [rounded, "-1e-400", "1.5"].map((token) => ({ rule, token })),
  ),
)("固定请求保存重开与复制保留数值分支 %#", ({ rule, token }) => {
  let app = setup(rule);
  const draft = app.createDraft(content(token));
  const request = app.exportDraft(draft.id, 1, draft.content);
  app = reopen(app);
  app.markHandoff(request.id, true);
  const downloaded = app.downloadRequest(request.id);
  expect(validatePlan(downloaded, app.capabilities.active)).toEqual([]);
  noninteger((downloaded.actions as any[])[0].params, app);
  const copy = app.copyRequest(request.id);
  app = reopen(app);
  const next = app.exportDraft(
    copy.id,
    copy.revision,
    app.draft(copy.id).content,
  );
  expect(next.id).not.toBe(request.id);
  noninteger((app.downloadRequest(next.id).actions as any[])[0].params, app);
});
it("HTTP 固定正文在递交、ACK 更新和状态响应中保持同一数值事实", async () => {
  const app = setup(nonintegerRules[0]);
  const base = await http(app);
  const draft = app.createDraft(content(rounded));
  const response = await fetch(`${base}/drafts/${draft.id}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      revision: 1,
      content: draft.content,
      capabilityVersion: app.capabilities.version,
    }),
  });
  const request = parseClientJson(await response.text()) as ExportedRequest;
  noninteger((request.body.actions as any[])[0].params, app);
  const first = parseJson(
    await (await fetch(`${base}/requests/${request.id}/download`)).text(),
  ) as any;
  expect(validatePlan(first, app.capabilities.active)).toEqual([]);
  app.applyReports([reportInput(mappedReport(Buffer.from("ack")))]);
  const marked = await fetch(`${base}/requests/${request.id}/handoff`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: '{"marked":true}',
  });
  noninteger(
    (
      (parseClientJson(await marked.text()) as ExportedRequest).body
        .actions as any[]
    )[0].params,
    app,
  );
  const second = parseJson(
    await (await fetch(`${base}/requests/${request.id}/download`)).text(),
  ) as any;
  expect(second.last_report_id).toBe("1");
  delete second.last_report_id;
  expect(stringifyJson(second)).toBe(stringifyJson(first));
  const state = parseClientJson(
    await (await fetch(`${base}/state`)).text(),
  ) as any;
  noninteger(state.requests[0].body.actions[0].params, app);
});
it("HTTP 预设创建更新响应和重开状态保留分支，非法更新不改变已存参数", async () => {
  let app = setup(nonintegerRules[1]);
  let base = await http(app);
  const send = (token: string, id?: string) =>
    fetch(`${base}/presets${id ? "/" + id : ""}`, {
      method: id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: `{"name":"预设","deviceId":"cam","actionType":"camera_record","params":{"type":"fixed","value":${token}}}`,
    });
  const created = await send(rounded);
  expect(created.status).toBe(201);
  const preset = parseClientJson(await created.text()) as Preset;
  noninteger(preset.params, app);
  const updated = await send("-1e-400", preset.id);
  expect(updated.status).toBe(200);
  noninteger((parseClientJson(await updated.text()) as Preset).params, app);
  const saved = stringifyJson(app.store.get("presets", preset.id));
  for (const token of ["1", '"\\ud800"', "true", "null"]) {
    expect((await send(token, preset.id)).status).toBe(400);
    expect(stringifyJson(app.store.get("presets", preset.id))).toBe(saved);
  }
  app = reopen(app);
  base = await http(app);
  const state = parseClientJson(
    await (await fetch(`${base}/state`)).text(),
  ) as any;
  noninteger(state.presets[0].params, app);
  const applied = setValue(
    content(),
    ["actions", 0, "params"],
    state.presets[0].params,
  );
  expect(() => app.validateContent(applied)).not.toThrow();
  noninteger((parseJson(applied.text) as any).actions[0].params, app);
});
it.each(["requests", "drafts"])(
  "固定导出 %s 写入失败回滚请求与草稿",
  (namespace) => {
    const app = setup(nonintegerRules[0]);
    const draft = app.createDraft(content(rounded));
    const set = app.store.set.bind(app.store);
    vi.spyOn(app.store, "set").mockImplementation((kind, id, value) => {
      if (kind === namespace) throw new Error("write rejected");
      return set(kind, id, value);
    });
    expect(() => app.exportDraft(draft.id, 1, draft.content)).toThrow(
      "write rejected",
    );
    expect(app.store.all("requests")).toEqual([]);
    expect(app.draft(draft.id)).toEqual(draft);
  },
);
it("预设写入失败保留已有预设且不留下新预设", () => {
  const app = setup(nonintegerRules[0]);
  const input = {
    name: "预设",
    deviceId: "cam",
    actionType: "camera_record",
    params: parseJson(`{"type":"fixed","value":${rounded}}`),
  };
  const preset = app.savePreset(input);
  const prior = stringifyJson(app.store.all("presets"));
  vi.spyOn(app.store, "set").mockImplementation(() => {
    throw new Error("write rejected");
  });
  expect(() => app.savePreset({ ...input, id: preset.id })).toThrow(
    "write rejected",
  );
  expect(() => app.savePreset(input)).toThrow("write rejected");
  expect(stringifyJson(app.store.all("presets"))).toBe(prior);
});

function httpSession(draft: Draft, base: string) {
  return new DraftSession(
    draft,
    {
      save: async (id, revision, value) => {
        const response = await fetch(`${base}/drafts/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: stringifyJson({ revision, content: value }),
        });
        const result = parseClientJson(await response.text());
        if (!response.ok) throw Error((result as { message: string }).message);
        return result as Draft;
      },
      read: async (id) => {
        const state = parseClientJson(
          await (await fetch(`${base}/state`)).text(),
        ) as { drafts: Draft[] };
        return state.drafts.find((item) => item.id === id)!;
      },
    },
    () => {},
  );
}
it.each(["1.0000000000000001", "1e-999"])(
  "类型直属数值%s随Session/HTTP/SQLite保存重开及恢复，停用非法不阻当前导出",
  async (token) => {
    let app = setup(),
      base = await http(app);
    const original: DraftContent = {
      text: `{"name":"计划","actions":[{"name":"拍摄","type":"camera_record","scheduled_at":"${time}","params":${token},"extra":${token}}]}`,
    };
    const draft = app.createDraft(original),
      session = httpSession(draft, base);
    const switched = setValue(
      switchActionType(draft.content, 0, "report_status"),
      ["actions", 0, "params"],
      { scope: "full" },
    );
    session.edit(switched);
    await session.flush();
    expect(session.saved).toBe(true);
    expect(app.draft(draft.id).revision).toBe(2);
    expect(app.draft(draft.id).content.actionVariants!["0"][0].fieldsText).toBe(
      `{"params":${token},"extra":${token}}`,
    );
    expect(app.draft(draft.id).lastWrite!.input).toEqual(switched);
    app = reopen(app);
    base = await http(app);
    const state = parseClientJson(
      await (await fetch(`${base}/state`)).text(),
    ) as { drafts: Draft[] };
    const recovered = state.drafts.find((item) => item.id === draft.id)!;
    expect(
      stringifyJson(recovered.content.actionVariants!["0"][0].fields),
    ).toBe(`{"params":${token},"extra":${token}}`);
    const exported = await fetch(`${base}/drafts/${draft.id}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: stringifyJson({
        revision: recovered.revision,
        content: recovered.content,
        capabilityVersion: app.capabilities.version,
      }),
    });
    expect(exported.status).toBe(200);
    const request = parseClientJson(await exported.text()) as ExportedRequest;
    expect(request.body.actions).toEqual([
      {
        name: "拍摄",
        type: "report_status",
        scheduled_at: time,
        params: { scope: "full" },
      },
    ]);
    const copy = app.copyDraft(draft.id),
      restored = switchActionType(copy.content, 0, "camera_record");
    expect(restored.text).toContain(`"params": ${token}`);
    expect(restored.text).toContain(`"extra": ${token}`);
    const restoredSession = httpSession(copy, base);
    restoredSession.edit(restored);
    await restoredSession.flush();
    const denied = await fetch(`${base}/drafts/${copy.id}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: stringifyJson({
        revision: restoredSession.revision,
        content: restoredSession.content,
        capabilityVersion: app.capabilities.version,
      }),
    });
    expect(denied.status).toBe(400);
    expect(
      (parseClientJson(await denied.text()) as { code: string }).code,
    ).toBe("invalid_plan");
    expect(app.store.all("requests")).toHaveLength(1);
    app = reopen(app);
    expect(app.draft(copy.id).content.text).toContain(token);
    expect(app.draft(draft.id).content.actionVariants!["0"][0].fieldsText).toBe(
      `{"params":${token},"extra":${token}}`,
    );
  },
);
it.each([true, false])(
  "已有停用类型原文%s在HTTP保存及SQLite重开后恢复直属字段",
  async (hasText) => {
    let app = setup(),
      base = await http(app);
    const input: DraftContent = {
      text: `{"name":"计划","actions":[{"name":"A","type":"report_status","scheduled_at":"${time}","params":{"scope":"full"}}]}`,
      actionVariants: {
        "0": [
          {
            type: "camera_record",
            fields: { params: 1, extra: 0 },
            ...(hasText
              ? { fieldsText: '{"params":1.0000000000000001,"extra":1e-999}' }
              : {}),
            pending: {},
          },
        ],
      },
    };
    const draft = app.createDraft(),
      session = httpSession(draft, base);
    session.edit(input);
    await session.flush();
    app = reopen(app);
    base = await http(app);
    const state = parseClientJson(
      await (await fetch(`${base}/state`)).text(),
    ) as { drafts: Draft[] };
    const recovered = state.drafts.find((item) => item.id === draft.id)!;
    const restored = switchActionType(recovered.content, 0, "camera_record");
    expect(restored.text).toContain(
      hasText ? '"params": 1.0000000000000001' : '"params": 1',
    );
    expect(restored.text).toContain(hasText ? '"extra": 1e-999' : '"extra": 0');
    if (!hasText) {
      expect(restored.text).not.toContain("1.0000000000000001");
      expect(restored.text).not.toContain("1e-999");
    }
    expect(recovered.content.actionVariants!["0"][0].fieldsText).toBe(
      input.actionVariants!["0"][0].fieldsText,
    );
  },
);
it("复制动作和能力协调不会丢停用类型的直属字段或type数字证据", () => {
  const app = setup();
  const original = {
    text: `{"name":"计划","actions":[{"name":"A","type":1e-999,"scheduled_at":"${time}","params":1.0000000000000001}]}`,
  };
  const switched = switchActionType(original, 0, "report_status");
  const tagged = initializePreviewMetadata(switched, "disabled", "n");
  const copied = copyDraftAction(tagged, 0);
  const coordinated = coordinatePreviews(copied, app.capabilities).content;
  expect(previewIntent(coordinated)).toBe("disabled");
  for (const index of ["0", "1"]) {
    const saved = coordinated.actionVariants![index][0];
    expect(stringifyJson(saved)).toContain('"type":1e-999');
    expect(saved.fieldsText).toBe('{"params":1.0000000000000001}');
  }
});
it("能力移除中间自动项时停用资料移位仍保留直属字段和type数字事实", () => {
  const app = setup();
  const inactive = switchActionType(
    {
      text: '{"actions":[{"name":"B","type":1e-999,"params":1.0000000000000001}]}',
    },
    0,
    "report_status",
  );
  const source = (parseClientJson(content().text) as { actions: object[] })
    .actions[0];
  const input: DraftContent = {
    text: stringifyJson({
      name: "计划",
      actions: [source, { name: "B", type: "report_status" }],
    }),
    actionVariants: { "1": inactive.actionVariants!["0"] },
  };
  const coordinated = coordinatePreviews(
    initializePreviewMetadata(input, "enabled", "n"),
    app.capabilities,
  ).content;
  const root = parseClientJson(coordinated.text) as { actions: object[] };
  root.actions = [root.actions[0], root.actions[2], root.actions[1]];
  coordinated.text = stringifyJson(root);
  const ids = coordinated.automaticPreviews!.actions;
  coordinated.automaticPreviews!.actions = [ids[0], ids[2], ids[1]];
  coordinated.actionVariants = { "2": coordinated.actionVariants!["1"] };
  const unsupported = cloneClientJson(app.capabilities);
  unsupported.active!.devices[0].actions[0].parameter_types[0].preview_supported = false;
  const next = coordinatePreviews(coordinated, unsupported).content;
  expect(
    (parseClientJson(next.text) as { actions: { name: string }[] }).actions.map(
      (a) => a.name,
    ),
  ).toEqual(["拍摄", "B"]);
  expect(Object.keys(next.actionVariants!)).toEqual(["1"]);
  expect(stringifyJson(next.actionVariants!["1"][0])).toContain(
    '"type":1e-999',
  );
  expect(next.actionVariants!["1"][0].fieldsText).toBe(
    '{"params":1.0000000000000001}',
  );
  expect(previewIntent(next)).toBe("enabled");
});
it("类型切换保存的数据库写入失败保留旧完整记录及页面输入", async () => {
  const app = setup(),
    base = await http(app);
  const original = {
    text: `{"name":"计划","actions":[{"name":"A","type":"camera_record","params":1e-999}]}`,
  };
  const draft = app.createDraft(original),
    session = httpSession(draft, base),
    next = switchActionType(draft.content, 0, "report_status");
  const before = stringifyJson(app.draft(draft.id)),
    set = app.store.set.bind(app.store);
  vi.spyOn(app.store, "set").mockImplementation((kind, id, value) => {
    if (kind === "drafts") throw Error("write rejected");
    return set(kind, id, value);
  });
  session.edit(next);
  await expect(session.flush()).rejects.toThrow();
  expect(session.saved).toBe(false);
  expect(session.content).toEqual(next);
  expect(session.content.actionVariants!["0"][0].fieldsText).toBe(
    '{"params":1e-999}',
  );
  expect(stringifyJson(app.draft(draft.id))).toBe(before);
  expect(app.store.all("requests")).toEqual([]);
});
it("停用type的原词元经HTTP和SQLite重开后仍区分裸零，复制保持旧候选", async () => {
  let app = setup(),
    base = await http(app);
  const draft = app.createDraft({
    text: `{"name":"计划","actions":[{"name":"A","type":1e-999,"scheduled_at":"${time}","params":false}]}`,
  });
  const session = httpSession(draft, base),
    switched = switchActionType(draft.content, 0, "report_status");
  session.edit(switched);
  await session.flush();
  app = reopen(app);
  base = await http(app);
  const state = parseClientJson(
    await (await fetch(`${base}/state`)).text(),
  ) as { drafts: Draft[] };
  const recovered = state.drafts.find((item) => item.id === draft.id)!;
  expect(stringifyJson(recovered.content.actionVariants!["0"][0])).toContain(
    '"type":1e-999',
  );
  const selected = switchActionType(recovered.content, 0, 0);
  expect(
    (parseClientJson(selected.text) as { actions: unknown[] }).actions[0],
  ).toEqual({ name: "A", scheduled_at: time, type: 0 });
  expect(stringifyJson(selected.actionVariants!["0"][0])).toContain(
    '"type":1e-999',
  );
  const second = httpSession(recovered, base);
  second.edit(selected);
  await second.flush();
  app = reopen(app);
  const copied = app.copyDraft(draft.id);
  expect(stringifyJson(copied.content.actionVariants!["0"][0])).toContain(
    '"type":1e-999',
  );
  expect(copied.content.actionVariants!["0"][0].fields.params).toBe(false);
});
