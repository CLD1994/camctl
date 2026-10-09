import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import {
  parseReport,
  validateReport,
  validateReportAgainstHistory,
  mergeReport,
  reportDecision,
  selectSyncReport,
} from "../../src/domain/reports";
import type {
  StatusReport,
  ReportAction,
  Output,
  Delivery,
} from "../../src/shared/types";
import type { CameraResult } from "../../src/shared/status-report.generated";

const error = { code: "future_code", stage: "execution", details: {} };
const output = (id = "1"): Output => ({
  output_id: id,
  source_action_instance_id: "1",
  kind: "original",
  availability: "available",
  cleanup: { status: "not_requested" },
  checksum: { status: "not_obtained" },
  media: { check_status: "not_performed", duration: { status: "unknown" } },
});
const camera = (): ReportAction => ({
  action_instance_id: "1",
  name: "录像",
  type: "camera_record",
  device_id: "cam0",
  scheduled_at: "2026-01-01 00:00:00",
  input_params: { type: "historical" },
  effective_params: { type: "historical" },
  policy: { max_delay_ms: 0 },
  status: "succeeded",
  outputs: [output()],
});
const delivery = (): Delivery => ({
  delivery_id: "1",
  output_id: "1",
  source_action_instance_id: "1",
  file_name: "1.mp4",
  display_name: "录像.mp4",
  status: "published",
  size: 100,
  sha256: "a".repeat(64),
});
const obtain = (): ReportAction => ({
  action_instance_id: "2",
  name: "取回",
  type: "obtain_action_outputs",
  scheduled_at: "2026-01-01 00:00:00",
  input_params: { source: { action_instance_id: "1" } },
  status: "succeeded",
  result: { failures: [] },
  deliveries: [delivery()],
});
const report = (): StatusReport => ({
  report_id: "1",
  from_wm: 0,
  to_wm: 20,
  plans: [
    {
      plan_instance_id: "1",
      request_id: "1",
      created_at: "2026-01-01 00:00:00",
      name: "计划",
      status: "completed",
      actions: [camera(), obtain()],
    },
  ],
});
function previewReport(): StatusReport {
  const r = report();
  r.plans![0].actions![0].outputs!.push({
    ...output("3"),
    kind: "preview",
    preview_of_output_id: "1",
  });
  return r;
}
function linkedCancellation(target: unknown): StatusReport {
  const r = report();
  r.plans![0].actions![0].group = "采集";
  const auto = r.plans![0].actions![1];
  auto.input_params = {
    source: { action_name: "录像" },
    filter: "preview",
    purpose: "auto_preview",
  };
  auto.automation = { purpose: "auto_preview", source_action_instance_id: "1" };
  r.plans!.push({
    plan_instance_id: "2",
    request_id: "2",
    created_at: "2026-01-01 00:00:00",
    name: "取消计划",
    status: "completed",
    actions: [
      {
        action_instance_id: "7",
        name: "取消",
        type: "cancel_task",
        input_params: { target },
        status: "succeeded",
        result: {
          items: [
            {
              action_instance_id: "1",
              status: "succeeded",
              outcome: "already_terminal",
            },
            {
              action_instance_id: "2",
              status: "succeeded",
              outcome: "already_terminal",
            },
          ],
        },
      },
    ],
  });
  return r;
}
describe("公共报告状态与关联闭合", () => {
  const sourceCases = [
    { mode: "action", source: { action_instance_id: "1" } },
    { mode: "name", source: { action_name: "录像" } },
    { mode: "group", source: { group: "采集" } },
    { mode: "plan-group", source: { plan_instance_id: "1", group: "采集" } },
    { mode: "current-plan", source: { current_plan: true } },
    { mode: "plan", source: { plan_instance_id: "1" } },
  ];
  it.each(sourceCases)("六种取回来源接受已知匹配 $mode", ({ source }) => {
    const r = report();
    r.plans![0].actions![0].group = "采集";
    r.plans![0].actions![1].input_params = { source };
    expect(() => validateReport(r)).not.toThrow();
  });
  for (const boundary of ["earlier", "same", "later"] as const)
    it.each(sourceCases)(
      `${boundary} 晚到的匹配来源允许关联 $mode`,
      ({ source }) => {
        const old = report();
        old.plans![0].actions = [obtain()];
        old.plans![0].actions[0].input_params = { source };
        const next = report();
        next.report_id = "2";
        next.to_wm =
          boundary === "earlier" ? 10 : boundary === "same" ? 20 : 30;
        next.plans![0].actions = [camera()];
        next.plans![0].actions[0].group = "采集";
        expect(() => validateReportAgainstHistory(old, next)).not.toThrow();
      },
    );
  it("自动预览来源缺席时保留取消关联，来源晚到后核验组归属", () => {
    const old = linkedCancellation({ plan_instance_id: "1", group: "采集" });
    old.plans![0].actions!.shift();
    expect(() => validateReport(old)).not.toThrow();
    const next = report();
    next.report_id = "2";
    next.from_wm = 20;
    next.to_wm = 30;
    next.plans![0].actions = [camera()];
    next.plans![0].actions[0].group = "其他组";
    expect(() => mergeReport(old, next)).toThrow();
  });
  for (const boundary of ["earlier", "same", "later", "gap"] as const)
    it.each(sourceCases)(
      `${boundary} 晚到来源拒绝已知矛盾 $mode`,
      ({ source, mode }) => {
        const old = report();
        old.plans![0].actions = [obtain()];
        old.plans![0].actions[0].input_params = { source };
        expect(() => validateReport(old)).not.toThrow();
        const next = report();
        next.report_id = "2";
        next.to_wm =
          boundary === "earlier" ? 10 : boundary === "same" ? 20 : 30;
        next.from_wm = boundary === "gap" ? 25 : 0;
        const c = camera();
        c.group = "其他组";
        if (mode === "action") {
          c.type = "motor_control";
          delete c.device_id;
          delete c.effective_params;
          delete c.outputs;
          c.input_params = { position: 1 };
        }
        if (mode === "name") c.name = "另一动作";
        next.plans![0].actions = [c];
        if (
          mode === "current-plan" ||
          mode === "plan" ||
          mode === "plan-group"
        ) {
          next.plans![0].plan_instance_id = "2";
          next.plans![0].request_id = "2";
        }
        expect(() => validateReportAgainstHistory(old, next)).toThrow();
      },
    );
  for (const boundary of ["earlier", "same", "later", "gap"] as const)
    it.each([
      { action_name: "另一动作" },
      { action_instance_id: "9" },
      { current_plan: true },
      { plan_instance_id: "9" },
    ])(`${boundary} 范围清理在产物晚到后拒绝归属矛盾 %#`, (source) => {
      const old = report();
      old.plans![0].actions = [
        {
          action_instance_id: "6",
          name: "清理",
          type: "delete_action_outputs",
          scheduled_at: "2026-01-01 00:00:00",
          input_params: { source },
          status: "succeeded",
          result: {
            items: [
              { output_id: "1", status: "succeeded", outcome: "deleted" },
            ],
          },
        },
      ];
      expect(() => validateReport(old)).not.toThrow();
      const next = report();
      next.report_id = "2";
      next.to_wm = boundary === "earlier" ? 10 : boundary === "same" ? 20 : 30;
      next.from_wm = boundary === "gap" ? 25 : 0;
      next.plans![0].actions = [camera()];
      if ("current_plan" in source) {
        next.plans![0].plan_instance_id = "2";
        next.plans![0].request_id = "2";
      }
      expect(() => validateReportAgainstHistory(old, next)).toThrow();
    });
  it("自动预览实例引用不能与已知同名来源矛盾", () => {
    const r = linkedCancellation({ action_instance_id: "1" });
    r.plans = [r.plans![0]];
    r.plans[0].actions![1].automation!.source_action_instance_id = "9";
    expect(() => validateReport(r)).toThrow();
  });
  it("没有实例引用的自动预览仍检查已知拍摄的取消范围", () => {
    const r = linkedCancellation({ action_instance_id: "9" });
    delete r.plans![0].actions![1].automation!.source_action_instance_id;
    r.plans![1].actions![0].result = {
      items: [
        {
          action_instance_id: "2",
          status: "succeeded",
          outcome: "already_terminal",
        },
      ],
    };
    expect(() => validateReport(r)).toThrow();
  });
  it.each([
    "obtain_action_outputs",
    "delete_action_outputs",
    "cancel_task",
  ] as const)("pending 计划拒绝 %s 的空执行结果", (type) => {
    const r = report();
    r.plans![0].status = "pending";
    r.plans![0].actions = [
      {
        action_instance_id: "6",
        name: "已取消",
        type,
        status: "canceled",
        scheduled_at: "2026-01-01 00:00:00",
        input_params:
          type === "obtain_action_outputs"
            ? { source: { action_instance_id: "1" } }
            : type === "delete_action_outputs"
              ? { output_ids: ["1"] }
              : { target: { action_instance_id: "1" } },
        result:
          type === "obtain_action_outputs" ? { failures: [] } : { items: [] },
      },
    ];
    expect(() => validateReport(r)).toThrow();
  });
  it("pending 计划保留无需处理录像内容的执行前取消", () => {
    const r = report();
    r.plans![0].status = "pending";
    const c = camera();
    c.status = "canceled";
    delete c.outputs;
    c.result = { discard_cleanup: { status: "not_needed" } };
    r.plans![0].actions = [c];
    expect(() => validateReport(r)).not.toThrow();
  });
  it.each(["output_not_found", "output_source_mismatch"])(
    "%s 不建立虚构产物关联",
    (code) => {
      const r = report();
      const a = r.plans![0].actions![1];
      a.status = "failed";
      a.error = error;
      delete a.deliveries;
      a.input_params = {
        source: { action_instance_id: "1" },
        output_ids: ["8"],
      };
      a.result = {
        failures: [
          {
            source_action_instance_id: "1",
            output_id: "8",
            error: {
              code,
              stage: "execution",
              details: { requested_output_id: "8" },
            },
          },
        ],
      };
      expect(() => validateReport(r)).toThrow();
    },
  );
  it("无效产物失败的请求 ID 必须属于原筛选列表", () => {
    const r = report();
    const a = r.plans![0].actions![1];
    a.status = "failed";
    a.error = error;
    delete a.deliveries;
    a.input_params = { source: { action_instance_id: "1" }, output_ids: ["8"] };
    a.result = {
      failures: [
        {
          source_action_instance_id: "1",
          error: {
            code: "output_not_found",
            stage: "execution",
            details: { requested_output_id: "9" },
          },
        },
      ],
    };
    expect(() => validateReport(r)).toThrow();
  });
  it.each(["earlier", "same", "later"] as const)(
    "%s 报告不能改变自动取回的固定来源",
    (boundary) => {
      const old = linkedCancellation({ action_instance_id: "1" });
      old.plans = [old.plans![0]];
      const next = structuredClone(old);
      next.report_id = "2";
      next.to_wm = boundary === "earlier" ? 10 : boundary === "same" ? 20 : 30;
      next.plans![0].actions![1].automation!.source_action_instance_id = "9";
      expect(() => validateReportAgainstHistory(old, next)).toThrow();
    },
  );
  it.each([
    "camera_record",
    "camera_take_photo",
    "camera_timelapse",
    "motor_control",
  ] as const)("%s 接受两种过期原因而不补造执行", (type) => {
    for (const reason of ["window_missed", "window_exhausted"] as const) {
      const r = report();
      const action = camera();
      action.type = type;
      action.status = "expired";
      action.expiration_reason = reason;
      delete action.outputs;
      if (type === "motor_control") {
        delete action.device_id;
        delete action.effective_params;
        action.input_params = { position: 1 };
      }
      r.plans![0].actions = [action];
      expect(() => validateReport(r)).not.toThrow();
    }
  });
  it.each(["canceled", "expired"] as const)(
    "pending 计划接受执行前 %s 与待执行动作共存",
    (status) => {
      const r = report();
      r.plans![0].status = "pending";
      const action = camera();
      action.status = status;
      delete action.outputs;
      if (status === "expired") action.expiration_reason = "window_exhausted";
      const waiting = camera();
      waiting.action_instance_id = "3";
      waiting.name = "待拍摄";
      waiting.status = "pending";
      delete waiting.outputs;
      r.plans![0].actions = [action, waiting];
      expect(() => validateReport(r)).not.toThrow();
    },
  );
  it.each(["running", "succeeded", "failed"] as const)(
    "pending 计划拒绝明确的 %s 执行事实",
    (status) => {
      const r = report();
      r.plans![0].status = "pending";
      r.plans![0].actions = [camera()];
      r.plans![0].actions[0].status = status;
      if (status === "failed") r.plans![0].actions[0].error = error;
      expect(() => validateReport(r)).toThrow();
    },
  );
  it.each([
    { action_instance_id: "1" },
    { plan_instance_id: "1", group: "采集" },
  ])("取消直接目标允许自动预览联动 %#", (target) =>
    expect(() => validateReport(linkedCancellation(target))).not.toThrow(),
  );
  it("拍摄拒绝取消时不能作为预览联动依据", () => {
    const r = linkedCancellation({ action_instance_id: "1" });
    const cancel = r.plans![1].actions![0];
    cancel.status = "failed";
    cancel.error = error;
    cancel.result = {
      items: [
        {
          action_instance_id: "1",
          status: "failed",
          error: {
            code: "task_cancel_unsupported",
            stage: "execution",
            details: {},
          },
        },
        {
          action_instance_id: "2",
          status: "succeeded",
          outcome: "already_terminal",
        },
      ],
    };
    expect(() => validateReport(r)).toThrow();
  });
  it.each([
    { plan_instance_id: "1" },
    { request_id: "1" },
    { action_instance_id: "2" },
  ])("自动取回属于直接目标时独立处理 %#", (target) => {
    const r = linkedCancellation(target);
    r.plans![1].actions![0].result = {
      items: [
        {
          action_instance_id: "2",
          status: "succeeded",
          outcome: "already_terminal",
        },
      ],
    };
    expect(() => validateReport(r)).not.toThrow();
  });
  it.each([{ current_plan: true }, { plan_instance_id: "1" }])(
    "计划取回拒绝其他计划的已知来源 %#",
    (source) => {
      const r = report();
      const c = r.plans![0].actions!.shift()!;
      r.plans![0].actions![0].input_params = { source };
      r.plans!.push({
        ...r.plans![0],
        plan_instance_id: "2",
        request_id: "2",
        actions: [c],
      });
      expect(() => validateReport(r)).toThrow();
    },
  );
  it.each([
    { action_name: "另一动作" },
    { action_instance_id: "8" },
    { current_plan: true },
    { plan_instance_id: "8" },
  ])("范围清理拒绝已知范围外产物 %#", (source) => {
    const r = report();
    r.plans![0].actions!.push({
      action_instance_id: "6",
      name: "清理",
      type: "delete_action_outputs",
      scheduled_at: "2026-01-01 00:00:00",
      input_params: { source },
      status: "succeeded",
      result: {
        items: [{ output_id: "1", status: "succeeded", outcome: "deleted" }],
      },
    });
    if ("current_plan" in source) {
      const c = r.plans![0].actions!.shift()!;
      r.plans!.push({
        ...r.plans![0],
        plan_instance_id: "2",
        request_id: "2",
        actions: [c],
      });
    }
    expect(() => validateReport(r)).toThrow();
  });
  it.each(["self", "non-original", "other-action"] as const)(
    "预览拒绝 %s 原文件关联",
    (kind) => {
      const r = previewReport();
      const preview = r.plans![0].actions![0].outputs![1];
      if (kind === "self") preview.preview_of_output_id = "3";
      if (kind === "non-original") {
        const second = { ...preview, output_id: "4" };
        r.plans![0].actions![0].outputs!.push(second);
        preview.preview_of_output_id = "4";
      }
      if (kind === "other-action") {
        const c = camera();
        c.action_instance_id = "9";
        c.name = "其他拍摄";
        c.outputs = [{ ...output("4"), source_action_instance_id: "9" }];
        r.plans![0].actions!.push(c);
        preview.preview_of_output_id = "4";
      }
      expect(() => validateReport(r)).toThrow();
    },
  );
  it.each(["earlier", "same", "later"] as const)(
    "%s 报告不能改变预览原文件关系",
    (boundary) => {
      const old = previewReport();
      const next = previewReport();
      next.report_id = "2";
      next.to_wm = boundary === "earlier" ? 10 : boundary === "same" ? 20 : 30;
      next.plans![0].actions![0].outputs = [
        { ...output("3"), kind: "preview", preview_of_output_id: "4" },
      ];
      expect(() => validateReportAgainstHistory(old, next)).toThrow();
    },
  );
  it("未知预览原文件保留引用，晚到矛盾被拒绝", () => {
    const old = previewReport();
    old.plans![0].actions![0].outputs!.shift();
    expect(() => validateReport(old)).not.toThrow();
    const next = report();
    next.report_id = "2";
    next.from_wm = 20;
    next.to_wm = 30;
    next.plans![0].actions![0].outputs = [
      { ...output(), kind: "preview", preview_of_output_id: "8" },
    ];
    next.plans![0].actions = [next.plans![0].actions![0]];
    expect(() => mergeReport(old, next)).toThrow();
  });
  it("同源多个无效请求产物的失败在新水位完整保留", () => {
    const old = report();
    const a = old.plans![0].actions![1];
    a.status = "failed";
    a.error = error;
    delete a.deliveries;
    a.input_params = {
      source: { action_instance_id: "1" },
      output_ids: ["8", "9"],
    };
    a.result = {
      failures: [
        {
          source_action_instance_id: "1",
          error: {
            code: "output_not_found",
            stage: "execution",
            details: { requested_output_id: "8" },
          },
        },
        {
          source_action_instance_id: "1",
          error: {
            code: "output_not_found",
            stage: "execution",
            details: { requested_output_id: "9" },
          },
        },
      ],
    };
    const next = structuredClone(old);
    next.report_id = "2";
    next.from_wm = 20;
    next.to_wm = 30;
    expect(() => mergeReport(old, next)).not.toThrow();
  });
});
function parse(value: unknown, text = JSON.stringify(value)) {
  const bytes = new TextEncoder().encode(text);
  return parseReport(
    `status-report-1-${createHash("sha256").update(bytes).digest("hex")}.json`,
    bytes,
  );
}
describe("名称来源与结果身份", () => {
  const kinds = [
    "delivery",
    "failure-source",
    "failure-output",
    "no_outputs",
    "output_not_found",
    "output_source_mismatch",
  ] as const;
  function referenced(kind: (typeof kinds)[number], sourceId = "9") {
    const r = report();
    const a = r.plans![0].actions![1];
    a.input_params = { source: { action_name: "录像" } };
    if (kind === "delivery") {
      a.deliveries![0].source_action_instance_id = sourceId;
      a.deliveries![0].output_id = "8";
    } else {
      delete a.deliveries;
      a.status = "failed";
      a.error = error;
      if (kind === "output_not_found" || kind === "output_source_mismatch")
        a.input_params = { source: { action_name: "录像" }, output_ids: ["8"] };
      a.result = {
        failures: [
          {
            source_action_instance_id: sourceId,
            ...(kind === "failure-output" ? { output_id: "8" } : {}),
            error: {
              code:
                kind === "failure-source" || kind === "failure-output"
                  ? "future_code"
                  : kind,
              stage: "output_selection",
              details:
                kind === "output_not_found" || kind === "output_source_mismatch"
                  ? { requested_output_id: "8" }
                  : {},
            },
          },
        ],
      };
    }
    return r;
  }
  it.each(kinds)("%s 的结果来源缺席时仍拒绝已知名称身份矛盾", (kind) => {
    expect(() => validateReport(referenced(kind))).toThrow();
  });
  it.each(kinds)("%s 的名称与结果来源均未知时保留引用", (kind) => {
    const r = referenced(kind);
    r.plans![0].actions!.shift();
    expect(() => validateReport(r)).not.toThrow();
  });
  it.each(kinds)("%s 的名称与结果来源身份一致时允许", (kind) => {
    expect(() => validateReport(referenced(kind, "1"))).not.toThrow();
  });
  for (const boundary of ["earlier", "same", "later", "gap"] as const) {
    it.each(kinds)(`${boundary} 名称晚到后拒绝 %s 的身份矛盾`, (kind) => {
      const old = referenced(kind);
      old.plans![0].actions!.shift();
      validateReport(old);
      const next = report();
      next.report_id = "2";
      next.from_wm = boundary === "gap" ? 25 : 0;
      next.to_wm = boundary === "earlier" ? 10 : boundary === "same" ? 20 : 30;
      next.plans![0].actions = [camera()];
      expect(() => validateReportAgainstHistory(old, next)).toThrow();
    });
    it.each(kinds)(`${boundary} 名称晚到后允许 %s 的相同身份`, (kind) => {
      const old = referenced(kind, "1");
      old.plans![0].actions!.shift();
      const next = report();
      next.report_id = "2";
      next.from_wm = boundary === "gap" ? 25 : 0;
      next.to_wm = boundary === "earlier" ? 10 : boundary === "same" ? 20 : 30;
      next.plans![0].actions = [camera()];
      expect(() => validateReportAgainstHistory(old, next)).not.toThrow();
    });
  }
});
describe("parseReport", () => {
  it("接受完整报告且不依赖当前能力说明", () =>
    expect(parse(report())).toEqual(report()));
  it("接受只包含诊断的报告及未知错误码", () =>
    expect(
      parse({
        report_id: "1",
        from_wm: 0,
        to_wm: 1,
        plan_file_diagnostics: [
          {
            diagnostic_id: "1",
            file_name: "输入.json",
            errors: [{ ...error, stage: "admission" }],
          },
        ],
      }).plan_file_diagnostics?.length,
    ).toBe(1));
  it("先校验原始字节摘要", () =>
    expect(() =>
      parseReport(
        `status-report-1-${"0".repeat(64)}.json`,
        new TextEncoder().encode("{}"),
      ),
    ).toThrow());
  it("接受空白不同但摘要对应原文的报告", () =>
    expect(
      parse(
        { report_id: "1", from_wm: 0, to_wm: 0 },
        ' { "report_id": "1", "from_wm":0, "to_wm":0 }\n',
      ).report_id,
    ).toBe("1"));
  it("拒绝文件名身份与根不一致", () =>
    expect(() => parse({ report_id: "2", from_wm: 0, to_wm: 1 })).toThrow());
  it("拒绝重复转义成员", () =>
    expect(() =>
      parse({}, '{"report_id":"1","from_wm":0,"to_wm":1,"\\u0074o_wm":2}'),
    ).toThrow());
  it("拒绝逆序水位", () =>
    expect(() => parse({ report_id: "1", from_wm: 2, to_wm: 1 })).toThrow());
  it("拒绝不存在的日期", () => {
    const r = report();
    r.plans![0].created_at = "2026-02-29 00:00:00";
    expect(() => parse(r)).toThrow();
  });
  it("拒绝名称首尾 Unicode 空白", () => {
    const r = report();
    r.plans![0].name = "计划\u2003";
    expect(() => parse(r)).toThrow();
  });
  it("初始失败保留非法原始输入", () => {
    const r = report();
    r.plans![0].actions = [
      {
        action_instance_id: "1",
        name: "录像",
        type: "camera_record",
        device_id: null,
        group: false,
        scheduled_at: 5,
        input_params: null,
        policy: null,
        extra_input_fields: { typo: true },
        status: "failed",
        error: { ...error, stage: "admission" },
      },
    ];
    expect(parse(r).plans![0].actions![0].input_params).toBeNull();
  });
  it("拒绝跨计划重复动作身份", () => {
    const r = report();
    r.plans!.push({
      ...r.plans![0],
      plan_instance_id: "2",
      request_id: "2",
    });
    expect(() => parse(r)).toThrow();
  });
  it("拒绝产物归属与父动作不符", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].source_action_instance_id = "2";
    expect(() => parse(r)).toThrow();
  });
  it("拒绝交付的来源与已知产物不符", () => {
    const r = report();
    r.plans![0].actions![1].deliveries![0].source_action_instance_id = "2";
    expect(() => parse(r)).toThrow();
  });
  it("来源尚未出现时保留交付引用", () => {
    const r = report();
    r.plans![0].actions = [obtain()];
    expect(parse(r).plans![0].actions![0].deliveries![0].output_id).toBe("1");
  });
  it("拒绝交付文件名截短身份", () => {
    const r = report();
    r.plans![0].actions![1].deliveries![0].file_name = "d.mp4";
    expect(() => parse(r)).toThrow();
  });
  it("拒绝未结束删除限制与可取回资格并存", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].cleanup.status = "running";
    expect(() => parse(r)).toThrow();
  });
  it("拒绝已知源摘要与交付摘要不符", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].checksum = {
      status: "available",
      sha256: "b".repeat(64),
    };
    expect(() => parse(r)).toThrow();
  });
  it("拒绝合法受理报告的非法取消目标组合", () => {
    const r = report();
    r.plans![0].actions = [
      {
        action_instance_id: "4",
        name: "取消",
        type: "cancel_task",
        status: "pending",
        input_params: { target: { group: "组" } },
      },
    ];
    r.plans![0].status = "pending";
    expect(() => parse(r)).toThrow();
  });
  it("拒绝已开始取回但来源参数缺失", () => {
    const r = report();
    r.plans![0].actions![1].input_params = {};
    expect(() => parse(r)).toThrow();
  });
  it("拒绝未执行却带有运行结果的取消动作", () => {
    const r = report();
    r.plans![0].status = "pending";
    r.plans![0].actions = [
      {
        action_instance_id: "4",
        name: "取消",
        type: "cancel_task",
        status: "canceled",
        input_params: { target: { request_id: "2" } },
        result: { items: [] },
      },
    ];
    expect(() => parse(r)).toThrow();
  });
  it.each(["still_running", "end_unconfirmed"] as const)(
    "接受终态拍摄动作的设备执行提示 %s",
    (status) => {
      const r = report();
      r.plans![0].actions![0].device_execution = { status };
      expect(
        parse(r).plans![0].actions![0].device_execution,
      ).toEqual({ status });
    },
  );
  it("接受设备执行提示携带观察错误", () => {
    const r = report();
    r.plans![0].actions![0].device_execution = {
      status: "still_running",
      error,
    };
    expect(
      parse(r).plans![0].actions![0].device_execution?.error,
    ).toEqual(error);
  });
  it.each(["running", undefined])(
    "拒绝未知或缺失状态的设备执行提示 %j",
    (status) => {
      const r = report();
      r.plans![0].actions![0].device_execution = { status } as never;
      expect(() => parse(r)).toThrow();
    },
  );
  it("拒绝非拍摄动作携带设备执行提示", () => {
    const r = report();
    r.plans![0].actions![1].device_execution = { status: "still_running" };
    expect(() => parse(r)).toThrow();
  });
  it("拒绝未终态动作携带设备执行提示", () => {
    const r = report();
    r.plans![0].status = "running";
    const camera = r.plans![0].actions![0];
    camera.status = "running";
    camera.outputs = [];
    camera.device_execution = { status: "still_running" };
    expect(() => parse(r)).toThrow();
  });
  it.each([
    { check_status: "completed", duration: { status: "available", seconds: 60 } },
    { check_status: "failed", duration: { status: "unknown" }, error },
    { check_status: "unconfirmed", duration: { status: "unknown" }, error },
  ])("接受媒体检查最终结论 %j", (media) => {
    const r = report();
    r.plans![0].actions![0].outputs![0].media = media as never;
    expect(parse(r).plans![0].actions![0].outputs![0].media).toEqual(media);
  });
  it("拒绝媒体失败结论缺少观察错误", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].media = {
      check_status: "failed",
      duration: { status: "unknown" },
    } as never;
    expect(() => parse(r)).toThrow();
  });
  it("拒绝未执行检查携带观察错误", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].media = {
      check_status: "not_performed",
      duration: { status: "unknown" },
      error,
    } as never;
    expect(() => parse(r)).toThrow();
  });
  it("拒绝交付长度与正式产物不一致", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].size = 100;
    r.plans![0].actions![1].deliveries![0].size = 99;
    expect(() => parse(r)).toThrow();
  });
});
describe("reportDecision", () => {
  it.each([
    [20, 0, 20, "covered"],
    [20, 10, 19, "covered"],
    [20, 20, 40, "apply"],
    [20, 10, 40, "apply"],
    [20, 40, 45, "gap"],
    [0, 0, 0, "covered"],
  ] as const)("覆盖 C=%s F=%s T=%s", (c, f, t, result) =>
    expect(reportDecision(c, f, t)).toBe(result),
  );
  it("非法水位不能变为缺口或覆盖", () =>
    expect(() => reportDecision(0, 2, 1)).toThrow());
});
describe("selectSyncReport", () => {
  it("按终点选择且排除未覆盖报告", () =>
    expect(
      selectSyncReport(
        [
          { report_id: "100", to_wm: 10 },
          { report_id: "2", to_wm: 20 },
          { report_id: "3", to_wm: 40 },
        ],
        20,
      ),
    ).toBe("2"));
  it("无可靠基础返回完整同步选择", () =>
    expect(selectSyncReport([], 0)).toBeNull());
});
describe("mergeReport", () => {
  it.each([undefined, []])(
    "拒绝 completed 父快照保留 pending 子动作 %#",
    (actions) => {
      const old = report();
      old.plans![0].status = "pending";
      old.plans![0].actions = [
        { ...camera(), status: "pending" },
      ];
      delete old.plans![0].actions[0].outputs;
      const next = report();
      next.report_id = "2";
      next.to_wm = 30;
      next.plans![0].actions = actions;
      expect(() => mergeReport(old, next)).toThrow();
    },
  );
  it("拒绝 pending 父快照保留已开始子动作", () => {
    const old = report();
    old.plans![0].status = "running";
    const next = report();
    next.report_id = "2";
    next.to_wm = 30;
    next.plans![0].status = "pending";
    next.plans![0].actions = [];
    expect(() => mergeReport(old, next)).toThrow();
  });
  it("替换自身字段同时保留未出现的子实体", () => {
    const r = report();
    const next = report();
    next.report_id = "2";
    next.from_wm = 20;
    next.to_wm = 30;
    next.plans![0].actions = [{ ...camera(), outputs: [] }];
    expect(mergeReport(r, next).plans![0].actions).toHaveLength(2);
    expect(mergeReport(r, next).plans![0].actions![0].outputs).toHaveLength(1);
  });
  it("移除缺席的可选自身字段", () => {
    const r = report();
    r.plans![0].status = "running";
    r.plans![0].actions = [
      {
        action_instance_id: "3",
        name: "报告",
        type: "report_status",
        input_params: { scope: "full" },
        status: "running",
      },
    ];
    const next = structuredClone(r);
    next.report_id = "2";
    next.from_wm = 20;
    next.to_wm = 30;
    next.plans![0].status = "completed";
    next.plans![0].actions![0] = {
      ...next.plans![0].actions![0],
      status: "succeeded",
      result: { report_id: "1" },
    };
    expect(mergeReport(r, next).plans![0].actions![0].result).toBeDefined();
  });
  it("拒绝跨快照改变请求关联", () => {
    const r = report();
    const n = structuredClone(r);
    n.report_id = "2";
    n.to_wm = 30;
    n.plans![0].request_id = "other";
    expect(() => mergeReport(r, n)).toThrow();
  });
  it("旧报告不覆盖当前终态", () => {
    const r = report();
    const old = report();
    old.to_wm = 10;
    old.plans![0].status = "pending";
    old.plans![0].actions = [
      { ...camera(), status: "pending" },
    ];
    delete old.plans![0].actions[0].outputs;
    expect(mergeReport(r, old)).toEqual(r);
  });
  it("旧报告仍不得改变身份关联", () => {
    const r = report();
    const old = report();
    old.to_wm = 10;
    old.plans![0].request_id = "other";
    expect(() => mergeReport(r, old)).toThrow();
  });
  it("新报告不得改变既有动作终态", () => {
    const r = report();
    const n = report();
    n.report_id = "2";
    n.to_wm = 30;
    n.plans![0].actions![0].status = "failed";
    n.plans![0].actions![0].error = error;
    expect(() => mergeReport(r, n)).toThrow();
  });
  it("缺口不能应用部分结果", () => {
    const r = report();
    const n = report();
    n.report_id = "2";
    n.from_wm = 30;
    n.to_wm = 40;
    expect(() => mergeReport(r, n)).toThrow();
    expect(r.to_wm).toBe(20);
  });
      it("后续报告不能丢弃已经取得的源摘要", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].checksum = {
      status: "available",
      sha256: "a".repeat(64),
    };
    const n = report();
    n.report_id = "2";
    n.to_wm = 30;
    expect(() => mergeReport(r, n)).toThrow();
  });
});

