import { afterAll, afterEach, beforeAll, expect, it } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { Readable } from "node:stream";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import type { Video } from "../../src/server/models";
import { mediaSnapshot } from "../helpers/media-snapshot";
import { reportInput } from "./fixtures";

let browser: Browser;
let mp4: Buffer;
const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1cAAAAASUVORK5CYII=",
  "base64",
);
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({
    headless: true,
    channel: process.env.CAMCTL_TEST_BROWSER_CHANNEL,
  });
  mp4 = readFileSync(
    new URL("../fixtures/media/local-verification.mp4", import.meta.url),
  );
  expect(mp4.subarray(4, 8).toString()).toBe("ftyp");
}, 20000);
afterAll(async () => {
  await browser?.close();
});
afterEach(async () => {
  for (const cleanup of clean.splice(0)) await cleanup();
});

async function setup(
  bytes: Buffer,
  fileName: string,
  mediaType?: string,
  displayName = fileName,
  availability: "available" | "cleaned" | "unknown" = "available",
) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-media-download-"));
  const snapshot = mediaSnapshot(bytes, fileName, mediaType);
  snapshot.plans![0].actions![1].deliveries![0].display_name = displayName;
  const output = snapshot.plans![0].actions![0].outputs![0];
  output.availability = availability;
  if (availability === "cleaned") output.cleanup = { status: "completed" };
  if (availability === "unknown")
    output.error = { code: "future_code", stage: "execution", details: {} };
  const application = new Application(directory);
  expect(application.store.initialize().state).toBe("ready");
  application.applyReports([reportInput(snapshot)]);
  expect(
    application.coverage(),
    JSON.stringify(application.state().imports),
  ).toBe(snapshot.to_wm);
  const files = new Files(application);
  const uploaded = files.createBatch([
    { fileName, size: bytes.length, kind: "media" },
  ]).files[0];
  await files.upload(uploaded.id, Readable.from([bytes]));
  await files.idle();
  const video = files.videos()[0];
  expect(video.status).toBe("verified");
  const server = createHttpApp(application, files).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const context = await browser.newContext({ acceptDownloads: true });
  clean.push(async () => {
    await context.close();
    await files.idle();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
    application.store.close();
    rmSync(directory, { recursive: true, force: true });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  await page.goto(base);
  await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").click();
  const card = page.locator(".result-card").filter({
    has: page.getByRole("heading", { name: "拍摄", exact: true }),
  });
  await card
    .getByRole("button", { name: "展开动作 拍摄", exact: true })
    .click();
  return { page, card, base, video, snapshot, application, files };
}

it.each([
  ["bin", "application/octet-stream", "下载"],
  ["png", "image/png", "下载"],
  ["mp4", "video/mp4", "下载"],
] as const)(
  "产物卡片下载 %s 保留文件名和完整字节",
  async (extension, type, label) => {
    const bytes =
      extension === "png"
        ? png
        : extension === "mp4"
          ? mp4
          : Buffer.from("original");
    const fileName = `1.${extension}`;
    const displayName = `原片-媒体查看-拍摄.${extension}`;
    const { page, card, base, video } = await setup(
      bytes,
      fileName,
      type,
      displayName,
    );
    const pending = page.waitForEvent("download");
    await card.getByRole("button", { name: label, exact: true }).click();
    const downloaded = await pending;
    expect(downloaded.suggestedFilename()).toBe(displayName);
    expect(readFileSync((await downloaded.path())!)).toEqual(bytes);
    const response = await fetch(
      `${base}/api/media/${video.id}/content?download=true`,
    );
    expect(
      decodeURIComponent(response.headers.get("content-disposition")!),
    ).toContain(displayName);
    expect(Buffer.from(await response.arrayBuffer())).toEqual(bytes);
  },
);

it("大图下载使用对应交付的可读名称并保留完整字节", async () => {
  const { page, card } = await setup(
    png,
    "1.png",
    "image/png",
    "照片-媒体查看-拍摄.png",
  );
  await card
    .getByRole("button", { name: "查看图片 1.png", exact: true })
    .click();
  const button = page
    .getByRole("dialog")
    .getByRole("button", { name: "下载", exact: true });
  const pending = page.waitForEvent("download");
  await button.click();
  const downloaded = await pending;
  expect(downloaded.suggestedFilename()).toBe("照片-媒体查看-拍摄.png");
  expect(readFileSync((await downloaded.path())!)).toEqual(png);
});

it("产物只展示一行操作与一个技术详情入口", async () => {
  const { card: action, page } = await setup(png, "1.png", "image/png");
  const card = action.locator(".product-card");
  await check(
    card.getByRole("button", { name: "下载", exact: true }),
  ).toBeVisible();
  await check(
    card.getByRole("button", { name: "清理产物", exact: true }),
  ).toBeVisible();
  await check(
    card.getByRole("button", { name: "取回", exact: true }),
  ).toHaveCount(0);
  await check(card.locator(".badge").filter({ hasText: /^可用$/ })).toHaveCount(
    0,
  );
  await card.locator("details > summary").click();
  await check(card.locator("details > summary")).toHaveCount(1);
  await check(card.locator("details button")).toHaveCount(0);
  await check(card.locator("details")).toContainText("1.png");
  await check(card.locator(".delivery .facts")).toBeVisible();
  const facts = card.locator(".output-facts");
  await check(facts).toContainText("源文件 SHA-256");
  await check(facts).toContainText("主机媒体检查");
  await check(facts).toContainText("媒体时长");
  await check(facts.locator(".facts")).toHaveCount(0);
  await check(facts.locator("dt").filter({ hasText: /^状态$/ })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await check(facts.getByText("源文件 SHA-256", { exact: true })).toBeVisible();
  expect(
    await facts.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);
});

it.each(["cleaned", "unknown"] as const)(
  "主机产物为 %s 时保留本地下载并显示状态",
  async (availability) => {
    const { card } = await setup(
      png,
      "1.png",
      "image/png",
      "照片-媒体查看-拍摄.png",
      availability,
    );
    await check(
      card.getByRole("button", { name: "下载", exact: true }),
    ).toBeVisible();
    await check(
      card.getByRole("button", { name: "清理产物", exact: true }),
    ).toHaveCount(0);
    await check(card.locator(".product-actions .badge")).toHaveText(
      availability === "cleaned" ? "已清理" : "未知",
    );
  },
);

it.each([
  ["MP4", undefined],
  ["mp4", "application/octet-stream"],
  ["LRF", undefined],
  ["oSv", "application/octet-stream"],
] as const)(
  "%s 文件可直接播放、按范围读取并下载原文件",
  async (extension, type) => {
    const fileName = `1.${extension}`;
    const { page, card, base, snapshot, video } = await setup(
      mp4,
      fileName,
      type,
    );
    const player = card.locator("video");
    await check(player).toBeVisible();
    await check
      .poll(() =>
        player.evaluate((video: HTMLVideoElement) => video.readyState),
      )
      .toBeGreaterThanOrEqual(2);
    await player.evaluate((video: HTMLVideoElement) => video.play());
    await check
      .poll(() =>
        player.evaluate((video: HTMLVideoElement) => video.currentTime),
      )
      .toBeGreaterThan(0);
    const ranged = await fetch(`${base}/api/media/${video.id}/content`, {
      headers: { Range: "bytes=0-15" },
    });
    expect(ranged.status).toBe(206);
    expect(ranged.headers.get("content-type")).toContain("video/mp4");
    expect(ranged.headers.get("content-disposition")).toBe("inline");
    expect(Buffer.from(await ranged.arrayBuffer())).toEqual(
      mp4.subarray(0, 16),
    );
    const pending = page.waitForEvent("download");
    await card.getByRole("button", { name: "下载", exact: true }).click();
    const downloaded = await pending;
    expect(downloaded.suggestedFilename()).toBe(fileName);
    expect(readFileSync((await downloaded.path())!)).toEqual(mp4);
    expect(snapshot.plans![0].actions![0].outputs![0].media_type).toBe(type);
  },
);

it("未核验的 LRF 不开放预览和下载", async () => {
  const { page, card, base, video, application } = await setup(mp4, "1.LRF");
  const saved = application.store.get<Video>("videos", video.fileName)!;
  application.store.set("videos", video.fileName, {
    ...saved,
    status: "verifying",
  });
  await page.reload();
  await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").click();
  await card
    .getByRole("button", { name: "展开动作 拍摄", exact: true })
    .click();
  await check(card.locator("video")).toHaveCount(0);
  await check(card.getByRole("button", { name: /^下载原/ })).toHaveCount(0);
  const response = await fetch(
    `${base}/api/media/${video.id}/content?download=true`,
  );
  expect(response.status).toBe(409);
  expect((await response.json()).code).toBe("video_not_verified");
});

it("LRF 无法解码时仍可下载核验通过的原文件", async () => {
  const bytes = Buffer.from("unsupported video contents");
  const { page, card, files } = await setup(bytes, "1.LRF");
  await check(card.getByRole("alert")).toBeVisible();
  await check(card.locator("video")).toHaveCount(0);
  expect(files.videos()[0].status).toBe("verified");
  const pending = page.waitForEvent("download");
  await card.getByRole("button", { name: "下载", exact: true }).click();
  const downloaded = await pending;
  expect(downloaded.suggestedFilename()).toBe("1.LRF");
  expect(readFileSync((await downloaded.path())!)).toEqual(bytes);
});
