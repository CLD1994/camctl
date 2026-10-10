import { pendingInput, pendingAction } from "./pending-support";
import { choose } from "./select-support";
import { beforeAll, afterAll, afterEach, expect, it } from "vitest";
import {
  chromium,
  expect as browserExpect,
  type Browser,
} from "@playwright/test";
import {
  mkdtempSync,
  rmSync,
  copyFileSync,
  readFileSync,
  readdirSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Server } from "node:http";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { Draft } from "../../src/server/models";
import { makeMediaFixture } from "../helpers/media";
import { mappedReport, reportInput } from "./fixtures";

let browser: Browser;
let fixtures: Awaited<ReturnType<typeof makeMediaFixture>>;
let fixtureDirectory: string;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
  fixtureDirectory = mkdtempSync(join(tmpdir(), "camctl-browser-media-"));
  fixtures = await makeMediaFixture(fixtureDirectory);
}, 20000);
afterAll(async () => {
  await browser?.close();
  if (fixtureDirectory)
    rmSync(fixtureDirectory, { recursive: true, force: true });
});
afterEach(async () => {
  for (const c of clean.splice(0)) await c();
});
async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-browser-"));
  copyFileSync(
    "../../protocol/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"),
  );
  const application = new Application(directory);
  const files = new Files(application);
  const lifecycle = new RequestLifecycle();
  const server = await new Promise<Server>((resolve) => {
    const s = createHttpApp(application, files, lifecycle).listen(
      0,
      "127.0.0.1",
      () => resolve(s),
    );
  });
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((e) => (e ? reject(e) : resolve())),
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
  const response = await page.goto(base);
  expect(response?.status(), "构建后的客户端首页应可访问").toBe(200);
  return { application, files, page, base };
}
it("网页初始化并恢复未完成的草稿输入", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByTestId("draft-json-input").fill('{"name":"未完成",');
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  expect(
    application.store.all<{ content: { text: string } }>("drafts")[0].content
      .text,
  ).toBe('{"name":"未完成",');
  await page.reload();
  await page.getByTestId("draft-open-button").first().click();
  await page.getByTestId("draft-json-toggle").click();
  await browserExpect(page.getByTestId("draft-json-input")).toHaveValue(
    '{"name":"未完成",',
  );
}, 20000);
it("报告独有详情按准确对象准备取回清理和取消，主机事实保持", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("nav-import").click();
  await page.getByTestId("import-files").setInputFiles(fixtures.reportPath);
  await browserExpect.poll(() => application.coverage()).toBe(20);
  const snapshot = JSON.stringify(application.snapshot());
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  await page
    .getByRole("button", { name: /^展开动作 / })
    .first()
    .waitFor();
  while (await page.getByRole("button", { name: /^展开动作 / }).count())
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .click();
  for (const summary of await page
    .getByText("更多操作与交付记录", { exact: true })
    .all())
    await summary.click();
  await browserExpect(page.getByTestId("download-request-button")).toHaveCount(
    0,
  );
  await browserExpect(page.getByTestId("copy-request-button")).toHaveCount(0);
  await page
    .getByRole("button", { name: "准备取回", exact: true })
    .first()
    .click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  const draft = application.store.all<Draft>("drafts")[0];
  expect(JSON.parse(draft.content.text).actions[0]).toEqual({
    name: "取回产物",
    type: "obtain_action_outputs",
    params: { source: { action_instance_id: "1" }, output_ids: ["4"] },
  });
  const append = async (button: string) => {
    await page.getByTestId("tab-records").click();
    await page.getByTestId("record-open-button").first().click();
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .waitFor();
    while (await page.getByRole("button", { name: /^展开动作 / }).count())
      await page
        .getByRole("button", { name: /^展开动作 / })
        .first()
        .click();
    for (const summary of await page
      .getByText("更多操作与交付记录", { exact: true })
      .all())
      await summary.click();
    await page
      .getByRole("button", { name: button, exact: true })
      .first()
      .click();
    await choose(page.getByLabel("目标草稿"), draft.id);
    await page.getByRole("button", { name: "加入草稿", exact: true }).click();
    await browserExpect(page.getByRole("dialog")).toHaveCount(0);
  };
  await append("准备清理源产物");
  await append("准备取消动作");
  await append("准备取消计划");
  await append("准备取回计划默认产物");
  await append("准备清理计划全部源产物");
  await append("准备取回默认产物");
  await append("准备清理动作全部源产物");
  const actions = JSON.parse(application.draft(draft.id).content.text).actions;
  expect(actions.map((a: { params: unknown }) => a.params)).toEqual([
    { source: { action_instance_id: "1" }, output_ids: ["4"] },
    { output_ids: ["4"] },
    { target: { action_instance_id: "1" } },
    { target: { plan_instance_id: "1" } },
    { source: { plan_instance_id: "1" } },
    { source: { plan_instance_id: "1" } },
    { source: { action_instance_id: "1" } },
    { source: { action_instance_id: "1" } },
  ]);
  expect(
    actions.every(
      (a: Record<string, unknown>) => !Object.hasOwn(a, "scheduled_at"),
    ),
  ).toBe(true);
  expect(JSON.stringify(application.snapshot())).toBe(snapshot);
  for (const input of await page.getByLabel("执行时间", { exact: true }).all())
    await input.fill("2026-10-12T10:00");
  await page.getByTestId("export-button").click();
  await browserExpect(
    page.getByTestId("download-request-button"),
  ).toBeVisible();
  const request = application.store.all<{ body: { actions: unknown[] } }>(
    "requests",
  )[0];
  expect(request.body.actions).toMatchObject(
    actions.map((a: object) => ({ ...a, scheduled_at: expect.any(String) })),
  );
  expect(JSON.stringify(application.snapshot())).toBe(snapshot);
}, 20000);
it("报告只有历史计划身份时仍可准备范围动作并核实追加回执丢失", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const report = mappedReport(Buffer.from("probe"));
  const plan = report.plans![0];
  plan.actions = undefined;
  plan.plan_instance_id = "9223372036854775807";
  application.applyReports([reportInput(report)]);
  const draft = application.createDraft({
    text: '{"name":"目标","actions":[{"name":"同步","type":"report_status","params":{"scope":"full"}}]}',
  });
  await page.reload();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").click();
  await page.getByRole("button", { name: "准备取回计划默认产物" }).click();
  await browserExpect(page.getByRole("dialog")).toContainText("默认产物");
  await choose(page.getByLabel("目标草稿"), draft.id);
  await page.route("**/api/drafts/*/actions", async (route) => {
    await route.fetch();
    await route.abort();
  });
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await browserExpect(page.getByRole("dialog")).toHaveCount(0);
  const actions = JSON.parse(application.draft(draft.id).content.text).actions;
  expect(actions).toHaveLength(2);
  expect(actions[1].params).toEqual({
    source: { plan_instance_id: "9223372036854775807" },
  });
  expect(application.store.all("drafts")).toHaveLength(1);
}, 20000);

