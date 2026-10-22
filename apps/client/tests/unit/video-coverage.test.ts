import { describe, expect, it } from "vitest";
import { createHash } from "node:crypto";
import {
  parseReport,
  validateReport,
  validateReportAgainstHistory,
} from "../../src/domain/reports";
import { actionIssueText, resultNotes } from "../../src/web/result-model";
import type {
  Output,
  ReportAction,
  StatusReport,
} from "../../src/shared/types";
import { parseJson } from "../../src/shared/json";
import { scopeReport, segmentReport } from "../helpers/segment-reports";

type Coverage = {
  metric: "duration_s" | "frames";
  target: number;
  confirmed: number | null;
  status: "not_performed" | "reached" | "confirmed_short" | "unconfirmed";
  checked_output_ids: string[];
  default_scope: {
    state: "unknown" | "empty" | "nonempty";
    member_count: number | null;
    output_ids: string[];
  };
  reason?: { code: string; output_id?: string; message?: string };
};

const coverage = (changes: Partial<Coverage> = {}): Coverage => ({
  metric: "duration_s",
  target: 60,
  confirmed: null,
  status: "not_performed",
  checked_output_ids: [],
  default_scope: {
    state: "nonempty",
    member_count: 1,
    output_ids: ["11"],
  },
  ...changes,
});

function original(id: string, mediaType = "video/mp4"): Output {
  return {
    output_id: id,
    source_action_instance_id: "10",
    kind: "original",
    media_type: mediaType,
    availability: "available",
    cleanup: { status: "not_requested" },
    checksum: { status: "not_obtained" },
    media: { check_status: "not_performed", duration: { status: "unknown" } },
  };
}

function report(
  value: Coverage,
  type: "camera_record" | "camera_timelapse" = "camera_record",
  outputs: Output[] = [original("11")],
): StatusReport {
  const action: ReportAction = {
    action_instance_id: "10",
    name: "录像",
    type,
    device_id: "cam0",
    scheduled_at: "2026-10-23 00:00:00",
    input_params: { type: "capture" },
    effective_params: { type: "capture" },
    policy: { max_delay_ms: 0 },
    status: "succeeded",
    result: { coverage: value },
    outputs,
  };
  return {
    report_id: "1",
    from_wm: 0,
    to_wm: 10,
    plans: [
      {
        plan_instance_id: "1",
        request_id: "1",
        created_at: "2026-10-23 00:00:00",
        name: "录像计划",
        status: "completed",
        actions: [action],
      },
    ],
  };
}

const source = (value: StatusReport) => value.plans![0].actions![0];

