import { beforeAll, afterAll, afterEach, expect, it } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, sep, basename } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { RequestLifecycle, createStop } from "../../src/server/lifecycle";
import { createServer, type ViteDevServer } from "vite";
import type { DraftConflictUiFixture } from "./draft-conflict-ui-fixture";
import type { DraftContent } from "../../src/server/models";
import { completeDraftInput } from "../helpers/draft-conflicts";
import {
  cloneClientJson,
  parseClientJson,
  stringifyJson,
} from "../../src/shared/json";
let browser: Browser;
let fixtureServer: ViteDevServer, fixtureBase: string, fixtureCache: string;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  fixtureCache = mkdtempSync(join(tmpdir(), "camctl-draft-ui-cache-"));
  fixtureServer = await createServer({
    configFile: false,
    cacheDir: fixtureCache,
    server: { host: "127.0.0.1", port: 0 },
    plugins: [
      {
        name: "draft-conflict-fixture",
        configureServer(vite) {
          vite.middlewares.use(async (request, response, next) => {
            if (request.url !== "/draft-conflict-fixture") return next();
            response.setHeader("Content-Type", "text/html");
            response.end(
              await vite.transformIndexHtml(
                "/draft-conflict-fixture",
                '<!doctype html><html lang="zh"><div id="root"></div><script type="module" src="/tests/integration/draft-conflict-ui-fixture.tsx"></script></html>',
              ),
            );
          });
        },
      },
    ],
  });
  await fixtureServer.listen();
  fixtureBase = `http://127.0.0.1:${(fixtureServer.httpServer!.address() as { port: number }).port}`;
  browser = await chromium.launch({ headless: true });
});
afterEach(async () => {
  for (const close of clean.splice(0)) await close();
});
afterAll(async () => {
  await browser.close();
  await fixtureServer.close();
  if (!resolve(fixtureCache).startsWith(`${resolve(tmpdir())}${sep}`) ||
    !basename(fixtureCache).startsWith("camctl-draft-ui-cache-"))
    throw Error("组件服务临时缓存清理路径无效");
  rmSync(fixtureCache, {recursive: true, force: true});
});
it("组件服务优化缓存与其他并行夹具保持独立", async () => {
  const peer = await createServer({configFile: false, server: {host: "127.0.0.1", port: 0}, plugins: [{name: "preview-lock-page"}]});
  try {
    expect(fixtureServer.config.cacheDir).not.toBe(peer.config.cacheDir);
    expect(resolve(fixtureServer.config.cacheDir)).toBe(resolve(fixtureCache));
  }
  finally { await peer.close(); }
});
async function setup(content?: DraftContent) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-draft-ui-")),
    app = new Application(directory),
    files = new Files(app),
    lifecycle = new RequestLifecycle();
  const server = createHttpApp(app, files, lifecycle).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const context = await browser.newContext(),
    page = await context.newPage();
  page.setDefaultTimeout(5000);
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      ),
    lifecycle,
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
  const draft = app.createDraft(
    content ?? { text: '{"name":"base","actions":[]}' },
  );
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  return { app, page, draft };
}
it.each(["exported", "deleted"] as const)("真实App来源%s放弃已删除另存目标后保留可见只读输入及焦点", async terminal => {
  const initial = completeDraftInput();
  initial.text = initial.text.replace("local", "base");
  const {app, page, draft} = await setup(initial);
  await page.getByLabel("计划名称", {exact: true}).fill("local");
  await page.locator('textarea[aria-label^="未完成输入："]').fill("--");
  const remote = app.saveDraft(draft.id, draft.revision, {text: '{"name":"remote","actions":[{"name":"sync","type":"report_status","scheduled_at":"2026-10-11 10:00:00","params":{"scope":"full"}}]}'});
  let creates = 0, createdId = "", selectedInput: DraftContent | undefined;
  await page.route("**/api/draft-creations/*", async route => {
    if(route.request().method() !== "PUT") return route.continue();
    creates++;
    selectedInput = (parseClientJson(route.request().postData()!) as {content: DraftContent}).content;
    const response = await route.fetch(), result = await response.json();
    createdId = result.creation.created.id;
    app.deleteDraft(createdId, result.creation.created.revision);
    return route.abort("failed");
  });
  await page.getByRole("button", {name: "查看完整差异", exact: true}).click();
  await page.getByRole("dialog").getByRole("button", {name: /将本页另存/}).click();
  const dialog = page.getByRole("dialog");
  await check(dialog).toContainText("另存需要继续处理");
  expect(creates).toBe(1);
  expect(createdId).not.toBe("");
  if(terminal === "exported") app.exportDraft(remote.id, remote.revision, remote.content);
  else app.deleteDraft(remote.id, remote.revision);
  await check(page.locator(".save-line")).toContainText(terminal === "exported" ? "原草稿已导出" : "原草稿已删除");
  await dialog.getByRole("button", {name: "重试", exact: true}).click();
  await check(dialog).toContainText("当前目标已删除或导出");
  await check(dialog.getByRole("button", {name: "放弃", exact: true})).toBeEnabled();
  await dialog.getByRole("button", {name: "放弃", exact: true}).click();
  const editor = page.getByTestId("draft-editor");
  await check(dialog).toHaveCount(0);
  await check(editor).toBeVisible();
  await check(page.getByLabel("计划名称", {exact: true})).toHaveValue("local");
  await check(page.getByLabel("计划名称", {exact: true})).toBeDisabled();
  await check(page.locator('textarea[aria-label^="未完成输入："]')).toHaveValue("--");
  await check(page.locator('textarea[aria-label^="未完成输入："]')).toBeDisabled();
  await check(page.getByLabel("保留的额外编辑内容", {exact: true})).toHaveValue(selectedInput!.text);
  await check(editor).toBeFocused();
  await page.getByText("额外编辑内容的完整技术详情", {exact: true}).click();
  const details = page.locator("details").filter({has: page.getByText("额外编辑内容的完整技术详情", {exact: true})}).locator("pre");
  await check(details).toBeVisible();
  const retained = (await details.textContent())!;
  expect(stringifyJson(parseClientJson(retained))).toBe(stringifyJson(selectedInput));
  expect(retained).toContain("1.0000000000000001");
  expect(retained).toContain("1e-999");
  await check(page.getByTestId("tab-records")).toHaveAttribute("aria-selected", "false");
  await check(page.getByTestId("draft-operation-notice")).toHaveCount(0);
  expect(creates).toBe(1);
  expect(await page.evaluate(() => {const event = new Event("beforeunload", {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented;})).toBe(true);
});
it("真实App自有来源GET失败排除双重消费后仍撤销放弃资格", async () => {
  const {app, page, draft} = await setup();
  await page.getByLabel("计划名称", {exact: true}).fill("local");
  app.saveDraft(draft.id, draft.revision, {text: '{"name":"remote","actions":[]}'});
  let created = false, sourceFailures = 0;
  const release: Array<() => void> = [];
  clean.unshift(async () => { for(const done of release) done(); });
  await page.route("**/api/state", async route => {
    if(!created) return route.continue();
    if(sourceFailures++ === 0) return route.abort("failed");
    await new Promise<void>(yes => {release.push(yes);});
    await route.abort("failed").catch(() => {});
  });
  await page.route("**/api/draft-creations/*", async route => {
    if(route.request().method() !== "PUT") return route.continue();
    const response = await route.fetch();
    created = true;
    await route.fulfill({response});
  });
  await page.getByRole("button", {name: "查看完整差异", exact: true}).click();
  await page.getByRole("dialog").getByRole("button", {name: /将本页另存/}).click();
  const dialog = page.getByRole("dialog");
  await check(dialog).toContainText("另存需要继续处理");
  await check(dialog.getByRole("button", {name: "重试", exact: true})).toBeEnabled();
  await check(dialog.getByRole("button", {name: "放弃", exact: true})).toBeDisabled();
  await check(dialog).toContainText(/读取|核实/);
  expect(app.state().drafts).toHaveLength(2);
  await page.keyboard.press("Escape");
  await check(page.getByLabel("计划名称", {exact: true})).toHaveValue("local");
  expect(await page.evaluate(() => {const event = new Event("beforeunload", {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented;})).toBe(true);
});
it("真实App普通导出进入记录并聚焦可见记录入口", async () => {
  const {page} = await setup({text: '{"name":"normal","actions":[{"name":"sync","type":"report_status","scheduled_at":"2026-10-11 10:00:00","params":{"scope":"full"}}]}'});
  await page.getByRole("button", {name: "校验并导出 JSON", exact: true}).click();
  await check(page.getByTestId("tab-records")).toHaveAttribute("aria-selected", "true");
  await check(page.getByTestId("draft-editor")).toHaveCount(0);
  await check(page.getByTestId("tab-records")).toBeFocused();
  expect(await page.evaluate(() => {const event = new Event("beforeunload", {cancelable: true}); window.dispatchEvent(event); return event.defaultPrevented;})).toBe(false);
});
it.each([{count: 0, mounted: false}, {count: 2, mounted: false}, {count: 0, mounted: true}, {count: 2, mounted: true}])(
  "真实App旧草稿$count动作Editor挂载历史$mounted后真实追加并重开", async ({count, mounted}) => {
    const {app, page, draft} = await setup();
    const legacy = app.saveDraft(draft.id, draft.revision, {text: JSON.stringify({name: "legacy", actions: Array.from({length: count}, (_, index) => ({name: `old-${index}`, type: "report_status", params: {scope: "full"}}))})});
    expect(legacy.content.automaticPreviews).toBeUndefined();
    if(mounted) {
      await check(page.locator(".action-card")).toHaveCount(count);
      await check(page.getByLabel("计划名称", {exact: true})).toHaveValue("legacy");
      await page.getByTestId("tab-records").click();
    } else await page.reload();
    await check(page.getByTestId("draft-editor")).toHaveCount(0);
    await page.getByRole("button", {name: "准备状态同步", exact: true}).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("目标草稿", {exact: true}).click();
    await page.getByRole("option", {name: /^legacy/}).click();
    await dialog.getByRole("button", {name: "加入草稿", exact: true}).click();
    await check(dialog).toHaveCount(0);
    await check(page.getByTestId("draft-editor")).toBeVisible();
    await check(page.locator(".action-card")).toHaveCount(count + 1);
    await check(page.getByLabel("动作名称", {exact: true}).last()).toHaveValue("状态同步");
    await check(page.getByLabel("计划名称", {exact: true})).toBeEnabled();
    expect(app.draft(draft.id).revision).toBe(legacy.revision + 1);
    expect(JSON.parse(app.draft(draft.id).content.text).actions.map((action: {name: string}) => action.name)).toEqual([...Array.from({length: count}, (_, index) => `old-${index}`), "状态同步"]);
  },
);
async function fixture(
  kind: "copy" | "adopt" | "replace",
  mode?: Parameters<DraftConflictUiFixture["creationMode"]>[0],
) {
  const page = await browser.newPage(),
    errors: string[] = [],
    loadFailures: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("requestfailed", request => loadFailures.push(`${request.url()}: ${request.failure()?.errorText}`));
  page.on("response", response => { if(response.status() >= 400) loadFailures.push(`${response.status()} ${response.url()}`); });
  clean.push(async () => {
    expect(errors).toEqual([]);
    await page.close();
  });
  await page.goto(`${fixtureBase}/draft-conflict-fixture`);
  try { await check(page.getByTestId("draft-editor")).toBeVisible(); }
  catch(error) { throw new Error(`组件入口未装配：${JSON.stringify({errors, loadFailures, url: page.url()})}`, {cause: error}); }
  if (mode)
    await page.evaluate(
      (value) => window.draftConflictUiFixture.creationMode(value),
      mode,
    );
  await page.evaluate(
    (value) => window.draftConflictUiFixture.begin(value),
    kind,
  );
  await check(page.getByRole("dialog")).toBeVisible();
  return page;
}
it.each(["copy", "adopt", "replace"] as const)(
  "组件%s本地失败自动显示两个按钮，放弃保留全部输入且旧按钮无效",
  async (kind) => {
    const page = await fixture(kind),
      dialog = page.getByRole("dialog");
    await check(dialog.getByRole("button")).toHaveCount(2);
    const before = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    if (kind === "replace")
      await check(dialog).toContainText("后端已经可靠写入版本 3");
    await dialog
      .getByRole("button", { name: "重试", exact: true })
      .evaluate((element) => {
        const key = Object.keys(element).find((key) =>
          key.startsWith("__reactProps$"),
        )!;
        (window as unknown as { heldRetry: () => void }).heldRetry = (
          element as unknown as Record<string, { onClick: () => void }>
        )[key].onClick;
      });
    await dialog.getByRole("button", { name: "放弃", exact: true }).click();
    await check(dialog).toHaveCount(0);
    await page.evaluate(() =>
      (window as unknown as { heldRetry: () => void }).heldRetry(),
    );
    const after = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    expect(after.input).toBe(before.input);
    expect(after.writes).toBe(before.writes);
    expect(after.creates).toBe(before.creates);
    expect(after.pending).toBe(false);
    expect(after.navigation).toBe(0);
    expect(after.viewCommits).toBe(0);
    expect(after.input).toContain("1.0000000000000001");
    expect(after.input).toContain("1e-999");
    await check(page.getByTestId("draft-editor")).toBeFocused();
  },
);
it.each(["copy", "adopt", "replace"] as const)(
  "组件%s异常重试只本地接纳且不重复写入",
  async (kind) => {
    const page = await fixture(kind),
      before = await page.evaluate(() =>
        window.draftConflictUiFixture.snapshot(),
      );
    await page.evaluate(() => window.draftConflictUiFixture.fault("none"));
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "重试", exact: true })
      .click();
    await check(page.getByRole("dialog")).toHaveCount(0);
    const after = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    expect(after.writes).toBe(before.writes);
    expect(after.creates).toBe(before.creates);
    expect(after.pending).toBe(false);
    expect(after.viewCommits).toBe(1);
    if (kind === "copy") {
      expect(after.navigation).toBe(1);
      expect(after.map).toBe(2);
      expect(after.warned).toBe(false);
    }
  },
);
it("组件Escape隐藏后同异常观察与旧回调不重开或执行，重新打开保持资格", async () => {
  const page = await fixture("copy"),
    dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "重试", exact: true })
    .evaluate((element) => {
      const key = Object.keys(element).find((key) =>
        key.startsWith("__reactProps$"),
      )!;
      (window as unknown as { heldRetry: () => void }).heldRetry = (
        element as unknown as Record<string, { onClick: () => void }>
      )[key].onClick;
    });
  const before = await page.evaluate(() =>
    window.draftConflictUiFixture.snapshot(),
  );
  await page.keyboard.press("Escape");
  await check(dialog).toHaveCount(0);
  await page.evaluate(() => {
    window.draftConflictUiFixture.observe("same");
    (window as unknown as { heldRetry: () => void }).heldRetry();
  });
  await check(dialog).toHaveCount(0);
  expect(
    await page.evaluate(() => window.draftConflictUiFixture.snapshot()),
  ).toEqual(before);
  await page.getByRole("button", { name: /重新打开另存异常/ }).click();
  await check(dialog).toBeVisible();
  await page.evaluate(() =>
    (window as unknown as { heldRetry: () => void }).heldRetry(),
  );
  expect(
    await page.evaluate(() => window.draftConflictUiFixture.snapshot()),
  ).toEqual(before);
});
it("组件核实在途两个按钮禁用且Escape不能隐藏，旧按钮不增加GET", async () => {
  const page = await fixture("copy"),
    dialog = page.getByRole("dialog");
  await page.evaluate(() => {
    window.draftConflictUiFixture.fault("none");
    window.draftConflictUiFixture.holdRead(true);
  });
  await dialog
    .getByRole("button", { name: "重试", exact: true })
    .evaluate((element) => {
      const key = Object.keys(element).find((key) =>
        key.startsWith("__reactProps$"),
      )!;
      (window as unknown as { heldRetry: () => void }).heldRetry = (
        element as unknown as Record<string, { onClick: () => void }>
      )[key].onClick;
    });
  await dialog.getByRole("button", { name: "重试", exact: true }).click();
  await check(
    dialog.getByRole("button", { name: "重试", exact: true }),
  ).toBeDisabled();
  await check(
    dialog.getByRole("button", { name: "放弃", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await check(dialog).toBeVisible();
  const before = await page.evaluate(() =>
    window.draftConflictUiFixture.snapshot(),
  );
  await page.evaluate(() =>
    (window as unknown as { heldRetry: () => void }).heldRetry(),
  );
  expect(
    await page.evaluate(() => window.draftConflictUiFixture.snapshot()),
  ).toEqual(before);
  await page.evaluate(() => window.draftConflictUiFixture.finishRead());
  await check(dialog).toHaveCount(0);
});
it.each(["deleted", "exported"] as const)(
  "组件另存目标%s仅显示新另存说明，放弃不复建旧目标",
  async (terminal) => {
    const page = await fixture("copy");
    await page.evaluate((value) => {
      window.draftConflictUiFixture.fault("none");
      window.draftConflictUiFixture.terminalTarget(value);
    }, terminal);
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: "重试", exact: true }).click();
    await check(dialog).toContainText("独立另存一份新草稿");
    const before = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    expect(before.creates).toBe(1);
    await dialog.getByRole("button", { name: "放弃", exact: true }).click();
    const after = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    expect(after.creates).toBe(1);
    expect(after.input).toBe(before.input);
    expect(after.pending).toBe(false);
  },
);
it.each(["target", "validate", "navigation"] as const)(
  "组件%s准备拒绝在首commit前保留Map导航和固定输入",
  async (fault) => {
    const page = await fixture("copy"),
      before = await page.evaluate(() =>
        window.draftConflictUiFixture.snapshot(),
      );
    await page.evaluate(
      (value) => window.draftConflictUiFixture.fault(value),
      fault,
    );
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "重试", exact: true })
      .click();
    await check(page.getByRole("dialog")).toBeVisible();
    const after = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    expect(after.input).toBe(before.input);
    expect(after.creates).toBe(before.creates);
    expect(after.navigation).toBe(0);
    expect(after.viewCommits).toBe(0);
    expect(after.map).toBe(1);
    expect(after.pending).toBe(true);
  },
);
it.each(["unknown", "wrong_identity", "wrong_input"] as const)(
  "组件另存%s显示不可放弃且重试不创建新身份",
  async (mode) => {
    const page = await fixture("copy", mode),
      dialog = page.getByRole("dialog");
    await check(dialog.getByRole("button")).toHaveCount(2);
    await check(
      dialog.getByRole("button", { name: "放弃", exact: true }),
    ).toBeDisabled();
    await check(dialog).toContainText("固定身份");
    const before = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    await dialog.getByRole("button", { name: "重试", exact: true }).click();
    await check(
      dialog.getByRole("button", { name: "重试", exact: true }),
    ).toBeEnabled();
    const after = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    expect(after.creates).toBe(before.creates);
    expect(after.input).toBe(before.input);
    expect(after.map).toBe(1);
    expect(after.navigation).toBe(0);
    expect(after.pending).toBe(true);
  },
);
it.each(["retry", "abandon"] as const)(
  "组件可靠未创建%s只结束所选安排，输入完整保留",
  async (action) => {
    const page = await fixture("copy", "not_created"),
      before = await page.evaluate(() =>
        window.draftConflictUiFixture.snapshot(),
      );
    await check(
      page
        .getByRole("dialog")
        .getByRole("button", { name: "放弃", exact: true }),
    ).toBeEnabled();
    await page.evaluate(() => {
      window.draftConflictUiFixture.creationMode("created");
      window.draftConflictUiFixture.fault("none");
    });
    await page
      .getByRole("dialog")
      .getByRole("button", {
        name: action === "retry" ? "重试" : "放弃",
        exact: true,
      })
      .click();
    await check(page.getByRole("dialog")).toHaveCount(0);
    const after = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    expect(after.creates).toBe(before.creates + (action === "retry" ? 1 : 0));
    expect(after.pending).toBe(false);
    expect(after.writes).toBe(0);
    if (action === "abandon") {
      expect(after.input).toBe(before.input);
      expect(after.warned).toBe(true);
      expect(after.map).toBe(1);
    } else {
      expect(after.map).toBe(2);
      expect(after.navigation).toBe(1);
    }
  },
);
it.each(["copy", "adopt", "replace"] as const)(
  "组件%s在源终态放弃保留六份资料并保持只读",
  async (kind) => {
    const page = await fixture(kind),
      before = await page.evaluate(() =>
        window.draftConflictUiFixture.snapshot(),
      );
    await page.evaluate(() =>
      window.draftConflictUiFixture.observe("exported"),
    );
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "放弃", exact: true })
      .click();
    await check(page.getByRole("dialog")).toHaveCount(0);
    const after = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
    expect(after.input).toBe(before.input);
    expect(after.writes).toBe(before.writes);
    expect(after.creates).toBe(before.creates);
    expect(after.pending).toBe(false);
    expect(after.editable).toBe(false);
    expect(after.warned).toBe(kind !== "replace");
    await check(
      page.locator('textarea[aria-label^="未完成输入："]'),
    ).toBeDisabled();
  },
);
it.each((["copy", "adopt", "replace"] as const).flatMap(kind => (["changed", "deleted", "exported"] as const).flatMap(source => (["retry", "abandon"] as const).map(action => ({kind,source,action})))))(
  "Task7受控组件 $kind 来源 $source 执行 $action 后保持副作用次数与完整输入归属",
  async ({kind,source,action}) => {
    const page = await fixture(kind);
    const before = await page.evaluate(() => window.draftConflictUiFixture.snapshot());
    await page.evaluate(value => { window.draftConflictUiFixture.observe(value); window.draftConflictUiFixture.fault("none"); },source);
    await page.getByRole("dialog").getByRole("button",{name:action === "retry" ? "重试" : "放弃",exact:true}).click();
    await check(page.getByRole("dialog")).toHaveCount(0);
    const after = await page.evaluate(() => window.draftConflictUiFixture.snapshot());
    expect(after.writes).toBe(before.writes); expect(after.creates).toBe(before.creates);
    expect(after.pending).toBe(false);
    if(kind === "copy" && action === "retry") {
      expect(after.navigation).toBe(1); expect(after.viewCommits).toBe(source === "deleted" ? 0 : 1);
      expect(after.displayedInput).toBe(before.input);
    } else if(kind === "replace" && source === "changed" && action === "retry") {
      expect(after.navigation).toBe(0); expect(after.viewCommits).toBe(1);
      expect(after.input).toBe(after.written); expect(after.input).toContain("changed");
    } else {
      expect(after.navigation).toBe(0); expect(after.viewCommits).toBe(0);
      expect(after.input).toBe(before.input);
      if(source !== "changed") expect(after.editable).toBe(false);
    }
  },
);
it("组件采用资格失效后恢复，重试回确认且旧选择不提交", async () => {
  const page = await fixture("adopt"),
    before = await page.evaluate(() =>
      window.draftConflictUiFixture.snapshot(),
    );
  await page.evaluate(() =>
    window.draftConflictUiFixture.observe("unavailable"),
  );
  await check(
    page.getByRole("dialog").getByRole("button", { name: "放弃", exact: true }),
  ).toBeDisabled();
  await page.evaluate(() => {
    window.draftConflictUiFixture.observe("same");
    window.draftConflictUiFixture.fault("none");
  });
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "重试", exact: true })
    .click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  const after = await page.evaluate(() =>
    window.draftConflictUiFixture.snapshot(),
  );
  expect(after.input).toBe(before.input);
  expect(after.viewCommits).toBe(0);
  expect(after.navigation).toBe(0);
  expect(after.pending).toBe(false);
  expect(after.writes).toBe(0);
});
it("组件完成所选A后独立B完整恢复输入继续显示且离页责任保持", async () => {
  const page = await fixture("copy");
  await page.evaluate(() => window.draftConflictUiFixture.addIndependent());
  const independent = page.getByTestId("draft-independent-recoveries");
  await check(independent).toBeVisible();
  await page.keyboard.press("Escape");
  await independent.locator("summary").click();
  await check(independent).toContainText("independent--");
  await page
    .getByRole("button", { name: "重新打开另存异常", exact: true })
    .click();
  await page.evaluate(() => window.draftConflictUiFixture.fault("none"));
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "重试", exact: true })
    .click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  await check(page.getByTestId("draft-operation-notice")).toHaveCount(0);
  await check(independent).toContainText("independent--");
  const after = await page.evaluate(() =>
    window.draftConflictUiFixture.snapshot(),
  );
  expect(after.pending).toBe(false);
  expect(after.map).toBe(2);
  expect(after.warned).toBe(true);
});
it.each(["copy", "adopt", "replace"] as const)(
  "组件%s领域已锁但尚未重绘时旧编辑回调被拒绝",
  async (kind) => {
    const page = await browser.newPage();
    clean.push(() => page.close());
    await page.goto(`${fixtureBase}/draft-conflict-fixture`);
    const input = page.locator('textarea[aria-label^="未完成输入："]');
    await check(input).toBeEnabled();
    const result = await input.evaluate(async (element, value) => {
      const key = Object.keys(element).find((key) =>
        key.startsWith("__reactProps$"),
      )!;
      const callback = (
        element as unknown as Record<
          string,
          { onChange: (event: unknown) => void }
        >
      )[key].onChange;
      const before = window.draftConflictUiFixture.snapshot();
      await window.draftConflictUiFixture.beginWithoutPaint(value);
      let refused = false;
      try {
        callback({ target: { value: "stale edit" } });
      } catch {
        refused = true;
      }
      return {
        before,
        after: window.draftConflictUiFixture.snapshot(),
        refused,
      };
    }, kind);
    expect(result.refused).toBe(true);
    expect(result.after.input).toBe(result.before.input);
    expect(result.after.pending).toBe(true);
    expect(result.after.navigation).toBe(0);
    expect(result.after.viewCommits).toBe(0);
  },
);
it("页面核实读到未写入只读，明确重试保存才发新PUT", async () => {
  const { page } = await setup();
  let puts = 0,
    unavailable = true;
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    puts++;
    if (puts === 1) return route.abort("failed");
    return route.continue();
  });
  await page.route("**/api/state", async (route) =>
    unavailable ? route.abort("failed") : route.continue(),
  );
  await page.getByLabel("计划名称", { exact: true }).fill("local");
  await check(page.getByTestId("save-status")).toContainText(/未确认|核实/);
  unavailable = false;
  await check(page.getByText("本地服务已连接", { exact: true })).toBeVisible();
  const verify = page.getByRole("button", {
    name: "核实保存结果",
    exact: true,
  });
  await verify.click();
  await page.waitForTimeout(600);
  expect(puts).toBe(1);
  try { await page.getByRole("button", { name: "重试保存", exact: true }).click(); }
  catch(error) { throw new Error(`核实后保存入口：${await page.locator(".save-line").innerText()}`,{cause:error}); }
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(puts).toBe(2);
});
it("另存未知自动开两个按钮，Escape隐藏后同异常不重开，源导出与页面切换仍保留责任", async () => {
  const { app, page, draft } = await setup();
  await page.getByLabel("计划名称", { exact: true }).fill("local");
  const remote = app.saveDraft(draft.id, draft.revision, {
    text: '{"name":"remote","actions":[{"name":"同步","type":"report_status","scheduled_at":"2026-10-11 10:00:00","params":{"scope":"full"}}]}',
  });
  let unavailable = true,
    creates = 0;
  const attempts: string[] = [];
  await page.route("**/api/draft-creations/*", async (route) => {
    attempts.push(route.request().url().split("/").at(-1)!);
    if (route.request().method() === "PUT") creates++;
    if (unavailable) return route.abort("failed");
    return route.continue();
  });
  await page.getByRole("button", { name: "查看完整差异", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /将本页另存/ })
    .click();
  const dialog = page.getByRole("dialog"),
    notice = page.getByTestId("draft-operation-notice");
  await check(dialog).toContainText("另存需要继续处理");
  await check(dialog.getByRole("button")).toHaveCount(2);
  await check(
    dialog.getByRole("button", { name: "放弃", exact: true }),
  ).toBeDisabled();
  await check(
    dialog.getByRole("button", { name: "重试", exact: true }),
  ).toBeEnabled();
  expect(
    await page.evaluate(() => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    }),
  ).toBe(true);
  const before = creates;
  await page.keyboard.press("Enter");
  expect(creates).toBe(before);
  for (const key of ["Tab", "Shift+Tab"])
    for (let i = 0; i < 5; i++) {
      await page.keyboard.press(key);
      await check
        .poll(() =>
          dialog.evaluate((element) =>
            element.contains(document.activeElement),
          ),
        )
        .toBe(true);
    }
  await page.locator(".modal-backdrop").click({ position: { x: 3, y: 3 } });
  await check(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await check(dialog).toHaveCount(0);
  const reopen = notice.getByRole("button", { name: /重新打开另存异常/ });
  await check(reopen).toBeFocused();
  app.exportDraft(remote.id, remote.revision, remote.content);
  await page.waitForTimeout(3300);
  await check(dialog).toHaveCount(0);
  await check(notice).toBeVisible();
  await check(page.getByTestId("draft-editor")).toBeVisible();
  await check(page.getByLabel("计划名称", { exact: true })).toBeDisabled();
  await page.getByTestId("nav-import").click();
  await check(page.getByTestId("draft-editor")).toHaveCount(0);
  await check(notice).toBeVisible();
  await reopen.click();
  await check(dialog).toContainText("完整输入仍由原会话保留");
  unavailable = false;
  await dialog.getByRole("button", { name: "重试", exact: true }).click();
  await check(dialog).toHaveCount(0);
  await check(page.getByLabel("计划名称", { exact: true })).toHaveValue(
    "local",
  );
  await check(page.getByTestId("draft-editor")).toBeFocused();
  expect(new Set(attempts).size).toBe(1);
  expect(creates).toBe(2);
  expect(app.draft(remote.id).exportedRequestId).toBeTruthy();
  expect(
    await page.evaluate(() => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    }),
  ).toBe(false);
});
it.each(["adopt", "replace"] as const)(
  "真实App/HTTP %s完整版本成功后直接进入原会话且无异常",
  async (kind) => {
    const input = parseClientJson(
      '{"text":"{\\"name\\":\\"base\\",\\"actions\\":[{\\"name\\":\\"A\\",\\"type\\":\\"report_status\\",\\"params\\":{\\"scope\\":\\"full\\"}}]}","pending":{"/amount":{"kind":"number","text":"-"}},"actionVariants":{"0":[{"type":"camera_record","fields":{"n":1.0000000000000001},"pending":{}}]},"parameterVariants":{"0":[{"paramsText":"{\\"params\\":{\\"type\\":\\"photo\\",\\"n\\":1e-999}}","pending":{}}]},"objectVariants":{"0":{"/x":{"discriminator":"mode","variants":[{"valueText":"{\\"value\\":{\\"mode\\":\\"manual\\"}}","pending":{}}]}}},"automaticPreviews":{"namespace":"source","next":1,"intent":"unset","actions":[{"id":"source:0"}]}}',
    ) as DraftContent;
    const { app, page, draft } = await setup(input);
    let puts = 0;
    await page.route("**/api/drafts/*", (route) => {
      if (route.request().method() === "PUT") puts++;
      return route.continue();
    });
    await page.locator('textarea[aria-label^="未完成输入："]').fill("--");
    const remoteContent = cloneClientJson(draft.content);
    remoteContent.text = remoteContent.text.replace("base", "remote");
    remoteContent.pending = { "/amount": { kind: "number", text: "+" } };
    const remote = app.saveDraft(draft.id, draft.revision, remoteContent);
    await page
      .getByRole("button", { name: "查看完整差异", exact: true })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog
      .getByRole("button", {
        name: kind === "adopt" ? "使用已保存版本" : "用本页版本替换已保存版本",
        exact: true,
      })
      .click();
    await dialog
      .getByRole("button", {
        name: kind === "adopt" ? "确认使用已保存版本" : "确认替换已保存版本",
        exact: true,
      })
      .click();
    await check(dialog).toHaveCount(0);
    await check(page.getByTestId("draft-operation-notice")).toHaveCount(0);
    await check(page.getByTestId("draft-editor")).toBeFocused();
    await check(
      page.locator('textarea[aria-label^="未完成输入："]'),
    ).toHaveValue(kind === "adopt" ? "+" : "--");
    const actual = app.draft(draft.id);
    expect(actual.revision).toBe(
      remote.revision + (kind === "replace" ? 1 : 0),
    );
    expect(stringifyJson(actual.content.pending)).toBe(
      stringifyJson(
        kind === "adopt"
          ? remoteContent.pending
          : { "/amount": { kind: "number", text: "--" } },
      ),
    );
    expect(actual.content.text).toBe(
      kind === "adopt" ? remoteContent.text : input.text,
    );
    for (const section of [
      "actionVariants",
      "parameterVariants",
      "objectVariants",
      "automaticPreviews",
    ] as const)
      expect(stringifyJson(actual.content[section])).toBe(
        stringifyJson(draft.content[section]),
      );
    expect(stringifyJson(actual.content)).toContain("1.0000000000000001");
    expect(stringifyJson(actual.content)).toContain("1e-999");
    expect(puts).toBe(kind === "adopt" ? 1 : 2); // 首次普通保存的条件拒绝与明确替换分开计数。
  },
);
it("真实页面Escape隐藏比较后捕获的旧另存回调不能发请求或导航", async () => {
  const { app, page, draft } = await setup();
  await page.getByLabel("计划名称", { exact: true }).fill("local");
  app.saveDraft(draft.id, draft.revision, {
    text: '{"name":"remote","actions":[]}',
  });
  await page.getByRole("button", { name: "查看完整差异", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /将本页另存/ })
    .evaluate((element) => {
      const key = Object.keys(element).find((key) =>
        key.startsWith("__reactProps$"),
      )!;
      (window as unknown as { heldCopy: () => void }).heldCopy = (
        element as unknown as Record<string, { onClick: () => void }>
      )[key].onClick;
    });
  await page.keyboard.press("Escape");
  await check(page.getByRole("dialog")).toHaveCount(0);
  await page.evaluate(() =>
    (window as unknown as { heldCopy: () => void }).heldCopy(),
  );
  await page.waitForTimeout(500);
  expect(app.state().drafts).toHaveLength(1);
  await check(page.getByLabel("计划名称", { exact: true })).toHaveValue(
    "local",
  );
  await check(page.getByTestId("draft-operation-notice")).toHaveCount(0);
});
it("完整差异两侧独立顺序，键盘关闭只隐藏，另存成功直接进入目标", async () => {
  const { app, page, draft } = await setup();
  await page.getByLabel("计划名称", { exact: true }).fill("local");
  app.saveDraft(draft.id, draft.revision, {
    text: '{"name":"remote","actions":[{"name":"same","type":"report_status","params":{"scope":"full"}}]}',
  });
  const entry = page.getByRole("button", { name: "查看完整差异", exact: true });
  await entry.click();
  const dialog = page.getByRole("dialog");
  await check(dialog).toContainText("本页版本");
  await check(dialog).toContainText("已保存版本");
  await check(dialog).toContainText("停用对象模式");
  for (const key of ["Tab", "Shift+Tab"])
    for (let i = 0; i < 10; i++) {
      await page.keyboard.press(key);
      await check
        .poll(() =>
          dialog.evaluate((element) =>
            element.contains(document.activeElement),
          ),
        )
        .toBe(true);
    }
  await page.locator(".modal-backdrop").click({ position: { x: 3, y: 3 } });
  await check(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await check(dialog).toHaveCount(0);
  await check(entry).toBeFocused();
  await entry.click();
  await dialog.getByRole("button", { name: /将本页另存/ }).click();
  await check(dialog).toHaveCount(0);
  await check(page.getByLabel("计划名称", { exact: true })).toHaveValue(
    "local",
  );
  await check(page.getByTestId("draft-operation-notice")).toHaveCount(0);
  expect(app.state().drafts).toHaveLength(2);
});
