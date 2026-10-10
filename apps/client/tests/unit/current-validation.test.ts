import { it, expect } from "vitest";
import { presentIssues } from "../../src/web/validation-presentation";
import { validatePlan } from "../../src/shared/plan";
import type { Capabilities, Issue } from "../../src/shared/types";

const at = "2026-10-11 00:00:00";
const plan = (action: Record<string, unknown>) => ({
  request_id: "1",
  created_at: at,
  name: "字段校验",
  actions: [{ name: "当前动作", scheduled_at: at, ...action }],
});
const motor = { type: "motor_control" };
function current(
  action: Record<string, unknown>,
  capabilities: Capabilities | null = null,
) {
  const value = plan(action);
  const raw = validatePlan(value, capabilities);
  return { raw, shown: presentIssues(raw, value, capabilities) };
}

it("缺失电机容器只呈现两个当前必填字段，原始诊断保留", () => {
  const { raw, shown } = current(motor);
  expect(shown.map((i) => i.target)).toEqual([
    "/actions/0/params/position",
    "/actions/0/policy/max_delay_ms",
  ]);
  expect(raw.map((i) => i.code)).toContain("schema_if");
  expect(raw.map((i) => i.code)).toContain("invalid_params");
});

it.each([null, [], "原值", 4])("非对象电机参数保留结构错误：%j", (params) => {
  const { shown } = current({ ...motor, params, policy: { max_delay_ms: 0 } });
  expect(shown).toHaveLength(1);
  expect(shown[0].target).toBe("/actions/0/params");
  expect(shown[0].issue.code).toBe("schema_type");
  expect(shown[0].message).toContain("对象");
});

it.each([
  "obtain_action_outputs",
  "delete_action_outputs",
  "cancel_task",
  "report_status",
])("非对象内置参数只归属对象结构：%s", (type) => {
  const { shown } = current({ type, params: null });
  expect(shown.map((i) => [i.target, i.issue.code])).toEqual([
    ["/actions/0/params", "schema_type"],
  ]);
});

it.each([null, [], "原值"])("非对象策略不投射到最大延迟：%j", (policy) => {
  const { shown } = current({ ...motor, params: { position: 0 }, policy });
  expect(shown.map((i) => i.target)).toEqual(["/actions/0/policy"]);
});

it.each([
  [{ ...motor, params: { position: 0 } }, ["/actions/0/policy/max_delay_ms"]],
  [{ ...motor, policy: { max_delay_ms: 0 } }, ["/actions/0/params/position"]],
  [{ ...motor, params: { position: 0 }, policy: { max_delay_ms: 0 } }, []],
] as const)("单项修正只清除该项反馈", (action, targets) => {
  expect(current(action).shown.map((i) => i.target)).toEqual(targets);
});

it.each([
  [{}, ["/actions/0/params/scope"]],
  [{ scope: "since" }, ["/actions/0/params/after_report_id"]],
  [{ scope: "full" }, []],
  [
    { scope: "full", after_report_id: "1" },
    ["/actions/0/params/after_report_id"],
  ],
] as const)("报告只呈现当前明确范围的失败：%j", (params, targets) => {
  expect(
    current({ type: "report_status", params }).shown.map((i) => i.target),
  ).toEqual(targets);
});

it.each(["bad", null, [], {}])("非法报告范围保留选择错误：%j", (scope) => {
  const { shown } = current({ type: "report_status", params: { scope } });
  expect(shown.some((i) => i.target === "/actions/0/params/scope")).toBe(true);
  expect(
    shown.some((i) => i.target === "/actions/0/params/after_report_id"),
  ).toBe(false);
});

