import { afterEach, expect, it, vi } from "vitest";
import {
  cpSync,
  mkdtempSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve, sep } from "node:path";
import { Application } from "../../src/server/application";
import type { DraftContent, DraftCreation } from "../../src/server/models";
import {
  cloneClientJson,
  parseClientJson,
  stringifyJson,
} from "../../src/shared/json";
import { initializePreviewMetadata } from "../../src/shared/automatic-previews";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import type { Server } from "node:http";
import { DatabaseSync } from "node:sqlite";
const applications: Application[] = [];
const directories = new Set<string>();
const httpCleanup: Array<() => Promise<void>> = [];
function setup(): Application {
  const directory = mkdtempSync(join(tmpdir(), "camctl-draft-creations-"));
  const app = new Application(directory);
  applications.push(app);
  directories.add(directory);
  app.store.initialize();
  return app;
}
afterEach(async () => {
  for (const close of httpCleanup.splice(0)) await close();
  for (const app of applications.splice(0)) app.store.close();
  for (const value of directories) {
    const directory = resolve(value);
    if (
      !directory.startsWith(resolve(tmpdir()) + sep) ||
      !basename(directory).startsWith("camctl-draft-creations-")
    )
      throw Error("清理目标不属于本测试临时目录");
    rmSync(directory, { recursive: true, force: true });
  }
  directories.clear();
  vi.restoreAllMocks();
});
const content = {
  text: '{"name":"同步","actions":[{"name":"同步","type":"report_status","params":{"scope":"full"}}]}',
};
function created(
  app: Application,
  input: DraftContent = content,
): DraftCreation {
  const result = app.createDraftCopy("attempt-1", input);
  if (result.kind !== "created") throw Error("没有创建事实");
  return result.creation;
}
async function http(app: Application) {
  const files = new Files(app);
  const server = await new Promise<Server>((done) => {
    const server = createHttpApp(app, files).listen(0, "127.0.0.1", () =>
      done(server),
    );
  });
  httpCleanup.push(async () => {
    await files.idle();
    await new Promise<void>((done, fail) =>
      server.close((error) => (error ? fail(error) : done())),
    );
  });
  return `http://127.0.0.1:${(server.address() as { port: number }).port}/api`;
}
it("同次创建在目标删除后仍返回创建事实，不复建目标", () => {
  const app = setup();
  const input = {
    text: '{"name":"本页","actions":[]}',
    pending: { "": { kind: "json" as const, text: "{" } },
  };
  const first = app.createDraftCopy("attempt-1", input);
  expect(first.kind).toBe("created");
  if (first.kind !== "created") throw Error("没有创建事实");
  app.deleteDraft(first.creation.created.id, 1);
  const retried = app.createDraftCopy("attempt-1", input);
  expect(retried).toEqual({
    kind: "created",
    creation: first.creation,
    target: { kind: "deleted" },
  });
  expect(app.store.all("drafts")).toHaveLength(0);
});
it.each(["drafts", "draft_creations"])(
  "%s 写入失败不留下部分创建",
  (namespace) => {
    const app = setup(),
      original = app.store.set.bind(app.store);
    let injected = false;
    vi.spyOn(app.store, "set").mockImplementation((ns, id, value) => {
      if (ns === namespace && !injected) {
        injected = true;
        throw Error("定向写入错误");
      }
      original(ns, id, value);
    });
    expect(() => app.createDraftCopy("attempt-1", content)).toThrow(
      "定向写入错误",
    );
    expect(app.store.all("drafts")).toEqual([]);
    expect(app.store.all("draft_creations")).toEqual([]);
    created(app);
    expect(app.store.all("drafts")).toHaveLength(1);
  },
);
it("能力派生只推进实际目标，不改写首次创建事实或原输入", () => {
  const app = setup();
  const input = initializePreviewMetadata(
    {
      text: '{"name":"照片","actions":[{"name":"拍摄","type":"camera_record","device_id":"cam","scheduled_at":"2026-10-11 01:00:00","params":{"type":"photo"}}]}',
    },
    "enabled",
    "original",
  );
  const fact = created(app, input);
  writeFileSync(
    join(app.store.directory, "device-capabilities.json"),
    JSON.stringify({
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
                  preview_supported: true,
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
    }),
  );
  expect(app.reloadCapabilities().error).toBeNull();
  const target = app.draft(fact.created.id);
  expect(target.revision).toBe(2);
  expect(JSON.parse(target.content.text).actions).toHaveLength(2);
  expect(app.createDraftCopy(fact.attemptId, input)).toEqual({
    kind: "created",
    creation: fact,
    target: { kind: "editable", draft: target },
  });
  expect(app.store.get("draft_creations", fact.attemptId)).toEqual(fact);
});
it("两个连接共同观察编辑与删除，重试不回写首次快照", () => {
  const first = setup(),
    fact = created(first),
    second = new Application(first.store.directory);
  applications.push(second);
  const updated = second.saveDraft(fact.created.id, 1, { text: "{" });
  expect(first.draftCreation(fact.attemptId)).toEqual({
    kind: "created",
    creation: fact,
    target: { kind: "editable", draft: updated },
  });
  second.deleteDraft(updated.id, updated.revision);
  expect(first.createDraftCopy(fact.attemptId, content)).toEqual({
    kind: "created",
    creation: fact,
    target: { kind: "deleted" },
  });
  expect(second.store.all("drafts")).toEqual([]);
});
it("真实 HTTP 创建事实、重启和核实保留输入与快照的原数字词元", async () => {
  const app = setup(),
    base = await http(app);
  const input = parseClientJson(
    '{"text":"{","actionVariants":{"0":[{"type":"camera_record","fields":{"params":{"amount":1.0000000000000001,"tiny":1e-999}},"pending":{}}]}}',
  ) as DraftContent;
  const response = await fetch(base + "/draft-creations/attempt-1", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: stringifyJson({ content: input }),
  });
  const body = await response.text();
  expect(response.status).toBe(200);
  expect(body.match(/1\.0000000000000001/g)).toHaveLength(3);
  expect(body.match(/1e-999/g)).toHaveLength(3);
  app.store.close();
  const reopened = new Application(app.store.directory);
  applications.push(reopened);
  const result = stringifyJson(reopened.draftCreation("attempt-1"));
  expect(result.match(/1\.0000000000000001/g)).toHaveLength(3);
  expect(result.match(/1e-999/g)).toHaveLength(3);
});
it("没有创建依据时明确返回未创建", () => {
  expect(setup().draftCreation("attempt-missing")).toEqual({
    kind: "not_created",
    attemptId: "attempt-missing",
  });
});
it.each([null, false, 0, {}, { attemptId: "other" }])(
  "无效依据 %j 不认领或重新创建",
  (value) => {
    const app = setup(),
      fact = created(app);
    app.store.set("draft_creations", fact.attemptId, value);
    for (const operation of [
      () => app.draftCreation(fact.attemptId),
      () => app.createDraftCopy(fact.attemptId, content),
    ])
      expect(operation).toThrowError(
        expect.objectContaining({ code: "data_fault" }),
      );
    expect(app.store.get("draft_creations", fact.attemptId)).toEqual(value);
    expect(app.store.all("drafts")).toEqual([fact.created]);
  },
);
it.each(["input", "created", "identity", "revision", "timestamp"])(
  "损坏创建事实的 %s 报告依据故障",
  (field) => {
    const app = setup(),
      fact = created(app),
      damaged = cloneClientJson(fact);
    if (field === "input") damaged.input.pending = [] as any;
    if (field === "created") damaged.created.content = null as any;
    if (field === "identity") damaged.attemptId = "other";
    if (field === "revision") damaged.created.revision = 2;
    if (field === "timestamp") damaged.created.createdAt = "bad";
    app.store.set("draft_creations", fact.attemptId, damaged);
    expect(() => app.draftCreation(fact.attemptId)).toThrowError(
      expect.objectContaining({ code: "data_fault" }),
    );
    expect(() => app.createDraftCopy(fact.attemptId, content)).toThrowError(
      expect.objectContaining({ code: "data_fault" }),
    );
    expect(app.store.all("drafts")).toEqual([fact.created]);
  },
);
it.each([null, false, 0, { id: "other" }])(
  "无效目标 %j 不解释为删除",
  (target) => {
    const app = setup(),
      fact = created(app);
    app.store.set("drafts", fact.created.id, target);
    expect(() => app.draftCreation(fact.attemptId)).toThrowError(
      expect.objectContaining({ code: "data_fault" }),
    );
    expect(() => app.createDraftCopy(fact.attemptId, content)).toThrowError(
      expect.objectContaining({ code: "data_fault" }),
    );
    expect(app.store.get("drafts", fact.created.id)).toEqual(target);
  },
);
it("实际目标编辑后返回当前版本且首次快照不变", () => {
  const app = setup(),
    fact = created(app);
  const updated = app.saveDraft(fact.created.id, 1, { text: "{" });
  expect(app.draftCreation(fact.attemptId)).toEqual({
    kind: "created",
    creation: fact,
    target: { kind: "editable", draft: updated },
  });
  expect(app.createDraftCopy(fact.attemptId, content)).toEqual(
    app.draftCreation(fact.attemptId),
  );
  expect(app.store.get("draft_creations", fact.attemptId)).toEqual(fact);
});
it("目标导出后保留创建事实并报告固定目标", () => {
  const app = setup(),
    fact = created(app);
  app.exportDraft(fact.created.id, 1, fact.created.content);
  const target = app.draft(fact.created.id);
  expect(app.createDraftCopy(fact.attemptId, content)).toEqual({
    kind: "created",
    creation: fact,
    target: { kind: "exported", draft: target },
  });
});
it.each(["exported", "deleted"])("原草稿 %s 不影响新草稿核实", (terminal) => {
  const app = setup(),
    original = app.createDraft(content),
    fact = created(app);
  if (terminal === "deleted") app.deleteDraft(original.id, original.revision);
  else app.exportDraft(original.id, original.revision, original.content);
  expect(app.draftCreation(fact.attemptId)).toEqual({
    kind: "created",
    creation: fact,
    target: { kind: "editable", draft: fact.created },
  });
});
it.each(["restart", "backup", "second_connection"])(
  "%s 保留同次永久创建依据",
  (mode) => {
    const app = setup(),
      fact = created(app);
    let directory = app.store.directory;
    if (mode !== "second_connection") app.store.close();
    if (mode === "backup") {
      directory = mkdtempSync(join(tmpdir(), "camctl-draft-creations-backup-"));
      directories.add(directory);
      cpSync(app.store.directory, directory, { recursive: true });
    }
    const reopened = new Application(directory);
    applications.push(reopened);
    expect(reopened.createDraftCopy(fact.attemptId, content)).toEqual({
      kind: "created",
      creation: fact,
      target: { kind: "editable", draft: fact.created },
    });
    expect(reopened.store.all("drafts")).toHaveLength(1);
  },
);
it("数据库文件缺失时查询与重复创建报告故障", () => {
  const app = setup();
  created(app);
  app.store.close();
  unlinkSync(app.store.filename);
  expect(() => app.draftCreation("attempt-1")).toThrowError(
    expect.objectContaining({ code: "data_fault" }),
  );
  expect(() => app.createDraftCopy("attempt-1", content)).toThrowError(
    expect.objectContaining({ code: "data_fault" }),
  );
});
const branches: DraftContent = {
  text: '{"name":"本页","actions":[{"type":"camera_record","params":{"type":"active","exposure":{"mode":"active"}}}]}',
  pending: { "/name": { kind: "json", text: "{" } },
  actionVariants: {
    "0": [
      {
        type: "report_status",
        fields: { params: { scope: "invalid" } },
        pending: {},
      },
    ],
  },
  parameterVariants: {
    "0": [
      {
        paramsText:
          '{"params":{"type":"inactive","amount":1.0000000000000001}}',
        pending: {},
      },
    ],
  },
  objectVariants: {
    "0": {
      "/exposure": {
        discriminator: "mode",
        variants: [
          {
            valueText: '{"value":{"mode":"inactive","amount":0}}',
            pending: {},
          },
        ],
      },
    },
  },
  automaticPreviews: {
    namespace: "original",
    next: 1,
    intent: "unset",
    actions: [{ id: "source-1" }],
  },
};
it.each([
  "pending",
  "actionVariants",
  "parameterVariants",
  "objectVariants",
  "automaticPreviews",
  "numeric_token",
  "combined",
])("同次身份的 %s 差异不能借正文相同认领结果", (field) => {
  const input = cloneClientJson(branches),
    changed = cloneClientJson(input);
  if (field === "numeric_token") {
    changed.actionVariants!["0"][0].fields = parseClientJson(
      '{"params":{"scope":"invalid","amount":1.0000000000000001}}',
    ) as any;
    input.actionVariants!["0"][0].fields = {
      params: { scope: "invalid", amount: 1 },
    };
    const app = setup(),
      other = created(app, input);
    expect(() => app.createDraftCopy(other.attemptId, changed)).toThrowError(
      expect.objectContaining({ code: "creation_conflict", status: 409 }),
    );
    return;
  }
  const app = setup(),
    fact = created(app, input);
  if (field === "pending" || field === "combined")
    changed.pending!["/name"].text = "[";
  if (field === "actionVariants")
    changed.actionVariants!["0"][0].fields.params = { scope: "other" };
  if (field === "parameterVariants" || field === "combined")
    changed.parameterVariants!["0"][0].paramsText =
      '{"params":{"type":"inactive","amount":1}}';
  if (field === "objectVariants")
    changed.objectVariants!["0"]["/exposure"].variants[0].valueText =
      '{"value":{"mode":"inactive","amount":1}}';
  if (field === "automaticPreviews")
    changed.automaticPreviews!.intent = "disabled";
  expect(() => app.createDraftCopy(fact.attemptId, changed)).toThrowError(
    expect.objectContaining({ code: "creation_conflict", status: 409 }),
  );
  expect(app.store.all("drafts")).toEqual([fact.created]);
  expect(app.store.get("draft_creations", fact.attemptId)).toEqual(fact);
});
it("合法私有资料与业务非法原文完整保存为独立身份", () => {
  const app = setup(),
    fact = created(app, branches);
  expect(fact.input).toEqual(branches);
  expect(fact.created.content.automaticPreviews!.namespace).not.toBe(
    "original",
  );
  expect(fact.created.content.automaticPreviews!.actions[0].id).not.toBe(
    "source-1",
  );
  expect(fact.created.content.parameterVariants).toEqual(
    branches.parameterVariants,
  );
  expect(() =>
    app.exportDraft(fact.created.id, 1, fact.created.content),
  ).toThrow();
});
it.each([
  null,
  { text: 0 },
  { text: "{", pending: [] },
  { text: "{", parameterVariants: [] },
  { text: "{", objectVariants: [] },
])("结构非法输入 %j 不留下任何记录", (input) => {
  const app = setup();
  expect(() => app.createDraftCopy("attempt-1", input as any)).toThrowError(
    expect.objectContaining({ code: "invalid_content" }),
  );
  expect(app.store.all("drafts")).toEqual([]);
  expect(app.store.all("draft_creations")).toEqual([]);
});
it("同身份的非法编辑结构仍明确拒绝并保留既有事实", () => {
  const app = setup(),
    fact = created(app);
  expect(() =>
    app.createDraftCopy(fact.attemptId, { text: "{", pending: [] } as any),
  ).toThrowError(
    expect.objectContaining({ code: "invalid_content", status: 400 }),
  );
  expect(app.store.get("draft_creations", fact.attemptId)).toEqual(fact);
  expect(app.store.all("drafts")).toEqual([fact.created]);
});
it("HTTP 同身份并发与重复 PUT 仅创建一次，GET 按身份读取", async () => {
  const app = setup(),
    base = await http(app),
    url = base + "/draft-creations/attempt-1";
  expect(await (await fetch(url)).json()).toEqual({
    kind: "not_created",
    attemptId: "attempt-1",
  });
  const replies = await Promise.all(
    Array.from({ length: 4 }, () =>
      fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: stringifyJson({ content: branches }),
      }),
    ),
  );
  expect(replies.map((reply) => reply.status)).toEqual([200, 200, 200, 200]);
  const results = await Promise.all(replies.map((reply) => reply.json()));
  expect(
    results.every(
      (result) => JSON.stringify(result) === JSON.stringify(results[0]),
    ),
  ).toBe(true);
  expect(await (await fetch(url)).json()).toEqual(results[0]);
  expect(app.store.all("drafts")).toHaveLength(1);
  const conflict = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: stringifyJson({ content: { ...branches, text: "{" } }),
  });
  expect(conflict.status).toBe(409);
  expect((await conflict.json()).code).toBe("creation_conflict");
  expect(await (await fetch(base + "/state")).text()).not.toContain(
    '"draft_creations"',
  );
});
it("HTTP 损坏依据或数据库不可用返回故障，不能解释为未创建", async () => {
  const app = setup(),
    fact = created(app),
    base = await http(app),
    url = base + "/draft-creations/attempt-1";
  app.store.set("draft_creations", fact.attemptId, null);
  for (const method of ["GET", "PUT"]) {
    const response = await fetch(url, {
      method,
      ...(method === "PUT"
        ? {
            headers: { "Content-Type": "application/json" },
            body: stringifyJson({ content }),
          }
        : {}),
    });
    expect(response.status).toBe(503);
    expect((await response.json()).code).toBe("data_fault");
  }
  app.store.close();
  unlinkSync(app.store.filename);
  const response = await fetch(base + "/draft-creations/missing");
  expect(response.status).toBe(503);
  expect((await response.json()).code).toBe("data_fault");
});
it.each(["draft_creations", "drafts"])(
  "持久化 %s 原文无法解析时查询和重试报告依据故障",
  async (namespace) => {
    const app = setup(),
      fact = created(app),
      base = await http(app);
    const id =
      namespace === "draft_creations" ? fact.attemptId : fact.created.id;
    const connection = new DatabaseSync(app.store.filename);
    try {
      connection
        .prepare("UPDATE records SET value=? WHERE namespace=? AND id=?")
        .run("{", namespace, id);
    } finally {
      connection.close();
    }
    for (const method of ["GET", "PUT"]) {
      const response = await fetch(base + "/draft-creations/attempt-1", {
        method,
        ...(method === "PUT"
          ? {
              headers: { "Content-Type": "application/json" },
              body: stringifyJson({ content }),
            }
          : {}),
      });
      expect(response.status).toBe(503);
      expect((await response.json()).code).toBe("data_fault");
    }
  },
);
