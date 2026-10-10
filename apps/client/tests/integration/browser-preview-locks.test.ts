import { beforeAll, afterAll, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { createServer, type ViteDevServer } from "vite";
import type { PreviewLock } from "./preview-lock-fixture";

let browser: Browser;
let server: ViteDevServer;
let base: string;
beforeAll(async () => {
  server = await createServer({
    configFile: false,
    server: { host: "127.0.0.1", port: 0 },
    plugins: [
      {
        name: "preview-lock-page",
        configureServer(vite) {
          vite.middlewares.use(async (request, response, next) => {
            if (request.url !== "/preview-lock-fixture") {
              next();
              return;
            }
            response.setHeader("Content-Type", "text/html");
            response.end(
              await vite.transformIndexHtml(
                "/preview-lock-fixture",
                '<!doctype html><html lang="zh"><div id="root"></div><script type="module" src="/tests/integration/preview-lock-fixture.tsx"></script></html>',
              ),
            );
          });
        },
      },
    ],
  });
  await server.listen();
  base = `http://127.0.0.1:${(server.httpServer!.address() as { port: number }).port}`;
  browser = await chromium.launch({ headless: true });
}, 30000);
afterAll(async () => {
  await browser?.close();
  await server?.close();
});

const locks: PreviewLock[] = [
  "busy",
  "exporting",
  "export-unknown",
  "exported",
  "deleting",
  "delete-unknown",
  "deleted",
  "append",
  "identity",
];
it.each(locks)("真实Editor %s锁态禁用三态入口且零写入", async (lock) => {
  const page = await browser.newPage();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(`${base}/preview-lock-fixture`);
    await check(page.getByTestId("preview-intent")).toBeVisible();
    for (const intent of ["unset", "enabled", "disabled"] as const) {
      // 身份问题由无资料的当前集合改变引发；已知资料场景用同一故障集合。
      await page.evaluate(
        (value) => window.previewLockFixture.reset(value),
        intent,
      );
      await page.evaluate(
        (state) => window.previewLockFixture.lock(state),
        lock,
      );
      const button = page.getByTestId("preview-intent");
      await check(button).toBeDisabled();
      const before = await page.evaluate(() =>
        window.previewLockFixture.snapshot(),
      );
      await button.dispatchEvent("click");
      await button.dispatchEvent("keydown", { key: "Enter" });
      await button.dispatchEvent("keyup", { key: "Enter" });
      await check(page.getByRole("menu")).toHaveCount(0);
      expect(
        await page.evaluate(() => window.previewLockFixture.snapshot()),
      ).toEqual(before);
    }
    expect(errors).toEqual([]);
  } finally {
    await page.close();
  }
});

it.each(locks)("菜单打开后%s加锁，仍挂载的Portal项不能写入", async (lock) => {
  const page = await browser.newPage();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(`${base}/preview-lock-fixture`);
    await check(page.getByTestId("preview-intent")).toBeVisible();
    // 测试延长真实关闭动画，保证点击的是仍挂在Portal上的项。
    await page.addStyleTag({
      content:
        ".preview-menu-popup { transition: opacity 1s; } .preview-menu-popup[data-ending-style] { opacity: 0; }",
    });
    await page.getByTestId("preview-intent").click();
    const items = page.getByRole("menuitem");
    await check(items).toHaveCount(2);
    const handles = await items.elementHandles();
    await page.evaluate((state) => window.previewLockFixture.lock(state), lock);
    await check(page.getByTestId("preview-intent")).toBeDisabled();
    await check(page.getByTestId("preview-intent")).not.toHaveAttribute(
      "aria-expanded",
      "true",
    );
    const before = await page.evaluate(() =>
      window.previewLockFixture.snapshot(),
    );
    for (const item of handles) {
      expect(await item.evaluate((element) => element.isConnected)).toBe(true);
      expect(await item.getAttribute("aria-disabled")).toBe("true");
      await item.dispatchEvent("click");
      await item.dispatchEvent("keydown", { key: "Enter" });
      await item.dispatchEvent("keyup", { key: "Enter" });
      await item.dispatchEvent("keydown", { key: " " });
      await item.dispatchEvent("keyup", { key: " " });
    }
    await check(page.getByRole("menu", { includeHidden: true })).toHaveCount(0);
    expect(
      await page.evaluate(() => window.previewLockFixture.snapshot()),
    ).toEqual(before);
    expect(errors).toEqual([]);
  } finally {
    await page.close();
  }
});

it("追加锁在React重绘前发生，旧菜单项也不能接纳编辑", async () => {
  const page = await browser.newPage();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(`${base}/preview-lock-fixture`);
    await page.getByTestId("preview-intent").click();
    await check(page.getByRole("menuitem")).toHaveCount(2);
    const facts = await page.evaluate(() => {
      const item = document.querySelector<HTMLElement>('[role="menuitem"]')!;
      void window.previewLockFixture.lock("append", false);
      const before = window.previewLockFixture.snapshot();
      item.click();
      return { before, after: window.previewLockFixture.snapshot() };
    });
    expect(facts.after).toEqual(facts.before);
    await check(page.getByTestId("preview-intent")).toBeDisabled();
    expect(errors).toEqual([]);
  } finally {
    await page.close();
  }
});
