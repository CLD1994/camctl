import { afterAll, afterEach, beforeAll, expect, it } from "vitest";
import {
  chromium,
  expect as check,
  type Browser,
  type Page,
} from "@playwright/test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import { parseJson, stringifyJson } from "../../src/shared/json";
import type { DraftContent } from "../../src/server/models";
import type { ParameterType } from "../../src/shared/types";
import {
  linkedParameter,
  storageParameter,
  storageScenarios,
} from "../fixtures/linked-parameters";
import { choose, readOptions } from "./select-support";

let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
});
afterAll(async () => {
  await browser?.close();
});
afterEach(async () => {
  for (const close of clean.splice(0)) await close();
});

async function setup(
  params = '{"interval_s":1,"duration_s":3,"exposure":{"mode":"manual","iso":800}}',
  pending?: DraftContent["pending"],
  parameter: ParameterType = linkedParameter(),
) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-linked-parameters-"));
  const capabilityPath = join(directory, "device-capabilities.json");
  const capabilities = {
    devices: [
      {
        device_id: "camera",
        driver_id: "test",
        actions: [{ type: "camera_timelapse", parameter_types: [parameter] }],
      },
    ],
  };
  writeFileSync(capabilityPath, stringifyJson(capabilities));
  const app = new Application(directory),
    files = new Files(app),
    requests = new RequestLifecycle();
  app.store.initialize();
  const draft = app.createDraft({
    text: `{"name":"关联编辑","actions":[{"name":"拍摄","type":"camera_timelapse","device_id":"camera","scheduled_at":"2026-10-16 12:00:00","policy":{"max_delay_ms":0},"params":{"type":"linked_capture"${params === "{}" ? "" : ","}${params.slice(1)}}]}`,
    ...(pending ? { pending } : {}),
  });
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const context = await browser.newContext(),
    page = await context.newPage();
  page.setDefaultTimeout(5000);
  const diagnostics: string[] = [];
  page.on("pageerror", (error) =>
    diagnostics.push(`pageerror: ${error.message}`),
  );
  page.on("requestfailed", (request) =>
    diagnostics.push(
      `requestfailed: ${request.method()} ${request.url()} ${request.failure()?.errorText}`,
    ),
  );
  page.on("response", (response) => {
    if (response.status() >= 400)
      diagnostics.push(
        `HTTP ${response.status()}: ${response.request().method()} ${response.url()}`,
      );
  });
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
    if (diagnostics.length) console.info(diagnostics.join("\n"));
    expect(
      diagnostics.filter((entry) => entry.startsWith("pageerror")),
    ).toEqual([]);
  });
  await page.goto(
    `http://127.0.0.1:${(server.address() as { port: number }).port}`,
  );
  await page.getByTestId("draft-open-button").click();
  const saved = () => app.draft(draft.id);
  const current = () =>
    (parseJson(saved().content.text) as any).actions[0].params;
  return { app, page, draft, saved, current, capabilityPath, capabilities };
}
const interval = (page: Page) =>
  page.getByRole("combobox", { name: "拍摄间隔 (interval_s)", exact: true });
const duration = (page: Page) =>
  page.getByRole("combobox", { name: "采集时长 (duration_s)", exact: true });
const mode = (page: Page) =>
  page.getByRole("combobox", { name: "mode (mode)", exact: true });
const iso = (page: Page) =>
  page.getByRole("combobox", { name: "iso (iso)", exact: true });
async function values(control: ReturnType<typeof interval>) {
  return (await readOptions(control))
    .filter((item) => item.value !== "" && item.value !== "invalid")
    .map((item) => item.text);
}

