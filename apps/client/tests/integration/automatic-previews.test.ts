import { afterEach, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import { initializePreviewMetadata } from "../../src/shared/automatic-previews";
const apps: Application[] = [],
  dirs: string[] = [];
const catalog = (support = true) => ({
  devices: [
    {
      device_id: "cam",
      driver_id: "test",
      actions: [
        {
          type: "camera_record",
          parameter_types: [
            {
              type: "photo",
              name: "照片",
              description: "照片",
              preview_supported: support,
              schema: {
                $schema: "https://json-schema.org/draft/2020-12/schema",
                type: "object",
                required: ["type"],
                properties: { type: { const: "photo" } },
                additionalProperties: false,
              },
            },
          ],
        },
      ],
    },
  ],
});
const capture = {
  name: "拍摄",
  type: "camera_record",
  device_id: "cam",
  scheduled_at: "2026-10-10 01:00:00",
  params: { type: "photo" },
  policy: { max_delay_ms: 0 },
};
const input = () =>
  initializePreviewMetadata(
    { text: JSON.stringify({ name: "计划", actions: [capture] }) },
    "enabled",
    "original",
  );
function setup() {
  const directory = mkdtempSync(join(tmpdir(), "client-previews-"));
  dirs.push(directory);
  writeFileSync(
    join(directory, "device-capabilities.json"),
    JSON.stringify(catalog()),
  );
  const app = new Application(directory, { next: () => 100n });
  apps.push(app);
  app.store.initialize();
  return app;
}
afterEach(() => {
  apps.splice(0).forEach((a) => a.store.close());
  dirs.splice(0).forEach((d) => rmSync(d, { recursive: true, force: true }));
});
it("空白默认开启，外部正文保持尚未设置，保存独立协调", () => {
  const app = setup();
  expect(app.createDraft().content.automaticPreviews?.intent).toBe("enabled");
  const external = app.createDraft({
    text: JSON.stringify({ name: "外部", actions: [capture] }),
  });
  expect(external.content.automaticPreviews).toBeUndefined();
  const draft = app.createDraft(input());
  expect(JSON.parse(draft.content.text).actions).toHaveLength(2);
  const saved = app.saveDraft(draft.id, draft.revision, {
    ...input(),
    automaticPreviews: { ...input().automaticPreviews!, intent: "disabled" },
  });
  expect(JSON.parse(saved.content.text).actions).toHaveLength(1);
});
it("重载协调所有未导出草稿并持久化，原请求和复制资料固定", () => {
  const app = setup(),
    one = app.createDraft(input()),
    two = app.createDraft(input());
  const fixed = app.exportDraft(
    one.id,
    one.revision,
    one.content,
    app.capabilities.version,
  );
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify(catalog(false)),
  );
  app.reloadCapabilities();
  expect(JSON.parse(app.draft(two.id).content.text).actions).toHaveLength(1);
  expect(app.draft(two.id).revision).toBe(2);
  expect(app.request(fixed.id).copyContent?.automaticPreviews?.intent).toBe(
    "enabled",
  );
  expect(app.request(fixed.id).body.actions).toHaveLength(2);
  expect(app.request(fixed.id).body).not.toHaveProperty("automaticPreviews");
  app.store.close();
  const reopened = new Application(app.store.directory, { next: () => 101n });
  apps.push(reopened);
  expect(reopened.draft(two.id).revision).toBe(2);
  const copy = reopened.copyRequest(fixed.id);
  expect(copy.content.automaticPreviews?.intent).toBe("enabled");
  expect(copy.content.automaticPreviews?.namespace).not.toBe(
    one.content.automaticPreviews?.namespace,
  );
});
it("失败加载保留关联且阻止依赖能力的首次导出，同 generation 的跨重启版本不可复用", () => {
  const app = setup(),
    d = app.createDraft(input()),
    version = app.capabilities.version;
  writeFileSync(join(app.store.directory, "device-capabilities.json"), "{");
  app.reloadCapabilities();
  expect(app.draft(d.id).content).toEqual(d.content);
  expect(() =>
    app.exportDraft(d.id, d.revision, d.content, version),
  ).toThrowError(expect.objectContaining({ code: "capabilities_unavailable" }));
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify(catalog(false)),
  );
  app.store.close();
  const reopened = new Application(app.store.directory, { next: () => 101n });
  apps.push(reopened);
  const current = reopened.draft(d.id);
  expect(reopened.capabilities.version).not.toBe(version);
  expect(() =>
    reopened.exportDraft(
      current.id,
      current.revision,
      current.content,
      version,
    ),
  ).toThrowError(expect.objectContaining({ code: "capabilities_changed" }));
});
it("导出只接受完整保存版本，事务失败不保存请求或复制资料", () => {
  const app = setup(),
    d = app.createDraft(input());
  expect(() =>
    app.exportDraft(
      d.id,
      d.revision,
      {
        ...d.content,
        automaticPreviews: { ...d.content.automaticPreviews!, intent: "unset" },
      },
      app.capabilities.version,
    ),
  ).toThrowError(expect.objectContaining({ code: "content_conflict" }));
  const set = app.store.set.bind(app.store);
  vi.spyOn(app.store, "set").mockImplementation((ns, id, value) => {
    if (ns === "drafts") throw Error("写库失败");
    set(ns, id, value);
  });
  expect(() =>
    app.exportDraft(d.id, d.revision, d.content, app.capabilities.version),
  ).toThrow("写库失败");
  expect(app.store.all("requests")).toEqual([]);
  expect(app.draft(d.id).exportedRequestId).toBeUndefined();
});

