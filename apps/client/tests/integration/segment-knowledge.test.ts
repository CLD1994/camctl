import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { Readable } from "node:stream";
import { Application } from "../../src/server/application";
import { Files } from "../../src/server/files";
import { DataError } from "../../src/server/database";
import {
  stringifyJson,
  cloneProtocolJson,
  parseJson,
} from "../../src/shared/json";
import type { StatusReport } from "../../src/shared/types";
import type { ImportFile } from "../../src/server/models";
import { knowledgeReport, atWatermark } from "../helpers/segment-knowledge";
import { reportInput } from "./fixtures";

let directory: string, app: Application, files: Files;
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "camctl-segment-knowledge-"));
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
async function upload(report: StatusReport) {
  const raw = Buffer.from(stringifyJson(report));
  const fileName = `status-report-${report.report_id}-${createHash("sha256").update(raw).digest("hex")}.json`;
  const f = files.createBatch([{ fileName, size: raw.length, kind: "report" }])
    .files[0];
  await files.upload(f.id, Readable.from([raw]));
  await files.idle();
  return app.store.get<ImportFile>("imports", f.id)!;
}
function stableState() {
  return {
    snapshot: stringifyJson(app.snapshot()),
    coverage: app.coverage(),
    ack: app.ackId(),
  };
}
async function rejected(report: StatusReport) {
  const before = stableState();
  const file = await upload(report);
  expect(file.status).toBe("failed");
  expect(app.store.report(report.report_id)).toBeUndefined();
  expect(stableState()).toEqual(before);
}
for (const boundary of ["later", "covered"] as const)
  for (const reboot of [false, true])
    for (const same of [false, true])
      it(`${boundary} 知识经过省略 ${reboot ? "重启" : "同进程"} ${same ? "同ID通过" : "异ID拒绝"}`, async () => {
        const known = atWatermark(knowledgeReport([[, "11"]]), "1", 0, 10);
        const omitted = atWatermark(knowledgeReport(), "2", 0, 20);
        if (boundary === "later") {
          expect((await upload(known)).status).toBe("accepted");
          expect((await upload(omitted)).status).toBe("accepted");
          expect(
            (app.snapshot().plans![0].actions![0].error!.details as any).gaps[0]
              .right.output_id,
          ).toBeUndefined();
        } else {
          expect((await upload(omitted)).status).toBe("accepted");
          const before = stringifyJson(app.snapshot());
          expect((await upload(known)).status).toBe("covered");
          expect(stringifyJson(app.snapshot())).toBe(before);
        }
        if (reboot) await restart();
        const next = atWatermark(
          knowledgeReport([[, same ? "11" : "12"]]),
          "3",
          20,
          30,
        );
        if (same) {
          expect((await upload(next)).status).toBe("accepted");
          expect(app.coverage()).toBe(30);
          expect(app.ackId()).toBe("3");
        } else await rejected(next);
      });
it.each(
  ["same", "later", "covered"].flatMap((boundary) =>
    [false, true].map((same) => [boundary, same] as const),
  ),
)("%s 两份中省略副本不能掩盖第二个已知副本 same=%s", async (boundary, same) => {
  const old = knowledgeReport([[], ["2"]]);
  old.to_wm = boundary === "covered" ? 20 : 10;
  expect((await upload(old)).status).toBe("accepted");
  const next = atWatermark(
    knowledgeReport([[], [same ? "2" : "10"]]),
    "2",
    boundary === "later" ? 10 : 0,
    boundary === "later" ? 20 : 10,
  );
  next.plans![0].actions![1].action_instance_id = "22";
  next.plans![0].actions![1].name = "另一取回";
  if (!same) await rejected(next);
  else {
    const before = stableState();
    expect((await upload(next)).status).toBe(
      boundary === "later" ? "accepted" : "covered",
    );
    expect(app.store.report("2")).toBeDefined();
    if (boundary !== "later")
      expect(stringifyJson(app.snapshot())).toBe(before.snapshot);
  }
});
it("同份省略/两已知矛盾副本不能产生任何成功记录", async () => {
  await rejected(knowledgeReport([[], ["2"], ["10"]]));
});
for (let coordinate = 0; coordinate < 4; coordinate++)
  it(`历史各份分散补齐两缺口四侧后，第${coordinate}侧冲突仍拒绝`, async () => {
    const ids = ["2", "10", "11", "12"];
    for (let i = 0; i < 4; i++) {
      const refs: (string | undefined)[] = [];
      refs[i] = ids[i];
      expect(
        (
          await upload(
            atWatermark(
              knowledgeReport([refs]),
              String(i + 1),
              i * 10,
              (i + 1) * 10,
            ),
          )
        ).status,
      ).toBe("accepted");
    }
    await restart();
    const refs: (string | undefined)[] = [];
    refs[coordinate] = ids[(coordinate + 1) % 4];
    await rejected(atWatermark(knowledgeReport([refs]), "5", 40, 50));
  });
