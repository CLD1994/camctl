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

function cleanupHistory(watermark = 20) {
  const report = mappedReport(Buffer.from("unused"));
  report.to_wm = watermark;
  report.plans![0].actions = [
    {
      action_instance_id: "cleanup",
      name: "清理",
      type: "delete_action_outputs",
      scheduled_at: "2026-01-01 00:00:00",
      input_params: { output_ids: ["out-1", "out-2"] },
      status: "failed",
      execution: { started: true },
      error: { code: "delete_items_failed", stage: "execution", details: {} },
      result: {
        items: [
          {
            output_id: "out-1",
            status: "failed",
            error: {
              code: "output_not_found",
              stage: "output_selection",
              details: {},
            },
          },
        ],
      },
    },
  ];
  return report;
}
it.each(["earlier-terminal", "same-own", "later-item", "gap-item"] as const)(
  "历史修复：%s 不产生原文接收或确认资格",
  (kind) => {
    const { app } = setup();
    const initial = reportInput(cleanupHistory());
    app.applyReports([initial]);
    const knownGap = cleanupHistory(50);
    knownGap.report_id = 50;
    knownGap.from_wm = 40;
    app.applyReports([reportInput(knownGap)]);
    const incoming = cleanupHistory();
    incoming.report_id = 99;
    const action = incoming.plans![0].actions![0];
    if (kind === "earlier-terminal") {
      incoming.to_wm = 10;
      action.status = "succeeded";
      delete action.error;
      action.result = {
        items: [
          {
            output_id: "out-1",
            status: "succeeded",
            outcome: "absence_confirmed",
          },
          {
            output_id: "out-2",
            status: "succeeded",
            outcome: "absence_confirmed",
          },
        ],
      };
    }
    if (kind === "same-own")
      action.error = {
        code: "different_final_error",
        stage: "execution",
        details: {},
      };
    if (kind === "later-item") {
      incoming.from_wm = 20;
      incoming.to_wm = 30;
      action.result = { items: [] };
    }
    if (kind === "gap-item") {
      incoming.from_wm = 80;
      incoming.to_wm = 90;
      action.result = { items: [] };
    }
    const file = reportInput(incoming);
    const before = app.snapshot();
    app.applyReports([file]);
    const imported = app.store.get<ImportFile>("imports", file.file.id)!;
    expect(imported.status).toBe("failed");
    expect(imported.message).toBeTruthy();
    expect(app.store.report(99)).toBeUndefined();
    expect(app.store.reports()).toHaveLength(1);
    expect(app.snapshot()).toEqual(before);
    expect(Buffer.from(app.store.report(1)!.bytes)).toEqual(initial.bytes);
    expect(app.state()).toMatchObject({
      coverage: 20,
      gapTarget: 50,
      ackId: 1,
    });
    expect(app.syncParams()).toEqual({ scope: "since", after_report_id: 1 });
  },
);
it("历史修复：合法较早与同水位报告保存精确原文而保持投影", () => {
  const { app } = setup();
  const initial = reportInput(cleanupHistory());
  app.applyReports([initial]);
  const snapshot = app.snapshot();
  const earlier = cleanupHistory(10);
  earlier.report_id = 91;
  earlier.plans![0].status = "pending";
  const action = earlier.plans![0].actions![0];
  action.status = "pending";
  action.execution = { started: false };
  delete action.error;
  delete action.result;
  const same = cleanupHistory();
  same.report_id = 92;
  same.from_wm = 10;
  for (const report of [earlier, same]) {
    const input = reportInput(report);
    app.applyReports([input]);
    expect(app.store.get<ImportFile>("imports", input.file.id)?.status).toBe(
      "covered",
    );
    expect(Buffer.from(app.store.report(report.report_id)!.bytes)).toEqual(
      input.bytes,
    );
    expect(app.store.report(report.report_id)!.file_name).toBe(
      input.file.fileName,
    );
    expect(app.snapshot()).toEqual(snapshot);
  }
  expect(app.coverage()).toBe(20);
  expect(app.ackId()).toBe(1);
});
it("历史修复：同批错误文件不撤销成功且合法后续条目可推进", () => {
  const { app } = setup();
  const initial = cleanupHistory();
  initial.plans![0].status = "running";
  initial.plans![0].actions![0].status = "running";
  delete initial.plans![0].actions![0].error;
  const bad = structuredClone(initial);
  bad.report_id = 2;
  bad.from_wm = 20;
  bad.to_wm = 25;
  bad.plans![0].actions![0].result = { items: [] };
  const next = cleanupHistory(30);
  next.report_id = 3;
  next.from_wm = 20;
  next.plans![0].actions![0].result = {
    items: [
      {
        output_id: "out-1",
        status: "failed",
        error: {
          code: "output_not_found",
          stage: "output_selection",
          details: {},
        },
      },
      { output_id: "out-2", status: "succeeded", outcome: "deleted" },
    ],
  };
  const inputs = [reportInput(initial), reportInput(bad), reportInput(next)];
  app.applyReports(inputs);
  expect(
    inputs.map(
      (input) => app.store.get<ImportFile>("imports", input.file.id)?.status,
    ),
  ).toEqual(["accepted", "failed", "accepted"]);
  expect(app.coverage()).toBe(30);
  expect(app.ackId()).toBe(3);
  expect(app.store.reports().map((r) => r.report_id)).toEqual([1, 3]);
  expect(Buffer.from(app.store.report(1)!.bytes)).toEqual(inputs[0].bytes);
  expect(app.snapshot().plans![0].actions![0].result).toEqual(
    next.plans![0].actions![0].result,
  );
});
