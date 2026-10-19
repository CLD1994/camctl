import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import { parseJson, stringifyJson } from "../../src/shared/json";
import { choose, readOptions } from "./select-support";
import {
  clientTestCapabilitiesText,
  clientTestTasks,
} from "./capability-fixtures";

let browser: Browser;
const clean: Array<() => Promise<void>> = [];
beforeAll(async () => {
  browser = await chromium.launch({ headless: true });
});
afterAll(async () => {
  await browser.close();
});
afterEach(async () => {
  for (const close of clean.splice(0)) await close();
});

const capabilityText =
  '{"devices":[{"device_id":"action6","driver_id":"dji-action6","actions":[{"type":"camera_record","parameter_types":[{"type":"action6_record","name":"普通录像参数","description":"软件参数编辑测试","preview_supported":false,"schema":{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,"properties":{"type":{"const":"action6_record"},"duration_s":{"type":"integer","minimum":1,"maximum":9223372036854775},"resolution":{"enum":["8k30","4k30"]},"fov":{"enum":["wide","natural_wide","standard"]},"stabilization":{"enum":["off","rocksteady"]},"aperture":{"enum":["f2.8","f4.0"]},"bitrate":{"enum":["standard","high"]},"exposure":{"oneOf":[{"type":"object","properties":{"mode":{"const":"manual"},"iso":{"enum":[100,800]}},"required":["mode","iso"],"additionalProperties":false},{"type":"object","properties":{"mode":{"const":"auto"},"compensation_ev":{"enum":[-0.7,0,0.7]}},"required":["mode","compensation_ev"],"additionalProperties":false}]}},"required":["type","duration_s","resolution","fov","stabilization","exposure"]}}]}]}]}';

async function setup(
  duration = "10",
  incompleteExposure = false,
  exposure: {
    value?: string;
    omit?: boolean;
    pending?: string;
    childPending?: string;
    capabilities?: string;
    draftText?: string;
  } = {},
) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-camera-fields-"));
  writeFileSync(
    join(directory, "device-capabilities.json"),
    exposure.capabilities ?? capabilityText,
  );
  const app = new Application(directory),
    files = new Files(app),
    requests = new RequestLifecycle();
  app.store.initialize();
  const draft = app.createDraft({
    text:
      exposure.draftText ??
      `{"name":"参数编辑","actions":[{"name":"录像","type":"camera_record","device_id":"action6","scheduled_at":"2026-10-11 12:00:00","policy":{"max_delay_ms":0},"params":{"type":"action6_record","duration_s":${duration},"resolution":"4k30","fov":"wide","stabilization":"off"${exposure.omit ? "" : `,"exposure":${exposure.value ?? '{"mode":"manual","iso":800}'}`}}}]}`,
    ...(incompleteExposure ||
    exposure.pending !== undefined ||
    exposure.childPending !== undefined
      ? {
          pending: {
            [exposure.childPending === undefined
              ? "/actions/0/params/exposure"
              : "/actions/0/params/exposure/iso"]: {
              kind: "json",
              text: exposure.childPending ?? exposure.pending ?? '{"mode":',
            },
          },
        }
      : {}),
  });
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const context = await browser.newContext({ timezoneId: "Asia/Shanghai" });
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
  clean.push(async () => {
    await context.close();
    await stop();
    rmSync(directory, { recursive: true, force: true });
  });
  await page.goto(
    `http://127.0.0.1:${(server.address() as { port: number }).port}`,
  );
  await page.getByTestId("draft-open-button").click();
  return { app, page, draft };
}

it("曝光缺省时先选择模式，浏览页面不自动填写任何选项", async () => {
  const { app, page, draft } = await setup("10", false, { omit: true });
  const before = app.draft(draft.id);
  const mode = page.getByRole("combobox", { name: "mode (mode)", exact: true });
  await check(mode).toBeVisible();
  await check(mode).toHaveAttribute("data-value", "");
  await check(
    page.getByRole("combobox", { name: "iso (iso)", exact: true }),
  ).toHaveCount(0);
  await check(
    page.getByRole("combobox", {
      name: "compensation_ev (compensation_ev)",
      exact: true,
    }),
  ).toHaveCount(0);
  await readOptions(mode);
  expect(app.draft(draft.id)).toEqual(before);
});

it("普通录像独立参数与曝光级联回显并保持可选字段缺省", async () => {
  const { app, page, draft } = await setup();
  for (const [field, values] of [
    ["resolution", ["8k30", "4k30"]],
    ["fov", ["wide", "natural_wide", "standard"]],
    ["stabilization", ["off", "rocksteady"]],
    ["aperture", ["f2.8", "f4.0"]],
    ["bitrate", ["standard", "high"]],
  ] as const) {
    const control = page.getByRole("combobox", {
      name: `${field} (${field})`,
      exact: true,
    });
    await check(control).toBeVisible();
    const options = await readOptions(control);
    expect(options.map((option) => option.text)).toEqual(
      expect.arrayContaining([...values]),
    );
  }
  await check(
    page.getByLabel("duration_s (duration_s)", { exact: true }),
  ).toHaveValue("10");
  await check(
    page.getByRole("combobox", { name: "mode (mode)", exact: true }),
  ).toHaveAttribute("data-value", "branch:0");
  await check(
    page.getByRole("combobox", { name: "iso (iso)", exact: true }),
  ).toContainText("800");
  await check(
    page.getByRole("combobox", {
      name: "compensation_ev (compensation_ev)",
      exact: true,
    }),
  ).toHaveCount(0);
  const params = (parseJson(app.draft(draft.id).content.text) as any).actions[0]
    .params;
  expect(params).not.toHaveProperty("aperture");
  expect(params).not.toHaveProperty("bitrate");
  const saved = app.draft(draft.id);
  const request = app.exportDraft(draft.id, saved.revision, saved.content);
  expect(stringifyJson(request.body)).toContain('"duration_s":10');
});

