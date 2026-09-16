import { choose } from "./select-support";
import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import {
  mkdtempSync,
  rmSync,
  copyFileSync,
  readFileSync,
  readdirSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { mappedReport, reportInput } from "./fixtures";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { Draft } from "../../src/server/models";
import { loadCapabilities } from "../../src/shared/capabilities";
let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
});
afterAll(async () => {
  await browser.close();
});
afterEach(async () => {
  for (const fn of clean.splice(0)) await fn();
});
async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-web-review-"));
  copyFileSync(
    "../../protocol/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"),
  );
  const app = new Application(directory),
    files = new Files(app),
    requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((r) => server.once("listening", r));
  const context = await browser.newContext({ acceptDownloads: true }),
    page = await context.newPage();
  page.setDefaultTimeout(5000);
  const stop = createStop(
    () => new Promise<void>((r, j) => server.close((e) => (e ? j(e) : r()))),
    requests,
    () => files.idle(),
    () => app.store.close(),
  );
  clean.push(async () => {
    await context.close();
    await stop();
    rmSync(directory, { recursive: true, force: true });
  });
  await page.goto(
    `http://127.0.0.1:${(server.address() as { port: number }).port}`,
  );
  await page.getByTestId("initialize-button").click();
  return { app, page };
}
const text = (name: string) =>
  JSON.stringify({
    name,
    actions: [
      { name: "同步", type: "report_status", params: { scope: "full" } },
    ],
  });
