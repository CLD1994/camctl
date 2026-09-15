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
const output = (id = "o1"): Output => ({
  output_id: id,
  source_action_instance_id: "a1",
  kind: "original",
  availability: "available",
  cleanup: { status: "not_requested" },
  checksum: { status: "not_obtained" },
  media: { check_status: "not_performed", duration: { status: "unknown" } },
});
const camera = (): ReportAction => ({
  action_instance_id: "a1",
  name: "录像",
  type: "camera_record",
  device_id: "cam0",
  scheduled_at: "2026-01-01 00:00:00",
  input_params: { type: "historical" },
  effective_params: { type: "historical" },
  policy: { max_delay_ms: 0 },
  status: "succeeded",
  execution: { started: true },
  outputs: [output()],
});
const delivery = (): Delivery => ({
  delivery_id: "d1",
  output_id: "o1",
  source_action_instance_id: "a1",
  file_name: "d1.mp4",
  display_name: "录像.mp4",
  status: "published",
  size: 100,
  sha256: "a".repeat(64),
  copy: {
    max_read_attempts: 3,
    read_idle_timeout_s: 10,
    max_recopies: 1,
    recopies_used: 0,
    round: 1,
    committed_bytes: 100,
    source_size: 100,
    read_attempts: [{ attempt_no: 1, status: "succeeded" }],
    verification: { status: "source_checksum_unavailable" },
    work_file_cleanup: { status: "not_needed" },
  },
});
const obtain = (): ReportAction => ({
  action_instance_id: "a2",
  name: "取回",
  type: "obtain_action_outputs",
  scheduled_at: "2026-01-01 00:00:00",
  input_params: { source: { action_instance_id: "a1" } },
  status: "succeeded",
  execution: { started: true },
  result: { failures: [] },
  deliveries: [delivery()],
});
const report = (): StatusReport => ({
  report_id: 1,
  from_wm: 0,
  to_wm: 20,
  plans: [
    {
      plan_instance_id: "p1",
      request_id: "r1",
      plan_seq: 1,
      created_at: "2026-01-01 00:00:00",
      name: "计划",
      status: "completed",
      actions: [camera(), obtain()],
    },
  ],
});
function parse(value: unknown, text = JSON.stringify(value)) {
  const bytes = new TextEncoder().encode(text);
  return parseReport(
    `status-report-1-${createHash("sha256").update(bytes).digest("hex")}.json`,
    bytes,
  );
}
describe("parseReport", () => {
  it("接受完整报告且不依赖当前能力说明", () =>
    expect(parse(report())).toEqual(report()));
  it("接受只包含诊断的报告及未知错误码", () =>
    expect(
      parse({
        report_id: 1,
        from_wm: 0,
        to_wm: 1,
        plan_file_diagnostics: [
          {
            diagnostic_id: "x",
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
        { report_id: 1, from_wm: 0, to_wm: 0 },
        ' { "report_id": 1, "from_wm":0, "to_wm":0 }\n',
      ).report_id,
    ).toBe(1));
  it("拒绝文件名身份与根不一致", () =>
    expect(() => parse({ report_id: 2, from_wm: 0, to_wm: 1 })).toThrow());
  it("拒绝重复转义成员", () =>
    expect(() =>
      parse({}, '{"report_id":1,"from_wm":0,"to_wm":1,"\\u0074o_wm":2}'),
    ).toThrow());
  it("拒绝逆序水位", () =>
    expect(() => parse({ report_id: 1, from_wm: 2, to_wm: 1 })).toThrow());
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
        action_instance_id: "a1",
        name: "录像",
        type: "camera_record",
        device_id: null,
        group: false,
        scheduled_at: 5,
        input_params: null,
        policy: null,
        extra_input_fields: { typo: true },
        status: "failed",
        execution: { started: false },
        error: { ...error, stage: "admission" },
      },
    ];
    expect(parse(r).plans![0].actions![0].input_params).toBeNull();
  });
  it("拒绝跨计划重复动作身份", () => {
    const r = report();
    r.plans!.push({
      ...r.plans![0],
      plan_instance_id: "p2",
      request_id: "r2",
      plan_seq: 2,
    });
    expect(() => parse(r)).toThrow();
  });
  it("拒绝产物归属与父动作不符", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].source_action_instance_id = "a2";
    expect(() => parse(r)).toThrow();
  });
  it("拒绝交付的来源与已知产物不符", () => {
    const r = report();
    r.plans![0].actions![1].deliveries![0].source_action_instance_id = "a2";
    expect(() => parse(r)).toThrow();
  });
  it("来源尚未出现时保留交付引用", () => {
    const r = report();
    r.plans![0].actions = [obtain()];
    expect(parse(r).plans![0].actions![0].deliveries![0].output_id).toBe("o1");
  });
  it("拒绝交付文件名截短身份", () => {
    const r = report();
    r.plans![0].actions![1].deliveries![0].file_name = "d.mp4";
    expect(() => parse(r)).toThrow();
  });
  it("拒绝已准备交付的进度不等于完整长度", () => {
    const r = report();
    r.plans![0].actions![1].deliveries![0].copy.committed_bytes = 99;
    expect(() => parse(r)).toThrow();
  });
  it("拒绝读取尝试不连续", () => {
    const r = report();
    r.plans![0].actions![1].deliveries![0].copy.read_attempts[0].attempt_no = 2;
    expect(() => parse(r)).toThrow();
  });
  it("拒绝读取尝试超过上限", () => {
    const r = report();
    const c = r.plans![0].actions![1].deliveries![0].copy;
    c.max_read_attempts = 1;
    c.read_attempts.push({ attempt_no: 2, status: "succeeded" });
    expect(() => parse(r)).toThrow();
  });
  it("拒绝拷贝轮次与重拷计数矛盾", () => {
    const r = report();
    r.plans![0].actions![1].deliveries![0].copy.round = 2;
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
        action_instance_id: "c",
        name: "取消",
        type: "cancel_task",
        status: "pending",
        execution: { started: false },
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
    r.plans![0].actions = [
      {
        action_instance_id: "c",
        name: "取消",
        type: "cancel_task",
        status: "canceled",
        execution: { started: false },
        input_params: { target: { request_id: "r2" } },
        result: { items: [] },
      },
    ];
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
          { report_id: 100, to_wm: 10 },
          { report_id: 2, to_wm: 20 },
          { report_id: 3, to_wm: 40 },
        ],
        20,
      ),
    ).toBe(2));
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
        { ...camera(), status: "pending", execution: { started: false } },
      ];
      delete old.plans![0].actions[0].outputs;
      const next = report();
      next.report_id = 2;
      next.to_wm = 30;
      next.plans![0].actions = actions;
      expect(() => mergeReport(old, next)).toThrow();
    },
  );
  it("拒绝 pending 父快照保留已开始子动作", () => {
    const old = report();
    old.plans![0].status = "running";
    const next = report();
    next.report_id = 2;
    next.to_wm = 30;
    next.plans![0].status = "pending";
    next.plans![0].actions = [];
    expect(() => mergeReport(old, next)).toThrow();
  });
  it("替换自身字段同时保留未出现的子实体", () => {
    const r = report();
    const next = report();
    next.report_id = 2;
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
        action_instance_id: "a3",
        name: "报告",
        type: "report_status",
        status: "running",
        execution: { started: true },
        waiting: [{ code: "report_publication", details: {} }],
      },
    ];
    const next = structuredClone(r);
    next.report_id = 2;
    next.from_wm = 20;
    next.to_wm = 30;
    next.plans![0].status = "completed";
    next.plans![0].actions![0] = {
      ...next.plans![0].actions![0],
      status: "succeeded",
      result: { report_id: 1 },
    };
    delete next.plans![0].actions![0].waiting;
    expect(mergeReport(r, next).plans![0].actions![0].waiting).toBeUndefined();
  });
  it("拒绝跨快照改变请求关联", () => {
    const r = report();
    const n = structuredClone(r);
    n.report_id = 2;
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
      { ...camera(), status: "pending", execution: { started: false } },
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
    n.report_id = 2;
    n.to_wm = 30;
    n.plans![0].actions![0].status = "failed";
    n.plans![0].actions![0].error = error;
    expect(() => mergeReport(r, n)).toThrow();
  });
  it("缺口不能应用部分结果", () => {
    const r = report();
    const n = report();
    n.report_id = 2;
    n.from_wm = 30;
    n.to_wm = 40;
    expect(() => mergeReport(r, n)).toThrow();
    expect(r.to_wm).toBe(20);
  });
  it("后续报告不能重置已用读取尝试", () => {
    const r = report();
    const n = report();
    n.report_id = 2;
    n.to_wm = 30;
    n.plans![0].actions![1].deliveries![0].copy.read_attempts = [];
    expect(() => mergeReport(r, n)).toThrow();
  });
  it("后续报告不能改写固化读取预算", () => {
    const r = report();
    const n = report();
    n.report_id = 2;
    n.to_wm = 30;
    n.plans![0].actions![1].deliveries![0].copy.max_read_attempts = 5;
    expect(() => mergeReport(r, n)).toThrow();
  });
  it("后续报告不能丢弃已经取得的源摘要", () => {
    const r = report();
    r.plans![0].actions![0].outputs![0].checksum = {
      status: "available",
      sha256: "a".repeat(64),
    };
    const n = report();
    n.report_id = 2;
    n.to_wm = 30;
    expect(() => mergeReport(r, n)).toThrow();
  });
});