describe("原请求选择范围", () => {
  it.each([
    { action_instance_id: "other" },
    { action_name: "另一录像" },
    { group: "其他组" },
    { plan_instance_id: "other", group: "组" },
  ])("拒绝与已知来源不匹配的交付 %#", (source) => {
    const r = report();
    r.plans![0].actions![0].group = "组";
    r.plans![0].actions![1].input_params = { source };
    expect(() => parse(r)).toThrow();
  });
  it.each([
    { action_instance_id: "1" },
    { action_name: "录像" },
    { group: "组" },
    { plan_instance_id: "1", group: "组" },
  ])("接受匹配的四种来源 %#", (source) => {
    const r = report();
    r.plans![0].actions![0].group = "组";
    r.plans![0].actions![1].input_params = { source };
    expect(parse(r).report_id).toBe("1");
  });
  it("拒绝列表外产物交付", () => {
    const r = report();
    r.plans![0].actions![1].input_params = {
      source: { action_instance_id: "1" },
      output_ids: ["other"],
    };
    expect(() => parse(r)).toThrow();
  });
  it("取回失败项也必须符合直接来源", () => {
    const r = report();
    r.plans![0].actions![1].status = "failed";
    r.plans![0].actions![1].error = error;
    r.plans![0].actions![1].result = {
      failures: [{ source_action_instance_id: "other", error }],
    };
    expect(() => parse(r)).toThrow();
  });
  it("未知局部来源先到时保留引用，晚到矛盾来源拒绝", () => {
    const old = report();
    old.plans![0].actions = [obtain()];
    old.plans![0].actions[0].input_params = {
      source: { action_name: "另一录像" },
    };
    expect(parse(old)).toEqual(old);
    const n = report();
    n.report_id = "2";
    n.from_wm = 20;
    n.to_wm = 30;
    n.plans![0].actions = [camera()];
    expect(() => mergeReport(old, n)).toThrow();
  });
});

