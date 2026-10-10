import { beforeAll, afterAll, afterEach, expect, it } from "vitest";
import {
  chromium,
  expect as browserExpect,
  type Browser,
  type Page,
} from "@playwright/test";
import {
  mkdtempSync,
  rmSync,
  copyFileSync,
  writeFileSync,
  mkdirSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Server } from "node:http";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { Draft } from "../../src/server/models";
import { mappedReport, reportInput } from "./fixtures";
import {
  initializePreviewMetadata,
  previewIntent,
} from "../../src/shared/automatic-previews";
import { Readable } from "node:stream";
import { makeMediaFixture } from "../helpers/media";
import { createHash } from "node:crypto";

let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
}, 20000);
afterAll(async () => {
  await browser?.close();
});
afterEach(async () => {
  for (const c of clean.splice(0)) await c();
});
it("复制动作遇到不可解释的其他动作时保留原文并显示失败", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const d = application.createDraft({
    text: '{"name":"未完成列表","actions":[{"name":"普通","type":"report_status","params":{"scope":"full"}},null]}',
  });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await page.getByRole("button", { name: "复制动作", exact: true }).click();
  await browserExpect(page.getByRole("alert")).toContainText("动作列表");
  expect(application.draft(d.id).content).toEqual(d.content);
}, 20000);
it("旧资料的非法自动用途仅查看不改变，技术详情保留完整输入且导出拒绝", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const params = {
    source: { action_instance_id: "7" },
    output_ids: ["4"],
    filter: "preview",
    purpose: "auto_preview",
  };
  const d = application.createDraft({
    text: JSON.stringify({
      name: "待核实",
      actions: [
        {
          name: "取回",
          type: "obtain_action_outputs",
          scheduled_at: "2026-10-10 01:00:00",
          params,
        },
      ],
    }),
  });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await browserExpect(page.getByTestId("preview-intent")).toContainText(
    "尚未设置",
  );
  const automatic = page.getByTestId("derived-preview");
  await automatic.locator("summary").click();
  expect(JSON.parse(await automatic.locator("pre").innerText()).params).toEqual(
    params,
  );
  await page.getByTestId("draft-json-toggle").click();
  expect(await page.getByTestId("draft-json-input").inputValue()).toBe(
    d.content.text,
  );
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("export-button").click();
  await browserExpect(page.getByRole("alert")).toBeVisible();
  expect(application.store.all("requests")).toEqual([]);
  expect(application.draft(d.id).content).toEqual(d.content);
}, 20000);
it("原片预览与已收到修复成品独立显示，取回仍使用真实成品ID", async () => {
  const { page, application, files } = await setup();
  await page.getByTestId("initialize-button").click();
  const fixture = await makeMediaFixture(
    join(application.store.directory, "fixture"),
  );
  const bytes = fixture.video;
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1cAAAAASUVORK5CYII=",
    "base64",
  );
  const r = mappedReport(bytes),
    p = r.plans![0],
    c = p.actions![0],
    a = p.actions![1],
    original = c.outputs![0];
  c.outputs = [
    original,
    {
      ...original,
      output_id: "10",
      kind: "preview",
      preview_of_output_id: original.output_id,
      original_name: "原片预览.png",
      media_type: "image/png",
      size: png.length,
      checksum: {
        status: "available",
        sha256: createHash("sha256").update(png).digest("hex"),
      },
    },
    {
      ...original,
      output_id: "11",
      kind: "repaired",
      derived_from_output_id: original.output_id,
      original_name: "修复成品.webm",
      media_type: "video/webm",
    },
  ];
  a.automation = {
    purpose: "auto_preview",
    source_action_instance_id: c.action_instance_id,
  };
  a.input_params = {
    source: { action_name: c.name },
    purpose: "auto_preview",
    filter: "preview",
  };
  a.scheduled_at = c.scheduled_at;
  a.deliveries = a.deliveries!.map((d) => ({
    ...d,
    output_id: "11",
    file_name: "1.webm",
    display_name: "修复成品.webm",
  }));
  const manual = {
    ...a,
    action_instance_id: "3",
    name: "手动预览",
    automation: undefined,
    input_params: { source: { action_name: c.name }, output_ids: ["10"] },
    deliveries: [
      {
        ...a.deliveries[0],
        delivery_id: "3",
        output_id: "10",
        file_name: "3.png",
        display_name: "原片预览.png",
        size: png.length,
        sha256: createHash("sha256").update(png).digest("hex"),
      },
    ],
  };
  p.actions = [c, a, manual];
  application.applyReports([reportInput(r)]);
  expect(application.coverage()).toBe(r.to_wm);
  for (const [fileName, data] of [
    ["1.webm", bytes],
    ["3.png", png],
  ] as const) {
    const f = files.createBatch([
      { fileName, size: data.length, kind: "media" },
    ]).files[0];
    await files.upload(f.id, Readable.from([data]));
  }
  await files.idle();
  await page.reload();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").click();
  const auto = page.locator('[data-action-id="2"]');
  await browserExpect(auto).toContainText("修复成品已收到 1");
  const capture = page.locator('[data-action-id="1"]');
  await capture
    .getByRole("button", { name: `展开动作 ${c.name}`, exact: true })
    .click();
  const preview = capture.locator(".product-card").filter({
    has: page.getByRole("checkbox", {
      name: "选择 原片预览.png",
      exact: true,
    }),
  });
  await browserExpect(preview).toContainText(original.original_name!);
  await browserExpect(preview).toContainText("不代表修复结果");
  const originalCard = capture.locator(".product-card").filter({
    has: page.getByRole("checkbox", {
      name: `选择 ${original.original_name}`,
      exact: true,
    }),
  });
  await browserExpect(originalCard.locator(".product-state")).toContainText(
    "尚无本地副本",
  );
  const repaired = capture.locator(".product-card").filter({
    has: page.getByRole("checkbox", {
      name: "选择 修复成品.webm",
      exact: true,
    }),
  });
  await repaired.locator("summary").filter({ hasText: "更多操作" }).click();
  await repaired.getByRole("button", { name: "准备取回", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "加入草稿", exact: true })
    .click();
  await browserExpect
    .poll(() => application.store.all<Draft>("drafts").length)
    .toBe(1);
  await browserExpect(page.getByRole("dialog")).toHaveCount(0);
  expect(
    JSON.parse(application.store.all<Draft>("drafts")[0].content.text)
      .actions[0].params.output_ids,
  ).toEqual(["11"]);
}, 20000);
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
it.each([1280, 390])(
  "三态开关、完整替换确认与草稿复制保留可靠意图，宽度 %i",
  async (width) => {
    const { page, application } = await setup();
    await page.setViewportSize({ width, height: width === 390 ? 844 : 720 });
    const inspect = async (state: string) => {
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      if (process.env.CAMCTL_VISUAL_OUTPUT) {
        mkdirSync(process.env.CAMCTL_VISUAL_OUTPUT, { recursive: true });
        await page.screenshot({
          path: join(
            process.env.CAMCTL_VISUAL_OUTPUT,
            `task-5-editor-${width}-${state}.png`,
          ),
          fullPage: true,
        });
      }
    };
    await page.getByTestId("initialize-button").click();
    await page.getByTestId("new-draft-button").click();
    await browserExpect(page.getByTestId("preview-intent")).toContainText(
      "已启用",
    );
    await browserExpect(page.getByTestId("preview-status")).toContainText(
      "支持预览",
    );
    await inspect("enabled");
    await page.getByRole("button", { name: "已启用预览", exact: true }).click();
    await browserExpect(page.getByTestId("save-status")).toContainText(
      "已保存",
    );
    await page.getByRole("button", { name: "复制草稿", exact: true }).click();
    await browserExpect
      .poll(() => application.store.all<Draft>("drafts").length)
      .toBe(2);
    await browserExpect(page.getByTestId("preview-intent")).toContainText(
      "已禁用",
    );
    await inspect("disabled");
    await page.getByTestId("draft-json-toggle").click();
    const before = await page.getByTestId("draft-json-input").inputValue();
    page.once("dialog", (d) => d.dismiss());
    await page.getByTestId("draft-json-input").fill('{"name":"未完成",');
    await browserExpect(page.getByTestId("draft-json-input")).toHaveValue(
      before,
    );
    await browserExpect(page.getByTestId("preview-intent")).toContainText(
      "已禁用",
    );
    page.once("dialog", (d) => d.accept());
    await page.getByTestId("draft-json-input").fill('{"name":"未完成",');
    await browserExpect(page.getByTestId("preview-intent")).toContainText(
      "尚未设置",
    );
    await inspect("unset");
    await page.getByTestId("preview-intent").click();
    await browserExpect(page.getByRole("menu")).toBeVisible();
    await inspect("unset-menu");
    await page.keyboard.press("Escape");
    await browserExpect(page.getByTestId("save-status")).toContainText(
      "已保存",
    );
    await page.reload();
    await page.getByTestId("draft-open-button").last().click();
    await browserExpect(page.getByTestId("preview-intent")).toContainText(
      "尚未设置",
    );
  },
  20000,
);
it("重载先等旧轮询结束，同generation的失败和随后恢复不被旧观察覆盖", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const d = previewDraft(application);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  let captured = false,
    release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  let first = true,
    reloads = 0;
  await page.route("**/api/state", async (route) => {
    if (first) {
      first = false;
      const response = await route.fetch();
      captured = true;
      await gate;
      await route.fulfill({ response });
    } else await route.continue();
  });
  await browserExpect.poll(() => captured).toBe(true);
  const prior = application.capabilities.generation;
  writeFileSync(
    join(application.store.directory, "device-capabilities.json"),
    "{",
  );
  await page.route("**/api/capabilities/reload", async (route) => {
    reloads++;
    await route.continue();
  });
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await browserExpect(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeDisabled();
  expect(reloads).toBe(0);
  release();
  await browserExpect.poll(() => reloads).toBe(1);
  await browserExpect(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  expect(application.capabilities.generation).toBe(prior);
  expect(application.capabilities.error).toBeTruthy();
  await browserExpect(page.locator(".device-page")).toContainText("继续使用");
  const k = application.capabilities.active!;
  k.devices[0].actions
    .find((a) => a.type === "camera_record")!
    .parameter_types.find((p) => p.type === "demo_fixed")!.preview_supported =
    false;
  writeFileSync(
    join(application.store.directory, "device-capabilities.json"),
    JSON.stringify(k),
  );
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await browserExpect
    .poll(() => application.capabilities.generation)
    .toBe(prior + 1);
  await browserExpect(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await page.getByTestId("nav-plans").click();
  await browserExpect(page.getByTestId("preview-status")).toContainText(
    "支持预览",
  );
  expect(JSON.parse(application.draft(d.id).content.text).actions).toHaveLength(
    1,
  );
}, 20000);
it("报告独有自动取回在来源晚到后归并，保留展开与选择且不借手动副本", async () => {
  const { page, application, files } = await setup();
  await page.getByTestId("initialize-button").click();
  const bytes = Buffer.from("a preview fixture");
  const r = mappedReport(bytes),
    p = r.plans![0],
    c = p.actions![0],
    a = p.actions![1];
  a.automation = {
    purpose: "auto_preview",
    source_action_instance_id: c.action_instance_id,
  };
  a.input_params = {
    source: { action_name: c.name },
    filter: "preview",
    purpose: "auto_preview",
  };
  a.scheduled_at = c.scheduled_at;
  const manual = {
    ...a,
    action_instance_id: "3",
    name: "手动取回",
    automation: undefined,
    input_params: { source: { action_name: c.name } },
    deliveries: a.deliveries!.map((d) => ({
      ...d,
      delivery_id: "3",
      file_name: "3.mp4",
    })),
  };
  p.actions = [a, manual];
  application.applyReports([reportInput(r)]);
  expect(application.coverage()).toBe(r.to_wm);
  const f = files.createBatch([
    { fileName: "3.mp4", size: bytes.length, kind: "media" },
  ]).files[0];
  await files.upload(f.id, Readable.from([bytes]));
  await files.idle();
  await page.reload();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").click();
  const auto = page.locator(`[data-action-id="${a.action_instance_id}"]`);
  await browserExpect(auto).toContainText(c.action_instance_id);
  await browserExpect(auto).toContainText("等待来源");
  await auto
    .getByRole("button", { name: `展开动作 ${a.name}`, exact: true })
    .click();
  await auto.getByRole("checkbox", { name: /选择 / }).check();
  await browserExpect(auto).toContainText("已选择 1");
  const newer = {
    ...r,
    report_id: "2",
    from_wm: r.to_wm,
    to_wm: r.to_wm + 1,
    plans: [{ ...p, actions: [c] }],
  };
  application.applyReports([reportInput(newer)]);
  expect(application.coverage()).toBe(newer.to_wm);
  await browserExpect(auto).toHaveClass(/automatic-result/);
  await browserExpect(auto).toContainText("已选择 1");
  await browserExpect(auto).toContainText("本次自动取回已收到并核验 0");
  await browserExpect(auto).toContainText("主机已发布，等待接收 1");
  await browserExpect(page.locator('[data-action-id="3"]')).not.toHaveClass(
    /automatic-result/,
  );
  await page.getByRole("button", { name: "查看全部动作", exact: true }).click();
  await browserExpect(auto).not.toHaveClass(/automatic-result/);
  await browserExpect(auto).toContainText("已选择 1");
  await page.getByRole("button", { name: "折叠计划", exact: true }).click();
  await page.getByRole("button", { name: "展开计划", exact: true }).click();
  await browserExpect(auto).toContainText("已选择 1");
}, 20000);
it("可靠来源的多个受理失败和缺少来源ID的动作全部可见", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const r = mappedReport(Buffer.from("x")),
    p = r.plans![0],
    c = p.actions![0],
    original = p.actions![1];
  const failures = ["2", "3", "4"].map((id) => ({
    ...original,
    action_instance_id: id,
    name: `自动失败${id}`,
    status: "failed" as const,
    automation: {
      purpose: "auto_preview" as const,
      source_action_instance_id: c.action_instance_id,
    },
    deliveries: undefined,
    result: undefined,
    error: {
      code: "duplicate_auto_preview",
      stage: "admission",
      details: {
        source_action_name: c.name,
        conflicting_action_names: ["自动失败2", "自动失败3", "自动失败4"],
      },
    },
  }));
  const unknown = {
    ...failures[0],
    action_instance_id: "5",
    name: "来源未知",
    automation: { purpose: "auto_preview" as const },
    error: {
      code: "action_validation_failed",
      stage: "admission",
      details: { message: "来源未确认" },
    },
  };
  p.actions = [c, ...failures, unknown];
  application.applyReports([reportInput(r)]);
  expect(application.coverage()).toBe(r.to_wm);
  await page.reload();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").click();
  await browserExpect(page.locator(".automatic-result")).toHaveCount(3);
  for (const a of failures)
    await browserExpect(
      page.locator(`[data-action-id="${a.action_instance_id}"]`),
    ).toContainText("未执行");
  await browserExpect(page.locator('[data-action-id="5"]')).toContainText(
    "关联尚未确认",
  );
  await browserExpect(page.locator('[data-action-id="5"]')).not.toHaveClass(
    /automatic-result/,
  );
}, 20000);
function previewDraft(application: Application, name = "拍摄计划") {
  const k = application.capabilities.active!;
  k.devices[0].actions
    .find((a) => a.type === "camera_record")!
    .parameter_types.find((p) => p.type === "demo_fixed")!.preview_supported =
    true;
  writeFileSync(
    join(application.store.directory, "device-capabilities.json"),
    JSON.stringify(k),
  );
  return application.createDraft(
    initializePreviewMetadata(
      {
        text: JSON.stringify({
          name,
          actions: [
            {
              name: "拍摄",
              type: "camera_record",
              device_id: "demo_cam0",
              scheduled_at: "2026-10-10 01:00:00",
              params: { type: "demo_fixed" },
              policy: { max_delay_ms: 0 },
            },
          ],
        }),
      },
      "enabled",
      "fixture",
    ),
  );
}

const previewButton = (page: Page) => page.getByTestId("preview-intent");
const manualPreview = {
  name: "手动预览",
  type: "obtain_action_outputs",
  scheduled_at: "2026-10-10 02:00:00",
  params: { source: { action_name: "拍摄" }, filter: "preview" },
};
function externalPreviewDraft(application: Application) {
  const supported = previewDraft(application);
  const body = JSON.parse(supported.content.text);
  body.actions.push(manualPreview);
  application.deleteDraft(supported.id, supported.revision);
  return application.createDraft({ text: JSON.stringify(body) });
}

it("紧凑入口已知双向单击，手动取回完整保留且保存重开", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const external = externalPreviewDraft(application);
  const d = application.createDraft(
    initializePreviewMetadata(external.content, "enabled", "known"),
  );
  application.deleteDraft(external.id, external.revision);
  const manualIdentity = d.content.automaticPreviews!.actions.at(-1)!.id;
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  const button = previewButton(page);
  await browserExpect(button).toHaveAccessibleName("已启用预览");
  expect(await button.getAttribute("aria-pressed")).toBeNull();
  await browserExpect(
    page.locator('[aria-label="自动获取预览文件"].notice'),
  ).toHaveCount(0);
  await button.press("Enter");
  await browserExpect(button).toHaveAccessibleName("已禁用预览");
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  let saved = application.draft(d.id).content;
  expect(previewIntent(saved)).toBe("disabled");
  expect(JSON.parse(saved.text).actions).toHaveLength(2);
  expect(JSON.parse(saved.text).actions.at(-1)).toEqual(manualPreview);
  expect(saved.automaticPreviews!.actions.at(-1)!.id).toBe(manualIdentity);
  await button.press("Space");
  await browserExpect(button).toHaveAccessibleName("已启用预览");
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  saved = application.draft(d.id).content;
  expect(previewIntent(saved)).toBe("enabled");
  expect(JSON.parse(saved.text).actions).toHaveLength(3);
  const manualIndex = JSON.parse(saved.text).actions.findIndex(
    (action: typeof manualPreview) => action.name === manualPreview.name,
  );
  expect(JSON.parse(saved.text).actions[manualIndex]).toEqual(manualPreview);
  expect(saved.automaticPreviews!.actions[manualIndex].id).toBe(manualIdentity);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await browserExpect(previewButton(page)).toHaveAccessibleName("已启用预览");
  expect(application.draft(d.id).content).toEqual(saved);
});

