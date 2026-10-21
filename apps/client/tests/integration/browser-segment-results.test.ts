import { beforeAll, afterAll, beforeEach, afterEach, expect, it } from "vitest";
import {
  chromium,
  expect as check,
  type Browser,
  type Page,
} from "@playwright/test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Server } from "node:http";
import { Readable } from "node:stream";
import { createHash } from "node:crypto";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { parseJson, stringifyJson } from "../../src/shared/json";
import type { StatusReport } from "../../src/shared/types";
import {
  segmentOutput,
  segmentReport,
  scopeReport,
} from "../helpers/segment-reports";

let browser: Browser,
  page: Page,
  app: Application,
  files: Files,
  server: Server,
  directory: string,
  base: string;
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
});
afterAll(async () => {
  await browser.close();
});
async function serve(port = 0) {
  server = createHttpApp(app, files).listen(port, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
}
beforeEach(async () => {
  directory = mkdtempSync(join(tmpdir(), "camctl-web-segments-"));
  app = new Application(directory);
  app.store.initialize();
  files = new Files(app);
  await serve();
  page = await browser.newPage();
  page.setDefaultTimeout(5000);
  await page.goto(base);
});
afterEach(async () => {
  await page.close();
  await files.idle();
  await new Promise<void>((resolve) => server.close(() => resolve()));
  app.store.close();
  rmSync(directory, { recursive: true, force: true });
});
async function uploadReport(r: StatusReport) {
  const bytes = Buffer.from(stringifyJson(r));
  const fileName = `status-report-${r.report_id}-${createHash("sha256").update(bytes).digest("hex")}.json`;
  const f = files.createBatch([
    { fileName, size: bytes.length, kind: "report" },
  ]).files[0];
  await files.upload(f.id, Readable.from([bytes]));
  await files.idle();
  expect(app.coverage()).toBe(r.to_wm);
}
async function view() {
  await page.reload();
  await page.getByTestId("tab-records").click();
  await page.getByTestId("record-open-button").first().click();
  const card = page.locator('[data-action-id="10"]');
  await card
    .getByRole("button", { name: "展开动作 分段录像", exact: true })
    .click();
  return card;
}

it("来源缺段、成功前缀取回、本地缺片与错误副本分别展示", async () => {
  const r = segmentReport();
  const source = r.plans![0].actions![0];
  const bytes = Buffer.from("verified original segment bytes"),
    sha256 = createHash("sha256").update(bytes).digest("hex");
  for (const o of source.outputs!) {
    o.size = bytes.length;
    o.checksum = { status: "available", sha256 };
  }
  r.plans![0].actions!.push({
    action_instance_id: "20",
    name: "成功前缀取回",
    type: "obtain_action_outputs",
    status: "succeeded",
    scheduled_at: "2026-01-01 00:00:00",
    input_params: { source: { action_instance_id: "10" } },
    result: { failures: [] },
    deliveries: ["2", "10"].map((id, i) => ({
      delivery_id: String(100 + i),
      output_id: id,
      source_action_instance_id: "10",
      file_name: `${100 + i}.OSV`,
      display_name: `原片${id}`,
      status: "published",
      size: bytes.length,
      sha256,
    })),
  });
  await uploadReport(r);
  const f = files.createBatch([
    { fileName: "100.OSV", size: bytes.length, kind: "media" },
  ]).files[0];
  await files.upload(f.id, Readable.from([bytes]));
  await files.idle();
  let card = await view();
  await check(card.locator(".action-summary")).toContainText("CAM1/3");
  await check(card.locator(".action-summary")).toContainText("observed-left");
  await check(card.locator(".action-summary")).toContainText("observed-right");
  await check(card.locator(".action-summary")).toContainText("1 / 4");
  await check(card.getByText("失败", { exact: true })).toBeVisible();
  const obtain = page.locator('[data-action-id="20"]');
  await check(obtain.getByText("成功", { exact: true })).toBeVisible();
  await check(obtain.locator(".action-summary")).toContainText("1 / 2");
  expect(app.ackId()).toBe("1");
  await card.getByText("执行技术详情", { exact: true }).click();
  const technical = card.locator(":scope > div > details.advanced");
  await check(technical).toContainText("retained_output_ids");
  await check(technical).toContainText("first_gap");
  const bad = Buffer.from("mismatch");
  const second = files.createBatch([
    { fileName: "101.OSV", size: bad.length, kind: "media" },
  ]).files[0];
  await files.upload(second.id, Readable.from([bad]));
  await files.idle();
  card = await view();
  await check(card.locator(".action-summary")).toContainText(
    "1 次交付或文件核验异常",
  );
  expect(app.ackId()).toBe("1");
  expect(app.snapshot().plans![0].actions!.map((a) => a.status)).toEqual([
    "failed",
    "succeeded",
  ]);
  await card
    .getByRole("button", { name: "准备取回默认产物", exact: true })
    .click();
  await check(page.getByText(/已保存的默认范围取回/)).toBeVisible();
});

it.each(["empty", "unknown"] as const)(
  "来源级%s范围失败可定位且保留原片",
  async (state) => {
    await uploadReport(scopeReport(state));
    await view();
    const card = page.locator('[data-action-id="20"]');
    await check(card.locator(".action-summary")).toContainText("来源动作 10");
    await check(card.locator(".action-summary")).toContainText(
      "已保留 4 份原片",
    );
    await check(card.locator(".action-summary")).toContainText(
      state === "empty" ? "默认范围为空" : "默认范围未确认",
    );
    if (state === "unknown")
      await check(card.locator(".action-summary")).toContainText(
        "缺口分类未确认",
      );
    expect(app.ackId()).toBe("1");
  },
);

it("大目录号经真实HTTP与React的摘要和技术详情及后端重启保持精确", async () => {
  const r = parseJson(
    stringifyJson(segmentReport()).replaceAll(
      '"directory_number":1',
      '"directory_number":9007199254740993',
    ),
  ) as StatusReport;
  await uploadReport(r);
  for (let round = 0; round < 2; round++) {
    const card = await view();
    await check(card.locator(".action-summary")).toContainText(
      "CAM9007199254740993/3",
    );
    await card.getByText("执行技术详情", { exact: true }).click();
    const technical = card.locator(":scope > div > details.advanced");
    await check(technical).toContainText("9007199254740993");
    await check(technical).not.toContainText("9007199254740992");
    if (round === 0) {
      const port = (server.address() as { port: number }).port;
      await files.idle();
      await new Promise<void>((resolve) => server.close(() => resolve()));
      app.store.close();
      app = new Application(directory);
      files = new Files(app);
      await files.recover();
      await files.idle();
      await serve(port);
    }
  }
});

it("13份分段产物按已保存ID顺序分页，名称反序且增量不重排", async () => {
  const r = segmentReport();
  const source = r.plans![0].actions![0];
  const ids = [
    "2",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "20",
    "21",
  ];
  source.outputs = ids.map((id, i) => ({
    ...segmentOutput(id),
    original_name: `output${id}-CAM_${String(13 - i).padStart(4, "0")}.OSV`,
  }));
  const d = source.error!.details as any;
  d.retained_original_count = 13;
  d.retained_output_ids = ids;
  await uploadReport(r);
  const card = await view();
  await check(card.locator(".product-card .product-name strong")).toHaveText(
    ids
      .slice(0, 12)
      .map((id, i) => `output${id}-CAM_${String(13 - i).padStart(4, "0")}.OSV`),
  );
  await card.getByRole("button", { name: "视频下一页", exact: true }).click();
  await check(card.locator(".product-card .product-name strong")).toHaveText([
    "output21-CAM_0001.OSV",
  ]);
  const delta: StatusReport = {
    report_id: "2",
    from_wm: 10,
    to_wm: 20,
    plans: [{ ...r.plans![0], actions: undefined }],
  };
  await uploadReport(delta);
  await check(page.locator(".workspace-footer")).toContainText("20");
  await check(card.locator(".product-card .product-name strong")).toHaveText([
    "output21-CAM_0001.OSV",
  ]);
  const reopened = await view();
  await reopened
    .getByRole("button", { name: "视频下一页", exact: true })
    .click();
  await check(
    reopened.locator(".product-card .product-name strong"),
  ).toHaveText(["output21-CAM_0001.OSV"]);
});
