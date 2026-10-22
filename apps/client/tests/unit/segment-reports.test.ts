import { describe, it, expect } from "vitest";
import {
  validateReport,
  mergeReport,
  validateReportAgainstHistory,
} from "../../src/domain/reports";
import {
  parseJson,
  cloneProtocolJson,
  stringifyJson,
} from "../../src/shared/json";
import type { StatusReport } from "../../src/shared/types";
import { segmentReport, scopeReport } from "../helpers/segment-reports";
import { createProtocolValidator } from "../../src/shared/protocol-validation";

const source = (r: StatusReport) => r.plans![0].actions![0];
const details = (r: StatusReport) => source(r).error!.details as any;
const schema = createProtocolValidator().getSchema(
  "status-report.schema.json",
)!;

describe("完整分段错误报告", () => {
  it("接受跨目录缺口区间并将可靠文件身份保留为不透明文本", () => {
    const r = segmentReport();
    for (const gap of [details(r).first_gap, details(r).gaps[0]]) {
      gap.start_key = { directory_number: 1, index: 999 };
      gap.end_key = { directory_number: 2, index: 1 };
      gap.left.file_identity = "opaque-left";
      gap.right.file_identity = "opaque-right";
    }
    expect(() => validateReport(r)).not.toThrow();
  });
  it("同份已知来源的冻结默认范围不能互相矛盾", () => {
    const r = scopeReport("empty");
    details(r).default_scope = {
      state: "nonempty",
      member_count: 2,
      output_ids: ["2", "10"],
    };
    expect(() => validateReport(r)).toThrow();
  });
  it.each(["scope", "retained", "gap", "neighbor"])(
    "两方向历史拒绝同来源冻结%s变更",
    (change) => {
      const a = segmentReport();
      const b = cloneProtocolJson(a);
      b.report_id = "2";
      b.from_wm = 10;
      b.to_wm = 20;
      const d = details(b);
      if (change === "scope") d.default_scope.output_ids = ["2", "11"];
      if (change === "retained") {
        d.retained_output_ids = ["2", "10", "11", "99"];
        source(b).outputs = [];
      }
      if (change === "gap")
        for (const g of [d.first_gap, d.gaps[0]])
          g.start_key.index = g.end_key.index = 4;
      if (change === "neighbor")
        for (const g of [d.first_gap, d.gaps[0]]) g.left.output_id = "2";
      expect(() => validateReport(b)).not.toThrow();
      expect(() => validateReportAgainstHistory(a, b)).toThrow();
      expect(() => validateReportAgainstHistory(b, a)).toThrow();
    },
  );
  it("同来源详情允许成员ID顺序不同及可选邻接output后补", () => {
    const a = segmentReport();
    for (const g of [details(a).first_gap, details(a).gaps[0]])
      delete g.left.output_id;
    const b = segmentReport();
    b.report_id = "2";
    b.from_wm = 10;
    b.to_wm = 20;
    details(b).default_scope.output_ids.reverse();
    details(b).retained_output_ids.reverse();
    details(b).description = "另一段人类说明";
    expect(() => validateReportAgainstHistory(a, b)).not.toThrow();
    expect(() => validateReportAgainstHistory(b, a)).not.toThrow();
  });
  it.each([
    segmentReport,
    () => scopeReport("empty"),
    () => scopeReport("unknown"),
  ])("接受完整详情且保留其事实 %#", (make) =>
    expect(() => validateReport(make())).not.toThrow(),
  );
  const incomplete: Array<[string, (d: any) => void]> = [
    ["缺少首项", (d: any) => delete d.first_gap],
    ["gaps", (d: any) => delete d.gaps],
    ["retained", (d: any) => delete d.retained_output_ids],
    ["scope", (d: any) => delete d.default_scope.output_ids],
    ["少缺口", (d: any) => (d.gaps = [])],
    ["多缺口", (d: any) => d.gaps.push(cloneProtocolJson(d.first_gap))],
    ["首项邻接", (d: any) => (d.first_gap.right.file_identity = "other")],
    ["少原片", (d: any) => d.retained_output_ids.pop()],
    ["少范围", (d: any) => d.default_scope.output_ids.pop()],
    ["范围越界", (d: any) => (d.default_scope.output_ids[1] = "99")],
    ["区间倒置", (d: any) => (d.gaps[0].start_key.index = 4)],
    [
      "零文件多位置",
      (d: any) => {
        for (const g of [d.first_gap, d.gaps[0]]) {
          g.kind = "zero_byte_non_tail";
          g.end_key.index = 4;
          g.empty_file_identity = "empty";
        }
      },
    ],
  ];
  it.each(incomplete)("拒绝未完整展开或自相矛盾：%s", (_label, change) => {
    const r = segmentReport();
    change(details(r));
    expect(() => validateReport(r)).toThrow();
  });
  it.each(["empty", "unknown"] as const)("%s范围仍必须展开三数组", (state) => {
    for (const key of ["gaps", "retained_output_ids", "output_ids"]) {
      const r = scopeReport(state);
      const d = (r.plans![0].actions![1].result as any).failures[0].error
        .details;
      delete (key === "output_ids" ? d.default_scope : d)[key];
      expect(() => validateReport(r)).toThrow();
    }
  });
  it.each([0, 1, null])("unknown范围仍区分缺口计数%s的完整事实", (count) => {
    const r = scopeReport("unknown");
    const failure = (r.plans![0].actions![1].result as any).failures[0];
    if (count === 1) {
      const d = source(segmentReport()).error!.details;
      failure.error.details = cloneProtocolJson(d);
      failure.error.details.default_scope = {
        state: "unknown",
        member_count: null,
        output_ids: [],
      };
    } else failure.error.details.gap_count = count;
    expect(() => validateReport(r)).not.toThrow();
  });
  it("空范围与零保留原片可明确已确认无缺口", () => {
    const r = scopeReport("empty");
    source(r).error = { code: "future", stage: "execution", details: {} };
    source(r).outputs = [];
    const d = (r.plans![0].actions![1].result as any).failures[0].error.details;
    d.gap_count = 0;
    delete d.first_gap;
    d.gaps = [];
    d.retained_original_count = 0;
    d.retained_output_ids = [];
    expect(() => validateReport(r)).not.toThrow();
  });
  it("缺口未知时非空gaps或首项仍拒绝", () => {
    for (const field of ["first_gap", "gaps"]) {
      const r = scopeReport("unknown");
      const d = (r.plans![0].actions![1].result as any).failures[0].error
        .details;
      const original = source(segmentReport()).error!.details as any;
      d[field] = cloneProtocolJson(original[field]);
      expect(() => validateReport(r)).toThrow();
    }
  });
  it("未知范围不能用非空数组冒充已选范围", () => {
    const r = scopeReport("unknown");
    (
      r.plans![0].actions![1].result as any
    ).failures[0].error.details.default_scope.output_ids = ["2"];
    expect(() => validateReport(r)).toThrow();
  });
  it("识别码的stage来自根登记，未知code继续保留", () => {
    const r = segmentReport();
    source(r).error!.stage = "device_stop";
    expect(() => validateReport(r)).toThrow();
    source(r).error = {
      code: "future_segment_error",
      stage: "future",
      details: { unknown: true },
    };
    expect(() => validateReport(r)).not.toThrow();
  });
  it("分段来源允许原生延时", () => {
    const r = segmentReport();
    source(r).type = "camera_timelapse";
    expect(() => validateReport(r)).not.toThrow();
  });
  it("分段来源不能是单张拍摄", () => {
    const r = segmentReport();
    source(r).type = "camera_take_photo";
    expect(() => validateReport(r)).toThrow();
  });
  it("来源错误不能指向其他动作", () => {
    const r = segmentReport();
    details(r).source_action_instance_id = "30";
    expect(() => validateReport(r)).toThrow();
  });
  it.each(["output_id", "delivery_id"])("范围来源级错误不附%s", (key) => {
    const r = scopeReport("empty");
    (r.plans![0].actions![1].result as any).failures[0][key] = "2";
    expect(() => validateReport(r)).toThrow();
  });
  it("范围详情来源必须匹配失败项来源", () => {
    const r = scopeReport("empty");
    (
      r.plans![0].actions![1].result as any
    ).failures[0].error.details.source_action_instance_id = "30";
    expect(() => validateReport(r)).toThrow();
  });
  it.each(["other-source", "preview", "repaired"])(
    "拒绝已知%s原片引用",
    (kind) => {
      const r = segmentReport();
      const o = source(r).outputs![0];
      if (kind === "other-source") {
        source(r).outputs = [];
        r.plans![0].actions!.push({
          ...source(segmentReport()),
          action_instance_id: "30",
          name: "另一录像",
          error: undefined,
          status: "succeeded",
          outputs: [{ ...o, source_action_instance_id: "30" }],
        });
      } else {
        o.kind = kind as "preview" | "repaired";
        if (kind === "preview") o.preview_of_output_id = "10";
        else o.derived_from_output_id = "10";
      }
      expect(() => validateReport(r)).toThrow();
    },
  );
  it("大整数首缺口不能按舍入相等通过", () => {
    const text = stringifyJson(segmentReport()).replaceAll(
      '"directory_number":1',
      '"directory_number":9007199254740992',
    );
    const r = parseJson(
      text.replace(
        '"directory_number":9007199254740992',
        '"directory_number":9007199254740993',
      ),
    );
    expect(() => validateReport(r)).toThrow();
  });
  it("大整数区间按原数学值比较", () => {
    const r = segmentReport();
    for (const g of [details(r).first_gap, details(r).gaps[0]])
      g.start_key.index = 1;
    const text = stringifyJson(r)
      .replaceAll(
        '"directory_number":1,"index":1',
        '"directory_number":9007199254740993,"index":1',
      )
      .replaceAll(
        '"directory_number":1,"index":3',
        '"directory_number":9007199254740992,"index":3',
      );
    expect(() => validateReport(parseJson(text))).toThrow();
  });
  it("大计数必须和实际数组完整对应", () => {
    const r = segmentReport();
    details(r).gap_count = 9007199254740992;
    expect(() => validateReport(r)).toThrow();
  });
  it.each(["preview", "wrong-source"])(
    "未知引用先保留，%s晚到时两方向都拒绝",
    (change) => {
      const a = segmentReport();
      source(a).outputs = [];
      expect(() => validateReport(a)).not.toThrow();
      const b = segmentReport();
      b.report_id = "2";
      b.from_wm = 10;
      b.to_wm = 20;

      if (change === "preview") {
        source(b).outputs![0].kind = "preview";
        source(b).outputs![0].preview_of_output_id = "10";
      } else {
        source(b).outputs = [];
        b.plans![0].actions!.push({
          ...source(segmentReport()),
          action_instance_id: "30",
          name: "另一录像",
          error: undefined,
          status: "succeeded",
          outputs: [
            {
              ...source(segmentReport()).outputs![0],
              source_action_instance_id: "30",
            },
          ],
        });
      }
      expect(() => validateReportAgainstHistory(a, b)).toThrow();
      expect(() => validateReportAgainstHistory(b, a)).toThrow();
    },
  );
  it("合法增量不重带旧产物，合并仍保留完整详情与旧实体", () => {
    const a = segmentReport();
    const b = cloneProtocolJson(a);
    b.report_id = "2";
    b.from_wm = 10;
    b.to_wm = 20;
    source(b).outputs = [];
    const merged = mergeReport(a, b);
    expect(source(merged).outputs).toHaveLength(4);
    expect(details(merged).retained_output_ids).toEqual([
      "2",
      "10",
      "11",
      "12",
    ]);
  });
  it("参数中像error的普通对象不属于公共错误槽", () => {
    const r = segmentReport();
    source(r).input_params = {
      ...(source(r).input_params as object),
      metadata: { code: "capture_segment_gap", stage: "bad", details: {} },
    };
    expect(() => validateReport(r)).not.toThrow();
  });
  it.each([
    [
      "device_execution",
      (r: any, e: any) =>
        (source(r).device_execution = { status: "end_unconfirmed", error: e }),
    ],
    [
      "check.error",
      (r: any, e: any) =>
        (source(r).result = {
          check: {
            check_status: "failed",
            duration: { status: "unknown" },
            error: e,
          },
        }),
    ],
    [
      "check.issues",
      (r: any, e: any) =>
        (source(r).result = {
          check: {
            check_status: "completed",
            duration: { status: "unknown" },
            issues: [e],
          },
        }),
    ],
    [
      "repair",
      (r: any, e: any) =>
        (source(r).result = { repair: { status: "failed", error: e } }),
    ],
    [
      "discard",
      (r: any, e: any) =>
        (source(r).result = {
          discard_cleanup: { status: "failed", error: e },
        }),
    ],
    ["output", (r: any, e: any) => (source(r).outputs![0].error = e)],
    [
      "cleanup",
      (r: any, e: any) => {
        const o = source(r).outputs![0];
        o.availability = "restricted";
        o.cleanup = { status: "incomplete", error: e };
      },
    ],
    [
      "media.error",
      (r: any, e: any) =>
        (source(r).outputs![0].media = {
          check_status: "failed",
          duration: { status: "unknown" },
          error: e,
        }),
    ],
    [
      "media.issues",
      (r: any, e: any) =>
        (source(r).outputs![0].media = {
          check_status: "completed",
          duration: { status: "unknown" },
          issues: [e],
        }),
    ],
    [
      "delivery",
      (r: any, e: any) =>
        r.plans[0].actions.push({
          action_instance_id: "20",
          name: "取回",
          type: "obtain_action_outputs",
          status: "failed",
          scheduled_at: "2026-01-01 00:00:00",
          input_params: { source: { action_instance_id: "10" } },
          error: { code: "future", stage: "execution", details: {} },
          result: {
            failures: [
              {
                source_action_instance_id: "10",
                output_id: "2",
                delivery_id: "100",
                error: e,
              },
            ],
          },
          deliveries: [
            {
              delivery_id: "100",
              output_id: "2",
              source_action_instance_id: "10",
              file_name: "100.OSV",
              display_name: "原片",
              status: "failed",
              error: e,
            },
          ],
        }),
    ],
    [
      "delete.item",
      (r: any, e: any) =>
        r.plans[0].actions.push({
          action_instance_id: "20",
          name: "清理",
          type: "delete_action_outputs",
          status: "failed",
          scheduled_at: "2026-01-01 00:00:00",
          input_params: { output_ids: ["2"] },
          error: { code: "future", stage: "execution", details: {} },
          result: { items: [{ output_id: "2", status: "failed", error: e }] },
        }),
    ],
    [
      "cancel.item",
      (r: any, e: any) =>
        r.plans[0].actions.push({
          action_instance_id: "20",
          name: "取消",
          type: "cancel_task",
          status: "failed",
          input_params: { target: { action_instance_id: "10" } },
          error: { code: "future", stage: "execution", details: {} },
          result: {
            items: [{ action_instance_id: "10", status: "failed", error: e }],
          },
        }),
    ],
    [
      "withdrawal",
      (r: any, e: any) =>
        r.plans[0].actions.push({
          action_instance_id: "20",
          name: "取消",
          type: "cancel_task",
          status: "failed",
          input_params: { target: { action_instance_id: "10" } },
          error: { code: "future", stage: "execution", details: {} },
          result: {
            items: [
              {
                action_instance_id: "10",
                status: "succeeded",
                outcome: "already_terminal",
                withdrawals: [
                  { delivery_id: "100", status: "failed", error: e },
                ],
              },
            ],
          },
        }),
    ],
    [
      "diagnostic",
      (r: any, e: any) =>
        (r.plan_file_diagnostics = [
          {
            diagnostic_id: "1",
            file_name: "test.json",
            errors: [{ ...e, stage: "ack" }],
          },
        ]),
    ],
  ] as const)("拒绝公共错误槽错放分段码：%s", (_slot, place) => {
    const r = segmentReport();
    const e = cloneProtocolJson(source(r).error!);
    source(r).error = { code: "future", stage: "execution", details: {} };
    place(r, e);
    expect(schema(r), JSON.stringify(schema.errors)).toBe(true);
    const control = parseJson(
      stringifyJson(r).replaceAll(
        '"capture_segment_gap"',
        '"future_segment_code"',
      ),
    );
    expect(() => validateReport(control)).not.toThrow();
    expect(() => validateReport(r)).toThrow();
  });
  it("scope码不能放在action.error，capture码不能放在来源failure", () => {
    const r = scopeReport("empty");
    const f = (r.plans![0].actions![1].result as any).failures[0];
    source(r).error = cloneProtocolJson(f.error);
    expect(schema(r)).toBe(true);
    expect(() => validateReport(r)).toThrow();
    const q = scopeReport("empty");
    (q.plans![0].actions![1].result as any).failures[0].error =
      cloneProtocolJson(source(q).error!);
    expect(schema(q)).toBe(true);
    expect(() => validateReport(q)).toThrow();
  });
  it("scope引用的单张拍摄来源晚到时拒绝，不把缺席当不存在", () => {
    const a = scopeReport("unknown");
    a.plans![0].actions = a.plans![0].actions!.slice(1);
    expect(() => validateReport(a)).not.toThrow();
    const b = segmentReport();
    source(b).error = undefined;
    source(b).status = "succeeded";
    source(b).type = "camera_take_photo";
    b.report_id = "2";
    b.from_wm = 10;
    b.to_wm = 20;
    expect(() => validateReportAgainstHistory(a, b)).toThrow();
    expect(() => validateReportAgainstHistory(b, a)).toThrow();
  });
});
