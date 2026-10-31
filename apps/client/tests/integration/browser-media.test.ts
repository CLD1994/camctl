import { choose, readOptions } from "./select-support";
import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import {
  mkdtempSync,
  rmSync,
  copyFileSync,
  readFileSync,
  readdirSync,
  mkdirSync,
  writeFileSync,
  unlinkSync,
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
import type { Output, ReportAction } from "../../src/shared/types";
import { loadCapabilities } from "../../src/shared/capabilities";
let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({
    headless: true,
    channel: process.env.CAMCTL_TEST_BROWSER_CHANNEL,
  });
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
  return { app, files, page };
}
const receivedPng = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1cAAAAASUVORK5CYII=", "base64");
it.each(["plan", "action"] as const)("默认 %s 取回导出的请求排除已收到产物", async (scope) => {
  const { app, files, page } = await setup();
  const report = mappedReport(receivedPng), plan = report.plans![0], capture = plan.actions![0], delivery = plan.actions![1].deliveries![0];
  app.applyReports([reportInput(report)]);
  const uploaded = files.createBatch([{ fileName: delivery.file_name, size: receivedPng.length, kind: "media" }]).files[0];
  await files.upload(uploaded.id, Readable.from([receivedPng])); await files.idle();
  await page.reload(); await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").click();
  if (scope === "action") await page.locator(`[data-action-id="${capture.action_instance_id}"]`).getByRole("button", { name: /^展开动作/ }).click();
  await page.getByRole("button", { name: scope === "plan" ? "取回计划产物" : "取回默认产物", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await check(page.getByText(/1 个产物排除项/)).toBeVisible();
  await page.getByLabel("执行时间", { exact: true }).fill("2026-10-26T10:00");
  await page.getByTestId("export-button").click();
  await check.poll(() => app.store.all("requests").length).toBe(1);
  const request = app.store.all<any>("requests")[0];
  expect(request.body.actions[0].params).toEqual({
    source: scope === "plan" ? { plan_instance_id: plan.plan_instance_id } : { action_instance_id: capture.action_instance_id },
    exclude_output_ids: [delivery.output_id],
  });
  if (process.env.CAMCTL_RESULT_OPERATIONS_OUTPUT) {
    mkdirSync(process.env.CAMCTL_RESULT_OPERATIONS_OUTPUT, { recursive: true });
    writeFileSync(join(process.env.CAMCTL_RESULT_OPERATIONS_OUTPUT, `default-${scope}.json`), JSON.stringify(request.body));
  }
});
it("本次交付尚未接收时可通过其他合格副本打开大图", async () => {
  const { app, files, page } = await setup();
  const report = mappedReport(receivedPng);
  const capture = report.plans![0].actions![0], obtain = report.plans![0].actions![1];
  capture.outputs![0].media_type = "image/png";
  capture.outputs![0].original_name = "共享图片.png";
  obtain.deliveries![0].file_name = "1001.png";
  obtain.deliveries![0].delivery_id = "1001";
  const other = structuredClone(obtain);
  other.action_instance_id = "8"; other.name = "再次取回";
  other.deliveries![0].delivery_id = "1002"; other.deliveries![0].file_name = "1002.png";
  report.plans![0].actions!.push(other);
  app.applyReports([reportInput(report)]);
  expect(app.coverage(), JSON.stringify(app.state().imports)).toBe(report.to_wm);
  const uploaded = files.createBatch([{ fileName: "1001.png", size: receivedPng.length, kind: "media" }]).files[0];
  await files.upload(uploaded.id, Readable.from([receivedPng])); await files.idle();
  await page.reload(); await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").click();
  const action = page.locator('[data-action-id="8"]');
  await action.getByRole("button", { name: /^展开动作/ }).click();
  await check(action.locator(".action-summary")).toContainText("本地已核验 0 / 1");
  await action.getByRole("button", { name: "查看图片 共享图片.png", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "图片查看" });
  await check.poll(() => dialog.locator("img").evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  await check(dialog.getByRole("button", { name: "下载", exact: true })).toBeVisible();
});
it("按来源取回时重新核验全部所选文件并保留已移走的副本", async () => {
  const { app, files, page } = await setup();
  const report = mappedReport(receivedPng), plan = report.plans![0];
  const capture = plan.actions![0], obtain = plan.actions![1];
  const original = capture.outputs![0];
  capture.outputs = ["101", "102"].map((id) => ({ ...original, output_id: id, original_name: `${id}.png`, media_type: "image/png" }));
  const second = structuredClone(capture); second.action_instance_id = "9"; second.name = "第二拍摄";
  second.outputs = [{ ...capture.outputs[0], output_id: "201", source_action_instance_id: "9", original_name: "201.png" }];
  plan.actions!.push(second);
  obtain.deliveries![0].output_id = "101"; obtain.deliveries![0].file_name = "1001.png";
  obtain.deliveries![0].delivery_id = "1001";
  obtain.input_params = { source: { plan_instance_id: plan.plan_instance_id } };
  obtain.deliveries!.push(
    { ...obtain.deliveries![0], delivery_id: "1002", output_id: "102", file_name: "1002.png" },
    { ...obtain.deliveries![0], delivery_id: "1003", output_id: "201", file_name: "1003.png", source_action_instance_id: "9" },
  );
  app.applyReports([reportInput(report)]);
  expect(app.coverage(), JSON.stringify(app.state().imports)).toBe(report.to_wm);
  const uploaded = files.createBatch([{ fileName: "1001.png", size: receivedPng.length, kind: "media" }]).files[0];
  await files.upload(uploaded.id, Readable.from([receivedPng])); await files.idle();
  unlinkSync(files.videos()[0].path);
  await page.reload(); await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").click();
  const first = page.locator(`[data-action-id="${obtain.action_instance_id}"]`);
  await first.getByRole("button", { name: /^展开动作/ }).click();
  await first.getByLabel("全选当前页", { exact: true }).check();
  await first.getByRole("button", { name: new RegExp(`准备取回来源 ${capture.action_instance_id} 的所选`) }).click();
  await page.getByRole("dialog").getByRole("button", { name: "加入草稿", exact: true }).click();
  await check.poll(() => app.store.all<Draft>("drafts").length).toBe(1);
  await check.poll(() => JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions.length).toBe(1);
  expect(JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0].params.output_ids).toEqual(["101", "102"]);
});

it("自动取回归并后百张图片分页，刷新保持选择与页码并验证窄屏", async () => {
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
  delete capture.result;
  const o = capture.outputs![0],
    d = obtain.deliveries![0];
  capture.outputs = Array.from({ length: 100 }, (_, i) => ({
    ...structuredClone(o),
    output_id: String(1000 + i),
    original_name: "图片" + i + ".png",
    media_type: "image/png",
  }));
  obtain.input_params = {
    source: { action_name: capture.name },
    purpose: "auto_preview",
    filter: "preview",
  };
  obtain.automation = {
    purpose: "auto_preview",
    source_action_instance_id: capture.action_instance_id,
  };
  obtain.scheduled_at = capture.scheduled_at;
  obtain.deliveries = Array.from({ length: 100 }, (_, i) => ({
    ...structuredClone(d),
    delivery_id: String(2000 + i),
    output_id: String(1000 + i),
    file_name: String(2000 + i) + ".png",
    display_name: "图片" + i + ".png",
  }));
  capture.outputs.push({
    ...o,
    output_id: "1200",
    media_type: "video/mp4",
    original_name: "独立视频.mp4",
  });
  obtain.deliveries.push({
    ...d,
    delivery_id: "2200",
    output_id: "1200",
    file_name: "2200.mp4",
    display_name: "独立视频.mp4",
  });
  app.applyReports([reportInput(r)]);
  expect(app.coverage(), JSON.stringify(app.state().imports)).toBe(r.to_wm);
  for (let i = 0; i < 14; i++) {
    const f = files.createBatch([
      {
        fileName: String(2000 + i) + ".png",
        size: bytes.length,
        kind: "media",
      },
    ]).files[0];
    await files.upload(f.id, Readable.from([bytes]));
  }
  await files.idle();
  await page.reload();
  await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").first().click();
  const card = page.locator(`[data-action-id="${capture.action_instance_id}"]`);
  await check(
    card.getByRole("button", { name: `展开动作 ${capture.name}`, exact: true }),
  ).toBeVisible();
  await check(page.locator("img[data-thumbnail]")).toHaveCount(0);
  await card
    .getByRole("button", { name: `展开动作 ${capture.name}`, exact: true })
    .click();
  await check(
    page.locator(`[data-action-id="${obtain.action_instance_id}"]`),
  ).not.toBeVisible();
  await check(card.locator("img[data-thumbnail]")).toHaveCount(12);
  if (process.env.CAMCTL_VISUAL_OUTPUT) {
    mkdirSync(process.env.CAMCTL_VISUAL_OUTPUT, { recursive: true });
    await page.screenshot({
      path: join(process.env.CAMCTL_VISUAL_OUTPUT, "task-5-media-desktop.png"),
      fullPage: true,
    });
  }
  await check
    .poll(() =>
      card
        .locator("img[data-thumbnail]")
        .first()
        .evaluate((img: HTMLImageElement) => img.naturalWidth),
    )
    .toBeGreaterThan(0);
  await card
    .locator(":scope > div > .media-results")
    .getByLabel("选择 图片0.png", { exact: true })
    .check();
  await card.getByRole("button", { name: "图片下一页", exact: true }).click();
  await check(card).toContainText("已选择 1");
  const newer = {
    ...r,
    report_id: "2",
    from_wm: r.to_wm,
    to_wm: r.to_wm + 1,
    plans: [{ ...r.plans![0], actions: undefined }],
  };
  app.applyReports([reportInput(newer)]);
  await check(page.locator(".workspace-footer")).toContainText(
    String(newer.to_wm),
  );
  await check(card).toContainText("第 2 / 9 页");
  await card
    .getByRole("button", { name: `折叠动作 ${capture.name}`, exact: true })
    .click();
  await check(page.locator("img[data-thumbnail]")).toHaveCount(0);
  await card
    .getByRole("button", { name: `展开动作 ${capture.name}`, exact: true })
    .click();
  await check(card).toContainText("已选择 1");
  await check(card).toContainText("第 2 / 9 页");
  await check(card.locator("img[data-thumbnail]")).toHaveCount(2);
  await card
    .getByRole("button", { name: "查看图片 图片12.png", exact: true })
    .click();
  await check(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("ArrowLeft");
  await check(page.getByRole("dialog")).toContainText("图片11.png");
  await page.keyboard.press("Escape");
  await check(page.getByRole("dialog")).toHaveCount(0);
  await choose(
    card.locator(":scope > div > .media-results").getByLabel("图片筛选"),
    "ready",
  );
  await check(card.locator("img[data-thumbnail]")).toHaveCount(12);
  await page.setViewportSize({ width: 390, height: 844 });
  await card
    .getByRole("button", { name: "底部图片下一页", exact: true })
    .click();
  await check(card.locator("img[data-thumbnail]")).toHaveCount(2);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  if (process.env.CAMCTL_VISUAL_OUTPUT)
    await page.screenshot({
      path: join(process.env.CAMCTL_VISUAL_OUTPUT, "task-5-media-narrow.png"),
      fullPage: true,
    });
}, 30000);

it("混合设备取回按每个产物的真实拍摄来源显示清理入口", async () => {
  const { app, page } = await setup();
  const report = mappedReport(Buffer.from("mixed-source"));
  const plan = report.plans![0]!;
  const templateCapture = plan.actions![0]!;
  const templateRetrieve = plan.actions![1]!;
  const outputTemplate = templateCapture.outputs![0]!;
  const deliveryTemplate = templateRetrieve.deliveries![0]!;
  const output = (
    outputId: string,
    sourceActionId: string,
    name: string,
    kind: Output["kind"] = "original",
    originalId = "4",
  ): Output => ({
    ...structuredClone(outputTemplate),
    output_id: outputId,
    source_action_instance_id: sourceActionId,
    original_name: name,
    kind,
    ...(kind === "preview" ? { preview_of_output_id: originalId } : {}),
    ...(kind === "repaired" ? { derived_from_output_id: originalId } : {}),
  });
  const capture = (
    id: string,
    name: string,
    deviceId: string | undefined,
    outputs: Output[],
  ): ReportAction => {
    const action: ReportAction = {
      ...structuredClone(templateCapture),
      action_instance_id: id,
      name,
      device_id: deviceId,
      outputs,
      result: { repair: { status: "succeeded" } },
    };
    if (deviceId === undefined) delete action.device_id;
    return action;
  };

  const falseOriginal = output("4", "1", "设备源原片.mp4");
  const falsePreview = output("40", "1", "设备源预览.mp4", "preview");
  const repaired = output("41", "1", "设备源修复成品.mp4", "repaired");
  const trueOriginal = output("5", "3", "主机源原片.mp4");
  const legacyOriginal = output("6", "4", "旧目录原片.mp4");
  const unlistedOriginal = output("7", "5", "未列出设备原片.mp4");
  const historyOriginal = output("8", "6", "历史主机原片.mp4");
  const historyPreview = output("80", "6", "历史主机预览.mp4", "preview", "8");
  const historyRepaired = output(
    "81",
    "6",
    "历史主机修复成品.mp4",
    "repaired",
    "8",
  );
  const captures = [
    capture("1", "设备源拍摄", "device-false", [
      falseOriginal,
      falsePreview,
      repaired,
    ]),
    capture("3", "主机源拍摄", "device-true", [trueOriginal]),
    capture("4", "旧目录拍摄", "device-legacy", [legacyOriginal]),
    capture("5", "未列出设备拍摄", "device-unlisted", [unlistedOriginal]),
    capture("6", "历史主机拍摄", "device-history", [
      historyOriginal,
      historyPreview,
      historyRepaired,
    ]),
  ];
  const displayedOutputs = [
    falseOriginal,
    falsePreview,
    repaired,
    trueOriginal,
    legacyOriginal,
    unlistedOriginal,
    historyOriginal,
    historyPreview,
    historyRepaired,
  ];
  const retrieve: ReportAction = {
    ...structuredClone(templateRetrieve),
    action_instance_id: "2",
    name: "混合来源取回",
    input_params: { source: { plan_instance_id: "1" } },
    result: { failures: [] },
    deliveries: displayedOutputs.map((item, index) => ({
      ...structuredClone(deliveryTemplate),
      delivery_id: String(index + 1),
      output_id: item.output_id,
      source_action_instance_id: item.source_action_instance_id,
      file_name: `${index + 1}.mp4`,
      display_name: item.original_name!,
    })),
  };
  plan.actions = [...captures.slice(0, 1), retrieve, ...captures.slice(1)];
  const capabilityFile = join(app.store.directory, "device-capabilities.json");
  const capabilityCatalog = {
    devices: [
      {
        device_id: "device-false",
        driver_id: "shared-driver",
        delete_supported: false,
        actions: [],
      },
      {
        device_id: "device-true",
        driver_id: "shared-driver",
        delete_supported: true,
        actions: [],
      },
      {
        device_id: "device-legacy",
        driver_id: "shared-driver",
        actions: [],
      },
      {
        device_id: "device-history",
        driver_id: "shared-driver",
        delete_supported: true,
        actions: [],
      },
    ],
  };
  writeFileSync(capabilityFile, JSON.stringify(capabilityCatalog));
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  const guideDevices = page
    .getByTestId("device-guide")
    .locator(".guide-device");
  await check(
    guideDevices.nth(0).locator(".guide-cleanup-support strong"),
  ).toHaveText("不支持删除");
  await check(
    guideDevices.nth(1).locator(".guide-cleanup-support strong"),
  ).toHaveText("支持删除");
  await check(
    guideDevices.nth(2).locator(".guide-cleanup-support strong"),
  ).toHaveText("未知（未声明）");
  app.applyReports([reportInput(report)]);
  expect(app.coverage(), JSON.stringify(app.state().imports)).toBe(
    report.to_wm,
  );
  await page.reload();
  await page.getByRole("tab", { name: /计划记录/ }).click();
  await check(page.getByTestId("record-open-button")).toHaveCount(1);
  await page.getByTestId("record-open-button").first().click();
  await check(
    page.getByRole("button", { name: "清理计划产物" }),
  ).toBeVisible();
  const actionCleanup = async (id: string, name: string) => {
    const card = page.locator(`.result-card[data-action-id="${id}"]`);
    await card.getByRole("button", { name: `展开动作 ${name}` }).click();
    return card.getByRole("button", {
      name: "清理全部产物",
      exact: true,
    });
  };
  await check(await actionCleanup("1", "设备源拍摄")).toHaveCount(0);
  await check(await actionCleanup("3", "主机源拍摄")).toBeVisible();
  await check(await actionCleanup("4", "旧目录拍摄")).toBeVisible();
  await check(await actionCleanup("5", "未列出设备拍摄")).toBeVisible();
  await check(await actionCleanup("6", "历史主机拍摄")).toBeVisible();
  const retrieval = page.locator('.result-card[data-action-id="2"]');
  await retrieval
    .getByRole("button", { name: "展开动作 混合来源取回" })
    .click();
  await check(retrieval.locator(".product-card")).toHaveCount(9);
  expect(
    await retrieval.locator(".product-name strong").allTextContents(),
  ).toEqual([
    "设备源原片.mp4",
    "设备源预览.mp4",
    "设备源修复成品.mp4",
    "主机源原片.mp4",
    "旧目录原片.mp4",
    "未列出设备原片.mp4",
    "历史主机原片.mp4",
    "历史主机预览.mp4",
    "历史主机修复成品.mp4",
  ]);
  const cleanup = async (index: number) => {
    const card = retrieval.locator(".product-card").nth(index);
    return card.getByRole("button", { name: "清理产物", exact: true });
  };
  const outputAvailability = (index: number) =>
    retrieval
      .locator(".product-card")
      .nth(index)
      .locator(".product-actions .badge");
  await check(await cleanup(0)).toHaveCount(0);
  await check(await cleanup(1)).toHaveCount(0);
  await check(outputAvailability(0)).toHaveCount(0);
  await check(outputAvailability(1)).toHaveCount(0);
  await check(await cleanup(2)).toBeVisible();
  await check(await cleanup(3)).toBeVisible();
  await check(await cleanup(4)).toBeVisible();
  await check(await cleanup(5)).toBeVisible();
  await check(await cleanup(6)).toBeVisible();
  await check(await cleanup(7)).toBeVisible();
  await check(await cleanup(8)).toBeVisible();

  const reloadAndOpenRecord = async (deleteSupported: boolean) => {
    capabilityCatalog.devices[0]!.delete_supported = deleteSupported;
    capabilityCatalog.devices[3]!.delete_supported = !deleteSupported;
    writeFileSync(capabilityFile, JSON.stringify(capabilityCatalog));
    await page.getByTestId("nav-devices").click();
    await page.getByRole("button", { name: "重新加载能力说明" }).click();
    const currentGuide = page
      .getByTestId("device-guide")
      .locator(".guide-device");
    await check(
      currentGuide.nth(0).locator(".guide-cleanup-support strong"),
    ).toHaveText(deleteSupported ? "支持删除" : "不支持删除");
    await check(
      currentGuide.nth(3).locator(".guide-cleanup-support strong"),
    ).toHaveText(deleteSupported ? "不支持删除" : "支持删除");
    await page.getByTestId("nav-plans").click();
    await page.getByTestId("tab-records").click();
    await page.getByTestId("record-open-button").first().click();
    await retrieval
      .getByRole("button", { name: "展开动作 混合来源取回" })
      .click();
  };
  await reloadAndOpenRecord(true);
  await check(await actionCleanup("1", "设备源拍摄")).toBeVisible();
  await check(await actionCleanup("6", "历史主机拍摄")).toHaveCount(0);
  await check(await cleanup(0)).toBeVisible();
  await check(await cleanup(1)).toBeVisible();
  await check(await cleanup(2)).toBeVisible();
  await check(await cleanup(6)).toHaveCount(0);
  await check(await cleanup(7)).toHaveCount(0);
  await check(await cleanup(8)).toBeVisible();
  await reloadAndOpenRecord(false);
  await check(await actionCleanup("1", "设备源拍摄")).toHaveCount(0);
  await check(await actionCleanup("6", "历史主机拍摄")).toBeVisible();
  await check(await cleanup(0)).toHaveCount(0);
  await check(await cleanup(1)).toHaveCount(0);
  await check(await cleanup(2)).toBeVisible();
  await check(await cleanup(6)).toBeVisible();
  await check(await cleanup(7)).toBeVisible();
  await check(await cleanup(8)).toBeVisible();
}, 30000);

it("旧清理动作仍能编辑并按原参数导出", async () => {
  const { app, page } = await setup();
  const original = {
    name: "历史清理",
    actions: [
      {
        name: "旧拍摄",
        type: "camera_record",
        device_id: "demo_cam0",
        scheduled_at: "2026-09-16 08:00:00",
        params: { type: "demo_fixed" },
        policy: { max_delay_ms: 0 },
      },
      {
        name: "旧清理",
        type: "delete_action_outputs",
        scheduled_at: "2026-09-16 09:00:00",
        params: { source: { action_name: "旧拍摄" } },
      },
    ],
  };
  app.createDraft({ text: JSON.stringify(original) });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  const cleanup = page.locator(".action-card").nth(1);
  const actionType = cleanup.getByLabel("动作类型", { exact: true });
  await check(actionType).toHaveAttribute(
    "data-value",
    "delete_action_outputs",
  );
  expect(
    await readOptions(actionType).then((options) =>
      options.find((option) => option.value === "delete_action_outputs"),
    ),
  ).toMatchObject({ disabled: false, selected: true });
  await check(cleanup.getByLabel("来源动作名称", { exact: true })).toHaveValue(
    "旧拍摄",
  );
  await page.getByTestId("export-button").click();
  await check
    .poll(() => app.store.all<Record<string, unknown>>("requests").length)
    .toBe(1);
  const request = app.store.all<{
    body: { actions: Array<Record<string, unknown>> };
  }>("requests")[0]!;
  expect(request.body.actions[1]).toMatchObject({
    type: "delete_action_outputs",
    params: { source: { action_name: "旧拍摄" } },
  });
}, 20000);

it("照片与延时表单按能力显示，切换保留参数并导出当前类型", async () => {
  const { app, page } = await setup();
  copyFileSync(
    "../../protocol/examples/capabilities/demo-capture-devices.json",
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
  await check(page.getByLabel("照片尺寸 (resolution)")).toHaveAttribute(
    "data-value",
    "0",
  );
  await choose(
    page.getByLabel("动作类型", { exact: true }),
    "camera_timelapse",
  );
  await choose(page.getByLabel("目标设备", { exact: true }), "demo_cam0");
  await choose(page.getByLabel("参数类型", { exact: true }), "demo_count");
  await page.getByLabel("拍摄张数 (count)").fill("100");
  await page.getByLabel("间隔（秒） (interval_s)").fill("5");
  await page.getByLabel("最大允许延迟 (max_delay_ms)").fill("0");
  await choose(
    page.getByLabel("动作类型", { exact: true }),
    "camera_take_photo",
  );
  await check(page.getByLabel("照片尺寸 (resolution)")).toHaveAttribute(
    "data-value",
    "0",
  );
  await choose(
    page.getByLabel("动作类型", { exact: true }),
    "camera_timelapse",
  );
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