it.each([
  ["obtain_action_outputs", "source", { action_name: "" }, "action_name"],
  [
    "obtain_action_outputs",
    "source",
    { action_instance_id: "bad" },
    "action_instance_id",
  ],
  ["obtain_action_outputs", "source", { group: "" }, "group"],
  [
    "obtain_action_outputs",
    "source",
    { plan_instance_id: "bad", group: "合法组" },
    "plan_instance_id",
  ],
  ["obtain_action_outputs", "source", { current_plan: false }, "current_plan"],
  [
    "obtain_action_outputs",
    "source",
    { plan_instance_id: "bad" },
    "plan_instance_id",
  ],
  ["cancel_task", "target", { request_id: "bad" }, "request_id"],
  [
    "cancel_task",
    "target",
    { action_instance_id: "bad" },
    "action_instance_id",
  ],
  ["cancel_task", "target", { plan_instance_id: "bad" }, "plan_instance_id"],
  [
    "cancel_task",
    "target",
    { plan_instance_id: "bad", group: "合法组" },
    "plan_instance_id",
  ],
])("坏引用值只属于当前字段形状：%s %j", (type, key, reference, field) => {
  const { shown } = current({ type, params: { [key]: reference } });
  expect([...new Set(shown.map((i) => i.target))]).toEqual([
    `/actions/0/params/${key}/${field}`,
  ]);
  expect(
    shown.some((i) => i.issue.code === "schema_additionalProperties"),
  ).toBe(false);
});

it.each([
  { type: "delete_action_outputs", params: { output_ids: [] } },
  {
    type: "obtain_action_outputs",
    params: { source: { action_instance_id: "1" }, output_ids: [] },
  },
])("空产物列表归属列表而不制造第一项：$type", (action) => {
  expect(current(action).shown.map((i) => i.target)).toEqual([
    "/actions/0/params/output_ids",
  ]);
});

it.each([
  {
    type: "delete_action_outputs",
    params: { source: { action_instance_id: "1" }, output_ids: ["1"] },
  },
  {
    type: "obtain_action_outputs",
    params: {
      source: { action_instance_id: "1" },
      output_ids: ["1"],
      filter: "preview",
    },
  },
  {
    type: "obtain_action_outputs",
    params: {
      source: { action_name: "录像" },
      purpose: "auto_preview",
      output_ids: ["1"],
    },
  },
])("真实互斥失败保留参数组合与JSON定位：$type", (action) => {
  const { shown } = current(action);
  expect(
    shown.some(
      (i) =>
        i.issue.code === "schema_oneOf" &&
        i.target === "/actions/0/params" &&
        i.json,
    ),
  ).toBe(true);
});

const cameraCapabilities = (schema: Record<string, unknown>): Capabilities => ({
  devices: [
    {
      device_id: "camera",
      driver_id: "test",
      actions: [
        {
          type: "camera_record",
          parameter_types: [
            {
              type: "selected",
              name: "当前参数",
              description: "测试",
              preview_supported: false,
              schema: {
                $schema: "https://json-schema.org/draft/2020-12/schema",
                type: "object",
                properties: {
                  type: { const: "selected" },
                  ...(schema.properties as object),
                },
                required: ["type"],
                ...schema,
              },
            },
          ],
        },
      ],
    },
  ],
});

it("缺失拍摄参数只要求当前参数类型，不追加参数结构摘要", () => {
  const caps = cameraCapabilities({});
  const { shown } = current(
    { type: "camera_record", device_id: "camera", policy: { max_delay_ms: 0 } },
    caps,
  );
  expect(shown.map((i) => i.target)).toEqual(["/actions/0/params/type"]);
});

it("真实设备oneOf多匹配不因独立叶子失败被折叠", () => {
  const caps = cameraCapabilities({
    properties: {
      type: { const: "selected" },
      count: { type: "integer", minimum: 1 },
    },
    oneOf: [
      { properties: { type: { const: "selected" } } },
      { required: ["type"] },
    ],
  });
  const { shown } = current(
    {
      type: "camera_record",
      device_id: "camera",
      params: { type: "selected", count: 0 },
      policy: { max_delay_ms: 0 },
    },
    caps,
  );
  expect(shown.some((i) => i.issue.code === "schema_oneOf")).toBe(true);
  expect(shown.some((i) => i.target === "/actions/0/params/count")).toBe(true);
});