function flowReport(): StatusReport {
  const r = report();
  r.plans![0].actions = [
    {
      ...camera(),
      status: "failed",
      error,
      result: {
        recording: {
          start: { max_attempts: 3, attempts: [] },
          stop: { max_attempts: 3, attempts: [] },
          followup_stops: [
            {
              flow_id: "f1",
              trigger_action_instance_id: "trigger",
              max_attempts: 3,
              status: "running",
              attempts: [{ attempt_no: 1, status: "failed", error }],
            },
          ],
        },
      },
    },
  ];
  return r;
}
describe("收场历史保护", () => {
  it("应急未确认结果由后续独立流程补充而非改写", () => {
    const old = flowReport();
    (
      old.plans![0].actions![0].result as CameraResult
    ).recording!.emergency_stops = [
      {
        flow_id: "e1",
        session_id: "s1",
        max_attempts: 3,
        attempts_used: 1,
        outcome: "unconfirmed",
      },
    ];
    const n = structuredClone(old);
    n.report_id = 2;
    n.to_wm = 30;
    const flows = (n.plans![0].actions![0].result as CameraResult).recording!
      .emergency_stops!;
    flows[0].outcome = "stopped";
    expect(() => mergeReport(old, n)).toThrow();
    flows[0].outcome = "unconfirmed";
    flows.push({
      flow_id: "e2",
      session_id: "s2",
      max_attempts: 3,
      attempts_used: 1,
      outcome: "stopped",
    });
    expect(mergeReport(old, n).to_wm).toBe(30);
  });
  it.each(["start", "stop", "delivery"])(
    "旧覆盖报告也不能改变已登记预算 %s",
    (kind) => {
      const current = flowReport();
      current.plans![0].actions!.push(obtain());
      const old = structuredClone(current);
      old.report_id = 2;
      old.to_wm = 10;
      if (kind === "delivery")
        old.plans![0].actions![1].deliveries![0].copy.max_read_attempts = 4;
      else
        (old.plans![0].actions![0].result as CameraResult).recording![
          kind as "start" | "stop"
        ].max_attempts = 4;
      expect(() => mergeReport(current, old)).toThrow();
    },
  );
  it.each(["omit", "attempts", "trigger", "budget", "result"])(
    "拒绝新快照丢失或改写已登记收场 %s",
    (change) => {
      const old = flowReport();
      const n = structuredClone(old);
      n.report_id = 2;
      n.from_wm = 20;
      n.to_wm = 30;
      const recording = (n.plans![0].actions![0].result as CameraResult)
        .recording!;
      const f = recording.followup_stops![0];
      if (change === "omit") delete recording.followup_stops;
      if (change === "attempts") f.attempts = [];
      if (change === "trigger") f.trigger_action_instance_id = "other";
      if (change === "budget") f.max_attempts = 4;
      if (change === "result")
        f.attempts[0] = { attempt_no: 1, status: "succeeded" };
      const before = structuredClone(old);
      expect(() => mergeReport(old, n)).toThrow();
      expect(old).toEqual(before);
    },
  );
  it("允许原流程运行尝试取得结果并追加合法流程", () => {
    const old = flowReport();
    (
      old.plans![0].actions![0].result as CameraResult
    ).recording!.followup_stops![0].attempts = [
      { attempt_no: 1, status: "running" },
    ];
    const n = structuredClone(old);
    n.report_id = 2;
    n.to_wm = 30;
    const flows = (n.plans![0].actions![0].result as CameraResult).recording!
      .followup_stops!;
    flows[0].attempts = [{ attempt_no: 1, status: "succeeded" }];
    flows[0].status = "stopped";
    flows.push({
      flow_id: "f2",
      trigger_action_instance_id: "another",
      max_attempts: 2,
      attempts: [],
      status: "pending",
    });
    expect(mergeReport(old, n).to_wm).toBe(30);
  });
  it("旧覆盖收场进度不按正向恢复规则拒绝", () => {
    const old = flowReport();
    const current = structuredClone(old);
    current.report_id = 2;
    current.to_wm = 30;
    (
      current.plans![0].actions![0].result as CameraResult
    ).recording!.followup_stops![0].attempts.push({
      attempt_no: 2,
      status: "succeeded",
    });
    expect(mergeReport(current, old)).toEqual(current);
  });
  it.each(["omit", "session", "budget", "used"])(
    "拒绝改写已补记应急停止 %s",
    (change) => {
      const old = flowReport();
      (
        old.plans![0].actions![0].result as CameraResult
      ).recording!.emergency_stops = [
        {
          flow_id: "e1",
          session_id: "s1",
          max_attempts: 3,
          attempts_used: 1,
          outcome: "stopped",
        },
      ];
      const n = structuredClone(old);
      n.report_id = 2;
      n.to_wm = 30;
      const recording = (n.plans![0].actions![0].result as CameraResult)
        .recording!;
      const e = recording.emergency_stops![0];
      if (change === "omit") delete recording.emergency_stops;
      if (change === "session") e.session_id = "s2";
      if (change === "budget") e.max_attempts = 4;
      if (change === "used") e.attempts_used = 0;
      expect(() => mergeReport(old, n)).toThrow();
    },
  );
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
    { action_instance_id: "a1" },
    { action_name: "录像" },
    { group: "组" },
    { plan_instance_id: "p1", group: "组" },
  ])("接受匹配的四种来源 %#", (source) => {
    const r = report();
    r.plans![0].actions![0].group = "组";
    r.plans![0].actions![1].input_params = { source };
    expect(parse(r).report_id).toBe(1);
  });
  it("拒绝列表外产物交付", () => {
    const r = report();
    r.plans![0].actions![1].input_params = {
      source: { action_instance_id: "a1" },
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
    n.report_id = 2;
    n.from_wm = 20;
    n.to_wm = 30;
    n.plans![0].actions = [camera()];
    expect(() => mergeReport(old, n)).toThrow();
  });
});

