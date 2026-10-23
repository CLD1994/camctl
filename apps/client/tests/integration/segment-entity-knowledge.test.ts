import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { Readable } from "node:stream";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { DataError } from "../../src/server/database";
import { parseReport } from "../../src/domain/reports";
import { stringifyJson } from "../../src/shared/json";
import type { StatusReport } from "../../src/shared/types";
import type { ImportFile } from "../../src/server/models";
import { reportInput } from "./fixtures";
import {
  atWatermark,
  segmentFactsOnly,
  segmentEntitiesOnly,
  type SegmentEntityCase,
} from "../helpers/segment-knowledge";

let directory: string, app: Application, files: Files;
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "camctl-segment-entity-"));
  app = new Application(directory);
  app.store.initialize();
  files = new Files(app);
});
afterEach(async () => {
  await files.idle();
  app.store.close();
  vi.restoreAllMocks();
  rmSync(directory, { recursive: true, force: true });
});
async function restart() {
  await files.idle();
  app.store.close();
  app = new Application(directory);
  files = new Files(app);
}
function encoded(report: StatusReport) {
  const bytes = Buffer.from(stringifyJson(report));
  const fileName = `status-report-${report.report_id}-${createHash("sha256").update(bytes).digest("hex")}.json`;
  parseReport(fileName, bytes); // 每份夹具自身必须严格合法，避免其他错误掩盖 F/E 关联。
  return { bytes, fileName };
}
async function upload(report: StatusReport) {
  const { bytes, fileName } = encoded(report);
  const f = files.createBatch([
    { fileName, size: bytes.length, kind: "report" },
  ]).files[0];
  await files.upload(f.id, Readable.from([bytes]));
  await files.idle();
  return app.store.get<ImportFile>("imports", f.id)!;
}
async function uploadBatch(reports: StatusReport[]) {
  const raw = reports.map(encoded);
  const batch = files.createBatch(
    raw.map(({ bytes, fileName }) => ({
      fileName,
      size: bytes.length,
      kind: "report" as const,
    })),
  );
  for (let i = 0; i < raw.length; i++)
    await files.upload(batch.files[i].id, Readable.from([raw[i].bytes]));
  await files.idle();
  return batch.files.map(({ id }) => app.store.get<ImportFile>("imports", id)!);
}
const empty = (id: string, to: number, from = 0): StatusReport => ({
  report_id: id,
  from_wm: from,
  to_wm: to,
});
function stable() {
  return {
    snapshot: stringifyJson(app.snapshot()),
    coverage: app.coverage(),
    ack: app.ackId(),
  };
}
function assertNoCommit(
  report: StatusReport,
  before: ReturnType<typeof stable>,
) {
  expect(app.store.report(report.report_id)).toBeUndefined();
  expect(stable()).toEqual(before);
}
function inputs(reports: StatusReport[]) {
  return reports.map((r) => {
    encoded(r);
    const input = reportInput(r);
    app.store.set("imports", input.file.id, input.file);
    return input;
  });
}
const cases: SegmentEntityCase[] = [
  "record",
  "photo",
  "timelapse",
  "source-absent",
  "original",
  "preview",
  "repaired",
  "wrong-source",
  "output-absent",
];
const invalid: SegmentEntityCase[] = [
  "photo",
  "preview",
  "repaired",
  "wrong-source",
];
for (const location of ["current", "covered"] as const)
  for (const direction of ["E-F", "F-E"] as const)
    for (const reboot of [false, true])
      it.each(cases)(
        `${location} ${direction} reboot=${reboot} %s 的所有已知实体观察都参加关联`,
        async (kind) => {
          const first =
            direction === "E-F"
              ? segmentEntitiesOnly(kind)
              : segmentFactsOnly();
          const next =
            direction === "E-F"
              ? segmentFactsOnly()
              : segmentEntitiesOnly(kind);
          if (location === "covered")
            expect((await upload(empty("2", 20))).status).toBe("accepted");
          expect((await upload(first)).status).toBe(
            location === "covered" ? "covered" : "accepted",
          );
          const coverage = location === "covered" ? 20 : 10;
          atWatermark(next, "3", coverage, coverage + 10);
          const before = stable();
          if (reboot) await restart();
          const result = await upload(next);
          if (invalid.includes(kind)) {
            expect(result.status).toBe("failed");
            assertNoCommit(next, before);
          } else {
            expect(result.status).toBe("accepted");
            expect(app.store.report("3")).toBeDefined();
          }
        },
      );
