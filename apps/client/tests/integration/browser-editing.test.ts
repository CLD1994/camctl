import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, rmSync, copyFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { Draft, Preset, ExportedRequest } from "../../src/server/models";
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
async function setup(capabilityText?: string) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-web-edit-"));
  copyFileSync(
    "../../protocol/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"),
  );
  if (capabilityText !== undefined)
    writeFileSync(join(directory, "device-capabilities.json"), capabilityText);
  const app = new Application(directory),
    files = new Files(app),
    requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((r) => server.once("listening", r));
  const context = await browser.newContext({
    timezoneId: "Asia/Shanghai",
    acceptDownloads: true,
  });
  const page = await context.newPage();
  const stop = createStop(
    () => new Promise<void>((r, j) => server.close((e) => (e ? j(e) : r()))),
    requests,
    () => files.idle(),
    () => app.store.close(),
  );
  clean.push(async () => {
    await context.close();
    await stop();
    rmSync(directory, { recursive: true, force: true });
  });
  expect(
    (
      await page.goto(
        `http://127.0.0.1:${(server.address() as { port: number }).port}`,
      )
    )?.status(),
  ).toBe(200);
  await page.getByTestId("initialize-button").click();
  return { page, app, directory };
}
it("取回来源只为指定动作实例展示附加产物筛选", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .selectOption("obtain_action_outputs");
  const source = page.getByLabel("取回来源", { exact: true });
  for (const mode of ["action_name", "group", "plan_group"]) {
    await source.selectOption(mode);
    await check(page.getByLabel("指定产物筛选")).toHaveCount(0);
  }
  await source.selectOption("action_instance_id");
  await page.getByLabel("指定产物筛选").check();
  await page.getByRole("button", { name: "添加产物 ID", exact: true }).click();
  await page.getByLabel("产物 ID 1", { exact: true }).fill("o-1");
  await source.selectOption("action_name");
  await check(page.getByLabel("指定产物筛选")).toHaveCount(0);
  await check
    .poll(
      () =>
        JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0]
          ?.params,
    )
    .toEqual({ source: { action_name: "" } });
}, 20000);
it("类型独立编辑在刷新后恢复，不要求移除另一类型参数", async () => {
  const { page } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  const type = page.getByLabel("动作类型", { exact: true });
  await type.selectOption("camera_record");
  await page.getByLabel("目标设备", { exact: true }).selectOption("demo_cam0");
  await page.getByLabel("参数类型", { exact: true }).selectOption("demo_fixed");
  await page
    .getByLabel("最大允许延迟 (max_delay_ms)", { exact: true })
    .fill("1e");
  await type.selectOption("obtain_action_outputs");
  await check(
    page.getByRole("button", { name: /移除不适用|省略设备/ }),
  ).toHaveCount(0);
  await page.getByLabel("取回来源").selectOption("action_instance_id");
  await page.getByLabel("来源动作实例 ID").fill("a-1");
  await type.selectOption("report_status");
  await page.getByLabel("报告范围").selectOption("full");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await type.selectOption("camera_record");
  await check(
    page.getByLabel("最大允许延迟 (max_delay_ms)", { exact: true }),
  ).toHaveValue("1e");
  await check(page.getByLabel("目标设备", { exact: true })).toHaveValue(
    "demo_cam0",
  );
  await type.selectOption("obtain_action_outputs");
  await check(page.getByLabel("来源动作实例 ID")).toHaveValue("a-1");
  await type.selectOption("report_status");
  await check(page.getByLabel("报告范围")).toHaveValue("full");
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("save-status")).toHaveCount(0);
}, 20000);
it("必填标签覆盖公共字段，普通选填直接显示且可清空", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .selectOption("camera_record");
  for (const label of [
    "计划名称",
    "动作名称",
    "动作类型",
    "目标设备",
    "参数类型",
    "执行时间",
  ]) {
    await check(
      page
        .getByLabel(label, { exact: true })
        .locator("xpath=ancestor::label[1]")
        .locator(".required"),
    ).toHaveCount(1);
  }
  const group = page.getByLabel("动作组 (group)", { exact: true });
  await check(group).toBeVisible();
  await check(page.getByRole("checkbox", { name: /^填写/ })).toHaveCount(0);
  await group.fill("G");
  await page
    .getByRole("button", { name: "清空动作组 (group)", exact: true })
    .click();
  await check(group).toBeVisible();
  await check
    .poll(
      () =>
        JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0],
    )
    .toEqual({ name: "动作 1", type: "camera_record" });
}, 20000);
it("查看整份 JSON 保留类型内容，替换时确认且取消不丢失", async () => {
  const { page, app } = await setup();
  const content = {
    text: JSON.stringify({
      name: "计划",
      actions: [{ name: "报告", type: "report_status" }],
    }),
    actionVariants: {
      "0": [
        {
          type: "camera_record",
          fields: { params: { type: "demo_fixed" } },
          pending: {},
        },
      ],
    },
  };
  const draft = app.createDraft(content);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await page.getByTestId("draft-json-toggle").click();
  expect(app.draft(draft.id).content).toEqual(content);
  page.once("dialog", (dialog) => dialog.dismiss());
  await page
    .getByTestId("draft-json-input")
    .fill('{"name":"替换","actions":[]}');
  await check(page.getByTestId("draft-json-input")).toHaveValue(content.text);
  expect(app.draft(draft.id).content).toEqual(content);
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByTestId("draft-json-input")
    .fill('{"name":"替换","actions":[]}');
  await check
    .poll(() => app.draft(draft.id).content)
    .toEqual({ text: '{"name":"替换","actions":[]}', pending: {} });
}, 20000);
it.each([
  ["空设备目录", '{"devices":[]}'],
  [
    "无拍摄能力设备",
    '{"devices":[{"device_id":"idle","driver_id":"other","actions":[]}]}',
  ],
  ["能力说明不可用", "{"],
])(
  "%s 时新增动作不提供录像且可导出报告请求",
  async (_state, capabilityText) => {
    const { page, app } = await setup(capabilityText);
    await page.getByTestId("new-draft-button").click();
    await page.getByRole("button", { name: "添加动作", exact: true }).click();
    const type = page.getByLabel("动作类型", { exact: true });
    await check(type).toHaveValue("");
    await check(type.locator('option[value="camera_record"]')).toHaveCount(0);
    await type.selectOption("report_status");
    await page.getByTestId("export-button").click();
    await check
      .poll(() => app.store.all<ExportedRequest>("requests").length)
      .toBe(1);
    const request = app.store.all<ExportedRequest>("requests")[0];
    expect(request.body.actions).toEqual([
      { name: "动作 1", type: "report_status" },
    ]);
  },
  20000,
);
it("设备能力联动保留失效草稿并阻止导出", async () => {
  const { page, app, directory } = await setup();
  const capabilities = structuredClone(app.capabilities.active);
  expect(capabilities).toBeTruthy();
  capabilities!.devices.push({
    device_id: "no-record",
    driver_id: "other",
    actions: [],
  });
  writeFileSync(
    join(directory, "device-capabilities.json"),
    JSON.stringify(capabilities),
  );
  await page.getByTestId("nav-devices").click();
  await page
    .getByRole("button", { name: "重新加载能力说明", exact: true })
    .click();
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  const action = page.locator(".action-card").first();
  await check(action.getByLabel("动作类型", { exact: true })).toHaveValue("");
  await check(action.getByLabel("动作参数 JSON", { exact: true })).toHaveCount(
    0,
  );
  await check(action.getByLabel("目标设备", { exact: true })).toHaveCount(0);
  await action
    .getByLabel("动作类型", { exact: true })
    .selectOption("camera_record");
  await action
    .getByLabel("目标设备", { exact: true })
    .selectOption("demo_cam0");
  for (const type of [
    "report_status",
    "cancel_task",
    "delete_action_outputs",
    "obtain_action_outputs",
    "",
  ]) {
    await action.getByLabel("动作类型", { exact: true }).selectOption(type);
    await check(action.getByLabel("目标设备", { exact: true })).toHaveCount(0);
  }
  await action
    .getByLabel("动作类型", { exact: true })
    .selectOption("camera_record");
  await check(action.getByLabel("目标设备", { exact: true })).toHaveValue(
    "demo_cam0",
  );
  await check(
    action
      .getByLabel("目标设备", { exact: true })
      .locator('option[value="no-record"]'),
  ).toHaveCount(0);
  await action
    .getByLabel("参数类型", { exact: true })
    .selectOption("demo_fixed");
  await action.getByLabel("执行时间", { exact: true }).fill("2026-09-16T12:00");
  await action
    .getByLabel("最大允许延迟 (max_delay_ms)", { exact: true })
    .fill("0");
  const saved = () =>
    JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0];
  await check.poll(saved).toEqual({
    name: "动作 1",
    type: "camera_record",
    device_id: "demo_cam0",
    params: { type: "demo_fixed" },
    policy: { max_delay_ms: 0 },
    scheduled_at: "2026-09-16 04:00:00",
  });
  const before = saved();
  capabilities!.devices[0].actions = [];
  writeFileSync(
    join(directory, "device-capabilities.json"),
    JSON.stringify(capabilities),
  );
  await page.getByTestId("nav-devices").click();
  await page
    .getByRole("button", { name: "重新加载能力说明", exact: true })
    .click();
  await page.getByTestId("nav-plans").click();
  await check(
    action.getByLabel("动作类型", { exact: true }).locator("option:checked"),
  ).toHaveJSProperty("disabled", true);
  await check(
    action.getByLabel("目标设备", { exact: true }).locator("option:checked"),
  ).toHaveJSProperty("disabled", true);
  await check(
    action.getByLabel("参数类型", { exact: true }).locator("option:checked"),
  ).toHaveJSProperty("disabled", true);
  expect(saved()).toEqual(before);
  await page.getByTestId("export-button").click();
  expect(app.store.all("requests")).toHaveLength(0);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(action.getByLabel("目标设备", { exact: true })).toHaveValue(
    "demo_cam0",
  );
  expect(saved()).toEqual(before);
}, 20000);
it("内置取回表单区分所属组与来源组并保存四种引用", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "取回",
      actions: [
        { name: "录像", type: "camera_record", group: "早班" },
        {
          name: "取回",
          type: "obtain_action_outputs",
          group: "错误所属组",
          params: { source: { action_name: "录像" } },
        },
      ],
    }),
  );
  await page.getByTestId("draft-json-toggle").click();
  const action = page.locator(".action-card").nth(1);
  await check(action.getByLabel("动作组 (group)", { exact: true })).toHaveCount(
    0,
  );
  await action.getByRole("button", { name: "移除不适用的动作组" }).click();
  await check(action.getByLabel("来源动作名称")).toHaveValue("录像");
  await check(action.getByLabel("指定产物筛选")).toHaveCount(0);
  await action.getByLabel("取回来源").selectOption("action_instance_id");
  await action.getByLabel("来源动作实例 ID").fill("a-1");
  await action.getByLabel("指定产物筛选").check();
  await action.getByRole("button", { name: "添加产物 ID" }).click();
  await action.getByLabel("产物 ID 1", { exact: true }).fill("out-1");
  await action.getByLabel("取回来源").selectOption("group");
  await action.getByLabel("来源组").fill("早班");
  await check(action.getByLabel("指定产物筛选")).toHaveCount(0);
  const saved = () =>
    JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[1];
  await check.poll(saved).toEqual({
    name: "取回",
    type: "obtain_action_outputs",
    params: { source: { group: "早班" } },
  });
  await action.getByLabel("取回来源").selectOption("action_instance_id");
  await action.getByLabel("来源动作实例 ID").fill("remote-action");
  await check
    .poll(() => saved().params)
    .toEqual({ source: { action_instance_id: "remote-action" } });
  await action.getByLabel("取回来源").selectOption("plan_group");
  await action.getByLabel("来源计划实例 ID").fill("remote-plan");
  await action.getByLabel("来源组").fill("夜班");
  await check
    .poll(() => saved().params)
    .toEqual({ source: { plan_instance_id: "remote-plan", group: "夜班" } });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(
    page.locator(".action-card").nth(1).getByLabel("来源组"),
  ).toHaveValue("夜班");
}, 20000);