it("时间双向筛选只写操作字段，独立曝光不完整时仍可修正时间", async () => {
  const { page, current, saved, app, draft } = await setup(
    '{"interval_s":1,"duration_s":3,"exposure":{"mode":"manual"}}',
  );
  expect(await values(interval(page))).toEqual(["1", "2"]);
  expect(await values(duration(page))).toEqual(["2", "3", "4"]);
  const before = saved().content;
  await choose(interval(page), "1");
  await check.poll(() => current().interval_s).toBe(2);
  expect(current()).toEqual({
    type: "linked_capture",
    interval_s: 2,
    duration_s: 3,
    exposure: { mode: "manual" },
  });
  expect(saved().content.pending ?? {}).toEqual(before.pending ?? {});
  expect(saved().content.objectVariants).toEqual(before.objectVariants);
  expect(await values(duration(page))).toEqual(["3", "4"]);
  const invalid = saved();
  expect(() =>
    app.exportDraft(draft.id, invalid.revision, invalid.content),
  ).toThrow();
  await choose(iso(page), "1");
  await check.poll(() => current().exposure.iso).toBe(800);
});

it("时间缺省不选择首项，首次曝光选择只建立mode且Auto没有EV", async () => {
  const { page, current, saved } = await setup("{}");
  const before = saved();
  await check(interval(page)).toHaveAttribute("data-value", "");
  await check(duration(page)).toHaveAttribute("data-value", "");
  expect(await values(interval(page))).toEqual(["1", "2", "3"]);
  expect(saved()).toEqual(before);
  await choose(mode(page), "branch:0");
  await check(iso(page)).toHaveAttribute("data-value", "");
  await check.poll(() => current().exposure).toEqual({ mode: "manual" });
  expect(current()).not.toHaveProperty("interval_s");
  expect(current()).not.toHaveProperty("duration_s");
  await choose(mode(page), "branch:1");
  await check.poll(() => current().exposure).toEqual({ mode: "auto" });
  await check(iso(page)).toHaveCount(0);
  await check(
    page.getByRole("combobox", {
      name: "compensation_ev (compensation_ev)",
      exact: true,
    }),
  ).toHaveCount(0);
  await choose(mode(page), "branch:0");
  await check.poll(() => current().exposure).toEqual({ mode: "manual" });
});

it.each(["null", '"3"', "true", "99"])(
  "其他时间字段非法 %s 时保留原值，经参数JSON修正恢复选择",
  async (value) => {
    const { page, current, saved } = await setup(
      `{"interval_s":1,"duration_s":${value},"exposure":{"mode":"auto"}}`,
    );
    const before = saved();
    expect(await values(interval(page))).toEqual([]);
    await check(
      page.locator(".danger-text").filter({ hasText: "没有兼容选项" }),
    ).toBeVisible();
    expect(saved()).toEqual(before);
    await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
    await page
      .getByLabel("参数 JSON 文本", { exact: true })
      .fill(
        '{"type":"linked_capture","interval_s":1,"duration_s":3,"exposure":{"mode":"auto"}}',
      );
    await check.poll(() => current().duration_s).toBe(3);
    await page.getByRole("button", { name: "参数表单", exact: true }).click();
    expect(await values(interval(page))).toEqual(["1", "2"]);
    await choose(interval(page), "1");
    await check.poll(() => current().interval_s).toBe(2);
    expect(current().duration_s).toBe(3);
  },
);

it("非法时间组合按另一字段修正自身，不重写另一字段", async () => {
  const { page, current } = await setup(
    '{"interval_s":3,"duration_s":2,"exposure":{"mode":"auto"}}',
  );
  await check(interval(page)).toHaveAttribute("data-value", "invalid");
  await check(duration(page)).toHaveAttribute("data-value", "invalid");
  expect(await values(interval(page))).toEqual(["1"]);
  expect(await values(duration(page))).toEqual(["4"]);
  await choose(duration(page), "2");
  await check.poll(() => current().duration_s).toBe(4);
  expect(current().interval_s).toBe(3);
});