describe("取消与清理目标审计", () => {
  const targets = [
    { request_id: "1" },
    { plan_instance_id: "1" },
    { action_instance_id: "1" },
    { plan_instance_id: "1", group: "组" },
  ];
  function canceled(target: unknown): StatusReport {
    const r = report();
    r.plans![0].actions![0].group = "组";
    r.plans![0].actions!.push({
      action_instance_id: "7",
      name: "取消",
      type: "cancel_task",
      input_params: { target },
      status: "succeeded",
      result: {
        items: [
          {
            action_instance_id: "1",
            status: "succeeded",
            outcome: "already_terminal",
          },
        ],
      },
    });
    return r;
  }
  it.each(targets)("四种取消目标匹配已知动作 %#", (target) =>
    expect(parse(canceled(target)).report_id).toBe("1"),
  );
  it.each([
    { request_id: "30" },
    { plan_instance_id: "30" },
    { action_instance_id: "30" },
    { plan_instance_id: "1", group: "其他" },
  ])("四种取消目标排除已知动作 %#", (target) =>
    expect(() => parse(canceled(target))).toThrow(),
  );
  it("取消撤回项必须属于该项目动作", () => {
    const r = canceled({ action_instance_id: "1" });
    r.plans![0].actions![2].result = {
      items: [
        {
          action_instance_id: "1",
          status: "succeeded",
          outcome: "already_terminal",
          withdrawals: [{ delivery_id: "1", status: "withdrawn" }],
        },
      ],
    };
    expect(() => parse(r)).toThrow();
  });
  it("取消尚未取得的目标不按不存在拒绝", () => {
    const r = canceled({ request_id: "20" });
    r.plans![0].actions![2].result = {
      items: [
        {
          action_instance_id: "21",
          status: "succeeded",
          outcome: "already_terminal",
        },
      ],
    };
    expect(parse(r).report_id).toBe("1");
  });
  it("清理结果严格保留请求顺序且允许合法进行中前缀", () => {
    const r = report();
    r.plans![0].status = "running";
    r.plans![0].actions!.push({
      action_instance_id: "6",
      name: "清理",
      type: "delete_action_outputs",
      scheduled_at: "2026-01-01 00:00:00",
      input_params: { output_ids: ["1", "2"] },
      status: "running",
      result: { items: [{ output_id: "1", status: "running" }] },
    });
    expect(parse(r).report_id).toBe("1");
    r.plans![0].actions![2].result = {
      items: [{ output_id: "2", status: "running" }],
    };
    expect(() => parse(r)).toThrow();
  });
});