it.each(["camera_take_photo", "camera_timelapse", "camera_record"] as const)(
  "拍摄 %s 在无产物且未开始时可准备清理，失败核实后仍使用同一目标",
  async (type) => {
    const { page, application } = await setup();
    await page.getByTestId("initialize-button").click();
    const report = mappedReport(Buffer.from("probe"));
    report.plans![0].status = "pending";
    report.plans![0].actions = [
      {
        action_instance_id: "99",
        name: "未开始拍摄",
        type,
        status: "pending",
        device_id: "demo_cam0",
        scheduled_at: "2026-10-12 01:00:00",
        input_params: { type: "demo_fixed" },
        effective_params: { type: "demo_fixed" },
        policy: { max_delay_ms: 0 },
      },
    ];
    application.applyReports([reportInput(report)]);
    const draft = application.createDraft({
      text: '{"name":"目标","actions":[]}',
    });
    await page.reload();
    await page.getByTestId("tab-records").click();
    await page.getByTestId("record-open-button").click();
    await page.getByRole("button", { name: "展开动作 未开始拍摄" }).click();
    await page.getByRole("button", { name: "准备清理动作全部源产物" }).click();
    await choose(page.getByLabel("目标草稿"), draft.id);
    let once = true;
    await page.route("**/api/drafts/*/actions", async (route) => {
      if (once) {
        once = false;
        await route.fulfill({ status: 500, json: { error: "写入失败" } });
      } else await route.continue();
    });
    await page.getByRole("button", { name: "加入草稿", exact: true }).click();
    await browserExpect(
      page.getByRole("button", { name: "重试同一目标追加", exact: true }),
    ).toBeVisible();
    expect(
      JSON.parse(application.draft(draft.id).content.text).actions,
    ).toEqual([]);
    await page
      .getByRole("button", { name: "重试同一目标追加", exact: true })
      .click();
    await browserExpect(page.getByRole("dialog")).toHaveCount(0);
    const actions = JSON.parse(
      application.draft(draft.id).content.text,
    ).actions;
    expect(actions).toHaveLength(1);
    expect(actions[0].params).toEqual({ source: { action_instance_id: "99" } });
  },
  20000,
);

