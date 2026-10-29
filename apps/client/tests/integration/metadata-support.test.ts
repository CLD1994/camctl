import { afterAll, beforeAll, expect, it } from "vitest";
import { chromium, type Browser, type Page } from "@playwright/test";
import {
  checkVisibleCleanupSupport,
  checkVisibleFields,
  checkVisibleGuide,
} from "./metadata-support";

let browser: Browser;
let page: Page;
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage();
});
afterAll(async () => browser.close());
const field = {
  label: "模式 (mode)",
  object: "exposure",
  description: "字段说明",
};
const form = (control = "", description = "") => `
  <section class="object-branch-field"><h5><small class="field-key">exposure</small></h5>
  <p class="muted" ${description}>对象说明</p>
  <label class="field">字段说明<input aria-label="模式 (mode)" ${control}></label></section>`;
const guide = (device: string, description: string, hidden = "") => `
  <section class="guide-device"><div class="guide-device-heading"><h2>${device}</h2></div>
  <article class="guide-task"><h3>照片</h3><p class="guide-description">${description}</p>
  <dl class="guide-fields"><div><dt>曝光</dt><dd ${hidden}>曝光说明</dd></div></dl></article></section>`;
const task = (device: string, description: string) => ({
  device,
  name: "照片",
  description,
  fields: [{ title: "曝光", description: "曝光说明" }],
});

it("批量读取保留控件、对象说明和同名任务的设备归属", async () => {
  await page.setContent(
    form() + guide("host", "主机任务") + guide("device", "设备任务"),
  );
  await checkVisibleFields(
    page,
    [field],
    [{ name: "exposure", description: "对象说明" }],
  );
  await checkVisibleGuide(page, [
    task("host", "主机任务"),
    task("device", "设备任务"),
  ]);
});

it("隐藏控件不能满足可见控件检查", async () => {
  await page.setContent(form('style="display:none"'));
  await expect(checkVisibleFields(page, [field], [])).rejects.toThrow();
});

it("隐藏对象说明不能满足可见说明检查", async () => {
  await page.setContent(form("", 'style="display:none"'));
  await expect(
    checkVisibleFields(
      page,
      [field],
      [{ name: "exposure", description: "对象说明" }],
    ),
  ).rejects.toThrow();
});

it("同名任务不能借用另一个设备的说明", async () => {
  await page.setContent(
    guide("host", "主机任务") + guide("device", "设备任务"),
  );
  await expect(
    checkVisibleGuide(page, [task("host", "设备任务")]),
  ).rejects.toThrow();
});

it("隐藏字段说明不能满足指南检查", async () => {
  await page.setContent(guide("host", "主机任务", 'style="display:none"'));
  await expect(
    checkVisibleGuide(page, [task("host", "主机任务")]),
  ).rejects.toThrow();
});

it.each([
  ["说明不同", () => form().replace("字段说明", "错误说明")],
  ["对象不同", () => form().replace("exposure", "other")],
  ["控件隐藏", () => form('style="display:none"')],
])("同 label 的重复控件不能因%s被忽略", async (_, duplicate) => {
  await page.setContent(form() + duplicate());
  await expect(checkVisibleFields(page, [field], [])).rejects.toThrow();
});

it.each(["", 'style="display:none"'])(
  "同任务的错误重复说明不能被忽略（%s）",
  async (hidden) => {
    await page.setContent(
      guide("host", "主机任务").replace(
        "</p>",
        `</p><p class="guide-description" ${hidden}>错误说明</p>`,
      ),
    );
    await expect(
      checkVisibleGuide(page, [task("host", "主机任务")]),
    ).rejects.toThrow();
  },
);

it("删除声明保留设备归属与可见正文", async () => {
  await page.setContent(`<section class="guide-device"><div class="guide-device-heading"><h2>host</h2></div>
    <p class="guide-cleanup-support"><strong>支持删除</strong></p></section>`);
  await checkVisibleCleanupSupport(page, [
    { device: "host", text: "支持删除" },
  ]);
});

it.each(["", 'style="display:none"'])(
  "同设备的错误重复删除声明不能被忽略（%s）",
  async (hidden) => {
    await page.setContent(`<section class="guide-device"><div class="guide-device-heading"><h2>host</h2></div>
      <p class="guide-cleanup-support"><strong>支持删除</strong><strong ${hidden}>错误声明</strong></p></section>`);
    await expect(
      checkVisibleCleanupSupport(page, [{ device: "host", text: "支持删除" }]),
    ).rejects.toThrow();
  },
);

it.each(["", 'style="display:none"'])(
  "同标题的错误重复字段不能被忽略（%s）",
  async (hidden) => {
    await page.setContent(
      guide("host", "主机任务").replace(
        "</dl>",
        `<div ${hidden}><dt>曝光</dt><dd>错误说明</dd></div></dl>`,
      ),
    );
    await expect(
      checkVisibleGuide(page, [task("host", "主机任务")]),
    ).rejects.toThrow();
  },
);
