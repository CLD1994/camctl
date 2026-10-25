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
  const page = await browser.newPage();
  try {
    mp4 = Buffer.from(
      await page.evaluate(async () => {
        const canvas = document.createElement("canvas");
        canvas.width = 480;
        canvas.height = 270;
        document.body.append(canvas);
        const context = canvas.getContext("2d")!;
        const stream = canvas.captureStream(10);
        const recorder = new MediaRecorder(stream, { mimeType: "video/mp4" });
        const chunks: Blob[] = [];
        const done = new Promise<Blob>((resolve) => {
          recorder.ondataavailable = (event) => chunks.push(event.data);
          recorder.onstop = () =>
            resolve(new Blob(chunks, { type: "video/mp4" }));
        });
        recorder.start();
        for (let frame = 0; frame < 30; frame++) {
          context.fillStyle = frame % 2 ? "blue" : "green";
          context.fillRect(0, 0, canvas.width, canvas.height);
          await new Promise((resolve) => setTimeout(resolve, 85));
        }
        recorder.stop();
        const blob = await done;
        const bytes = await blob.arrayBuffer();
        stream.getTracks().forEach((track) => track.stop());
        return Array.from(new Uint8Array(bytes));
      }),
    );
    expect(mp4.length).toBeGreaterThan(0);
    expect(mp4.subarray(4, 8).toString()).toBe("ftyp");
  } finally {
    await page.close();
  }
}, 20000);
afterAll(async () => {
  await browser?.close();
});
afterEach(async () => {
  for (const cleanup of clean.splice(0)) await cleanup();
});

async function setup(bytes: Buffer, fileName: string, mediaType?: string) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-media-download-"));
  const snapshot = mediaSnapshot(bytes, fileName, mediaType);
  const application = new Application(directory);
  expect(application.store.initialize().state).toBe("ready");
  application.applyReports([reportInput(snapshot)]);
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
  ["bin", "application/octet-stream", "下载原文件"],
  ["png", "image/png", "下载原图片"],
  ["mp4", "video/mp4", "下载原视频"],
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
    const { page, card } = await setup(bytes, fileName, type);
    const pending = page.waitForEvent("download");
    await card.getByRole("button", { name: label, exact: true }).click();
    const downloaded = await pending;
    expect(downloaded.suggestedFilename()).toBe(fileName);
    expect(readFileSync((await downloaded.path())!)).toEqual(bytes);
  },
);

it.each(["交付记录", "大图弹窗"])("%s 下载图片保留完整字节", async (entry) => {
  const { page, card } = await setup(png, "1.png", "image/png");
  if (entry === "交付记录")
    await card.getByText("更多操作与交付记录", { exact: true }).click();
  else
    await card
      .getByRole("button", { name: "查看图片 1.png", exact: true })
      .click();
  const button =
    entry === "交付记录"
      ? card.getByRole("button", { name: "下载此次交付", exact: true })
      : page
          .getByRole("dialog")
          .getByRole("button", { name: "下载原图片", exact: true });
  const pending = page.waitForEvent("download");
  await button.click();
  const downloaded = await pending;
  expect(downloaded.suggestedFilename()).toBe("1.png");
  expect(readFileSync((await downloaded.path())!)).toEqual(png);
});

it.each([
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
    await card.getByRole("button", { name: "下载原视频", exact: true }).click();
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
  await card.getByRole("button", { name: "下载原视频", exact: true }).click();
  const downloaded = await pending;
  expect(downloaded.suggestedFilename()).toBe("1.LRF");
  expect(readFileSync((await downloaded.path())!)).toEqual(bytes);
});