it.each([false, true])(
  "级联说明按当前分支显示且浏览不写入参数，含成员：%s",
  async (members) => {
    const branches = [
      {
        value: "fixed",
        title: "定量选择",
        description: "固定分支的字段说明 <&>",
        branchTitle: "定量分支",
        branchDescription: "定量分支整体说明",
      },
      {
        value: "adaptive",
        title: "自适应选择",
        description: "自动分支的字段说明",
        branchTitle: "自适应分支",
        branchDescription: "自适应分支整体说明",
      },
    ];
    const object = {
      title: "采集配置",
      description: "此对象的公共说明 <&>",
      oneOf: branches.map((branch) => ({
        title: branch.branchTitle,
        description: branch.branchDescription,
        type: "object",
        additionalProperties: false,
        properties: {
          kind: {
            const: branch.value,
            title: branch.title,
            description: branch.description,
          },
          ...(members ? { level: { title: "档位", enum: [1, 2] } } : {}),
        },
        required: ["kind", ...(members ? ["level"] : [])],
      })),
    };
    const capabilities = {
      devices: [
        {
          device_id: "metadata-device",
          driver_id: "metadata-driver",
          actions: [
            {
              type: "camera_record",
              parameter_types: [
                {
                  type: "metadata-record",
                  name: "说明展示任务",
                  description: "普通级联说明验证",
                  preview_supported: false,
                  schema: {
                    $schema: "https://json-schema.org/draft/2020-12/schema",
                    type: "object",
                    additionalProperties: false,
                    properties: {
                      type: { const: "metadata-record" },
                      duration_s: { type: "integer" },
                      profile: object,
                    },
                    required: ["type", "duration_s", "profile"],
                  },
                },
              ],
            },
          ],
        },
      ],
    };
    const { app, page, draft } = await setup("10", false, {
      capabilities: JSON.stringify(capabilities),
      draftText: JSON.stringify({
        name: "说明展示",
        actions: [
          {
            name: "拍摄",
            type: "camera_record",
            device_id: "metadata-device",
            scheduled_at: "2026-10-11 12:00:00",
            policy: { max_delay_ms: 0 },
            params: { type: "metadata-record", duration_s: 10 },
          },
        ],
      }),
    });
    const section = page.locator(".object-branch-field");
    const mode = section.getByRole("combobox", { name: /\(kind\)$/ });
    const current = () =>
      (parseJson(app.draft(draft.id).content.text) as any).actions[0].params;
    const before = app.draft(draft.id);
    await check(
      section.getByText(object.description, { exact: true }),
    ).toBeVisible();
    await check(mode).toHaveAccessibleName("kind (kind)");
    for (const branch of branches) {
      await check(
        section.getByText(branch.description, { exact: true }),
      ).toHaveCount(0);
      await check(
        section.getByText(branch.branchDescription, { exact: true }),
      ).toHaveCount(0);
    }
    await readOptions(mode);
    expect(app.draft(draft.id)).toEqual(before);
    for (const [index, branch] of branches.entries()) {
      await choose(mode, `branch:${index}`);
      await check.poll(() => current().profile).toEqual({ kind: branch.value });
      await check(mode).toHaveAccessibleName(`${branch.title} (kind)`);
      await check(
        section.getByText(branch.description, { exact: true }),
      ).toBeVisible();
      await check(
        section.getByRole("heading", { name: branch.branchTitle, exact: true }),
      ).toBeVisible();
      await check(
        section.getByText(branch.branchDescription, { exact: true }),
      ).toBeVisible();
      const other = branches[1 - index];
      await check(
        section.getByText(other.description, { exact: true }),
      ).toHaveCount(0);
      await check(
        section.getByText(other.branchDescription, { exact: true }),
      ).toHaveCount(0);
      if (members) {
        await choose(
          section.getByRole("combobox", { name: "档位 (level)", exact: true }),
          String(index),
        );
        await check.poll(() => current().profile.level).toBe(index + 1);
      }
    }
    const saved = app.draft(draft.id);
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    await check(mode).toHaveAccessibleName("自适应选择 (kind)");
    await check(
      section.getByText(branches[1].description, { exact: true }),
    ).toBeVisible();
    expect(app.draft(draft.id)).toEqual(saved);
    expect(current()).toEqual({
      type: "metadata-record",
      duration_s: 10,
      profile: { kind: "adaptive", ...(members ? { level: 2 } : {}) },
    });
    object.oneOf[1].properties.kind.title = "更新后的模式标题";
    object.oneOf[1].properties.kind.description = "更新后的模式说明";
    object.oneOf[1].description = "更新后的分支说明";
    writeFileSync(
      join(app.store.directory, "device-capabilities.json"),
      JSON.stringify(capabilities),
    );
    expect(app.reloadCapabilities().error).toBeNull();
    await page.reload();
    await page.getByTestId("draft-open-button").click();
    await check(mode).toHaveAccessibleName("更新后的模式标题 (kind)");
    await check(
      section.getByText("更新后的模式说明", { exact: true }),
    ).toBeVisible();
    await check(
      section.getByText("更新后的分支说明", { exact: true }),
    ).toBeVisible();
    await check(
      section.getByText(branches[1].description, { exact: true }),
    ).toHaveCount(0);
    expect(app.draft(draft.id)).toEqual(saved);
  },
);