it.each(["missing", "unset"])(
  "尚未设置的%s资料打开、导航、悬停和取消零修改零保存",
  async (metadata) => {
    const { page, application } = await setup();
    await page.getByTestId("initialize-button").click();
    let d = externalPreviewDraft(application);
    if (metadata === "unset") {
      const old = d;
      d = application.createDraft(
        initializePreviewMetadata(d.content, "unset", "unset"),
      );
      application.deleteDraft(old.id, old.revision);
    }
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    let writes = 0;
    await page.route(`**/api/drafts/${d.id}`, async (route) => {
      if (route.request().method() === "PUT") writes++;
      await route.continue();
    });
    const before = application.draft(d.id);
    const button = previewButton(page);
    await browserExpect(button).toContainText("尚未设置");
    expect(await button.getAttribute("aria-pressed")).toBeNull();
    await button.focus();
    await button.press("Enter");
    await browserExpect(page.getByRole("menu")).toBeVisible();
    await browserExpect(page.getByRole("menuitem")).toHaveCount(2);
    expect(await page.getByRole("menuitemradio").count()).toBe(0);
    expect(await page.getByRole("menuitemcheckbox").count()).toBe(0);
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowUp");
    await page.getByRole("menuitem", { name: "禁用预览", exact: true }).hover();
    await page.keyboard.press("Escape");
    await browserExpect(button).toBeFocused();
    await browserExpect(page.getByRole("menu")).toHaveCount(0);
    await button.click();
    await browserExpect(page.getByRole("menu")).toBeVisible();
    await page.mouse.click(5, 5);
    await browserExpect(page.getByRole("menu")).toHaveCount(0);
    // 两次正常轮询跨过自动保存延迟，证明没有安排写入。
    for (let n = 0; n < 2; n++)
      await page.waitForResponse((response) =>
        response.url().endsWith("/api/state"),
      );
    expect(application.draft(d.id)).toEqual(before);
    expect(writes).toBe(0);
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    await browserExpect(previewButton(page)).toContainText("尚未设置");
    expect(application.draft(d.id)).toEqual(before);
  },
);