it.each(["a.b", "[x]", "01", "a/b~c"])(
  "真实Schema生产者保留缺失特殊键路径：%s",
  (key) => {
    const caps = cameraCapabilities({
      properties: {
        type: { const: "selected" },
        [key]: { title: "特殊参数", type: "string" },
      },
      required: ["type", key],
    });
    const { raw, shown } = current(
      {
        type: "camera_record",
        device_id: "camera",
        params: { type: "selected" },
        policy: { max_delay_ms: 0 },
      },
      caps,
    );
    const target = `/actions/0/params/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`;
    expect(shown.map((i) => i.target)).toEqual([target]);
    expect(shown[0].location).toContain("特殊参数");
    expect(raw[0].pointer).toBe(target);
  },
);

it("没有Ajv分支证据的组合不根据同层叶子诊断消失", () => {
  const value = plan({ ...motor, params: {}, policy: {} });
  const raw: Issue[] = [
    { path: "actions[0].params", code: "schema_oneOf", message: "combination" },
    {
      path: "actions[0].params.position",
      code: "schema_required",
      message: "required",
    },
  ];
  expect(presentIssues(raw, value).map((i) => i.issue.code)).toContain(
    "schema_oneOf",
  );
});

it.each([
  [{ type: "report_status", params: {} }, /选择.*报告范围/],
  [{ type: "report_status", params: { scope: "bad" } }, /允许.*报告范围/],
  [{ type: "delete_action_outputs", params: {} }, /选择.*清理范围/],
  [{ type: "obtain_action_outputs", params: { source: {} } }, /选择.*来源/],
  [{ type: "obtain_action_outputs", params: {} }, /选择.*来源/],
  [{ type: "cancel_task", params: { target: {} } }, /选择.*取消目标/],
  [{ type: "cancel_task", params: {} }, /选择.*取消目标/],
  [
    {
      type: "delete_action_outputs",
      params: { source: { action_instance_id: "1" }, output_ids: ["1"] },
    },
    /来源.*产物列表.*不能同时/,
  ],
  [
    {
      type: "obtain_action_outputs",
      params: {
        source: { action_instance_id: "1" },
        filter: "preview",
        output_ids: ["1"],
      },
    },
    /筛选.*产物列表.*不能同时/,
  ],
] as const)("固定选择责任保留具体修正动作：%j", (action, fact) => {
  const { raw, shown } = current(action);
  expect(raw.length).toBeGreaterThan(0);
  expect(
    shown.find(
      (i) =>
        i.issue.code === "schema_oneOf" || i.issue.code === "schema_required",
    )?.message,
  ).toMatch(fact);
});

it.each(["delete_action_outputs", "obtain_action_outputs"])(
  "互斥组合同时保留可证明的来源和列表失败：%s",
  (type) => {
    const { raw, shown } = current({
      type,
      params: {
        source: { action_instance_id: "bad" },
        output_ids: [],
        ...(type === "obtain_action_outputs" ? { filter: "not_allowed" } : {}),
      },
    });
    expect(raw.length).toBeGreaterThan(0);
    expect(shown.map((i) => [i.target, i.issue.code])).toEqual(
      expect.arrayContaining([
        ["/actions/0/params", "schema_oneOf"],
        ["/actions/0/params/source/action_instance_id", "schema_pattern"],
        ["/actions/0/params/output_ids", "schema_minItems"],
        ...(type === "obtain_action_outputs"
          ? [["/actions/0/params/filter", "schema_enum"]]
          : []),
      ]),
    );
    expect(shown.some((i) => i.issue.code === "schema_required")).toBe(false);
    expect(
      shown.some((i) => i.issue.code === "schema_additionalProperties"),
    ).toBe(false);
  },
);