function statusReport(
  status: ReportAction["status"] = "running",
): StatusReport {
  const r = report();
  r.plans![0].status =
    status === "pending"
      ? "pending"
      : status === "running"
        ? "running"
        : "completed";
  r.plans![0].actions = [
    {
      action_instance_id: "3",
      name: "同步",
      type: "report_status",
      input_params: { scope: "full" },
      status,
      ...(status === "succeeded" ? { result: { report_id: "1" } } : {}),
      ...(["failed", "expired"].includes(status) ? { error } : {}),
    },
  ];
  return r;
}

it.each([undefined, {}])("已受理的报告动作必须有明确同步范围 %j", (params) => {
  const r = statusReport();
  if (params === undefined) delete r.plans![0].actions![0].input_params;
  else r.plans![0].actions![0].input_params = params;
  expect(() => validateReport(r)).toThrow();
});

it.each([undefined, {}])("报告动作受理失败时保留不完整参数 %j", (params) => {
  const r = statusReport("failed");
  const action = r.plans![0].actions![0];
  action.error = { code: "invalid_params", stage: "admission", details: {} };
  if (params === undefined) delete action.input_params;
  else action.input_params = params;
  expect(parse(r)).toEqual(r);
});
function historyPair(
  early: StatusReport,
  late: StatusReport,
  direction: "earlier" | "same" | "later",
  valid: boolean,
) {
  early.to_wm = direction === "same" ? 20 : 10;
  late.to_wm = 20;
  early.report_id = "91";
  late.report_id = "2";
  validateReport(early);
  validateReport(late);
  const before = [structuredClone(early), structuredClone(late)];
  const check = () =>
    direction === "earlier"
      ? validateReportAgainstHistory(late, early)
      : validateReportAgainstHistory(early, late);
  if (valid) expect(check).not.toThrow();
  else expect(check).toThrow();
  expect([early, late]).toEqual(before);
}
describe("历史观察边界", () => {
  it.each(["earlier", "later"] as const)(
    "主动作合法前进与不可逆事实 %s",
    (direction) => {
      for (const [early, late, valid] of [
        ["pending", "succeeded", true],
        ["running", "succeeded", true],
        ["failed", "succeeded", false],
        ["succeeded", "running", false],
        ["running", "pending", false],
      ] as const)
        historyPair(statusReport(early), statusReport(late), direction, valid);
    },
  );
  it.each(["pending", "failed"] as const)(
    "同水位 succeeded 不能变成 %s",
    (status) =>
      historyPair(
        statusReport("succeeded"),
        statusReport(status),
        "same",
        false,
      ),
  );
  it.each(["error", "result", "media", "cleanup", "device_execution"] as const)(
    "同水位自身字段差异 %s",
    (field) => {
      const a =
        field === "error"
          ? statusReport("failed")
          : field === "result"
            ? statusReport("succeeded")
            : report();
      const b = structuredClone(a);
      if (field === "error")
        b.plans![0].actions![0].error = { ...error, code: "10" };
      if (field === "result") b.plans![0].actions![0].result = { report_id: "5" };
      if (field === "media")
        b.plans![0].actions![0].outputs![0].media.check_status = "running";
      if (field === "cleanup")
        b.plans![0].actions![0].outputs![0].cleanup = { status: "canceled" };
      if (field === "device_execution")
        b.plans![0].actions![0].device_execution = { status: "still_running" };
      historyPair(a, b, "same", false);
    },
  );
  describe("设备执行提示合并", () => {
    const later = (
      to: number,
      device_execution?: { status: "still_running" | "end_unconfirmed" },
    ): StatusReport => {
      const r = report();
      r.report_id = "2";
      r.from_wm = 20;
      r.to_wm = to;
      if (device_execution)
        r.plans![0].actions![0].device_execution = device_execution;
      return r;
    };
    it("较新报告使提示出现、改变并随省略而消失", () => {
      const appear = mergeReport(report(), later(40, { status: "still_running" }));
      expect(
        appear.plans![0].actions![0].device_execution?.status,
      ).toBe("still_running");
      const changed = mergeReport(
        appear,
        later(60, { status: "end_unconfirmed" }),
      );
      expect(
        changed.plans![0].actions![0].device_execution?.status,
      ).toBe("end_unconfirmed");
      const gone = mergeReport(changed, later(80));
      expect(gone.plans![0].actions![0].device_execution).toBeUndefined();
      expect(gone.plans![0].actions![0].status).toBe("succeeded");
    });
    it("提示消失后较旧报告不能使其重新出现", () => {
      const current = mergeReport(report(), later(40, { status: "still_running" }));
      const cleared = mergeReport(current, later(60));
      expect(cleared.plans![0].actions![0].device_execution).toBeUndefined();
      const older = later(50, { status: "still_running" });
      older.report_id = "3";
      const merged = mergeReport(cleared, older);
      expect(merged.plans![0].actions![0].device_execution).toBeUndefined();
      expect(merged.to_wm).toBe(60);
    });
  });
  it.each(["omit", "empty", "order"] as const)(
    "同水位集合%s不属于自身差异",
    (kind) => {
      const a = report();
      const b = structuredClone(a);
      if (kind === "order") b.plans![0].actions!.reverse();
      else if (kind === "omit") delete b.plans![0].actions;
      else b.plans![0].actions = [];
      historyPair(a, b, "same", true);
    },
  );
  it.each(["earlier", "same", "later"] as const)(
    "计划运行不能倒退 %s",
    (direction) => {
      const a = statusReport("pending");
      a.plans![0].status = "running";
      const b = statusReport("pending");
      historyPair(a, b, direction, false);
    },
  );
  it.each(["earlier", "same", "later"] as const)(
    "交付阶段倒退 %s",
    (direction) => {
      for (const [early, late] of [
        ["prepared", "pending"],
        ["publishing", "preparing"],
      ] as const) {
        const a = report();
        const b = report();
        a.plans![0].actions![1].deliveries![0].status = early;
        b.plans![0].actions![1].deliveries![0].status = late;
        historyPair(a, b, direction, false);
      }
    },
  );
  it.each(["earlier", "later"] as const)(
    "允许交付跨阶段及撤回 %s",
    (direction) => {
      for (const [early, late] of [
        ["pending", "published"],
        ["prepared", "publishing"],
        ["published", "withdrawn"],
      ] as const) {
        const a = report();
        const b = report();
        a.plans![0].actions![1].deliveries![0].status = early;
        b.plans![0].actions![1].deliveries![0].status = late;
        historyPair(a, b, direction, true);
      }
    },
  );
  it.each(["earlier", "same", "later"] as const)(
    "已知内容大小与摘要不能矛盾 %s",
    (direction) => {
      const a = report();
      const b = report();
      for (const [r, size, hash] of [
        [a, 100, "a"],
        [b, 200, "b"],
      ] as const) {
        const o = r.plans![0].actions![0].outputs![0];
        o.size = size;
        o.checksum = { status: "available", sha256: hash.repeat(64) };
        const d = r.plans![0].actions![1].deliveries![0];
        d.size = size;
        d.sha256 = hash.repeat(64);
      }
      historyPair(a, b, direction, false);
    },
  );
});

