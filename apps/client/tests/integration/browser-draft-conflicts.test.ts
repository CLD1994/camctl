import { beforeAll, afterAll, afterEach, expect, it } from "vitest";
import { chromium, expect as check, type Browser, type Page, type Route } from "@playwright/test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve, sep } from "node:path";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createStop, RequestLifecycle } from "../../src/server/lifecycle";
import { createHttpApp } from "../../src/server/http";
import type { Draft, DraftContent, DraftCreation } from "../../src/server/models";
import { parseClientJson, stringifyJson } from "../../src/shared/json";
import { choose } from "./select-support";
import { pendingInput } from "./pending-support";

let browser: Browser;
const cleanup: Array<() => Promise<void>> = [];
beforeAll(async () => { browser = await chromium.launch({ headless: true }); });
afterAll(async () => { await browser.close(); });
afterEach(async () => { for (const close of cleanup.splice(0)) await close(); });

const exposure = { oneOf: [
  { type: "object", properties: { mode: { const: "manual" }, amount: { type: "number" } }, required: ["mode", "amount"], additionalProperties: false },
  { type: "object", properties: { mode: { const: "auto" }, amount: { type: "number" } }, required: ["mode", "amount"], additionalProperties: false },
] };
const catalog = { devices: [{ device_id: "cam", driver_id: "test", actions: ["camera_record", "camera_take_photo"].map(type => ({ type, parameter_types: ["standard", "bitrate"].map(type => ({ type, name: type, description: "隔离软件验收", preview_supported: false, schema: { $schema: "https://json-schema.org/draft/2020-12/schema", type: "object", properties: { type: { const: type }, n: {type: "number"}, tiny: {type: "number"}, marker: {type: "string"}, exposure }, required: ["type"], additionalProperties: false } })) })) }] };

// 字面量预期独立于生产复制、协调及比较函数；1/0 与这两种原数字具有相同 Number 投影。
function full(name = "base", tag = "L"): DraftContent {
  return parseClientJson(JSON.stringify({
    text: `{"name":"${name}","actions":[{"name":"camera","type":"camera_record","device_id":"cam","scheduled_at":"2026-10-11 12:00:00","policy":{"max_delay_ms":0},"params":{"type":"standard","n":1.0000000000000001,"tiny":1e-999,"exposure":{"mode":"auto","amount":0}}},{"name":"preview","type":"obtain_action_outputs","scheduled_at":"2026-10-11 12:00:00","params":{"source":{"action_name":"camera"},"filter":"preview","purpose":"auto_preview"}}]}`,
    pending: { "/actions/0/policy/max_delay_ms": { kind: "number", text: "1e" }, "/extra": { kind: "json", text: `{"${tag}":` } },
    actionVariants: { "0": [{ type: "camera_take_photo", fields: { device_id: "cam", params: { type: "standard", exposure: { mode: "auto", amount: 0 } }, direct: 1, nested: { tiny: 0 }, marker: tag }, fieldsText: `{"device_id":"cam","params":{"type":"standard","exposure":{"mode":"auto","amount":0}},"direct":1.0000000000000001,"nested":{"tiny":1e-999},"marker":"${tag}"}`, pending: { "/direct": { kind: "number", text: "-" } }, parameterVariants: [{ paramsText: `{"params":{"type":"bitrate","n":1e-999,"marker":"photo-${tag}"}}`, pending: { "/n": { kind: "number", text: "+" } } }] }] },
    parameterVariants: { "0": [{ paramsText: `{"params":{"type":"bitrate","n":1e-999,"marker":"${tag}","exposure":{"mode":"auto","amount":0}}}`, pending: { "/n": { kind: "number", text: "-" } } }] },
    objectVariants: { "0": { "/exposure": { discriminator: "mode", variants: [{ valueText: `{"value":{"mode":"manual","amount":1e-999}}`, pending: { "/amount": { kind: "number", text: "-" } } }] } } },
    automaticPreviews: { namespace: "shared", next: 2, intent: "unset", actions: [{ id: "source" }, { id: "preview", sourceId: "source" }] },
  })) as DraftContent;
}
function gate() {
  let release!: () => void;
  const promise = new Promise<void>(yes => { release = yes; });
  return { promise, release };
}
async function pair(input: DraftContent = { text: '{"name":"base","actions":[]}' }) {
  const directory = mkdtempSync(join(tmpdir(), "camctl-draft-conflicts-"));
  writeFileSync(join(directory, "device-capabilities.json"), JSON.stringify(catalog));
  const app = new Application(directory);
  app.store.initialize();
  const draft = app.createDraft(input), files = new Files(app), requests = new RequestLifecycle();
  const server = createHttpApp(app, files, requests).listen(0, "127.0.0.1");
  await new Promise<void>(yes => server.once("listening", yes));
  const contexts = await Promise.all([browser.newContext(), browser.newContext()]);
  const [a, b] = await Promise.all(contexts.map(context => context.newPage()));
  const releases: Array<() => void> = [], diagnostics: string[] = [];
  const stop = createStop(() => new Promise<void>((yes, no) => server.close(error => error ? no(error) : yes())), requests, () => files.idle(), () => app.store.close());
  cleanup.push(async () => {
    releases.forEach(release => release());
    await Promise.all(contexts.map(context => context.close()));
    await stop();
    const target = resolve(directory);
    if (!target.startsWith(resolve(tmpdir()) + sep) || !basename(target).startsWith("camctl-draft-conflicts-")) throw Error("清理目标不属于隔离验收目录");
    rmSync(target, { recursive: true, force: true });
  });
  const url = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  for (const page of [a, b]) {
    page.setDefaultTimeout(5000);
    page.on("response", response => { if (response.status() >= 400) diagnostics.push(`${response.status()} ${response.url()}`); });
    page.on("requestfailed", request => diagnostics.push(`${request.failure()?.errorText} ${request.url()}`));
    page.on("pageerror", error => diagnostics.push(error.stack ?? error.message));
    await page.goto(url);
    try { await page.getByTestId("draft-open-button").click(); await check(page.getByTestId("draft-editor")).toBeVisible(); }
    catch (error) { throw new Error(`页面装配失败：${diagnostics.join("\n")}`, { cause: error }); }
  }
  return { app, a, b, draft, url, releases, diagnostics };
}
async function conflict(p: Awaited<ReturnType<typeof pair>>, remote?: DraftContent) {
  const hold = gate(); p.releases.push(hold.release);
  let puts = 0;
  await p.b.route(`**/api/drafts/${p.draft.id}`, async route => {
    if (route.request().method() !== "PUT") return route.continue();
    puts++; if (puts === 1) await hold.promise;
    return route.continue();
  });
  const sent = p.b.waitForRequest(request => request.method() === "PUT" && new URL(request.url()).pathname === `/api/drafts/${p.draft.id}`);
  await p.b.getByLabel("计划名称", { exact: true }).fill("local");
  const localRequest = await sent;
  const local = (parseClientJson(localRequest.postData()!) as { content: DraftContent }).content;
  const response = await p.a.request.put(`${p.url}/api/drafts/${p.draft.id}`, { data: { revision: p.draft.revision, content: remote ?? { text: '{"name":"remote","actions":[]}' } } });
  expect(response.status()).toBe(200);
  hold.release();
  await check(p.b.getByRole("button", { name: "查看完整差异", exact: true })).toBeVisible();
  return { local, puts: () => puts };
}
async function select(page: Page, operation: "copy" | "adopt" | "replace", cancel = true) {
  const entry = page.getByRole("button", { name: "查看完整差异", exact: true });
  await entry.click();
  const dialog = page.getByRole("dialog");
  await check(dialog).toBeVisible();
  if (cancel) { await page.keyboard.press("Escape"); await check(entry).toBeFocused(); await entry.click(); }
  if (operation === "copy") return dialog.getByRole("button", { name: /将本页另存/ }).click();
  const name = operation === "adopt" ? "使用已保存版本" : "用本页版本替换已保存版本";
  await dialog.getByRole("button", { name, exact: true }).click();
  if (cancel) { await dialog.getByRole("button", { name: "返回比较", exact: true }).click(); await dialog.getByRole("button", { name, exact: true }).click(); }
  await dialog.getByRole("button", { name: operation === "adopt" ? "确认使用已保存版本" : "确认替换已保存版本", exact: true }).click();
}
async function raw(page: Page) {
  await page.getByTestId("draft-json-toggle").click();
  const text = await page.getByTestId("draft-json-input").inputValue();
  await page.getByTestId("draft-json-toggle").click();
  return text;
}
async function roundTrip(page: Page, tag: string) {
  expect(await raw(page)).toContain("1.0000000000000001");
  expect(await raw(page)).toContain("1e-999");
  await check(pendingInput(page, "/extra")).toHaveValue(`{"${tag}":`);
  const camera = page.locator(".action-card").first();
  await choose(camera.getByLabel("动作类型", { exact: true }), "camera_take_photo");
  expect(await raw(page)).toContain('"direct": 1.0000000000000001');
  expect(await raw(page)).toContain('"tiny": 1e-999');
  await check(pendingInput(page, "/actions/0/direct")).toHaveValue("-");
  await choose(camera.getByLabel("参数类型", { exact: true }), "bitrate");
  expect(await raw(page)).toContain(`photo-${tag}`);
  await check(pendingInput(page, "/actions/0/params/n")).toHaveValue("+");
  await choose(camera.getByLabel("参数类型", { exact: true }), "standard");
  if (!await camera.getByRole("combobox", {name: "mode (mode)", exact: true}).count()) throw Error(`模式控件未装配：${await camera.innerText()}`);
  await choose(camera.getByLabel("动作类型", { exact: true }), "camera_record");
  await check(pendingInput(page, "/actions/0/policy/max_delay_ms")).toHaveValue("1e");
  await choose(camera.getByLabel("参数类型", { exact: true }), "bitrate");
  expect(await raw(page)).toContain('"n": 1e-999');
  await check(pendingInput(page, "/actions/0/params/n")).toHaveValue("-");
  await choose(camera.getByLabel("参数类型", { exact: true }), "standard");
  await choose(camera.getByRole("combobox", { name: "mode (mode)", exact: true }), "branch:0");
  expect(await raw(page)).toContain('"amount": 1e-999');
  await check(pendingInput(page, "/actions/0/params/exposure/amount")).toHaveValue("-");
  await choose(camera.getByRole("combobox", { name: "mode (mode)", exact: true }), "branch:1");
  expect(await raw(page)).toContain('"amount": 0');
}

