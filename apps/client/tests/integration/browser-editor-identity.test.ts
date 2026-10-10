import { beforeAll, afterAll, afterEach, expect, it } from "vitest";
import {
  chromium,
  expect as browserExpect,
  type Browser,
  type Page,
} from "@playwright/test";
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { resolve, join } from "node:path";
import type { Server } from "node:http";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import {
  initializePreviewMetadata,
  previewIntent,
} from "../../src/shared/automatic-previews";
import type { DraftContent } from "../../src/server/models";
import { choose } from "./select-support";

let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
});
afterAll(async () => {
  await browser?.close();
});
afterEach(async () => {
  for (const cleanup of clean.splice(0)) await cleanup();
});
const camera = (name: string) => ({
  name,
  type: "camera_record",
  device_id: "demo_cam0",
  scheduled_at: "2026-10-10 01:00:00",
  params: { type: "demo_fixed" },
  policy: { max_delay_ms: 0 },
});
const original = (): DraftContent => ({
  text: JSON.stringify({
    name: "身份检查",
    actions: [camera("A"), camera("B"), camera("C")],
  }),
});
const card = (page: Page, name: string) =>
  page.locator(".action-card").filter({
    has: page.locator("h3").filter({ hasText: new RegExp(`^\\d+${name}$`) }),
  });
async function mark(page: Page, name: string) {
  const button = card(page, name).getByRole("button", {
    name: "参数 JSON",
    exact: true,
  });
  if (await button.count()) await button.click();
  await card(page, name)
    .getByRole("button", { name: "收起动作", exact: true })
    .click();
}
async function retained(page: Page, name: string) {
  await browserExpect(
    card(page, name).getByRole("button", { name: "展开动作", exact: true }),
  ).toHaveAttribute("aria-expanded", "false");
  await card(page, name)
    .getByRole("button", { name: "展开动作", exact: true })
    .click();
  await browserExpect(
    card(page, name).getByRole("button", { name: "参数表单", exact: true }),
  ).toBeVisible();
}
async function setup(content = original()) {
  const scratch = resolve("../../.superpowers/sdd/2026-10-09-client-spec-sync");
  mkdirSync(scratch, { recursive: true });
  const directory = mkdtempSync(join(scratch, "task-5-fix1-browser-"));
  copyFileSync(
    "../../protocol/examples/capabilities/demo-device.json",
    join(directory, "device-capabilities.json"),
  );
  const application = new Application(directory),
    files = new Files(application),
    lifecycle = new RequestLifecycle();
  const server = await new Promise<Server>((resolveServer) => {
    const s = createHttpApp(application, files, lifecycle).listen(
      0,
      "127.0.0.1",
      () => resolveServer(s),
    );
  });
  const stop = createStop(
    () =>
      new Promise<void>((resolveStop, reject) =>
        server.close((e) => (e ? reject(e) : resolveStop())),
      ),
    lifecycle,
    () => files.idle(),
    () => application.store.close(),
  );
  const context = await browser.newContext(),
    page = await context.newPage();
  page.setDefaultTimeout(2500);
  clean.push(async () => {
    await context.close();
    await stop();
    rmSync(directory, { recursive: true, force: true });
  });
  await page.goto(
    `http://127.0.0.1:${(server.address() as { port: number }).port}`,
  );
  await page.getByTestId("initialize-button").click();
  const draft = application.createDraft(content);
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  return { application, page, draft };
}

