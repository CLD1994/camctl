import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, rmSync, copyFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { Draft, Preset, ExportedRequest } from "../../src/server/models";
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
  const directory = mkdtempSync(join(tmpdir(), "camctl-web-edit-"));
  copyFileSync(
    "docs/superpowers/specs/camctl/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"),
  );
  const app = new Application(directory),
    files = new Files(app),
    requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((r) => server.once("listening", r));
  const context = await browser.newContext({
    timezoneId: "Asia/Shanghai",
    acceptDownloads: true,
  });
  const page = await context.newPage();
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
  expect(
    (
      await page.goto(
        `http://127.0.0.1:${(server.address() as { port: number }).port}`,
      )
    )?.status(),
  ).toBe(200);
  await page.getByTestId("initialize-button").click();
  return { page, app };
}
it("Schema普通控件支持本地引用，合法预设不依赖整份计划完成", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_adjustable");
  await page.getByLabel("分辨率 (resolution)").selectOption("0");
  await page.getByLabel("帧率 (frame_rate_fps)").selectOption("1");
  await check(page.getByTestId("validation-issues")).toContainText(
    "frame_rate_fps",
  );
  await page.getByLabel("分辨率 (resolution)").selectOption("1");
  await page.getByLabel("预设名称").fill("巡检");
  await page.getByRole("button", { name: "保存为新预设" }).click();
  await check.poll(() => app.store.all<Preset>("presets").length).toBe(1);
  expect(app.store.all<Preset>("presets")[0].params).toEqual({
    type: "demo_adjustable",
    resolution: "1080p",
    frame_rate_fps: 60,
  });
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(
    JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0]
      .scheduled_at,
  ).toBeUndefined();
  await page.getByLabel("帧率 (frame_rate_fps)").selectOption("0");
  await page.getByRole("button", { name: "应用预设", exact: true }).click();
  await check(page.getByLabel("帧率 (frame_rate_fps)")).toHaveValue("1");
}, 20000);
it("未完成参数JSON切换视图和刷新后仍可修正", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_fixed");
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page.getByLabel("参数 JSON 文本").fill('{"type":');
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  await check(page.getByLabel("参数 JSON 文本")).toHaveValue('{"type":');
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(
    Object.values(app.store.all<Draft>("drafts")[0].content.pending ?? {})[0]
      .text,
  ).toBe('{"type":');
  await page.reload();
  await page.getByTestId("draft-open-button").first().click();
  await check(page.getByLabel("参数 JSON 文本")).toHaveValue('{"type":');
  await page.getByLabel("参数 JSON 文本").fill('{"type":"removed"}');
  await check(page.getByTestId("validation-issues")).toContainText(
    "params.type",
  );
}, 20000);
it("保存响应延迟时立即导出仍使用最新完整内容", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  let release!: () => void;
  const held = new Promise<void>((r) => (release = r));
  let entered!: () => void;
  const started = new Promise<void>((r) => (entered = r));
  let once = true;
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "PUT" && once) {
      once = false;
      const response = await route.fetch();
      entered();
      await held;
      await route.fulfill({ response });
    } else await route.continue();
  });
  const plan = (name: string) =>
    JSON.stringify({
      name,
      actions: [
        { name: "同步", type: "report_status", params: { scope: "full" } },
      ],
    });
  await page.getByTestId("draft-json-input").fill(plan("较早"));
  await started;
  await page.getByTestId("draft-json-input").fill(plan("最新"));
  await page.getByTestId("export-button").click();
  expect(app.store.all("requests")).toHaveLength(0);
  release();
  await check.poll(() => app.store.all("requests").length).toBe(1);
  expect(
    app.store.all<{ body: { name: string } }>("requests")[0].body.name,
  ).toBe("最新");
}, 20000);
it("准备同步追加到所选已有草稿并保留动作与未完成输入", async () => {
  const { page, app } = await setup();
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "后续计划",
      actions: [{ name: "原动作", type: "report_status" }],
    }),
    pending: { "/actions/0/params": { kind: "json", text: "{" } },
  });
  await page.reload();
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByLabel("目标草稿").selectOption(draft.id);
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check
    .poll(() => JSON.parse(app.draft(draft.id).content.text).actions.length)
    .toBe(2);
  const updated = app.draft(draft.id);
  expect(updated.content.pending).toEqual({
    "/actions/0/params": { kind: "json", text: "{" },
  });
  expect(JSON.parse(updated.content.text).actions[1]).toEqual({
    name: "状态同步",
    type: "report_status",
    params: { scope: "full" },
  });
}, 20000);
it("添加动作保留已有动作的未完成参数并继续阻止导出", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page.getByLabel("参数 JSON 文本").fill("{");
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const content = app.store.all<Draft>("drafts")[0].content;
  expect(content.pending?.["/actions/0/params"]).toEqual({
    kind: "json",
    text: "{",
  });
  expect(JSON.parse(content.text).actions).toHaveLength(2);
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("validation-issues")).toContainText(
    "unfinished_input",
  );
  expect(app.store.all("requests")).toHaveLength(0);
}, 20000);
it("后续动作响应丢失后依据草稿内容确认追加成功", async () => {
  const { page, app } = await setup();
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "后续计划",
      actions: [{ name: "原动作", type: "report_status" }],
    }),
  });
  await page.reload();
  let once = true;
  await page.route("**/api/drafts/*/actions", async (route) => {
    if (once) {
      once = false;
      await route.fetch();
      await route.abort();
    } else await route.continue();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByLabel("目标草稿").selectOption(draft.id);
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  expect(JSON.parse(app.draft(draft.id).content.text).actions).toHaveLength(2);
  await check(page.getByTestId("save-status")).toContainText("已保存");
}, 20000);
it("导出回执丢失后仍打开已保存的同一原请求", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "回执核实",
      actions: [{ name: "同步", type: "report_status" }],
    }),
  );
  await page.route("**/api/drafts/*/export", async (route) => {
    await route.fetch();
    await route.abort();
  });
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("download-request-button")).toBeVisible();
  expect(app.store.all("requests")).toHaveLength(1);
}, 20000);
it("普通字段保留缺省和显式空值，重载说明不写入Schema默认值", async () => {
  const { page, app } = await setup();
  const caps = structuredClone(app.capabilities.active!);
  caps.devices[0].actions[0].parameter_types[1].schema = {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    properties: {
      type: { const: "demo_fixed" },
      enabled: { type: "boolean", title: "启用" },
      count: { type: "number", title: "次数", default: 8 },
      note: { type: ["string", "null"], title: "备注" },
      choice: { enum: [false, 0, null, ""], title: "选项" },
    },
    required: ["type"],
    additionalProperties: false,
  };
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify(caps),
  );
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_fixed");
  await check(page.getByLabel("次数 (count)")).toHaveValue("");
  await page.getByLabel("启用 (enabled)").selectOption("false");
  await page.getByLabel("次数 (count)").fill("0");
  await page.getByRole("button", { name: "设为 null", exact: true }).click();
  await page.getByLabel("选项 (choice)").selectOption("3");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const draft = app.store.all<Draft>("drafts")[0];
  expect(JSON.parse(draft.content.text).actions[0].params).toEqual({
    type: "demo_fixed",
    enabled: false,
    count: 0,
    note: null,
    choice: "",
  });
  const before = draft.content.text;
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await page.getByTestId("nav-plans").click();
  expect(app.draft(draft.id).content.text).toBe(before);
}, 20000);
it("人工标记清除只更正交接记录并保留固定正文", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "人工交接",
      actions: [{ name: "同步", type: "report_status" }],
    }),
  );
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("download-request-button")).toBeVisible();
  const record = app.store.all<ExportedRequest>("requests")[0];
  await page.getByRole("button", { name: "标记已递交", exact: true }).click();
  await check.poll(() => app.request(record.id).handedAt).not.toBeNull();
  const marked = app.request(record.id).handedAt;
  await page.getByTestId("download-request-button").click();
  expect(app.request(record.id).handedAt).toBe(marked);
  await page.getByRole("button", { name: "清除递交标记", exact: true }).click();
  await check.poll(() => app.request(record.id).handedAt).toBeNull();
  expect(app.request(record.id).body).toEqual(record.body);
  expect(app.snapshot().plans).toBeUndefined();
}, 20000);
it("预设创建回执丢失时展示未确认并读取实际记录，不重复创建", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_fixed");
  await page.getByLabel("预设名称").fill("回执待核实");
  await page.route("**/api/presets", async (route) => {
    await route.fetch();
    await route.abort();
  });
  await page.getByRole("button", { name: "保存为新预设" }).click();
  await check(page.locator(".preset-box")).toContainText("尚未确认");
  expect(app.store.all("presets")).toHaveLength(1);
  await check(page.getByLabel("已有预设").locator("option")).toHaveCount(2);
}, 20000);