describe("取消与清理目标审计", () => {
  const targets = [
    { request_id: "r1" },
    { plan_instance_id: "p1" },
    { action_instance_id: "a1" },
    { plan_instance_id: "p1", group: "组" },
  ];
  function canceled(target: unknown): StatusReport {
    const r = report();
    r.plans![0].actions![0].group = "组";
    r.plans![0].actions!.push({
      action_instance_id: "cancel",
      name: "取消",
      type: "cancel_task",
      input_params: { target },
      status: "succeeded",
      execution: { started: true },
      result: {
        items: [
          {
            action_instance_id: "a1",
            status: "succeeded",
            outcome: "already_terminal",
          },
        ],
      },
    });
    return r;
  }
  it.each(targets)("四种取消目标匹配已知动作 %#", (target) =>
    expect(parse(canceled(target)).report_id).toBe(1),
  );
  it.each([
    { request_id: "wrong" },
    { plan_instance_id: "wrong" },
    { action_instance_id: "wrong" },
    { plan_instance_id: "p1", group: "其他" },
  ])("四种取消目标排除已知动作 %#", (target) =>
    expect(() => parse(canceled(target))).toThrow(),
  );
  it("取消撤回项必须属于该项目动作", () => {
    const r = canceled({ action_instance_id: "a1" });
    r.plans![0].actions![2].result = {
      items: [
        {
          action_instance_id: "a1",
          status: "succeeded",
          outcome: "already_terminal",
          withdrawals: [{ delivery_id: "d1", status: "withdrawn" }],
        },
      ],
    };
    expect(() => parse(r)).toThrow();
  });
  it("取消尚未取得的目标不按不存在拒绝", () => {
    const r = canceled({ request_id: "future" });
    r.plans![0].actions![2].result = {
      items: [
        {
          action_instance_id: "future-action",
          status: "succeeded",
          outcome: "already_terminal",
        },
      ],
    };
    expect(parse(r).report_id).toBe(1);
  });
  it("清理结果严格保留请求顺序且允许合法进行中前缀", () => {
    const r = report();
    r.plans![0].status = "running";
    r.plans![0].actions!.push({
      action_instance_id: "delete",
      name: "清理",
      type: "delete_action_outputs",
      scheduled_at: "2026-01-01 00:00:00",
      input_params: { output_ids: ["o1", "o2"] },
      status: "running",
      execution: { started: true },
      result: { items: [{ output_id: "o1", status: "running" }] },
    });
    expect(parse(r).report_id).toBe(1);
    r.plans![0].actions![2].result = {
      items: [{ output_id: "o2", status: "running" }],
    };
    expect(() => parse(r)).toThrow();
  });
});
describe("录像与拷贝历史审计", () => {
  it.each(["start", "stop", "source_copy", "delivery"] as const)(
    "新自身快照保留 %s 的已登记尝试",
    (kind) => {
      const old = flowReport();
      old.plans![0].actions!.push(obtain());
      const result = old.plans![0].actions![0].result as CameraResult;
      if (kind === "source_copy")
        result.source_copy = structuredClone(delivery().copy);
      else if (kind === "start" || kind === "stop")
        result.recording![kind].attempts = [
          { attempt_no: 1, status: "failed", error },
        ];
      const n = structuredClone(old);
      n.report_id = 2;
      n.to_wm = 30;
      const next = n.plans![0].actions![0].result as CameraResult;
      if (kind === "source_copy") next.source_copy!.read_attempts = [];
      else if (kind === "delivery")
        n.plans![0].actions![1].deliveries![0].copy.read_attempts = [];
      else next.recording![kind].attempts = [];
      expect(() => mergeReport(old, n)).toThrow();
    },
  );
});

