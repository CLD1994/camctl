import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, rmSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { DraftContent } from "../../src/server/models";
import { choose } from "./select-support";

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

async function setup(content: DraftContent) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-web-validation-"));
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
  page.setDefaultTimeout(2000);
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
  const draft = app.createDraft(content);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  return { app, page, draft };
}

const camera = (name = "录像 A") => ({
  name,
  type: "camera_record",
  device_id: "demo_cam0",
  params: { type: "demo_fixed" },
  policy: { max_delay_ms: 0 },
  scheduled_at: "2026-10-11 00:00:00",
});
const content = (actions: unknown[]): DraftContent => ({
  text: JSON.stringify({ name: "校验定位", actions }),
});

it("空动作校验先提供中文修正对象，机器诊断可展开", async () => {
  const { page } = await setup(content([]));
  const issues = page.getByTestId("validation-issues");
  await check(issues).toContainText("动作");
  await check(issues.getByRole("button")).toContainText("至少");
  await check(issues.getByText(/must NOT/)).toBeHidden();
  await issues.getByText("技术详情", { exact: true }).first().click();
  await check(issues).toContainText("schema_minItems");
});

it("校验入口展开并聚焦第二个折叠动作的执行时间而不改写内容", async () => {
  const second: Record<string, unknown> = camera("录像 B");
  delete second.scheduled_at;
  const { app, page, draft } = await setup(content([camera(), second]));
  const before = app.draft(draft.id);
  await page.getByRole("button", { name: "全部收起", exact: true }).click();
  await page
    .getByTestId("validation-issues")
    .getByRole("button", { name: /录像 B.*执行时间/ })
    .first()
    .click();
  const field = page
    .locator(".action-card")
    .nth(1)
    .getByLabel("执行时间", { exact: true });
  await check(field).toBeFocused();
  await check(field).toHaveAttribute("aria-invalid", "true");
  const describedBy = await field.getAttribute("aria-describedby");
  expect(describedBy).toBeTruthy();
  await check(page.locator(`[id="${describedBy}"]`)).toContainText("必填");
  expect(app.draft(draft.id)).toEqual(before);
  await field.fill("2026-10-11T08:00");
  await check(field).not.toHaveAttribute("aria-invalid", "true");
});

it("相机参数错误定位到精确参数选择控件", async () => {
  const action = {
    ...camera(),
    params: { type: "demo_adjustable", resolution: "bad", frame_rate_fps: 30 },
  };
  const { page } = await setup(content([action]));
  await page
    .getByTestId("validation-issues")
    .getByRole("button", { name: /录像 A.*分辨率/ })
    .first()
    .click();
  const field = page.getByLabel("分辨率 (resolution)", { exact: true });
  await check(field).toBeFocused();
  await check(field).toHaveAttribute("aria-invalid", "true");
});

it("参数JSON模式中的子字段错误定位到负责该对象的JSON", async () => {
  const action = {
    ...camera(),
    params: { type: "demo_adjustable", resolution: "bad", frame_rate_fps: 30 },
  };
  const { app, page, draft } = await setup(content([action]));
  const before = app.draft(draft.id);
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page
    .getByTestId("validation-issues")
    .getByRole("button", { name: /分辨率/ })
    .first()
    .click();
  await check(page.getByLabel("参数 JSON 文本", { exact: true })).toBeFocused();
  expect(app.draft(draft.id)).toEqual(before);
});

it("内置动作引用错误关联来源名称控件", async () => {
  const { page } = await setup(
    content([
      camera(),
      {
        name: "取回 B",
        type: "obtain_action_outputs",
        scheduled_at: "2026-10-11 00:00:00",
        params: { source: { action_name: "不存在" } },
      },
    ]),
  );
  await page
    .getByTestId("validation-issues")
    .getByRole("button", { name: /取回 B.*来源动作名称/ })
    .first()
    .click();
  const field = page.getByLabel("来源动作名称", { exact: true });
  await check(field).toBeFocused();
  await check(field).toHaveAttribute("aria-invalid", "true");
});

it("报告范围错误定位选择控件，合法选择清除字段错误", async () => {
  const { page } = await setup(
    content([{ name: "同步", type: "report_status", params: {} }]),
  );
  await page
    .getByTestId("validation-issues")
    .getByRole("button", { name: /同步.*报告范围/ })
    .first()
    .click();
  const field = page.getByLabel("报告范围", { exact: true });
  await check(field).toBeFocused();
  await check(field).toHaveAttribute("aria-invalid", "true");
  await choose(field, "full");
  await check(field).not.toHaveAttribute("aria-invalid", "true");
});

