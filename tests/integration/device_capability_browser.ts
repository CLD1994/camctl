// 安装版 describe 与真实页面的联合凭据；测试报告不表示真实设备产物。
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import {
  appendFileSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { observePost } from "../../apps/client/tests/helpers/browser-post";
import { checkVisibleCleanupSupport } from "../../apps/client/tests/integration/metadata-support";
import { join } from "node:path";
import { Application } from "../../apps/client/src/server/application";
import { Files } from "../../apps/client/src/server/files";
import { createHttpApp } from "../../apps/client/src/server/http";
import {
  createStop,
  RequestLifecycle,
} from "../../apps/client/src/server/lifecycle";
import { stringifyJson } from "../../apps/client/src/shared/json";
import type { ReportAction } from "../../apps/client/src/shared/types";
import {
  mappedReport,
  reportInput,
} from "../../apps/client/tests/integration/fixtures";

async function main() {
  const requireClient = createRequire(
    new URL("../../apps/client/package.json", import.meta.url),
  );
  const { chromium, expect: check } = requireClient("@playwright/test");
  const [storeDir, currentFile, changedFile, receiptPath] =
    process.argv.slice(2);
  const tracePath = receiptPath! + ".trace.jsonl";
  const note = (phase: string, facts: Record<string, unknown> = {}) =>
    appendFileSync(
      tracePath,
      JSON.stringify({ phase, at: performance.now(), ...facts }) + "\n",
    );
  const current = readFileSync(currentFile!, "utf8");
  const changed = readFileSync(changedFile!, "utf8");
  const path = join(storeDir!, "device-capabilities.json");
  mkdirSync(storeDir!, { recursive: true });
  writeFileSync(path, current);

  // 独立预期来自源类型决策表，不从被测 JSON 布尔值推导。
  const initial = {
    action6: true,
    osmo: true,
    "hb-host": true,
    "hb-device": true,
    "future-host": true,
    "future-device": false,
    "future-delete": true,
  };
  const devices = Object.keys(initial);
  const seeded = new Application(storeDir!);
  seeded.store.initialize();
  const report = mappedReport(Buffer.from("installed-capability-test"));
  const plan = report.plans![0]!;
  plan.name = "安装版来源记录";
  const captureTemplate = plan.actions![0]!;
  const retrieveTemplate = plan.actions![1]!;
  const outputTemplate = captureTemplate.outputs![0]!;
  const deliveryTemplate = retrieveTemplate.deliveries![0]!;
  const captures = devices.map((device, index): ReportAction => {
    const actionId = String(10 + index);
    const originalId = String(100 + index * 3);
    return {
      ...structuredClone(captureTemplate),
      action_instance_id: actionId,
      name: device,
      device_id: device,
      result: { repair: { status: "succeeded" } },
      outputs: (device === "future-device"
        ? (["original", "preview", "repaired"] as const)
        : (["original"] as const)
      ).map((kind, offset) => ({
        ...structuredClone(outputTemplate),
        output_id: String(100 + index * 3 + offset),
        source_action_instance_id: actionId,
        original_name: `${device}-${kind}.mp4`,
        kind,
        ...(kind === "preview" ? { preview_of_output_id: originalId } : {}),
        ...(kind === "repaired" ? { derived_from_output_id: originalId } : {}),
      })),
    };
  });
  plan.actions = [
    ...captures,
    {
      ...structuredClone(retrieveTemplate),
      action_instance_id: "2",
      name: "混合来源取回",
      input_params: { source: { plan_instance_id: "1" } },
      result: { failures: [] },
      deliveries: captures
        .flatMap((capture) => capture.outputs ?? [])
        .map((output, index) => {
          assert.ok(output.original_name);
          return {
            ...structuredClone(deliveryTemplate),
            delivery_id: String(200 + index),
            output_id: output.output_id,
            source_action_instance_id: output.source_action_instance_id,
            file_name: `${200 + index}.mp4`,
            display_name: output.original_name,
          };
        }),
    },
  ];
  seeded.applyReports([reportInput(report)]);
  assert.equal(seeded.coverage(), report.to_wm);
  const exported = seeded.createDraft({
    text: JSON.stringify({
      name: "保留原请求",
      actions: [
        { name: "同步", type: "report_status", params: { scope: "full" } },
      ],
    }),
  });
  seeded.exportDraft(exported.id, exported.revision, exported.content);
  seeded.createDraft({
    text: JSON.stringify({
      name: "保留旧清理草稿",
      actions: [
        {
          name: "历史清理",
          type: "delete_action_outputs",
          scheduled_at: "2026-10-18 10:00:00",
          params: { source: { plan_instance_id: "1" } },
        },
      ],
    }),
  });
  const saved = (app: Application) =>
    stringifyJson({
      drafts: app.store.all("drafts"),
      requests: app.store.all("requests"),
    });
  const originalSaved = saved(seeded);
  seeded.store.close();

  const browser = await chromium.launch({ headless: true });
  async function start() {
    const app = new Application(storeDir!);
    const files = new Files(app),
      requests = new RequestLifecycle();
    const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
    server.prependListener("request", (request, response) => {
      if (
        !["/api/state", "/api/capabilities/reload"].includes(request.url ?? "")
      )
        return;
      note("server-request", { method: request.method, url: request.url });
      response.once("finish", () =>
        note("server-response", {
          method: request.method,
          url: request.url,
          status: response.statusCode,
          version: app.capabilities.version,
        }),
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
    let context: Awaited<ReturnType<typeof browser.newContext>> | undefined;
    try {
      await new Promise<void>((resolve, reject) => {
        server.once("listening", resolve);
        server.once("error", reject);
      });
      context = await browser.newContext();
      const page = await context.newPage();
      page.setDefaultTimeout(5000);
      let sequence = 0;
      const active = new Map<unknown, number>();
      page.on("request", (request: { url(): string; method(): string }) => {
        if (!/\/api\/(state|capabilities\/reload)$/.test(request.url())) return;
        active.set(request, ++sequence);
        note("browser-request", {
          id: sequence,
          method: request.method(),
          url: request.url(),
        });
      });
      page.on(
        "response",
        (response: { request(): unknown; status(): number }) => {
          if (active.has(response.request()))
            note("browser-response", {
              id: active.get(response.request()),
              status: response.status(),
            });
        },
      );
      for (const event of ["requestfinished", "requestfailed"] as const)
        page.on(event, (request: { failure(): unknown }) => {
          if (active.has(request))
            note(event, {
              id: active.get(request),
              failure: request.failure(),
            });
          active.delete(request);
        });
      page.on("pageerror", (error: Error) =>
        note("pageerror", { message: error.message }),
      );
      page.on("console", (message: { type(): string; text(): string }) => {
        if (message.type() === "error")
          note("console-error", { message: message.text() });
      });
      await page.goto(
        `http://127.0.0.1:${(server.address() as { port: number }).port}`,
        {
          waitUntil: "domcontentloaded",
        },
      );
      return {
        app,
        page,
        active,
        close: async () => {
          await context!.close();
          await stop();
        },
      };
    } catch (error) {
      await context?.close();
      await stop();
      throw error;
    }
  }

  const receipt: Record<string, boolean> = {};
  let running = await start().catch(async (error) => {
    await browser.close();
    throw error;
  });
  async function inspect(expected: Record<string, boolean | undefined>) {
    const { page, app } = running;
    await page.getByTestId("nav-devices").click();
    if (Object.keys(expected).length > 0) {
      await checkVisibleCleanupSupport(
        page,
        devices.map((device) => ({
          device,
          text: expected[device] === false ? "不支持删除" : "支持删除",
        })),
      );
    }
    await page.getByTestId("nav-plans").click();
    await page.getByTestId("tab-records").click();
    await page
      .getByTestId("record-open-button")
      .filter({ hasText: "安装版来源记录" })
      .click();
    const record = page.locator(".record-detail").filter({
      has: page.getByRole("heading", { name: "安装版来源记录", exact: true }),
    });
    await check(
      record.getByRole("button", { name: "清理计划产物" }),
    ).toBeVisible();
    async function expandAction(id: string, name: string) {
      const card = record.locator(`.result-card[data-action-id="${id}"]`);
      const toggle = card.getByRole("button", {
        name: new RegExp(`动作 ${name}$`),
      });
      if ((await toggle.getAttribute("aria-expanded")) !== "true")
        await toggle.click();
      return card;
    }
    for (const [index, device] of devices.entries()) {
      await expandAction(String(10 + index), device);
    }
    await check
      .poll(() =>
        record
          .getByRole("button", { name: "清理全部产物", exact: true })
          .evaluateAll((nodes) =>
            nodes
              .map((node) =>
                node.closest(".result-card")?.getAttribute("data-action-id"),
              )
              .sort(),
          ),
      )
      .toEqual(
        devices
          .flatMap((device, index) =>
            expected[device] === false ? [] : [String(10 + index)],
          )
          .sort(),
      );
    const retrieve = await expandAction("2", "混合来源取回");
    const expectedCleanup = devices.flatMap((device) =>
      Array.from(
        { length: device === "future-device" ? 3 : 1 },
        (_, offset) => offset === 2 || expected[device] !== false,
      ),
    );
    await check(retrieve.locator(".product-card")).toHaveCount(
      expectedCleanup.length,
    );
    const outputs =
      app
        .state()
        .snapshot?.plans?.flatMap((plan) =>
          (plan.actions ?? []).flatMap((action) => action.outputs ?? []),
        ) ?? [];
    assert.equal(outputs.length, expectedCleanup.length);
    assert.ok(outputs.every((output) => output.availability === "available"));
    await check
      .poll(async () => {
        const [buttons, badgeCount] = await Promise.all([
          retrieve
            .getByRole("button", { name: "清理产物", exact: true })
            .evaluateAll((nodes) =>
              nodes
                .map((node) =>
                  Array.from(
                    node
                      .closest(".result-card")!
                      .querySelectorAll(".product-card"),
                  ).indexOf(node.closest(".product-card")!),
                )
                .sort((a, b) => a - b),
            ),
          retrieve.locator(".product-card .product-actions .badge").count(),
        ]);
        return { buttons, badgeCount };
      })
      .toEqual({
        buttons: expectedCleanup.flatMap((enabled, index) =>
          enabled ? [index] : [],
        ),
        badgeCount: 0,
      });
    assert.equal(saved(app), originalSaved);
  }
  async function reload(text: string, failed: boolean, retain = false) {
    const { page, app } = running;
    writeFileSync(path, text);
    const oldVersion = app.capabilities.version;
    await page.getByTestId("nav-devices").click();
    const button = page.getByRole("button", { name: "重新加载能力说明" });
    await check(button).toBeEnabled();
    const observation = observePost(
      page,
      new URL("/api/capabilities/reload", page.url()).href,
    );
    let response;
    try {
      note("reload-click-start", {
        failed,
        retain,
        oldVersion,
        buttonWasEnabled: true,
        activeRequests: [...running.active.values()],
      });
      await button.click();
      const pending = observation.wait();
      note("reload-click-finished", {
        failed,
        retain,
        activeRequests: [...running.active.values()],
      });
      response = await pending;
      note("reload-response-observed", { status: response.status() });
    } finally {
      observation.dispose();
    }
    assert.equal(response.status(), 200);
    await check(button).toBeEnabled();
    if (failed) {
      await check(page.locator(".device-page .notice.warning")).toBeVisible();
      assert.ok(app.capabilities.error);
      assert.equal(app.capabilities.version, oldVersion);
      if (retain)
        await check(page.locator(".device-page")).toContainText(
          "继续使用此前成功启用的说明",
        );
    } else {
      await check.poll(() => app.capabilities.version).not.toBe(oldVersion);
      await check(page.locator(".device-page .notice.warning")).toHaveCount(0);
      assert.equal(app.capabilities.error, null);
    }
  }
  try {
    await inspect(initial);
    receipt.installed = true;
    await reload(changed, false);
    const deviceSource = { ...initial, "future-host": false };
    await inspect(deviceSource);
    receipt.changed_source = true;
    const invalid = current.replace(
      /"delete_supported"\s*:\s*false/,
      '"delete_supported":null',
    );
    assert.ok(invalid !== current, "非法字段夹具必须改变原 JSON");
    await reload(invalid, true, true);
    await inspect(deviceSource);
    receipt.invalid_reload = true;
    const duplicate = current.replace(
      '"devices":',
      '"devices": [], "devices":',
    );
    assert.ok(duplicate !== current, "重复键夹具必须改变原 JSON");
    await reload(duplicate, true, true);
    await inspect(deviceSource);
    receipt.duplicate_key_reload = true;
    await reload(current, false);
    await inspect(initial);
    receipt.restored_source = true;
    writeFileSync(path, invalid);
    await running.close();
    running = await start();
    assert.equal(running.app.capabilities.active, null);
    await running.page.getByTestId("nav-devices").click();
    await check(
      running.page.locator(".device-page .notice.warning"),
    ).toBeVisible();
    await inspect({});
    receipt.failed_startup = true;
    await reload('{"devices":[]}', false);
    await check(running.page.getByTestId("device-guide")).toContainText(
      "尚未配置设备",
    );
    await inspect({});
    receipt.empty_directory = true;
    assert.equal(saved(running.app), originalSaved);
    receipt.saved_inputs_unchanged = true;
    writeFileSync(receiptPath!, JSON.stringify(receipt));
  } catch (error) {
    note("failure", {
      message: String(error),
      version: running.app.capabilities.version,
      capabilityError: running.app.capabilities.error,
      activeRequests: [...running.active.values()],
    });
    try {
      writeFileSync(
        receiptPath! + ".failure.json",
        JSON.stringify(
          {
            url: running.page.url(),
            body: await running.page.locator("body").innerText(),
            closed: running.page.isClosed(),
            reloadDisabled: await running.page
              .getByRole("button", { name: "重新加载能力说明" })
              .isDisabled(),
          },
          null,
          2,
        ),
      );
      await running.page.screenshot({
        path: receiptPath! + ".failure.png",
        fullPage: true,
      });
    } catch (diagnosticError) {
      note("diagnostic-failure", { message: String(diagnosticError) });
    }
    throw error;
  } finally {
    await running.close();
    await browser.close();
  }
}
void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
