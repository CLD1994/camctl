import { afterEach, describe, expect, it } from "vitest";
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
    actions: [{ name: "完整同步", type: "report_status", params: { scope: "full" } }],
  }),
};

describe("导出与请求身份", () => {
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
    const ok = recovered.exportDraft(recovered.createDraft(content).id, 1, content);
    expect(ok.id).toBe("33");
  });
});