type ResultKind = "obtain" | "delete" | "cancel" | "withdrawal";
function resultReport(
  kind: ResultKind,
  result: Record<string, unknown>,
  status: ReportAction["status"] = "running",
): StatusReport {
  const r = statusReport(status);
  r.plans![0].actions = [
    {
      action_instance_id: "4",
      name: "处理",
      type:
        kind === "obtain"
          ? "obtain_action_outputs"
          : kind === "delete"
            ? "delete_action_outputs"
            : "cancel_task",
      scheduled_at: "2026-01-01 00:00:00",
      input_params:
        kind === "obtain"
          ? { source: { action_instance_id: "5" } }
          : kind === "delete"
            ? { output_ids: ["1", "2"] }
            : { target: { plan_instance_id: "6" } },
      status,
      result,
      ...(status === "failed" ? { error } : {}),
    },
  ];
  return r;
}
function finalResult(kind: ResultKind): Record<string, unknown> {
  if (kind === "obtain")
    return {
      failures: [
        { source_action_instance_id: "5", output_id: "1", error },
      ],
    };
  if (kind === "delete")
    return { items: [{ output_id: "1", status: "failed", error }] };
  return {
    items: [
      {
        action_instance_id: "7",
        status: "failed",
        error,
        ...(kind === "withdrawal"
          ? {
              withdrawals: [
                { delivery_id: "8", status: "failed", error },
              ],
            }
          : {}),
      },
    ],
  };
}
function resultEntries(
  result: Record<string, unknown>,
  kind: ResultKind,
): Array<Record<string, unknown>> {
  if (kind === "obtain")
    return result.failures as Array<Record<string, unknown>>;
  const items = result.items as Array<Record<string, unknown>>;
  return kind === "withdrawal"
    ? (items[0].withdrawals as Array<Record<string, unknown>>)
    : items;
}
describe("最终条目历史", () => {
  const kinds: ResultKind[] = ["obtain", "delete", "cancel", "withdrawal"];
  const directions = ["earlier", "same", "later"] as const;
  it.each(
    kinds.flatMap((kind) =>
      directions.flatMap((direction) =>
        ["missing", "identity", "error", "status"]
          .filter(
            (change) =>
              (kind !== "obtain" || change !== "status") &&
              (kind !== "delete" || change !== "identity"),
          )
          .map((change) => ({ kind, direction, change })),
      ),
    ),
  )("拒绝 $kind 的 $change，边界 $direction", ({ kind, direction, change }) => {
    const before = finalResult(kind);
    const after = structuredClone(before);
    const items = resultEntries(after, kind);
    if (change === "missing") items.length = 0;
    if (change === "error") items[0].error = { ...error, code: "changed" };
    if (change === "identity") {
      const key =
        kind === "obtain" || kind === "delete"
          ? "output_id"
          : kind === "cancel"
            ? "action_instance_id"
            : "delivery_id";
      items[0][key] = kind === "delete" ? "2" : "99";
    }
    if (change === "status") {
      items[0].status = kind === "withdrawal" ? "withdrawn" : "succeeded";
      delete items[0].error;
      if (kind !== "withdrawal")
        items[0].outcome = kind === "delete" ? "deleted" : "already_terminal";
    }
    historyPair(
      resultReport(kind, before, "failed"),
      resultReport(kind, after, "failed"),
      direction,
      false,
    );
  });
  it.each(["delete", "cancel", "withdrawal"] as const)(
    "缺少已登记未完成 $0 项不能解释为空集合",
    (kind) => {
      const before = finalResult(kind);
      const entries = resultEntries(before, kind);
      entries[0].status = "pending";
      delete entries[0].error;
      if (kind === "withdrawal") {
        const item = (before.items as Array<Record<string, unknown>>)[0];
        item.status = "running";
        delete item.error;
      }
      const after = structuredClone(before);
      resultEntries(after, kind).length = 0;
      historyPair(
        resultReport(kind, before),
        resultReport(kind, after),
        "later",
        false,
      );
    },
  );
  it.each(
    kinds.flatMap((kind) =>
      directions.map((direction) => ({ kind, direction })),
    ),
  )("允许 $kind 新独立条目按时间增加：$direction", ({ kind, direction }) => {
    const before = finalResult(kind);
    if (kind === "withdrawal") {
      const item = (before.items as Array<Record<string, unknown>>)[0];
      item.status = "running";
      delete item.error;
    }
    const after = structuredClone(before);
    const entry = structuredClone(resultEntries(after, kind)[0]);
    if (kind === "obtain" || kind === "delete") entry.output_id = "2";
    else if (kind === "cancel") entry.action_instance_id = "10";
    else entry.delivery_id = "10";
    resultEntries(after, kind).push(entry);
    historyPair(
      resultReport(kind, before),
      resultReport(kind, after),
      direction,
      direction !== "same",
    );
  });
  it.each(
    ["delete", "cancel", "withdrawal"].flatMap((kind) =>
      directions.map((direction) => ({ kind: kind as ResultKind, direction })),
    ),
  )("允许 $kind 未完成项取得最终结果：$direction", ({ kind, direction }) => {
    const after = finalResult(kind);
    const before = structuredClone(after);
    const item = resultEntries(before, kind)[0];
    item.status = "pending";
    delete item.error;
    if (kind === "withdrawal")
      for (const result of [before, after]) {
        const parent = (result.items as Array<Record<string, unknown>>)[0];
        parent.status = "running";
        delete parent.error;
      }
    historyPair(
      resultReport(kind, before),
      resultReport(kind, after),
      direction,
      direction !== "same",
    );
  });
  it.each(["delete", "cancel"] as const)("最终 $0 outcome 不可改写", (kind) => {
    const before = finalResult(kind);
    const first = resultEntries(before, kind)[0];
    first.status = "succeeded";
    delete first.error;
    first.outcome = kind === "delete" ? "deleted" : "canceled";
    const after = structuredClone(before);
    resultEntries(after, kind)[0].outcome =
      kind === "delete" ? "absence_confirmed" : "already_terminal";
    historyPair(
      resultReport(kind, before),
      resultReport(kind, after),
      "later",
      false,
    );
  });
  it("较早尚无最终失败可以由较晚补充", () =>
    historyPair(
      resultReport("obtain", { failures: [] }),
      resultReport("obtain", finalResult("obtain")),
      "earlier",
      true,
    ));
});