for (const reverse of [false, true])
  for (const reboot of [false, true])
    it.each(invalid)(
      `同一实体多个covered观察不得覆盖反证 reverse=${reverse} reboot=${reboot} %s`,
      async (kind) => {
        expect((await upload(empty("5", 50))).status).toBe("accepted");
        const bad = segmentEntitiesOnly(kind);
        const good = segmentEntitiesOnly(
          kind === "photo" ? "record" : "original",
        );
        const reports = reverse ? [good, bad] : [bad, good];
        for (let i = 0; i < reports.length; i++)
          expect(
            (
              await upload(
                atWatermark(reports[i], String(i + 1), 0, (i + 1) * 10),
              )
            ).status,
          ).toBe("covered");
        const before = stable();
        if (reboot) await restart();
        const facts = atWatermark(segmentFactsOnly(), "6", 50, 60);
        expect((await upload(facts)).status).toBe("failed");
        assertNoCommit(facts, before);
      },
    );
for (const reverse of [false, true])
  for (const reboot of [false, true])
    it.each(invalid)(
      `可靠多原文F/E自相矛盾是DataError整批停止 reverse=${reverse} reboot=${reboot} %s`,
      async (kind) => {
        expect((await upload(empty("5", 50))).status).toBe("accepted");
        const source = segmentEntitiesOnly(kind === "photo" ? kind : "record");
        const output =
          kind === "photo" ? empty("2", 20) : segmentEntitiesOnly(kind);
        const originals = [
          atWatermark(source, "1", 0, 10),
          atWatermark(output, "2", 0, 20),
          atWatermark(segmentFactsOnly(), "3", 0, 30),
        ];
        // 模拟可靠库已含分别严格合法、合起来矛盾的旧原文；不冒充正常接收。
        for (const r of reverse ? originals.reverse() : originals) {
          const raw = encoded(r);
          app.store.saveReport(
            r.report_id,
            raw.fileName,
            raw.bytes,
            r.from_wm,
            r.to_wm,
          );
        }
        const before = stable();
        if (reboot) await restart();
        const batch = inputs([empty("6", 60, 50), empty("7", 70, 60)]);
        expect(() => app.applyReports(batch)).toThrow(DataError);
        for (const { file } of batch)
          expect(app.store.get<ImportFile>("imports", file.id)?.status).toBe(
            "received",
          );
        assertNoCommit(empty("6", 60), before);
        expect(app.store.report("7")).toBeUndefined();
      },
    );