it.each([
  [
    { source: { action_instance_id: "1" }, output_ids: [] },
    ["/actions/0/params/output_ids"],
  ],
  [
    { source: { action_instance_id: "bad" }, output_ids: ["1"] },
    ["/actions/0/params/source/action_instance_id"],
  ],
  [{ source: { action_instance_id: "1" }, output_ids: ["1"] }, []],
] as const)("互斥状态修正一项只清该项，组合持续：%j", (params, targets) => {
  const { raw, shown } = current({ type: "delete_action_outputs", params });
  expect(raw.length).toBeGreaterThan(0);
  expect(
    shown.filter((i) => i.issue.code !== "schema_oneOf").map((i) => i.target),
  ).toEqual(targets);
  expect(shown.filter((i) => i.issue.code === "schema_oneOf")).toHaveLength(1);
});

it.each([null, "原值", ["bad"]])(
  "互斥中的产物列表仍验证类型或条目：%j",
  (output_ids) => {
    const { shown } = current({
      type: "delete_action_outputs",
      params: { source: { action_instance_id: "1" }, output_ids },
    });
    expect(shown.some((i) => i.issue.code === "schema_oneOf")).toBe(true);
    expect(
      shown.some(
        (i) =>
          i.target?.startsWith("/actions/0/params/output_ids") &&
          i.issue.code !== "schema_oneOf",
      ),
    ).toBe(true);
  },
);

it.each(
  [
    ["obtain_action_outputs", "source"],
    ["delete_action_outputs", "source"],
    ["cancel_task", "target"],
  ].flatMap(([type, key]) =>
    [null, [], "原值", 5].map((value) => ({ type, key, value })),
  ),
)("非对象引用只属于JSON结构责任：$type $value", ({ type, key, value }) => {
  const { raw, shown } = current({ type, params: { [key]: value } });
  expect(raw.length).toBeGreaterThan(0);
  expect(shown).toHaveLength(1);
  expect(shown[0]).toMatchObject({
    target: `/actions/0/params/${key}`,
    json: true,
    issue: { code: "schema_type" },
  });
});

it.each([
  ["obtain_action_outputs", "source"],
  ["delete_action_outputs", "source"],
  ["cancel_task", "target"],
])("缺失或空引用仍属于选择入口：%s", (type, key) => {
  for (const params of [{}, { [key]: {} }]) {
    const { shown } = current({ type, params });
    expect(
      shown.some((i) => i.target === `/actions/0/params/${key}` && !i.json),
    ).toBe(true);
  }
});

it.each([
  [{ a: true, b: true, c: true, d: true }, [0, 1]],
  [{}, null],
] as const)("设备独立组合规则保留来源身份：%j", (values, passingSchemas) => {
  const caps = cameraCapabilities({
    allOf: [
      { oneOf: [{ required: ["a"] }, { required: ["b"] }] },
      { oneOf: [{ required: ["c"] }, { required: ["d"] }] },
    ],
  });
  const { raw, shown } = current(
    {
      type: "camera_record",
      device_id: "camera",
      params: { type: "selected", ...values },
      policy: { max_delay_ms: 0 },
    },
    caps,
  );
  const combinations = shown.filter((i) => i.issue.code === "schema_oneOf");
  expect(raw.filter((i) => i.code === "schema_oneOf")).toHaveLength(2);
  expect(combinations.map((i) => i.issue.schema?.schemaPath)).toEqual([
    "#/allOf/0/oneOf",
    "#/allOf/1/oneOf",
  ]);
  expect(
    combinations.map((i) => i.issue.schema?.params.passingSchemas),
  ).toEqual([passingSchemas, passingSchemas]);
});