it.each([false, true])(
  "普通未知code替换投影后历史知识仍生效 reboot=%s",
  async (reboot) => {
    expect((await upload(knowledgeReport([["2"]]))).status).toBe("accepted");
    const unknown = atWatermark(knowledgeReport(), "2", 10, 20);
    unknown.plans![0].actions![0].error = {
      code: "future_error",
      stage: "future_stage",
      details: { arbitrary: true },
    };
    expect((await upload(unknown)).status).toBe("accepted");
    if (reboot) await restart();
    await rejected(atWatermark(knowledgeReport([["10"]]), "3", 20, 30));
  },
);
it("gap/failed/conflict不贡献知识，duplicate只复用原文", async () => {
  const first = knowledgeReport();
  expect((await upload(first)).status).toBe("accepted");
  expect((await upload(first)).status).toBe("duplicate");
  expect((await upload(knowledgeReport([["2"]]))).status).toBe("conflict");
  const gap = atWatermark(knowledgeReport([["2"]]), "2", 20, 30);
  expect((await upload(gap)).status).toBe("gap");
  expect(app.store.report("2")).toBeUndefined();
  await rejected(
    atWatermark(knowledgeReport([[], ["2"], ["11"]]), "3", 10, 20),
  );
  await restart();
  expect(
    (await upload(atWatermark(knowledgeReport([["10"]]), "4", 10, 20))).status,
  ).toBe("accepted");
});
it("事务在原文插入后失败，不保存新邻接知识或覆盖/ACK", async () => {
  expect((await upload(knowledgeReport())).status).toBe("accepted");
  const before = stableState();
  const input = reportInput(atWatermark(knowledgeReport([["2"]]), "2", 10, 20));
  app.store.set("imports", input.file.id, input.file);
  const save = app.store.saveReport.bind(app.store);
  const spy = vi
    .spyOn(app.store, "saveReport")
    .mockImplementation((...args) => {
      save(...args);
      throw new DataError("保存故障");
    });
  expect(() => app.applyReports([input])).toThrow(DataError);
  spy.mockRestore();
  expect(app.store.report("2")).toBeUndefined();
  expect(stableState()).toEqual(before);
  expect(app.store.get<ImportFile>("imports", input.file.id)?.status).toBe(
    "received",
  );
  expect(
    (await upload(atWatermark(knowledgeReport([["10"]]), "3", 10, 20))).status,
  ).toBe("accepted");
});
it.each([
  ["read", false],
  ["parse", false],
  ["read", true],
  ["parse", true],
] as const)(
  "可靠历史原文%s故障 reboot=%s 阻止接受，不归咎incoming",
  async (kind, reboot) => {
    expect((await upload(knowledgeReport())).status).toBe("accepted");
    const before = stableState();
    if (reboot) await restart();
    const saved = app.store.report("1")!;
    const spy = vi
      .spyOn(app.store, "acceptedReportSources")
      .mockImplementation(function* () {
        if (kind === "read") throw new Error("原文读取故障");
        yield { ...saved, bytes: Buffer.from("{not-json}") };
      });
    const input = reportInput(
      atWatermark(knowledgeReport([["2"]]), "2", 10, 20),
    );
    app.store.set("imports", input.file.id, input.file);
    expect(() => app.applyReports([input])).toThrow(DataError);
    spy.mockRestore();
    expect(app.store.report("2")).toBeUndefined();
    expect(stableState()).toEqual(before);
    expect(app.store.get<ImportFile>("imports", input.file.id)?.status).toBe(
      "received",
    );
  },
);
it.each(["preview", "wrong-source"])(
  "旧详情被未知code省略后，晚到%s实体仍拒绝",
  async (kind) => {
    const first = knowledgeReport([["2"]]);
    first.plans![0].actions![0].outputs = [];
    expect((await upload(first)).status).toBe("accepted");
    const omitted = atWatermark(knowledgeReport(), "2", 10, 20);
    const source = omitted.plans![0].actions![0];
    source.error = { code: "future", stage: "future", details: {} };
    source.outputs = [];
    expect((await upload(omitted)).status).toBe("accepted");
    await restart();
    const next = atWatermark(knowledgeReport(), "3", 20, 30);
    const camera = next.plans![0].actions![0];
    camera.error = { code: "future", stage: "future", details: {} };
    if (kind === "preview") {
      camera.outputs![0].kind = "preview";
      camera.outputs![0].preview_of_output_id = "10";
    } else {
      const output = camera.outputs![0];
      camera.outputs = [];
      next.plans![0].actions!.push({
        ...camera,
        action_instance_id: "30",
        name: "另一来源",
        outputs: [{ ...output, source_action_instance_id: "30" }],
      });
    }
    await rejected(next);
  },
);