// 忽略完整资料、选择后清分支、浮点化数字或将未选侧拼入所选版本时失败。
it.each(["copy", "adopt", "replace"] as const)("双浏览器完整%s取消保持两侧，成功重开往返所属类型模式和原数字", async operation => {
  const p = await pair(full()), remote = full("remote", "R"), c = await conflict(p, remote);
  await p.b.getByRole("button", { name: "查看完整差异", exact: true }).click();
  const dialog = p.b.getByRole("dialog");
  await dialog.getByText("完整技术详情", { exact: true }).click();
  await check(dialog).toContainText('"L"'); await check(dialog).toContainText('"R"');
  await p.b.keyboard.press("Escape");
  expect(stringifyJson(p.app.draft(p.draft.id).content)).toBe(stringifyJson(remote));
  await p.b.evaluate(() => { (window as any).focusTrace = []; document.addEventListener("focusin", event => { const node = event.target as HTMLElement; (window as any).focusTrace.push(node.outerHTML.slice(0, 180)); if (node.dataset.testid === "draft-editor") { new MutationObserver((_, observer) => { if (!node.isConnected) { (window as any).focusTrace.push("focused Editor removed; current=" + document.activeElement?.tagName); observer.disconnect(); } }).observe(document.body, {childList: true, subtree: true}); } }); });
  await select(p.b, operation);
  await check(p.b.getByRole("dialog")).toHaveCount(0);
  await check(p.b.getByTestId("draft-operation-notice")).toHaveCount(0);
  try { await check.poll(() => p.b.evaluate(() => document.activeElement?.outerHTML.slice(0, 200))).toContain('data-testid="draft-editor"'); }
  catch (error) { throw new Error(`焦点序列 ${await p.b.evaluate(() => JSON.stringify((window as any).focusTrace))}`, {cause: error}); }
  let target = p.app.draft(p.draft.id);
  if (operation === "copy") target = p.app.store.all<Draft>("drafts").find(item => item.id !== p.draft.id)!;
  const expected = operation === "adopt" ? remote : c.local;
  const actual = target.content;
  for (const part of ["text", "pending", "actionVariants", "parameterVariants", "objectVariants"] as const) expect(stringifyJson(actual[part])).toBe(stringifyJson(expected[part]));
  expect(actual.automaticPreviews?.intent).toBe("unset");
  expect(actual.automaticPreviews?.actions[1].sourceId).toBe(actual.automaticPreviews?.actions[0].id);
  if (operation === "copy") {
    const sourceIds = new Set(c.local.automaticPreviews!.actions.map(action => action.id));
    for (const action of actual.automaticPreviews!.actions) expect(sourceIds.has(action.id)).toBe(false);
    expect(actual.automaticPreviews!.actions).toHaveLength(c.local.automaticPreviews!.actions.length);
  } else expect(actual.automaticPreviews).toEqual(expected.automaticPreviews);
  expect(c.puts()).toBe(operation === "replace" ? 2 : 1);
  await p.b.reload();
  await p.b.getByTestId("draft-open-button").filter({ hasText: operation === "adopt" ? "remote" : "local" }).click();
  await roundTrip(p.b, operation === "adopt" ? "R" : "L");
});