it.each(["{", "[]", "null", "ambiguous"])(
  "类型恢复%s拒绝由Editor呈现，保留正文pending资料与组件状态",
  async (failure) => {
    const input = original();
    input.pending = {
      "/actions/0/params/count": { kind: "number", text: "1e" },
    };
    input.actionVariants = {
      "0": [
        {
          type: "report_status",
          fields: { params: { scope: "full" } },
          pending: {},
        },
      ],
    };
    if (failure === "ambiguous")
      input.actionVariants["0"].push({
        type: "report_status",
        fields: { params: { scope: "since", after_report_id: "1" } },
        pending: {},
      });
    const { page, application, draft } = await setup(input);
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    if (failure !== "ambiguous") {
      // 此 fault 注入只验证 Editor 接纳到损坏恢复原文后的拒绝呈现；服务器仍保存合法完整记录。
      await page.route("**/api/state", async (route) => {
        const response = await route.fetch(),
          state = await response.json();
        state.drafts.find(
          (item: { id: string }) => item.id === draft.id,
        ).content.actionVariants["0"][0].fieldsText = failure;
        await route.fulfill({ response, json: state });
      });
      await page.reload();
      await page.getByTestId("draft-open-button").click();
    }
    await mark(page, "B");
    await card(page, "A")
      .getByRole("button", { name: "参数 JSON", exact: true })
      .click();
    let writes = 0;
    await page.route("**/api/drafts/*", async (route) => {
      if (route.request().method() === "PUT") writes++;
      await route.continue();
    });
    const selectRejectedType = async () => {
      await card(page, "A").getByLabel("动作类型", { exact: true }).click();
      await page
        .getByRole("listbox")
        .locator('[role="option"][data-value="report_status"]')
        .click();
    };
    await selectRejectedType();
    await browserExpect(card(page, "A").getByRole("alert")).toBeVisible();
    await browserExpect(
      card(page, "A").getByRole("button", { name: "参数表单", exact: true }),
    ).toBeVisible();
    await browserExpect(
      card(page, "A").getByLabel("动作类型", { exact: true }),
    ).toHaveAttribute("data-value", "camera_record");
    await browserExpect(
      page.getByLabel("未完成输入 /actions/0/params/count"),
    ).toHaveValue("1e");
    await retained(page, "B");
    expect(application.draft(draft.id).content).toEqual(draft.content);
    expect(application.draft(draft.id).revision).toBe(1);
    expect(writes).toBe(0);
    expect(pageErrors).toEqual([]);
    // 同一候选仍存在，第二次选择仍准确拒绝。
    await selectRejectedType();
    await browserExpect(card(page, "A").getByRole("alert")).toBeVisible();
    expect(pageErrors).toEqual([]);
  },
);
it("真实类型选择保存直属原数字并恢复，其他动作局部状态保持", async () => {
  const input = original();
  input.text = input.text.replace(
    '"params":{"type":"demo_fixed"}',
    '"params":1e-999,"extra":1.0000000000000001',
  );
  const { page, application, draft } = await setup(input);
  await mark(page, "B");
  await choose(
    card(page, "A").getByLabel("动作类型", { exact: true }),
    "report_status",
  );
  await browserExpect
    .poll(
      () =>
        application.draft(draft.id).content.actionVariants?.["0"]?.[0]
          .fieldsText,
    )
    .toContain('"params":1e-999');
  expect(
    application.draft(draft.id).content.actionVariants!["0"][0].fieldsText,
  ).toContain('"extra":1.0000000000000001');
  await retained(page, "B");
  await mark(page, "B");
  await choose(
    card(page, "A").getByLabel("动作类型", { exact: true }),
    "camera_record",
  );
  await browserExpect
    .poll(() => application.draft(draft.id).content.text)
    .toContain('"params": 1e-999');
  expect(application.draft(draft.id).content.text).toContain(
    '"extra": 1.0000000000000001',
  );
  await retained(page, "B");
});

