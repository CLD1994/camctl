import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import {
  parseReport,
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