const cameraPlan = () => ({
  name: "缓存修正",
  actions: [
    {
      name: "录像",
      type: "camera_record",
      device_id: "demo_cam0",
      params: { type: "demo_fixed" },
      policy: { max_delay_ms: 0 },
      scheduled_at: "2026-09-17 00:00:00",
    },
  ],
});
it("通用入口修正回原合法参数后JSON显示、保存和导出同源", async () => {
  const { app, page } = await setup(),
    draft = app.createDraft({ text: JSON.stringify(cameraPlan()) });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await page
    .locator(".action-card")
    .first()
    .getByRole("button", { name: "参数 JSON", exact: true })
    .click();
  const widget = page.getByLabel("参数 JSON 文本"),
    pending = page.getByLabel("未完成输入 /actions/0/params");
  await widget.fill("{");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(app.draft(draft.id).content.pending?.["/actions/0/params"].text).toBe(
    "{",
  );
  await pending.fill('{"type":');
  await check(widget).toHaveValue('{"type":');
  await page
    .getByRole("button", { name: "应用修正 /actions/0/params", exact: true })
    .click();
  await check(widget).toHaveValue('{"type":');
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(app.draft(draft.id).content.pending?.["/actions/0/params"].text).toBe(
    '{"type":',
  );
  await pending.fill('{"type":"demo_fixed"}');
  await check(widget).toHaveValue('{"type":"demo_fixed"}');
  await page
    .getByRole("button", { name: "应用修正 /actions/0/params", exact: true })
    .click();
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(app.draft(draft.id).content.pending).toEqual({});
  expect(
    JSON.parse(app.draft(draft.id).content.text).actions[0].params,
  ).toEqual({ type: "demo_fixed" });
  await check(widget).toHaveValue(
    JSON.stringify({ type: "demo_fixed" }, null, 2),
  );
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("download-request-button")).toBeVisible();
  expect(
    app.store.all<{ body: { actions: Array<{ params: unknown }> } }>(
      "requests",
    )[0].body.actions[0].params,
  ).toEqual({ type: "demo_fixed" });
}, 20000);
it.each([
  '{"name":"未完成","actions":[',
  "[]",
  '{"name":"未完成"}',
  '{"name":"未完成","actions":{}}',
])(
  "追加结构不满足时保留可编辑原文且不发送追加：%s",
  async (original) => {
    const { app, page } = await setup(),
      draft = app.createDraft({ text: original });
    await page.reload();
    let appends = 0;
    await page.route("**/api/drafts/*/actions", async (route) => {
      appends++;
      await route.continue();
    });
    await page
      .getByRole("button", { name: "准备状态同步", exact: true })
      .click();
    await choose(page.getByLabel("目标草稿"), draft.id);
    await page.getByRole("button", { name: "加入草稿", exact: true }).click();
    await page
      .getByRole("button", { name: "查看目标草稿", exact: true })
      .click();
    await page.getByTestId("draft-json-toggle").click();
    await check(page.getByTestId("draft-json-input")).toBeEnabled();
    await check(page.getByTestId("draft-json-input")).toHaveValue(original);
    expect(appends).toBe(0);
    expect(app.draft(draft.id).revision).toBe(1);
    expect(app.store.all("drafts")).toHaveLength(1);
  },
  20000,
);
it.each(["different", "omit", "preset_same", "preset_different"] as const)(
  "JSON展示跟随明确修正或替换且保留其他pending：%s",
  async (mode) => {
    const { app, page } = await setup(),
      plan = cameraPlan();
    const others = {
      "/actions/0/policy/max_delay_ms": { kind: "number" as const, text: "-" },
      "/actions/1/params": { kind: "json" as const, text: "[" },
    };
    const draft = app.createDraft({
      text: JSON.stringify({
        ...plan,
        actions: [...plan.actions, { name: "另一动作", type: "report_status" }],
      }),
      pending: others,
    });
    const want =
      mode === "preset_same"
        ? { type: "demo_fixed" }
        : mode === "preset_different"
          ? { type: "demo_adjustable", resolution: "1080p", frame_rate_fps: 30 }
          : mode === "different"
            ? { type: 30, flag: false, optional: null, list: [1, "1"] }
            : undefined;
    const preset = mode.startsWith("preset")
      ? app.savePreset({
          name: "恢复预设",
          deviceId: "demo_cam0",
          actionType: "camera_record",
          params: want,
        })
      : undefined;
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    await page
      .locator(".action-card")
      .first()
      .getByRole("button", { name: "参数 JSON", exact: true })
      .click();
    const widget = page.getByLabel("参数 JSON 文本");
    await widget.fill("{");
    if (preset) {
      await page.getByText("拍摄参数预设", { exact: true }).first().click();
      await choose(page.getByLabel("已有预设"), preset.id);
      await page.getByRole("button", { name: "应用预设", exact: true }).click();
    } else if (mode === "omit")
      await page
        .getByRole("button", {
          name: "放弃输入 /actions/0/params",
          exact: true,
        })
        .click();
    else {
      await page
        .getByLabel("未完成输入 /actions/0/params")
        .fill(JSON.stringify(want));
      await page
        .getByRole("button", {
          name: "应用修正 /actions/0/params",
          exact: true,
        })
        .click();
    }
    await check(widget).toHaveValue(
      want === undefined ? "" : JSON.stringify(want, null, 2),
    );
    await check(page.getByTestId("save-status")).toContainText("已保存");
    const saved = app.draft(draft.id).content;
    expect(saved.pending).toEqual(others);
    expect(JSON.parse(saved.text).actions[0].params).toEqual(want);
    expect(JSON.parse(saved.text).actions[0].policy).toEqual({
      max_delay_ms: 0,
    });
  },
  20000,
);
it.each(["policy", "action_params"] as const)(
  "共享JSON控件同值修正后展示合法值：%s",
  async (mode) => {
    const { app, page } = await setup(),
      isPolicy = mode === "policy",
      original = isPolicy ? { max_delay_ms: 0 } : { scope: "full" },
      path = isPolicy ? "/actions/0/policy" : "/actions/0/params";
    const draft = app.createDraft({
      text: JSON.stringify(
        isPolicy
          ? cameraPlan()
          : {
              name: "同步",
              actions: [
                { name: "报告", type: "report_status", params: original },
              ],
            },
      ),
    });
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    if (isPolicy)
      await page.getByText("完整业务策略 JSON", { exact: true }).click();
    else
      await page
        .getByRole("button", { name: "参数 JSON", exact: true })
        .click();
    const widget = page.getByLabel(
      isPolicy ? "业务策略 JSON" : "动作参数 JSON",
    );
    await widget.fill("{");
    await page.getByLabel(`未完成输入 ${path}`).fill(JSON.stringify(original));
    await page
      .getByRole("button", { name: `应用修正 ${path}`, exact: true })
      .click();
    await check(widget).toHaveValue(JSON.stringify(original, null, 2));
    await check(page.getByTestId("save-status")).toContainText("已保存");
    expect(app.draft(draft.id).content.pending).toEqual({});
    expect(
      JSON.parse(app.draft(draft.id).content.text).actions[0][
        isPolicy ? "policy" : "params"
      ],
    ).toEqual(original);
  },
  20000,
);
it("结构修正后在同页同目标重新准备，按新revision保留未完成内容追加一次", async () => {
  const { app, page } = await setup(),
    draft = app.createDraft({ text: '{"name":"未完成","actions":[' });
  await page.reload();
  const revisions: number[] = [];
  await page.route("**/api/drafts/*/actions", async (route) => {
    revisions.push(route.request().postDataJSON().revision);
    await route.continue();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await choose(page.getByLabel("目标草稿"), draft.id);
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(
    page.getByRole("button", { name: "重试准备目标草稿", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "查看目标草稿", exact: true }).click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "修正结构",
      actions: [{ name: "原录像", type: "camera_record" }],
    }),
  );
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await page.getByTestId("draft-json-toggle").click();
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page.getByLabel("参数 JSON 文本").fill("{");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const baseline = app.draft(draft.id);
  expect(revisions).toEqual([]);
  await page
    .getByRole("button", { name: "返回核实后续操作", exact: true })
    .click();
  await page
    .getByRole("button", { name: "重试准备目标草稿", exact: true })
    .click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  expect(app.store.all("drafts")).toHaveLength(1);
  const saved = app.draft(draft.id);
  expect(revisions).toEqual([baseline.revision]);
  expect(saved.revision).toBe(baseline.revision + 1);
  expect(saved.content.pending).toEqual(baseline.content.pending);
  const actions = JSON.parse(saved.content.text).actions;
  expect(actions[0]).toEqual({ name: "原录像", type: "camera_record" });
  expect(actions[1]).toEqual({
    name: "状态同步",
    type: "report_status",
    params: { scope: "full" },
  });
  await check(page.getByLabel("动作名称").first()).toBeEnabled();
}, 20000);
it("数值未完成时兄弟字段编辑保留原文，修正后导出准确新值", async () => {
  const { app, page } = await setup();
  const parameter =
    app.capabilities.active!.devices[0].actions[0].parameter_types.find(
      (p) => p.type === "demo_fixed",
    )!;
  parameter.schema = {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    properties: {
      type: { const: "demo_fixed" },
      count: { type: "number" },
      note: { type: "string" },
    },
    required: ["type", "count", "note"],
    additionalProperties: false,
  };
  app.capabilities.active = loadCapabilities(app.capabilities.active);
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "真实输入",
      actions: [
        {
          name: "录像",
          type: "camera_record",
          device_id: "demo_cam0",
          params: { type: "demo_fixed", count: 3, note: "old" },
          policy: { max_delay_ms: 0 },
          scheduled_at: "2026-09-17 00:00:00",
        },
      ],
    }),
  });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await page.getByLabel("count (count)", { exact: true }).fill("1e");
  await page.getByLabel("note (note)", { exact: true }).fill("changed");
  await page
    .locator(".action-card")
    .first()
    .getByRole("button", { name: "参数 JSON", exact: true })
    .click();
  await check(page.getByLabel("参数 JSON 文本")).toHaveAttribute(
    "readonly",
    "",
  );
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(app.draft(draft.id).content.pending).toEqual({
    "/actions/0/params/count": { kind: "number", text: "1e" },
  });
  await page.getByTestId("export-button").click();
  await check(page.getByRole("alert")).toBeVisible();
  expect(app.store.all("requests")).toHaveLength(0);
  await check(
    page.getByLabel("未完成输入 /actions/0/params/count"),
  ).toBeEnabled();
  await page.getByLabel("未完成输入 /actions/0/params/count").fill("12");
  await page
    .getByRole("button", {
      name: "应用修正 /actions/0/params/count",
      exact: true,
    })
    .click();
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("download-request-button")).toBeVisible();
  const body = app.store.all<{ body: { actions: Array<{ params: unknown }> } }>(
    "requests",
  )[0].body;
  expect(body.actions[0].params).toEqual({
    type: "demo_fixed",
    count: 12,
    note: "changed",
  });
}, 20000);
it("父JSON只读保留未完成路径，Schema没有控件时仍能明确修正", async () => {
  const { app, page } = await setup();
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "参数",
      actions: [
        {
          name: "录像",
          type: "camera_record",
          device_id: "demo_cam0",
          params: { type: "demo_fixed", count: 3, note: "old" },
          policy: { max_delay_ms: 0 },
          scheduled_at: "2026-09-17 00:00:00",
        },
      ],
    }),
    pending: { "/actions/0/params/count": { kind: "number", text: "1e" } },
  });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await page
    .locator(".action-card")
    .first()
    .getByRole("button", { name: "参数 JSON", exact: true })
    .click();
  await check(page.getByLabel("参数 JSON 文本")).toHaveAttribute(
    "readonly",
    "",
  );
  await page.getByTestId("draft-json-toggle").click();
  await check(page.getByTestId("draft-json-input")).toHaveAttribute(
    "readonly",
    "",
  );
  const path = "/actions/0/params/count";
  await check(page.getByLabel(`未完成输入 ${path}`)).toHaveValue("1e");
  await page.getByLabel(`未完成输入 ${path}`).fill("12");
  await page
    .getByRole("button", { name: `应用修正 ${path}`, exact: true })
    .click();
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(app.draft(draft.id).content.pending).toEqual({});
  expect(
    JSON.parse(app.draft(draft.id).content.text).actions[0].params.count,
  ).toBe(12);
}, 20000);
it("保存回执和首次核实均丢失后能保存更新输入", async () => {
  const { app, page } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  let offline = false,
    first = true;
  await page.route("**/api/state", async (route) =>
    offline ? route.abort() : route.continue(),
  );
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "PUT" && first) {
      first = false;
      await route.fetch();
      offline = true;
      await route.abort();
    } else await route.continue();
  });
  await page.getByTestId("draft-json-input").fill(text("A"));
  await check(page.getByTestId("save-status")).toContainText("未确认");
  await page.getByTestId("draft-json-input").fill(text("B"));
  offline = false;
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const actual = app.store.all<Draft>("drafts")[0];
  expect(JSON.parse(actual.content.text).name).toBe("B");
  expect(actual.revision).toBe(3);
}, 20000);
it("导出回执和即时核实丢失期间锁定编辑，随后轮询恢复原记录", async () => {
  const { app, page } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(text("固定请求"));
  let offline = false;
  await page.route("**/api/state", async (route) =>
    offline ? route.abort() : route.continue(),
  );
  await page.route("**/api/drafts/*/export", async (route) => {
    await route.fetch();
    offline = true;
    await route.abort();
  });
  await page.getByTestId("export-button").click();
  await check(
    page.getByRole("button", { name: "重新核实导出结果", exact: true }),
  ).toBeVisible();
  await check(page.getByTestId("draft-json-input")).toBeDisabled();
  offline = false;
  await check(page.getByTestId("download-request-button")).toBeVisible();
  expect(app.store.all("requests")).toHaveLength(1);
  expect(
    app.store.all<{ body: { name: string } }>("requests")[0].body.name,
  ).toBe("固定请求");
}, 20000);
it("观察已导出后保留额外输入，跨页面明确另存后不重复恢复", async () => {
  const { app, page } = await setup();
  const draft = app.createDraft({ text: text("固定A") });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "PUT") {
      const saved = app.draft(draft.id);
      app.exportDraft(draft.id, saved.revision, saved.content);
      await route.abort();
    } else await route.continue();
  });
  await page.getByTestId("draft-json-input").fill(text("保留B"));
  await check(page.getByLabel("保留的额外编辑内容")).toHaveValue(text("保留B"));
  await page.getByTestId("nav-devices").click();
  await check(page.getByLabel("保留的额外编辑内容")).toHaveCount(0);
  await page.getByTestId("nav-plans").click();
  await check(page.getByLabel("保留的额外编辑内容")).toHaveValue(text("保留B"));
  await page
    .getByRole("button", { name: "将保留内容保存为新草稿", exact: true })
    .click();
  await check(page.getByTestId("draft-open-button")).toHaveCount(1);
  await check(page.getByLabel("保留的额外编辑内容")).toHaveCount(0);
  await page.reload();
  await check(page.getByTestId("draft-open-button")).toHaveCount(1);
  expect(
    app.store.all<Draft>("drafts").find((d) => !d.exportedRequestId)!.content
      .text,
  ).toBe(text("保留B"));
  expect(
    app.store.all<{ body: { name: string } }>("requests")[0].body.name,
  ).toBe("固定A");
}, 20000);
it("冲突行下载名和字节始终属于已接受报告", async () => {
  const { page } = await setup(),
    accepted = reportInput(mappedReport(Buffer.from("video")));
  const conflict = Buffer.from(
    JSON.stringify(JSON.parse(accepted.bytes.toString()), null, 2),
  );
  const conflictName = `status-report-1-${createHash("sha256").update(conflict).digest("hex")}.json`;
  await page.getByTestId("nav-import").click();
  for (const [bytes, name] of [
    [accepted.bytes, accepted.file.fileName],
    [accepted.bytes, accepted.file.fileName],
    [conflict, conflictName],
  ] as const) {
    await page
      .getByTestId("import-files")
      .setInputFiles({ name, mimeType: "application/json", buffer: bytes });
    const row = page.locator(".import-batch").first();
    await check(
      row.getByRole("button", { name: "下载已接受报告原文", exact: true }),
    ).toBeVisible();
    const waiting = page.waitForEvent("download");
    await row
      .getByRole("button", { name: "下载已接受报告原文", exact: true })
      .click();
    const downloaded = await waiting;
    expect(downloaded.suggestedFilename()).toBe(accepted.file.fileName);
    expect(readFileSync((await downloaded.path())!)).toEqual(accepted.bytes);
    expect(downloaded.suggestedFilename()).toContain(
      createHash("sha256")
        .update(readFileSync((await downloaded.path())!))
        .digest("hex"),
    );
  }
  await page.getByTestId("import-files").setInputFiles({
    name: "status-report-9-invalid.json",
    mimeType: "application/json",
    buffer: Buffer.from("{}"),
  });
  await check(
    page.locator(".import-batch").first().locator(".badge"),
  ).toContainText("失败");
  await check(
    page
      .locator(".import-batch")
      .first()
      .getByRole("button", { name: "下载已接受报告原文", exact: true }),
  ).toHaveCount(0);
}, 25000);
it("新目标追加与首次核实丢失后恢复只打开同一份草稿", async () => {
  const { app, page } = await setup();
  let offline = false,
    creates = 0,
    appends = 0;
  await page.route("**/api/state", (r) => (offline ? r.abort() : r.continue()));
  await page.route("**/api/drafts", async (r) => {
    if (r.request().method() === "POST") creates++;
    await r.continue();
  });
  await page.route("**/api/drafts/*/actions", async (r) => {
    appends++;
    await r.fetch();
    offline = true;
    await r.abort();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(
    page.getByRole("button", { name: "重新核实追加结果", exact: true }),
  ).toBeVisible();
  const target = app.store.all<Draft>("drafts")[0].id;
  await page.getByRole("button", { name: "返回", exact: true }).click();
  offline = false;
  await page
    .getByRole("button", { name: "返回核实后续操作", exact: true })
    .click();
  await page
    .getByRole("button", { name: "重新核实追加结果", exact: true })
    .click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  expect(app.store.all("drafts")).toHaveLength(1);
  expect(app.draft(target).revision).toBe(2);
  expect(creates).toBe(1);
  expect(appends).toBe(1);
}, 20000);
it("新目标明确未追加后重试沿用相同目标", async () => {
  const { app, page } = await setup();
  let attempts = 0,
    creates = 0;
  await page.route("**/api/drafts", async (r) => {
    if (r.request().method() === "POST") creates++;
    await r.continue();
  });
  await page.route("**/api/drafts/*/actions", (r) =>
    ++attempts === 1 ? r.abort() : r.continue(),
  );
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(
    page.getByRole("button", { name: "重试同一目标追加", exact: true }),
  ).toBeVisible();
  const target = app.store.all<Draft>("drafts")[0].id;
  expect(app.draft(target).revision).toBe(1);
  await page
    .getByRole("button", { name: "重试同一目标追加", exact: true })
    .click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  expect(app.draft(target).revision).toBe(2);
  expect(creates).toBe(1);
  expect(attempts).toBe(2);
}, 20000);
it("已有目标追加未知恢复保留原动作和pending", async () => {
  const { app, page } = await setup();
  const original = {
    text: JSON.stringify({
      name: "已有草稿",
      actions: [{ name: "原动作", type: "report_status" }],
    }),
    pending: { "/actions/0/params": { kind: "json" as const, text: "{" } },
  };
  const target = app.createDraft(original);
  await page.reload();
  let offline = false,
    appends = 0;
  await page.route("**/api/state", (r) => (offline ? r.abort() : r.continue()));
  await page.route("**/api/drafts/*/actions", async (r) => {
    appends++;
    await r.fetch();
    offline = true;
    await r.abort();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await choose(page.getByLabel("目标草稿"), target.id);
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(
    page.getByRole("button", { name: "重新核实追加结果", exact: true }),
  ).toBeVisible();
  offline = false;
  await page
    .getByRole("button", { name: "重新核实追加结果", exact: true })
    .click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  expect(app.store.all("drafts")).toHaveLength(1);
  expect(app.draft(target.id).content.pending).toEqual(original.pending);
  expect(JSON.parse(app.draft(target.id).content.text).actions).toHaveLength(2);
  expect(appends).toBe(1);
}, 20000);
it("追加结果与基线和预期均不同则保留冲突，反复核实不写入", async () => {
  const { app, page } = await setup();
  let appends = 0;
  await page.route("**/api/drafts/*/actions", async (r) => {
    appends++;
    const id = r.request().url().split("/").at(-2)!,
      before = app.draft(id);
    app.saveDraft(id, before.revision, {
      text: JSON.stringify({ name: "不同内容", actions: [] }),
    });
    await r.abort();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(
    page.getByRole("button", { name: "重新核实追加结果", exact: true }),
  ).toBeVisible();
  await page.getByText("查看实际草稿记录", { exact: true }).click();
  await check(page.getByRole("dialog").locator("pre").last()).toContainText(
    "不同内容",
  );
  await page
    .getByRole("button", { name: "重新核实追加结果", exact: true })
    .click();
  await check(page.getByRole("dialog")).toBeVisible();
  expect(appends).toBe(1);
  expect(app.store.all("drafts")).toHaveLength(1);
}, 20000);
it("创建响应丢失只提供实际草稿核对，不自动再次创建", async () => {
  const { app, page } = await setup();
  let creates = 0,
    appends = 0;
  await page.route("**/api/drafts", async (r) => {
    creates++;
    await r.fetch();
    await r.abort();
  });
  await page.route("**/api/drafts/*/actions", async (r) => {
    appends++;
    await r.continue();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(
    page.getByRole("button", { name: "查看实际草稿列表", exact: true }),
  ).toBeVisible();
  await check(page.getByRole("dialog").getByRole("button")).toHaveCount(2);
  await page.getByRole("button", { name: "返回", exact: true }).click();
  await page
    .getByRole("button", { name: "返回核实后续操作", exact: true })
    .click();
  await page
    .getByRole("button", { name: "查看实际草稿列表", exact: true })
    .click();
  await check(page.getByTestId("draft-open-button")).toHaveCount(1);
  expect(creates).toBe(1);
  expect(appends).toBe(0);
}, 20000);
it("报告详情逐项失败来源ID和原输入保持报告字面值", async () => {
  const { app, page } = await setup(),
    directory = "../../protocol/examples/client-protocol/03-obtain-partial";
  const report = JSON.parse(
    readFileSync(
      join(
        directory,
        readdirSync(directory).find((name) =>
          name.startsWith("status-report-"),
        )!,
      ),
      "utf8",
    ).replaceAll("a-202", "pending"),
  );
  const action = report.plans[0].actions[0];
  action.input_params = { type: "running", status: "pending" };
  action.effective_params = { type: "running", status: "failed" };
  app.applyReports([reportInput(report)]);
  expect(app.coverage(), JSON.stringify(app.state().imports)).toBe(39);
  await page.reload();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  const card = page.locator(".result-card").first();
  await page
    .getByRole("button", { name: /^展开动作 / })
    .first()
    .waitFor();
  while (await page.getByRole("button", { name: /^展开动作 / }).count())
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .click();
  await page
    .locator(".result-card")
    .last()
    .getByText("执行技术详情", { exact: true })
    .click();
  await check(
    page
      .locator(".result-card")
      .last()
      .locator("details")
      .filter({ has: page.getByText("执行技术详情", { exact: true }) })
      .getByText("pending", { exact: true }),
  ).toBeVisible();
  await card.getByText("输入与生效参数", { exact: true }).click();
  await check(card.getByText("running", { exact: true }).first()).toBeVisible();
  await check(card.getByText("failed", { exact: true })).toBeVisible();
}, 20000);

it("页面反馈只在所属页面显示并在三秒后消失", async () => {
  const { page } = await setup();
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  const feedback = page.locator(".notice.success");
  await check(feedback).toBeVisible();
  await page.getByTestId("nav-import").click();
  await check(feedback).toHaveCount(0);
  await page.getByTestId("nav-devices").click();
  await check(feedback).toBeVisible();
  await check(feedback).toHaveCount(0, { timeout: 4000 });
});
it("计划和动作独立折叠且轮询保留选择", async () => {
  const { app, page } = await setup();
  app.applyReports([reportInput(mappedReport(Buffer.from("video")))]);
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  await page
    .getByRole("button", { name: "展开动作 主录像", exact: true })
    .click();
  await page
    .getByRole("button", { name: "折叠动作 主录像", exact: true })
    .click();
  await check(
    page.getByRole("button", { name: "展开动作 主录像", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "折叠计划", exact: true }).click();
  for (const card of await page.locator(".result-card").all())
    await check(card).toBeHidden();
  await page.getByRole("button", { name: "展开计划", exact: true }).click();
  await page.waitForResponse((response) =>
    response.url().endsWith("/api/state"),
  );
  await check(
    page.getByRole("button", { name: "展开动作 主录像", exact: true }),
  ).toBeVisible();
});
it("导入按文件分页，大批次不突破每页上限", async () => {
  const { app, page } = await setup();
  for (let i = 0; i < 23; i++)
    app.store.set("imports", `file-${i}`, {
      id: `file-${i}`,
      batchId: "one-batch",
      fileName: `clip-${i}.mp4`,
      kind: "video",
      expectedSize: 1,
      bytesReceived: 1,
      status: "waiting_report",
      createdAt: `2026-09-16 00:00:${String(i).padStart(2, "0")}`,
    });
  await page.getByTestId("nav-import").click();
  await check(page.locator(".import-row")).toHaveCount(10);
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await check(page.locator(".import-row")).toHaveCount(10);
  await page.waitForResponse((response) =>
    response.url().endsWith("/api/state"),
  );
  await check(page.locator(".import-row").first()).toContainText("clip-12.mp4");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await check(page.locator(".import-row")).toHaveCount(3);
  await check(
    page.getByRole("button", { name: "下一页", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "上一页", exact: true }).click();
  await check(page.locator(".import-row")).toHaveCount(10);
});

it("同文案的新提示独立计时，旧计时不会提前清除", async () => {
  const { page } = await setup();
  await page.clock.install();
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(page.locator(".notice.success")).toBeVisible();
  await page.clock.fastForward(2000);
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(page.locator(".notice.success")).toBeVisible();
  await page.clock.fastForward(1100);
  await check(page.locator(".notice.success")).toBeVisible();
  await page.clock.fastForward(2000);
  await check(page.locator(".notice.success")).toHaveCount(0);
});
it("异步操作完成时提示仍属于发起页面", async () => {
  const { page } = await setup();
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/capabilities/reload", async (route) => {
    const response = await route.fetch();
    await held;
    await route.fulfill({ response });
  });
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await page.getByTestId("nav-import").click();
  const finished = page.waitForResponse("**/api/capabilities/reload");
  release();
  await finished;
  await check(page.locator(".notice.success")).toHaveCount(0);
  await page.getByTestId("nav-devices").click();
  await check(page.locator(".notice.success")).toBeVisible();
});
