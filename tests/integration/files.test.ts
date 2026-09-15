import { afterEach, expect, it, vi } from "vitest";
import {
  mkdtempSync,
  rmSync,
  readFileSync,
  unlinkSync,
  cpSync,
  existsSync,
  statSync,
} from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Readable } from "node:stream";
import { createHash } from "node:crypto";
import { Application } from "../../src/server/application";
import { Files, type FileEffects } from "../../src/server/files";
import type { Video } from "../../src/server/models";
import type { ImportFile } from "../../src/server/models";
import { DataError } from "../../src/server/database";
import { mappedReport, reportInput } from "./fixtures";
const dirs: string[] = [];
const contexts: Array<{ app: Application; files: Files }> = [];
function setup() {
  const dir = mkdtempSync(join(tmpdir(), "camctl-files-"));
  dirs.push(dir);
  const app = new Application(dir);
  app.store.initialize();
  const files = new Files(app);
  contexts.push({ app, files });
  return { app, files, dir };
}
afterEach(async () => {
  for (const c of contexts.splice(0)) {
    await c.files.idle();
    c.app.store.close();
  }
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});
async function upload(files: Files, name: string, bytes: Buffer) {
  const batch = files.createBatch([
    { fileName: name, size: bytes.length, kind: "video" },
  ]);
  await files.upload(batch.files[0].id, Readable.from([bytes]));
  await files.idle();
  return batch.files[0];
}
function mapping(app: Application, bytes: Buffer) {
  const r = mappedReport(bytes);
  app.applyReports([reportInput(r)]);
  return r.plans![0].actions!.find((a) => a.type === "obtain_action_outputs")!
    .deliveries![0].file_name;
}
it("完整视频无报告时保存并拒绝下载", async () => {
  const { files } = setup();
  await upload(files, "d-test.mp4", Buffer.from("video"));
  expect(files.videos()[0].status).toBe("waiting_report");
  await expect(files.openVideo(files.videos()[0].id)).rejects.toThrow();
});
it("报告映射到达后后台核验已保存视频", async () => {
  const { app, files } = setup();
  const bytes = Buffer.from("video");
  const name = mapping(app, bytes);
  await upload(files, name, bytes);
  const video = files.videos()[0];
  expect(video.status).toBe("verified");
  expect(readFileSync((await files.openVideo(video.id)).path)).toEqual(bytes);
});
it("重复相同内容只保留一个当前副本", async () => {
  const { files } = setup();
  await upload(files, "d-test.mp4", Buffer.from("same"));
  await upload(files, "d-test.mp4", Buffer.from("same"));
  expect(files.videos()).toHaveLength(1);
});
it("同名不同内容无报告时保留先前副本", async () => {
  const { files } = setup();
  await upload(files, "d-test.mp4", Buffer.from("first"));
  await upload(files, "d-test.mp4", Buffer.from("second"));
  const v = files.videos()[0];
  expect(readFileSync(v.path).toString()).toBe("first");
});
it("正确补发替换已确认错误的副本", async () => {
  const { app, files } = setup();
  const good = Buffer.from("correct");
  const name = mapping(app, good);
  await upload(files, name, Buffer.from("bad"));
  expect(files.videos()[0].status).toBe("mismatch");
  await upload(files, name, good);
  expect(files.videos()[0].status).toBe("verified");
  expect(readFileSync(files.videos()[0].path)).toEqual(good);
});
it("未完整上传不得登记完整副本", async () => {
  const { files } = setup();
  const batch = files.createBatch([
    { fileName: "d-test.mp4", kind: "video", size: 10 },
  ]);
  await expect(
    files.upload(batch.files[0].id, Readable.from([Buffer.from("short")])),
  ).rejects.toThrow();
  expect(files.videos()).toHaveLength(0);
});
it("已核验文件丢失时拒绝读取", async () => {
  const { app, files } = setup();
  const bytes = Buffer.from("video");
  await upload(files, mapping(app, bytes), bytes);
  const video = files.videos()[0];
  unlinkSync(video.path);
  await expect(files.openVideo(video.id)).rejects.toThrow();
});
it("重启恢复等待报告文件，不要求重新上传", async () => {
  const { app, files } = setup();
  await upload(files, "d-001.mp4", Buffer.from("video"));
  mapping(app, Buffer.from("video"));
  const next = new Files(app);
  await next.recover();
  await next.idle();
  expect(next.videos()[0].status).toBe("verified");
});
it("完整目录恢复到其他位置仍能读取原视频", async () => {
  const { app, files, dir } = setup();
  const bytes = Buffer.from("video");
  await upload(files, mapping(app, bytes), bytes);
  const currentId = files.videos()[0].id;
  const originalReport = Buffer.from(app.store.report(1)!.bytes);
  const content = {
    text: JSON.stringify({
      name: "同步",
      actions: [
        { name: "同步", type: "report_status", params: { scope: "full" } },
      ],
    }),
  };
  const draft = app.createDraft(content);
  const request = app.exportDraft(draft.id, draft.revision, content);
  const download = app.downloadRequest(request.id);
  const gap = mappedReport(bytes);
  gap.report_id = 2;
  gap.from_wm = 30;
  gap.to_wm = 40;
  app.applyReports([reportInput(gap)]);
  const incomplete = files.createBatch([
    { fileName: "unfinished.mp4", size: 100, kind: "video" },
  ]).files[0];
  const saved = await pending(files, "waiting.mp4", Buffer.from("waiting"));
  await files.idle();
  app.store.close();
  const restored = mkdtempSync(join(tmpdir(), "camctl-restored-"));
  dirs.push(restored);
  cpSync(dir, restored, { recursive: true });
  rmSync(dir, { recursive: true, force: true });
  const next = new Application(restored);
  const nextFiles = new Files(next);
  contexts.push({ app: next, files: nextFiles });
  await nextFiles.recover();
  await nextFiles.idle();
  const v = nextFiles.videos()[0];
  expect(v.id).toBe(currentId);
  expect(next.request(request.id)).toEqual(request);
  expect(next.downloadRequest(request.id)).toEqual(download);
  expect(Buffer.from(next.store.report(1)!.bytes)).toEqual(originalReport);
  expect(next.state()).toMatchObject({
    coverage: 20,
    gapTarget: 40,
    ackId: 1,
    historyMissing: true,
  });
  expect(next.store.get<ImportFile>("imports", incomplete.id)?.status).toBe(
    "interrupted",
  );
  expect(next.store.get<ImportFile>("imports", saved.id)?.status).toBe(
    "waiting_report",
  );
  expect(v.status).toBe("verified");
  expect(readFileSync((await nextFiles.openVideo(v.id)).path)).toEqual(bytes);
});
it("恢复旧核验任务不得覆盖已切换的正确补发", async () => {
  const { app, files } = setup();
  const good = Buffer.from("correct");
  const name = mapping(app, good);
  await upload(files, name, Buffer.from("wrong"));
  const old = files.videos()[0];
  const skip = vi.spyOn(files, "processVideo").mockResolvedValueOnce();
  const incoming = await upload(files, name, good);
  skip.mockRestore();
  await files.recover();
  await files.idle();
  const current = files.videos()[0];
  expect(current.id).toBe(incoming.id);
  expect(current.status).toBe("verified");
  expect(readFileSync((await files.openVideo(current.id)).path)).toEqual(good);
  expect(app.store.get<ImportFile>("imports", old.importId)?.status).toBe(
    "mismatch",
  );
});
it("核验事务已提交但返回未知时保留已保存的核验依据", async () => {
  const { app, files } = setup();
  await upload(files, "d-001.mp4", Buffer.from("video"));
  mapping(app, Buffer.from("video"));
  const original = app.store.transaction.bind(app.store);
  let injected = false;
  const spy = vi
    .spyOn(app.store, "transaction")
    .mockImplementation((operation) => {
      const result = original(operation);
      if (!injected && files.videos()[0]?.status === "verified") {
        injected = true;
        throw new DataError("提交结果未知");
      }
      return result;
    });
  await files.recover();
  await files.idle();
  spy.mockRestore();
  const video = files.videos()[0];
  expect(injected).toBe(true);
  expect(video.status).toBe("verified");
  expect(video.verifiedAgainst).toBeDefined();
  expect((await files.openVideo(video.id)).id).toBe(video.id);
});
it("完整上传登记提交后响应未知时不改成 interrupted", async () => {
  const { app, files } = setup();
  const original = app.store.transaction.bind(app.store);
  let injected = false;
  const spy = vi
    .spyOn(app.store, "transaction")
    .mockImplementation((operation) => {
      const result = original(operation);
      if (
        !injected &&
        app.store
          .all<{ processed: boolean }>("complete_files")
          .some((c) => !c.processed)
      ) {
        injected = true;
        throw new DataError("提交结果未知");
      }
      return result;
    });
  const file = await upload(files, "d-test.mp4", Buffer.from("video"));
  spy.mockRestore();
  expect(app.store.get<ImportFile>("imports", file.id)?.status).toBe(
    "waiting_report",
  );
  expect(files.videos()).toHaveLength(1);
});