it("能力源字段元数据驱动表单与设备指南，查看指南不改草稿", async () => {
  const tasks = clientTestTasks();
  expect(tasks.length).toBeGreaterThan(0);
  for (const { device, action, parameter } of tasks) {
    const properties = parameter.schema.properties as Record<
      string,
      Record<string, any>
    >;
    const fields = Object.entries(properties).filter(
      ([name]) => name !== "type",
    );
    const params: Record<string, unknown> = { type: parameter.type };
    for (const [name, schema] of fields) {
      if (Array.isArray(schema.oneOf)) {
        const branch = schema.oneOf[0];
        params[name] = Object.fromEntries(
          Object.entries(
            branch.properties as Record<string, Record<string, unknown>>,
          ).map(([key, field]) => [
            key,
            Object.hasOwn(field, "const")
              ? field.const
              : (field.enum as unknown[])[0],
          ]),
        );
      } else if (Array.isArray(schema.enum)) params[name] = schema.enum[0];
      else if (Object.hasOwn(schema, "const")) params[name] = schema.const;
      else if (name === "duration_s") params[name] = 10;
    }
    const { app, page, draft } = await setup("10", false, {
      capabilities: clientTestCapabilitiesText(),
      draftText: JSON.stringify({
        name: "能力元数据测试",
        actions: [
          {
            name: parameter.name,
            type: action.type,
            device_id: device.device_id,
            scheduled_at: "2026-10-11 12:00:00",
            policy: { max_delay_ms: 0 },
            params,
          },
        ],
      }),
    });
    const before = app.draft(draft.id);
    if (fields.length === 0)
      await check(
        page.getByText("该任务使用固定设置，无需填写其他参数。", {
          exact: true,
        }),
      ).toBeVisible();
    for (const [name, schema] of fields) {
      if (Array.isArray(schema.oneOf)) {
        const object = page
          .locator(".object-branch-field")
          .filter({ has: page.locator("h5").getByText(name, { exact: true }) });
        if (typeof schema.description === "string")
          await check(
            object.getByText(schema.description, { exact: true }),
          ).toBeVisible();
        // 逐个核对实际存在的字段，判别字段本身也是需要展示说明的普通事实。
        for (const [member, field] of Object.entries(
          schema.oneOf[0].properties as Record<string, Record<string, unknown>>,
        )) {
          const control = page.getByLabel(
            `${typeof field.title === "string" ? field.title : member} (${member})`,
            { exact: true },
          );
          await check(control).toBeVisible();
          if (typeof field.description === "string")
            await check(
              object.locator(".field").filter({ has: control }),
            ).toContainText(field.description);
        }
      } else {
        const control = page.getByLabel(
          `${typeof schema.title === "string" ? schema.title : name} (${name})`,
          { exact: true },
        );
        await check(control).toBeVisible();
        if (typeof schema.description === "string")
          await check(
            page.locator(".field").filter({ has: control }),
          ).toContainText(schema.description);
      }
    }
    await page.getByRole("button", { name: "设备说明" }).click();
    const guideTask = page
      .getByTestId("device-guide")
      .locator(".guide-device")
      .filter({
        has: page.getByRole("heading", { name: device.device_id, exact: true }),
      })
      .locator(".guide-task")
      .filter({
        has: page.getByRole("heading", { name: parameter.name, exact: true }),
      });
    await check(guideTask.locator(".guide-description")).toHaveText(
      parameter.description,
    );
    for (const [name, schema] of fields) {
      const guideField = guideTask.locator(".guide-fields > div").filter({
        has: page.locator("dt").filter({
          hasText: typeof schema.title === "string" ? schema.title : name,
        }),
      });
      if (typeof schema.description === "string")
        await check(guideField).toContainText(schema.description);
    }
    expect(app.draft(draft.id)).toEqual(before);
    // 取值仅用于建立可显示样例；真实后端仍须证明各任务参数符合完整规则。
    expect(() => app.validateContent(before.content)).not.toThrow();
  }
}, 60000);

it("能力源的仅模式曝光对象可切换并随动作保存必选码率", async () => {
  const tasks = clientTestTasks().filter(({ parameter }) => {
    const fields = parameter.schema.properties as Record<string, any>;
    return (
      Array.isArray(fields.bitrate?.enum) &&
      Array.isArray(fields.exposure?.oneOf) &&
      fields.exposure.oneOf.every(
        (branch: any) =>
          Object.keys(branch.properties).length === 1 && branch.properties.mode,
      )
    );
  });
  expect(tasks.length).toBeGreaterThan(0);
  for (const { device, action, parameter } of tasks) {
    const fields = parameter.schema.properties as Record<string, any>;
    const branches = fields.exposure.oneOf;
    const params: Record<string, unknown> = { type: parameter.type };
    for (const [name, schema] of Object.entries(fields)) {
      if (name === "type") continue;
      params[name] =
        name === "exposure"
          ? { mode: branches[0].properties.mode.const }
          : Array.isArray(schema.enum)
            ? schema.enum[0]
            : 10;
    }
    const draftText = JSON.stringify({
      name: "模式与码率",
      actions: [
        {
          name: parameter.name,
          type: action.type,
          device_id: device.device_id,
          scheduled_at: "2026-10-11 12:00:00",
          policy: { max_delay_ms: 0 },
          params,
        },
      ],
    });
    const { app, page, draft } = await setup("10", false, {
      capabilities: clientTestCapabilitiesText(),
      draftText,
    });
    const bitrate = page.getByRole("combobox", {
      name: `${fields.bitrate.title} (bitrate)`,
      exact: true,
    });
    await check(bitrate).toBeVisible();
    await check(
      page.getByRole("combobox", {
        name: /\((?:iso|compensation_ev|outputs)\)$/,
      }),
    ).toHaveCount(0);
    await choose(bitrate, "1");
    await choose(
      page.getByRole("combobox", {
        name: `${branches[0].properties.mode.title} (mode)`,
        exact: true,
      }),
      "branch:1",
    );
    await check
      .poll(
        () =>
          (parseJson(app.draft(draft.id).content.text) as any).actions[0]
            .params,
      )
      .toEqual({
        ...params,
        bitrate: fields.bitrate.enum[1],
        exposure: { mode: branches[1].properties.mode.const },
      });
    const saved = app.draft(draft.id);
    expect(() =>
      app.exportDraft(draft.id, saved.revision, saved.content),
    ).not.toThrow();
  }
});