it("动作详情按受理失败与无开始依据的取消终态展示事实", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const report = mappedReport(Buffer.from("probe"));
  const capture = report.plans![0].actions!.find(
    (a) => a.type === "camera_record",
  )!;
  report.plans![0].actions = [
    {
      ...capture,
      action_instance_id: "21",
      name: "受理失败",
      status: "failed",
      effective_params: undefined,
      outputs: undefined,
      result: undefined,
      error: {
        code: "action_validation_failed",
        stage: "admission",
        details: { message: "参数错误" },
      },
    },
    {
      ...capture,
      action_instance_id: "22",
      name: "取消经历未知",
      status: "canceled",
      outputs: undefined,
      result: undefined,
      error: undefined,
    },
  ];
  application.applyReports([reportInput(report)]);
  await page.reload();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").click();
  await page.getByRole("button", { name: "展开动作 受理失败" }).click();
  await page.getByRole("button", { name: "展开动作 取消经历未知" }).click();
  const admission = page
    .locator(".result-card")
    .filter({ hasText: "受理失败" });
  await browserExpect(admission).toContainText("未执行");
  await browserExpect(admission).not.toContainText("执行已开始");
  const canceled = page
    .locator(".result-card")
    .filter({ hasText: "取消经历未知" });
  await browserExpect(canceled).toContainText("未提供开始经历");
  await browserExpect(canceled).not.toContainText("执行已开始");
}, 20000);

it("原文件预览和修复成品的精确操作保持所选 ID，不扩大来源范围", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const report = mappedReport(Buffer.from("probe"));
  const capture = report.plans![0].actions!.find(
    (a) => a.type === "camera_record",
  )!;
  const original = capture.outputs![0];
  capture.outputs = [
    { ...original, output_id: "4", original_name: "原文件" },
    {
      ...original,
      output_id: "5",
      original_name: "预览",
      kind: "preview",
      preview_of_output_id: "4",
    },
    {
      ...original,
      output_id: "6",
      original_name: "修复成品",
      kind: "repaired",
      derived_from_output_id: "4",
    },
  ];
  report.plans![0].actions = [capture];
  application.applyReports([reportInput(report)]);
  const snapshot = JSON.stringify(application.snapshot());
  const draft = application.createDraft({
    text: '{"name":"精确目标","actions":[]}',
  });
  await page.reload();
  for (const [name, id] of [
    ["原文件", "4"],
    ["预览", "5"],
    ["修复成品", "6"],
  ]) {
    for (const operation of ["准备取回", "准备清理源产物"]) {
      await page.getByTestId("tab-records").click();
      await page.getByTestId("record-open-button").click();
      await page.getByRole("button", { name: /^展开动作 / }).click();
      const product = page
        .locator(".product-card")
        .filter({ has: page.getByText(name, { exact: true }) });
      await product.getByText("更多操作与交付记录", { exact: true }).click();
      await product
        .getByRole("button", { name: operation, exact: true })
        .click();
      await choose(page.getByLabel("目标草稿"), draft.id);
      await page.getByRole("button", { name: "加入草稿", exact: true }).click();
      await browserExpect(page.getByRole("dialog")).toHaveCount(0);
      const action = JSON.parse(
        application.draft(draft.id).content.text,
      ).actions.at(-1);
      expect(action.params).toEqual(
        operation === "准备取回"
          ? {
              source: { action_instance_id: capture.action_instance_id },
              output_ids: [id],
            }
          : { output_ids: [id] },
      );
    }
  }
  expect(JSON.stringify(application.snapshot())).toBe(snapshot);
}, 30000);

