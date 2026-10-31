import { beforeAll, afterAll, afterEach, expect, it } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import {
  copyFileSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";

let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({
    headless: true,
    channel: process.env.CAMCTL_TEST_BROWSER_CHANNEL,
  });
});
afterAll(async () => {
  await browser?.close();
});
afterEach(async () => {
  for (const fn of clean.splice(0)) await fn();
});

async function setup(width: number) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-editor-response-"));
  const capabilitiesFile = join(directory, "device-capabilities.json");
  copyFileSync(
    "../../protocol/examples/capabilities/demo-device.json",
    capabilitiesFile,
  );
  const app = new Application(directory);
  app.store.initialize();
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "输入响应",
      actions: [
        {
          name: "录像",
          type: "camera_record",
          device_id: "demo_cam0",
          params: { type: "demo_fixed" },
          policy: { max_delay_ms: 0 },
          scheduled_at: "2026-10-25 00:00:00",
        },
      ],
    }),
  });
  const files = new Files(app),
    requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const context = await browser.newContext({
    viewport: { width, height: 1244 },
  });
  const page = await context.newPage();
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((e) => (e ? reject(e) : resolve())),
      ),
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
  await page.getByTestId("draft-open-button").click();
  return { page, app, draft, capabilitiesFile };
}

it.each([1416, 390])(
  "%i 像素视口在输入、保存和轮询时保持表单位置且写入完整输入",
  async (width) => {
    const { page, app, draft } = await setup(width);
    const input = page.getByLabel("计划名称", { exact: true });
    const position = () =>
      input.evaluate(
        (element) => element.getBoundingClientRect().top + window.scrollY,
      );
    await check(page.getByTestId("save-status")).toHaveText("已保存");
    const savedY = await position();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let started!: () => void;
    const saving = new Promise<void>((resolve) => {
      started = resolve;
    });
    await page.route("**/api/drafts/**", async (route) => {
      if (route.request().method() === "PUT") {
        started();
        await gate;
      }
      await route.continue();
    });
    try {
      await input.fill("输入响应已修改");
      await check(
        page.getByRole("button", { name: "保存当前输入", exact: true }),
      ).toHaveCount(0);
      const dirtyY = await position();
      await saving;
      const savingY = await position();
      release();
      await check(page.getByTestId("save-status")).toHaveText("已保存");
      await expect
        .poll(() => JSON.parse(app.draft(draft.id).content.text).name)
        .toBe("输入响应已修改");
      const response = page.waitForResponse((r) =>
        r.url().endsWith("/api/state"),
      );
      await response;
      expect([dirtyY, savingY, await position()]).toEqual([
        savedY,
        savedY,
        savedY,
      ]);
    } finally {
      release();
    }
  },
);

it("轮询消费新的能力版本后重新校验当前输入", async () => {
  const { page, app, capabilitiesFile } = await setup(1416);
  await check(page.getByTestId("validation-issues")).toHaveCount(0);
  const capabilities = JSON.parse(readFileSync(capabilitiesFile, "utf8"));
  const fixed = capabilities.devices[0].actions[0].parameter_types.find(
    (p: { type: string }) => p.type === "demo_fixed",
  );
  fixed.schema.required.push("new_required_field");
  fixed.schema.properties.new_required_field = { type: "string" };
  writeFileSync(capabilitiesFile, JSON.stringify(capabilities));
  app.reloadCapabilities();
  await check(page.getByLabel("参数类型", { exact: true })).toBeVisible();
  await check(page.getByTestId("validation-issues")).toHaveCount(0);
  await check(page.locator('[aria-invalid="true"]')).not.toHaveCount(0);
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("validation-issues")).toBeVisible();
  await check(page.getByTestId("validation-issues")).toContainText(
    "new_required_field",
  );
});