it.each([
  { value: "1" },
  { value: "null" },
  { value: '"auto"' },
  { value: "[]" },
  { value: '{"mode":"future","iso":800}' },
  { value: '{"mode":"manual","iso":800,"compensation_ev":0}' },
])("不能解释的曝光原值 %j 保留 JSON 原值及同源结构说明", async (exposure) => {
  const { app, page, draft } = await setup("10", false, exposure);
  const before = app.draft(draft.id);
  const control = page.getByLabel("exposure (exposure)", { exact: true });
  const field = page.locator(".field").filter({ has: control });
  await check(control).toHaveClass(/code-input/);
  await check(control).toHaveValue(
    JSON.stringify(JSON.parse(exposure.value), null, 2),
  );
  expect(await control.evaluate((element) => element.tagName)).toBe("TEXTAREA");
  expect(Number(await control.getAttribute("rows"))).toBeGreaterThanOrEqual(3);
  await check(field.locator(".json-field-label")).toContainText("JSON");
  const rules = field.locator(".json-field-rules");
  for (const fact of [
    "mode",
    '"manual"',
    "iso",
    "100",
    "800",
    '"auto"',
    "compensation_ev",
    "-0.7",
    "0.7",
  ])
    await check(rules).toContainText(fact);
  await rules.locator("summary").click();
  await check(rules.locator("pre")).toContainText('"oneOf"');
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await check(
    page.getByRole("button", { name: "重新填写exposure", exact: true }),
  ).toBeVisible();
  expect(app.draft(draft.id)).toEqual(before);
});

it("裸曝光方式原文与非法旧值的诊断分开呈现并保留全部责任", async () => {
  const { app, page, draft } = await setup("10", false, {
    value: "1",
    pending: "auto",
  });
  const control = page.getByLabel("exposure (exposure)", { exact: true });
  const field = page.locator(".field").filter({ has: control });
  await check(control).toHaveValue("auto");
  await check(field.locator(".current-input-error")).toContainText("输入");
  await check(field.locator(".current-input-error")).not.toContainText("对象");
  await check(field.locator(".retained-content-errors")).toContainText("对象");
  await check(page.locator(".pending-validation-note")).toBeVisible();
  await page.locator(".raw-validation > summary").click();
  await check(
    page.locator(".raw-validation li").filter({ hasText: "schema_type" }),
  ).toHaveCount(2);
  await check(
    page.locator(".raw-validation li").filter({ hasText: "schema_oneOf" }),
  ).toHaveCount(1);
  await check(
    page.getByRole("textbox", { name: /未完成输入：.*exposure/ }),
  ).toBeVisible();
  const saved = app.draft(draft.id);
  expect(
    (parseJson(saved.content.text) as any).actions[0].params.exposure,
  ).toBe(1);
  expect(saved.content.pending?.["/actions/0/params/exposure"]?.text).toBe(
    "auto",
  );
  expect(() =>
    app.exportDraft(draft.id, saved.revision, saved.content),
  ).toThrowError(expect.objectContaining({ code: "unfinished_input" }));
  await choose(
    page.getByRole("combobox", {
      name: "resolution (resolution)",
      exact: true,
    }),
    "0",
  );
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .resolution,
    )
    .toBe("8k30");
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(control).toHaveValue("auto");
  await check(field.locator(".retained-content-errors")).toContainText("对象");
  const reloaded = app.draft(draft.id);
  expect(
    (parseJson(reloaded.content.text) as any).actions[0].params.exposure,
  ).toBe(1);
  expect(reloaded.content.pending?.["/actions/0/params/exposure"]?.text).toBe(
    "auto",
  );
});

it.each(["params", "plan"])(
  "%s JSON 入口同样区分成员未完成原文与旧完整内容诊断",
  async (scope) => {
    const { app, page, draft } = await setup("10", false, {
      value: "1",
      pending: "auto",
    });
    const before = app.draft(draft.id);
    if (scope === "params")
      await page
        .getByRole("button", { name: "参数 JSON", exact: true })
        .click();
    else await page.getByTestId("draft-json-toggle").click();
    const control =
      scope === "params"
        ? page.getByLabel("参数 JSON 文本", { exact: true })
        : page.getByTestId("draft-json-input");
    const field = page.locator(".field").filter({ has: control });
    await check(control).toHaveAttribute("readonly", "");
    await check(field.locator(".current-input-error")).toContainText("输入");
    await check(field.locator(".retained-content-errors")).toContainText(
      "对象",
    );
    expect(app.draft(draft.id)).toEqual(before);
  },
);