it("交付来源缺少快照时仍按同源或跨来源选择准备精确取回", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const report = mappedReport(Buffer.from("probe"));
  const obtain = report.plans![0].actions!.find(
    (a) => a.type === "obtain_action_outputs",
  )!;
  const delivery = obtain.deliveries![0];
  obtain.deliveries = [
    {
      ...delivery,
      delivery_id: "81",
      output_id: "41",
      source_action_instance_id: "91",
      file_name: "81.mp4",
      display_name: "来源甲一",
    },
    {
      ...delivery,
      delivery_id: "82",
      output_id: "42",
      source_action_instance_id: "91",
      file_name: "82.mp4",
      display_name: "来源甲二",
    },
    {
      ...delivery,
      delivery_id: "83",
      output_id: "43",
      source_action_instance_id: "92",
      file_name: "83.mp4",
      display_name: "来源乙",
    },
  ];
  report.plans![0].actions = [obtain];
  application.applyReports([reportInput(report)]);
  const draft = application.createDraft({
    text: '{"name":"选择目标","actions":[]}',
  });
  await page.reload();
  const open = async () => {
    await page.getByTestId("tab-records").click();
    await page.getByTestId("record-open-button").click();
    await page.getByRole("button", { name: /^展开动作 / }).click();
  };
  const append = async () => {
    await choose(page.getByLabel("目标草稿"), draft.id);
    await page.getByRole("button", { name: "加入草稿", exact: true }).click();
    await browserExpect(page.getByRole("dialog")).toHaveCount(0);
  };
  await open();
  await page.getByLabel("选择 来源甲一").check();
  await page.getByLabel("选择 来源甲二").check();
  await page
    .getByRole("button", { name: "准备取回所选产物", exact: true })
    .click();
  await append();
  await open();
  await page.getByLabel("选择 来源甲一").check();
  await page.getByLabel("选择 来源乙", { exact: true }).check();
  await browserExpect(
    page.getByRole("button", { name: "准备取回所选产物", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: /准备取回来源 92 的所选/ }).click();
  await append();
  expect(
    JSON.parse(application.draft(draft.id).content.text).actions.map(
      (a: { params: unknown }) => a.params,
    ),
  ).toEqual([
    { source: { action_instance_id: "91" }, output_ids: ["41", "42"] },
    { source: { action_instance_id: "92" }, output_ids: ["43"] },
  ]);
}, 25000);
it("视频上传仍被挂起时报告可以应用并查看结果", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  let release!: () => void;
  const hold = new Promise<void>((r) => (release = r));
  await page.route("**/api/imports/*/content", async (route) => {
    const id = route.request().url().split("/").at(-2);
    const item = application.store
      .all<{ id: string; kind: string }>("imports")
      .find((f) => f.id === id);
    if (item?.kind !== "report") await hold;
    await route.continue();
  });
  try {
    await page.getByTestId("nav-import").click();
    await page
      .getByTestId("import-files")
      .setInputFiles([fixtures.videoPath, fixtures.reportPath]);
    await browserExpect.poll(() => application.coverage()).toBe(20);
    expect(application.store.all("videos")).toHaveLength(0);
    await page.getByTestId("nav-plans").click();
    await page.getByTestId("tab-records").click();
    await browserExpect(page.getByTestId("record-open-button")).toHaveCount(1);
  } finally {
    release();
  }
  await browserExpect
    .poll(() => application.store.all<{ status: string }>("videos")[0]?.status)
    .toBe("verified");
}, 20000);
it("未发布交付没有本地文件时不暗示文件等待送达", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("nav-import").click();
  await page.getByTestId("import-files").setInputFiles(
    join(
      "../../protocol/examples/client-protocol/03-obtain-partial",
      readdirSync(
        "../../protocol/examples/client-protocol/03-obtain-partial",
      ).find((name) => /^status-report-1-[0-9a-f]{64}\.json$/.test(name))!,
    ),
  );
  await browserExpect.poll(() => application.snapshot().plans?.length).toBe(1);
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  await page
    .getByRole("button", { name: /^展开动作 / })
    .first()
    .waitFor();
  while (await page.getByRole("button", { name: /^展开动作 / }).count())
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .click();
  for (const summary of await page
    .getByText("更多操作与交付记录", { exact: true })
    .all())
    await summary.click();
  const failed = page.locator(".delivery").filter({ hasText: "3.mp4" }).first();
  await browserExpect(failed).toContainText("尚无本地副本");
  await browserExpect(failed).not.toContainText("等待接收");
  const published = page
    .locator(".delivery")
    .filter({ hasText: "2.mp4" })
    .first();
  await browserExpect(published).toContainText("等待接收");
}, 20000);
it("网页导出与再次下载保持同一请求，复制后产生新请求", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("new-draft-button").click();
  await page.getByTestId("draft-json-toggle").click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByTestId("draft-json-input").fill(
    JSON.stringify({
      name: "网页同步",
      actions: [
        {
          name: "完整同步",
          type: "report_status",
          params: { scope: "full" },
        },
      ],
    }),
  );
  const firstEvent = page.waitForEvent("download");
  await page.getByTestId("export-button").click();
  const first = JSON.parse(
    readFileSync((await (await firstEvent).path())!, "utf8"),
  );
  const nextEvent = page.waitForEvent("download");
  await page.getByTestId("download-request-button").click();
  const next = JSON.parse(
    readFileSync((await (await nextEvent).path())!, "utf8"),
  );
  expect(next.request_id).toBe(first.request_id);
  await page.getByTestId("copy-request-button").click();
  const copyEvent = page.waitForEvent("download");
  await page.getByTestId("export-button").click();
  const copy = JSON.parse(
    readFileSync((await (await copyEvent).path())!, "utf8"),
  );
  expect(copy.request_id).not.toBe(first.request_id);
  expect(application.store.all("requests")).toHaveLength(2);
}, 20000);
it("网页混合导入真实报告与视频，核验后播放并按范围下载", async () => {
  const { page, application, base } = await setup();
  await page.getByTestId("initialize-button").click();
  await page.getByTestId("nav-import").click();
  await page
    .getByTestId("import-files")
    .setInputFiles([fixtures.videoPath, fixtures.reportPath]);
  await browserExpect.poll(() => application.coverage()).toBe(20);
  await page.getByTestId("nav-plans").click();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  await page
    .getByRole("button", { name: /^展开动作 / })
    .first()
    .waitFor();
  while (await page.getByRole("button", { name: /^展开动作 / }).count())
    await page
      .getByRole("button", { name: /^展开动作 / })
      .first()
      .click();
  for (const summary of await page
    .getByText("更多操作与交付记录", { exact: true })
    .all())
    await summary.click();
  const video = page.locator("video").first();
  await browserExpect(video).toBeVisible();
  await browserExpect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState))
    .toBeGreaterThanOrEqual(2);
  await video.evaluate((v: HTMLVideoElement) => v.play());
  await browserExpect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime))
    .toBeGreaterThan(0);
  const current = application.store.all<{ id: string }>("videos")[0];
  const ranged = await fetch(`${base}/api/videos/${current.id}/content`, {
    headers: { Range: "bytes=0-15" },
  });
  expect(ranged.status).toBe(206);
  expect(Buffer.from(await ranged.arrayBuffer())).toEqual(
    fixtures.video.subarray(0, 16),
  );
}, 20000);

