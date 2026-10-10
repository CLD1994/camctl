import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import {
  chromium,
  expect as check,
  type Browser,
  type Page,
} from "@playwright/test";
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import type { DraftContent, ExportedRequest } from "../../src/server/models";
import { parseClientJson, stringifyJson } from "../../src/shared/json";
import { choose, readOptions } from "./select-support";
import { initializePreviewMetadata } from "../../src/shared/automatic-previews";

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
const capabilityText = readFileSync(
  "../../protocol/examples/video-size-estimate/capabilities.json",
  "utf8",
);
const sample = parseClientJson(capabilityText) as any;
const device = sample.devices[0].device_id;
const camera = (seconds = 60, name = "录像") => ({
  name,
  type: "camera_record",
  device_id: device,
  params: { type: "example_record", bitrate_mode: "high", duration_s: seconds },
  policy: { max_delay_ms: 0 },
  scheduled_at: "2026-10-11 00:00:00",
});
const draftContent = (actions: unknown[]): DraftContent => ({
  text: stringifyJson({ name: "估算", actions }),
});
function gate() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}
async function setup(
  content = draftContent([camera()]),
  text = capabilityText,
  controlledClock = false,
) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-video-estimate-"));
  writeFileSync(join(directory, "device-capabilities.json"), text);
  const app = new Application(directory),
    files = new Files(app),
    requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const context = await browser.newContext(),
    page = await context.newPage();
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  if (controlledClock) {
    await page.clock.install({ time: new Date("2026-10-10T00:00:00Z") });
    // 应用加载前暂停，保存与轮询的计时都从同一个冻结点开始。
    await page.clock.pauseAt(new Date("2026-10-10T00:01:00Z"));
  }
  page.setDefaultTimeout(5000);
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
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
  return { app, page, draft, directory, pageErrors };
}
it("真实普通录像用码率单选与时长输入，独立 pending 不遮蔽码率修正", async () => {
  const { page, app, draft } = await setup();
  const bitrate = page.getByRole("combobox", {
    name: "码率档位 (bitrate_mode)",
    exact: true,
  });
  expect((await readOptions(bitrate)).map((option) => option.text)).toEqual([
    "请选择",
    "standard",
    "high",
  ]);
  await check(bitrate).toContainText("high");
  const duration = page.getByLabel("录像时长 (duration_s)", { exact: true });
  expect(await duration.evaluate((element) => element.tagName)).toBe("INPUT");
  await duration.fill("60e额");
  await check(bitrate).toBeEnabled();
  await choose(bitrate, "0");
  await check
    .poll(
      () =>
        app.draft(draft.id).content.pending?.["/actions/0/params/duration_s"]
          ?.text,
    )
    .toBe("60e额");
  await check
    .poll(() => parseClientJson(app.draft(draft.id).content.text) as any)
    .toMatchObject({
      actions: [{ params: { bitrate_mode: "standard", duration_s: 60 } }],
    });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(duration).toHaveValue("60e额");
  await check(bitrate).toContainText("standard");
  await duration.fill("60");
  await check(page.getByTestId("video-size-value")).toContainText("712.5 MB");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await page.getByTestId("nav-devices").click();
  const task = page.locator(".guide-task").filter({
    has: page.getByRole("heading", { name: "演示：普通录像", exact: true }),
  });
  await check(task.getByRole("table")).toHaveCount(0);
  await check(task).toContainText("standard");
  await check(task).toContainText("high");
}, 20000);
it("参数类型空选项保持未填写，查看不补首项且切回恢复完整内容", async () => {
  const { params: _params, ...action } = camera();
  const { page, app, draft } = await setup(
    initializePreviewMetadata(
      draftContent([action]),
      "disabled",
      "parameter-unselected",
    ),
  );
  const type = page.getByLabel("参数类型", { exact: true });
  const before = app.draft(draft.id);
  await check(type).toHaveAttribute("data-value", "");
  expect((await readOptions(type)).map((option) => option.value)).toEqual([
    "",
    "example_record",
    "example_record_from",
  ]);
  await page.getByRole("button", { name: "收起动作", exact: true }).click();
  await page.getByRole("button", { name: "展开动作", exact: true }).click();
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  await page.waitForResponse((response) =>
    response.url().endsWith("/api/state"),
  );
  expect(app.draft(draft.id)).toEqual(before);
  await choose(type, "example_record");
  const bitrate = page.getByLabel("码率档位 (bitrate_mode)", { exact: true });
  await check(bitrate).toHaveAttribute("data-value", "");
  await check(
    page.getByLabel("录像时长 (duration_s)", { exact: true }),
  ).toHaveValue("");
  await choose(bitrate, "0");
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("60");
  await choose(type, "");
  await check(type).toHaveAttribute("data-value", "");
  await check
    .poll(
      () =>
        (parseClientJson(app.draft(draft.id).content.text) as any).actions[0],
    )
    .toEqual(action);
  await choose(type, "example_record");
  await check(bitrate).toContainText("standard");
  await check(
    page.getByLabel("录像时长 (duration_s)", { exact: true }),
  ).toHaveValue("60");
  await check(page.getByTestId("video-size-value")).toContainText("712.5 MB");
});
it("参数类型各自保存60秒和10秒，首次不继承字段且重开后分别恢复", async () => {
  const initial = initializePreviewMetadata(
    draftContent([
      {
        ...camera(),
        params: {
          type: "example_record",
          bitrate_mode: "standard",
          duration_s: 60,
        },
      },
    ]),
    "disabled",
    "parameter-estimate",
  );
  const { page, app, draft } = await setup(initial);
  const type = page.getByLabel("参数类型", { exact: true });
  const before = app.draft(draft.id);
  let writes = 0;
  page.on("request", (request) => {
    if (
      request.method() === "PUT" &&
      request.url().includes(`/api/drafts/${draft.id}`)
    )
      writes++;
  });
  await choose(type, "example_record");
  await page.waitForResponse((response) =>
    response.url().endsWith("/api/state"),
  );
  expect(writes).toBe(0);
  expect(app.draft(draft.id)).toEqual(before);
  await choose(type, "example_record_from");
  await check(
    page.getByLabel("参考码率 (bitrate_mbps)", { exact: true }),
  ).toHaveValue("");
  await check(
    page.getByLabel("成片时长 (duration_s)", { exact: true }),
  ).toHaveValue("");
  await check
    .poll(
      () =>
        (parseClientJson(app.draft(draft.id).content.text) as any).actions[0]
          .params,
    )
    .toEqual({ type: "example_record_from" });
  await page.getByLabel("参考码率 (bitrate_mbps)", { exact: true }).fill("70");
  await page.getByLabel("成片时长 (duration_s)", { exact: true }).fill("10");
  await check(page.getByTestId("video-size-value")).toContainText("87.5 MB");
  await choose(type, "example_record");
  await check(
    page.getByLabel("码率档位 (bitrate_mode)", { exact: true }),
  ).toContainText("standard");
  await check(
    page.getByLabel("录像时长 (duration_s)", { exact: true }),
  ).toHaveValue("60");
  await check(page.getByTestId("video-size-value")).toContainText("712.5 MB");
  await check(page.getByTestId("retained-parameters")).toHaveCount(0);
  await choose(type, "example_record_from");
  await check(
    page.getByLabel("参考码率 (bitrate_mbps)", { exact: true }),
  ).toHaveValue("70");
  await check(
    page.getByLabel("成片时长 (duration_s)", { exact: true }),
  ).toHaveValue("10");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const saved = app.draft(draft.id);
  expect((parseClientJson(saved.content.text) as any).actions[0]).toEqual({
    ...camera(),
    params: { type: "example_record_from", bitrate_mbps: 70, duration_s: 10 },
  });
  expect(saved.content.parameterVariants?.["0"]).toEqual([
    {
      paramsText:
        '{"params":{"type":"example_record","bitrate_mode":"standard","duration_s":60}}',
      pending: {},
    },
  ]);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByTestId("video-size-value")).toContainText("87.5 MB");
  await choose(type, "example_record");
  await check(
    page.getByLabel("录像时长 (duration_s)", { exact: true }),
  ).toHaveValue("60");
  await check(page.getByTestId("video-size-value")).toContainText("712.5 MB");
  await choose(type, "example_record_from");
  await check(
    page.getByLabel("成片时长 (duration_s)", { exact: true }),
  ).toHaveValue("10");
  await check(page.getByTestId("video-size-value")).toContainText("87.5 MB");
}, 20000);
it("参数成员未完成输入随类型保存，切回恢复且只阻止当前类型导出", async () => {
  const { page, app, draft } = await setup(
    initializePreviewMetadata(
      draftContent([camera()]),
      "disabled",
      "parameter-pending",
    ),
  );
  const type = page.getByLabel("参数类型", { exact: true });
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("60e额");
  await check(type).toBeEnabled();
  await choose(type, "example_record_from");
  await page.getByLabel("参考码率 (bitrate_mbps)", { exact: true }).fill("70");
  await page.getByLabel("成片时长 (duration_s)", { exact: true }).fill("10");
  await check(page.getByTestId("video-size-value")).toContainText("87.5 MB");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  expect(app.draft(draft.id).content.pending ?? {}).toEqual({});
  expect(
    app.draft(draft.id).content.parameterVariants?.["0"]?.[0].pending,
  ).toEqual({ "/duration_s": { kind: "number", text: "60e额" } });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await choose(type, "example_record");
  await check(
    page.getByLabel("录像时长 (duration_s)", { exact: true }),
  ).toHaveValue("60e额");
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("export-button")).toBeEnabled();
  expect(app.store.all("requests")).toEqual([]);
  await choose(type, "example_record_from");
  await check(page.getByTestId("video-size-value")).toContainText("87.5 MB");
  await page.getByTestId("export-button").click();
  await check.poll(() => app.store.all("requests").length).toBe(1);
  const request = app.store.all<ExportedRequest>("requests")[0];
  expect(request.body).toMatchObject({
    actions: [
      {
        params: {
          type: "example_record_from",
          bitrate_mbps: 70,
          duration_s: 10,
        },
      },
    ],
  });
  expect(request.body.actions).toHaveLength(1);
  expect(request.body).not.toHaveProperty("parameterVariants");
});
it("整个参数原文尚未完成时类型选择暂停，修正后恢复手动选择", async () => {
  const { page, app, draft } = await setup();
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  const field = page.getByLabel("参数 JSON 文本", { exact: true });
  await field.fill("{");
  await check(page.getByLabel("参数类型", { exact: true })).toBeDisabled();
  await check
    .poll(
      () => app.draft(draft.id).content.pending?.["/actions/0/params"]?.text,
    )
    .toBe("{");
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByLabel("参数类型", { exact: true })).toBeDisabled();
  await check(field).toHaveValue("{");
  await field.fill(
    '{"type":"example_record","bitrate_mode":"standard","duration_s":60}',
  );
  await check(page.getByLabel("参数类型", { exact: true })).toBeEnabled();
  await check(page.getByTestId("video-size-value")).toContainText("712.5 MB");
});
it("仅有参数类型资料的整份JSON替换仍确认，取消和查看均保留资料", async () => {
  const initial: DraftContent = {
    ...draftContent([camera()]),
    parameterVariants: {
      "0": [
        {
          paramsText:
            '{"params":{"type":"example_record_from","bitrate_mbps":70,"duration_s":10}}',
          pending: {},
        },
      ],
    },
  };
  const { page, app, draft } = await setup(initial);
  const before = app.draft(draft.id);
  await page.getByTestId("draft-json-toggle").click();
  expect(app.draft(draft.id)).toEqual(before);
  page.once("dialog", (dialog) => dialog.dismiss());
  await page
    .getByTestId("draft-json-input")
    .fill('{"name":"替换","actions":[]}');
  await check(page.getByTestId("draft-json-input")).toHaveValue(
    before.content.text,
  );
  expect(app.draft(draft.id)).toEqual(before);
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByTestId("draft-json-input")
    .fill('{"name":"替换","actions":[]}');
  await check
    .poll(() => app.draft(draft.id).content)
    .toEqual({ text: '{"name":"替换","actions":[]}', pending: {} });
});
it("导出后仅参数资料的额外输入可完整另存并切回查看", async () => {
  const initial: DraftContent = {
    ...draftContent([camera()]),
    parameterVariants: {
      "0": [
        {
          paramsText:
            '{"params":{"type":"example_record_from","bitrate_mbps":70,"duration_s":10}}',
          pending: {},
        },
      ],
    },
  };
  const { page, app, draft } = await setup(initial);
  await page.route(`**/api/drafts/${draft.id}`, async (route) => {
    if (route.request().method() === "PUT") {
      const saved = app.draft(draft.id);
      app.exportDraft(
        draft.id,
        saved.revision,
        saved.content,
        app.capabilities.version,
      );
      await route.abort();
    } else await route.continue();
  });
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("61");
  const recovery = page.locator("section.panel").filter({
    has: page.getByRole("heading", {
      name: "保留的额外编辑内容",
      exact: true,
    }),
  });
  await check(recovery).toBeVisible();
  await check(recovery).toContainText(/参数类型.*切换查看/);
  await recovery
    .getByRole("button", { name: "将保留内容保存为新草稿", exact: true })
    .click();
  const newDraft = app.store
    .all<any>("drafts")
    .find((item) => !item.exportedRequestId)!;
  expect(
    (parseClientJson(newDraft.content.text) as any).actions[0].params
      .duration_s,
  ).toBe(61);
  expect(newDraft.content.parameterVariants).toEqual(initial.parameterVariants);
  await choose(
    page.getByLabel("参数类型", { exact: true }),
    "example_record_from",
  );
  await check(page.getByTestId("video-size-value")).toContainText("87.5 MB");
});
it.each(["/actions/00/params/ghost", "/actions/0/params/ghost~2"])(
  "未知原文路径 %s 与两层类型资料读取不崩溃且保留已关闭的派生预览",
  async (pendingPath) => {
    const initial = initializePreviewMetadata(
      {
        ...draftContent([
          camera(),
          {
            name: "录像预览",
            type: "obtain_action_outputs",
            scheduled_at: "2026-10-11 00:00:00",
            params: {
              source: { action_name: "录像" },
              filter: "preview",
              purpose: "auto_preview",
            },
          },
        ]),
        pending: { [pendingPath]: { kind: "json" as const, text: "{" } },
        parameterVariants: {
          "0": [
            {
              paramsText:
                '{"params":{"type":"example_record_from","bitrate_mbps":70,"duration_s":10}}',
              pending: {},
            },
          ],
        },
        actionVariants: {
          "0": [
            {
              type: "camera_timelapse",
              fields: {
                device_id: device,
                params: {
                  type: "example_timelapse",
                  capture_duration_s: 60,
                  interval_s: 1,
                },
              },
              pending: {},
              parameterVariants: [
                {
                  paramsText:
                    '{"params":{"type":"example_timelapse_frames","frames":1.0000000000000001}}',
                  pending: {
                    "/frames": { kind: "number" as const, text: "1e" },
                  },
                },
              ],
            },
          ],
        },
      },
      "disabled",
      "unknown-input",
    );
    const { page, app, draft, pageErrors } = await setup(initial);
    const before = app.draft(draft.id);
    await check(page.getByTestId("validation-issues")).toBeVisible();
    await check(page.getByLabel("参数类型", { exact: true })).toBeDisabled();
    await check(
      page.locator(
        `[data-pending-path=${JSON.stringify(pendingPath)}] textarea`,
      ),
    ).toHaveValue("{");
    await page.waitForResponse((response) =>
      response.url().endsWith("/api/state"),
    );
    expect(pageErrors).toEqual([]);
    expect(app.draft(draft.id)).toEqual(before);
    expect(before.content.pending).toEqual(initial.pending);
    expect(before.content.parameterVariants).toEqual(initial.parameterVariants);
    expect(before.content.actionVariants).toEqual(initial.actionVariants);
    expect((parseClientJson(before.content.text) as any).actions).toHaveLength(
      2,
    );
    expect(before.content.automaticPreviews?.intent).toBe("disabled");
    expect(before.content.automaticPreviews?.actions[1].sourceId).toBe(
      before.content.automaticPreviews?.actions[0].id,
    );
  },
);
it("表单和JSON共用当前完整参数，显示与折叠不会保存", async () => {
  const { page, app, draft } = await setup(
    initializePreviewMetadata(
      draftContent([camera()]),
      "disabled",
      "estimate-view-only",
    ),
  );
  let writes = 0;
  page.on("request", (request) => {
    if (
      request.method() === "PUT" &&
      request.url().includes(`/api/drafts/${draft.id}`)
    )
      writes++;
  });
  const before = app.draft(draft.id);
  await check(page.getByTestId("video-size-value")).toContainText("975 MB");
  await check(page.getByTestId("video-size-estimate")).toContainText(/60.*秒/);
  await check(page.getByTestId("video-size-estimate")).toContainText(
    "130 Mbps",
  );
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await check(page.getByTestId("video-size-value")).toContainText("975 MB");
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  await page.getByRole("button", { name: "收起动作", exact: true }).click();
  await page.getByRole("button", { name: "展开动作", exact: true }).click();
  // 等待真实轮询应答，覆盖一次显示协调周期，而非随机等待。
  await page.waitForResponse((response) =>
    response.url().endsWith("/api/state"),
  );
  expect(writes).toBe(0);
  expect(app.draft(draft.id)).toEqual(before);
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("30");
  await check(page.getByTestId("video-size-value")).toContainText("487.5 MB");
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  const field = page.getByLabel("参数 JSON 文本", { exact: true });
  await field.fill("{");
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await check(page.getByTestId("video-size-estimate")).toContainText(/未完成/);
  await field.fill(
    '{"type":"example_record","bitrate_mode":"high","duration_s":60,"extra":true}',
  );
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await field.fill(
    '{"type":"example_record","bitrate_mode":"high","duration_s":120}',
  );
  await check(page.getByTestId("video-size-value")).toContainText("1.95 GB");
});
it("预设、参数类型和动作类型切换使用实际当前输入", async () => {
  const { page } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  await page.getByText("拍摄参数预设", { exact: true }).click();
  await page.getByLabel("预设名称", { exact: true }).fill("一分钟");
  await page.getByRole("button", { name: "保存为新预设", exact: true }).click();
  await check(page.locator(".preset-box")).toContainText(/预设已保存/);
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("30");
  await check(page.getByTestId("video-size-value")).toContainText("487.5");
  await page.getByRole("button", { name: "应用预设", exact: true }).click();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  await choose(
    page.getByLabel("参数类型", { exact: true }),
    "example_record_from",
  );
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page
    .getByLabel("参数 JSON 文本", { exact: true })
    .fill('{"type":"example_record_from","bitrate_mbps":80,"duration_s":60}');
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
  await choose(page.getByLabel("动作类型", { exact: true }), "report_status");
  await check(page.getByTestId("video-size-estimate")).toHaveCount(0);
  await choose(page.getByLabel("动作类型", { exact: true }), "camera_record");
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
});
it("动作插入删除与草稿重开不复用其他动作结果", async () => {
  const { page } = await setup(
    draftContent([camera(60, "A"), camera(30, "B")]),
  );
  await check(page.getByTestId("video-size-value").nth(0)).toContainText("975");
  await check(page.getByTestId("video-size-value").nth(1)).toContainText(
    "487.5",
  );
  await page
    .locator(".action-card")
    .first()
    .getByRole("button", { name: "删除动作", exact: true })
    .click();
  await check(page.getByTestId("video-size-value")).toContainText("487.5");
  await page.getByRole("button", { name: "添加动作", exact: true }).click();
  const added = page.locator(".action-card").last();
  await choose(added.getByLabel("动作类型", { exact: true }), "camera_record");
  await choose(added.getByLabel("目标设备", { exact: true }), device);
  await choose(added.getByLabel("参数类型", { exact: true }), "example_record");
  await added.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await added
    .getByLabel("参数 JSON 文本", { exact: true })
    .fill('{"type":"example_record","bitrate_mode":"high","duration_s":120}');
  await check(added.getByTestId("video-size-value")).toContainText("1.95 GB");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByTestId("video-size-value").nth(0)).toContainText(
    "487.5",
  );
  await check(page.getByTestId("video-size-value").nth(1)).toContainText(
    "1.95 GB",
  );
});
it.each([
  "",
  "/actions",
  "/actions/0",
  "/actions/0/device_id",
  "/actions/0/params/type",
  "/actions/0/params/other",
])("浏览器相关pending %s 保留原文且无数值", async (path) => {
  const content = {
    ...draftContent([camera()]),
    pending: { [path]: { kind: "json" as const, text: "{" } },
  };
  const { page, app, draft } = await setup(content);
  await check(page.getByTestId("video-size-estimate")).toContainText(/未完成/);
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  expect(app.draft(draft.id).content).toEqual(content);
});
it("设备切换按精确作用域选择码率，不沿用上一设备数值", async () => {
  const document = parseClientJson(capabilityText) as any;
  const second = parseClientJson(stringifyJson(document.devices[0])) as any;
  second.device_id = "estimate_demo_cam1";
  second.actions.find(
    (a: any) => a.type === "camera_record",
  ).parameter_types[0].video_size_estimate.bitrate_mbps.values.high = 80;
  document.devices.push(second);
  const { page } = await setup(
    draftContent([camera()]),
    stringifyJson(document),
  );
  await check(page.getByTestId("video-size-value")).toContainText("975");
  await choose(page.getByLabel("目标设备", { exact: true }), second.device_id);
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
  await choose(page.getByLabel("目标设备", { exact: true }), device);
  await check(page.getByTestId("video-size-value")).toContainText("975 MB");
});
it("保存准备失败未发POST时恢复当前输入的估算", async () => {
  const { page } = await setup();
  let postCount = 0,
    stateUnavailable = false;
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "PUT") {
      stateUnavailable = true;
      await route.abort();
    } else await route.continue();
  });
  await page.route("**/api/state", async (route) => {
    if (stateUnavailable) await route.abort();
    else await route.continue();
  });
  page.on("request", (request) => {
    if (request.url().endsWith("/api/capabilities/reload")) postCount++;
  });
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("30");
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await check(page.locator("body")).toContainText(/尚未开始能力重载/);
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toContainText("487.5 MB");
  expect(postCount).toBe(0);
});
it("失败重载的正常响应观察整体恢复此前说明", async () => {
  const { page, directory, app } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  writeFileSync(join(directory, "device-capabilities.json"), '{"devices":[]}');
  // 空设备目录有效，先实际成功替换后，录像选择失效。
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  expect(app.capabilities.active?.devices).toHaveLength(0);
  writeFileSync(join(directory, "device-capabilities.json"), capabilityText);
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  writeFileSync(join(directory, "device-capabilities.json"), "{}");
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  await check(page.getByTestId("video-size-estimate")).toContainText(/此前/);
});
it("能力首次不可用呈现诊断，修正说明后首次启用恢复", async () => {
  const { page, directory } = await setup(draftContent([camera()]), "{}");
  await check(page.getByTestId("video-size-estimate")).toContainText(/不可用/);
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  writeFileSync(join(directory, "device-capabilities.json"), capabilityText);
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toContainText("975");
});
it("HTTP交接保留非整数帧参数证据，数学整数修正后恢复", async () => {
  const document = parseClientJson(capabilityText) as any;
  const parameter = document.devices[0].actions
    .find((a: any) => a.type === "camera_timelapse")
    .parameter_types.find(
      (p: any) => p.video_size_estimate.duration.method === "timelapse_frames",
    );
  // 实际引用成员来自共同演示说明，原文传递经过真实HTTP再由浏览器解析。
  const path = parameter.video_size_estimate.duration.frames.path.slice(1);
  parameter.schema.properties[path] = {
    type: "number",
    exclusiveMinimum: 0,
    title: "预计帧数",
  };
  const action = {
    ...camera(),
    type: "camera_timelapse",
    params: { type: parameter.type, [path]: 1 },
  };
  const content = draftContent([action]);
  content.text = content.text.replace(
    `"${path}":1`,
    `"${path}":1.00000000000000000001`,
  );
  const { page } = await setup(content, stringifyJson(document));
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await check(page.getByTestId("video-size-estimate")).toContainText(
    /预计帧数/,
  );
  await check(page.getByTestId("video-size-estimate")).toContainText(
    `/${path}`,
  );
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page
    .getByLabel("参数 JSON 文本", { exact: true })
    .fill(`{"type":"${parameter.type}","${path}":2.4e2}`);
  await check(page.getByTestId("video-size-value")).toContainText("175 MB");
});
it("非法小数帧常量经过真实加载和HTTP观察不能启用", async () => {
  const document = parseClientJson(capabilityText) as any;
  const parameter = document.devices[0].actions
    .find((a: any) => a.type === "camera_timelapse")
    .parameter_types.find(
      (p: any) => p.video_size_estimate.duration.method === "timelapse_frames",
    );
  parameter.video_size_estimate.duration.frames = {
    source: "constant",
    value: 1,
  };
  const text = stringifyJson(document).replace(
    '"source":"constant","value":1',
    '"source":"constant","value":1.00000000000000000001',
  );
  const { page, app } = await setup(draftContent([camera()]), text);
  expect(app.capabilities.active).toBeNull();
  expect(app.capabilities.error).toBeTruthy();
  await check(page.getByTestId("video-size-estimate")).toContainText(/不可用/);
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
});
it("POST成功但新观察丢失时暂停，后续成功观察启用实际新说明", async () => {
  const { page, directory } = await setup();
  const document = parseClientJson(capabilityText) as any;
  document.devices[0].actions.find(
    (a: any) => a.type === "camera_record",
  ).parameter_types[0].video_size_estimate.bitrate_mbps.values.high = 80;
  writeFileSync(
    join(directory, "device-capabilities.json"),
    stringifyJson(document),
  );
  let unavailable = false;
  await page.route("**/api/state", async (route) => {
    if (unavailable) await route.abort();
    else await route.continue();
  });
  await page.route("**/api/capabilities/reload", async (route) => {
    const response = await route.fetch();
    unavailable = true;
    await route.fulfill({ response });
  });
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-estimate")).toContainText(/核实/);
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  unavailable = false;
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
});
it("晚到的重载前观察及POST未结束的观察不能解除暂停", async () => {
  const { page, directory } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const old = gate(),
    post = gate();
  let oldStarted = false,
    postStarted = false;
  await page.route("**/api/state", async (route) => {
    if (!oldStarted) {
      oldStarted = true;
      const response = await route.fetch();
      await old.promise;
      await route.fulfill({ response });
    } else await route.continue();
  });
  await check.poll(() => oldStarted).toBe(true);
  const document = parseClientJson(capabilityText) as any;
  document.devices[0].actions
    .find((a: any) => a.type === "camera_record")
    .parameter_types.find(
      (p: any) => p.type === "example_record",
    ).video_size_estimate.bitrate_mbps.values.high = 80;
  writeFileSync(
    join(directory, "device-capabilities.json"),
    stringifyJson(document),
  );
  await page.route("**/api/capabilities/reload", async (route) => {
    postStarted = true;
    await post.promise;
    await route.continue();
  });
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  old.release();
  await check.poll(() => postStarted).toBe(true);
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await check(page.getByTestId("video-size-estimate")).toContainText(/更新/);
  post.release();
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
});
it("响应丢失暂停，失败观察不能解锁，版本不变的新观察恢复旧说明", async () => {
  const { page, app, directory, draft } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const before = app.draft(draft.id),
    version = app.capabilities.version;
  let unavailable = false,
    handled = false;
  await page.route("**/api/state", async (route) => {
    if (unavailable) await route.abort();
    else await route.continue();
  });
  const document = parseClientJson(capabilityText) as any;
  document.devices[0].actions.find(
    (a: any) => a.type === "camera_record",
  ).parameter_types[0].video_size_estimate = null;
  writeFileSync(
    join(directory, "device-capabilities.json"),
    stringifyJson(document),
  );
  await page.route("**/api/capabilities/reload", async (route) => {
    await route.fetch();
    handled = true;
    unavailable = true;
    await route.abort();
  });
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check.poll(() => handled).toBe(true);
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await check(page.getByTestId("video-size-estimate")).toContainText(/核实/);
  await page.waitForRequest((request) => request.url().endsWith("/api/state"));
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  unavailable = false;
  await check(page.getByTestId("video-size-value")).toContainText("975");
  await check(page.getByTestId("video-size-estimate")).toContainText(/此前/);
  expect(app.capabilities.version).toBe(version);
  expect(app.capabilities.error).toBeTruthy();
  expect(app.draft(draft.id)).toEqual(before);
});

