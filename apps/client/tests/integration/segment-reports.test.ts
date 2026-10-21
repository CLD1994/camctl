import { beforeEach, afterEach, expect, it } from "vitest";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { Readable } from "node:stream";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { createHttpApp } from "../../src/server/http";
import {
  parseJson,
  parseClientJson,
  stringifyJson,
  cloneProtocolJson,
  originalNumberToken,
} from "../../src/shared/json";
import { resultProducts } from "../../src/web/result-model";
import type { StatusReport } from "../../src/shared/types";
import { segmentReport } from "../helpers/segment-reports";

let directory: string, app: Application, files: Files;
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "camctl-segment-report-"));
  app = new Application(directory);
  app.store.initialize();
  files = new Files(app);
});
afterEach(async () => {
  await files.idle();
  app.store.close();
  rmSync(directory, { recursive: true, force: true });
});
const bytes = Buffer.from(
  "same original bytes for independently delivered segments",
);
function reportWithDeliveries(): StatusReport {
  const r = segmentReport();
  const source = r.plans![0].actions![0];
  const d = source.error!.details as any;
  d.default_scope.member_count = 3;
  d.default_scope.output_ids = ["2", "10", "11"];
  for (const gap of [d.first_gap, d.gaps[0]]) {
    gap.start_key.index = 4;
    gap.end_key.index = 4;
    gap.left.output_id = "11";
    gap.right.output_id = "12";
  }
  const hash = createHash("sha256").update(bytes).digest("hex");
  for (const output of source.outputs!) {
    output.size = bytes.length;
    output.checksum = { status: "available", sha256: hash };
  }
  r.plans![0].actions!.push({
    action_instance_id: "20",
    name: "前缀取回",
    type: "obtain_action_outputs",
    status: "succeeded",
    scheduled_at: "2026-01-01 00:00:00",
    input_params: { source: { action_instance_id: "10" } },
    result: { failures: [] },
    deliveries: ["2", "10", "11"].map((id, i) => ({
      delivery_id: String(100 + i),
      output_id: id,
      source_action_instance_id: "10",
      file_name: `${100 + i}.OSV`,
      display_name: `原片${id}.OSV`,
      status: "published",
      size: bytes.length,
      sha256: hash,
    })),
  });
  return r;
}
async function uploadReport(report: StatusReport) {
  const raw = Buffer.from(stringifyJson(report));
  const name = `status-report-${report.report_id}-${createHash("sha256").update(raw).digest("hex")}.json`;
  const f = files.createBatch([
    { fileName: name, size: raw.length, kind: "report" },
  ]).files[0];
  await files.upload(f.id, Readable.from([raw]));
  await files.idle();
  return { file: app.store.get<any>("imports", f.id), raw };
}
async function uploadMedia(name: string, content = bytes) {
  const f = files.createBatch([
    { fileName: name, size: content.length, kind: "media" },
  ]).files[0];
  await files.upload(f.id, Readable.from([content]));
  await files.idle();
  return f;
}