const realEffects: FileEffects = {
  async fingerprint(path) {
    const bytes = readFileSync(path);
    return {
      size: bytes.length,
      sha256: createHash("sha256").update(bytes).digest("hex"),
    };
  },
  async readable(path, size) {
    if (statSync(path).size !== size) throw Error("大小变化");
    readFileSync(path);
  },
  async remove(path) {
    unlinkSync(path);
  },
};
async function pending(files: Files, name: string, bytes: Buffer) {
  const skip = vi.spyOn(files, "processVideo").mockResolvedValueOnce();
  try {
    return await upload(files, name, bytes);
  } finally {
    skip.mockRestore();
  }
}
it.each(["success", "failure"])(
  "异步可用性检查结束时旧副本已替换，旧检查%s不写回",
  async (outcome) => {
    const { app, files } = setup();
    const good = Buffer.from("correct");
    const name = mapping(app, good);
    await upload(files, name, Buffer.from("wrong"));
    const old = files.videos()[0];
    let release!: () => void;
    let entered!: () => void;
    const ready = new Promise<void>((resolve) => (entered = resolve));
    const barrier = new Promise<void>((resolve) => (release = resolve));
    const worker = new Files(app, {
      ...realEffects,
      async readable(path, size) {
        if (path === old.path) {
          entered();
          await barrier;
          if (outcome === "failure") throw Error("旧文件已清理");
        } else await realEffects.readable(path, size);
      },
    });
    // 单独建立旧 current 核验队列，再在屏障期间执行已登记的新副本。
    await worker.recover();
    await ready;
    const incoming = await pending(files, name, good);
    await files.processVideo(incoming.id);
    release();
    await worker.idle();
    expect(worker.videos()[0].id).toBe(incoming.id);
    expect(worker.videos()[0].status).toBe("verified");
    expect(app.store.get<ImportFile>("imports", old.id)?.status).toBe(
      "mismatch",
    );
  },
);
it.each(["accept", "duplicate", "conflict", "replace"] as const)(
  "处理%s的所有事务结果分区",
  async (branch) => {
    for (const fault of ["before", "rollback", "after", "readback"] as const) {
      const { app, files } = setup();
      const bytes = Buffer.from("correct");
      let name = "d-test.mp4";
      if (branch === "replace") name = mapping(app, bytes);
      if (branch !== "accept")
        await upload(
          files,
          name,
          branch === "duplicate" ? bytes : Buffer.from("wrong"),
        );
      const prior = files.videos()[0];
      const incoming = await pending(files, name, bytes);
      const complete = app.store.get<{ path: string; processed: boolean }>(
        "complete_files",
        incoming.id,
      )!;
      const path = join(app.store.directory, complete.path);
      const original = app.store.transaction.bind(app.store);
      let readSpy: ReturnType<typeof vi.spyOn> | undefined;
      const txn = vi
        .spyOn(app.store, "transaction")
        .mockImplementation((operation) => {
          if (fault === "before") throw new DataError("提交前失败");
          if (fault === "rollback")
            return original(() => {
              operation();
              throw new DataError("可靠回滚");
            });
          const result = original(operation);
          if (fault === "readback")
            readSpy = vi.spyOn(app.store, "get").mockImplementation(() => {
              throw new DataError("核实读取失败");
            });
          throw new DataError("提交响应未知");
        });
      try {
        if (fault === "after") await files.processVideo(incoming.id);
        else
          await expect(files.processVideo(incoming.id)).rejects.toBeInstanceOf(
            DataError,
          );
      } finally {
        txn.mockRestore();
        readSpy?.mockRestore();
      }
      const committed = fault === "after" || fault === "readback";
      expect(
        app.store.get<{ processed: boolean }>("complete_files", incoming.id)
          ?.processed,
      ).toBe(committed);
      expect(app.store.get<ImportFile>("imports", incoming.id)?.status).toBe(
        committed
          ? (
              {
                accept: "waiting_report",
                duplicate: "duplicate",
                conflict: "conflict",
                replace: "verified",
              } as const
            )[branch]
          : "received",
      );
      expect(files.videos()[0]?.id).toBe(
        committed && (branch === "accept" || branch === "replace")
          ? incoming.id
          : prior?.id,
      );
      expect(existsSync(path)).toBe(
        !(
          fault === "after" &&
          (branch === "duplicate" || branch === "conflict")
        ),
      );
      if (prior)
        expect(existsSync(prior.path)).toBe(
          !(fault === "after" && branch === "replace"),
        );
    }
  },
);
it.each(["before", "rollback", "readback"] as const)(
  "完整登记%s保持实际提交事实且拒绝fail改写",
  async (fault) => {
    const { app, files } = setup();
    const bytes = Buffer.from("video");
    const batch = files.createBatch([
      { fileName: "d-test.mp4", size: bytes.length, kind: "video" },
    ]);
    const id = batch.files[0].id;
    const original = app.store.transaction.bind(app.store);
    let readSpy: ReturnType<typeof vi.spyOn> | undefined;
    const txn = vi
      .spyOn(app.store, "transaction")
      .mockImplementation((operation) => {
        if (fault === "before") throw new DataError("提交前失败");
        if (fault === "rollback")
          return original(() => {
            operation();
            throw new DataError("回滚");
          });
        const result = original(operation);
        readSpy = vi.spyOn(app.store, "get").mockImplementation(() => {
          throw new DataError("核实读取失败");
        });
        throw new DataError("结果未知");
      });
    try {
      await expect(
        files.upload(id, Readable.from([bytes])),
      ).rejects.toBeInstanceOf(DataError);
    } finally {
      txn.mockRestore();
      readSpy?.mockRestore();
    }
    expect(app.store.get("complete_files", id) !== undefined).toBe(
      fault === "readback",
    );
    expect(app.store.get<ImportFile>("imports", id)?.status).toBe(
      fault === "readback" ? "received" : "uploading",
    );
    if (fault === "readback") {
      expect((await files.failUpload(id, "浏览器断线")).status).toBe(
        "received",
      );
      await files.recover();
      await files.idle();
      expect(files.videos()[0].status).toBe("waiting_report");
    }
  },
);
it.each(["duplicate", "conflict", "replace"] as const)(
  "成功%s之后清理失败不改变业务结果",
  async (branch) => {
    const { app, files } = setup();
    const bytes = Buffer.from("correct");
    const name = branch === "replace" ? mapping(app, bytes) : "d-test.mp4";
    await upload(
      files,
      name,
      branch === "duplicate" ? bytes : Buffer.from("wrong"),
    );
    const prior = files.videos()[0];
    const incoming = await pending(files, name, bytes);
    const worker = new Files(app, {
      ...realEffects,
      async remove() {
        throw Error("清理被占用");
      },
    });
    await worker.processVideo(incoming.id);
    expect(app.store.get<ImportFile>("imports", incoming.id)?.status).toBe(
      branch === "replace" ? "verified" : branch,
    );
    expect(worker.videos()[0].id).toBe(
      branch === "replace" ? incoming.id : prior.id,
    );
    expect(app.store.all("file_diagnostics")).toHaveLength(1);
    expect(existsSync(prior.path)).toBe(true);
  },
);
it("文件临时不可读后沿用已保存成功结果，恢复不重哈希", async () => {
  const { app, files } = setup();
  const bytes = Buffer.from("video");
  await upload(files, mapping(app, bytes), bytes);
  const video = files.videos()[0];
  let unavailable = true;
  const scan = vi.fn(realEffects.fingerprint);
  const worker = new Files(app, {
    ...realEffects,
    fingerprint: scan,
    async readable(path, size) {
      if (unavailable) throw Error("文件不可读");
      await realEffects.readable(path, size);
    },
  });
  await expect(worker.openVideo(video.id)).rejects.toThrow();
  expect(worker.videos()[0].status).toBe("unavailable");
  expect(app.store.get<ImportFile>("imports", video.id)?.status).toBe(
    "unavailable",
  );
  expect(worker.videos()[0].verifiedAgainst).toEqual(video.verifiedAgainst);
  unavailable = false;
  await worker.recover();
  await worker.idle();
  expect(worker.videos()[0].status).toBe("verified");
  expect(scan).not.toHaveBeenCalled();
  expect((await worker.failUpload(video.id, "迟到失败通知")).status).toBe(
    "verified",
  );
});

