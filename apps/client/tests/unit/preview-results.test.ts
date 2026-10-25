import { expect, it } from "vitest";
import * as model from "../../src/web/result-model";
import type {
  ReportAction,
  ReportPlan,
  Output,
  Delivery,
} from "../../src/shared/types";
import type { Video } from "../../src/server/models";

const capture = {
  action_instance_id: "1",
  name: "拍摄",
  type: "camera_record",
  status: "succeeded",
} as ReportAction;
const automatic = (id = "2", source: string | undefined = "1") =>
  ({
    action_instance_id: id,
    name: "取回",
    type: "obtain_action_outputs",
    status: "pending",
    automation: {
      purpose: "auto_preview",
      ...(source ? { source_action_instance_id: source } : {}),
    },
  }) as ReportAction;
const plan = (actions: ReportAction[]) =>
  ({
    plan_instance_id: "1",
    request_id: "1",
    name: "计划",
    status: "running",
    actions,
  }) as ReportPlan;
const output = (id: string, extra: Partial<Output> = {}) =>
  ({
    output_id: id,
    source_action_instance_id: "1",
    kind: "original",
    original_name: id,
    availability: "available",
    cleanup: { status: "not_requested" },
    checksum: { status: "not_obtained" },
    media: { check_status: "not_performed", duration: { status: "unknown" } },
    ...extra,
  }) as Output;
const delivery = (id: string, status: Delivery["status"] = "published") =>
  ({
    delivery_id: id,
    output_id: "10",
    source_action_instance_id: "1",
    file_name: `${id}.png`,
    display_name: id,
    status,
  }) as Delivery;
it("可靠关联的全部自动动作收进拍摄详情，手动动作独立", () => {
  const autos = [automatic("2"), automatic("3"), automatic("4")].map(
    (a) =>
      ({
        ...a,
        status: "failed",
        error: {
          code: "duplicate_auto_preview",
          stage: "admission",
          details: {},
        },
      }) as ReportAction,
  );
  const manual = { ...automatic("5"), automation: undefined };
  const rows = model.resultActionRows(
    plan([autos[0], manual, capture, ...autos.slice(1)]),
  );
  expect(
    rows.map((r) => [
      r.action.action_instance_id,
      r.automaticActions.map((a) => a.action_instance_id),
    ]),
  ).toEqual([
    ["5", []],
    ["1", ["2", "3", "4"]],
  ]);
  expect(rows[1].automaticActions[0]).toBe(autos[0]);
});
it.each([undefined, "99"])("缺少可靠来源 %s 时保留独立动作及诊断", (source) => {
  const a = automatic();
  a.automation = {
    purpose: "auto_preview",
    ...(source ? { source_action_instance_id: source } : {}),
  };
  const [row] = model.resultActionRows(plan([a]));
  expect(row.action).toBe(a);
  expect(row.automaticActions).toEqual([]);
  expect(row.association).toBeTruthy();
  if (source) expect(row.association).toContain(source);
});
it("来源晚到后自动动作保留身份并归入来源详情", () => {
  const a = automatic();
  expect(model.resultActionRows(plan([a]))[0].action).toBe(a);
  const rows = model.resultActionRows(plan([a, capture]));
  expect(rows).toHaveLength(1);
  expect(rows[0].action).toBe(capture);
  expect(rows[0].automaticActions).toEqual([a]);
});
it("原输入的自动用途及 delivery 来源不能补造 automation 关系", () => {
  const a = {
    ...automatic(),
    automation: undefined,
    input_params: { purpose: "auto_preview", source: { action_name: "拍摄" } },
    deliveries: [delivery("20")],
  };
  const row = model.resultActionRows(plan([capture, a]))[1];
  expect(row.automaticActions).toEqual([]);
  expect(row.association).toBeTruthy();
});
it.each([
  "pending",
  "preparing",
  "prepared",
  "publishing",
  "published",
  "failed",
  "canceled",
  "withdrawn",
] as const)("自动摘要保留真实交付阶段 %s", (status) => {
  const a = { ...automatic(), deliveries: [delivery("20", status)] };
  expect(model.automaticResult(a, [plan([capture, a])], []).stages).toEqual([
    { status, count: 1 },
  ]);
});
it("自动取回不借手动副本；好副本仍保留取消撤回及来源失败", () => {
  const a = {
    ...automatic(),
    status: "failed",
    deliveries: [
      delivery("20"),
      delivery("21", "withdrawn"),
      delivery("22", "canceled"),
    ],
    result: {
      failures: [
        {
          source_action_instance_id: "1",
          error: { code: "no_outputs", stage: "selection", details: {} },
        },
      ],
    },
  } as ReportAction;
  const manual = {
    ...automatic("3"),
    automation: undefined,
    deliveries: [delivery("30")],
  };
  const videos = [
    { id: "v", fileName: "30.png", status: "verified" },
  ] as Video[];
  const plans = [plan([capture, a, manual])];
  expect(model.automaticResult(a, plans, videos).ready).toBe(0);
  const result = model.automaticResult(a, plans, [
    ...videos,
    { id: "v2", fileName: "20.png", status: "verified" } as Video,
  ]);
  expect(result.ready).toBe(1);
  expect(result.problems).toBe(2);
  expect(result.notes.some((n) => n.error)).toBe(true);
});
it("预览与修复成品按实际原文件建立关系，只有预览收到不表示原片收到", () => {
  const c = {
    ...capture,
    outputs: [
      output("10"),
      output("11", { kind: "preview", preview_of_output_id: "10" }),
      output("12", { kind: "repaired", derived_from_output_id: "10" }),
      output("13"),
      output("14", { kind: "preview", preview_of_output_id: "13" }),
    ],
  };
  const a = {
    ...automatic(),
    deliveries: [{ ...delivery("20"), output_id: "11" }],
  };
  const rows = model.resultProducts(
    c,
    [plan([c, a])],
    [{ id: "v", fileName: "20.png", status: "verified" } as Video],
  );
  expect(rows[0].state).toBe("waiting");
  expect(rows[1].state).toBe("ready");
  expect(rows[1].original?.id).toBe("10");
  expect(rows[1].original?.output).toBe(c.outputs[0]);
  expect(rows[1].hasRepaired).toBe(true);
  expect(rows[4].hasRepaired).toBe(false);
  expect(rows[2].original?.id).toBe("10");
});
it("关系对象缺失保留引用，只有交付时不猜测预览角色", () => {
  const c = {
    ...capture,
    outputs: [output("11", { kind: "preview", preview_of_output_id: "10" })],
  };
  const [preview] = model.resultProducts(c, [plan([c])], []);
  expect(preview.original).toEqual({ id: "10", output: undefined });
  const [unknown] = model.resultProducts(
    { ...automatic(), deliveries: [delivery("20")] },
    [],
    [],
  );
  expect(unknown.output).toBeUndefined();
  expect(unknown.original).toBeUndefined();
});
it.each([
  "waiting_report",
  "verifying",
  "verified",
  "mismatch",
  "unavailable",
] as const)("自动摘要逐本地副本保留 %s", (status) => {
  const a = { ...automatic(), deliveries: [delivery("20")] };
  const result = model.automaticResult(
    a,
    [],
    [{ id: "v", fileName: "20.png", status } as Video],
  );
  expect(result.local).toEqual([{ status, count: 1 }]);
  expect(result.waitingPublished).toBe(0);
});
it("仅发布没有本地副本时单独统计等待接收", () => {
  expect(
    model.automaticResult(
      { ...automatic(), deliveries: [delivery("20")] },
      [],
      [],
    ).waitingPublished,
  ).toBe(1);
});