it("时间pending只暂停时间组，曝光往返保留成员pending与原ISO", async () => {
  const pending: DraftContent["pending"] = {
    "/actions/0/params/interval_s": { kind: "number", text: "1e" },
    "/actions/0/params/exposure/iso": { kind: "number", text: "8e" },
  };
  const { page, saved, current } = await setup(undefined, pending);
  await check(interval(page)).toBeDisabled();
  await check(duration(page)).toBeDisabled();
  await check(mode(page)).toBeEnabled();
  await choose(mode(page), "branch:1");
  await check.poll(() => current().exposure).toEqual({ mode: "auto" });
  expect(saved().content.pending).toEqual({
    "/actions/0/params/interval_s": pending["/actions/0/params/interval_s"],
  });
  await choose(mode(page), "branch:0");
  await check.poll(() => saved().content.pending).toEqual(pending);
  expect(current().exposure).toEqual({ mode: "manual", iso: 800 });
  await check(iso(page)).toContainText("8e");
  expect(current().interval_s).toBe(1);
  expect(current().duration_s).toBe(3);
});

it("曝光整段pending不遮蔽时间选择且完整原文随保存保留", async () => {
  const pending = {
    "/actions/0/params/exposure": { kind: "json" as const, text: '{"mode":' },
  };
  const { page, saved, current } = await setup(undefined, pending);
  await choose(interval(page), "1");
  await check.poll(() => current().interval_s).toBe(2);
  expect(saved().content.pending).toEqual(pending);
  expect(current().exposure).toEqual({ mode: "manual", iso: 800 });
});

it("完整诊断保留根额外字段、时间与当前曝光错误，其他模式错误不混入表单", async () => {
  const { page } = await setup(
    '{"interval_s":3,"duration_s":2,"exposure":{"mode":"manual"},"extra":true}',
  );
  await check(iso(page)).toHaveAttribute("aria-invalid", "true");
  await check(page.locator(".validation")).toContainText("extra");
  await check(interval(page)).toHaveAttribute("aria-invalid", "true");
  await page.locator(".raw-validation > summary").click();
  for (const code of [
    "schema_additionalProperties",
    "schema_exclusiveMaximum",
    "schema_required",
    "schema_oneOf",
  ])
    await check(page.locator(".raw-validation")).toContainText(code);
});

it("查看、切换视图、折叠及重载重新派生选项，完整草稿及版本保持", async () => {
  const { page, saved, capabilityPath, capabilities } = await setup();
  await choose(mode(page), "branch:1");
  await check
    .poll(
      () => (parseJson(saved().content.text) as any).actions[0].params.exposure,
    )
    .toEqual({ mode: "auto" });
  const before = saved();
  await readOptions(interval(page));
  await page.getByRole("button", { name: "参数 JSON", exact: true }).click();
  await page.getByRole("button", { name: "参数表单", exact: true }).click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-toggle").click();
  await page.getByRole("button", { name: "全部收起", exact: true }).click();
  await page.getByRole("button", { name: "全部展开", exact: true }).click();
  await page.getByRole("button", { name: /设备说明/ }).click();
  const guide = page.getByTestId("device-guide");
  const table = guide.locator(".guide-combinations table");
  await check(table.locator("thead th")).toHaveText(["拍摄间隔", "采集时长"]);
  await check(table.locator("tbody tr")).toHaveCount(6);
  await check(guide.locator(".guide-fields")).toContainText("video、raw");
  const parameter = capabilities.devices[0].actions[0].parameter_types[0];
  (parameter.schema.properties as any).interval_s.enum = [1, 2, 3, 4, 5, 6];
  (parameter.schema.properties as any).duration_s.enum = [7, 8, 9, 10, 11, 12];
  writeFileSync(capabilityPath, stringifyJson(capabilities));
  await page
    .getByRole("button", { name: "重新加载能力说明", exact: true })
    .click();
  await check(guide.locator(".notice").filter({ hasText: "36" })).toContainText(
    "拍摄间隔、采集时长",
  );
  await check(table).toHaveCount(0);
  await check(guide).not.toContainText("grouped");
  expect(saved()).toEqual(before);
});