it("删除产物通过列表编辑并由后端拒绝重复或空列表", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "清理",
      actions: [
        {
          name: "清理",
          type: "delete_action_outputs",
          scheduled_at: "2026-09-16 01:00:00",
          params: { output_ids: [] },
        },
      ],
    }),
  );
  await page.getByTestId("draft-json-toggle").click();
  await page.getByRole("button", { name: "添加产物 ID" }).click();
  await page.getByLabel("产物 ID 1", { exact: true }).fill("o-1");
  await page.getByRole("button", { name: "添加产物 ID" }).click();
  await page.getByLabel("产物 ID 2", { exact: true }).fill("o-1");
  await page.getByTestId("export-button").click();
  expect(app.store.all("requests")).toHaveLength(0);
  await page.getByRole("button", { name: "移除产物 2", exact: true }).click();
  await page.getByTestId("export-button").click();
  await check
    .poll(() => app.store.all<ExportedRequest>("requests").length)
    .toBe(1);
}, 20000);

it("取消目标四种模式互斥且手工跨计划 ID 不因本地未知被拒绝", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "取消",
      actions: [{ name: "取消", type: "cancel_task", params: {} }],
    }),
  );
  await page.getByTestId("draft-json-toggle").click();
  for (const [mode, label, key] of [
    ["request_id", "目标请求 ID", "request_id"],
    ["plan_instance_id", "目标计划实例 ID", "plan_instance_id"],
    ["action_instance_id", "目标动作实例 ID", "action_instance_id"],
  ]) {
    await page.getByLabel("取消目标").selectOption(mode);
    await page.getByLabel(label, { exact: true }).fill("remote-1");
    await check
      .poll(
        () =>
          JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0]
            ?.params,
      )
      .toEqual({ target: { [key]: "remote-1" } });
  }
  await page.getByLabel("取消目标").selectOption("plan_group");
  await page.getByLabel("目标计划实例 ID").fill("p-1");
  await page.getByLabel("目标组").fill("A");
  await page.getByTestId("export-button").click();
  await check.poll(() => app.store.all("requests").length).toBe(1);
}, 20000);