it.each(["preview", "original"] as const)(
  "同批先成功covered %s 的E立即参与下一份F",
  async (kind) => {
    expect((await upload(empty("5", 50))).status).toBe("accepted");
    const before = stable();
    const facts = atWatermark(segmentFactsOnly(), "6", 50, 60);
    const batch = await uploadBatch([segmentEntitiesOnly(kind), facts]);
    expect(batch[0].status).toBe("covered");
    expect(app.store.report("1")).toBeDefined();
    expect(batch[1].status).toBe(kind === "preview" ? "failed" : "accepted");
    if (kind === "preview") assertNoCommit(facts, before);
  },
);
it("gap/conflict不贡献E，incoming failed不污染知识且可继续独立输入", async () => {
  const base = empty("5", 50);
  expect((await upload(base)).status).toBe("accepted");
  expect((await upload(base)).status).toBe("duplicate");
  expect(
    (await upload(atWatermark(segmentEntitiesOnly("preview"), "5", 0, 50)))
      .status,
  ).toBe("conflict");
  expect(
    (await upload(atWatermark(segmentEntitiesOnly("preview"), "8", 60, 70)))
      .status,
  ).toBe("gap");
  await restart();
  expect(
    (await upload(atWatermark(segmentFactsOnly(), "6", 50, 60))).status,
  ).toBe("accepted");
  const before = stable();
  const batch = await uploadBatch([
    atWatermark(segmentEntitiesOnly("preview"), "7", 60, 70),
    atWatermark(segmentEntitiesOnly("original"), "9", 60, 70),
  ]);
  expect(batch[0].status).toBe("failed");
  expect(app.store.report("7")).toBeUndefined();
  expect(batch[1].status).toBe("accepted");
  expect(app.coverage()).toBe(70);
  expect(app.ackId()).toBe("9");
  expect(app.store.report("8")).toBeUndefined();
  expect(before.coverage).toBe(60);
});
it("事务原文插入后失败不留下E，后续F可以成功", async () => {
  expect((await upload(empty("5", 50))).status).toBe("accepted");
  const before = stable();
  const batch = inputs([
    atWatermark(segmentEntitiesOnly("preview"), "6", 50, 60),
  ]);
  const save = app.store.saveReport.bind(app.store);
  const spy = vi
    .spyOn(app.store, "saveReport")
    .mockImplementation((...args) => {
      save(...args);
      throw new DataError("插入后故障");
    });
  expect(() => app.applyReports(batch)).toThrow(DataError);
  spy.mockRestore();
  assertNoCommit(empty("6", 60), before);
  expect(app.store.get<ImportFile>("imports", batch[0].file.id)?.status).toBe(
    "received",
  );
  expect(
    (await upload(atWatermark(segmentFactsOnly(), "7", 50, 60))).status,
  ).toBe("accepted");
});
for (const reverse of [false, true])
  for (const reboot of [false, true])
    it(`F/source/output跨原文分散并补全两个gap四侧 reverse=${reverse} reboot=${reboot}`, async () => {
      expect((await upload(empty("20", 200))).status).toBe("accepted");
      const ids = ["2", "10", "11", "12"];
      const source = segmentEntitiesOnly("record");
      const output = segmentEntitiesOnly("original");
      const camera = output.plans![0].actions![0];
      camera.outputs = ids.map((id) => ({
        ...camera.outputs![0],
        output_id: id,
      }));
      const rows = [
        atWatermark(source, "1", 0, 10),
        atWatermark(output, "2", 0, 20),
        ...ids.map((id, i) => {
          const refs: (string | undefined)[] = [];
          refs[i] = id;
          return atWatermark(
            segmentFactsOnly(refs, String(100 + i)),
            String(3 + i),
            0,
            (3 + i) * 10,
          );
        }),
      ];
      for (const row of reverse ? rows.reverse() : rows)
        expect((await upload(row)).status).toBe("covered");
      const before = stable();
      if (reboot) await restart();
      const next = atWatermark(
        segmentFactsOnly([, , "12"], "200"),
        "21",
        200,
        210,
      );
      expect((await upload(next)).status).toBe("failed");
      assertNoCommit(next, before);
      expect((await upload(empty("22", 210, 200))).status).toBe("accepted");
    });

for (const direction of ["E-F", "F-E"] as const)
  it.each([...invalid, "original", "source-absent"] as SegmentEntityCase[])(
    `同水位covered ${direction} %s 保留全部可靠身份`,
    async (kind) => {
      expect((await upload(empty("2", 20))).status).toBe("accepted");
      const first =
        direction === "E-F" ? segmentEntitiesOnly(kind) : segmentFactsOnly();
      const next =
        direction === "E-F" ? segmentFactsOnly() : segmentEntitiesOnly(kind);
      expect((await upload(first)).status).toBe("covered");
      atWatermark(next, "3", 0, 20);
      const before = stable();
      const result = await upload(next);
      if (invalid.includes(kind)) {
        expect(result.status).toBe("failed");
        assertNoCommit(next, before);
      } else {
        expect(result.status).toBe("covered");
        expect(app.store.report("3")).toBeDefined();
        expect(stringifyJson(app.snapshot())).toBe(before.snapshot);
      }
    },
  );