it.each(["rollback", "readback"] as const)(
  "后台核验%s不写回旧结果",
  async (fault) => {
    const { app, files } = setup();
    const bytes = Buffer.from("video");
    await upload(files, "d-001.mp4", bytes);
    mapping(app, bytes);
    const id = files.videos()[0].id;
    const original = app.store.transaction.bind(app.store);
    let readSpy: ReturnType<typeof vi.spyOn> | undefined;
    let injected = false;
    const txn = vi
      .spyOn(app.store, "transaction")
      .mockImplementation((operation) => {
        const result = original(() => {
          const r = operation();
          if (files.videos()[0]?.status === "verified") {
            injected = true;
            if (fault === "rollback") throw new DataError("核验回滚");
          }
          return r;
        });
        if (injected && fault === "readback") {
          readSpy = vi.spyOn(app.store, "get").mockImplementation(() => {
            throw new DataError("核实不可读");
          });
          throw new DataError("核验提交未知");
        }
        return result;
      });
    try {
      await files.recover();
      await files.idle();
    } finally {
      txn.mockRestore();
      readSpy?.mockRestore();
    }
    expect(injected).toBe(true);
    expect(files.videos()[0].status).toBe(
      fault === "rollback" ? "verifying" : "verified",
    );
    expect(app.store.get<ImportFile>("imports", id)?.status).toBe(
      files.videos()[0].status,
    );
    expect(files.workerError).not.toBeNull();
    await files.recover();
    await files.idle();
    expect(files.videos()[0].status).toBe("verified");
    expect(files.videos()[0].verifiedAgainst).toBeDefined();
  },
);
it("已有核验依据与实际内容矛盾时不替换为任意副本", async () => {
  const { app, files } = setup();
  const good = Buffer.from("correct");
  const name = mapping(app, good);
  await upload(files, name, Buffer.from("wrong"));
  const prior = files.videos()[0];
  const saved = app.store.get<Video>("videos", name)!;
  app.store.set("videos", name, {
    ...saved,
    verification: {
      status: "verified",
      against: {
        size: good.length,
        sha256: createHash("sha256").update(good).digest("hex"),
      },
    },
  });
  const incoming = await pending(files, name, good);
  await files.processVideo(incoming.id);
  expect(files.videos()[0].id).toBe(prior.id);
  expect(files.videos()[0].status).toBe("unavailable");
  expect(
    app.store.get<{ processed: boolean }>("complete_files", incoming.id)
      ?.processed,
  ).toBe(false);
});