it("报告表单保留缺省与非法原值，完整同步不携带旧起点", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "报告",
      actions: [
        {
          name: "报告",
          type: "report_status",
          params: { scope: "since", after_report_id: 99 },
        },
      ],
    }),
  );
  await page.getByTestId("draft-json-toggle").click();
  await check(page.getByLabel("同步起点报告")).toHaveValue("invalid");
  await page.getByLabel("报告范围").selectOption("full");
  await check
    .poll(
      () =>
        JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0]
          ?.params,
    )
    .toEqual({ scope: "full" });
  await page.getByLabel("报告范围").selectOption("normal");
  await check
    .poll(
      () =>
        JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0],
    )
    .toEqual({ name: "报告", type: "report_status" });
  await page.getByTestId("export-button").click();
  await check.poll(() => app.store.all("requests").length).toBe(1);
}, 20000);
it("动作类型独立保存设备策略且共用无效时间仍需修正", async () => {
  const { page, app } = await setup();
  const original = {
    name: "录像",
    type: "camera_record",
    device_id: "demo_cam0",
    scheduled_at: null,
    params: { type: "demo_fixed" },
    policy: { max_delay_ms: 0 },
  };
  const draft = app.createDraft({
    text: JSON.stringify({ name: "切换", actions: [original] }),
  });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await page.getByLabel("动作类型").selectOption("report_status");
  await check
    .poll(() => JSON.parse(app.draft(draft.id).content.text).actions[0])
    .toEqual({ name: "录像", type: "report_status", scheduled_at: null });
  await check(
    page.getByRole("button", { name: /省略设备字段|移除不适用的业务策略/ }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "不指定执行时间", exact: true })
    .click();
  await page.getByLabel("报告范围").selectOption("normal");
  await page.getByTestId("export-button").click();
  await check.poll(() => app.store.all("requests").length).toBe(1);
}, 20000);