it.each(["header", "missing-gaps", "wrong-source"])(
  "非法%s分段报告不提交原文投影覆盖和ACK",
  async (kind) => {
    const r = segmentReport();
    const source = r.plans![0].actions![0];
    const d = source.error!.details as any;
    if (kind === "header") {
      delete d.gaps;
      delete d.retained_output_ids;
      delete d.default_scope.output_ids;
    }
    if (kind === "missing-gaps") d.gaps = [];
    if (kind === "wrong-source") d.source_action_instance_id = "30";
    const result = await uploadReport(r);
    expect(result.file.status).toBe("failed");
    expect(app.store.report("1")).toBeUndefined();
    expect(app.coverage()).toBe(0);
    expect(app.ackId()).toBeNull();
    expect(app.snapshot().plans).toBeUndefined();
  },
);
it.each([
  ["preview", "later"],
  ["wrong-source", "later"],
  ["preview", "earlier"],
  ["wrong-source", "earlier"],
] as const)("待关联分段引用与%s矛盾时不提交%s报告", async (kind, boundary) => {
  const a = segmentReport();
  a.plans![0].actions![0].outputs = [];
  const b = segmentReport();
  b.report_id = "2";
  b.from_wm = 10;
  b.to_wm = 20;
  const source = b.plans![0].actions![0];
  if (kind === "preview") {
    source.outputs![0].kind = "preview";
    source.outputs![0].preview_of_output_id = "10";
  } else {
    const output = source.outputs![0];
    source.outputs = [];
    b.plans![0].actions!.push({
      ...source,
      action_instance_id: "30",
      name: "另一来源",
      error: undefined,
      status: "succeeded",
      outputs: [{ ...output, source_action_instance_id: "30" }],
    });
  }
  if (boundary === "later") {
    expect((await uploadReport(a)).file.status).toBe("accepted");
    expect((await uploadReport(b)).file.status).toBe("failed");
    expect(app.store.report("2")).toBeUndefined();
    expect(app.coverage()).toBe(10);
    expect(app.ackId()).toBe("1");
    expect(app.snapshot().plans![0].actions![0].outputs).toEqual([]);
  } else {
    b.from_wm = 0;
    source.error = { code: "future", stage: "execution", details: {} };
    expect((await uploadReport(b)).file.status).toBe("accepted");
    const before = stringifyJson(app.snapshot());
    expect((await uploadReport(a)).file.status).toBe("failed");
    expect(app.store.report("1")).toBeUndefined();
    expect(app.coverage()).toBe(20);
    expect(app.ackId()).toBe("2");
    expect(stringifyJson(app.snapshot())).toBe(before);
  }
});
it.each(["later", "earlier"] as const)(
  "同源冻结范围矛盾的%s报告不提交原文投影和ACK",
  async (boundary) => {
    const a = segmentReport(),
      b = segmentReport();
    b.report_id = "2";
    b.to_wm = 20;
    (b.plans![0].actions![0].error!.details as any).default_scope.output_ids = [
      "2",
      "12",
    ];
    const first = boundary === "later" ? a : b,
      second = boundary === "later" ? b : a;
    expect((await uploadReport(first)).file.status).toBe("accepted");
    const before = stringifyJson(app.snapshot());
    expect((await uploadReport(second)).file.status).toBe("failed");
    expect(app.store.report(second.report_id)).toBeUndefined();
    expect(stringifyJson(app.snapshot())).toBe(before);
    expect(app.coverage()).toBe(first.to_wm);
    expect(app.ackId()).toBe(first.report_id);
  },
);
it.each(["report-first", "media-first"] as const)(
  "%s同摘要异名独立核验，缺片与mismatch不影响ACK",
  async (order) => {
    const r = reportWithDeliveries();
    if (order === "report-first")
      expect((await uploadReport(r)).file.status).toBe("accepted");
    await uploadMedia("100.OSV");
    await uploadMedia("101.OSV");
    if (order === "media-first") {
      expect(files.videos().every((v) => v.status === "waiting_report")).toBe(
        true,
      );
      expect(app.ackId()).toBeNull();
      await uploadReport(r);
    }
    expect(app.ackId()).toBe("1");
    const videos = files.videos();
    expect(videos).toHaveLength(2);
    expect(new Set(videos.map((v) => v.path)).size).toBe(2);
    expect(new Set(videos.map((v) => v.id)).size).toBe(2);
    for (const v of videos) {
      expect(v.status).toBe("verified");
      expect(readFileSync((await files.openVideo(v.id)).path)).toEqual(bytes);
    }
    const obtain = app.snapshot().plans![0].actions![1];
    expect(
      resultProducts(obtain, app.snapshot().plans!, files.videos()).map(
        (p) => p.state,
      ),
    ).toEqual(["ready", "ready", "waiting"]);
    await uploadMedia("102.OSV", Buffer.from("wrong bytes"));
    expect(files.videos().find((v) => v.fileName === "102.OSV")!.status).toBe(
      "mismatch",
    );
    expect(
      resultProducts(obtain, app.snapshot().plans!, files.videos()).map(
        (p) => p.state,
      ),
    ).toEqual(["ready", "ready", "attention"]);
    expect(app.ackId()).toBe("1");
    expect(app.snapshot().plans![0].actions!.map((a) => a.status)).toEqual([
      "failed",
      "succeeded",
    ]);
    const deliveries = obtain.deliveries!;
    expect(
      deliveries.map((d) => [
        d.delivery_id,
        d.output_id,
        d.source_action_instance_id,
        d.file_name,
      ]),
    ).toEqual([
      ["100", "2", "10", "100.OSV"],
      ["101", "10", "10", "101.OSV"],
      ["102", "11", "10", "102.OSV"],
    ]);
  },
);
it("大目录号经过合并保存重启和HTTP仍保持原数学值，缺席子实体保留", async () => {
  const text = stringifyJson(reportWithDeliveries()).replaceAll(
    '"directory_number":1',
    '"directory_number":9007199254740993',
  );
  const a = parseJson(text) as StatusReport;
  const saved = await uploadReport(a);
  expect(saved.file.status).toBe("accepted");
  const b = cloneProtocolJson(a);
  b.report_id = "2";
  b.from_wm = 10;
  b.to_wm = 20;
  delete b.plans![0].actions![0].outputs;
  delete b.plans![0].actions![1].deliveries;
  await uploadReport(b);
  app.store.close();
  app = new Application(directory);
  files = new Files(app);
  await files.recover();
  await files.idle();
  const check = (r: any) => {
    const key = r.plans[0].actions[0].error.details.first_gap.start_key;
    expect(
      originalNumberToken(key, "directory_number", key.directory_number),
    ).toBe("9007199254740993");
  };
  check(app.snapshot());
  expect(app.snapshot().plans![0].actions![0].outputs).toHaveLength(4);
  expect(app.snapshot().plans![0].actions![1].deliveries).toHaveLength(3);
  expect(Buffer.from(app.store.report("1")!.bytes)).toEqual(saved.raw);
  expect(app.ackId()).toBe("2");
  const server = createHttpApp(app, files).listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  try {
    const response = await fetch(
      `http://127.0.0.1:${(server.address() as { port: number }).port}/api/state`,
    );
    check((parseClientJson(await response.text()) as any).snapshot);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
