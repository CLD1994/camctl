import { pendingInput } from "./pending-support";
import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, rmSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import { mappedReport, reportInput } from "./fixtures";

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
  const directory = mkdtempSync(join(tmpdir(), "camctl-web-navigation-"));
  copyFileSync(
    "../../protocol/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"),
  );
  const app = new Application(directory),
    files = new Files(app),
    requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const context = await browser.newContext(),
    page = await context.newPage();
  page.setDefaultTimeout(5000);
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
  await page.getByTestId("initialize-button").click();
  return { app, page };
}

// 删除弹窗焦点管理或 Escape 处理时，此测试应失败。
it("同步弹窗限制键盘焦点，Escape 返回本次入口", async () => {
  const { page } = await setup();
  const entry = page.getByRole("button", { name: "准备状态同步", exact: true });
  await entry.click();
  const dialog = page.getByRole("dialog");
  await check(dialog).toBeVisible();
  await check
    .poll(() =>
      dialog.evaluate((element) => element.contains(document.activeElement)),
    )
    .toBe(true);
  for (const key of ["Tab", "Shift+Tab"]) {
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press(key);
      await check
        .poll(() =>
          dialog.evaluate((element) =>
            element.contains(document.activeElement),
          ),
        )
        .toBe(true);
    }
  }
  await page.locator(".modal-backdrop").click({ position: { x: 5, y: 5 } });
  await check(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await check(dialog).toHaveCount(0);
  await check(entry).toBeFocused();
});

it("记录后续入口共用弹窗，选择列表先处理 Escape", async () => {
  const { app, page } = await setup();
  app.applyReports([reportInput(mappedReport(Buffer.from("video")))]);
  await page.reload();
  await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").first().click();
  const entry = page.getByRole("button", {
    name: "准备取回计划默认产物",
    exact: true,
  });
  await entry.click();
  const dialog = page.getByRole("dialog");
  await check
    .poll(() =>
      dialog.evaluate((element) => element.contains(document.activeElement)),
    )
    .toBe(true);
  const select = dialog.getByLabel("目标草稿");
  await select.click();
  await check(page.getByRole("listbox")).toBeVisible();
  await page.keyboard.press("Escape");
  await check(page.getByRole("listbox")).toHaveCount(0);
  await check(dialog).toBeVisible();
  await check(select).toBeFocused();
  await page.keyboard.press("Escape");
  await check(dialog).toHaveCount(0);
  await check(entry).toBeFocused();
});

it("挂起追加期间禁止关闭和重复提交，成功后聚焦当前草稿入口", async () => {
  const { app, page } = await setup();
  app.applyReports([reportInput(mappedReport(Buffer.from("video")))]);
  await page.reload();
  await page.getByRole("tab", { name: /计划记录/ }).click();
  await page.getByTestId("record-open-button").first().click();
  let appends = 0,
    release!: () => void;
  const response = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/drafts/*/actions", async (route) => {
    appends++;
    await response;
    await route.continue();
  });
  try {
    await page
      .getByRole("button", { name: "准备取回计划默认产物", exact: true })
      .click();
    const dialog = page.getByRole("dialog");
    const submit = dialog.getByRole("button", {
      name: "加入草稿",
      exact: true,
    });
    await check(submit).toBeEnabled();
    await submit.click();
    await check.poll(() => appends).toBe(1);
    await page.keyboard.press("Escape");
    await page.locator(".modal-backdrop").click({ position: { x: 5, y: 5 } });
    await check(dialog).toBeVisible();
    const retry = dialog.locator("button.primary");
    await check(retry).toBeDisabled();
    await retry.evaluate((element) => (element as HTMLButtonElement).click());
    expect(appends).toBe(1);
    release();
    await check(dialog).toHaveCount(0);
    await check(page.getByRole("tab", { name: /草稿/ })).toBeFocused();
    expect(app.store.all("drafts")).toHaveLength(1);
    expect(appends).toBe(1);
  } finally {
    release();
  }
});

it("未知追加关闭后保留同一核实操作，恢复时不重新追加", async () => {
  const { app, page } = await setup();
  let offline = false,
    creates = 0,
    appends = 0;
  await page.route("**/api/state", (route) =>
    offline ? route.abort() : route.continue(),
  );
  await page.route("**/api/drafts", async (route) => {
    if (route.request().method() === "POST") creates++;
    await route.continue();
  });
  await page.route("**/api/drafts/*/actions", async (route) => {
    appends++;
    await route.fetch();
    offline = true;
    await route.abort();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(
    page.getByRole("button", { name: "重新核实追加结果", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await check(page.getByRole("dialog")).toHaveCount(0);
  const resume = page.getByRole("button", {
    name: "返回核实后续操作",
    exact: true,
  });
  await check(resume).toBeVisible();
  offline = false;
  await resume.click();
  await page
    .getByRole("button", { name: "重新核实追加结果", exact: true })
    .click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  expect(creates).toBe(1);
  expect(appends).toBe(1);
  expect(app.store.all("drafts")).toHaveLength(1);
});

// 去掉页签导航或面板身份关联时，此测试应失败；导航不修改未完成输入。
it("页签方向键与 Home End 激活内容并保持草稿原输入", async () => {
  const { app, page } = await setup();
  const content = {
    text: '{"name":"未完成计划","actions":[{"name":"录像","type":"camera_record","device_id":"demo_cam0","params":{"type":"demo_fixed"},"policy":{"max_delay_ms":0}}]}',
    pending: {
      "/actions/0/policy/max_delay_ms": { kind: "number" as const, text: "1e" },
    },
  };
  const draft = app.createDraft(content);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  const drafts = page.getByRole("tab", { name: /草稿/ });
  const records = page.getByRole("tab", { name: /计划记录/ });
  await drafts.focus();
  for (const [key, target] of [
    ["ArrowRight", records],
    ["ArrowLeft", drafts],
    ["End", records],
    ["Home", drafts],
  ] as const) {
    await page.keyboard.press(key);
    await check(target).toBeFocused();
    await check(target).toHaveAttribute("aria-selected", "true");
    expect(
      await page
        .getByRole("tab")
        .evaluateAll(
          (elements) =>
            elements.filter((e) => (e as HTMLElement).tabIndex === 0).length,
        ),
    ).toBe(1);
    const panelId = await target.getAttribute("aria-controls");
    expect(panelId).toBeTruthy();
    const panel = page.locator(
      `[role="tabpanel"][id=${JSON.stringify(panelId)}]`,
    );
    await check(panel).toBeVisible();
    await check(panel).toHaveAttribute(
      "aria-labelledby",
      (await target.getAttribute("id"))!,
    );
    await check(page.locator('[role="tabpanel"][inert]')).toBeHidden();
  }
  await check(pendingInput(page, "/actions/0/policy/max_delay_ms")).toHaveValue(
    "1e",
  );
  expect(app.draft(draft.id).content).toEqual(content);
});
