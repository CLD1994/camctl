import { choose } from "./select-support";
import { beforeAll, afterAll, afterEach, expect, it } from "vitest";
import {
  chromium,
  expect as browserExpect,
  type Browser,
} from "@playwright/test";
import { mkdtempSync, rmSync, copyFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Server } from "node:http";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { Draft } from "../../src/server/models";
import { makeMediaFixture } from "../helpers/media";

let browser: Browser;
let fixtures: Awaited<ReturnType<typeof makeMediaFixture>>;
let fixtureDirectory: string;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
  fixtureDirectory = mkdtempSync(join(tmpdir(), "camctl-browser-media-"));
  fixtures = await makeMediaFixture(fixtureDirectory);
}, 20000);
afterAll(async () => {
  await browser?.close();
  if (fixtureDirectory)
    rmSync(fixtureDirectory, { recursive: true, force: true });
});
afterEach(async () => {
  for (const c of clean.splice(0)) await c();
});
async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-browser-"));
  copyFileSync(
    "../../protocol/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"),
  );
  const application = new Application(directory);
  const files = new Files(application);
  const lifecycle = new RequestLifecycle();
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(application, files, lifecycle).listen(
      0,
      "127.0.0.1",
      () => resolve(s),
    );
  });
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((e) => (e ? reject(e) : resolve())),
      ),
    lifecycle,
    () => files.idle(),
    () => application.store.close(),
  );
  clean.push(async () => {
    await context.close();
    await stop();
    rmSync(directory, { recursive: true, force: true });
  });
  const response = await page.goto(base);
  expect(response?.status(), "构建后的客户端首页应可访问").toBe(200);
  return { application, files, page, base };
}
it("网页初始化并恢复未完成的草稿输入", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill('{"name":"未完成",');
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  expect(
    application.store.all<{ content: { text: string } }>("drafts")[0].content
      .text,
  ).toBe('{"name":"未完成",');
  await page.reload();
  await page.getByTestId("draft-open-button").first().click();
  await page.getByTestId("draft-json-toggle").click();
  await browserExpect(page.getByTestId("draft-json-input")).toHaveValue(
    '{"name":"未完成",',
  );
}, 20000);
it("报告独有详情按准确对象准备取回清理和取消，主机事实保持", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("nav-import").click();
  await page.getByTestId("import-files").setInputFiles(fixtures.reportPath);
  await browserExpect.poll(() => application.coverage()).toBe(20);
  const snapshot = JSON.stringify(application.snapshot());
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  await page
    .getByRole("button", { name: /^展开动作 / })
    .first()
    .waitFor();
  while (await page.getByRole("button", { name: /^展开动作 / }).count())
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .click();
  for (const summary of await page
    .getByText("更多操作与交付记录", { exact: true })
    .all())
    await summary.click();
  await browserExpect(page.getByTestId("download-request-button")).toHaveCount(
    0,
  );
  await browserExpect(page.getByTestId("copy-request-button")).toHaveCount(0);
  await page
    .getByRole("button", { name: "准备取回", exact: true })
    .first()
    .click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  const draft = application.store.all<Draft>("drafts")[0];
  expect(JSON.parse(draft.content.text).actions[0]).toEqual({
    name: "取回产物",
    type: "obtain_action_outputs",
    params: { source: { action_instance_id: "a-001" }, output_ids: ["o-001"] },
  });
  const append = async (button: string) => {
    await page.getByTestId("tab-records").click();
    await page.getByTestId("record-open-button").first().click();
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .waitFor();
    while (await page.getByRole("button", { name: /^展开动作 / }).count())
      await page
        .getByRole("button", { name: /^展开动作 / })
        .first()
        .click();
    for (const summary of await page
      .getByText("更多操作与交付记录", { exact: true })
      .all())
      await summary.click();
    await page
      .getByRole("button", { name: button, exact: true })
      .first()
      .click();
    await choose(page.getByLabel("目标草稿"), draft.id);
    await page.getByRole("button", { name: "加入草稿", exact: true }).click();
    await browserExpect(page.getByRole("dialog")).toHaveCount(0);
  };
  await append("准备清理源产物");
  await append("准备取消动作");
  await append("准备取消计划");
  const actions = JSON.parse(application.draft(draft.id).content.text).actions;
  expect(actions.map((a: { params: unknown }) => a.params)).toEqual([
    { source: { action_instance_id: "a-001" }, output_ids: ["o-001"] },
    { output_ids: ["o-001"] },
    { target: { action_instance_id: "a-001" } },
    { target: { plan_instance_id: "p-001" } },
  ]);
  expect(
    actions.every(
      (a: Record<string, unknown>) => !Object.hasOwn(a, "scheduled_at"),
    ),
  ).toBe(true);
  expect(JSON.stringify(application.snapshot())).toBe(snapshot);
}, 20000);
it("视频上传仍被挂起时报告可以应用并查看结果", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  let release!: () => void;
  const hold = new Promise<void>((r) => (release = r));
  await page.route("**/api/imports/*/content", async (route) => {
    const id = route.request().url().split("/").at(-2);
    const item = application.store
      .all<{ id: string; kind: string }>("imports")
      .find((f) => f.id === id);
    if (item?.kind !== "report") await hold;
    await route.continue();
  });
  try {
    await page.getByTestId("nav-import").click();
    await page
      .getByTestId("import-files")
      .setInputFiles([fixtures.videoPath, fixtures.reportPath]);
    await browserExpect.poll(() => application.coverage()).toBe(20);
    expect(application.store.all("videos")).toHaveLength(0);
    await page.getByTestId("nav-plans").click();
    await page.getByTestId("tab-records").click();
    await browserExpect(page.getByTestId("record-open-button")).toHaveCount(1);
  } finally {
    release();
  }
  await browserExpect
    .poll(() => application.store.all<{ status: string }>("videos")[0]?.status)
    .toBe("verified");
}, 20000);
it("未发布交付没有本地文件时不暗示文件等待送达", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("nav-import").click();
  await page
    .getByTestId("import-files")
    .setInputFiles(
      "../../protocol/examples/client-protocol/03-obtain-partial/status-report-1-7b785ee3d38af0ea04a1b546cc21204ae119eccff83757e297d7b8b2d28b0b2c.json",
    );
  await browserExpect.poll(() => application.snapshot().plans?.length).toBe(1);
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  await page
    .getByRole("button", { name: /^展开动作 / })
    .first()
    .waitFor();
  while (await page.getByRole("button", { name: /^展开动作 / }).count())
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .click();
  for (const summary of await page
    .getByText("更多操作与交付记录", { exact: true })
    .all())
    await summary.click();
  const failed = page
    .locator(".delivery")
    .filter({ hasText: "d-202.mp4" })
    .first();
  await browserExpect(failed).toContainText("尚无本地副本");
  await browserExpect(failed).not.toContainText("等待接收");
  const published = page
    .locator(".delivery")
    .filter({ hasText: "d-201.mp4" })
    .first();
  await browserExpect(published).toContainText("等待接收");
}, 20000);
it("网页导出与再次下载保持同一请求，复制后产生新请求", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "网页同步",
      actions: [
        {
          name: "完整同步",
          type: "report_status",
          params: { scope: "full" },
        },
      ],
    }),
  );
  const firstEvent = page.waitForEvent("download");
  await page.getByTestId("export-button").click();
  const first = JSON.parse(
    readFileSync((await (await firstEvent).path())!, "utf8"),
  );
  const nextEvent = page.waitForEvent("download");
  await page.getByTestId("download-request-button").click();
  const next = JSON.parse(
    readFileSync((await (await nextEvent).path())!, "utf8"),
  );
  expect(next.request_id).toBe(first.request_id);
  await page.getByTestId("copy-request-button").click();
  const copyEvent = page.waitForEvent("download");
  await page.getByTestId("export-button").click();
  const copy = JSON.parse(
    readFileSync((await (await copyEvent).path())!, "utf8"),
  );
  expect(copy.request_id).not.toBe(first.request_id);
  expect(application.store.all("requests")).toHaveLength(2);
}, 20000);
it("网页混合导入真实报告与视频，核验后播放并按范围下载", async () => {
  const { page, application, base } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("nav-import").click();
  await page
    .getByTestId("import-files")
    .setInputFiles([fixtures.videoPath, fixtures.reportPath]);
  await browserExpect.poll(() => application.coverage()).toBe(20);
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  await page
    .getByRole("button", { name: /^展开动作 / })
    .first()
    .waitFor();
  while (await page.getByRole("button", { name: /^展开动作 / }).count())
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .click();
  for (const summary of await page
    .getByText("更多操作与交付记录", { exact: true })
    .all())
    await summary.click();
  const video = page.locator("video").first();
  await browserExpect(video).toBeVisible();
  await browserExpect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState))
    .toBeGreaterThanOrEqual(2);
  await video.evaluate((v: HTMLVideoElement) => v.play());
  await browserExpect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime))
    .toBeGreaterThan(0);
  const current = application.store.all<{ id: string }>("videos")[0];
  const ranged = await fetch(`${base}/api/videos/${current.id}/content`, {
    headers: { Range: "bytes=0-15" },
  });
  expect(ranged.status).toBe(206);
  expect(Buffer.from(await ranged.arrayBuffer())).toEqual(
    fixtures.video.subarray(0, 16),
  );
}, 20000);
