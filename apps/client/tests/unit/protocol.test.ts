import { describe, it, expect } from "vitest";
import { parseJson } from "../../src/shared/json";
import {
  loadCapabilities,
  validateParams,
} from "../../src/shared/capabilities";
import { validatePlan } from "../../src/shared/plan";

const schema = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  type: "object",
  required: ["type"],
  properties: {
    type: { const: "fixed" },
    duration: { type: "integer", minimum: 1, default: 60 },
  },
  additionalProperties: false,
};
const directory = () => ({
  devices: [
    {
      device_id: "cam0",
      driver_id: "demo",
      actions: [
        {
          type: "camera_record",
          parameter_types: [
            {
              type: "fixed",
              name: "固定",
              description: "演示",
              schema: structuredClone(schema),
            },
          ],
        },
      ],
    },
  ],
});
const record = () => ({
  name: "录像",
  type: "camera_record",
  device_id: "cam0",
  scheduled_at: "2026-09-16 00:00:00",
  params: { type: "fixed" },
  policy: { max_delay_ms: 0 },
});
const plan = (actions: unknown[] = [record()]) => ({
  request_id: "req1",
  name: "计划",
  created_at: "0001-01-01 00:00:00.000001",
  actions,
});
describe("parseJson", () => {
  it("保留合法 JSON 的实际值", () =>
    expect(parseJson(' {"n":1e2,"x":null} ')).toEqual({ n: 100, x: null }));
  it.each([
    '{"x":1,"\\u0078":2}',
    '{"a":{"x":1,"x":2}}',
    '{"x":1,}',
    "{}{}",
    "/*a*/{}",
    "1e999",
    "[NaN]",
  ])("拒绝无法无歧义解释的 JSON %s", (text) =>
    expect(() => parseJson(text)).toThrow(),
  );
  it("不同对象允许相同成员名", () =>
    expect(parseJson('[{"x":1},{"x":2}]')).toEqual([{ x: 1 }, { x: 2 }]));
});
describe("loadCapabilities", () => {
  it.each([{ range: undefined }, { range: [] }, { range: [1] }])(
    "接受标准合法的可选元组 %#",
    ({ range }) => {
      const d = directory();
      Object.assign(
        d.devices[0].actions[0].parameter_types[0].schema.properties,
        {
          range: {
            type: "array",
            prefixItems: [{ type: "number" }],
            items: false,
          },
        },
      );
      const caps = loadCapabilities(d);
      const params =
        range === undefined ? { type: "fixed" } : { type: "fixed", range };
      const before = structuredClone(params);
      expect(validateParams("cam0", "camera_record", params, caps)).toEqual([]);
      expect(params).toEqual(before);
    },
  );
  it.each([{ range: [1, 2] }, { range: ["1"] }])(
    "拒绝可选元组超长或错误元素 %#",
    (fields) => {
      const d = directory();
      Object.assign(
        d.devices[0].actions[0].parameter_types[0].schema.properties,
        {
          range: {
            type: "array",
            prefixItems: [{ type: "number" }],
            items: false,
          },
        },
      );
      expect(
        validateParams(
          "cam0",
          "camera_record",
          { type: "fixed", ...fields },
          loadCapabilities(d),
        ).length,
      ).toBeGreaterThan(0);
    },
  );
  it("标准允许 properties 与 patternProperties 共同约束", () => {
    const d = directory();
    Object.assign(d.devices[0].actions[0].parameter_types[0].schema, {
      patternProperties: { "^duration$": { maximum: 100 } },
    });
    const caps = loadCapabilities(d);
    expect(
      validateParams(
        "cam0",
        "camera_record",
        { type: "fixed", duration: 60 },
        caps,
      ),
    ).toEqual([]);
    expect(
      validateParams(
        "cam0",
        "camera_record",
        { type: "fixed", duration: 101 },
        caps,
      ).length,
    ).toBeGreaterThan(0);
  });
  it("接受明确的空目录", () =>
    expect(loadCapabilities({ devices: [] })).toEqual({ devices: [] }));
  it.each([null, {}, { devices: null }, { devices: [], extra: 1 }])(
    "拒绝非法外层结构 %#",
    (value) => expect(() => loadCapabilities(value)).toThrow(),
  );
  it("任一设备重复时整份失败", () => {
    const d = directory();
    d.devices.push(structuredClone(d.devices[0]));
    expect(() => loadCapabilities(d)).toThrow();
  });
  it("参数类型与规则不匹配时失败", () => {
    const d = directory();
    d.devices[0].actions[0].parameter_types[0].type = "other";
    expect(() => loadCapabilities(d)).toThrow();
  });
  it("缺少版本时失败", () => {
    const d = directory();
    delete (
      d.devices[0].actions[0].parameter_types[0].schema as Partial<
        typeof schema
      >
    ).$schema;
    expect(() => loadCapabilities(d)).toThrow();
  });
  it("引用无法解析时失败", () => {
    const d = directory();
    Object.assign(d.devices[0].actions[0].parameter_types[0].schema, {
      $ref: "https://invalid.example/schema",
    });
    expect(() => loadCapabilities(d)).toThrow();
  });
  it("内嵌独立资源缺少版本时整份失败", () => {
    const d = directory();
    Object.assign(d.devices[0].actions[0].parameter_types[0].schema, {
      $defs: {
        other: { $id: "https://example.invalid/other", type: "string" },
      },
    });
    expect(() => loadCapabilities(d)).toThrow();
  });
  it("加载后的启用规则与调用方可变输入独立", () => {
    const d = directory();
    const caps = loadCapabilities(d);
    d.devices[0].actions[0].parameter_types[0].type = "other";
    expect(
      validateParams("cam0", "camera_record", { type: "fixed" }, caps),
    ).toEqual([]);
  });
});
describe("validateParams", () => {
  it("不自动补齐默认值", () => {
    const params = { type: "fixed" };
    expect(
      validateParams(
        "cam0",
        "camera_record",
        params,
        loadCapabilities(directory()),
      ),
    ).toEqual([]);
    expect(params).toEqual({ type: "fixed" });
  });
  it.each([
    null,
    {},
    { type: "missing" },
    { type: "fixed", duration: "60" },
    { type: "fixed", duration: null },
    { type: "fixed", extra: 1 },
  ])("拒绝非法参数 %#", (params) =>
    expect(
      validateParams(
        "cam0",
        "camera_record",
        params,
        loadCapabilities(directory()),
      ).length,
    ).toBeGreaterThan(0),
  );
  it("没有能力说明时明确失败", () =>
    expect(validateParams("cam0", "camera_record", {}, null)[0].code).toBe(
      "capabilities_unavailable",
    ));
  it("设备不匹配时不使用首项规则", () =>
    expect(
      validateParams(
        "other",
        "camera_record",
        { type: "fixed" },
        loadCapabilities(directory()),
      ).length,
    ).toBeGreaterThan(0));
});
describe("validatePlan", () => {
  it("合法拍摄计划通过", () =>
    expect(validatePlan(plan(), loadCapabilities(directory()))).toEqual([]));
  it("允许向后引用录像", () =>
    expect(
      validatePlan(
        plan([
          {
            name: "取回",
            type: "obtain_action_outputs",
            scheduled_at: "2026-01-01 00:00:00",
            params: { source: { action_name: "录像" } },
          },
          record(),
        ]),
        loadCapabilities(directory()),
      ),
    ).toEqual([]));
  it.each([
    null,
    {},
    { ...plan(), actions: [] },
    { ...plan(), name: "\u0085计划" },
    { ...plan(), created_at: "2026-02-29 00:00:00" },
    { ...plan(), request_id: " a" },
    { ...plan(), extra: true },
  ])("拒绝公共契约错误 %#", (value) =>
    expect(
      validatePlan(value, loadCapabilities(directory())).length,
    ).toBeGreaterThan(0),
  );
  it("所有动作名称参与重复检查", () =>
    expect(
      validatePlan(
        plan([record(), record()]),
        loadCapabilities(directory()),
      ).some((i) => i.code === "duplicate_name"),
    ).toBe(true));
  it.each(["camera_take_photo", "future"])("拒绝未实现动作 %s", (type) =>
    expect(
      validatePlan(plan([{ name: "动作", type }]), null).length,
    ).toBeGreaterThan(0),
  );
  it("报告参数缺省时必须补齐", () => {
    expect(
      validatePlan(plan([{ name: "报告", type: "report_status" }]), null),
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          path: "actions[0].params",
          code: "invalid_params",
        }),
      ]),
    );
  });
  it.each([{ scope: "full" }])("无需能力说明的报告通过 %#", (params) =>
    expect(
      validatePlan(
        plan([{ name: "报告", type: "report_status", params }]),
        null,
      ),
    ).toEqual([]),
  );
  it.each([
    undefined,
    {},
    null,
    { scope: "since" },
    { after_report_id: 1 },
    { scope: "full", after_report_id: 1 },
    { scope: "since", after_report_id: true },
  ])("拒绝同步组合错误 %#", (params) =>
    expect(
      validatePlan(
        plan([{ name: "报告", type: "report_status", params }]),
        null,
      ).length,
    ).toBeGreaterThan(0),
  );
  it("局部同步要求已保存且覆盖起点", () => {
    const p = plan([
      {
        name: "报告",
        type: "report_status",
        params: { scope: "since", after_report_id: 4 },
      },
    ]);
    expect(
      validatePlan(p, null, {
        reports: [{ report_id: 4, to_wm: 20 }],
        coverage: 20,
      }),
    ).toEqual([]);
    expect(
      validatePlan(p, null, {
        reports: [{ report_id: 4, to_wm: 20 }],
        coverage: 19,
      }).length,
    ).toBeGreaterThan(0);
  });
  it.each([
    { request_id: "r" },
    { plan_instance_id: "p" },
    { action_instance_id: "a" },
    { plan_instance_id: "p", group: "组" },
  ])("接受取消目标组合 %#", (target) =>
    expect(
      validatePlan(
        plan([{ name: "取消", type: "cancel_task", params: { target } }]),
        null,
      ),
    ).toEqual([]),
  );
  it.each([
    {},
    { group: "组" },
    { action_name: "动作" },
    { request_id: "r", plan_instance_id: "p" },
  ])("拒绝取消目标组合 %#", (target) =>
    expect(
      validatePlan(
        plan([{ name: "取消", type: "cancel_task", params: { target } }]),
        null,
      ).length,
    ).toBeGreaterThan(0),
  );
  it.each([
    { output_ids: [] },
    { output_ids: ["o", "o"] },
    { output_ids: [null] },
  ])("拒绝非法清理目标 %#", (params) =>
    expect(
      validatePlan(
        plan([
          {
            name: "清理",
            type: "delete_action_outputs",
            scheduled_at: "2026-01-01 00:00:00",
            params,
          },
        ]),
        null,
      ).length,
    ).toBeGreaterThan(0),
  );
  it("无时效动作不接受拍摄策略", () =>
    expect(
      validatePlan(
        plan([
          { name: "报告", type: "report_status", policy: { max_delay_ms: 1 } },
        ]),
        null,
      ).length,
    ).toBeGreaterThan(0));
});

it.each([
  { if: { type: "object" } },
  { then: { type: "object" } },
  { minContains: 1 },
])("标准规定不产生约束的关键词组合可以加载 %#", (rules) => {
  const d = directory();
  Object.assign(d.devices[0].actions[0].parameter_types[0].schema, rules);
  const caps = loadCapabilities(d);
  expect(
    validateParams("cam0", "camera_record", { type: "fixed" }, caps),
  ).toEqual([]);
});
it.each([
  { futureConstraint: true },
  {
    properties: {
      type: { const: "fixed" },
      x: { type: "string", format: "future-format" },
    },
  },
])("不支持的必要关键词或格式仍拒绝 %#", (rules) => {
  const d = directory();
  Object.assign(d.devices[0].actions[0].parameter_types[0].schema, rules);
  expect(() => loadCapabilities(d)).toThrow();
});