async function pauseAfterLostReload(
  page: Page,
  directory: string,
  changed = true,
) {
  const control = { reads: 0, allow: false, posts: 0 };
  await page.route("**/api/state", async (route) => {
    if (control.allow || control.reads > 0) {
      if (!control.allow) control.reads--;
      await route.continue();
    } else await route.abort();
  });
  const document = parseClientJson(capabilityText) as any;
  document.devices[0].actions.find(
    (a: any) => a.type === "camera_record",
  ).parameter_types[0].video_size_estimate.bitrate_mbps.values.high = changed
    ? 80
    : 130;
  writeFileSync(
    join(directory, "device-capabilities.json"),
    changed ? stringifyJson(document) : "{}",
  );
  await page.route("**/api/capabilities/reload", async (route) => {
    control.posts++;
    await route.fetch();
    await route.abort();
  });
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-estimate")).toContainText(/核实/);
  return control;
}
it("再次准备失败保留此前未核实责任并仍能由之后新观察恢复", async () => {
  const { page, directory } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const control = await pauseAfterLostReload(page, directory);
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "PUT") await route.abort();
    else await route.continue();
  });
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("30");
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  await check(
    page.getByRole("button", { name: "重新加载能力说明" }),
  ).toBeEnabled();
  await check(page.locator("body")).toContainText(/尚未开始能力重载/);
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  await check(page.getByTestId("video-size-estimate")).toContainText(/核实/);
  expect(control.posts).toBe(1);
  await check(
    page.getByLabel("录像时长 (duration_s)", { exact: true }),
  ).toHaveValue("30");
  control.allow = true;
  await check(page.getByTestId("video-size-value")).toContainText("300 MB");
});
it("保存回执丢失的唯一核实观察恢复估算且不重入pendingWrite", async () => {
  const { page, app, draft, directory } = await setup(
    undefined,
    undefined,
    true,
  );
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const control = await pauseAfterLostReload(page, directory);
  let writes = 0;
  let successfulReads = 0;
  page.on("response", (response) => {
    if (response.url().endsWith("/api/state") && response.status() === 200)
      successfulReads++;
  });
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "PUT") {
      writes++;
      await route.fetch();
      control.reads = 1;
      await route.abort();
    } else await route.continue();
  });
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("30");
  // 只触发450ms自动保存，不触发从同一冻结点起算的900ms轮询。
  await page.clock.runFor(450);
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await check(page.getByTestId("video-size-value")).toContainText("300 MB");
  expect(writes).toBe(1);
  expect(successfulReads).toBe(1);
  expect(
    (parseClientJson(app.draft(draft.id).content.text) as any).actions[0].params
      .duration_s,
  ).toBe(30);
});
it("递交标记核实的唯一完整观察恢复全部会话与实际能力", async () => {
  const { page, app, directory } = await setup();
  const recordDraft = app.createDraft(
    draftContent([
      { name: "同步", type: "report_status", params: { scope: "full" } },
    ]),
  );
  const record = app.exportDraft(
    recordDraft.id,
    recordDraft.revision,
    recordDraft.content,
  );
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const control = await pauseAfterLostReload(page, directory);
  await page.route("**/api/requests/*/handoff", async (route) => {
    await route.fetch();
    control.reads = 1;
    await route.abort();
  });
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").click();
  await page.getByRole("button", { name: "标记已递交", exact: true }).click();
  await check(
    page.getByRole("button", { name: "清除递交标记", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: /^草稿/ }).click();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
  expect(app.request(record.id).handedAt).not.toBeNull();
});
it("删除核实观察恢复其他草稿估算且仍确认删除缺失事实", async () => {
  const { page, app, directory } = await setup();
  const spare = app.createDraft({
    text: stringifyJson({ name: "待删除", actions: [] }),
  });
  await page.reload();
  await page
    .getByTestId("draft-open-button")
    .filter({ hasText: "估算" })
    .click();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const control = await pauseAfterLostReload(page, directory);
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "DELETE") {
      await route.fetch();
      control.reads = 1;
      await route.abort();
    } else await route.continue();
  });
  await page
    .getByTestId("draft-open-button")
    .filter({ hasText: "待删除" })
    .click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除草稿", exact: true }).click();
  await check(
    page.getByTestId("draft-open-button").filter({ hasText: "待删除" }),
  ).toHaveCount(0);
  await page.getByTestId("draft-open-button").click();
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
  expect(app.state().drafts?.some((d) => d.id === spare.id)).toBe(false);
});
it("导出核实接纳实际原请求并恢复其他草稿能力", async () => {
  const { page, app, directory } = await setup();
  app.createDraft({
    text: stringifyJson({
      name: "待导出",
      actions: [
        { name: "同步", type: "report_status", params: { scope: "full" } },
      ],
    }),
  });
  await page.reload();
  await page
    .getByTestId("draft-open-button")
    .filter({ hasText: "估算" })
    .click();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  // 导出仍按原 capabilityVersion 检查；版本不变才能实际成功导出。
  const control = await pauseAfterLostReload(page, directory, false);
  await page.route("**/api/drafts/*/export", async (route) => {
    await route.fetch();
    control.reads = 2;
    await route.abort();
  });
  await page
    .getByTestId("draft-open-button")
    .filter({ hasText: "待导出" })
    .click();
  await check(page.getByLabel("计划名称", { exact: true })).toHaveValue(
    "待导出",
  );
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("download-request-button")).toBeVisible();
  await page.getByRole("tab", { name: /^草稿/ }).click();
  await page.getByTestId("draft-open-button").click();
  await check(page.getByTestId("video-size-value")).toContainText("975 MB");
  await check(page.getByTestId("video-size-estimate")).toContainText(/此前/);
  expect(app.state().requests).toHaveLength(1);
});
it("后续动作两次准备和追加核实共用观察但不重复追加", async () => {
  const { page, app, draft, directory } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const control = await pauseAfterLostReload(page, directory);
  let appends = 0;
  await page.route("**/api/drafts/*/actions", async (route) => {
    appends++;
    await route.fetch();
    control.reads = 1;
    await route.abort();
  });
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await choose(page.getByLabel("目标草稿"), draft.id);
  control.reads = 2;
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
  expect(appends).toBe(1);
  expect(
    (parseClientJson(app.draft(draft.id).content.text) as any).actions,
  ).toHaveLength(2);
});
it("查看目标的唯一完整观察恢复估算且不把准备失败解释为追加", async () => {
  const { page, app, draft, directory } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const control = await pauseAfterLostReload(page, directory);
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await choose(page.getByLabel("目标草稿"), draft.id);
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await check(
    page.getByRole("button", { name: "查看目标草稿", exact: true }),
  ).toBeEnabled();
  control.reads = 1;
  await page.getByRole("button", { name: "查看目标草稿", exact: true }).click();
  await check(page.getByRole("dialog")).toHaveCount(0);
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
  expect(
    (parseClientJson(app.draft(draft.id).content.text) as any).actions,
  ).toHaveLength(1);
});
it("上传失败核实接纳完整观察并仍按uploading事实记录失败", async () => {
  const { page, app, directory } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const control = await pauseAfterLostReload(page, directory);
  const initialRefresh = gate();
  clean.unshift(async () => {
    initialRefresh.release();
  });
  let uploading = false;
  await page.route("**/api/state", async (route) => {
    if (uploading) {
      await route.abort();
      initialRefresh.release();
    } else await route.fallback();
  });
  await page.route("**/api/imports/*/content", async (route) => {
    uploading = true;
    // 文件批次原有的即时刷新先失败；其后唯一成功GET来自上传错误核实。
    await initialRefresh.promise;
    uploading = false;
    control.reads = 1;
    await route.abort();
  });
  await page.getByTestId("nav-import").click();
  await page.getByTestId("import-files").setInputFiles({
    name: "video.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("demo"),
  });
  await check.poll(() => app.state().imports?.[0]?.status).toBe("interrupted");
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
  expect(app.state().imports).toHaveLength(1);
});
it("导出被旧能力版本拒绝后核实观察恢复估算但不补造原请求", async () => {
  const { page, app, directory } = await setup();
  app.createDraft({
    text: stringifyJson({
      name: "待导出",
      actions: [
        { name: "同步", type: "report_status", params: { scope: "full" } },
      ],
    }),
  });
  await page.reload();
  await page
    .getByTestId("draft-open-button")
    .filter({ hasText: "估算" })
    .click();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const control = await pauseAfterLostReload(page, directory);
  let responseCode = "";
  await page.route("**/api/drafts/*/export", async (route) => {
    const response = await route.fetch();
    responseCode = (parseClientJson(await response.text()) as any).code;
    control.reads = 1;
    await route.abort();
  });
  await page
    .getByTestId("draft-open-button")
    .filter({ hasText: "待导出" })
    .click();
  await check(page.getByLabel("计划名称", { exact: true })).toHaveValue(
    "待导出",
  );
  await page.getByTestId("export-button").click();
  await check(page.getByTestId("export-button")).toBeEnabled();
  await check(page.locator("body")).toContainText(/请求结果尚未确认/);
  expect(responseCode).toBe("capabilities_changed");
  expect(app.state().requests).toHaveLength(0);
  await page
    .getByTestId("draft-open-button")
    .filter({ hasText: "估算" })
    .click();
  await check(page.getByTestId("video-size-value")).toContainText("600 MB");
});
it("保存核实在准备期间接纳同组事实但直到POST结束后新观察才恢复", async () => {
  const { page, app, draft, directory } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const put = gate(),
    putEntered = gate(),
    read = gate(),
    readEntered = gate(),
    post = gate(),
    postEntered = gate();
  clean.unshift(async () => {
    put.release();
    read.release();
    post.release();
  });
  let saved = false,
    holdRead = true,
    available = false,
    writes = 0;
  await page.route("**/api/state", async (route) => {
    if (saved && holdRead) {
      const response = await route.fetch();
      holdRead = false;
      readEntered.release();
      await read.promise;
      await route.fulfill({ response });
    } else if (available || !saved) await route.continue();
    else await route.abort();
  });
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    writes++;
    await route.fetch();
    putEntered.release();
    await put.promise;
    saved = true;
    await route.abort();
  });
  await page.route("**/api/capabilities/reload", async (route) => {
    const response = await route.fetch();
    postEntered.release();
    await post.promise;
    await route.fulfill({ response });
  });
  const document = parseClientJson(capabilityText) as any;
  document.devices[0].actions.find(
    (a: any) => a.type === "camera_record",
  ).parameter_types[0].video_size_estimate.bitrate_mbps.values.high = 80;
  writeFileSync(
    join(directory, "device-capabilities.json"),
    stringifyJson(document),
  );
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("30");
  await putEntered.promise;
  await page.getByTestId("nav-devices").click();
  await page.getByRole("button", { name: "重新加载能力说明" }).click();
  put.release();
  await readEntered.promise;
  await page.getByTestId("nav-plans").click();
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  read.release();
  await postEntered.promise;
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await check(page.getByTestId("video-size-value")).toHaveCount(0);
  post.release();
  await check(page.getByTestId("video-size-estimate")).toContainText(/核实/);
  expect(writes).toBe(1);
  expect(
    (parseClientJson(app.draft(draft.id).content.text) as any).actions[0].params
      .duration_s,
  ).toBe(30);
  available = true;
  await check(page.getByTestId("video-size-value")).toContainText("300 MB");
});
it("较旧完整观察晚到不覆盖保存核实接纳的能力诊断与草稿", async () => {
  const { page, app, draft, directory } = await setup();
  await check(page.getByTestId("video-size-value")).toContainText("975");
  const old = gate(),
    entered = gate(),
    delivered = gate();
  clean.unshift(async () => {
    old.release();
  });
  let first = true,
    allow = false,
    writes = 0;
  await page.route("**/api/state", async (route) => {
    if (first) {
      first = false;
      const response = await route.fetch();
      entered.release();
      await old.promise;
      await route.fulfill({ response });
      delivered.release();
    } else if (allow) await route.continue();
    else await route.abort();
  });
  await entered.promise;
  const document = parseClientJson(capabilityText) as any;
  document.devices[0].actions.find(
    (a: any) => a.type === "camera_record",
  ).parameter_types[0].video_size_estimate.bitrate_mbps.values.high = 80;
  writeFileSync(
    join(directory, "device-capabilities.json"),
    stringifyJson(document),
  );
  app.reloadCapabilities();
  writeFileSync(join(directory, "device-capabilities.json"), "{}");
  app.reloadCapabilities();
  await page.route("**/api/drafts/*", async (route) => {
    if (route.request().method() === "PUT") {
      writes++;
      await route.fetch();
      allow = true;
      await route.abort();
    } else await route.continue();
  });
  await page.getByLabel("录像时长 (duration_s)", { exact: true }).fill("30");
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await check(page.getByTestId("video-size-value")).toContainText("300 MB");
  await check(page.getByTestId("video-size-estimate")).toContainText(/此前/);
  const oldResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/state") && response.status() === 200,
  );
  allow = false;
  old.release();
  await delivered.promise;
  await (await oldResponse).finished();
  // 浏览器收到旧响应后，等待其fetch完成和一次渲染帧，再检查完整快照。
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await check(page.getByTestId("video-size-value")).toContainText("300 MB");
  await check(page.getByTestId("video-size-estimate")).toContainText(/此前/);
  await check(
    page.getByLabel("录像时长 (duration_s)", { exact: true }),
  ).toHaveValue("30");
  expect(writes).toBe(1);
  expect(
    (parseClientJson(app.draft(draft.id).content.text) as any).actions[0].params
      .duration_s,
  ).toBe(30);
});
