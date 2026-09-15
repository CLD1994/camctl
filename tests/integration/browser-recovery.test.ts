import { afterEach, expect, it } from "vitest";
import { chromium, expect as browserExpect } from "@playwright/test";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Readable } from "node:stream";
import type { Server } from "node:http";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { RequestLifecycle, createStop } from "../../src/server/lifecycle";
import { mappedReport, reportInput } from "./fixtures";

const cleanup: Array<() => Promise<void>> = [];
afterEach(async () => {
  for (const close of cleanup.splice(0)) await close();
});
async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-browser-recovery-"));
  const app = new Application(directory);
  app.store.initialize();
  const files = new Files(app);
  const lifecycle = new RequestLifecycle();
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(app, files, lifecycle).listen(0, "127.0.0.1", () =>
      resolve(s),
    );
  });
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((e) => (e ? reject(e) : resolve())),
      ),
    lifecycle,
    () => files.idle(),
    () => app.store.close(),
  );
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  cleanup.push(async () => {
    await browser.close();
    await stop();
    rmSync(directory, { recursive: true, force: true });
  });
  await page.goto(
    `http://127.0.0.1:${(server.address() as { port: number }).port}`,
  );
  return { app, files, page };
}
it("网页保留缺口提示直到20→40→45完整补齐，并选择可靠同步起点", async () => {
  const { app, page } = await setup();
  const directory = resolve(
    "docs/superpowers/specs/camctl/examples/status-sync",
  );
  const report = (id: number) =>
    join(
      directory,
      readdirSync(directory).find((name) =>
        name.startsWith(`status-report-${id}-`),
      )!,
    );
  await page.getByTestId("nav-import").click();
  await page.getByTestId("import-files").setInputFiles(report(1));
  await browserExpect.poll(() => app.coverage()).toBe(20);
  await page.getByTestId("import-files").setInputFiles(report(3));
  await browserExpect(
    page.getByText("主机历史尚未补齐", { exact: true }),
  ).toBeVisible();
  expect(app.coverage()).toBe(20);
  await page.getByRole("button", { name: "准备补齐历史", exact: true }).click();
  await browserExpect(
    page.getByText("将从已完整保存的报告 1 之后补齐。", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "返回", exact: true }).click();
  await page.getByTestId("import-files").setInputFiles(report(2));
  await browserExpect.poll(() => app.coverage()).toBe(40);
  await browserExpect(
    page.getByText("主机历史尚未补齐", { exact: true }),
  ).toBeVisible();
  await page.getByTestId("import-files").setInputFiles(report(3));
  await browserExpect.poll(() => app.coverage()).toBe(45);
  await browserExpect(
    page.getByText("主机历史尚未补齐", { exact: true }),
  ).toHaveCount(0);
  expect(
    app.state().imports?.filter((file) => file.status === "gap"),
  ).toHaveLength(1);
}, 20000);
it("核验通过但媒体不能解码时保留原视频下载和核验结果", async () => {
  const { app, files, page } = await setup();
  const bytes = Buffer.from(
    "This file deliberately has no supported video encoding.",
  );
  app.applyReports([reportInput(mappedReport(bytes))]);
  const file = files.createBatch([
    { fileName: "d-001.mp4", kind: "video", size: bytes.length },
  ]).files[0];
  await files.upload(file.id, Readable.from([bytes]));
  await files.idle();
  await page.reload();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  await browserExpect(
    page.getByRole("alert").filter({ hasText: "无法在浏览器播放" }).first(),
  ).toBeVisible();
  await browserExpect(
    page.getByRole("button", { name: "下载原视频", exact: true }).first(),
  ).toBeEnabled();
  expect(files.videos()[0].status).toBe("verified");
  expect(app.snapshot().plans![0].status).toBe("completed");
}, 20000);