it.each([
  "missing",
  "reliable",
  "lost",
  "unknown",
  "retry",
  "capability-conflict",
])("共同同步追加到仍挂载的%s目标，保持原组件状态", async (scenario) => {
  const input =
    scenario === "reliable"
      ? initializePreviewMetadata(original(), "disabled", "stable")
      : original();
  const { page, application, draft } = await setup(input);
  let requests = 0,
    offline = false;
  await page.route("**/api/state", (route) =>
    offline ? route.abort() : route.continue(),
  );
  await page.route("**/api/drafts/*/actions", async (route) => {
    requests++;
    if (
      (scenario === "retry" || scenario === "capability-conflict") &&
      requests === 1
    ) {
      if (scenario === "capability-conflict") {
        application.reloadCapabilities();
        await route.continue();
      } else await route.fulfill({ status: 500, json: { error: "未提交" } });
    } else if (scenario === "lost" || scenario === "unknown") {
      await route.fetch();
      offline = scenario === "unknown";
      await route.abort();
    } else await route.continue();
  });
  await mark(page, "B");
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByLabel("目标草稿").click();
  await page
    .getByRole("listbox")
    .locator(`[role="option"][data-value="${draft.id}"]`)
    .click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  if (scenario === "unknown") {
    await browserExpect(
      page.getByRole("button", { name: "重新核实追加结果", exact: true }),
    ).toBeVisible();
    await browserExpect(
      card(page, "B").getByRole("button", {
        name: "展开动作",
        exact: true,
        includeHidden: true,
      }),
    ).toHaveAttribute("aria-expanded", "false");
    offline = false;
    await page
      .getByRole("button", { name: "重新核实追加结果", exact: true })
      .click();
  }
  if (scenario === "retry" || scenario === "capability-conflict") {
    await browserExpect(
      page.getByRole("button", { name: "重试同一目标追加", exact: true }),
    ).toBeVisible();
    expect(application.draft(draft.id).content).toEqual(input);
    await browserExpect(
      card(page, "B").getByRole("button", {
        name: "展开动作",
        exact: true,
        includeHidden: true,
      }),
    ).toHaveAttribute("aria-expanded", "false");
    if (scenario === "capability-conflict")
      await page.waitForResponse(
        async (response) =>
          response.url().endsWith("/api/state") &&
          (await response.json()).capabilities.version ===
            application.capabilities.version,
      );
    await page
      .getByRole("button", { name: "重试同一目标追加", exact: true })
      .click();
  }
  await browserExpect(page.getByRole("dialog")).toBeHidden();
  await browserExpect(page.locator(".action-card")).toHaveCount(4);
  await retained(page, "B");
  await browserExpect(
    card(page, "状态同步").getByRole("button", {
      name: "收起动作",
      exact: true,
    }),
  ).toHaveAttribute("aria-expanded", "true");
  await browserExpect(page.locator(".editor > fieldset")).toBeEnabled();
  const actual = application.draft(draft.id)!;
  expect(
    JSON.parse(actual.content.text).actions.map(
      (a: { name: string }) => a.name,
    ),
  ).toEqual(["A", "B", "C", "状态同步"]);
  if (scenario === "reliable") {
    expect(previewIntent(actual.content)).toBe("disabled");
    expect(actual.content.automaticPreviews!.actions.slice(0, 3)).toEqual(
      draft.content.automaticPreviews!.actions,
    );
  } else expect(actual.content.automaticPreviews).toBeUndefined();
  expect(requests).toBe(
    scenario === "retry" || scenario === "capability-conflict" ? 2 : 1,
  );
  await mark(page, "B");
  await page.waitForResponse((response) =>
    response.url().endsWith("/api/state"),
  );
  await page.waitForResponse((response) =>
    response.url().endsWith("/api/state"),
  );
  await retained(page, "B");
  await browserExpect(page.locator(".action-card")).toHaveCount(4);
});
it("共同追加的缺项资料准备拒绝，保留正文与原组件状态且没有追加POST", async () => {
  const input = initializePreviewMetadata(original(), "disabled", "partial");
  input.automaticPreviews!.actions.pop();
  const { page, application, draft } = await setup(input);
  let requests = 0;
  await page.route("**/api/drafts/*/actions", async (route) => {
    requests++;
    await route.continue();
  });
  await mark(page, "B");
  await page.getByRole("button", { name: "准备状态同步", exact: true }).click();
  await page.getByLabel("目标草稿").click();
  await page
    .getByRole("listbox")
    .locator(`[role="option"][data-value="${draft.id}"]`)
    .click();
  await page.getByRole("button", { name: "加入草稿", exact: true }).click();
  await browserExpect(
    page.getByRole("button", { name: "重试准备目标草稿", exact: true }),
  ).toBeVisible();
  await browserExpect(page.getByRole("dialog")).toContainText("身份");
  await page.getByRole("button", { name: "返回", exact: true }).click();
  await browserExpect(
    card(page, "B").getByRole("button", { name: "展开动作", exact: true }),
  ).toHaveAttribute("aria-expanded", "false");
  await browserExpect(
    card(page, "B").getByRole("button", {
      name: "参数表单",
      exact: true,
      includeHidden: true,
    }),
  ).toHaveCount(1);
  expect(application.draft(draft.id).content).toEqual(draft.content);
  expect(application.draft(draft.id).revision).toBe(1);
  expect(requests).toBe(0);
});
it.each(["missing", "partial"])(
  "删除前项保持原动作组件状态，%s资料不被补建",
  async (metadata) => {
    let input = original();
    if (metadata === "partial") {
      input = initializePreviewMetadata(input, "unset", "partial");
      input.automaticPreviews!.actions.pop();
    }
    const { application, page, draft } = await setup(input);
    const marked = metadata === "missing" ? "B" : "C";
    await mark(page, marked);
    await card(page, "A")
      .getByRole("button", { name: "删除动作", exact: true })
      .click();
    await retained(page, marked);
    await browserExpect(page.getByTestId("save-status")).toContainText(
      "已保存",
    );
    const saved = application.draft(draft.id).content;
    expect(previewIntent(saved)).toBe("unset");
    expect(saved.automaticPreviews).toEqual(
      metadata === "missing"
        ? undefined
        : {
            ...draft.content.automaticPreviews!,
            actions: [draft.content.automaticPreviews!.actions[1]],
          },
    );
    expect(JSON.parse(saved.text).actions).toEqual([camera("B"), camera("C")]);
  },
);
it.each(["add", "copy"])(
  "删除末项后%s的新动作默认展开且不继承局部JSON模式",
  async (operation) => {
    const { application, page, draft } = await setup();
    await mark(page, "B");
    await mark(page, "C");
    await card(page, "C")
      .getByRole("button", { name: "删除动作", exact: true })
      .click();
    if (operation === "copy")
      await card(page, "A")
        .getByRole("button", { name: "复制动作", exact: true })
        .click();
    else
      await page.getByRole("button", { name: "添加动作", exact: true }).click();
    await retained(page, "B");
    const fresh = page.locator(".action-card").last();
    await browserExpect(
      fresh.getByRole("button", { name: "收起动作", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
    if (operation === "copy")
      await browserExpect(
        fresh.getByRole("button", { name: "参数 JSON", exact: true }),
      ).toBeVisible();
    await browserExpect(page.getByTestId("save-status")).toContainText(
      "已保存",
    );
    expect(
      application.draft(draft.id).content.automaticPreviews,
    ).toBeUndefined();
  },
);
it.each(["开启", "关闭"])(
  "首次显式%s映射原组件状态且仅显式操作建立资料",
  async (choice) => {
    const { application, page, draft } = await setup();
    await mark(page, "B");
    expect(
      application.draft(draft.id).content.automaticPreviews,
    ).toBeUndefined();
    await page
      .getByRole("button", { name: `${choice}自动预览`, exact: true })
      .click();
    await retained(page, "B");
    await browserExpect(page.getByTestId("save-status")).toContainText(
      "已保存",
    );
    const saved = application.draft(draft.id).content;
    expect(previewIntent(saved)).toBe(
      choice === "开启" ? "enabled" : "disabled",
    );
    expect(JSON.parse(saved.text).actions).toEqual([
      camera("A"),
      camera("B"),
      camera("C"),
    ]);
  },
);
it.each(["开启", "关闭"])(
  "首次%s共同移除中间显式自动项时原动作保持状态",
  async (choice) => {
    const input = original();
    const plan = JSON.parse(input.text);
    plan.actions.splice(1, 0, {
      name: "自动A",
      type: "obtain_action_outputs",
      scheduled_at: "2026-10-10 01:00:00",
      params: {
        source: { action_name: "A" },
        filter: "preview",
        purpose: "auto_preview",
      },
    });
    input.text = JSON.stringify(plan);
    const { application, page, draft } = await setup(input);
    await mark(page, "B");
    await page
      .getByRole("button", { name: `${choice}自动预览`, exact: true })
      .click();
    await browserExpect(page.getByTestId("derived-preview")).toHaveCount(0);
    await retained(page, "B");
    await browserExpect(page.getByTestId("save-status")).toContainText(
      "已保存",
    );
    expect(
      JSON.parse(application.draft(draft.id).content.text).actions,
    ).toEqual([camera("A"), camera("B"), camera("C")]);
  },
);
it("无资料局部改名和轮询保持组件状态，复制整份草稿独立初始化", async () => {
  const { application, page, draft } = await setup();
  await mark(page, "B");
  await card(page, "A").getByLabel("动作名称", { exact: true }).fill("改名A");
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  await retained(page, "B");
  await mark(page, "B");
  await page.getByRole("button", { name: "复制草稿", exact: true }).click();
  await browserExpect(
    card(page, "B").getByRole("button", { name: "收起动作", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
  await browserExpect(
    card(page, "B").getByRole("button", { name: "参数 JSON", exact: true }),
  ).toBeVisible();
  expect(application.draft(draft.id).content.automaticPreviews).toBeUndefined();
  await page.getByTestId("draft-open-button").first().click();
  await browserExpect(
    card(page, "B").getByRole("button", { name: "参数 JSON", exact: true }),
  ).toBeVisible();
  expect(application.draft(draft.id).content.automaticPreviews).toBeUndefined();
});
it("查看整份JSON与取消替换保持组件状态，接受替换清除旧状态", async () => {
  const input = initializePreviewMetadata(original(), "disabled", "stable");
  const { application, page, draft } = await setup(input);
  await mark(page, "B");
  await page.getByTestId("draft-json-toggle").click();
  expect(await page.getByTestId("draft-json-input").inputValue()).toBe(
    draft.content.text,
  );
  page.once("dialog", (dialog) => dialog.dismiss());
  await page
    .getByTestId("draft-json-input")
    .fill(original().text.replace("身份检查", "新结构"));
  await page.getByTestId("draft-json-toggle").click();
  await retained(page, "B");
  expect(application.draft(draft.id).content).toEqual(draft.content);
  await mark(page, "B");
  await page.getByTestId("draft-json-toggle").click();
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByTestId("draft-json-input")
    .fill(original().text.replace("身份检查", "新结构"));
  await page.getByTestId("draft-json-toggle").click();
  await browserExpect(
    card(page, "B").getByRole("button", { name: "参数 JSON", exact: true }),
  ).toBeVisible();
  await browserExpect(
    card(page, "B").getByRole("button", { name: "收起动作", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  expect(application.draft(draft.id).content.automaticPreviews).toBeUndefined();
});
it("无资料整份替换重置组件，未完成正文后接受新结构不继承旧状态", async () => {
  const { application, page, draft } = await setup();
  await mark(page, "B");
  await page.getByTestId("draft-json-toggle").click();
  await page.getByTestId("draft-json-input").fill('{"name":');
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  expect(application.draft(draft.id).content.text).toBe('{"name":');
  expect(application.draft(draft.id).content.automaticPreviews).toBeUndefined();
  await page.getByTestId("draft-json-input").fill(original().text);
  await page.getByTestId("draft-json-toggle").click();
  await browserExpect(
    card(page, "B").getByRole("button", { name: "收起动作", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
  await browserExpect(
    card(page, "B").getByRole("button", { name: "参数 JSON", exact: true }),
  ).toBeVisible();
  await card(page, "C")
    .getByRole("button", { name: "删除动作", exact: true })
    .click();
  await card(page, "A")
    .getByRole("button", { name: "复制动作", exact: true })
    .click();
  await browserExpect(
    page
      .locator(".action-card")
      .last()
      .getByRole("button", { name: "收起动作", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
});
it("整数组取消保全部组件状态，确认同数替换unset且集合外pending重开保留", async () => {
  const input = initializePreviewMetadata(original(), "disabled", "stable");
  input.actionVariants = {
    "1": [
      {
        type: "report_status",
        fields: { params: { scope: "full" } },
        pending: {},
      },
    ],
  };
  input.pending = {
    "/actions": {
      kind: "json",
      text: JSON.stringify([camera("A"), camera("B"), camera("C")]),
    },
    "/actions/1/group": { kind: "json", text: '"未完成' },
    "/name": { kind: "json", text: '"集合外' },
  };
  const { page, application, draft } = await setup(input);
  await mark(page, "B");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page
    .getByRole("button", { name: "应用修正 /actions", exact: true })
    .click();
  await retained(page, "B");
  expect(application.draft(draft.id).content).toEqual(draft.content);
  await mark(page, "B");
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "应用修正 /actions", exact: true })
    .click();
  await browserExpect(
    card(page, "B").getByRole("button", { name: "收起动作", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
  await browserExpect(
    card(page, "B").getByRole("button", { name: "参数 JSON", exact: true }),
  ).toBeVisible();
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  const saved = application.draft(draft.id).content;
  expect(saved.automaticPreviews).toBeUndefined();
  expect(saved.actionVariants).toBeUndefined();
  expect(saved.pending).toEqual({ "/name": input.pending["/name"] });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await browserExpect(
    page.getByLabel("未完成输入 /name", { exact: true }),
  ).toHaveValue('"集合外');
  await browserExpect(page.getByTestId("preview-intent")).toContainText(
    "尚未设置",
  );
});
it("整actions字段移除先确认，取消保原值，确认不遗留资料且不补空数组", async () => {
  const input = initializePreviewMetadata(original(), "enabled", "stable");
  input.pending = {
    "/actions": { kind: "json", text: "[" },
    "/name": { kind: "json", text: '"保留' },
  };
  const { page, application, draft } = await setup(input);
  page.once("dialog", (dialog) => dialog.dismiss());
  await page
    .getByRole("button", { name: "放弃输入 /actions", exact: true })
    .click();
  expect(application.draft(draft.id).content).toEqual(draft.content);
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "放弃输入 /actions", exact: true })
    .click();
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  const saved = application.draft(draft.id).content;
  expect(Object.hasOwn(JSON.parse(saved.text), "actions")).toBe(false);
  expect(saved.automaticPreviews).toBeUndefined();
  expect(saved.pending).toEqual({ "/name": input.pending["/name"] });
  await page.getByTestId("draft-json-toggle").click();
  expect(
    Object.hasOwn(
      JSON.parse(await page.getByTestId("draft-json-input").inputValue()),
      "actions",
    ),
  ).toBe(false);
});
it.each(["null", "["])(
  "整数组非法候选%s保留已保存输入且不弹确认",
  async (text) => {
    const input = initializePreviewMetadata(original(), "disabled", "stable");
    input.pending = { "/actions": { kind: "json", text } };
    const { page, application, draft } = await setup(input);
    let dialogs = 0;
    page.on("dialog", async (dialog) => {
      dialogs++;
      await dialog.accept();
    });
    await page
      .getByRole("button", { name: "应用修正 /actions", exact: true })
      .click();
    await browserExpect(page.getByRole("alert")).toBeVisible();
    expect(dialogs).toBe(0);
    await browserExpect(
      page.getByLabel("未完成输入 /actions", { exact: true }),
    ).toHaveValue(text);
    expect(application.draft(draft.id).content).toEqual(draft.content);
  },
);
it("确认业务非法动作数组仍保存原数字和字符串，完整导出拒绝", async () => {
  const input = initializePreviewMetadata(original(), "enabled", "stable");
  input.pending = {
    "/actions": {
      kind: "json",
      text: '[{"name":"\\ud800","type":"motor_control","params":{"position":1.0000000000000001}}]',
    },
  };
  const { page, application, draft } = await setup(input);
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "应用修正 /actions", exact: true })
    .click();
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  const saved = application.draft(draft.id).content;
  expect(saved.text).toContain("1.0000000000000001");
  expect(saved.text).toContain("\\ud800");
  expect(saved.automaticPreviews).toBeUndefined();
  expect(saved.pending).toEqual({});
  await browserExpect(page.getByTestId("preview-intent")).toContainText(
    "尚未设置",
  );
  await page.getByTestId("export-button").click();
  await browserExpect(page.getByRole("alert").first()).toBeVisible();
  expect(application.store.all("requests")).toEqual([]);
  expect(application.draft(draft.id).content).toEqual(saved);
});
it("可靠身份删除来源和自动项后保持后方普通组件，能力派生增删也保持", async () => {
  const input = initializePreviewMetadata(original(), "enabled", "stable");
  const { application, page, draft } = await setup(input);
  const k = application.capabilities.active!;
  k.devices[0].actions
    .find((a) => a.type === "camera_record")!
    .parameter_types.find((p) => p.type === "demo_fixed")!.preview_supported =
    true;
  writeFileSync(
    join(application.store.directory, "device-capabilities.json"),
    JSON.stringify(k),
  );
  application.reloadCapabilities();
  await browserExpect(page.getByTestId("derived-preview")).toHaveCount(3);
  await mark(page, "B");
  await card(page, "A")
    .getByRole("button", { name: "删除动作", exact: true })
    .click();
  await browserExpect(page.getByTestId("derived-preview")).toHaveCount(2);
  await retained(page, "B");
  await mark(page, "B");
  await browserExpect(page.getByTestId("save-status")).toContainText("已保存");
  k.devices[0].actions
    .find((a) => a.type === "camera_record")!
    .parameter_types.find((p) => p.type === "demo_fixed")!.preview_supported =
    false;
  writeFileSync(
    join(application.store.directory, "device-capabilities.json"),
    JSON.stringify(k),
  );
  application.reloadCapabilities();
  await browserExpect(page.getByTestId("derived-preview")).toHaveCount(0);
  await retained(page, "B");
  expect(previewIntent(application.draft(draft.id).content)).toBe("enabled");
});
