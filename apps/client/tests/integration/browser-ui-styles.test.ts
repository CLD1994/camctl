import { afterAll, afterEach, beforeAll, expect, it } from "vitest";
import {
  chromium,
  expect as browserExpect,
  type Browser,
  type Page,
} from "@playwright/test";
import { copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";

let browser: Browser;
const clean: Array<() => Promise<void>> = [];

beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
});
afterAll(async () => {
  await browser?.close();
});
afterEach(async () => {
  for (const cleanup of clean.splice(0)) await cleanup();
});

async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-browser-ui-styles-"));
  copyFileSync(
    "../../protocol/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"),
  );
  const application = new Application(directory);
  const files = new Files(application);
  const lifecycle = new RequestLifecycle();
  const server = createHttpApp(application, files, lifecycle).listen(
    0,
    "127.0.0.1",
  );
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
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
  const response = await page.goto(
    `http://127.0.0.1:${(server.address() as { port: number }).port}`,
  );
  expect(response?.status()).toBe(200);
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("nav-import").click();
  return { application, page };
}

async function focusFileWithKeyboard(page: Page) {
  await page.getByTestId("nav-devices").focus();
  await page.keyboard.press("Tab");
  await browserExpect(page.getByTestId("import-files")).toBeFocused();
}

it("键盘聚焦透明文件输入时，可见选择按钮显示焦点边界", async () => {
  const { page } = await setup();
  const button = page.locator(".file-button");
  await browserExpect(button).toHaveCSS("outline-style", "none");
  await focusFileWithKeyboard(page);
  await browserExpect(button).not.toHaveCSS("outline-style", "none");
  expect(
    await button.evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).outlineWidth),
    ),
  ).toBeGreaterThan(0);
  await page.keyboard.press("Shift+Tab");
  await browserExpect(button).toHaveCSS("outline-style", "none");
});

it("文件输入只获得焦点或返回空选择时不创建导入或上传内容", async () => {
  const { application, page } = await setup();
  const importRequests: string[] = [];
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (
      (request.method() === "POST" && pathname === "/api/batches") ||
      (request.method() === "PUT" &&
        /^\/api\/imports\/.+\/content$/.test(pathname))
    )
      importRequests.push(`${request.method()} ${pathname}`);
  });
  await focusFileWithKeyboard(page);
  expect(importRequests).toEqual([]);
  const chooser = page.waitForEvent("filechooser");
  await page.getByTestId("import-files").click();
  await (await chooser).setFiles([]);
  await page.keyboard.press("Shift+Tab");
  await page.getByTestId("nav-import").click();
  expect(importRequests).toEqual([]);
  expect(application.store.all("imports")).toEqual([]);
  expect(application.store.all("batches")).toEqual([]);
});
