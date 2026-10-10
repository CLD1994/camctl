import { pendingInput, pendingAction } from "./pending-support";
import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, rmSync, copyFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { DraftContent } from "../../src/server/models";
import { choose, readOptions } from "./select-support";
import { switchActionType } from "../../src/web/action-drafts";
import type { Capabilities } from "../../src/shared/types";

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

async function setup(content: DraftContent, capabilities?: Capabilities) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-web-validation-"));
  if (capabilities)
    writeFileSync(
      join(directory, "device-capabilities.json"),
      JSON.stringify(capabilities),
    );
  else
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

it("电机缺失容器关联红框、短说明和统一数量，修正一项只清除一项", async () => {
  const { app, page, draft } = await setup(
    content([
      {
        name: "电机",
        type: "motor_control",
        scheduled_at: "2026-10-11 00:00:00",
      },
    ]),
  );
  const before = app.draft(draft.id);
  const issues = page.getByTestId("validation-issues");
  const position = page.getByLabel("位置 (position)", { exact: true });
  const delay = page.getByLabel("最大允许延迟 (max_delay_ms)", { exact: true });
  await check(issues.getByRole("button")).toHaveCount(2);
  await check(position).toHaveAttribute("aria-invalid", "true");
  await check(delay).toHaveAttribute("aria-invalid", "true");
  expect(await position.evaluate((e) => getComputedStyle(e).borderColor)).toBe(
    "rgb(174, 81, 72)",
  );
  expect(await delay.evaluate((e) => getComputedStyle(e).borderColor)).toBe(
    "rgb(174, 81, 72)",
  );
  const description = await position.getAttribute("aria-describedby");
  const text = await page
    .locator(`[id=${JSON.stringify(description)}]`)
    .innerText();
  expect(text).toMatch(/必填|填写/);
  expect(text).not.toContain("电机");
  expect(text).not.toContain("actions");
  await issues.getByRole("button", { name: /位置/ }).click();
  await check(position).toBeFocused();
  expect(app.draft(draft.id)).toEqual(before);
  await position.fill("0");
  await check(position).not.toHaveAttribute("aria-invalid", "true");
  await check(delay).toHaveAttribute("aria-invalid", "true");
  await check(issues.getByRole("button")).toHaveCount(1);
  await check(page.locator(".builtin-parameters .notice.warning")).toHaveCount(
    0,
  );
  await check(
    page.getByRole("button", { name: "移除不适用参数字段", exact: true }),
  ).toHaveCount(0);
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const withPosition = app.draft(draft.id);
  expect(JSON.parse(withPosition.content.text).actions[0]).toMatchObject({
    params: { position: 0 },
  });
  expect(
    Object.hasOwn(JSON.parse(withPosition.content.text).actions[0], "policy"),
  ).toBe(false);
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  expect(
    JSON.parse(
      await page.getByLabel("动作参数 JSON", { exact: true }).inputValue(),
    ),
  ).toEqual({ position: 0 });
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  expect(app.draft(draft.id)).toEqual(withPosition);
  await delay.fill("0");
  await check(issues.getByRole("button")).toHaveCount(0);
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await check(
    page.getByRole("button", { name: "移除不适用参数字段", exact: true }),
  ).toHaveCount(0);
  expect(app.validateContent(app.draft(draft.id).content)).toBeDefined();
});

