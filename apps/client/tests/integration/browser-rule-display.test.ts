import { beforeAll, afterAll, afterEach, it, expect } from "vitest";
import { chromium, expect as check, type Browser } from "@playwright/test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import { parseJson, stringifyJson } from "../../src/shared/json";

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

async function setup() {
  const directory = mkdtempSync(join(tmpdir(), "camctl-web-rules-"));
  const capabilities =
    parseJson(`{"devices":[{"device_id":"cam","driver_id":"demo","actions":[{"type":"camera_record","parameter_types":[
    {"type":"rules","name":"数值规则","description":"规则展示","preview_supported":false,"schema":{
      "$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,"required":["type"],
      "$defs":{"lower":{"type":"number","minimum":1e-999},"structured":{"type":"object","properties":{"targetOnly":{"enum":[0,1]}}}},
      "properties":{
        "type":{"const":"rules"},
        "lower":{"$ref":"#/$defs/lower","title":"下界"},
        "upper":{"type":"number","title":"上界","maximum":9223372036854775.807},
        "strictLower":{"type":"number","title":"严格下界","exclusiveMinimum":1.0000000000000001},
        "strictUpper":{"type":"number","title":"严格上界","exclusiveMaximum":9223372036854775.807},
        "numericDefault":{"type":"number","title":"数值默认值","default":1.0000000000000001},
        "objectDefault":{"type":"object","title":"对象默认值","default":{"nested":[1e-999,{"exact":9223372036854775.807}]}},
        "arrayDefault":{"type":"array","title":"数组默认值","default":[1.0000000000000001,{"tiny":1e-999}]},
        "stringDefault":{"type":"string","title":"字符串默认值","default":"1e-999"},
        "ordinary":{"type":"number","title":"普通数字","minimum":1,"maximum":2.5,"default":1.5}
        ,"objectRef":{"$ref":"#/$defs/structured","title":"引用结构","minProperties":1}
        ,"allowedRaw":true,"prohibitedRaw":false
      }}},
    {"type":"finite","name":"组合规则","description":"组合展示","preview_supported":false,"schema":{
      "$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","additionalProperties":false,"required":["type","value","text"],
      "properties":{"type":{"const":"finite"},"value":{"type":"number","enum":[2.5,9223372036854774]},"text":{"type":"string","enum":["1e-999"]}}
    }}]}]}]}`);
  writeFileSync(
    join(directory, "device-capabilities.json"),
    stringifyJson(capabilities),
  );
  const app = new Application(directory),
    files = new Files(app),
    requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const context = await browser.newContext(),
    page = await context.newPage();
  page.setDefaultTimeout(5000);
  const stop = createStop(
    () =>
      new Promise<void>((resolve, reject) =>
        server.close((e) => (e ? reject(e) : resolve())),
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
  expect(
    (
      await page.goto(
        `http://127.0.0.1:${(server.address() as { port: number }).port}`,
      )
    )?.status(),
  ).toBe(200);
  await page.getByTestId("initialize-button").click();
  const draft = app.createDraft({
    text: '{"name":"说明查看","actions":[{"name":"录像","type":"camera_record","device_id":"cam","scheduled_at":"2026-10-11 00:00:00","policy":{"max_delay_ms":0},"params":{"type":"rules"}}]}',
  });
  await page.reload();
  return { page, app, draft };
}

const rules = [
  { name: "lower", title: "下界", facts: ["1e-999"] },
  { name: "upper", title: "上界", facts: ["9223372036854775.807"] },
  { name: "strictLower", title: "严格下界", facts: ["1.0000000000000001"] },
  { name: "strictUpper", title: "严格上界", facts: ["9223372036854775.807"] },
  {
    name: "numericDefault",
    title: "数值默认值",
    facts: ["1.0000000000000001"],
  },
  {
    name: "objectDefault",
    title: "对象默认值",
    facts: ['{"nested":[1e-999,{"exact":9223372036854775.807}]}'],
  },
  {
    name: "arrayDefault",
    title: "数组默认值",
    facts: ['[1.0000000000000001,{"tiny":1e-999}]'],
  },
  { name: "stringDefault", title: "字符串默认值", facts: ["1e-999"] },
  { name: "ordinary", title: "普通数字", facts: ["1", "2.5", "1.5"] },
];

for (const surface of ["editor", "guide"] as const) {
  it.each([
    {
      name: "objectRef",
      title: "引用结构",
      expected: {
        $ref: "#/$defs/structured",
        title: "引用结构",
        minProperties: 1,
      },
    },
    { name: "allowedRaw", title: "allowedRaw", expected: true },
    { name: "prohibitedRaw", title: "prohibitedRaw", expected: false },
  ])(
    `${surface} 完整字段规则保留原引用及布尔 Schema $name`,
    async ({ name, title, expected }) => {
      const { page, app, draft } = await setup();
      const before = app.draft(draft.id);
      let field;
      if (surface === "editor") {
        await page.getByTestId("draft-open-button").click();
        const control = page.getByLabel(`${title} (${name})`, { exact: true });
        field = page.locator(".field").filter({ has: control });
      } else {
        await page.getByTestId("nav-devices").click();
        field = page
          .locator(".guide-fields > div")
          .filter({ has: page.locator("dt").filter({ hasText: title }) });
      }
      const rules = field.locator(".json-field-rules");
      await rules.locator("summary").click();
      const text = await rules.locator("pre").innerText();
      expect(parseJson(text)).toEqual(expected);
      expect((await rules.innerText()).split(text)[0]).not.toContain(
        "targetOnly",
      );
      expect(app.draft(draft.id)).toEqual(before);
    },
  );
}

// 舍入 Schema 说明中的数字、遗漏严格边界或拆掉嵌套默认值的词元保留时，相关用例失败。
for (const surface of ["editor", "guide"] as const) {
  it.each(rules)(
    `${surface} 展示 $name 的原数值事实且查看不保存草稿`,
    async ({ name, title, facts }) => {
      const { page, app, draft } = await setup();
      const before = app.draft(draft.id);
      let description;
      if (surface === "editor") {
        await page.getByTestId("draft-open-button").click();
        description = page
          .locator(".field")
          .filter({
            has: page.getByLabel(`${title} (${name})`, { exact: true }),
          })
          .locator(":scope > small")
          .last();
      } else {
        await page.getByTestId("nav-devices").click();
        description = page
          .locator(".guide-fields > div")
          .filter({
            has: page
              .locator("dt")
              .filter({ hasText: new RegExp(`^${title}`) }),
          })
          .locator("dd");
      }
      for (const fact of facts) await check(description).toContainText(fact);
      if (surface === "editor") {
        await page
          .getByRole("button", { name: "参数 JSON", exact: true })
          .click();
        await page
          .getByRole("button", { name: "参数表单", exact: true })
          .click();
      }
      expect(app.draft(draft.id)).toEqual(before);
    },
  );
}

// 技术详情若使用普通 JSON.stringify，数值事实会变成舍入值或零。
it("设备规则技术详情保留嵌套数值事实", async () => {
  const { page } = await setup();
  await page.getByTestId("nav-devices").click();
  const task = page.locator(".guide-task").filter({
    has: page.getByRole("heading", { name: "数值规则", exact: true }),
  });
  await task.getByText("任务技术详情", { exact: true }).click();
  const schema = await task.locator(".guide-technical > pre").innerText();
  for (const fact of [
    /"minimum":\s*1e-999\s*[,}]/,
    /"maximum":\s*9223372036854775\.807\s*[,}]/,
    /"exclusiveMinimum":\s*1\.0000000000000001\s*[,}]/,
  ])
    expect(schema).toMatch(fact);
});

it("设备组合表保留普通数字与数字字符串", async () => {
  const { page } = await setup();
  await page.getByTestId("nav-devices").click();
  const table = page.getByRole("table", { name: "组合规则允许的参数组合" });
  await check(table.locator("tbody tr")).toHaveCount(2);
  await check(table.locator("tbody td")).toHaveText([
    "2.5",
    "1e-999",
    "9223372036854774",
    "1e-999",
  ]);
});