it("能力加载故障不阻止不依赖设备的开启草稿导出", () => {
  const app = setup();
  const c = initializePreviewMetadata(
    {
      text: JSON.stringify({
        name: "同步",
        actions: [
          { name: "同步", type: "report_status", params: { scope: "full" } },
        ],
      }),
    },
    "enabled",
    "sync",
  );
  const d = app.createDraft(c);
  writeFileSync(join(app.store.directory, "device-capabilities.json"), "{");
  app.reloadCapabilities();
  expect(
    app.exportDraft(d.id, d.revision, d.content, app.capabilities.version).body
      .actions,
  ).toHaveLength(1);
});
it("能力派生保留原始写入依据，后端追加拒绝错误完整预期", () => {
  const app = setup(),
    d = app.createDraft(input());
  const user = {
    ...d.content,
    text: d.content.text.replace('"计划"', '"用户计划"'),
  };
  const saved = app.saveDraft(d.id, d.revision, user);
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify(catalog(false)),
  );
  app.reloadCapabilities();
  const actual = app.draft(d.id);
  expect(actual.lastWrite).toEqual({
    revision: 2,
    input: user,
    content: saved.content,
  });
  expect(actual.revision).toBe(3);
  expect(() =>
    app.appendAction(
      d.id,
      actual.revision,
      { name: "报告", type: "report_status" },
      {
        ...actual.content,
        automaticPreviews: {
          ...actual.content.automaticPreviews!,
          intent: "disabled",
        },
      },
      app.capabilities.version,
    ),
  ).toThrowError(expect.objectContaining({ code: "content_conflict" }));
  expect(app.draft(d.id)).toEqual(actual);
});
it("复制动作和草稿使用独立身份，未完成资料保持对应", () => {
  const app = setup(),
    d = app.createDraft(input());
  const copied = app.copyAction(d.id, d.revision, 0);
  expect(
    JSON.parse(copied.content.text).actions.map((a: any) => a.name),
  ).toEqual(["拍摄", "拍摄预览", "拍摄 2", "拍摄 2预览"]);
  const other = app.copyDraft(d.id);
  expect(other.content.automaticPreviews?.intent).toBe("enabled");
  expect(
    other.content.automaticPreviews?.actions.every(
      (a) =>
        !copied.content.automaticPreviews!.actions.some((b) => a.id === b.id),
    ),
  ).toBe(true);
});