it("删除前一个动作后，当前校验定位仍属于存续动作", async () => {
  const remaining: Record<string, unknown> = camera("存续录像");
  delete remaining.scheduled_at;
  const { app, page, draft } = await setup(
    content([camera("删除项"), remaining]),
  );
  await page
    .locator(".action-card")
    .first()
    .getByRole("button", { name: "删除动作", exact: true })
    .click();
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const before = app.draft(draft.id);
  await page.getByRole("button", { name: "全部收起", exact: true }).click();
  await page
    .getByTestId("validation-issues")
    .getByRole("button", { name: /动作 1.*存续录像.*执行时间/ })
    .first()
    .click();
  await check(
    page.locator(".action-card").getByLabel("执行时间", { exact: true }),
  ).toBeFocused();
  expect(app.draft(draft.id)).toEqual(before);
});

it.each([
  {
    path: "/actions/0/policy/max_delay_ms",
    kind: "number" as const,
    text: "-",
    label: "最大允许延迟",
  },
  {
    path: "/actions/0/params",
    kind: "json" as const,
    text: "{",
    label: "参数",
  },
])(
  "未完成输入定位原文本并保留保存资料：$kind",
  async ({ path, kind, text, label }) => {
    const { app, page, draft } = await setup({
      ...content([camera()]),
      pending: { [path]: { kind, text } },
    });
    const before = app.draft(draft.id);
    await page
      .getByTestId("validation-issues")
      .getByRole("button", { name: new RegExp(label) })
      .first()
      .click();
    const field = page.getByLabel(`未完成输入 ${path}`, { exact: true });
    await check(field).toBeFocused();
    await check(field).toHaveValue(text);
    await check(field).toHaveAttribute("aria-invalid", "true");
    expect(app.draft(draft.id)).toEqual(before);
  },
);

it.each([
  {
    value: { name: "未知字段", unexpected: true, actions: [camera()] },
    label: /unexpected/,
  },
  { value: { name: "异常动作", actions: ["原始值"] }, label: /动作 1/ },
])(
  "没有准确表单字段时聚焦整份JSON并保留原文：$value.name",
  async ({ value, label }) => {
    const { app, page, draft } = await setup({ text: JSON.stringify(value) });
    const before = app.draft(draft.id);
    await page
      .getByTestId("validation-issues")
      .getByRole("button", { name: label })
      .first()
      .click();
    await check(page.getByTestId("draft-json-input")).toBeFocused();
    await check(page.getByTestId("draft-json-input")).toHaveValue(
      before.content.text,
    );
    expect(app.draft(draft.id)).toEqual(before);
  },
);

it.each(["bad", "/actions/~2bad", ""])(
  "不可解析或根路径的未完成输入仍可显示和定位：%s",
  async (path) => {
    const { app, page, draft } = await setup({
      ...content([camera()]),
      pending: { [path]: { kind: "json", text: "[" } },
    });
    const before = app.draft(draft.id);
    await check(page.getByTestId("save-status")).toBeVisible();
    await page
      .getByTestId("validation-issues")
      .getByRole("button", { name: /输入尚未完成/ })
      .first()
      .click();
    const field = page.getByLabel(`未完成输入 ${path}`, { exact: true });
    await check(field).toBeFocused();
    await check(field).toHaveValue("[");
    await check(field).toHaveAttribute("aria-invalid", "true");
    expect(app.draft(draft.id)).toEqual(before);
  },
);

it("根未完成输入和整份JSON的定位身份独立", async () => {
  const { app, page, draft } = await setup({
    text: JSON.stringify({
      name: "根输入",
      unexpected: true,
      actions: [camera()],
    }),
    pending: { "": { kind: "json", text: "{" } },
  });
  const before = app.draft(draft.id);
  await page
    .getByTestId("validation-issues")
    .getByRole("button", { name: /unexpected/ })
    .click();
  await check(page.getByTestId("draft-json-input")).toBeFocused();
  await check(page.getByTestId("draft-json-input")).toHaveValue(
    before.content.text,
  );
  await check(page.getByLabel("未完成输入 ", { exact: true })).toHaveValue("{");
  expect(app.draft(draft.id)).toEqual(before);
});
