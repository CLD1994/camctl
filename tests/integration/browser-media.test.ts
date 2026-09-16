import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import {
  mkdtempSync,
  rmSync,
  copyFileSync,
  readFileSync,
  readdirSync,
} from "node:fs";
import { Readable } from "node:stream";
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
    "docs/superpowers/specs/camctl/examples/capabilities/demo-device.json",
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
  return { app, files, page };
}

it("百张图片默认摘要，分页加载并支持跨页大图与选择", async () => {
  const { app, files, page } = await setup();
  const bytes = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1cAAAAASUVORK5CYII=",
    "base64",
  );
  const r = mappedReport(bytes),
    capture = r.plans![0].actions![0],
    obtain = r.plans![0].actions![1];
  capture.type = "camera_timelapse";
  capture.name = "延时摄影";
  capture.result = { capture: { status: "completed", captured_count: 100 } };
  const o = capture.outputs![0],
    d = obtain.deliveries![0];
  capture.outputs = Array.from({ length: 100 }, (_, i) => ({
    ...structuredClone(o),
    output_id: "o-img-" + i,
    original_name: "图片" + i + ".png",
    media_type: "image/png",
  }));
  obtain.input_params = {
    source: { action_instance_id: capture.action_instance_id },
  };
  obtain.deliveries = Array.from({ length: 100 }, (_, i) => ({
    ...structuredClone(d),
    delivery_id: "d-img-" + i,
    output_id: "o-img-" + i,
    file_name: "d-img-" + i + ".png",
    display_name: "图片" + i + ".png",
  }));
  app.applyReports([reportInput(r)]);
  for (let i = 0; i < 14; i++) {
    const f = files.createBatch([
      { fileName: "d-img-" + i + ".png", size: bytes.length, kind: "media" },
    ]).files[0];
    await files.upload(f.id, Readable.from([bytes]));
  }
  await files.idle();
  await page.reload();
  await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").first().click();
  const card = page
    .locator(".result-card")
    .filter({
      has: page.getByRole("heading", { name: "延时摄影", exact: true }),
    });
  await check(
    card.getByRole("button", { name: "展开动作 延时摄影" }),
  ).toBeVisible();
  await check(page.locator("img[data-thumbnail]")).toHaveCount(0);
  await card.getByRole("button", { name: "展开动作 延时摄影" }).click();
  await check(card.locator("img[data-thumbnail]")).toHaveCount(12);
  await check
    .poll(() =>
      card
        .locator("img[data-thumbnail]")
        .first()
        .evaluate((img: HTMLImageElement) => img.naturalWidth),
    )
    .toBeGreaterThan(0);
  await card.getByLabel("选择 图片0.png", { exact: true }).check();
  await card.getByRole("button", { name: "图片下一页", exact: true }).click();
  await check(card).toContainText("已选择 1");
  await check(card.locator("img[data-thumbnail]")).toHaveCount(2);
  await card
    .getByRole("button", { name: "查看图片 图片12.png", exact: true })
    .click();
  await check(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("ArrowLeft");
  await check(page.getByRole("dialog")).toContainText("图片11.png");
  await page.keyboard.press("Escape");
  await check(page.getByRole("dialog")).toHaveCount(0);
  await card.getByLabel("图片筛选").selectOption("ready");
  await check(card.locator("img[data-thumbnail]")).toHaveCount(12);
  await page.setViewportSize({ width: 390, height: 844 });
  await card.getByRole('button',{name:'底部图片下一页',exact:true}).click();
  await check(card.locator('img[data-thumbnail]')).toHaveCount(2);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
}, 30000);

it("照片与延时表单按能力显示，切换保留参数并导出当前类型", async () => {
  const { app, page } = await setup();
  copyFileSync(
    "docs/superpowers/specs/camctl/examples/capabilities/demo-capture-devices.json",
    join(app.store.directory, "device-capabilities.json"),
  );
  app.reloadCapabilities();
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "三种拍摄",
      actions: [
        {
          name: "照片",
          type: "camera_take_photo",
          device_id: "demo_cam0",
          scheduled_at: "2026-09-16 08:00:00",
          params: { type: "demo_photo", resolution: "1920×1080" },
          policy: { max_delay_ms: 0 },
        },
      ],
    }),
  });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByLabel("照片尺寸 (resolution)")).toHaveValue("0");
  await page
    .getByLabel("动作类型", { exact: true })
    .selectOption("camera_timelapse");
  await page.getByLabel("目标设备", { exact: true }).selectOption("demo_cam0");
  await page.getByLabel("参数类型", { exact: true }).selectOption("demo_count");
  await page.getByLabel("拍摄张数 (count)").fill("100");
  await page.getByLabel("间隔（秒） (interval_s)").fill("5");
  await page.getByLabel("最大允许延迟 (max_delay_ms)").fill("0");
  await page
    .getByLabel("动作类型", { exact: true })
    .selectOption("camera_take_photo");
  await check(page.getByLabel("照片尺寸 (resolution)")).toHaveValue("0");
  await page
    .getByLabel("动作类型", { exact: true })
    .selectOption("camera_timelapse");
  await check(page.getByLabel("拍摄张数 (count)")).toHaveValue("100");
  await page.getByTestId("export-button").click();
  await check.poll(() => app.store.all("requests").length).toBe(1);
  const request = app.store.all<any>("requests")[0];
  expect(request.body.actions[0].type).toBe("camera_timelapse");
  expect(request.body.actions[0].params).toEqual({
    type: "demo_count",
    count: 100,
    interval_s: 5,
  });
  expect(app.draft(draft.id).content.actionVariants).toBeDefined();
});
