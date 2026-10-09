import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Application } from "../../src/server/application";
import type { RandomSource } from "../../src/domain/request-id";
import type { ExportedRequest } from "../../src/server/models";

class SequenceRandom implements RandomSource {
  constructor(private readonly values: bigint[]) {}
  next(): bigint {
    const value = this.values.shift();
    if (value === undefined) throw new Error("随机源耗尽");
    return value;
  }
}

const apps: Application[] = [];
const dirs: string[] = [];
function setup(random: RandomSource) {
  const dir = mkdtempSync(join(tmpdir(), "camctl-req-"));
  dirs.push(dir);
  const app = new Application(dir, random);
  apps.push(app);
  app.store.initialize();
  return app;
}
afterEach(() => {
  apps.splice(0).forEach((a) => a.store.close());
  dirs.splice(0).forEach((d) => rmSync(d, { recursive: true, force: true }));
});

const content = {
  text: JSON.stringify({
    name: "同步",
    actions: [
      { name: "完整同步", type: "report_status", params: { scope: "full" } },
    ],
  }),
};

describe("导出与请求身份", () => {
  it.each([0, 2])(
    "确认 %i 次占用后查询失败立即终止且保持旧数据",
    (conflicts) => {
      const app = setup(new SequenceRandom([11n, 1n, 2n, 3n, 4n]));
      const fixedDraft = app.createDraft(content);
      const fixed = app.exportDraft(
        fixedDraft.id,
        fixedDraft.revision,
        fixedDraft.content,
      );
      // 已占用候选来自真实 Store，只有指定查询失败由替身注入。
      for (let i = 1; i <= conflicts; i++)
        app.store.set("requests", String(i), {
          ...fixed,
          id: String(i),
          draftId: `seed-${i}`,
          body: { ...fixed.body, request_id: String(i) },
        });
      const draft = app.createDraft(content);
      const oldRequests = app.store.all("requests");
      const get = app.store.get.bind(app.store);
      const queried: string[] = [];
      const spy = vi
        .spyOn(app.store, "get")
        .mockImplementation(
          <T>(namespace: string, id: string): T | undefined => {
            if (namespace === "requests") {
              queried.push(id);
              if (queried.length === conflicts + 1)
                throw new Error("身份索引读取中断 task6");
            }
            return get<T>(namespace, id);
          },
        );
      try {
        expect(() =>
          app.exportDraft(draft.id, draft.revision, draft.content),
        ).toThrowError(
          expect.objectContaining({
            code: "request_id_fault",
            message: expect.stringContaining("身份索引读取中断 task6"),
          }),
        );
      } finally {
        spy.mockRestore();
      }
      expect(queried).toEqual(conflicts ? ["1", "2", "3"] : ["1"]);
      expect(app.store.all("requests")).toEqual(oldRequests);
      expect(app.draft(draft.id)).toEqual(draft);
      expect(app.request(fixed.id)).toEqual(fixed);
    },
  );

  it("已有固定请求重试不分配新身份也不查询候选使用情况", () => {
    const random = new SequenceRandom([11n]);
    const app = setup(random);
    const draft = app.createDraft(content);
    const fixed = app.exportDraft(draft.id, draft.revision, draft.content);
    const next = vi.spyOn(random, "next").mockImplementation(() => {
      throw new Error("不得重新抽号");
    });
    const get = app.store.get.bind(app.store);
    const lookup = vi
      .spyOn(app.store, "get")
      .mockImplementation(<T>(namespace: string, id: string): T | undefined => {
        if (namespace === "requests" && id !== fixed.id)
          throw new Error("不得查询候选");
        return get<T>(namespace, id);
      });
    try {
      expect(app.exportDraft(draft.id, 0, { text: "bad" }, null)).toEqual(
        fixed,
      );
      expect(app.downloadRequest(fixed.id)).toEqual(fixed.body);
      expect(app.store.all("requests")).toEqual([fixed]);
    } finally {
      lookup.mockRestore();
      next.mockRestore();
    }
  });

  it("随机源自身失败保留错误且不保存请求或导出标记", () => {
    const failure = new Error("系统随机源暂不可用 task6");
    const app = setup({
      next() {
        throw failure;
      },
    });
    const draft = app.createDraft(content);
    expect(() =>
      app.exportDraft(draft.id, draft.revision, draft.content),
    ).toThrow(failure);
    expect(app.store.all("requests")).toEqual([]);
    expect(app.draft(draft.id)).toEqual(draft);
  });

  it("导出正文的创建时间符合公共协议秒级格式", () => {
    const app = setup(new SequenceRandom([31n]));
    const exported = app.exportDraft(app.createDraft(content).id, 1, content);
    expect(exported.body.created_at).toMatch(
      /^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$/,
    );
  });

  it("test_export_retry_preserves_request_identity", () => {
    const random = new SequenceRandom([11n, 22n]);
    const app = setup(random);
    const draft = app.createDraft(content);
    const first = app.exportDraft(draft.id, 1, content);
    expect(first.id).toBe("11");
    expect(first.body.request_id).toBe("11");

    // 已导出草稿再次导出或下载都复用同一身份和正文。
    const retried = app.exportDraft(draft.id, 1, { text: "bad" });
    expect(retried.id).toBe("11");
    expect(retried.body).toEqual(first.body);
    expect(app.downloadRequest("11")).toEqual(first.body);
    expect(app.downloadRequest("11")).toEqual(first.body);
    expect(app.draft(draft.id).exportedRequestId).toBe("11");

    // 重启后原请求仍按原身份和正文提供。
    app.store.close();
    const reopened = new Application(app.store.directory, random);
    apps.push(reopened);
    expect(reopened.request("11").body).toEqual(first.body);
    expect(reopened.downloadRequest("11")).toEqual(first.body);

    // 新的草稿意图取得新身份。
    const second = reopened.createDraft(content);
    const next = reopened.exportDraft(second.id, 1, content);
    expect(next.id).toBe("22");
    expect(next.body.request_id).toBe("22");
  });

  it.each([
    { name: "零拒绝", value: 0n },
    { name: "越界拒绝", value: 9223372036854775808n },
  ])("随机源身份$name：导出被拒绝且不落新请求", ({ value }) => {
    const app = setup(new SequenceRandom([value]));
    const draft = app.createDraft(content);
    expect(() => app.exportDraft(draft.id, 1, content)).toThrowError(
      expect.objectContaining({ code: "request_id_fault" }),
    );
    expect(app.store.all("requests")).toEqual([]);
    expect(app.draft(draft.id).exportedRequestId).toBeUndefined();
  });

  it("最大合法值与超出 Number 安全范围的身份原样保留", () => {
    const app = setup(
      new SequenceRandom([9007199254740993n, 9223372036854775807n]),
    );
    const first = app.exportDraft(app.createDraft(content).id, 1, content);
    expect(first.id).toBe("9007199254740993");
    const second = app.exportDraft(app.createDraft(content).id, 1, content);
    expect(second.id).toBe("9223372036854775807");
    expect(second.body.request_id).toBe("9223372036854775807");
  });

  it("冲突重选耗尽返回导出错误且不返回默认身份", () => {
    const colliding = [1n, 2n, 3n, 4n, 5n, 6n, 7n, 8n, 9n];
    const app = setup(new SequenceRandom([...colliding]));
    for (const value of colliding) {
      const id = value.toString(10);
      const record: ExportedRequest = {
        id,
        draftId: `seed-${id}`,
        body: { request_id: id },
        exportedAt: "2026-01-15 08:00:00",
        handedAt: null,
      };
      app.store.set("requests", id, record);
    }
    const draft = app.createDraft(content);
    expect(() => app.exportDraft(draft.id, 1, content)).toThrowError(
      expect.objectContaining({ code: "request_id_fault" }),
    );
    // 重选耗尽不落任何新请求，也不把草稿标记为已导出。
    expect(app.store.all("requests")).toHaveLength(colliding.length);
    expect(app.draft(draft.id).exportedRequestId).toBeUndefined();
    // 部分身份冲突时重选后可用身份正常导出。
    const recovered = setup(new SequenceRandom([1n, 2n, 33n]));
    for (const value of [1n, 2n]) {
      const id = value.toString(10);
      const seed: ExportedRequest = {
        id,
        draftId: `seed-${id}`,
        body: { request_id: id },
        exportedAt: "2026-01-15 08:00:00",
        handedAt: null,
      };
      recovered.store.set("requests", id, seed);
    }
    const ok = recovered.exportDraft(
      recovered.createDraft(content).id,
      1,
      content,
    );
    expect(ok.id).toBe("33");
  });
});