// 清洁同步丢私有资料、使用本地时间或同步时额外保存时失败。
it.each(["text","pending-number","pending-json","actionVariants","parameterVariants","objectVariants","automaticPreviews"] as const)(
  "真实UI仅%s不同，面板显示单一完整资料差异且取消保留双方",
  async section => {
    const body=stringifyJson(parseClientJson('{"name":"base","actions":[{"type":"camera_record","name":"camera","scheduled_at":"2026-10-11 12:00:00","device_id":"cam","policy":{"max_delay_ms":0},"params":{"type":"standard","n":1.0000000000000001,"tiny":1e-999,"exposure":{"mode":"auto","amount":0}}}]}'),2);
    const initial:DraftContent={text:body,automaticPreviews:{namespace:"single",next:1,intent:"unset",actions:[{id:"camera"}]}};
    if(section==="pending-number") initial.pending={"/amount":{kind:"number",text:"-"}};
    if(section==="pending-json") initial.pending={"/extra":{kind:"json",text:"{"}};
    const p=await pair(initial), hold=gate(); p.releases.push(hold.release);
    await p.b.route(`**/api/drafts/${p.draft.id}`,async route => {if(route.request().method()!=="PUT") return route.continue(); await hold.promise; await route.continue();});
    const sent=p.b.waitForRequest(request => request.method()==="PUT" && new URL(request.url()).pathname===`/api/drafts/${p.draft.id}`);
    const camera=p.b.locator(".action-card").first();
    if(section==="text") await p.b.getByLabel("计划名称",{exact:true}).fill("local");
    else if(section==="pending-number") await pendingInput(p.b,"/amount").fill("+");
    else if(section==="pending-json") await pendingInput(p.b,"/extra").fill("[");
    else if(section==="actionVariants") {
      await choose(camera.getByLabel("动作类型",{exact:true}),"camera_take_photo");
      await choose(camera.getByLabel("目标设备",{exact:true}),"cam");
      await choose(camera.getByLabel("参数类型",{exact:true}),"standard");
      await camera.getByRole("textbox",{name:/^marker/}).fill("local");
      await choose(camera.getByLabel("动作类型",{exact:true}),"camera_record");
    }
    else if(section==="parameterVariants") {await choose(camera.getByLabel("参数类型",{exact:true}),"bitrate"); await choose(camera.getByLabel("参数类型",{exact:true}),"standard");}
    else if(section==="objectVariants") {await choose(camera.getByRole("combobox",{name:"mode (mode)",exact:true}),"branch:0"); await choose(camera.getByRole("combobox",{name:"mode (mode)",exact:true}),"branch:1");}
    else {await p.b.getByTestId("preview-intent").click(); await p.b.getByRole("menuitem",{name:"启用预览",exact:true}).click();}
    const local=(parseClientJson((await sent).postData()!) as {content:DraftContent}).content;
    const part: keyof DraftContent=section.startsWith("pending-") ? "pending" : section as keyof DraftContent;
    const remote:DraftContent={...p.draft.content};
    if(section==="text") remote.text=body.replace('"base"','"remote"');
    else if(section==="pending-number") remote.pending={"/amount":{kind:"number",text:"1e"}};
    else if(section==="pending-json") remote.pending={"/extra":{kind:"json",text:'"'}};
    else if(section==="actionVariants") remote.actionVariants={"0":[{type:"camera_take_photo",fields:{marker:"remote"},pending:{}}]};
    else if(section==="parameterVariants") remote.parameterVariants={"0":[{paramsText:'{"params":{"type":"bitrate","marker":"remote"}}',pending:{}}]};
    else if(section==="objectVariants") remote.objectVariants={"0":{"/exposure":{discriminator:"mode",variants:[{valueText:'{"value":{"mode":"manual","amount":1e-999}}',pending:{}}]}}};
    else remote.automaticPreviews={...p.draft.content.automaticPreviews!,intent:"disabled"};
    const fact=(content:DraftContent,key:keyof DraftContent) => stringifyJson(key==="text" ? content.text : key==="automaticPreviews" ? content[key] ?? null : content[key] ?? {});
    for(const key of ["text","pending","actionVariants","parameterVariants","objectVariants","automaticPreviews"] as const) {
      if(key!==part) {expect(fact(local,key),`${section}:本页附带变化${key}`).toBe(fact(p.draft.content,key)); expect(fact(remote,key)).toBe(fact(p.draft.content,key));}
      else {expect(fact(local,key)).not.toBe(fact(remote,key)); expect(fact(local,key)).not.toBe(fact(p.draft.content,key)); expect(fact(remote,key)).not.toBe(fact(p.draft.content,key));}
    }
    const response=await p.a.request.put(`${p.url}/api/drafts/${p.draft.id}`,{data:{revision:p.draft.revision,content:remote}}); expect(response.status()).toBe(200);
    expect(stringifyJson(p.app.draft(p.draft.id).content)).toBe(stringifyJson(remote));
    hold.release(); await check(p.b.getByRole("button",{name:"查看完整差异",exact:true})).toBeVisible();
    await p.b.getByRole("button",{name:"查看完整差异",exact:true}).click(); const dialog=p.b.getByRole("dialog");
    await check(dialog.locator("li").filter({hasText:"存在差异"})).toHaveCount(1);
    await check(dialog.locator("li").filter({hasText:"一致"})).toHaveCount(5);
    await dialog.getByText("完整技术详情",{exact:true}).click();
    const details=await dialog.locator(":scope > details > .draft-difference-sides > section > pre").allTextContents();
    expect(stringifyJson(parseClientJson(details[0]))).toBe(stringifyJson(local)); expect(stringifyJson(parseClientJson(details[1]))).toBe(stringifyJson(remote));
    if(section==="automaticPreviews") {expect(local.automaticPreviews!.intent).toBe("enabled"); expect(remote.automaticPreviews!.intent).toBe("disabled"); expect(local.automaticPreviews!.actions).toEqual(p.draft.content.automaticPreviews!.actions);}
    await p.b.keyboard.press("Escape"); expect(stringifyJson(p.app.draft(p.draft.id).content)).toBe(stringifyJson(remote));
  },
);