it("内置参数的非法组合与类型原样保留且未完成输入不能被表单覆盖", async () => {
  const { page, app } = await setup();
  const params = {
    source: { group: "G" },
    output_ids: [null, "o-1", "o-1"],
    extra: false,
  };
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "取回",
      actions: [{ name: "取回", type: "obtain_action_outputs", params }],
    }),
  });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  expect(
    JSON.parse(app.draft(draft.id).content.text).actions[0].params,
  ).toEqual(params);
  await page.getByRole("button", { name: "移除不适用的产物筛选" }).click();
  await page.getByRole("button", { name: "移除不适用参数字段" }).click();
  await page.getByLabel("取回来源").selectOption("action_instance_id");
  await page.getByLabel("来源动作实例 ID").fill("a-1");
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page.getByLabel("动作参数 JSON").fill('{"source":');
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  await check(page.getByLabel("取回来源")).toHaveCount(0);
  await check(page.getByLabel("动作参数 JSON")).toHaveValue('{"source":');
  await check
    .poll(
      () => app.draft(draft.id).content.pending?.["/actions/0/params"]?.text,
    )
    .toBe('{"source":');
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByLabel("动作参数 JSON")).toHaveValue('{"source":');
  await page
    .getByLabel("动作参数 JSON")
    .fill('{"source":{"action_instance_id":"a-2"}}');
  await check(page.getByLabel("来源动作实例 ID")).toHaveValue("a-2");
}, 20000);

it("报告表单仅提供可靠覆盖的报告起点且导出保持数值类型", async () => {
  const { page, app } = await setup();
  const report = mappedReport(Buffer.from("video"));
  app.applyReports([reportInput(report)]);
  expect(app.coverage()).toBe(report.to_wm);
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "同步",
      actions: [{ name: "同步", type: "report_status", params: {} }],
    }),
  });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  expect(
    JSON.parse(app.draft(draft.id).content.text).actions[0].params,
  ).toEqual({});
  await page.getByLabel("报告范围").selectOption("since");
  await check(page.getByLabel("同步起点报告")).toHaveValue("");
  await page.getByLabel("同步起点报告").selectOption(String(report.report_id));
  await page.getByTestId("export-button").click();
  await check
    .poll(() => app.store.all<ExportedRequest>("requests").length)
    .toBe(1);
  expect(
    (app.store.all<ExportedRequest>("requests")[0].body.actions as any[])[0]
      .params,
  ).toEqual({ scope: "since", after_report_id: report.report_id });
}, 20000);

it.each([null, [], "text", false])(
  "非法内置参数无需手写JSON即可明确重新填写：%j",
  async (params) => {
    const { page, app } = await setup();
    const draft = app.createDraft({
      text: JSON.stringify({
        name: "取消",
        actions: [{ name: "取消", type: "cancel_task", params }],
      }),
    });
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    expect(
      JSON.parse(app.draft(draft.id).content.text).actions[0].params,
    ).toEqual(params);
    await page.getByRole("button", { name: "清空参数并重新填写" }).click();
    await page.getByLabel("取消目标").selectOption("request_id");
    await page.getByLabel("目标请求 ID").fill("req-1");
    await page.getByTestId("export-button").click();
    await check.poll(() => app.store.all("requests").length).toBe(1);
  },
  20000,
);