it("复制原请求使用固定业务正文并继承开关，不复制输入中的旧请求身份", () => {
  const app = setup(),
    c = input();
  c.text = c.text.replace(
    '"name":"计划"',
    '"request_id":"17","created_at":"2026-01-01 00:00:00","name":"计划"',
  );
  const d = app.createDraft(c),
    r = app.exportDraft(d.id, d.revision, d.content, app.capabilities.version),
    copy = app.copyRequest(r.id);
  expect(JSON.parse(copy.content.text)).not.toHaveProperty("request_id");
  expect(JSON.parse(copy.content.text)).not.toHaveProperty("created_at");
  expect(copy.content.automaticPreviews?.intent).toBe("enabled");
});
it("重载写库失败回滚全部草稿且不发布新能力版本", () => {
  const app = setup(),
    a = app.createDraft(input()),
    b = app.createDraft(input()),
    k = app.capabilities;
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify(catalog(false)),
  );
  const set = app.store.set.bind(app.store);
  let writes = 0;
  vi.spyOn(app.store, "set").mockImplementation((ns, id, value) => {
    if (ns === "drafts" && ++writes === 2) throw Error("写库失败");
    set(ns, id, value);
  });
  expect(() => app.reloadCapabilities()).toThrow("写库失败");
  expect(app.capabilities).toBe(k);
  expect(app.draft(a.id)).toEqual(a);
  expect(app.draft(b.id)).toEqual(b);
});
it("保存响应丢失后真实重载保留可靠提交依据供会话核实", async () => {
  const { DraftSession } = await import("../../src/web/session");
  const app = setup(),
    d = app.createDraft(input());
  const session = new DraftSession(
    d,
    {
      save: async (id, revision, content) => {
        app.saveDraft(id, revision, content);
        writeFileSync(
          join(app.store.directory, "device-capabilities.json"),
          JSON.stringify(catalog(false)),
        );
        app.reloadCapabilities();
        session.observe(app.draft(id), undefined, app.capabilities);
        throw Error("响应丢失");
      },
      read: async () => app.draft(d.id),
    },
    () => {},
  );
  session.edit({
    ...d.content,
    text: d.content.text.replace('"计划"', '"修改"'),
  });
  await session.flush();
  expect(session.saved).toBe(true);
  expect(session.revision).toBe(3);
  expect(JSON.parse(session.content.text).actions).toHaveLength(1);
  expect(session.content.text).toContain("修改");
});

it("追加响应丢失后重载通过固定完整预期与原始提交依据核实", async () => {
  const { FollowOperation } = await import("../../src/web/followup");
  const app = setup(),
    d = app.createDraft(input());
  const operation = new FollowOperation(
    d.id,
    { name: "报告", type: "report_status", params: { scope: "full" } },
    {
      capabilities: () => app.capabilities,
      create: async () => {
        throw Error("不应创建目标");
      },
      prepare: async () => app.draft(d.id),
      append: async (id, revision, action, expected) => {
        app.appendAction(
          id,
          revision,
          action,
          expected!.content,
          expected!.capabilityVersion,
        );
        writeFileSync(
          join(app.store.directory, "device-capabilities.json"),
          JSON.stringify(catalog(false)),
        );
        app.reloadCapabilities();
        throw Error("响应丢失");
      },
      read: async () => app.draft(d.id),
    },
  );
  await operation.advance();
  expect(operation.phase).toBe("done");
  expect(operation.result?.revision).toBe(3);
  expect(
    JSON.parse(operation.result!.content.text).actions.map((a: any) => a.name),
  ).toEqual(["拍摄", "报告"]);
});

it("追加遇能力变化未提交后以同目标的新可靠版本重试", async () => {
  const { FollowOperation } = await import("../../src/web/followup");
  const app = setup(),
    d = app.createDraft(input());
  let first = true;
  const operation = new FollowOperation(
    d.id,
    { name: "报告", type: "report_status", params: { scope: "full" } },
    {
      capabilities: () => app.capabilities,
      create: async () => {
        throw Error("不应创建目标");
      },
      prepare: async () => app.draft(d.id),
      append: async (id, revision, action, expected) => {
        if (first) {
          first = false;
          writeFileSync(
            join(app.store.directory, "device-capabilities.json"),
            JSON.stringify(catalog(false)),
          );
          app.reloadCapabilities();
        }
        return app.appendAction(
          id,
          revision,
          action,
          expected!.content,
          expected!.capabilityVersion,
        );
      },
      read: async () => app.draft(d.id),
    },
  );
  await operation.advance();
  expect(operation.phase).toBe("not_appended");
  expect(operation.targetId).toBe(d.id);
  await operation.advance();
  expect(operation.phase).toBe("done");
  expect(operation.result?.id).toBe(d.id);
  expect(
    JSON.parse(operation.result!.content.text).actions.map((a: any) => a.name),
  ).toEqual(["拍摄", "报告"]);
});