it("电机真实额外字段准确诊断，明确移除只删除额外键", async () => {
  const params = { position: 0, unexpected: { 原值: true } };
  const { app, page, draft } = await setup(
    content([
      {
        name: "电机",
        type: "motor_control",
        scheduled_at: "2026-10-11 00:00:00",
        params,
        policy: { max_delay_ms: 0 },
      },
    ]),
  );
  const before = app.draft(draft.id);
  const warning = page.locator(".builtin-parameters .notice.warning");
  await check(warning).toContainText("unexpected");
  await check(warning).not.toContainText("position");
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  expect(
    JSON.parse(
      await page.getByLabel("动作参数 JSON", { exact: true }).inputValue(),
    ),
  ).toEqual(params);
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  expect(app.draft(draft.id)).toEqual(before);
  expect(() => app.validateContent(before.content)).toThrow();
  await page
    .getByRole("button", { name: "移除不适用参数字段", exact: true })
    .click();
  await check
    .poll(() => JSON.parse(app.draft(draft.id).content.text).actions[0].params)
    .toEqual({ position: 0 });
  await check(
    page.getByTestId("validation-issues").getByRole("button"),
  ).toHaveCount(0);
  await check(warning).toHaveCount(0);
  expect(app.validateContent(app.draft(draft.id).content)).toBeDefined();
});

it.each([null, [], "原值"])(
  "非对象业务策略只定位策略JSON并显示红框：%j",
  async (policy) => {
    const { app, page, draft } = await setup(
      content([
        {
          name: "电机",
          type: "motor_control",
          scheduled_at: "2026-10-11 00:00:00",
          params: { position: 0 },
          policy,
        },
      ]),
    );
    const before = app.draft(draft.id);
    await page
      .getByTestId("validation-issues")
      .getByRole("button", { name: /业务策略/ })
      .click();
    const field = page.getByLabel("业务策略 JSON", { exact: true });
    await check(field).toBeFocused();
    await check(field).toHaveAttribute("aria-invalid", "true");
    expect(await field.evaluate((e) => getComputedStyle(e).borderColor)).toBe(
      "rgb(174, 81, 72)",
    );
    expect(app.draft(draft.id)).toEqual(before);
  },
);