it("Schema普通控件支持本地引用，合法预设不依赖整份计划完成", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_adjustable");
  await page.getByLabel("分辨率 (resolution)").selectOption("0");
  await check(
    page.getByLabel("帧率 (frame_rate_fps)").locator("option"),
  ).toHaveText(["请选择", "30"]);
  await page.getByLabel("分辨率 (resolution)").selectOption("1");
  await page.getByLabel("帧率 (frame_rate_fps)").selectOption("1");
  await page.getByText("拍摄参数预设", { exact: true }).click();
  await page.getByLabel("预设名称").fill("巡检");
  await page.getByRole("button", { name: "保存为新预设" }).click();
  await check.poll(() => app.store.all<Preset>("presets").length).toBe(1);
  expect(app.store.all<Preset>("presets")[0].params).toEqual({
    type: "demo_adjustable",
    resolution: "1080p",
    frame_rate_fps: 60,
  });
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(
    JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0]
      .scheduled_at,
  ).toBeUndefined();
  await page.getByLabel("帧率 (frame_rate_fps)").selectOption("0");
  await page.getByRole("button", { name: "应用预设", exact: true }).click();
  await check(page.getByLabel("帧率 (frame_rate_fps)")).toHaveValue("1");
}, 20000);
it("普通字段没有重复值和省略，必填缺失阻止导出，字符串选项按原文显示", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await page.getByLabel("最大允许延迟 (max_delay_ms)").fill("0");
  const field = page
    .locator(".field")
    .filter({ has: page.getByLabel("最大允许延迟 (max_delay_ms)") });
  await check(
    field.getByRole("button", { name: "省略", exact: true }),
  ).toHaveCount(0);
  await check(field).not.toContainText("当前值");
  await page.getByText("完整业务策略 JSON", { exact: true }).click();
  await check(
    page.getByRole("button", { name: "省略策略字段", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_adjustable");
  await check(
    page.getByLabel("分辨率 (resolution)").locator("option"),
  ).toHaveText(["请选择", "4K", "1080p"]);
  await page.getByLabel("最大允许延迟 (max_delay_ms)").fill("");
  await page.getByTestId("export-button").click();
  expect(app.store.all("requests")).toHaveLength(0);
}, 20000);
it("JSON带入非法组合保留原值并通过联动修正", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_adjustable");
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page
    .getByLabel("参数 JSON 文本")
    .fill('{"type":"demo_adjustable","resolution":"4K","frame_rate_fps":60}');
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  await check(
    page.getByLabel("帧率 (frame_rate_fps)").locator('option[value="invalid"]'),
  ).toHaveJSProperty("disabled", true);
  await check(page.getByLabel("帧率 (frame_rate_fps)")).toHaveValue("invalid");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(
    JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0].params
      .frame_rate_fps,
  ).toBe(60);
  await page.getByLabel("帧率 (frame_rate_fps)").selectOption("0");
  await check(page.getByLabel("分辨率 (resolution)")).toHaveValue("0");
}, 20000);
it("动作折叠保留未完成输入和问题提示，删除后保持对应关系", async () => {
  const { page } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await page.getByLabel("动作名称", { exact: true }).fill("第一段");
  await page.getByLabel("最大允许延迟 (max_delay_ms)").fill("1e");
  await page.getByRole("button", { name: "收起动作", exact: true }).click();
  await check(page.getByLabel("最大允许延迟 (max_delay_ms)")).toBeHidden();
  await check(page.locator(".action-summary")).toContainText("待修正");
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await check(
    page.getByLabel("动作名称", { exact: true }).last(),
  ).toBeVisible();
  await page.getByRole("button", { name: "展开动作", exact: true }).click();
  await check(
    page.getByLabel("最大允许延迟 (max_delay_ms)").first(),
  ).toHaveValue("1e");
  await page
    .getByRole("button", { name: "收起动作", exact: true })
    .last()
    .click();
  await page
    .getByRole("button", { name: "删除动作", exact: true })
    .first()
    .click();
  await check(
    page.getByRole("button", { name: "展开动作", exact: true }),
  ).toHaveCount(1);
  await page.getByRole("button", { name: "全部展开", exact: true }).click();
  await check(page.getByLabel("动作名称", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "全部收起", exact: true }).click();
  await check(page.getByLabel("动作名称", { exact: true })).toBeHidden();
}, 20000);
it("设备指南展示任务及三个允许组合，技术信息默认折叠且窄屏不溢出", async () => {
  const { page } = await setup();
  await page.getByRole("button", { name: "设备说明" }).click();
  const guide = page.getByTestId("device-guide");
  await check(
    guide.getByRole("heading", { name: "演示：可调整画质的录像任务" }),
  ).toBeVisible();
  const rows = guide.getByRole("table").getByRole("row");
  await check(rows).toHaveCount(4);
  await check(rows.nth(1)).toContainText("4K");
  await check(rows.nth(1)).toContainText("30");
  await check(rows.nth(2)).toContainText("1080p");
  await check(rows.nth(3)).toContainText("60");
  await check(guide.locator("pre").first()).toBeHidden();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
}, 20000);
it("能力重载同时更新联动与指南，原草稿不被改写", async () => {
  const { page, app } = await setup();
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "重载检查",
      actions: [
        {
          name: "录像",
          type: "camera_record",
          device_id: "demo_cam0",
          params: {
            type: "demo_adjustable",
            resolution: "4K",
            frame_rate_fps: 30,
          },
          policy: { max_delay_ms: 0 },
        },
      ],
    }),
  });
  const before = app.draft(draft.id).content;
  const caps = structuredClone(app.capabilities.active!);
  caps.devices[0].actions[0].parameter_types[0].schema.then = {
    properties: { frame_rate_fps: { const: 60 } },
  };
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify(caps),
  );
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  const first = page
    .getByTestId("device-guide")
    .getByRole("table")
    .getByRole("row")
    .nth(1);
  await check(first.getByRole("cell")).toHaveText(["4K", "60"]);
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByLabel("帧率 (frame_rate_fps)")).toHaveValue("invalid");
  await check(
    page.getByLabel("帧率 (frame_rate_fps)").locator("option:not([disabled])"),
  ).toHaveText(["请选择", "60"]);
  expect(app.draft(draft.id).content).toEqual(before);
}, 20000);
it("设备指南区分空目录、没有拍摄能力与加载失败后保留旧说明", async () => {
  const { page, app } = await setup();
  await page.getByTestId("nav-devices").click();
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    '{"devices":[]}',
  );
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(page.getByTestId("device-guide")).toContainText("尚未配置设备");
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify({
      devices: [
        { device_id: "empty_cam", driver_id: "empty_driver", actions: [] },
      ],
    }),
  );
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(page.getByTestId("device-guide")).toContainText(
    "未提供可用的拍摄任务",
  );
  writeFileSync(join(app.store.directory, "device-capabilities.json"), "{bad");
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page
      .getByTestId("device-guide")
      .getByRole("heading", { name: "empty_cam" }),
  ).toBeVisible();
  await check(
    page.getByText("继续使用此前成功启用的说明。", { exact: true }),
  ).toBeVisible();
}, 20000);
it.each(["open", "empty"])(
  "没有普通字段时仍准确表达规则状态：%s",
  async (mode) => {
    const { page, app } = await setup();
    const caps = structuredClone(app.capabilities.active!);
    const schema = caps.devices[0].actions[0].parameter_types[1].schema;
    if (mode === "open") delete schema.additionalProperties;
    else schema.not = {};
    writeFileSync(
      join(app.store.directory, "device-capabilities.json"),
      JSON.stringify(caps),
    );
    app.reloadCapabilities();
    await page.reload();
    await page.getByTestId("new-draft-button").click();
    await page.getByRole("button", { name: "添加动作", exact: true }).click();
    await page
      .getByLabel("动作类型", { exact: true })
      .last()
      .selectOption("camera_record");
    await page.getByLabel("目标设备").selectOption("demo_cam0");
    await page.getByLabel("参数类型").selectOption("demo_fixed");
    await check(page.locator(".action-card")).not.toContainText(
      "该任务使用固定设置，无需填写其他参数",
    );
    await check(page.locator(".action-card")).toContainText(
      mode === "open" ? "参数 JSON" : "没有允许的参数组合",
    );
  },
  20000,
);
it("未完成参数JSON切换视图和刷新后仍可修正", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_fixed");
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page.getByLabel("参数 JSON 文本").fill('{"type":');
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  await check(page.getByLabel("参数 JSON 文本")).toHaveValue('{"type":');
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(
    Object.values(app.store.all<Draft>("drafts")[0].content.pending ?? {})[0]
      .text,
  ).toBe('{"type":');
  await page.reload();
  await page.getByTestId("draft-open-button").first().click();
  await check(page.getByLabel("参数 JSON 文本")).toHaveValue('{"type":');
  await page.getByLabel("参数 JSON 文本").fill('{"type":"removed"}');
  await check(page.getByTestId("validation-issues")).toContainText(
    "params.type",
  );
}, 20000);
it("保存响应延迟时立即导出仍使用最新完整内容", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  let release!: () => void;
  const held = new Promise<void>((r) => (release = r));
  let entered!: () => void;
  const started = new Promise<void>((r) => (entered = r));
  let once = true;
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "PUT" && once) {
      once = false;
      const response = await route.fetch();
      entered();
      await held;
      await route.fulfill({ response });
    } else await route.continue();
  });
  const plan = (name: string) =>
    JSON.stringify({
      name,
      actions: [
        { name: "同步", type: "report_status", params: { scope: "full" } },
      ],
    });
  await page.getByTestId("draft-json-input").fill(plan("较早"));
  await started;
  await page.getByTestId("draft-json-input").fill(plan("最新"));
  await page.getByTestId("export-button").click();
  expect(app.store.all("requests")).toHaveLength(0);
  release();
  await check.poll(() => app.store.all("requests").length).toBe(1);
  expect(
    app.store.all<{ body: { name: string } }>("requests")[0].body.name,
  ).toBe("最新");
}, 20000);
it("准备同步追加到所选已有草稿并保留动作与未完成输入", async () => {
  const { page, app } = await setup();
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "后续计划",
      actions: [{ name: "原动作", type: "report_status" }],
    }),
    pending: { "/actions/0/params": { kind: "json", text: "{" } },
  });
  await page.reload();
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByLabel("目标草稿").selectOption(draft.id);
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check
    .poll(() => JSON.parse(app.draft(draft.id).content.text).actions.length)
    .toBe(2);
  const updated = app.draft(draft.id);
  expect(updated.content.pending).toEqual({
    "/actions/0/params": { kind: "json", text: "{" },
  });
  expect(JSON.parse(updated.content.text).actions[1]).toEqual({
    name: "状态同步",
    type: "report_status",
    params: { scope: "full" },
  });
}, 20000);
it("添加动作保留已有动作的未完成参数并继续阻止导出", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page.getByLabel("参数 JSON 文本").fill("{");
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const content = app.store.all<Draft>("drafts")[0].content;
  expect(content.pending?.["/actions/0/params"]).toEqual({
    kind: "json",
    text: "{",
  });
  expect(JSON.parse(content.text).actions).toHaveLength(2);
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("validation-issues")).toContainText(
    "unfinished_input",
  );
  expect(app.store.all("requests")).toHaveLength(0);
}, 20000);
it("后续动作响应丢失后依据草稿内容确认追加成功", async () => {
  const { page, app } = await setup();
  const draft = app.createDraft({
    text: JSON.stringify({
      name: "后续计划",
      actions: [{ name: "原动作", type: "report_status" }],
    }),
  });
  await page.reload();
  let once = true;
  await page.route("**/api/drafts/*/actions", async (route) => {
    if (once) {
      once = false;
      await route.fetch();
      await route.abort();
    } else await route.continue();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByLabel("目标草稿").selectOption(draft.id);
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  expect(JSON.parse(app.draft(draft.id).content.text).actions).toHaveLength(2);
  await check(page.getByTestId("save-status")).toContainText("已保存");
}, 20000);
it("导出回执丢失后仍打开已保存的同一原请求", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "回执核实",
      actions: [{ name: "同步", type: "report_status" }],
    }),
  );
  await page.route("**/api/drafts/*/export", async (route) => {
    await route.fetch();
    await route.abort();
  });
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("download-request-button")).toBeVisible();
  expect(app.store.all("requests")).toHaveLength(1);
}, 20000);
it("普通字段保留缺省和显式空值，重载说明不写入Schema默认值", async () => {
  const { page, app } = await setup();
  const caps = structuredClone(app.capabilities.active!);
  caps.devices[0].actions[0].parameter_types[1].schema = {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    properties: {
      type: { const: "demo_fixed" },
      enabled: { type: "boolean", title: "启用" },
      count: { type: "number", title: "次数", default: 8 },
      note: { type: ["string", "null"], title: "备注" },
      choice: { enum: [false, 0, null, ""], title: "选项" },
    },
    required: ["type"],
    additionalProperties: false,
  };
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify(caps),
  );
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_fixed");
  await check(
    page.getByRole("checkbox", { name: "填写次数 (count)", exact: true }),
  ).toHaveCount(0);
  await check(page.getByLabel("次数 (count)", { exact: true })).toHaveValue("");
  await page.getByLabel("启用 (enabled)", { exact: true }).fill("false");
  await page.getByLabel("次数 (count)", { exact: true }).fill("0");
  await page.getByRole("button", { name: "设为 null", exact: true }).click();
  await page.getByLabel("选项 (choice)", { exact: true }).fill('""');
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const draft = app.store.all<Draft>("drafts")[0];
  expect(JSON.parse(draft.content.text).actions[0]?.params).toEqual({
    type: "demo_fixed",
    enabled: false,
    count: 0,
    note: null,
    choice: "",
  });
  const before = draft.content.text;
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await page.getByTestId("nav-plans").click();
  expect(app.draft(draft.id).content.text).toBe(before);
}, 20000);
it("人工标记清除只更正交接记录并保留固定正文", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "人工交接",
      actions: [{ name: "同步", type: "report_status" }],
    }),
  );
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("download-request-button")).toBeVisible();
  const record = app.store.all<ExportedRequest>("requests")[0];
  await page.getByRole("button", { name: "标记已递交", exact: true }).click();
  await check.poll(() => app.request(record.id).handedAt).not.toBeNull();
  const marked = app.request(record.id).handedAt;
  await page.getByTestId("download-request-button").click();
  expect(app.request(record.id).handedAt).toBe(marked);
  await page.getByRole("button", { name: "清除递交标记", exact: true }).click();
  await check.poll(() => app.request(record.id).handedAt).toBeNull();
  expect(app.request(record.id).body).toEqual(record.body);
  expect(app.snapshot().plans).toBeUndefined();
}, 20000);
it("预设创建回执丢失时展示未确认并读取实际记录，不重复创建", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .last()
    .selectOption("camera_record");
  await page.getByLabel("目标设备").selectOption("demo_cam0");
  await page.getByLabel("参数类型").selectOption("demo_fixed");
  await page.getByText("拍摄参数预设", { exact: true }).click();
  await page.getByLabel("预设名称").fill("回执待核实");
  await page.route("**/api/presets", async (route) => {
    await route.fetch();
    await route.abort();
  });
  await page.getByRole("button", { name: "保存为新预设" }).click();
  await check(page.locator(".preset-box")).toContainText("尚未确认");
  expect(app.store.all("presets")).toHaveLength(1);
  await check(page.getByLabel("已有预设").locator("option")).toHaveCount(2);
}, 20000);