it("同一真实Schema规则的重复诊断可以合并，原始集合保持", () => {
  const caps = cameraCapabilities({
    oneOf: [{ required: ["type"] }, { required: ["type"] }],
  });
  const value = plan({
    type: "camera_record",
    device_id: "camera",
    params: { type: "selected" },
    policy: { max_delay_ms: 0 },
  });
  const raw = validatePlan(value, caps);
  const repeated = [...raw, ...raw.map((i) => ({ ...i }))];
  expect(
    presentIssues(repeated, value, caps).filter(
      (i) => i.issue.code === "schema_oneOf",
    ),
  ).toHaveLength(1);
  expect(repeated).toHaveLength(2);
});

it("没有规则来源时不按相同显示值去重", () => {
  const value = plan({ ...motor, params: {}, policy: {} });
  const issue: Issue = {
    path: "actions[0].params",
    code: "schema_oneOf",
    message: "technical",
  };
  expect(presentIssues([issue, { ...issue }], value)).toHaveLength(2);
});

function previewCurrent(params: Record<string, unknown>) {
  const capabilities = cameraCapabilities({});
  capabilities.devices[0].actions[0].parameter_types[0].preview_supported = true;
  const value = {
    ...plan({}),
    actions: [
      {
        name: "录像",
        type: "camera_record",
        device_id: "camera",
        params: { type: "selected" },
        policy: { max_delay_ms: 0 },
        scheduled_at: at,
      },
      { name: "取回", type: "obtain_action_outputs", scheduled_at: at, params },
    ],
  };
  const raw = validatePlan(value, capabilities);
  const original = JSON.stringify(raw);
  const shown = presentIssues(raw, value, capabilities);
  expect(JSON.stringify(raw)).toBe(original);
  expect(
    raw.some((issue) =>
      /source_not_found|preview_|time_mismatch/.test(issue.code),
    ),
  ).toBe(false);
  return { raw, shown };
}

const filterStates = [
  { name: "缺失", fields: {}, manual: null, automatic: "schema_required" },
  {
    name: "preview",
    fields: { filter: "preview" },
    manual: null,
    automatic: null,
  },
  {
    name: "default",
    fields: { filter: "default" },
    manual: null,
    automatic: "schema_const",
  },
  ...["bad", null, 5, {}, []].map((filter) => ({
    name: JSON.stringify(filter),
    fields: { filter },
    manual: "schema_enum",
    automatic: "schema_const",
  })),
];
const purposeStates = [
  { name: "缺省用途", fields: {}, mode: "manual" },
  { name: "手动用途", fields: { purpose: "manual" }, mode: "manual" },
  { name: "自动用途", fields: { purpose: "auto_preview" }, mode: "automatic" },
  ...["", "bad", null, 5, {}].map((purpose) => ({
    name: `非法用途 ${JSON.stringify(purpose)}`,
    fields: { purpose },
    mode: "unknown",
  })),
];
it.each(
  purposeStates.flatMap((purpose) =>
    [false, true].flatMap((list) =>
      filterStates.map((filter) => ({ purpose, list, filter })),
    ),
  ),
)(
  "用途与方式正交：$purpose.name 列表=$list 筛选=$filter.name",
  ({ purpose, list, filter }) => {
    const { raw, shown } = previewCurrent({
      source: { action_name: "录像" },
      ...purpose.fields,
      ...(list ? { output_ids: ["1"] } : {}),
      ...filter.fields,
    });
    const conflict =
      purpose.mode === "unknown" ||
      (list &&
        (purpose.mode === "automatic" ||
          Object.hasOwn(filter.fields, "filter")));
    const filterCode =
      purpose.mode === "automatic" ? filter.automatic : filter.manual;
    const expected = [
      ...(conflict ? [["/actions/1/params", "schema_oneOf"]] : []),
      ...(filterCode ? [["/actions/1/params/filter", filterCode]] : []),
      ...(purpose.mode === "unknown"
        ? [["/actions/1/params/purpose", "schema_enum"]]
        : []),
    ];
    expect(
      shown.map((issue) => [issue.target, issue.issue.code]).sort(),
    ).toEqual(expected.sort());
    expect(raw.length > 0).toBe(expected.length > 0);
  },
);