it("局部组选值写入原数字词元且其他字段保持", async () => {
  const parameter = linkedParameter();
  parameter.schema = parseJson(
    '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,"properties":{"type":{"const":"linked_capture"},"interval_s":{"title":"拍摄间隔","enum":[1e-1,0.2]},"duration_s":{"title":"采集时长","enum":[0.2,0.3]},"exposure":{"type":"object"}},"required":["type","interval_s","duration_s"],"allOf":[{"if":{"properties":{"duration_s":{"const":0.2}},"required":["duration_s"]},"then":{"properties":{"interval_s":{"exclusiveMaximum":0.2}}}}]}',
  ) as Record<string, unknown>;
  const { page, saved, current } = await setup(
    '{"interval_s":0.2,"duration_s":0.3,"exposure":{}}',
    undefined,
    parameter,
  );
  await choose(interval(page), "0");
  await check.poll(() => saved().content.text).toContain('"interval_s": 1e-1');
  expect(current().duration_s).toBe(0.3);
  expect(current().exposure).toEqual({});
});

for (const scenario of storageScenarios) {
  it.each(["narrow", "remove"])(
    `${scenario.device} 能力 %s 后重开保留完整原值与错误`,
    async (change) => {
      const { page, saved, current, capabilityPath, capabilities } =
        await setup(
          '{"interval_s":1,"duration_s":300,"exposure":{"mode":"manual","iso":800},"outputs":"video"}',
          { "/actions/0/policy/max_delay_ms": { kind: "number", text: "-" } },
          storageParameter(scenario.outputs),
        );
      await choose(mode(page), "branch:1");
      await check.poll(() => current().exposure).toEqual({ mode: "auto" });
      const before = saved();
      const narrowed = storageParameter(scenario.outputs);
      (narrowed.schema.properties as any).duration_s.enum = [600];
      capabilities.devices[0].actions[0].parameter_types[0] = narrowed;
      writeFileSync(
        capabilityPath,
        stringifyJson(change === "remove" ? { devices: [] } : capabilities),
      );
      expect(
        (
          await page.request.post(
            new URL("/api/capabilities/reload", page.url()).href,
          )
        ).status(),
      ).toBe(200);
      await page.reload();
      await page.getByTestId("draft-open-button").click();
      await check(page.locator(".validation")).toBeVisible();
      expect(saved()).toEqual(before);
      if (change === "narrow") {
        await check(duration(page)).toHaveAttribute("data-value", "invalid");
        await choose(mode(page), "branch:0");
        await check
          .poll(() => current().exposure)
          .toEqual({ mode: "manual", iso: 800 });
        expect(current().duration_s).toBe(300);
        expect(saved().content.pending).toEqual(before.content.pending);
      } else {
        await check(interval(page)).toHaveCount(0);
        expect(current()).toEqual({
          type: "linked_capture",
          interval_s: 1,
          duration_s: 300,
          exposure: { mode: "auto" },
          outputs: "video",
        });
      }
    },
  );
  it(`${scenario.device} 应用预设整体替换时间及曝光，清除组内输入并保留组外输入`, async () => {
    const pending: DraftContent["pending"] = {
      "/actions/0/params/interval_s": { kind: "number", text: "1e" },
      "/actions/0/params/exposure/iso": { kind: "number", text: "8e" },
      "/actions/0/policy/max_delay_ms": { kind: "number", text: "-" },
    };
    const { page, current, saved } = await setup(
      '{"interval_s":1,"duration_s":600,"exposure":{"mode":"manual","iso":800},"outputs":"video","extra":true}',
      pending,
      storageParameter(scenario.outputs),
    );
    const params = {
      type: "linked_capture",
      interval_s: 1,
      duration_s: 300,
      exposure: { mode: "auto" },
      outputs: "video",
    };
    const response = await page.request.post(
      new URL("/api/presets", page.url()).href,
      {
        data: {
          name: "最短预设",
          deviceId: "camera",
          actionType: "camera_timelapse",
          params,
        },
      },
    );
    expect(response.status()).toBe(201);
    const preset = parseJson(await response.text()) as { id: string };
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    const original = (parseJson(saved().content.text) as any).actions[0];
    await check(interval(page)).toBeDisabled();
    await page.locator(".preset-box > summary").click();
    await choose(
      page.getByRole("combobox", { name: "已有预设", exact: true }),
      preset.id,
    );
    await page.getByRole("button", { name: "应用预设", exact: true }).click();
    await check.poll(current).toEqual(params);
    expect(saved().content.pending).toEqual({
      "/actions/0/policy/max_delay_ms":
        pending["/actions/0/policy/max_delay_ms"],
    });
    const actual = (parseJson(saved().content.text) as any).actions[0];
    expect({ ...actual, params: undefined }).toEqual({
      ...original,
      params: undefined,
    });
    await check(interval(page)).toBeEnabled();
    expect(
      (
        await readOptions(
          page.getByRole("combobox", {
            name: "输出选择 (outputs)",
            exact: true,
          }),
        )
      )
        .filter((option) => option.value !== "")
        .map((option) => option.text),
    ).toEqual(scenario.outputs);
    await choose(duration(page), "1");
    await check.poll(() => current().duration_s).toBe(600);
    const state = parseJson(
      await (
        await page.request.get(new URL("/api/state", page.url()).href)
      ).text(),
    ) as any;
    expect(state.presets[0].params).toEqual(params);
    const draft = saved();
    const rejected = await page.request.post(
      new URL(`/api/drafts/${draft.id}/export`, page.url()).href,
      {
        data: {
          revision: draft.revision,
          content: draft.content,
          capabilityVersion: state.capabilities.version,
        },
      },
    );
    expect(rejected.status()).toBe(400);
    expect((parseJson(await rejected.text()) as any).code).toBe(
      "unfinished_input",
    );
  });
}