// 清洁可靠deleted必须结束当前Editor；保留Map中的dirty或独立责任不属于这一路径。
it("双浏览器A删除清洁草稿，B结束Editor并聚焦草稿列表且不复建原ID", async () => {
  const p = await pair(full()); let posts = 0, puts = 0;
  await p.b.route("**/api/drafts**", route => {
    if (route.request().method() === "POST") posts++;
    if (route.request().method() === "PUT") puts++;
    return route.continue();
  });
  p.a.on("dialog", dialog => dialog.accept());
  await p.a.getByRole("button", { name: "删除草稿", exact: true }).click();
  await check(p.a.getByTestId("draft-editor")).toHaveCount(0);
  await check(p.b.getByTestId("draft-open-button")).toHaveCount(0);
  await check(p.b.getByTestId("draft-editor")).toHaveCount(0);
  await check(p.b.getByRole("tab", { name: /^草稿\s+0$/ })).toBeFocused();
  expect(await p.b.evaluate(() => { const event = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(false);
  expect(posts).toBe(0); expect(puts).toBe(0);
  expect(p.app.state().drafts!.some(draft => draft.id === p.draft.id)).toBe(false);
  await p.b.reload(); await check(p.b.getByTestId("draft-open-button")).toHaveCount(0);
});

it.each(["export", "delete"] as const)("双浏览器A删除后B的%s未知责任保持具体核实入口和只读输入", async operation => {
  const input = { text: '{"name":"fixed","actions":[{"name":"sync","type":"report_status","params":{"scope":"full"}}]}' };
  const p = await pair(input); let offline = false, posts = 0, puts = 0;
  await p.b.route("**/api/state", route => offline ? route.abort("failed") : route.continue());
  await p.b.route("**/api/drafts**", route => {
    const method = route.request().method();
    if (method === "PUT") puts++;
    if (method === "POST") posts++;
    if ((operation === "delete" && method === "DELETE") || (operation === "export" && new URL(route.request().url()).pathname.endsWith("/export"))) {
      offline = true; return route.abort("failed");
    }
    return route.continue();
  });
  if (operation === "delete") p.b.on("dialog", dialog => dialog.accept());
  if (operation === "export") await p.b.getByTestId("export-button").click();
  else await p.b.getByRole("button", { name: "删除草稿", exact: true }).click();
  const verify = p.b.getByRole("button", { name: operation === "export" ? "重新核实导出结果" : "重新核实删除结果", exact: true });
  await check(verify).toBeVisible();
  p.a.on("dialog", dialog => dialog.accept());
  await p.a.getByRole("button", { name: "删除草稿", exact: true }).click();
  await check(p.a.getByTestId("draft-editor")).toHaveCount(0);
  offline = false;
  await check(p.b.getByTestId("draft-open-button")).toHaveCount(0);
  await check(p.b.getByTestId("draft-editor")).toBeVisible();
  await check(p.b.getByLabel("计划名称", { exact: true })).toHaveValue("fixed");
  await check(p.b.getByLabel("计划名称", { exact: true })).toBeDisabled();
  await check(verify).toBeEnabled();
  expect(await p.b.evaluate(() => { const event = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(true);
  expect(posts).toBe(operation === "export" ? 1 : 0); expect(puts).toBe(0);
  expect(p.app.state().drafts).toHaveLength(0); expect(p.app.store.all("requests")).toHaveLength(0);
});

it("双浏览器A删除时B的在途保存保留完整不同输入及只读另存入口", async () => {
  const p = await pair(full()), hold = gate(); p.releases.push(hold.release);
  let puts = 0, local!: DraftContent;
  await p.b.route(`**/api/drafts/${p.draft.id}`, async route => {
    if (route.request().method() !== "PUT") return route.continue();
    puts++; local = (parseClientJson(route.request().postData()!) as { content: DraftContent }).content;
    await hold.promise; return route.continue();
  });
  const sent = p.b.waitForRequest(request => request.method() === "PUT");
  await p.b.getByLabel("计划名称", { exact: true }).fill("local"); await sent;
  p.a.on("dialog", dialog => dialog.accept()); await p.a.getByRole("button", { name: "删除草稿", exact: true }).click();
  await check(p.a.getByTestId("draft-editor")).toHaveCount(0);
  await check(p.b.getByTestId("draft-open-button")).toHaveCount(0);
  await check(p.b.getByTestId("draft-editor")).toBeVisible();
  await check(p.b.getByLabel("计划名称", { exact: true })).toHaveValue("local");
  await check(p.b.getByLabel("计划名称", { exact: true })).toBeDisabled();
  hold.release();
  await retainedInput(p.b, local, true);
  await check(p.b.getByRole("button", { name: /将本页另存/, exact: false })).toBeEnabled();
  expect(puts).toBe(1); expect(p.app.state().drafts).toHaveLength(0);
});

it.each(["other-draft", "records", "devices"] as const)("旧deleted观察返回不抢占用户已经选择的%s", async destination => {
  const p = await pair();
  const created = await p.a.request.post(`${p.url}/api/drafts`, { data: { content: { text: '{"name":"other","actions":[]}' } } });
  expect(created.status()).toBe(201);
  await check(p.b.getByTestId("draft-open-button")).toHaveCount(2);
  const hold = gate(), start = gate(); p.releases.push(hold.release, start.release); let ready!: () => void;
  const fetched = new Promise<void>(yes => { ready = yes; });
  await p.b.route("**/api/state", async route => { await start.promise; const response = await route.fetch(); ready(); await hold.promise; return route.fulfill({ response }); });
  const deleted = await p.a.request.delete(`${p.url}/api/drafts/${p.draft.id}`, { data: { revision: p.draft.revision } }); expect(deleted.status()).toBe(200);
  start.release();
  await fetched;
  if (destination === "other-draft") await p.b.getByTestId("draft-open-button").filter({ hasText: "other" }).click();
  else if (destination === "records") await p.b.getByTestId("tab-records").click();
  else await p.b.getByTestId("nav-devices").click();
  const response = p.b.waitForResponse(response => new URL(response.url()).pathname === "/api/state" && response.ok());
  hold.release(); await response;
  if (destination === "other-draft") await check(p.b.getByLabel("计划名称", { exact: true })).toHaveValue("other");
  else if (destination === "records") await check(p.b.getByTestId("tab-records")).toHaveAttribute("aria-selected", "true");
  else await check(p.b.getByTestId("nav-devices")).toHaveAttribute("aria-current", "page");
  expect(p.app.state().drafts!.some(draft => draft.id === p.draft.id)).toBe(false);
});

it.each(["unavailable", "invalid"] as const)("清洁草稿读取%s保留实际Editor，不用读取问题执行deleted导航", async kind => {
  const p = await pair(full()); let writes = 0;
  await p.b.route("**/api/drafts**", route => { if (["POST", "PUT"].includes(route.request().method())) writes++; return route.continue(); });
  await p.b.route("**/api/state", async route => {
    if (kind === "unavailable") return route.abort("failed");
    // 完整集合结构仍合法；不同createdAt不能作为本会话原草稿的可靠身份。
    const response = await route.fetch(), state = await response.json(); state.drafts[0].createdAt = "2026-01-01 00:00:00";
    return route.fulfill({ response, json: state });
  });
  await check(p.b.locator(".save-line")).toContainText(kind === "unavailable" ? /读取后端记录失败/ : /完整版本契约/);
  await check(p.b.getByTestId("draft-editor")).toBeVisible();
  await check(p.b.getByLabel("计划名称", { exact: true })).toHaveValue("base");
  await check(pendingInput(p.b, "/extra")).toHaveValue('{"L":');
  expect(writes).toBe(0); expect(p.app.state().drafts).toHaveLength(1);
});

it.each([false, true])("双浏览器清洁同步完整远端版本，曾修改后恢复基线=%s不多PUT", async restore => {
  const initial = restore ? {text: '{"name":"base","actions":[]}'} : full();
  initial.text = stringifyJson(parseClientJson(initial.text), 2);
  const p = await pair(initial);
  let writes = 0; const bodies: string[] = [];
  await p.b.route(`**/api/drafts/${p.draft.id}`, route => { if (route.request().method() === "PUT") {writes++; bodies.push(route.request().postData()!);} return route.continue(); });
  if (restore) { await p.b.getByLabel("计划名称", {exact: true}).fill("temporary"); await p.b.getByLabel("计划名称", {exact: true}).fill("base"); }
  const next = full("remote", "R");
  const response = await p.a.request.put(`${p.url}/api/drafts/${p.draft.id}`, {data: {revision: 1, content: next}});
  expect(response.status()).toBe(200);
  try { await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("remote"); }
  catch (error) { throw new Error(`恢复基线诊断 baseline=${stringifyJson(p.draft.content)} actual=${stringifyJson(p.app.draft(p.draft.id))} bodies=${JSON.stringify(bodies)} UI=${await p.b.locator(".save-line").innerText()}`, {cause:error}); }
  await check(pendingInput(p.b, "/extra")).toHaveValue('{"R":');
  await check(p.b.getByTestId("save-status")).toContainText("已保存");
  await check(p.b.locator(".save-line")).toContainText(p.app.draft(p.draft.id).updatedAt);
  expect(stringifyJson(p.app.draft(p.draft.id).content)).toBe(stringifyJson(next));
  expect(p.app.draft(p.draft.id).revision).toBe(2); expect(writes).toBe(0);
});

// 将 ordinary 保存锁成并发选择，或较早回执覆盖后来输入时失败。
it("真实普通保存请求在途仍可继续输入，较早回执不覆盖第二份输入", async () => {
  const p = await pair(), hold = gate(); p.releases.push(hold.release);
  let writes = 0;
  await p.b.route(`**/api/drafts/${p.draft.id}`, async route => { if (route.request().method() !== "PUT") return route.continue(); writes++; if (writes === 1) await hold.promise; return route.continue(); });
  const sent = p.b.waitForRequest(request => request.method() === "PUT");
  await p.b.getByLabel("计划名称", {exact: true}).fill("first"); await sent;
  await check(p.b.getByLabel("计划名称", {exact: true})).toBeEnabled();
  await p.b.getByLabel("计划名称", {exact: true}).fill("second");
  hold.release();
  await check.poll(() => p.app.draft(p.draft.id).content.text).toContain("second");
  await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("second");
  await check(p.a.getByLabel("计划名称", {exact: true})).toHaveValue("second");
  expect(writes).toBe(2); expect(p.app.draft(p.draft.id).revision).toBe(3);
});

// 核实入口误用 flush 会发第二次 PUT；未知失败不能伪称未写入。
it("真实保存核实先读取失败后证实未写入，只读按钮零额外PUT", async () => {
  const p = await pair(); let unavailable = true, writes = 0;
  await p.b.route("**/api/state", route => unavailable ? route.abort("failed") : route.continue());
  await p.b.route(`**/api/drafts/${p.draft.id}`, route => { if (route.request().method() !== "PUT") return route.continue(); writes++; return route.abort("failed"); });
  await p.b.getByLabel("计划名称", {exact: true}).fill("local");
  const verify = p.b.getByRole("button", {name: "核实保存结果", exact: true});
  await check(verify).toBeVisible(); await verify.click();
  expect(writes).toBe(1); expect(p.app.draft(p.draft.id).revision).toBe(1);
  unavailable = false; await verify.click();
  await check(p.b.getByRole("button", {name: "重试保存", exact: true})).toBeVisible();
  expect(writes).toBe(1); await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("local");
  await p.b.unroute(`**/api/drafts/${p.draft.id}`);
  await p.b.getByRole("button", {name: "重试保存", exact: true}).click();
  await check.poll(() => p.app.draft(p.draft.id).revision).toBe(2);
});

// A 最后写回同一个 Y，内容相等仍不能认领 B 已丢失的旧回执。
it.each(["adopt","replace","copy"] as const)("真实旧Y保存响应丢失后被不同Z隔离，%s完成且旧请求不重发", async operation => {
  const p=await pair(); let offline=false,writes=0;
  await p.b.route("**/api/state",route => offline ? route.abort("failed") : route.continue());
  await p.b.route(`**/api/drafts/${p.draft.id}`,async route => {
    if(route.request().method()!=="PUT") return route.continue(); writes++;
    if(writes!==1) return route.continue();
    await route.fetch(); offline=true; return route.abort("failed");
  });
  await p.b.getByLabel("计划名称",{exact:true}).fill("Y");
  await check(p.b.getByRole("button",{name:"核实保存结果",exact:true})).toBeVisible();
  await check(p.a.getByLabel("计划名称",{exact:true})).toHaveValue("Y");
  await p.a.getByLabel("计划名称",{exact:true}).fill("Z"); await check.poll(() => p.app.draft(p.draft.id).revision).toBe(3);
  offline=false; await check(p.b.getByRole("button",{name:"查看完整差异",exact:true})).toBeVisible();
  await check(p.b.getByTestId("save-status")).toContainText("不可确认"); await select(p.b,operation);
  await check(p.b.getByTestId("save-status")).toContainText("已保存");
  await check(p.b.getByLabel("计划名称",{exact:true})).toHaveValue(operation==="adopt" ? "Z" : "Y");
  await p.b.waitForResponse(response => response.url().endsWith("/api/state"));
  expect(writes).toBe(operation==="replace" ? 2 : 1);
  expect((parseClientJson(p.app.draft(p.draft.id).content.text) as {name:string}).name).toBe(operation==="replace" ? "Y" : "Z");
  expect(p.app.store.all<Draft>("drafts")).toHaveLength(operation==="copy" ? 2 : 1);
});

it("真实纯版本推进保留完整本页输入，普通重试使用实际新revision", async () => {
  const p=await pair(full()),hold=gate(); p.releases.push(hold.release); let writes=0; const revisions:number[]=[];
  await p.b.route(`**/api/drafts/${p.draft.id}`,async route => {if(route.request().method()!=="PUT") return route.continue(); writes++; revisions.push((parseClientJson(route.request().postData()!) as {revision:number}).revision); if(writes===1) await hold.promise; return route.continue();});
  const sent=p.b.waitForRequest(request => request.method()==="PUT" && new URL(request.url()).pathname===`/api/drafts/${p.draft.id}`);
  await p.b.getByLabel("计划名称",{exact:true}).fill("local"); const local=(parseClientJson((await sent).postData()!) as {content:DraftContent}).content;
  const response=await p.a.request.put(`${p.url}/api/drafts/${p.draft.id}`,{data:{revision:p.draft.revision,content:p.draft.content}}); expect(response.status()).toBe(200); hold.release();
  await check(p.b.getByRole("button",{name:"重试保存",exact:true})).toBeVisible(); await check(p.b.getByRole("button",{name:"查看完整差异",exact:true})).toHaveCount(0);
  await check(p.b.getByLabel("计划名称",{exact:true})).toHaveValue("local"); await check(pendingInput(p.b,"/extra")).toHaveValue('{"L":');
  await p.b.getByRole("button",{name:"重试保存",exact:true}).click(); await check.poll(() => p.app.draft(p.draft.id).revision).toBe(3);
  expect(revisions).toEqual([1,2]); expect(stringifyJson(p.app.draft(p.draft.id).content)).toBe(stringifyJson(local));
});

it("真实B写Y回执丢失，A先Z后Y取代依据，R=L仍须明确采用且零重发", async () => {
  const p = await pair(); let unavailable = false, writes = 0;
  await p.b.route("**/api/state", route => unavailable ? route.abort("failed") : route.continue());
  await p.b.route(`**/api/drafts/${p.draft.id}`, async route => { if (route.request().method() !== "PUT") return route.continue(); writes++; await route.fetch(); unavailable = true; return route.abort("failed"); });
  await p.b.getByLabel("计划名称", {exact: true}).fill("Y");
  await check(p.b.getByRole("button", {name: "核实保存结果", exact: true})).toBeVisible();
  await check(p.a.getByLabel("计划名称", {exact: true})).toHaveValue("Y");
  await p.a.getByLabel("计划名称", {exact: true}).fill("Z");
  await check.poll(() => p.app.draft(p.draft.id).content.text).toContain('"Z"');
  await p.a.getByLabel("计划名称", {exact: true}).fill("Y");
  await check.poll(() => p.app.draft(p.draft.id).revision).toBe(4);
  expect(p.app.draft(p.draft.id).lastWrite?.revision).toBe(4);
  unavailable = false;
  await check(p.b.getByRole("button", {name: "查看完整差异", exact: true})).toBeVisible();
  await check(p.b.getByTestId("save-status")).toContainText("不可确认");
  await select(p.b, "adopt");
  await check(p.b.getByTestId("save-status")).toContainText("已保存");
  await check(p.b.locator(".save-line")).toContainText("不可确认");
  await p.b.waitForResponse(response => response.url().endsWith("/api/state"));
  expect(writes).toBe(1); expect(p.app.draft(p.draft.id).revision).toBe(4);
});

async function sourceTerminal(p: Awaited<ReturnType<typeof pair>>, kind: "conflict" | "deleted" | "exported") {
  if (kind === "conflict") return;
  const actual = p.app.draft(p.draft.id);
  if (kind === "deleted") expect((await p.a.request.delete(`${p.url}/api/drafts/${actual.id}`, {data: {revision: actual.revision}})).status()).toBe(200);
  else {
    const response = await p.a.request.put(`${p.url}/api/drafts/${actual.id}`, {data: {revision: actual.revision, content: {text: '{"name":"exported","actions":[{"name":"sync","type":"report_status","scheduled_at":"2026-10-11 12:00:00","params":{"scope":"full"}}]}'}}});
    expect(response.status()).toBe(200);
    const current = p.app.draft(actual.id);
    expect((await p.a.request.post(`${p.url}/api/drafts/${actual.id}/export`, {data: {revision: current.revision, content: current.content, capabilityVersion: p.app.capabilities.version}})).status()).toBe(200);
  }
  await check(p.b.locator(".save-line")).toContainText(kind === "deleted" ? "原草稿已删除" : "原草稿已导出");
}
async function retainedInput(page: Page, local: DraftContent, terminal: boolean) {
  await check(page.getByLabel("计划名称", {exact: true})).toHaveValue("local");
  await check(pendingInput(page, "/extra")).toHaveValue('{"L":');
  if (terminal) {
    await check(page.getByLabel("计划名称", {exact: true})).toBeDisabled();
    await page.getByText("额外编辑内容的完整技术详情", {exact: true}).click();
    const details = page.locator("details").filter({has: page.getByText("额外编辑内容的完整技术详情", {exact: true})}).locator("pre");
    expect(stringifyJson(parseClientJson((await details.textContent())!))).toBe(stringifyJson(local));
  }
  await check(page.getByTestId("draft-operation-notice")).toHaveCount(0);
  await check(page.getByTestId("draft-independent-recoveries")).toHaveCount(0);
}

it.each((["conflict", "deleted", "exported"] as const).flatMap(source => (["retry", "abandon"] as const).map(decision => [source, decision] as const)))(
  "真实另存未创建×来源%s后%s固定全输入且不改原事实", async (source, decision) => {
    const p = await pair(full()), c = await conflict(p, full("remote", "R"));
    const attempts: string[] = [], inputs: string[] = [];
    await p.b.route("**/api/draft-creations/*", async route => {
      if (route.request().method() !== "PUT") return route.continue();
      attempts.push(new URL(route.request().url()).pathname); inputs.push(route.request().postData()!);
      if (attempts.length === 1) return route.fulfill({status: 200, json: {kind: "not_created", attemptId: decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1)!)}});
      return route.continue();
    });
    await select(p.b, "copy", false);
    const dialog = p.b.getByRole("dialog");
    await check(dialog).toContainText("not_created");
    expect(p.app.store.all<DraftCreation>("draft_creations")).toHaveLength(0);
    await sourceTerminal(p, source);
    if (decision === "retry") {
      await dialog.getByRole("button", {name: "重试", exact: true}).click();
      await check(dialog).toHaveCount(0);
      expect(attempts).toHaveLength(2); expect(attempts[1]).toBe(attempts[0]); expect(inputs[1]).toBe(inputs[0]);
      await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("local");
      await check(p.b.getByLabel("计划名称", {exact: true})).toBeEnabled();
    } else {
      await dialog.getByRole("button", {name: "放弃", exact: true}).click();
      await check(dialog).toHaveCount(0); await check(p.b.getByTestId("draft-editor")).toBeFocused();
      await retainedInput(p.b, c.local, source !== "conflict");
      expect(attempts).toHaveLength(1);
    }
    expect(p.app.store.all<DraftCreation>("draft_creations")).toHaveLength(decision === "retry" ? 1 : 0);
  },
);

it.each((["deleted", "exported"] as const).flatMap(target => (["conflict", "deleted", "exported"] as const).flatMap(source => (["retry", "abandon"] as const).map(decision => [target, source, decision] as const))))(
  "真实另存响应丢失后目标%s×来源%s再%s保留首次事实和完整输入", async (target, source, decision) => {
    const p = await pair(full()), c = await conflict(p, full("remote", "R"));
    let offline = true, createdId = ""; const attempts: string[] = [], inputs: string[] = [];
    await p.b.route("**/api/draft-creations/*", async route => {
      if (route.request().method() === "GET") return offline ? route.abort("failed") : route.continue();
      attempts.push(new URL(route.request().url()).pathname); inputs.push(route.request().postData()!);
      const response = await route.fetch();
      if (attempts.length > 1) return route.fulfill({response});
      const result = await response.json(); createdId = result.creation.created.id;
      const created = p.app.draft(createdId);
      if (target === "deleted") p.app.deleteDraft(createdId, created.revision);
      else { const valid = p.app.saveDraft(createdId, created.revision, {text: '{"name":"target-exported","actions":[{"name":"sync","type":"report_status","scheduled_at":"2026-10-11 12:00:00","params":{"scope":"full"}}]}'}); p.app.exportDraft(createdId, valid.revision, valid.content); }
      return route.abort("failed");
    });
    await select(p.b, "copy", false);
    const dialog = p.b.getByRole("dialog");
    await check(dialog).toContainText("unknown");
    await check(dialog.getByRole("button", {name: "放弃", exact: true})).toBeDisabled();
    const original = stringifyJson(p.app.store.all<DraftCreation>("draft_creations")[0]);
    await sourceTerminal(p, source);
    offline = false;
    await dialog.getByRole("button", {name: "重试", exact: true}).click();
    await check(dialog).toContainText("当前目标已删除或导出");
    await check(p.b.getByLabel("计划名称", {exact: true})).toBeDisabled();
    expect(attempts).toHaveLength(1);
    if (decision === "retry") {
      await check(dialog).toContainText("新身份");
      await dialog.getByRole("button", {name: "重试", exact: true}).click();
      await check(dialog).toHaveCount(0);
      expect(attempts).toHaveLength(2); expect(attempts[1]).not.toBe(attempts[0]); expect(inputs[1]).toBe(inputs[0]);
      await check(p.b.getByLabel("计划名称", {exact: true})).toBeEnabled();
      await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("local");
    } else {
      await dialog.getByRole("button", {name: "放弃", exact: true}).click(); await check(dialog).toHaveCount(0);
      await check(p.b.getByTestId("draft-editor")).toBeFocused(); await retainedInput(p.b, c.local, source !== "conflict");
      expect(attempts).toHaveLength(1);
    }
    expect(stringifyJson(p.app.store.all<DraftCreation>("draft_creations")[0])).toBe(original);
    expect(p.app.store.all<DraftCreation>("draft_creations")).toHaveLength(decision === "retry" ? 2 : 1);
    if (target === "deleted") expect(p.app.state().drafts!.some(draft => draft.id === createdId)).toBe(false);
    else expect(p.app.draft(createdId).exportedRequestId).toBeDefined();
  },
);

const cameraAction = (name: string) => ({name, type: "camera_record", device_id: "cam", params: {type: "standard"}, policy: {max_delay_ms: 0}});
function identityContent(names: string[], reliable: boolean, name = "base"): DraftContent {
  return {text: JSON.stringify({name, actions: names.map(cameraAction)}), ...(reliable ? {automaticPreviews: {namespace: "old", next: names.length, intent: "disabled" as const, actions: names.map(id => ({id}))}} : {})};
}
const namedCard = (page: Page, name: string) => page.locator(".action-card").filter({has: page.locator("h3").filter({hasText: new RegExp(`^\\d+${name}$`)})});
async function markCard(page: Page, name: string) {
  const card = namedCard(page, name);
  await card.getByRole("button", {name: "参数 JSON", exact: true}).click();
  await card.getByRole("button", {name: "收起动作", exact: true}).click();
  await card.evaluate(node => { (window as any).markedCard = node; });
}

// 遥远接纳按位置沿用折叠/JSON，或旧草稿被动补身份资料时失败。
it.each(([false, true] as const).flatMap(reliable => (["clean", "adopt"] as const).flatMap(operation => (["add", "delete", "reorder"] as const).map(change => [reliable, operation, change] as const))))(
  "双浏览器身份可靠=%s的%s接纳远端%s按实际动作重建局部状态", async (reliable, operation, change) => {
    const p = await pair(identityContent(["A", "B"], reliable));
    await markCard(p.b, "B");
    const names = change === "add" ? ["A", "B", "C"] : change === "delete" ? ["B"] : ["B", "A"];
    const remote = identityContent(names, reliable, "remote");
    if (reliable) {
      const saved = p.draft.content.automaticPreviews!;
      remote.automaticPreviews = {...saved, actions: names.map(name => name === "C" ? {id: "independent-C"} : saved.actions[name === "A" ? 0 : 1])};
    }
    if (operation === "adopt") { await conflict(p, remote); await select(p.b, "adopt"); }
    else { expect((await p.a.request.put(`${p.url}/api/drafts/${p.draft.id}`, {data: {revision: 1, content: remote}})).status()).toBe(200); }
    await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("remote");
    await check(p.b.locator(".action-card")).toHaveCount(names.length);
    expect(await p.b.locator(".action-card h3").allTextContents()).toEqual(names.map((name, index) => `${index + 1}${name}`));
    const card = namedCard(p.b, "B");
    expect(await card.evaluate(node => node === (window as any).markedCard)).toBe(reliable);
    await check(card.getByRole("button", {name: reliable ? "展开动作" : "收起动作", exact: true})).toHaveAttribute("aria-expanded", reliable ? "false" : "true");
    if (reliable) await card.getByRole("button", {name: "展开动作", exact: true}).click();
    await check(card.getByRole("button", {name: reliable ? "参数表单" : "参数 JSON", exact: true})).toBeVisible();
    expect(p.app.draft(p.draft.id).content.automaticPreviews).toEqual(remote.automaticPreviews);
  },
);

// 计数分配回退、A/B新动作同ID或整份接纳误沿用另一新动作组件时失败。
it("真实A新增后B旧版本替换再新增，跳过中间版本仍保持独立身份和实际组件", async () => {
  const p = await pair(identityContent(["original"], true));
  const hold = gate(); p.releases.push(hold.release);
  let bWrites = 0;
  await p.b.route(`**/api/drafts/${p.draft.id}`, async route => { if (route.request().method() !== "PUT") return route.continue(); bWrites++; if (bWrites === 1) await hold.promise; return route.continue(); });
  const sent = p.b.waitForRequest(request => request.method() === "PUT");
  await p.b.getByLabel("计划名称", {exact: true}).fill("local"); await sent;
  await p.a.getByRole("button", {name: "添加动作", exact: true}).click();
  let fresh = p.a.locator(".action-card").last();
  await fresh.getByLabel("动作名称", {exact: true}).fill("Anew");
  await choose(fresh.getByLabel("动作类型", {exact: true}), "camera_record");
  await check.poll(() => p.app.draft(p.draft.id).revision).toBe(2);
  const aId = p.app.draft(p.draft.id).content.automaticPreviews!.actions[1].id;
  await markCard(p.a, "Anew");
  const observe = gate(); p.releases.push(observe.release);
  await p.a.route("**/api/state", async route => { await observe.promise; return route.continue(); });
  hold.release(); await check(p.b.getByRole("button", {name: "查看完整差异", exact: true})).toBeVisible();
  await select(p.b, "replace");
  await check.poll(() => p.app.draft(p.draft.id).revision).toBe(3);
  expect(p.app.draft(p.draft.id).content.automaticPreviews!.actions).toHaveLength(1);
  await p.b.getByRole("button", {name: "添加动作", exact: true}).click();
  fresh = p.b.locator(".action-card").last();
  await fresh.getByLabel("动作名称", {exact: true}).fill("Bnew");
  await choose(fresh.getByLabel("动作类型", {exact: true}), "camera_record");
  await check.poll(() => p.app.draft(p.draft.id).revision).toBe(4);
  const bId = p.app.draft(p.draft.id).content.automaticPreviews!.actions[1].id;
  expect(bId).not.toBe(aId);
  observe.release();
  await check(namedCard(p.a, "Bnew")).toBeVisible();
  expect(await namedCard(p.a, "Bnew").evaluate(node => node === (window as any).markedCard)).toBe(false);
  await check(namedCard(p.a, "Bnew").getByRole("button", {name: "收起动作", exact: true})).toHaveAttribute("aria-expanded", "true");
  await check(namedCard(p.a, "Bnew").getByRole("button", {name: "参数 JSON", exact: true})).toBeVisible();
});

// 替换取消条件事务检查、失败后提升版本重发或终态仍覆盖时失败。
it.each((["updated", "deleted", "exported"] as const).flatMap(change => (["retry", "abandon"] as const).map(decision => [change, decision] as const)))(
  "真实完整替换事务前A%s后%s只处理所选责任并保留完整输入", async (change, decision) => {
    const p = await pair(full()), c = await conflict(p, full("remote", "R"));
    const hold = gate(); p.releases.push(hold.release);
    let replacements = 0;
    await p.b.unroute(`**/api/drafts/${p.draft.id}`);
    await p.b.route(`**/api/drafts/${p.draft.id}`, async route => { if (route.request().method() !== "PUT") return route.continue(); replacements++; await hold.promise; return route.continue(); });
    const sent = p.b.waitForRequest(request => request.method() === "PUT");
    await select(p.b, "replace", false); await sent;
    await check(p.b.getByLabel("计划名称", {exact: true})).toBeDisabled();
    if (change === "updated") {
      const current = p.app.draft(p.draft.id);
      expect((await p.a.request.put(`${p.url}/api/drafts/${current.id}`, {data: {revision: current.revision, content: full("new-remote", "N")}})).status()).toBe(200);
    } else await sourceTerminal(p, change);
    hold.release();
    const dialog = p.b.getByRole("dialog");
    await check(dialog).toContainText(/替换.*需要继续处理/);
    await check(dialog.getByRole("button", {name: "放弃", exact: true})).toBeEnabled();
    if (decision === "retry") {
      await dialog.getByRole("button", {name: "重试", exact: true}).click();
      await check(dialog).toContainText("选择完整草稿版本");
      await check(dialog.getByRole("button", {name: "确认替换已保存版本", exact: true})).toHaveCount(0);
      if (change !== "updated") await check(dialog.getByRole("button", {name: "用本页版本替换已保存版本", exact: true})).toBeDisabled();
      await p.b.keyboard.press("Escape");
    } else { await dialog.getByRole("button", {name: "放弃", exact: true}).click(); await check(dialog).toHaveCount(0); }
    expect(replacements).toBe(1);
    await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("local");
    if (change === "updated") expect(p.app.draft(p.draft.id).content.text).toContain("new-remote");
    else await retainedInput(p.b, c.local, true);
  },
);

// 读取失败清空远端、假定删除或沿用旧确认资格时失败。
it("双浏览器可靠差异后读取不可用仍显示此前两侧且禁用覆盖", async () => {
  const p = await pair(full()); await conflict(p, full("remote", "R"));
  await p.b.route("**/api/state", route => route.abort("failed"));
  await p.b.getByRole("button", {name: "查看完整差异", exact: true}).click();
  const dialog = p.b.getByRole("dialog");
  await dialog.getByRole("button", {name: "重新核实", exact: true}).click();
  await check(dialog.getByRole("button", {name: "用本页版本替换已保存版本", exact: true})).toBeDisabled();
  await check(dialog).toContainText("local"); await check(dialog).toContainText("remote");
  await check(p.b.locator(".save-line")).toContainText(/读取|核实/);
  expect(p.app.draft(p.draft.id).revision).toBe(2);
  await p.b.keyboard.press("Escape");
  await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("local");
});

it.each(["adopt","replace"] as const)("真实两页独立新增动作后%s，持久身份及局部模式属于所选动作", async operation => {
  const p = await pair({text:'{"name":"base","actions":[{"name":"original","type":"camera_record","params":{"type":"standard"}}]}',automaticPreviews:{namespace:"shared",next:1,intent:"unset",actions:[{id:"original"}]}});
  const hold=gate(); p.releases.push(hold.release); let local!: DraftContent;
  const sent=p.b.waitForRequest(request => request.method()==="PUT" && new URL(request.url()).pathname===`/api/drafts/${p.draft.id}`);
  await p.b.route(`**/api/drafts/${p.draft.id}`,async route => {if(route.request().method()!=="PUT") return route.continue(); local=(parseClientJson(route.request().postData()!) as {content:DraftContent}).content; await hold.promise; await route.continue();});
  await p.b.getByRole("button",{name:"添加动作",exact:true}).click();
  let fresh=p.b.locator(".action-card").last(); await fresh.getByLabel("动作名称",{exact:true}).fill("Bnew"); await choose(fresh.getByLabel("动作类型",{exact:true}),"camera_record");
  local=(parseClientJson((await sent).postData()!) as {content:DraftContent}).content;
  const bId=local.automaticPreviews!.actions[1].id;
  await markCard(p.b,"Bnew");
  await p.a.getByRole("button",{name:"添加动作",exact:true}).click(); fresh=p.a.locator(".action-card").last();
  await fresh.getByLabel("动作名称",{exact:true}).fill("Anew"); await choose(fresh.getByLabel("动作类型",{exact:true}),"camera_record");
  await check.poll(() => p.app.draft(p.draft.id).revision).toBe(2);
  const aId=p.app.draft(p.draft.id).content.automaticPreviews!.actions[1].id; expect(aId).not.toBe(bId);
  hold.release(); await check(p.b.getByRole("button",{name:"查看完整差异",exact:true})).toBeVisible(); await select(p.b,operation);
  const selectedName=operation==="adopt" ? "Anew" : "Bnew", selectedId=operation==="adopt" ? aId : bId;
  await check(namedCard(p.b,selectedName)).toBeVisible();
  await check.poll(() => p.app.draft(p.draft.id).content.automaticPreviews!.actions[1].id).toBe(selectedId);
  expect(await namedCard(p.b,selectedName).evaluate(node => node===(window as any).markedCard)).toBe(operation==="replace");
  const names=(parseClientJson(p.app.draft(p.draft.id).content.text) as {actions:{name:string}[]}).actions.map(action => action.name);
  expect(names).toEqual(["original",selectedName]);
});

// beforeunload 合成 handler 正确并不证明浏览器实际提示；这里只采用真实 reload/close。
it("真实浏览器冲突输入刷新和关闭产生beforeunload提示，取消保留完整会话", async () => {
  const p = await pair(full()); await conflict(p, full("remote", "R"));
  await p.b.getByLabel("计划名称", {exact: true}).click();
  const refreshPrompt = p.b.waitForEvent("dialog");
  const refresh = p.b.reload().catch(error => { expect(String(error)).toMatch(/ERR_ABORTED|aborted|interrupted|TimeoutError/); });
  const first = await refreshPrompt; expect(first.type()).toBe("beforeunload"); await first.dismiss(); await refresh;
  await check(p.b.getByLabel("计划名称", {exact: true})).toHaveValue("local");
  await check(pendingInput(p.b, "/extra")).toHaveValue('{"L":');
  const closePrompt = p.b.waitForEvent("dialog");
  const closing = p.b.close({runBeforeUnload: true});
  const second = await closePrompt; expect(second.type()).toBe("beforeunload"); await second.dismiss(); await closing;
  expect(p.b.isClosed()).toBe(false);
  await check(pendingInput(p.b, "/extra")).toHaveValue('{"L":');
});