it.each(["gap", "scope", "retained"])(
  "未知码省略后仍检查历史冻结%s基本事实",
  async (change) => {
    const first = knowledgeReport();
    first.plans![0].actions![0].outputs = [];
    expect((await upload(first)).status).toBe("accepted");
    const omitted = atWatermark(knowledgeReport(), "2", 10, 20);
    omitted.plans![0].actions![0].error = {
      code: "future",
      stage: "future",
      details: {},
    };
    omitted.plans![0].actions![0].outputs = [];
    expect((await upload(omitted)).status).toBe("accepted");
    await restart();
    const next = atWatermark(knowledgeReport(), "3", 20, 30);
    next.plans![0].actions![0].outputs = [];
    const d = next.plans![0].actions![0].error!.details as any;
    if (change === "gap") {
      d.gaps[0].start_key.index = 4;
      d.gaps[0].end_key.index = 4;
      d.first_gap = cloneProtocolJson(d.gaps[0]);
    }
    if (change === "scope")
      d.default_scope = { state: "empty", member_count: 0, output_ids: [] };
    if (change === "retained") {
      d.retained_output_ids.pop();
      d.retained_original_count--;
    }
    await rejected(next);
  },
);
it("原文大整数及同源ID集合重排在重启后按数学值核对", async () => {
  const make = (id: string, from: number, to: number) =>
    parseJson(
      stringifyJson(atWatermark(knowledgeReport(), id, from, to)).replaceAll(
        '"directory_number":1',
        '"directory_number":9007199254740993',
      ),
    ) as StatusReport;
  expect((await upload(make("1", 0, 10))).status).toBe("accepted");
  await restart();
  const same = make("2", 10, 20);
  (
    same.plans![0].actions![0].error!.details as any
  ).retained_output_ids.reverse();
  expect((await upload(same)).status).toBe("accepted");
  const wrong = parseJson(
    stringifyJson(make("3", 20, 30)).replaceAll(
      "9007199254740993",
      "9007199254740992",
    ),
  ) as StatusReport;
  await rejected(wrong);
});

it.each([false, true])(
  "SQLite成功原文损坏停止整批，reboot=%s",
  async (reboot) => {
    expect((await upload(knowledgeReport())).status).toBe("accepted");
    const before = stableState();
    if (reboot) await restart();
    const raw = Buffer.from("{damaged-json}");
    const name = `status-report-1-${createHash("sha256").update(raw).digest("hex")}.json`;
    const db = new DatabaseSync(app.store.filename);
    try {
      db.prepare(
        "UPDATE reports SET file_name=?,bytes=? WHERE report_id='1'",
      ).run(name, raw);
    } finally {
      db.close();
    }
    const inputs = [2, 3].map((id) =>
      reportInput(
        atWatermark(knowledgeReport(), String(id), (id - 1) * 10, id * 10),
      ),
    );
    for (const { file } of inputs) app.store.set("imports", file.id, file);
    expect(() => app.applyReports(inputs)).toThrow(DataError);
    for (const { file } of inputs)
      expect(app.store.get<ImportFile>("imports", file.id)?.status).toBe(
        "received",
      );
    expect(app.store.report("2")).toBeUndefined();
    expect(app.store.report("3")).toBeUndefined();
    expect(stringifyJson(app.store.businessState().snapshot)).toBe(
      before.snapshot,
    );
    expect(app.coverage()).toBe(before.coverage);
    expect(app.ackId()).toBe(before.ack);
  },
);

it("既有DataError在首次原文恢复边界原样传播", async () => {
  expect((await upload(knowledgeReport())).status).toBe("accepted");
  await restart();
  const failure = new DataError("可靠原文读取故障");
  vi.spyOn(app.store, "acceptedReportSources").mockImplementation(function* () {
    throw failure;
  });
  expect(() =>
    app.applyReports([
      reportInput(atWatermark(knowledgeReport(), "2", 10, 20)),
    ]),
  ).toThrow(failure);
});