it("高级修正明确填写完整曝光对象后重新校验且不改变其他参数", async () => {
  const { app, page, draft } = await setup("10", false, {
    value: "1",
    pending: "auto",
  });
  const before = (parseJson(app.draft(draft.id).content.text) as any).actions[0]
    .params;
  const control = page.getByLabel("exposure (exposure)", { exact: true });
  const field = page.locator(".field").filter({ has: control });
  await check(field.locator(".json-field-rules")).toContainText(
    "compensation_ev",
  );
  await control.fill('{"mode":"auto","compensation_ev":0.7}');
  await check
    .poll(
      () => app.draft(draft.id).content.pending?.["/actions/0/params/exposure"],
    )
    .toBeUndefined();
  await check(
    page.getByRole("combobox", { name: "mode (mode)", exact: true }),
  ).toContainText("auto");
  await check(
    page.getByRole("combobox", {
      name: "compensation_ev (compensation_ev)",
      exact: true,
    }),
  ).not.toHaveAttribute("aria-invalid", "true");
  await check(page.locator(".retained-content-errors")).toHaveCount(0);
  const saved = app.draft(draft.id);
  expect((parseJson(saved.content.text) as any).actions[0].params).toEqual({
    ...before,
    exposure: { mode: "auto", compensation_ev: 0.7 },
  });
  expect(() =>
    app.exportDraft(draft.id, saved.revision, saved.content),
  ).not.toThrow();
});

it("设备说明展示同一曝光对象结构且查看说明不写草稿", async () => {
  const { app, page, draft } = await setup("10", false, {
    value: "1",
    pending: "auto",
  });
  const before = app.draft(draft.id);
  await page.getByRole("button", { name: /设备说明/ }).click();
  const rules = page
    .getByTestId("device-guide")
    .locator(".guide-fields > div")
    .filter({ has: page.locator("dt").filter({ hasText: "exposure" }) })
    .locator(".json-field-rules");
  for (const fact of ["mode", '"manual"', "iso", '"auto"', "compensation_ev"])
    await check(rules).toContainText(fact);
  expect(app.draft(draft.id)).toEqual(before);
});

it("未完成曝光 JSON 和未完成时长不遮蔽独立枚举，选择后原文保持", async () => {
  const { app, page, draft } = await setup("10", true);
  await page.getByLabel("duration_s (duration_s)", { exact: true }).fill("10e");
  await choose(
    page.getByRole("combobox", {
      name: "resolution (resolution)",
      exact: true,
    }),
    "0",
  );
  await check
    .poll(
      () =>
        app.draft(draft.id).content.pending?.["/actions/0/params/duration_s"]
          ?.text,
    )
    .toBe("10e");
  expect(
    app.draft(draft.id).content.pending?.["/actions/0/params/exposure"]?.text,
  ).toBe('{"mode":');
  expect(
    (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
      .resolution,
  ).toBe("8k30");
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(
    page.getByLabel("duration_s (duration_s)", { exact: true }),
  ).toHaveValue("10e");
  await check(
    page.getByLabel("exposure (exposure)", { exact: true }),
  ).toHaveValue('{"mode":');
  const saved = app.draft(draft.id);
  expect(() =>
    app.exportDraft(draft.id, saved.revision, saved.content),
  ).toThrowError(expect.objectContaining({ code: "unfinished_input" }));
});

it.each(["0", "1.5", "9223372036854776"])(
  "非法时长 %s 在网页显示诊断且后端拒绝导出",
  async (duration) => {
    const { app, page, draft } = await setup(duration);
    await check(
      page.getByLabel("duration_s (duration_s)", { exact: true }),
    ).toHaveAttribute("aria-invalid", "true");
    const saved = app.draft(draft.id);
    expect(() =>
      app.exportDraft(draft.id, saved.revision, saved.content),
    ).toThrowError(expect.objectContaining({ code: "invalid_plan" }));
    await check(
      page.getByRole("combobox", {
        name: "resolution (resolution)",
        exact: true,
      }),
    ).toBeEnabled();
  },
);

it("整数时长上界按原数学值通过后端校验并显示", async () => {
  const { app, page, draft } = await setup("9223372036854775");
  await check(
    page.getByLabel("duration_s (duration_s)", { exact: true }),
  ).toHaveValue("9223372036854775");
  const saved = app.draft(draft.id);
  expect(() =>
    app.exportDraft(draft.id, saved.revision, saved.content),
  ).not.toThrow();
});

it("超限时长的字段、摘要和 HTTP 诊断均保留原上界", async () => {
  const { app, page, draft } = await setup("9223372036854776");
  const field = page
    .locator(".field")
    .filter({
      has: page.getByLabel("duration_s (duration_s)", { exact: true }),
    })
    .locator(".field-error");
  await check(field).toContainText("9223372036854775");
  await check(page.locator(".validation")).toContainText("9223372036854775");
  const response = await page.request.post(
    `${page.url()}api/drafts/${draft.id}/validate`,
    {
      data: { content: app.draft(draft.id).content },
    },
  );
  expect(response.status()).toBe(400);
  const body = await response.text();
  expect((parseJson(body) as { code: string }).code).toBe("invalid_plan");
  expect(body).toMatch(/"limit":9223372036854775\s*[,}]/);
  await check(field).not.toContainText("9223372036854776");
});

