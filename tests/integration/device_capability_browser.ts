// 安装版 describe 与真实页面的联合凭据；测试报告不表示真实设备产物。
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
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
    "hb-device": false,
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
      outputs: (device === "hb-device"
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
    await new Promise<void>((resolve, reject) => {
      server.once("listening", resolve);
      server.once("error", reject);
    });
    const context = await browser.newContext();
    const page = await context.newPage();
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
    await page.goto(
      `http://127.0.0.1:${(server.address() as { port: number }).port}`,
    );
    return {
      app,
      page,
      close: async () => {
        await context.close();
        await stop();
      },
    };
  }

  const receipt: Record<string, boolean> = {};
  let running = await start();
  async function inspect(expected: Record<string, boolean | undefined>) {
    const { page, app } = running;
    await page.getByTestId("nav-devices").click();
    if (Object.keys(expected).length > 0) {
      await check(page.locator(".guide-device")).toHaveCount(devices.length);
      for (const device of devices) {
        const guide = page.locator(".guide-device").filter({
          has: page.getByRole("heading", { name: device, exact: true }),
        });
        await check(guide.locator(".guide-cleanup-support strong")).toHaveText(
          expected[device] === false ? "不支持删除" : "支持删除",
        );
      }
    }
    await page.getByTestId("nav-plans").click();
    await page.getByTestId("tab-records").click();
    await page
      .getByTestId("record-open-button")
      .filter({ hasText: "安装版来源记录" })
      .click();
    await check(
      page.getByRole("button", { name: "准备清理计划全部源产物" }),
    ).toBeVisible();
    for (const [index, device] of devices.entries()) {
      const capture = page.locator(
        `.result-card[data-action-id="${10 + index}"]`,
      );
      await capture
        .getByRole("button", { name: `展开动作 ${device}`, exact: true })
        .click();
      await check(
        capture.getByRole("button", {
          name: "准备清理动作全部源产物",
          exact: true,
        }),
      ).toHaveCount(expected[device] === false ? 0 : 1);
    }
    const retrieve = page.locator('.result-card[data-action-id="2"]');
    await retrieve
      .getByRole("button", { name: "展开动作 混合来源取回" })
      .click();
    let productIndex = 0;
    for (const device of devices) {
      const kinds = device === "hb-device" ? 3 : 1;
      for (let offset = 0; offset < kinds; offset++) {
        const product = retrieve.locator(".product-card").nth(productIndex++);
        await product.locator("details.advanced > summary").click();
        await check(
          product.getByRole("button", { name: "准备清理源产物", exact: true }),
        ).toHaveCount(offset === 2 || expected[device] !== false ? 1 : 0);
        await check(
          product.locator("details.advanced > .button-row .badge"),
        ).toHaveText("可用");
      }
    }
    assert.equal(saved(app), originalSaved);
  }
  async function reload(text: string, failed: boolean, retain = false) {
    const { page, app } = running;
    writeFileSync(path, text);
    const oldVersion = app.capabilities.version;
    await page.getByTestId("nav-devices").click();
    const response = page.waitForResponse((response: { url(): string }) =>
      response.url().endsWith("/api/capabilities/reload"),
    );
    await page.getByRole("button", { name: "重新加载能力说明" }).click();
    assert.equal((await response).status(), 200);
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
    const deviceSource = { ...initial, "hb-host": false };
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
  } finally {
    await running.close();
    await browser.close();
  }
}
void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