it("能力版本变化后后续追加从最新观察准备重试", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const report = mappedReport(Buffer.from("probe"));
  report.plans![0].actions = undefined;
  application.applyReports([reportInput(report)]);
  const draft = application.createDraft();
  await page.reload();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").click();
  await page.getByRole("button", { name: "准备取回计划默认产物" }).click();
  await choose(page.getByLabel("目标草稿"), draft.id);
  let first = true;
  await page.route("**/api/drafts/*/actions", async (route) => {
    if (first) {
      first = false;
      application.reloadCapabilities();
    }
    await route.continue();
  });
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await browserExpect(
    page.getByRole("button", { name: "重试同一目标追加", exact: true }),
  ).toBeVisible();
  expect(JSON.parse(application.draft(draft.id).content.text).actions).toEqual(
    [],
  );
  await page.waitForResponse(
    async (response) =>
      response.url().endsWith("/api/state") &&
      (await response.json()).capabilities.version ===
        application.capabilities.version,
  );
  await page
    .getByRole("button", { name: "重试同一目标追加", exact: true })
    .click();
  await browserExpect(page.getByRole("dialog")).toHaveCount(0);
  expect(
    JSON.parse(application.draft(draft.id).content.text).actions,
  ).toHaveLength(1);
  expect(application.store.all("drafts")).toHaveLength(1);
}, 20000);
import { linked } from "../helpers/preview-renaming";
it("真实PendingInput名称恢复及普通名称重名恢复可以保存后导出", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const parameter = application.capabilities
    .active!.devices[0].actions.find((a) => a.type === "camera_record")!
    .parameter_types.find((p) => p.type === "demo_fixed")!;
  parameter.preview_supported = true;
  const c = linked(),
    root = JSON.parse(c.text);
  for (const action of root.actions.slice(0, 2)) {
    action.device_id = "demo_cam0";
    action.params = { type: "demo_fixed" };
  }
  root.actions[4].params.source.action_name = "B";
  c.text = JSON.stringify(root);
  c.pending = { "/actions/0/name": { kind: "json", text: '"unfinished' } };
  const d = application.createDraft(c),
    id = d.content.automaticPreviews!.actions[2].id;
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await pendingInput(page, "/actions/0/name").fill('"C"');
  await pendingAction(page, "/actions/0/name", "apply").click();
  await browserExpect
    .poll(
      () =>
        JSON.parse(application.draft(d.id).content.text).actions[2].params
          .source.action_name,
    )
    .toBe("C");
  const first = page.locator(".action-card").first();
  await first.getByLabel("动作名称", { exact: true }).fill("B");
  await browserExpect
    .poll(
      () => JSON.parse(application.draft(d.id).content.text).actions[0].name,
    )
    .toBe("B");
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await page
    .locator(".action-card")
    .first()
    .getByLabel("动作名称", { exact: true })
    .fill("D");
  const download = page.waitForEvent("download");
  await page.getByTestId("export-button").click();
  const body = JSON.parse(
    readFileSync((await (await download).path())!, "utf8"),
  );
  expect(
    body.actions.slice(2).map((a: any) => a.params.source.action_name),
  ).toEqual(["D", "B", "B"]);
  expect(application.draft(d.id).content.automaticPreviews!.actions[2].id).toBe(
    id,
  );
  expect(body).not.toHaveProperty("automaticPreviews");
}, 20000);
it.each([
  ["scheduled_at", "2026-10-11 01:00:00", 5],
  ["type", "future_camera", 5],
  ["type", "report_status", 4],
] as const)(
  "真实PendingInput的Pointer恢复 %s=%s",
  async (field, value, count) => {
    const { page, application } = await setup();
    await page.getByTestId("initialize-button").click();
    application.capabilities
      .active!.devices[0].actions.find((a) => a.type === "camera_record")!
      .parameter_types.find((p) => p.type === "demo_fixed")!.preview_supported =
      true;
    const c = linked(),
      root = JSON.parse(c.text);
    for (const a of root.actions.slice(0, 2)) {
      a.device_id = "demo_cam0";
      a.params = { type: "demo_fixed" };
    }
    c.text = JSON.stringify(root);
    const path = `/actions/0/${field}`;
    c.pending = { [path]: { kind: "json", text: '"unfinished' } };
    const d = application.createDraft(c);
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    await pendingInput(page, path).fill(JSON.stringify(value));
    await pendingAction(page, path, "apply").click();
    await browserExpect
      .poll(
        () =>
          JSON.parse(application.draft(d.id).content.text).actions[0][field],
      )
      .toBe(value);
    const actual = JSON.parse(application.draft(d.id).content.text).actions;
    expect(actual).toHaveLength(count);
    if (field === "scheduled_at") expect(actual[2].scheduled_at).toBe(value);
    expect(application.draft(d.id).content.pending).toEqual({});
  },
  20000,
);
it.each(["rename", "remove"])(
  "其他动作消除名称冲突后网页保存并导出 %s",
  async (operation) => {
    const { page, application } = await setup();
    await page.getByTestId("initialize-button").click();
    application.capabilities
      .active!.devices[0].actions.find((a) => a.type === "camera_record")!
      .parameter_types.find((p) => p.type === "demo_fixed")!.preview_supported =
      true;
    const c = linked(),
      root = JSON.parse(c.text);
    for (const a of root.actions.slice(0, 2)) {
      a.device_id = "demo_cam0";
      a.params = { type: "demo_fixed" };
    }
    root.actions[4].params.source = { action_instance_id: "999" };
    c.text = JSON.stringify(root);
    const d = application.createDraft(c),
      autoId = d.content.automaticPreviews!.actions[2].id;
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    await page
      .locator(".action-card")
      .nth(0)
      .getByLabel("动作名称", { exact: true })
      .fill("B");
    await browserExpect
      .poll(
        () => JSON.parse(application.draft(d.id).content.text).actions[0].name,
      )
      .toBe("B");
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    const other = page.locator(".action-card").nth(1);
    if (operation === "rename")
      await other.getByLabel("动作名称", { exact: true }).fill("C");
    else
      await other
        .getByRole("button", { name: "删除动作", exact: true })
        .click();
    const downloaded = page.waitForEvent("download");
    await page.getByTestId("export-button").click();
    const body = JSON.parse(
        readFileSync((await (await downloaded).path())!, "utf8"),
      ),
      saved = application.draft(d.id);
    const index = saved.content.automaticPreviews!.actions.findIndex(
      (a) => a.id === autoId,
    );
    expect(body.actions[index].params.source.action_name).toBe("B");
    expect(
      saved.content.automaticPreviews!.actions[index].rename!.pending,
    ).toBe(false);
    expect(body.actions).toHaveLength(operation === "rename" ? 5 : 3);
  },
  20000,
);