const automaticSources = [
  {
    name: "缺失来源",
    fields: {},
    errors: [["/source", "schema_required", false]],
  },
  {
    name: "空对象",
    fields: { source: {} },
    errors: [["/source/action_name", "schema_required", false]],
  },
  ...[null, [], "原值", 5].map((source) => ({
    name: `非对象 ${JSON.stringify(source)}`,
    fields: { source },
    errors: [["/source", "schema_type", true]],
  })),
  { name: "合法名称", fields: { source: { action_name: "录像" } }, errors: [] },
  {
    name: "空名称",
    fields: { source: { action_name: "" } },
    errors: [
      ["/source/action_name", "schema_minLength", false],
      ["/source/action_name", "schema_pattern", false],
    ],
  },
  {
    name: "名称类型非法",
    fields: { source: { action_name: 5 } },
    errors: [["/source/action_name", "schema_type", false]],
  },
  ...[
    { action_instance_id: "1" },
    { action_name: "录像", action_instance_id: "1" },
    { unknown: true },
  ].map((source) => ({
    name: `不适用形状 ${JSON.stringify(source)}`,
    fields: { source },
    errors: [["/source", "schema_oneOf", true]],
  })),
];
it.each(
  automaticSources.flatMap((source) =>
    [false, true].flatMap((list) =>
      filterStates.map((filter) => ({ source, list, filter })),
    ),
  ),
)(
  "自动来源合同不因其他错误改变：$source.name 列表=$list 筛选=$filter.name",
  ({ source, list, filter }) => {
    const { shown, raw } = previewCurrent({
      purpose: "auto_preview",
      ...filter.fields,
      ...source.fields,
      ...(list ? { output_ids: ["1"] } : {}),
    });
    const expected = source.errors.map(([suffix, code, json]) => [
      `/actions/1/params${suffix}`,
      code,
      json,
    ]);
    if (filter.automatic)
      expected.push(["/actions/1/params/filter", filter.automatic, false]);
    if (list) expected.push(["/actions/1/params", "schema_oneOf", true]);
    expect(
      shown
        .map((issue) => [issue.target, issue.issue.code, Boolean(issue.json)])
        .sort(),
    ).toEqual(expected.sort());
    expect(raw.length > 0).toBe(expected.length > 0);
  },
);

it.each([
  [[], "schema_minItems", "/output_ids"],
  [null, "schema_type", "/output_ids"],
  ["原值", "schema_type", "/output_ids"],
  [["bad"], "schema_pattern", "/output_ids/0"],
  [["1", "1"], "schema_uniqueItems", "/output_ids"],
] as const)(
  "自动冲突保留已填写列表的独立限制：%j",
  (output_ids, code, suffix) => {
    const { shown } = previewCurrent({
      purpose: "auto_preview",
      filter: "default",
      source: { action_name: "录像" },
      output_ids,
    });
    expect(
      shown.map((issue) => [issue.target, issue.issue.code]).sort(),
    ).toEqual(
      [
        ["/actions/1/params", "schema_oneOf"],
        ["/actions/1/params/filter", "schema_const"],
        [`/actions/1/params${suffix}`, code],
      ].sort(),
    );
  },
);

it.each([undefined, "manual", "auto_preview", "bad", null, 5, {}])(
  "缺失来源只承担已证明的共同必填：%j",
  (purpose) => {
    const { shown } = previewCurrent({
      ...(purpose === undefined ? {} : { purpose }),
      filter: "preview",
      output_ids: ["1"],
    });
    expect(
      shown.filter((issue) => issue.target?.includes("/source")),
    ).toMatchObject([
      {
        target: "/actions/1/params/source",
        issue: { code: "schema_required" },
        message: expect.stringMatching(/选择.*来源/),
      },
    ]);
    expect(
      shown.some(
        (issue) => issue.target === "/actions/1/params/source/action_name",
      ),
    ).toBe(false);
  },
);