it.each([5400, 6000])(
  "旧 Action6 %s 秒预设重新应用保留原值及错误，不自动改为合法时长",
  async (seconds) => {
    const { page, saved, current, capabilities, capabilityPath } = await setup(
      `{"interval_s":1,"duration_s":${seconds},"exposure":{"mode":"auto"},"outputs":"video"}`,
      undefined,
      storageParameter(storageScenarios[0].outputs, true),
    );
    const params = current();
    const response = await page.request.post(
      new URL("/api/presets", page.url()).href,
      {
        data: {
          name: "旧预设",
          deviceId: "camera",
          actionType: "camera_timelapse",
          params,
        },
      },
    );
    expect(response.status()).toBe(201);
    const preset = parseJson(await response.text()) as { id: string };
    capabilities.devices[0].actions[0].parameter_types[0] = storageParameter(
      storageScenarios[0].outputs,
    );
    writeFileSync(capabilityPath, stringifyJson(capabilities));
    expect(
      (
        await page.request.post(
          new URL("/api/capabilities/reload", page.url()).href,
        )
      ).status(),
    ).toBe(200);
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    await check(duration(page)).toHaveAttribute("data-value", "invalid");
    expect(current().duration_s).toBe(seconds);
    const before = saved();
    await page.locator(".preset-box > summary").click();
    const control = page.getByRole("combobox", {
      name: "已有预设",
      exact: true,
    });
    expect(
      (await readOptions(control)).find((option) => option.value === preset.id)
        ?.text,
    ).toContain("需修正");
    await choose(control, preset.id);
    await page.getByRole("button", { name: "应用预设", exact: true }).click();
    await check(duration(page)).toHaveAttribute("aria-invalid", "true");
    expect(current()).toEqual(params);
    expect(saved().content.text).toBe(before.content.text);
    await check(
      page.getByRole("button", { name: "更新所选预设", exact: true }),
    ).toBeDisabled();
  },
);