it("后续收场已知触发动作必须属于同一相机", () => {
  const r = flowReport();
  const trigger = {
    ...camera(),
    action_instance_id: "trigger",
    name: "后续录像",
    device_id: "other-camera",
    outputs: [],
  };
  r.plans![0].actions!.push(trigger);
  expect(() => parse(r)).toThrow();
  trigger.device_id = "cam0";
  expect(parse(r).report_id).toBe(1);
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
      action_instance_id: "sync",
      name: "同步",
      type: "report_status",
      input_params: { scope: "full" },
      status,
      execution: { started: status !== "pending" },
      ...(status === "succeeded" ? { result: { report_id: 1 } } : {}),
      ...(["failed", "expired"].includes(status) ? { error } : {}),
    },
  ];
  return r;
}
function historyPair(
  early: StatusReport,
  late: StatusReport,
  direction: "earlier" | "same" | "later",
  valid: boolean,
) {
  early.to_wm = direction === "same" ? 20 : 10;
  late.to_wm = 20;
  early.report_id = 91;
  late.report_id = 2;
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
  it.each(["waiting", "error", "result", "media", "cleanup", "copy"] as const)(
    "同水位自身字段差异 %s",
    (field) => {
      const a =
        field === "waiting"
          ? statusReport()
          : field === "error"
            ? statusReport("failed")
            : field === "result"
              ? statusReport("succeeded")
              : report();
      const b = structuredClone(a);
      if (field === "waiting")
        b.plans![0].actions![0].waiting = [
          { code: "report_publication", details: {} },
        ];
      if (field === "error")
        b.plans![0].actions![0].error = { ...error, code: "another" };
      if (field === "result") b.plans![0].actions![0].result = { report_id: 5 };
      if (field === "media")
        b.plans![0].actions![0].outputs![0].media.check_status = "running";
      if (field === "cleanup")
        b.plans![0].actions![0].outputs![0].cleanup = { status: "canceled" };
      if (field === "copy")
        b.plans![0].actions![1].deliveries![0].copy.work_file_cleanup = {
          status: "completed",
        };
      historyPair(a, b, "same", false);
    },
  );
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
        d.copy.committed_bytes = size;
        d.copy.source_size = size;
      }
      historyPair(a, b, direction, false);
    },
  );
  it.each(["earlier", "same", "later"] as const)(
    "已有尝试不能在较晚边界消失 %s",
    (direction) => {
      const a = flowReport();
      const b = structuredClone(a);
      (
        b.plans![0].actions![0].result as CameraResult
      ).recording!.followup_stops![0].attempts = [];
      historyPair(a, b, direction, false);
    },
  );
  it.each(["earlier", "later"] as const)(
    "较少尝试可以补充而非倒置拒绝 %s",
    (direction) => {
      const a = flowReport();
      const b = structuredClone(a);
      (
        a.plans![0].actions![0].result as CameraResult
      ).recording!.followup_stops![0].attempts = [];
      historyPair(a, b, direction, true);
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
      action_instance_id: "work",
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
          ? { source: { action_instance_id: "source" } }
          : kind === "delete"
            ? { output_ids: ["o1", "o2"] }
            : { target: { plan_instance_id: "target" } },
      status,
      execution: { started: true },
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
        { source_action_instance_id: "source", output_id: "o1", error },
      ],
    };
  if (kind === "delete")
    return { items: [{ output_id: "o1", status: "failed", error }] };
  return {
    items: [
      {
        action_instance_id: "target-action",
        status: "failed",
        error,
        ...(kind === "withdrawal"
          ? {
              withdrawals: [
                { delivery_id: "target-delivery", status: "failed", error },
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
      items[0][key] = kind === "delete" ? "o2" : "changed";
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
    if (kind === "obtain" || kind === "delete") entry.output_id = "o2";
    else if (kind === "cancel") entry.action_instance_id = "another";
    else entry.delivery_id = "another";
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
  it.each(["source", "output", "delivery"] as const)(
    "取回最终失败的 %s 身份层级完整保留",
    (level) => {
      const failure = {
        source_action_instance_id: "source",
        ...(level !== "source" ? { output_id: "o1" } : {}),
        ...(level === "delivery" ? { delivery_id: "d1" } : {}),
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
    next.report_id = 2;
    next.from_wm = 20;
    next.to_wm = 30;
    next.plans![0].actions = [];
    expect(mergeReport(old, next).plans![0].actions).toEqual(
      old.plans![0].actions,
    );
  });
});
describe("历史观察边界公共流程覆盖", () => {
  function withAttempts(
    kind: "start" | "stop" | "followup" | "source_copy" | "delivery",
  ) {
    const r = flowReport();
    r.plans![0].actions!.push(obtain());
    const cameraResult = r.plans![0].actions![0].result as CameraResult;
    if (kind === "start" || kind === "stop")
      cameraResult.recording![kind].attempts = [
        { attempt_no: 1, status: "failed", error },
      ];
    if (kind === "source_copy")
      cameraResult.source_copy = structuredClone(delivery().copy);
    const list =
      kind === "delivery"
        ? r.plans![0].actions![1].deliveries![0].copy.read_attempts
        : kind === "source_copy"
          ? cameraResult.source_copy!.read_attempts
          : kind === "followup"
            ? cameraResult.recording!.followup_stops![0].attempts
            : cameraResult.recording![kind].attempts;
    return { r, list };
  }
  it.each(
    (["start", "stop", "followup", "source_copy", "delivery"] as const).flatMap(
      (kind) =>
        (["earlier", "same", "later"] as const).map((direction) => ({
          kind,
          direction,
        })),
    ),
  )("$kind 已登记尝试不得消失：$direction", ({ kind, direction }) => {
    const a = withAttempts(kind);
    const b = withAttempts(kind);
    b.list.length = 0;
    historyPair(a.r, b.r, direction, false);
  });
  it.each(["earlier", "same", "later"] as const)(
    "同轮拷贝字节不能回退：%s",
    (direction) => {
      const a = report();
      const b = report();
      for (const r of [a, b])
        r.plans![0].actions![1].deliveries![0].status = "preparing";
      b.plans![0].actions![1].deliveries![0].copy.committed_bytes = 90;
      historyPair(a, b, direction, false);
    },
  );
  it.each(["earlier", "later"] as const)(
    "新的重拷轮次允许从较小字节重新开始：%s",
    (direction) => {
      const a = report();
      const b = report();
      for (const r of [a, b])
        r.plans![0].actions![1].deliveries![0].status = "preparing";
      const copy = b.plans![0].actions![1].deliveries![0].copy;
      copy.round = 2;
      copy.recopies_used = 1;
      copy.committed_bytes = 0;
      historyPair(a, b, direction, true);
    },
  );
  it("同水位对象键序、根身份和from_wm不是业务变化", () => {
    const a = report();
    const n = structuredClone(a);
    n.plans![0] = Object.fromEntries(
      Object.entries(n.plans![0]).reverse(),
    ) as NonNullable<StatusReport["plans"]>[number];
    n.from_wm = 10;
    n.plans![0].actions![0].execution = { started: true };
    historyPair(a, n, "same", true);
  });
});

describe("历史观察边界终结收场", () => {
  it.each(["earlier", "same", "later"] as const)(
    "终结 followup 错误不改写：%s",
    (direction) => {
      const a = flowReport();
      const flow = (a.plans![0].actions![0].result as CameraResult).recording!
        .followup_stops![0];
      flow.status = "failed";
      flow.error = error;
      const b = structuredClone(a);
      (
        b.plans![0].actions![0].result as CameraResult
      ).recording!.followup_stops![0].error = { ...error, code: "changed" };
      historyPair(a, b, direction, false);
    },
  );
  it.each(["earlier", "same", "later"] as const)(
    "终结 flow 保留原错误而 unknown 尝试可由证据补充：%s",
    (direction) => {
      const a = flowReport();
      const flow = (a.plans![0].actions![0].result as CameraResult).recording!
        .followup_stops![0];
      flow.status = "failed";
      flow.error = error;
      flow.attempts = [{ attempt_no: 1, status: "unknown", error }];
      const b = structuredClone(a);
      (
        b.plans![0].actions![0].result as CameraResult
      ).recording!.followup_stops![0].attempts = [
        { attempt_no: 1, status: "succeeded" },
      ];
      historyPair(a, b, direction, direction !== "same");
    },
  );
});