describe("最终条目历史状态覆盖", () => {
  it.each([
    ["delete", "succeeded"],
    ["delete", "failed"],
    ["delete", "canceled"],
    ["cancel", "succeeded"],
    ["cancel", "failed"],
    ["withdrawal", "withdrawn"],
    ["withdrawal", "not_retractable"],
    ["withdrawal", "failed"],
  ] as const)("%s 的最终 %s 条目必须保留", (kind, status) => {
    const before = finalResult(kind);
    const item = resultEntries(before, kind)[0];
    item.status = status;
    if (status !== "failed") delete item.error;
    if (status === "succeeded")
      item.outcome = kind === "delete" ? "deleted" : "canceled";
    const after = structuredClone(before);
    resultEntries(after, kind).length = 0;
    historyPair(
      resultReport(kind, before, "failed"),
      resultReport(kind, after, "failed"),
      "later",
      false,
    );
  });
  it.each(["delete", "cancel"] as const)(
    "%s 的 running 条目不得退回 pending",
    (kind) => {
      const before = finalResult(kind);
      const first = resultEntries(before, kind)[0];
      first.status = "running";
      delete first.error;
      const after = structuredClone(before);
      resultEntries(after, kind)[0].status = "pending";
      historyPair(
        resultReport(kind, before),
        resultReport(kind, after),
        "later",
        false,
      );
    },
  );
  it.each(["5", "output", "delivery"] as const)(
    "取回最终失败的 %s 身份层级完整保留",
    (level) => {
      const failure = {
        source_action_instance_id: "5",
        ...(level !== "5" ? { output_id: "1" } : {}),
        ...(level === "delivery" ? { delivery_id: "1" } : {}),
        error,
      };
      const a = resultReport("obtain", { failures: [failure] });
      const b = resultReport("obtain", { failures: [] });
      historyPair(a, b, "earlier", false);
    },
  );
  it("动作本身没有出现在较晚增量中时保留整个动作", () => {
    const old = resultReport("delete", finalResult("delete"), "failed");
    const next = structuredClone(old);
    next.report_id = "2";
    next.from_wm = 20;
    next.to_wm = 30;
    next.plans![0].actions = [];
    expect(mergeReport(old, next).plans![0].actions).toEqual(
      old.plans![0].actions,
    );
  });
});