describe("视频覆盖报告", () => {
  it("消费未执行、达到、完整不足和未知四态，且未知不改写动作成功", () => {
    const notPerformed = report(coverage());
    expect(() => validateReport(notPerformed)).not.toThrow();
    expect(
      resultNotes(source(notPerformed)).some((note) =>
        /目标 60秒/.test(note.text),
      ),
    ).toBe(true);

    const reached = report(
      coverage({
        confirmed: 67,
        status: "reached",
        checked_output_ids: ["11"],
      }),
    );
    expect(() => validateReport(reached)).not.toThrow();
    expect(
      resultNotes(source(reached)).some((note) =>
        /已达到覆盖目标/.test(note.text),
      ),
    ).toBe(true);

    const short = report(
      coverage({
        confirmed: 0,
        status: "confirmed_short",
        checked_output_ids: [],
        default_scope: { state: "empty", member_count: 0, output_ids: [] },
      }),
      "camera_record",
      [],
    );
    expect(() => validateReport(short)).not.toThrow();
    expect(
      resultNotes(source(short)).some(
        (note) => note.error && /完整内容不足/.test(note.text),
      ),
    ).toBe(true);

    const unknown = report(
      coverage({
        confirmed: 45,
        status: "unconfirmed",
        checked_output_ids: ["11"],
        default_scope: {
          state: "nonempty",
          member_count: 1,
          output_ids: ["11"],
        },
        reason: { code: "missing_position", message: "下一位置无法确认" },
      }),
    );
    expect(() => validateReport(unknown)).not.toThrow();
    expect(source(unknown).status).toBe("succeeded");
    expect(
      resultNotes(source(unknown)).some(
        (note) => note.error && /下一位置无法确认/.test(note.text),
      ),
    ).toBe(true);
  });

  it("原生延时按整数帧量纲展示，照片不能进入覆盖范围", () => {
    const timelapse = report(
      coverage({
        metric: "frames",
        target: 151,
        confirmed: 165,
        status: "reached",
        checked_output_ids: ["11"],
      }),
      "camera_timelapse",
    );
    expect(() => validateReport(timelapse)).not.toThrow();
    expect(
      resultNotes(source(timelapse)).some((note) =>
        /目标 151帧/.test(note.text),
      ),
    ).toBe(true);

    const photo = report(
      coverage({
        metric: "frames",
        target: 151,
        default_scope: {
          state: "nonempty",
          member_count: 1,
          output_ids: ["11"],
        },
      }),
      "camera_timelapse",
      [original("11", "image/jpeg")],
    );
    expect(() => validateReport(photo)).toThrow(/原片视频/);
  });

  it.each(["camera_record", "camera_timelapse"] as const)(
    "%s 的视频范围允许原片类型未知，但拒绝明确的非视频类型",
    (type) => {
      const value = coverage(
        type === "camera_timelapse" ? { metric: "frames", target: 151 } : {},
      );
      const output = original("11");
      delete output.media_type;
      expect(() => validateReport(report(value, type, [output]))).not.toThrow();

      output.media_type = "image/jpeg";
      expect(() => validateReport(report(value, type, [output]))).toThrow(
        /原片视频/,
      );
    },
  );

  it("拒绝状态、量纲、数值或默认范围相互矛盾", () => {
    const wrongMetric = report(coverage({ metric: "frames" }));
    expect(() => validateReport(wrongMetric)).toThrow();

    const shortButReached = report(
      coverage({
        confirmed: 59,
        status: "reached",
        checked_output_ids: ["11"],
      }),
    );
    expect(() => validateReport(shortButReached)).toThrow(/覆盖状态/);

    const reachedButUnconfirmed = report(
      coverage({
        confirmed: 60,
        status: "unconfirmed",
        checked_output_ids: ["11"],
      }),
    );
    expect(() => validateReport(reachedButUnconfirmed)).toThrow(/未确认覆盖/);

    const wrongScopeCount = report(
      coverage({
        default_scope: {
          state: "nonempty",
          member_count: 2,
          output_ids: ["11"],
        },
      }),
    );
    expect(() => validateReport(wrongScopeCount)).toThrow(/数量/);
  });

  it("精确比较时长词元，不把小数舍入成达到目标", () => {
    const value = report(
      coverage({
        confirmed: 60,
        status: "reached",
        checked_output_ids: ["11"],
      }),
    );
    const text = JSON.stringify(value).replace(
      '"target":60',
      '"target":60.0000000000000000001',
    );
    const parsed = parseJson(text) as StatusReport;
    expect(() => validateReport(parsed)).toThrow(/覆盖状态/);
  });

  it("报告字节导入和同水位历史核对保留精确覆盖数字", () => {
    const bytes = Buffer.from(
      JSON.stringify(
        report(
          coverage({
            confirmed: 60,
            status: "reached",
            checked_output_ids: ["11"],
          }),
        ),
      ),
    );
    const digest = createHash("sha256").update(bytes).digest("hex");
    const imported = parseReport(`status-report-1-${digest}.json`, bytes);
    expect(() => validateReport(imported)).not.toThrow();

    const changedText = bytes
      .toString("utf8")
      .replace('"target":60', '"target":60.0000000000000000001')
      .replace('"confirmed":60', '"confirmed":60.0000000000000000001');
    const changed = parseJson(changedText) as StatusReport;
    changed.report_id = "2";
    expect(() => validateReportAgainstHistory(imported, changed)).toThrow(
      /覆盖事实/,
    );
  });

  it("允许覆盖在动作完成时首次出现，之后保持不可改写", () => {
    const running = report(coverage());
    const runningAction = source(running);
    runningAction.status = "running";
    delete runningAction.result;
    runningAction.outputs = [];
    running.plans![0].status = "running";
    running.to_wm = 10;

    const completed = report(
      coverage({
        confirmed: 67,
        status: "reached",
        checked_output_ids: ["11"],
      }),
    );
    completed.report_id = "2";
    completed.from_wm = 10;
    completed.to_wm = 20;
    expect(() =>
      validateReportAgainstHistory(running, completed),
    ).not.toThrow();

    const changed = structuredClone(completed);
    changed.report_id = "3";
    changed.from_wm = 20;
    changed.to_wm = 30;
    (source(changed).result as { coverage: Coverage }).coverage.target = 61;
    expect(() => validateReportAgainstHistory(completed, changed)).toThrow(
      /终态结果不可改写/,
    );
  });

  it("缺口详情支持原生延时来源并使用无型号前缀的位置文案", () => {
    const value = segmentReport();
    const action = source(value);
    action.type = "camera_timelapse";
    expect(() => validateReport(value)).not.toThrow();
    const text = actionIssueText(action)!;
    expect(text).toContain("目录 1/索引 3");
    expect(text).not.toContain("CAM_");
  });

  it("默认视频范围的来源级错误支持原生延时", () => {
    const value = scopeReport("empty");
    source(value).type = "camera_timelapse";
    expect(() => validateReport(value)).not.toThrow();
  });

  it("报告引用的产物稍后出现时，跨报告检查其视频身份", () => {
    const previous = report(
      coverage({
        default_scope: {
          state: "nonempty",
          member_count: 1,
          output_ids: ["12"],
        },
      }),
      "camera_record",
      [],
    );
    const incoming = report(
      coverage({
        default_scope: {
          state: "nonempty",
          member_count: 1,
          output_ids: ["12"],
        },
      }),
      "camera_record",
      [original("12", "image/jpeg")],
    );
    incoming.report_id = "2";
    incoming.from_wm = 10;
    incoming.to_wm = 20;
    expect(() => validateReportAgainstHistory(previous, incoming)).toThrow(
      /原片视频/,
    );
  });
});