it("首次选择曝光模式只建立mode，子选项保持未填写", async () => {
  const { app, page, draft } = await setup("10", false, { omit: true });
  const mode = page.getByRole("combobox", { name: "mode (mode)", exact: true });
  await choose(mode, "branch:0");
  const iso = page.getByRole("combobox", { name: "iso (iso)", exact: true });
  await check(iso).toHaveAttribute("data-value", "");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "manual" });
  const saved = app.draft(draft.id);
  expect(() =>
    app.exportDraft(draft.id, saved.revision, saved.content),
  ).toThrowError(expect.objectContaining({ code: "invalid_plan" }));
});

it("首次高级JSON替换曝光模式保存原有manual内容", async () => {
  const { app, page, draft } = await setup();
  expect(app.draft(draft.id).content.objectVariants).toBeUndefined();
  await check(
    page.getByRole("combobox", { name: "iso (iso)", exact: true }),
  ).toContainText("800");
  await page
    .getByRole("button", { name: "exposure JSON", exact: true })
    .click();
  await page
    .getByLabel("exposure (exposure)", { exact: true })
    .fill('{"mode":"auto","compensation_ev":0.7}');
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "auto", compensation_ev: 0.7 });
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await page
    .getByRole("button", { name: "exposure JSON", exact: true })
    .click();
  await choose(
    page.getByRole("combobox", { name: "mode (mode)", exact: true }),
    "branch:0",
  );
  await check(
    page.getByRole("combobox", { name: "iso (iso)", exact: true }),
  ).toContainText("800");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "manual", iso: 800 });
  await choose(
    page.getByRole("combobox", { name: "mode (mode)", exact: true }),
    "branch:1",
  );
  await check(
    page.getByRole("combobox", {
      name: "compensation_ev (compensation_ev)",
      exact: true,
    }),
  ).toContainText("0.7");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "auto", compensation_ev: 0.7 });
  await check(page.getByTestId("save-status")).toContainText("已保存");
  await page.locator(".object-branch-field").screenshot({
    path: resolve(
      "../../.superpowers/sdd/2026-10-11-client-parameter-fields/exposure-cascade-field.png",
    ),
  });
});

it("两种曝光模式各自保存，刷新后双向恢复且导出只含当前模式", async () => {
  const { app, page, draft } = await setup();
  const mode = page.getByRole("combobox", { name: "mode (mode)", exact: true });
  const compensation = page.getByRole("combobox", {
    name: "compensation_ev (compensation_ev)",
    exact: true,
  });
  await choose(mode, "branch:1");
  await check(compensation).toHaveAttribute("data-value", "");
  await choose(compensation, "2");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "auto", compensation_ev: 0.7 });
  await page.reload();
  await page.getByTestId("draft-open-button").click();
  await check(compensation).toContainText("0.7");
  await choose(mode, "branch:0");
  await check(
    page.getByRole("combobox", { name: "iso (iso)", exact: true }),
  ).toContainText("800");
  await check(compensation).toHaveCount(0);
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "manual", iso: 800 });
  await choose(mode, "branch:1");
  await check(compensation).toContainText("0.7");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "auto", compensation_ev: 0.7 });
  await check(page.getByTestId("save-status")).toContainText("已保存");
  const beforeBrowse = app.draft(draft.id);
  await choose(mode, "branch:1");
  await page
    .getByRole("button", { name: "exposure JSON", exact: true })
    .click();
  await check(
    page.getByLabel("exposure (exposure)", { exact: true }),
  ).toHaveValue(/"compensation_ev": 0.7/);
  await page
    .getByRole("button", { name: "exposure JSON", exact: true })
    .click();
  await readOptions(compensation);
  expect(app.draft(draft.id)).toEqual(beforeBrowse);
  const evidence = resolve(
    "../../.superpowers/sdd/2026-10-11-client-parameter-fields/exposure-cascade.png",
  );
  mkdirSync(
    resolve("../../.superpowers/sdd/2026-10-11-client-parameter-fields"),
    { recursive: true },
  );
  await page.screenshot({ path: evidence, fullPage: true });
  const savedAuto = app.draft(draft.id);
  const request = app.exportDraft(
    draft.id,
    savedAuto.revision,
    savedAuto.content,
  );
  expect(
    (parseJson(stringifyJson(request.body)) as any).actions[0].params.exposure,
  ).toEqual({ mode: "auto", compensation_ev: 0.7 });
  expect(stringifyJson(request.body)).not.toContain("objectVariants");
});

it("重新填写曝光后停用模式内容不会重新出现", async () => {
  const { app, page, draft } = await setup();
  const mode = page.getByRole("combobox", { name: "mode (mode)", exact: true });
  await choose(mode, "branch:1");
  await choose(
    page.getByRole("combobox", {
      name: "compensation_ev (compensation_ev)",
      exact: true,
    }),
    "2",
  );
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "auto", compensation_ev: 0.7 });
  await page
    .getByRole("button", { name: "exposure JSON", exact: true })
    .click();
  await page
    .getByLabel("exposure (exposure)", { exact: true })
    .fill('"unresolved"');
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toBe("unresolved");
  page.once("dialog", async (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "重新填写exposure", exact: true })
    .click();
  await check(mode).toHaveAttribute("data-value", "");
  await choose(mode, "branch:0");
  await check(
    page.getByRole("combobox", { name: "iso (iso)", exact: true }),
  ).toHaveAttribute("data-value", "");
  await choose(mode, "branch:1");
  await check(
    page.getByRole("combobox", {
      name: "compensation_ev (compensation_ev)",
      exact: true,
    }),
  ).toHaveAttribute("data-value", "");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "auto" });
});