function failureReport(status: Delivery["status"] = "failed"): StatusReport {
  const r = report();
  r.plans![0].status = "running";
  const action = obtain();
  action.status = "running";
  action.result = {
    failures:
      status === "failed"
        ? [
            {
              source_action_instance_id: "1",
              output_id: "1",
              delivery_id: "1",
              error,
            },
          ]
        : [],
  };
  action.deliveries![0].status = status;
  if (status === "failed") action.deliveries![0].error = error;
  r.plans![0].actions = [action];
  return r;
}
describe("交付最终失败关联", () => {
  it("同一最终错误允许未知码且不等于其他层次错误", () => {
    const r = failureReport();
    const action = r.plans![0].actions![0];
    action.status = "failed";
    action.error = { ...error, code: "obtain_items_failed" };
    r.plans![0].status = "completed";
    expect(() => validateReport(r)).not.toThrow();
  });
  it.each(["code", "stage", "details"] as const)(
    "同边界最终错误的 %s 不同拒绝",
    (field) => {
      const r = failureReport();
      const failures = (
        r.plans![0].actions![0].result as {
          failures: Array<{ error: Record<string, unknown> }>;
        }
      ).failures;
      failures[0].error = {
        ...error,
        [field]: field === "details" ? { reason: "different" } : "different",
      };
      expect(() => validateReport(r)).toThrow();
    },
  );
  it.each([
    "pending",
    "preparing",
    "prepared",
    "publishing",
    "published",
    "withdrawn",
    "canceled",
  ] as const)("最终 failure 不能同时对应 %s", (status) => {
    const r = failureReport();
    r.plans![0].actions![0].deliveries![0].status = status;
    expect(() => validateReport(r)).toThrow();
  });
  it("failed delivery 需要所属完整 result 的对应 failure", () => {
    const r = failureReport();
    r.plans![0].actions![0].result = { failures: [] };
    expect(() => validateReport(r)).toThrow();
  });
  it("来源级失败不能代替已知 failed delivery 的失败项", () => {
    const r = failureReport();
    r.plans![0].actions![0].result = {
      failures: [{ source_action_instance_id: "1", error }],
    };
    expect(() => validateReport(r)).toThrow();
  });
  it("failure 的 delivery 缺席时保留待关联", () => {
    const r = failureReport();
    delete r.plans![0].actions![0].deliveries;
    expect(() => validateReport(r)).not.toThrow();
  });
  it.each([undefined, "1"])(
    "未带delivery ID的失败不推断属于同产物交付 %#",
    (outputId) => {
      const r = failureReport("preparing");
      r.plans![0].actions![0].result = {
        failures: [
          {
            source_action_instance_id: "1",
            ...(outputId ? { output_id: outputId } : {}),
            error,
          },
        ],
      };
      expect(() => validateReport(r)).not.toThrow();
    },
  );
  it("错误对象键序不影响相等", () => {
    const r = failureReport();
    r.plans![0].actions![0].deliveries![0].error = {
      details: {},
      stage: "execution",
      code: "future_code",
    };
    expect(() => validateReport(r)).not.toThrow();
  });
  it.each(["missing", "null", "array-order"] as const)(
    "错误details保留值区别 %s",
    (change) => {
      const r = failureReport();
      const a = r.plans![0].actions![0];
      const first =
        change === "array-order"
          ? { list: [1, 2] }
          : change === "missing"
            ? {}
            : { value: null };
      const second =
        change === "array-order" ? { list: [2, 1] } : { value: "null" };
      a.deliveries![0].error = { ...error, details: first };
      a.result = {
        failures: [
          {
            source_action_instance_id: "1",
            output_id: "1",
            delivery_id: "1",
            error: { ...error, details: second },
          },
        ],
      };
      expect(() => validateReport(r)).toThrow();
    },
  );
});