it("报告未选择范围只标记范围，since缺起点不把合法范围标红", async () => {
  const { page } = await setup(
    content([
      { name: "同步", type: "report_status", params: {} },
      { name: "增量", type: "report_status", params: { scope: "since" } },
    ]),
  );
  const cards = page.locator(".action-card");
  await check(
    cards.nth(0).getByLabel("报告范围", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true");
  await check(
    cards.nth(1).getByLabel("报告范围", { exact: true }),
  ).not.toHaveAttribute("aria-invalid", "true");
  await check(
    cards.nth(1).getByLabel("同步起点报告", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true");
  const names = await page
    .getByTestId("validation-issues")
    .getByRole("button")
    .allTextContents();
  expect(names.some((text) => /同步.*同步起点/.test(text))).toBe(false);
});

it("来源坏ID只标记当前ID，空列表定位添加入口且查看零写入", async () => {
  const { app, page, draft } = await setup(
    content([
      {
        name: "取回",
        type: "obtain_action_outputs",
        scheduled_at: "2026-10-11 00:00:00",
        params: { source: { action_instance_id: "bad" } },
      },
      {
        name: "清理",
        type: "delete_action_outputs",
        scheduled_at: "2026-10-11 00:00:00",
        params: { output_ids: [] },
      },
    ]),
  );
  const before = app.draft(draft.id);
  const issues = page.getByTestId("validation-issues");
  await check(issues.getByRole("button")).toHaveCount(2);
  await check(
    page.getByLabel("来源动作实例 ID", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true");
  await check(page.getByLabel("取回来源", { exact: true })).not.toHaveAttribute(
    "aria-invalid",
    "true",
  );
  await issues.getByRole("button", { name: /产物 ID/ }).click();
  await check(
    page.getByRole("button", { name: "添加产物 ID", exact: true }),
  ).toBeFocused();
  await check(
    page.getByRole("button", { name: "添加产物 ID", exact: true }),
  ).toHaveAttribute("aria-invalid", "true");
  expect(app.draft(draft.id)).toEqual(before);
});

it("停用录像pending不阻止当前电机导出，切回恢复并按当前内容校验", async () => {
  const initial = switchActionType(
    {
      ...content([camera()]),
      pending: {
        "/actions/0/params/resolution": { kind: "json", text: '"未完成' },
      },
    },
    0,
    "motor_control",
  );
  const { app, page, draft } = await setup(initial);
  const cache = app.draft(draft.id).content.actionVariants;
  await page.getByLabel("位置 (position)", { exact: true }).fill("0");
  await page
    .getByLabel("最大允许延迟 (max_delay_ms)", { exact: true })
    .fill("0");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await check(
    page.getByTestId("validation-issues").getByRole("button"),
  ).toHaveCount(0);
  const saved = app.draft(draft.id);
  expect(saved.content.actionVariants).toEqual(cache);
  expect(
    (app.validateContent(saved.content).actions as Array<{ type: string }>)[0]
      .type,
  ).toBe("motor_control");
  const exportCopy = app.createDraft(saved.content);
  expect(
    app.exportDraft(exportCopy.id, exportCopy.revision, exportCopy.content),
  ).toBeDefined();
  await choose(page.getByLabel("动作类型", { exact: true }), "camera_record");
  await check(pendingInput(page, "/actions/0/params/resolution")).toHaveValue(
    '"未完成',
  );
  await check(page.getByTestId("validation-issues")).toContainText(
    "输入尚未完成",
  );
  expect(
    await page
      .getByTestId("validation-issues")
      .getByRole("button")
      .allTextContents(),
  ).not.toContain(expect.stringMatching(/位置/));
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

it("固定选择和互斥摘要保留具体动作，说明关联和查看零写入", async () => {
  const { app, page, draft } = await setup(
    content([
      { name: "同步", type: "report_status", params: {} },
      {
        name: "清理",
        type: "delete_action_outputs",
        scheduled_at: "2026-10-11 00:00:00",
        params: { source: { action_instance_id: "1" }, output_ids: ["1"] },
      },
    ]),
  );
  const before = app.draft(draft.id);
  const issues = page.getByTestId("validation-issues");
  const scope = page.getByLabel("报告范围", { exact: true });
  const describedBy = await scope.getAttribute("aria-describedby");
  await check(
    page.locator(`[id=${JSON.stringify(describedBy)}]`),
  ).toContainText(/选择.*报告范围/);
  await check(issues.getByRole("button", { name: /同步/ })).toContainText(
    /选择.*报告范围/,
  );
  await check(issues.getByRole("button", { name: /清理/ })).toContainText(
    /来源.*产物列表.*不能同时/,
  );
  await issues.getByRole("button", { name: /清理/ }).click();
  await check(page.getByTestId("draft-json-input")).toBeFocused();
  expect(app.draft(draft.id)).toEqual(before);
});

it("互斥与坏ID和空列表同时显示，修正一项只清该项", async () => {
  const { app, page, draft } = await setup(
    content([
      {
        name: "取回",
        type: "obtain_action_outputs",
        scheduled_at: "2026-10-11 00:00:00",
        params: {
          source: { action_instance_id: "bad" },
          output_ids: [],
          filter: "not_allowed",
        },
      },
    ]),
  );
  const before = app.draft(draft.id);
  const issues = page.getByTestId("validation-issues");
  const source = page.getByLabel("来源动作实例 ID", { exact: true });
  const add = page.getByRole("button", { name: "添加产物 ID", exact: true });
  await check(issues.getByRole("button")).toHaveCount(4);
  await check(page.locator(".action-problems")).toContainText("4 项");
  await check(source).toHaveAttribute("aria-invalid", "true");
  await check(add).toHaveAttribute("aria-invalid", "true");
  await issues.getByRole("button", { name: /产物 ID.*数量/ }).click();
  await check(add).toBeFocused();
  expect(app.draft(draft.id)).toEqual(before);
  expect(() => app.validateContent(before.content)).toThrow();
  await source.fill("1");
  await check(source).not.toHaveAttribute("aria-invalid", "true");
  await check(add).toHaveAttribute("aria-invalid", "true");
  await check(issues.getByRole("button")).toHaveCount(3);
  await check(issues.getByRole("button", { name: /动作参数/ })).toContainText(
    /筛选.*产物列表.*不能同时/,
  );
});

it.each([
  {
    type: "obtain_action_outputs",
    key: "source",
    value: null,
    label: "取回来源",
  },
  { type: "cancel_task", key: "target", value: [], label: "取消目标" },
])(
  "非对象引用Select不标红，结构定位JSON保持原值：$type",
  async ({ type, key, value, label }) => {
    const { app, page, draft } = await setup(
      content([
        {
          name: "引用",
          type,
          scheduled_at: "2026-10-11 00:00:00",
          params: { [key]: value },
        },
      ]),
    );
    const before = app.draft(draft.id);
    const selection = page.getByLabel(label, { exact: true });
    await check(selection).not.toHaveAttribute("aria-invalid", "true");
    await page
      .getByTestId("validation-issues")
      .getByRole("button", { name: /对象/ })
      .click();
    const json = page.getByTestId("draft-json-input");
    await check(json).toBeFocused();
    await check(json).toHaveAttribute("aria-invalid", "true");
    await check(json).toHaveValue(before.content.text);
    expect(app.draft(draft.id)).toEqual(before);
    await page.getByTestId("draft-json-toggle").click();
    await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
    await page
      .getByTestId("validation-issues")
      .getByRole("button", { name: /对象/ })
      .click();
    const params = page.getByLabel("动作参数 JSON", { exact: true });
    await check(params).toBeFocused();
    await check(params).toHaveAttribute("aria-invalid", "true");
    expect(JSON.parse(await params.inputValue())).toEqual({ [key]: value });
    expect(app.draft(draft.id)).toEqual(before);
  },
);

it("两个独立设备组合保留两项计数和JSON说明，定位零写入", async () => {
  const capabilities: Capabilities = {
    devices: [
      {
        device_id: "demo_cam0",
        driver_id: "test",
        actions: [
          {
            type: "camera_record",
            parameter_types: [
              {
                type: "double",
                name: "独立组合",
                description: "测试",
                preview_supported: false,
                schema: {
                  $schema: "https://json-schema.org/draft/2020-12/schema",
                  type: "object",
                  properties: {
                    type: { const: "double" },
                    a: { type: "boolean" },
                    b: { type: "boolean" },
                    c: { type: "boolean" },
                    d: { type: "boolean" },
                  },
                  required: ["type"],
                  allOf: [
                    { oneOf: [{ required: ["a"] }, { required: ["b"] }] },
                    { oneOf: [{ required: ["c"] }, { required: ["d"] }] },
                  ],
                },
              },
            ],
          },
        ],
      },
    ],
  };
  const { app, page, draft } = await setup(
    content([
      {
        ...camera(),
        params: { type: "double", a: true, b: true, c: true, d: true },
      },
    ]),
    capabilities,
  );
  const before = app.draft(draft.id);
  const issues = page.getByTestId("validation-issues");
  await check(issues.getByRole("button")).toHaveCount(2);
  await check(page.locator(".action-problems")).toContainText("2 项");
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await issues.getByRole("button").first().click();
  const field = page.getByLabel("参数 JSON 文本", { exact: true });
  await check(field).toBeFocused();
  const describedBy = await field.getAttribute("aria-describedby");
  expect(
    (
      await page.locator(`[id=${JSON.stringify(describedBy)}]`).innerText()
    ).split("；"),
  ).toHaveLength(2);
  expect(app.draft(draft.id)).toEqual(before);
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
    const field = pendingInput(page, path);
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
    const field = pendingInput(page, path);
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
  await check(pendingInput(page, "")).toHaveValue("{");
  expect(app.draft(draft.id)).toEqual(before);
});

it("未完成参数使用人类名称，动作选项隐藏机器类型且查看不修改草稿", async () => {
  const path = "/actions/0/params/resolution";
  const action = {
    ...camera("录像 <A>&"),
    params: {
      type: "demo_adjustable",
      resolution: "4K",
      frame_rate_fps: 30,
    },
  };
  const { app, page, draft } = await setup({
    ...content([action]),
    pending: { [path]: { kind: "json", text: '"4K' } },
  });
  const before = app.draft(draft.id);
  const field = pendingInput(page, path);
  const area = page.locator(`[data-pending-path=${JSON.stringify(path)}]`);
  await check(field).toHaveAccessibleName(/录像 <A>&.*分辨率/);
  expect(await field.getAttribute("aria-label")).not.toContain(path);
  await check(field).toHaveValue('"4K');
  await check(pendingAction(page, path, "apply")).toHaveText("确认修改");
  await check(pendingAction(page, path, "clear")).toHaveText("清除这项输入");
  await check(area.getByText(path, { exact: true })).toBeHidden();
  await area.getByText("技术详情", { exact: true }).click();
  await check(area.getByText(path, { exact: true })).toBeVisible();
  const select = page.getByLabel("动作类型", { exact: true });
  expect(await select.innerText()).not.toContain("camera_record");
  const options = await readOptions(select);
  const record = options.find((option) => option.value === "camera_record");
  expect(record).toBeDefined();
  expect(record!.text).not.toContain("camera_record");
  expect(record!.text).toMatch(/[\u4e00-\u9fff]/);
  expect(app.draft(draft.id)).toEqual(before);
});

it("多个无法识别的未完成输入以序号区分，并折叠保留原路径", async () => {
  const paths = ["bad", "/actions/0/params/unknown"];
  const { app, page, draft } = await setup({
    ...content([camera()]),
    pending: Object.fromEntries(
      paths.map((path) => [path, { kind: "json" as const, text: "[" }]),
    ),
  });
  const before = app.draft(draft.id);
  const labels: string[] = [];
  for (const path of paths) {
    const field = pendingInput(page, path);
    labels.push((await field.getAttribute("aria-label"))!);
    expect(labels.at(-1)).not.toContain(path);
    expect(labels.at(-1)).not.toContain("录像 A");
    await check(field).toHaveValue("[");
    const area = page.locator(`[data-pending-path=${JSON.stringify(path)}]`);
    await check(area.getByText(path, { exact: true })).toBeHidden();
    await area.getByText("技术详情", { exact: true }).click();
    await check(area.getByText(path, { exact: true })).toBeVisible();
  }
  expect(new Set(labels).size).toBe(paths.length);
  expect(app.draft(draft.id)).toEqual(before);
});

const automaticObtain = (params: Record<string, unknown>) => ({
  name: "自动取回",
  type: "obtain_action_outputs",
  scheduled_at: "2026-10-11 00:00:00",
  params: {
    purpose: "auto_preview",
    source: { action_name: "录像 A" },
    ...params,
  } as Record<string, unknown>,
});
const automaticCapabilities: Capabilities = {
  devices: [
    {
      device_id: "demo_cam0",
      driver_id: "test",
      actions: [
        {
          type: "camera_record",
          parameter_types: [
            {
              type: "demo_fixed",
              name: "固定拍摄",
              description: "支持预览的测试来源",
              preview_supported: true,
              schema: {
                $schema: "https://json-schema.org/draft/2020-12/schema",
                type: "object",
                properties: { type: { const: "demo_fixed" } },
                required: ["type"],
                additionalProperties: false,
              },
            },
          ],
        },
      ],
    },
  ],
};

it.each([{}, { filter: "default" }])(
  "自动用途与列表冲突仍显示筛选责任：%j",
  async (params) => {
    const { app, page, draft } = await setup(
      content([camera(), automaticObtain({ ...params, output_ids: ["1"] })]),
      automaticCapabilities,
    );
    const before = app.draft(draft.id);
    const issues = page.getByTestId("validation-issues");
    await check(issues.getByRole("button")).toHaveCount(2);
    await check(issues.getByRole("button", { name: /取回筛选/ })).toBeVisible();
    await check(issues.getByRole("button", { name: /动作参数/ })).toContainText(
      /自动.*产物列表.*不能同时/,
    );
    await issues.getByRole("button", { name: /取回筛选/ }).click();
    const json = page.getByTestId("draft-json-input");
    await check(json).toBeFocused();
    await check(json).toHaveAttribute("aria-invalid", "true");
    await check(json).toHaveValue(before.content.text);
    const describedBy = await json.getAttribute("aria-describedby");
    const description = await page
      .locator(`[id=${JSON.stringify(describedBy)}]`)
      .innerText();
    expect(description.split("；")).toHaveLength(2);
    expect(app.draft(draft.id)).toEqual(before);
    expect(() => app.validateContent(before.content)).toThrow();
  },
);

it.each([
  { source: undefined, label: /取回或清理来源/, fact: /选择.*来源/ },
  {
    source: { action_instance_id: "1" },
    label: /取回或清理来源/,
    fact: /自动.*来源.*动作名称/,
  },
])(
  "自动用途与列表冲突仍显示当前来源责任：%j",
  async ({ source, label, fact }) => {
    const action = automaticObtain({
      filter: "preview",
      output_ids: ["1"],
      ...(source === undefined ? {} : { source }),
    });
    if (source === undefined) delete action.params.source;
    const { app, page, draft } = await setup(
      content([camera(), action]),
      automaticCapabilities,
    );
    const before = app.draft(draft.id);
    const issues = page.getByTestId("validation-issues");
    await check(issues.getByRole("button")).toHaveCount(2);
    await check(issues.getByRole("button", { name: label })).toContainText(
      fact,
    );
    await issues.getByRole("button", { name: label }).click();
    await check(page.getByTestId("draft-json-input")).toBeFocused();
    await check(page.getByTestId("draft-json-input")).toHaveValue(
      before.content.text,
    );
    expect(app.draft(draft.id)).toEqual(before);
    expect(() => app.validateContent(before.content)).toThrow();
  },
);

it("自动用途冲突中的空列表保留 JSON 责任和零写入", async () => {
  const { app, page, draft } = await setup(
    content([camera(), automaticObtain({ filter: "default", output_ids: [] })]),
    automaticCapabilities,
  );
  const before = app.draft(draft.id);
  const issues = page.getByTestId("validation-issues");
  await check(issues.getByRole("button")).toHaveCount(3);
  await issues.getByRole("button", { name: /产物 ID.*数量/ }).click();
  await check(page.getByTestId("draft-json-input")).toBeFocused();
  await check(page.getByTestId("draft-json-input")).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  expect(app.draft(draft.id)).toEqual(before);
});

it("自动字段修正与移除列表分别只清除对应责任", async () => {
  const initial = [
    camera(),
    automaticObtain({ filter: "default", output_ids: ["1"] }),
  ];
  const { app, page, draft } = await setup(
    content(initial),
    automaticCapabilities,
  );
  const before = app.draft(draft.id);
  const issues = page.getByTestId("validation-issues");
  await check(issues.getByRole("button")).toHaveCount(2);
  await issues.getByRole("button", { name: /取回筛选/ }).click();
  expect(app.draft(draft.id)).toEqual(before);
  const json = page.getByTestId("draft-json-input");
  await json.fill(
    content([
      camera(),
      automaticObtain({ filter: "preview", output_ids: ["1"] }),
    ]).text,
  );
  await check(issues.getByRole("button")).toHaveCount(1);
  await check(issues).toContainText(/自动.*产物列表.*不能同时/);
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(() => app.validateContent(app.draft(draft.id).content)).toThrow();
  await json.fill(
    content([camera(), automaticObtain({ filter: "default" })]).text,
  );
  await check(issues.getByRole("button")).toHaveCount(1);
  await check(issues.getByRole("button", { name: /取回筛选/ })).toBeVisible();
  await json.fill(
    content([camera(), automaticObtain({ filter: "preview" })]).text,
  );
  await check(issues.getByRole("button")).toHaveCount(0);
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(app.validateContent(app.draft(draft.id).content)).toBeDefined();
});