it("新录像参数缺省时引导选择并默认折叠预设", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  await page
    .getByLabel("动作类型", { exact: true })
    .selectOption("camera_record");
  await check(page.getByLabel("参数 JSON 文本", { exact: true })).toHaveCount(
    0,
  );
  await check(page.getByLabel("已有预设")).not.toBeVisible();
  await page.getByLabel("目标设备", { exact: true }).selectOption("demo_cam0");
  await page.getByLabel("参数类型", { exact: true }).selectOption("demo_fixed");
  await check(page.getByLabel("参数 JSON 文本", { exact: true })).toHaveCount(
    0,
  );
  await page.getByText("拍摄参数预设", { exact: true }).click();
  await check(page.getByLabel("已有预设")).toBeVisible();
  await check
    .poll(
      () =>
        JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0]
          ?.params,
    )
    .toEqual({ type: "demo_fixed" });
});
it("执行时间按本地显示并可点击文本区打开选择器", async () => {
  const { page, app } = await setup();
  app.createDraft({
    text: JSON.stringify({
      name: "时间计划",
      actions: [
        {
          name: "同步",
          type: "report_status",
          scheduled_at: "2026-09-16 04:30:00",
        },
      ],
    }),
  });
  await page.getByTestId("draft-open-button").click();
  const input = page.getByLabel("执行时间", { exact: true });
  await check(input).toHaveValue("2026-09-16T12:30");
  await check(page.locator(".action-summary")).toContainText(
    "2026-09-16 12:30:00",
  );
  await check(page.locator(".editor")).not.toContainText("UTC");
  await input.evaluate((el) => {
    (window as any).pickerCalls = 0;
    (el as HTMLInputElement).showPicker = () => {
      (window as any).pickerCalls++;
    };
  });
  await input.click({ position: { x: 20, y: 12 } });
  expect(await page.evaluate(() => (window as any).pickerCalls)).toBe(1);
  await input.fill("2026-09-17T09:10");
  await check
    .poll(
      () =>
        JSON.parse(app.store.all<Draft>("drafts")[0].content.text).actions[0]
          .scheduled_at,
    )
    .toBe("2026-09-17 01:10:00");
});
it("确认删除草稿后刷新不恢复且取消时保留输入", async () => {
  const { page, app } = await setup();
  await page.getByTestId("new-draft-button").click();
  await page.getByLabel("计划名称", { exact: true }).fill("待删除的草稿");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "删除草稿", exact: true }).click();
  await check(page.getByLabel("计划名称", { exact: true })).toHaveValue(
    "待删除的草稿",
  );
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除草稿", exact: true }).click();
  await check(page.getByTestId("draft-open-button")).toHaveCount(0);
  expect(app.store.all("drafts")).toEqual([]);
  await page.reload();
  await check(page.getByTestId("new-draft-button")).toBeVisible();
  await check(page.getByTestId("draft-open-button")).toHaveCount(0);
});