it.each(["enabled", "disabled"])(
  "尚未设置显式选择%s一次写入，焦点返回且保留手动取回",
  async (selected) => {
    const { page, application } = await setup();
    await page.getByTestId("initialize-button").click();
    const d = externalPreviewDraft(application);
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    let writes = 0;
    await page.route(`**/api/drafts/${d.id}`, async (route) => {
      if (route.request().method() === "PUT") writes++;
      await route.continue();
    });
    await previewButton(page).press("Space");
    const item = page.getByRole("menuitem", {
      name: selected === "enabled" ? "启用预览" : "禁用预览",
      exact: true,
    });
    await item.focus();
    await item.press("Enter");
    await browserExpect(previewButton(page)).toHaveAccessibleName(
      selected === "enabled" ? "已启用预览" : "已禁用预览",
    );
    await browserExpect(previewButton(page)).toBeFocused();
    await browserExpect(page.getByRole("menu")).toHaveCount(0);
    await browserExpect(page.getByTestId("save-status")).toContainText(
      "已保存",
    );
    const saved = application.draft(d.id);
    expect(previewIntent(saved.content)).toBe(selected);
    expect(writes).toBe(1);
    expect(saved.revision).toBe(d.revision + 1);
    const actions = JSON.parse(saved.content.text).actions;
    expect(
      actions.find((a: typeof manualPreview) => a.name === manualPreview.name),
    ).toEqual(manualPreview);
    expect(actions).toHaveLength(selected === "enabled" ? 3 : 2);
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    await browserExpect(previewButton(page)).toHaveAccessibleName(
      selected === "enabled" ? "已启用预览" : "已禁用预览",
    );
    expect(application.draft(d.id)).toEqual(saved);
  },
);

