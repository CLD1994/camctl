import { afterEach, expect, it, vi } from "vitest";
import { readdirSync, readFileSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Readable } from "node:stream";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import type { ImportFile } from "../../src/server/models";
import { DataError } from "../../src/server/database";
import { reportInput, mappedReport } from "./fixtures";
const clean: Array<() => Promise<void>> = [];
afterEach(async () => {
  for (const c of clean.splice(0)) await c();
});
function setup() {
  const dir = mkdtempSync(join(tmpdir(), "camctl-report-"));
  const app = new Application(dir);
  app.store.initialize();
  const files = new Files(app);
  clean.push(async () => {
    await files.idle();
    app.store.close();
    rmSync(dir, { recursive: true, force: true });
  });
  return { app, files };
}
function report(n: number) {
  const dir = "docs/superpowers/specs/camctl/examples/status-sync";
  const fileName = readdirSync(dir).find((f) =>
    f.startsWith(`status-report-${n}-`),
  )!;
  return { fileName, bytes: readFileSync(join(dir, fileName)) };
}
function apply(app: Application, n: number) {
  const r = report(n);
  const file: ImportFile = {
    id: crypto.randomUUID(),
    batchId: "test",
    fileName: r.fileName,
    kind: "report",
    expectedSize: r.bytes.length,
    bytesReceived: r.bytes.length,
    status: "received",
    createdAt: "test",
  };
  app.store.set("imports", file.id, file);
  app.applyReports([{ file, bytes: r.bytes }]);
  return file.id;
}
it("缺口报告不推进水位，补到较早位置仍保留缺口提示", () => {
  const { app } = setup();
  apply(app, 1);
  expect(app.coverage()).toBe(20);
  apply(app, 3);
  expect(app.state()).toMatchObject({
    coverage: 20,
    gapTarget: 45,
    historyMissing: true,
  });
  apply(app, 2);
  expect(app.state()).toMatchObject({ coverage: 40, historyMissing: true });
  apply(app, 3);
  expect(app.state()).toMatchObject({ coverage: 45, historyMissing: false });
});
it("重复报告复用原字节与接收记录", () => {
  const { app } = setup();
  apply(app, 1);
  apply(app, 1);
  expect(app.store.reports()).toHaveLength(1);
  expect(Buffer.from(app.store.report(1)!.bytes)).toEqual(report(1).bytes);
});
it("反序上传同批报告先衔接，且不等待大视频上传", async () => {
  const { app, files } = setup();
  apply(app, 1);
  const r2 = report(2),
    r3 = report(3);
  const batch = files.createBatch([
    { fileName: r3.fileName, size: r3.bytes.length, kind: "report" },
    { fileName: r2.fileName, size: r2.bytes.length, kind: "report" },
    { fileName: "d-later.mp4", size: 1000000, kind: "video" },
  ]);
  await files.upload(batch.files[0].id, Readable.from([r3.bytes]));
  await files.idle();
  expect(app.coverage()).toBe(20);
  await files.upload(batch.files[1].id, Readable.from([r2.bytes]));
  await files.idle();
  expect(app.coverage()).toBe(45);
  expect(app.store.get<ImportFile>("imports", batch.files[2].id)?.status).toBe(
    "uploading",
  );
});
it("摘要错误只产生诊断，不保存原文或水位", () => {
  const { app } = setup();
  const r = report(1);
  const file: ImportFile = {
    id: "bad",
    batchId: "test",
    fileName: r.fileName,
    kind: "report",
    expectedSize: 3,
    bytesReceived: 3,
    status: "received",
    createdAt: "test",
  };
  app.store.set("imports", file.id, file);
  app.applyReports([{ file, bytes: Buffer.from("bad") }]);
  expect(app.coverage()).toBe(0);
  expect(app.store.reports()).toEqual([]);
  expect(app.store.get<ImportFile>("imports", "bad")?.status).toBe("failed");
});
it.each(["request", "parent", "filename"])(
  "历史矛盾的缺口 %s 只能失败，不能提高G",
  (change) => {
    const { app } = setup();
    const initial = mappedReport(Buffer.from("video"));
    app.applyReports([reportInput(initial)]);
    const next = structuredClone(initial);
    next.report_id = 2;
    next.from_wm = 40;
    next.to_wm = 45;
    if (change === "request") next.plans![0].request_id = "other";
    if (change === "parent") {
      next.plans![0].plan_instance_id = "other";
      next.plans![0].request_id = "other";
      next.plans![0].plan_seq = 2;
    }
    if (change === "filename")
      next.plans![0].actions![1].deliveries![0].file_name = "d-001.mkv";
    const input = reportInput(next);
    app.applyReports([input]);
    expect(app.store.get<ImportFile>("imports", input.file.id)?.status).toBe(
      "failed",
    );
    expect(app.state()).toMatchObject({ coverage: 20, gapTarget: null });
    expect(app.store.reports()).toHaveLength(1);
  },
);
it("快照读取故障停止后续报告而不把故障当作缺口", () => {
  const { app } = setup();
  apply(app, 1);
  const inputs = [2, 3].map((n) => {
    const r = report(n);
    const file: ImportFile = {
      id: crypto.randomUUID(),
      batchId: "test",
      fileName: r.fileName,
      kind: "report",
      expectedSize: r.bytes.length,
      bytesReceived: r.bytes.length,
      status: "received",
      createdAt: "test",
    };
    app.store.set("imports", file.id, file);
    return { file, bytes: r.bytes };
  });
  const read = vi.spyOn(app, "snapshot").mockImplementationOnce(() => {
    throw new DataError("读取故障");
  });
  expect(() => app.applyReports(inputs)).toThrow();
  read.mockRestore();
  expect(app.state()).toMatchObject({ coverage: 20, gapTarget: null });
  expect(app.store.get<ImportFile>("imports", inputs[1].file.id)?.status).toBe(
    "received",
  );
  app.applyReports(inputs);
  expect(app.coverage()).toBe(45);
});