it.each([true, false])(
  "删除响应丢失时可靠核实实际结果：已提交 %s",
  async (committed) => {
    const { page, app } = await setup();
    await page.getByTestId("new-draft-button").click();
    await page.getByLabel("计划名称", { exact: true }).fill("删除核实");
    await page.route("**/api/drafts/*", async (route) => {
      if (route.request().method() !== "DELETE") return route.continue();
      if (committed) await route.fetch();
      await route.abort();
    });
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "删除草稿", exact: true }).click();
    if (committed) {
      await check(page.getByTestId("draft-open-button")).toHaveCount(0);
      expect(app.store.all("drafts")).toEqual([]);
    } else {
      await check(page.getByRole("alert")).toBeVisible();
      await check(page.getByLabel("计划名称", { exact: true })).toBeEnabled();
      expect(app.store.all<Draft>("drafts")).toHaveLength(1);
      await check(page.getByLabel("计划名称", { exact: true })).toHaveValue(
        "删除核实",
      );
    }
  },
);
it.each(["network", "fault"] as const)(
  "删除核实不可用保持输入与锁定：%s",
  async (failure) => {
    const { page, app } = await setup();
    await page.getByTestId("new-draft-button").click();
    await page.getByLabel("计划名称", { exact: true }).fill("待核实删除");
    let uncertain = false;
    await page.route("**/api/state", async (route) => {
      if (!uncertain) return route.continue();
      if (failure === "network") return route.abort();
      const actual = app.state();
      await route.fulfill({
        json: {
          startup: { ...actual.startup, state: "fault" },
          capabilities: actual.capabilities,
        },
      });
    });
    await page.route("**/api/drafts/*", async (route) => {
      if (route.request().method() !== "DELETE") return route.continue();
      await route.fetch();
      uncertain = true;
      await route.abort();
    });
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "删除草稿", exact: true }).click();
    // 自动轮询可能显示故障页；恢复读取后会话仍必须保持未确认锁定。
    await check.poll(() => uncertain).toBe(true);
    uncertain = false;
    await page.unroute("**/api/state");
    await check(
      page.getByRole("button", { name: "重新核实删除结果" }),
    ).toBeVisible();
    await check(page.getByLabel("计划名称", { exact: true })).toHaveValue(
      "待核实删除",
    );
    await check(page.getByLabel("计划名称", { exact: true })).toBeDisabled();
    await check(page.getByTestId("export-button")).toBeDisabled();
    await page.getByRole("button", { name: "重新核实删除结果" }).click();
    await check(page.getByTestId("draft-open-button")).toHaveCount(0);
    await check(
      page.getByRole("heading", { name: "编辑草稿", exact: true }),
    ).toHaveCount(0);
  },
);