function failureOnly(): StatusReport {
  const r = failureReport();
  delete r.plans![0].actions![0].deliveries;
  return r;
}
describe("交付最终失败关联的历史水位", () => {
  it.each(
    (
      [
        "pending",
        "preparing",
        "prepared",
        "publishing",
        "published",
        "withdrawn",
        "canceled",
      ] as const
    ).flatMap((status) =>
      (["earlier", "same", "later"] as const).map((direction) => ({
        status,
        direction,
      })),
    ),
  )("旧 $status 与较晚 failure，输入 $direction", ({ status, direction }) =>
    historyPair(
      failureReport(status),
      failureOnly(),
      direction,
      direction !== "same" &&
        !["published", "withdrawn", "canceled"].includes(status),
    ),
  );
  it.each(
    (
      [
        "pending",
        "preparing",
        "prepared",
        "publishing",
        "published",
        "withdrawn",
        "canceled",
      ] as const
    ).flatMap((status) =>
      (["earlier", "same", "later"] as const).map((direction) => ({
        status,
        direction,
      })),
    ),
  )(
    "较早 failure 不能恢复为 $status，输入 $direction",
    ({ status, direction }) =>
      historyPair(failureOnly(), failureReport(status), direction, false),
  );
  it.each(["earlier", "same", "later"] as const)(
    "两个最终错误不等不能由时间差解释 %s",
    (direction) => {
      const a = failureReport();
      const b = failureOnly();
      b.plans![0].actions![0].result = {
        failures: [
          {
            source_action_instance_id: "1",
            output_id: "1",
            delivery_id: "1",
            error: { ...error, code: "changed" },
          },
        ],
      };
      historyPair(a, b, direction, false);
    },
  );
  it.each(["earlier", "same", "later"] as const)(
    "同一最终错误保持且delivery缺席合法 %s",
    (direction) => historyPair(failureReport(), failureOnly(), direction, true),
  );
  it.each(["earlier", "same", "later"] as const)(
    "已知failed delivery不能在较晚完整父结果中漏项 %s",
    (direction) => {
      const a = failureReport();
      const b = failureOnly();
      b.plans![0].actions![0].result = { failures: [] };
      historyPair(a, b, direction, false);
    },
  );
  it.each(["earlier", "later"] as const)(
    "较早无failure与较晚failed delivery相容 %s",
    (direction) => {
      const a = failureOnly();
      a.plans![0].actions![0].result = { failures: [] };
      historyPair(a, failureReport(), direction, true);
    },
  );
  it.each(["apply", "gap"] as const)(
    "历史允许较早准备，%s 依据真实覆盖分类",
    (mode) => {
      const current = failureReport("preparing");
      current.to_wm = 10;
      const incoming = failureOnly();
      incoming.report_id = "2";
      incoming.to_wm = 30;
      incoming.from_wm = mode === "apply" ? 10 : 20;
      const saved = structuredClone(current);
      expect(() =>
        validateReportAgainstHistory(current, incoming),
      ).not.toThrow();
      if (mode === "apply")
        expect(() => mergeReport(current, incoming)).toThrow(/状态矛盾/);
      else
        expect(
          reportDecision(current.to_wm, incoming.from_wm, incoming.to_wm),
        ).toBe("gap");
      expect(current).toEqual(saved);
    },
  );
  it("连续报告同时给出failed交付和同源错误可以应用", () => {
    const current = failureReport("preparing");
    current.to_wm = 10;
    const incoming = failureReport();
    incoming.to_wm = 30;
    incoming.from_wm = 10;
    incoming.report_id = "2";
    expect(mergeReport(current, incoming).to_wm).toBe(30);
  });
  it("来自其他取回动作的同output交付不能认领failure", () => {
    const r = failureOnly();
    const other = obtain();
    other.action_instance_id = "other";
    other.name = "另一取回";
    other.status = "running";
    r.plans![0].actions!.push(other);
    expect(() => validateReport(r)).toThrow();
  });
});