it("重新填写曝光取消时原文不变，确认后只清除该字段", async () => {
  const { app, page, draft } = await setup("10", false, {
    value: "1",
    pending: " auto ",
  });
  const before = app.draft(draft.id);
  const refill = page.getByRole("button", {
    name: "重新填写exposure",
    exact: true,
  });
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("exposure");
    await dialog.dismiss();
  });
  await refill.click();
  await check(
    page.getByLabel("exposure (exposure)", { exact: true }),
  ).toHaveValue(" auto ");
  expect(app.draft(draft.id)).toEqual(before);
  page.once("dialog", async (dialog) => dialog.accept());
  await refill.click();
  await check(
    page.getByRole("combobox", { name: "mode (mode)", exact: true }),
  ).toHaveAttribute("data-value", "");
  await check
    .poll(() => Object.keys(app.draft(draft.id).content.pending ?? {}))
    .toEqual([]);
  const params = (parseJson(app.draft(draft.id).content.text) as any).actions[0]
    .params;
  expect(params).toEqual({
    type: "action6_record",
    duration_s: 10,
    resolution: "4k30",
    fov: "wide",
    stabilization: "off",
  });
});

it("曝光子成员未完成原文可随模式切走，切回继续编辑", async () => {
  const { app, page, draft } = await setup("10", false, { childPending: "8e" });
  const mode = page.getByRole("combobox", { name: "mode (mode)", exact: true });
  await check(mode).toBeEnabled();
  await choose(mode, "branch:1");
  await check
    .poll(
      () =>
        app.draft(draft.id).content.pending?.["/actions/0/params/exposure/iso"],
    )
    .toBeUndefined();
  await choose(
    page.getByRole("combobox", {
      name: "compensation_ev (compensation_ev)",
      exact: true,
    }),
    "1",
  );
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ mode: "auto", compensation_ev: 0 });
  await choose(mode, "branch:0");
  await check
    .poll(
      () =>
        app.draft(draft.id).content.pending?.["/actions/0/params/exposure/iso"]
          ?.text,
    )
    .toBe("8e");
  await check(
    page.getByRole("textbox", { name: /未完成输入：.*iso/ }),
  ).toHaveValue("8e");
  const saved = app.draft(draft.id);
  expect(() =>
    app.exportDraft(draft.id, saved.revision, saved.content),
  ).toThrowError(expect.objectContaining({ code: "unfinished_input" }));
});

it("曝光缺失子成员的校验摘要定位对应选择器并保持草稿", async () => {
  const { app, page, draft } = await setup("10", false, {
    value: '{"mode":"manual"}',
  });
  const before = app.draft(draft.id);
  const iso = page.getByRole("combobox", { name: "iso (iso)", exact: true });
  await check(iso).toHaveAttribute("aria-invalid", "true");
  await check(
    page.getByRole("combobox", { name: "mode (mode)", exact: true }),
  ).not.toHaveAttribute("aria-invalid", "true");
  const summary = page
    .getByTestId("validation-issues")
    .locator(".validation-link");
  await check(summary).toHaveCount(1);
  await check(summary).not.toContainText("compensation_ev");
  await page
    .getByTestId("validation-issues")
    .getByRole("button", { name: /iso/ })
    .first()
    .click();
  await check(iso).toBeFocused();
  await page.locator(".raw-validation > summary").click();
  const raw = page.locator(".raw-validation li");
  await check(raw).toHaveCount(4);
  await check(raw.filter({ hasText: "schema_required" })).toHaveCount(2);
  await check(raw.filter({ hasText: "schema_const" })).toHaveCount(1);
  await check(raw.filter({ hasText: "schema_oneOf" })).toHaveCount(1);
  expect(await raw.locator("code").allTextContents()).toEqual(
    expect.arrayContaining([
      "actions[0].params.exposure.iso",
      "actions[0].params.exposure.compensation_ev",
      "actions[0].params.exposure.mode",
      "actions[0].params.exposure",
    ]),
  );
  const response = await page.request.post(
    `${page.url()}api/drafts/${draft.id}/validate`,
    {
      data: { content: before.content },
    },
  );
  expect(response.status()).toBe(400);
  const machine = parseJson(await response.text()) as any;
  expect(machine.code).toBe("invalid_plan");
  expect(
    machine.issues.map((issue: any) => ({
      code: issue.code,
      path: issue.path,
      params: issue.schema.params,
    })),
  ).toEqual(
    expect.arrayContaining([
      {
        code: "schema_required",
        path: "actions[0].params.exposure.iso",
        params: { missingProperty: "iso" },
      },
      {
        code: "schema_required",
        path: "actions[0].params.exposure.compensation_ev",
        params: { missingProperty: "compensation_ev" },
      },
      {
        code: "schema_const",
        path: "actions[0].params.exposure.mode",
        params: { allowedValue: "auto" },
      },
      {
        code: "schema_oneOf",
        path: "actions[0].params.exposure",
        params: { passingSchemas: null },
      },
    ]),
  );
  expect(machine.issues).toHaveLength(4);
  expect(app.draft(draft.id)).toEqual(before);
});