it("普通保存期间入口保持可编辑，第二个明确意图完整保存", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const d = previewDraft(application);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let writes = 0;
  await page.route(`**/api/drafts/${d.id}`, async (route) => {
    if (route.request().method() === "PUT" && ++writes === 1) await gate;
    await route.continue();
  });
  try {
    await previewButton(page).click();
    await browserExpect.poll(() => writes).toBe(1);
    await browserExpect(page.getByTestId("save-status")).toContainText(
      "保存中",
    );
    await browserExpect(previewButton(page)).toBeEnabled();
    await previewButton(page).click();
    await browserExpect(previewButton(page)).toHaveAccessibleName("已启用预览");
  } finally {
    release();
  }
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  expect(writes).toBe(2);
  expect(previewIntent(application.draft(d.id).content)).toBe("enabled");
  expect(JSON.parse(application.draft(d.id).content.text).actions).toHaveLength(
    2,
  );
});

it("能力故障和不可解释正文的实际关联诊断在入口附近保留", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const d = previewDraft(application);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  writeFileSync(
    join(application.store.directory, "device-capabilities.json"),
    "{",
  );
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await browserExpect.poll(() => application.capabilities.error).toBeTruthy();
  await page.getByTestId("nav-plans").click();
  await browserExpect(previewButton(page)).toHaveAccessibleName("已启用预览");
  await browserExpect(page.getByTestId("preview-diagnostics")).toContainText(
    "能力尚不能确认",
  );
  expect(application.draft(d.id).content).toEqual(d.content);
  const invalid = application.createDraft({
    ...initializePreviewMetadata(
      { text: '{"name":"待修正","actions":[]}' },
      "disabled",
      "invalid",
    ),
    text: "{",
  });
  await page.reload();
  await page.getByTestId("draft-open-button").last().click();
  await browserExpect(previewButton(page)).toHaveAccessibleName("已禁用预览");
  await browserExpect(page.getByTestId("preview-diagnostics")).toContainText(
    "正文尚不能可靠解释",
  );
  expect(application.draft(invalid.id).content).toEqual(invalid.content);
});
it("复制拍摄分配独立来源及自动取回身份，改名删项保持对应", async () => {
  const { page, application } = await setup();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByTestId("initialize-button").click();
  const d = previewDraft(application);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await browserExpect(
    page.locator('[data-testid="derived-preview"]'),
  ).toHaveCount(1);
  await page.getByRole("button", { name: "复制动作", exact: true }).click();
  await browserExpect(
    page.locator('[data-testid="derived-preview"]'),
  ).toHaveCount(2);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  if (process.env.CAMCTL_VISUAL_OUTPUT) {
    mkdirSync(process.env.CAMCTL_VISUAL_OUTPUT, { recursive: true });
    await page.screenshot({
      path: join(
        process.env.CAMCTL_VISUAL_OUTPUT,
        "task-5-editor-390-copy-action.png",
      ),
      fullPage: true,
    });
  }
  await page.getByLabel("动作名称", { exact: true }).last().fill("独立拍摄");
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  const c = application.draft(d.id).content;
  expect(new Set(c.automaticPreviews!.actions.map((a) => a.id)).size).toBe(4);
  expect(
    JSON.parse(c.text)
      .actions.filter((a: any) => a.params.purpose === "auto_preview")
      .map((a: any) => a.params.source.action_name),
  ).toEqual(["拍摄", "独立拍摄"]);
  await page
    .getByRole("button", { name: "删除动作", exact: true })
    .last()
    .click();
  await browserExpect(
    page.locator('[data-testid="derived-preview"]'),
  ).toHaveCount(1);
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  expect(
    application.draft(d.id).content.automaticPreviews!.actions.map((a) => a.id),
  ).toEqual(d.content.automaticPreviews!.actions.map((a) => a.id));
}, 20000);
it("能力重载等待所有开放会话的在途保存，旧轮询不能回退失败观察", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const a = previewDraft(application, "一"),
    b = previewDraft(application, "二");
  await page.reload();
  await page.getByTestId("draft-open-button").first().click();
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  let put = false,
    reloads = 0;
  await page.route(`**/api/drafts/${a.id}`, async (route) => {
    if (route.request().method() === "PUT") {
      put = true;
      await gate;
    }
    await route.continue();
  });
  await page.getByLabel("计划名称", { exact: true }).fill("未保存一");
  await browserExpect.poll(() => put).toBe(true);
  await page.getByTestId("draft-open-button").last().click();
  await page.getByLabel("计划名称", { exact: true }).fill("未保存二");
  await page.route("**/api/capabilities/reload", async (route) => {
    reloads++;
    await route.continue();
  });
  writeFileSync(
    join(application.store.directory, "device-capabilities.json"),
    "{",
  );
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await browserExpect(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeDisabled();
  expect(reloads).toBe(0);
  release();
  await browserExpect.poll(() => reloads).toBe(1);
  await browserExpect(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  expect(JSON.parse(application.draft(a.id).content.text).name).toBe(
    "未保存一",
  );
  expect(JSON.parse(application.draft(b.id).content.text).name).toBe(
    "未保存二",
  );
  await browserExpect(page.locator(".device-page")).toContainText("继续使用");
  expect(application.capabilities.error).toBeTruthy();
}, 20000);
it("保存响应丢失且无法读取时不发起能力重载，恢复连接后核实并重载", async () => {
  const { page, application } = await setup();
  await page.getByTestId("initialize-button").click();
  const d = previewDraft(application);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  let failed = false,
    reloads = 0;
  await page.route("**/api/state", async (route) => {
    if (failed) await route.abort();
    else await route.continue();
  });
  await page.route(`**/api/drafts/${d.id}`, async (route) => {
    if (route.request().method() === "PUT") {
      await route.fetch();
      failed = true;
    }
    await route.abort();
  });
  await page.route("**/api/capabilities/reload", async (route) => {
    reloads++;
    await route.continue();
  });
  await page.getByLabel("计划名称", { exact: true }).fill("响应未知");
  await browserExpect.poll(() => failed).toBe(true);
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await browserExpect(page.getByRole("alert")).toContainText(
    "尚未开始能力重载",
  );
  expect(reloads).toBe(0);
  await page.unroute(`**/api/drafts/${d.id}`);
  await page.unroute("**/api/state");
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await browserExpect.poll(() => reloads).toBe(1);
  expect(JSON.parse(application.draft(d.id).content.text).name).toBe(
    "响应未知",
  );
}, 20000);