it.each([{ omit: true }, { value: "{}" }])(
  "未选择曝光模式时摘要只定位mode且浏览不写入：%j",
  async (exposure) => {
    const { app, page, draft } = await setup("10", false, exposure);
    const before = app.draft(draft.id);
    const summary = page
      .getByTestId("validation-issues")
      .locator(".validation-link");
    await check(summary).toHaveCount(1);
    await check(summary).not.toContainText("iso");
    await check(summary).not.toContainText("compensation_ev");
    await summary.click();
    await check(
      page.getByRole("combobox", { name: "mode (mode)", exact: true }),
    ).toBeFocused();
    await check(page.getByTestId("draft-json-input")).toHaveCount(0);
    await check(
      page.getByRole("combobox", { name: "iso (iso)", exact: true }),
    ).toHaveCount(0);
    await check(
      page.getByRole("combobox", {
        name: "compensation_ev (compensation_ev)",
        exact: true,
      }),
    ).toHaveCount(0);
    await page.locator(".raw-validation > summary").click();
    const raw = page.locator(".raw-validation li");
    await check(raw).toHaveCount(exposure.omit ? 1 : 5);
    await check(raw.filter({ hasText: "schema_required" })).toHaveCount(
      exposure.omit ? 1 : 4,
    );
    await check(raw.filter({ hasText: "schema_oneOf" })).toHaveCount(
      exposure.omit ? 0 : 1,
    );
    expect(await raw.locator("code").allTextContents()).toEqual(
      exposure.omit
        ? ["actions[0].params.exposure"]
        : [
            "actions[0].params.exposure.mode",
            "actions[0].params.exposure.iso",
            "actions[0].params.exposure.mode",
            "actions[0].params.exposure.compensation_ev",
            "actions[0].params.exposure",
          ],
    );
    expect(app.draft(draft.id)).toEqual(before);
  },
);

it("设备判别字段变更后保留旧模式资料，明确重新填写才使用kind", async () => {
  const capabilities = JSON.parse(capabilityText);
  const second = structuredClone(capabilities.devices[0]);
  second.device_id = "action6-kind";
  const branches =
    second.actions[0].parameter_types[0].schema.properties.exposure.oneOf;
  for (const branch of branches) {
    branch.properties.kind = branch.properties.mode;
    delete branch.properties.mode;
    branch.required = branch.required.map((name: string) =>
      name === "mode" ? "kind" : name,
    );
  }
  capabilities.devices.push(second);
  const { app, page, draft } = await setup("10", false, {
    capabilities: JSON.stringify(capabilities),
  });
  await choose(
    page.getByRole("combobox", { name: "mode (mode)", exact: true }),
    "branch:1",
  );
  await page
    .getByRole("button", { name: "exposure JSON", exact: true })
    .click();
  await page.getByLabel("exposure (exposure)", { exact: true }).fill("{}");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({});
  expect(app.draft(draft.id).content.objectVariants).toBeDefined();
  await choose(
    page.getByRole("combobox", { name: "目标设备", exact: true }),
    "action6-kind",
  );
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0]
          .device_id,
    )
    .toBe("action6-kind");
  const before = app.draft(draft.id);
  const refill = page.getByRole("button", {
    name: "重新填写exposure",
    exact: true,
  });
  await check(refill).toBeVisible();
  await check(
    page.getByLabel("exposure (exposure)", { exact: true }),
  ).toHaveValue("{}");
  expect(app.draft(draft.id)).toEqual(before);
  page.once("dialog", async (dialog) => dialog.accept());
  await refill.click();
  const kind = page.getByRole("combobox", { name: "kind (kind)", exact: true });
  await check(kind).toBeEnabled();
  await check(kind).toHaveAttribute("data-value", "");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params,
    )
    .toEqual({
      type: "action6_record",
      duration_s: 10,
      resolution: "4k30",
      fov: "wide",
      stabilization: "off",
    });
  await choose(kind, "branch:0");
  await check(
    page.getByRole("combobox", { name: "iso (iso)", exact: true }),
  ).toHaveAttribute("data-value", "");
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
          .exposure,
    )
    .toEqual({ kind: "manual" });
});

it("设备切换使用该设备的子选项且保留原曝光内容", async () => {
  const capabilities = JSON.parse(capabilityText);
  const second = structuredClone(capabilities.devices[0]);
  second.device_id = "action6-secondary";
  second.actions[0].parameter_types[0].schema.properties.exposure.oneOf[0].properties.iso.enum =
    [200, 1600];
  second.actions[0].parameter_types[0].schema.properties.exposure.oneOf[1].properties.compensation_ev.enum =
    [-1, 0, 1];
  capabilities.devices.push(second);
  const { app, page, draft } = await setup("10", false, {
    value: '{"mode":"manual"}',
    capabilities: JSON.stringify(capabilities),
  });
  const iso = page.getByRole("combobox", { name: "iso (iso)", exact: true });
  expect((await readOptions(iso)).map((option) => option.text)).toEqual(
    expect.arrayContaining(["100", "800"]),
  );
  await choose(
    page.getByRole("combobox", { name: "目标设备", exact: true }),
    "action6-secondary",
  );
  expect((await readOptions(iso)).map((option) => option.text)).toEqual(
    expect.arrayContaining(["200", "1600"]),
  );
  expect((await readOptions(iso)).map((option) => option.text)).not.toContain(
    "800",
  );
  await check
    .poll(
      () =>
        (parseJson(app.draft(draft.id).content.text) as any).actions[0]
          .device_id,
    )
    .toBe("action6-secondary");
  expect(
    (parseJson(app.draft(draft.id).content.text) as any).actions[0].params
      .exposure,
  ).toEqual({ mode: "manual" });
  await choose(
    page.getByRole("combobox", { name: "mode (mode)", exact: true }),
    "branch:1",
  );
  const compensation = page.getByRole("combobox", {
    name: "compensation_ev (compensation_ev)",
    exact: true,
  });
  expect(
    (await readOptions(compensation)).map((option) => option.text),
  ).toEqual(expect.arrayContaining(["-1", "0", "1"]));
});
