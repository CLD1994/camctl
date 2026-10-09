import { expect, it } from "vitest";
import { parseJson } from "../../src/shared/json";
import { createValidator, DIALECT } from "../../src/shared/validation";
import {
  loadCapabilities,
  validateParams,
} from "../../src/shared/capabilities";
import { validatePlan } from "../../src/shared/plan";
import {
  editValue,
  setValue,
  removeAction,
  appendDraftAction,
} from "../../src/web/editing";
import { switchActionType } from "../../src/web/action-drafts";

const time = "2026-10-10 00:00:00";
const types = ["camera_take_photo", "camera_timelapse", "camera_record"];
const directory = (rule: object = { type: "integer" }, preview = true) => ({
  devices: [
    {
      device_id: "cam",
      driver_id: "demo",
      actions: types.map((type) => ({
        type,
        parameter_types: [
          {
            type: "fixed",
            name: "固定",
            description: "测试",
            preview_supported: preview,
            schema: {
              $schema: DIALECT,
              type: "object",
              required: ["type"],
              additionalProperties: false,
              properties: { type: { const: "fixed" }, value: rule },
            },
          },
        ],
      })),
    },
  ],
});
const caps = loadCapabilities(directory());
const camera = (type = "camera_record") => ({
  name: "拍摄",
  type,
  device_id: "cam",
  scheduled_at: time,
  params: { type: "fixed" },
  policy: { max_delay_ms: 0 },
});
const plan = (actions: unknown[] = [camera()]) => ({
  request_id: "1",
  created_at: time,
  name: "计划",
  actions,
});
const obtain = (source: object, name = "取回", auto = false) => ({
  name,
  type: "obtain_action_outputs",
  scheduled_at: time,
  params: {
    source,
    ...(auto ? { purpose: "auto_preview", filter: "preview" } : {}),
  },
});
const rounded = "1.0000000000000001";

it.each(["\\ud800", "\\udc00", "\\udc00\\ud800", "\\ud800x\\udc00"])(
  "拒绝任意位置的非法标量 %s",
  (escaped) => {
    for (const text of [
      `{"${escaped}":1}`,
      `{"unused":[{"x":"${escaped}"}]}`,
    ]) {
      expect(() => parseJson(text)).toThrow();
      try {
        parseJson(text);
      } catch (error) {
        const message = String(error);
        expect(message).toContain("\\u");
        expect(
          new TextDecoder("utf-8", { fatal: true }).decode(
            new TextEncoder().encode(message),
          ),
        ).toBe(message);
      }
    }
  },
);
it("保留合法代理对与不归一化的字符串", () => {
  expect(parseJson(' {"\\ud83d\\ude00":"😀","x":"e\\u0301"} ')).toEqual({
    "😀": "😀",
    x: "e\u0301",
  });
});

it.each(
  types.flatMap((type) =>
    [rounded, "-1e-400"].map((token) => ({ type, token })),
  ),
)("公共策略按数学值拒绝 $type $token", ({ type, token }) => {
  const text = JSON.stringify(plan([camera(type)])).replace(
    '"max_delay_ms":0',
    `"max_delay_ms":${token}`,
  );
  expect(validatePlan(parseJson(text), caps)).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ path: "actions[0].policy.max_delay_ms" }),
    ]),
  );
});
it.each(["1.0", "1e0", "0", "9007199254740991"])(
  "公共策略接受合法整数 %s",
  (token) => {
    expect(
      validatePlan(
        parseJson(
          JSON.stringify(plan()).replace(
            '"max_delay_ms":0',
            `"max_delay_ms":${token}`,
          ),
        ),
        caps,
      ),
    ).toEqual([]);
  },
);

it.each([
  [{ type: "integer" }, false],
  [{ type: ["integer", "string"] }, false],
  [{ type: ["integer", "number"] }, true],
  [{ anyOf: [{ type: "integer" }, { type: "number" }] }, true],
  [{ oneOf: [{ type: "integer" }, { type: "number" }] }, true],
  [{ not: { type: "integer" } }, true],
  [{ if: { type: "integer" }, then: false, else: true }, true],
  [{ allOf: [{ type: "integer" }, { minimum: 0 }] }, false],
])("真实能力校验遵循精确整数分支 %#", (rule, valid) => {
  const parameters = parseJson(`{"type":"fixed","value":${rounded}}`);
  expect(
    validateParams(
      "cam",
      "camera_record",
      parameters,
      loadCapabilities(directory(rule as object)),
    ).length === 0,
  ).toBe(valid);
});
it.each([
  { $defs: { integer: { type: "integer" } }, $ref: "#/$defs/integer" },
  {
    $defs: {
      integer: {
        $id: "https://example.invalid/integer",
        $schema: DIALECT,
        type: "integer",
      },
    },
    $ref: "https://example.invalid/integer",
  },
])("实际引用仍执行精确整数约束 %#", (rule) => {
  const validate = createValidator().compile({
    type: "object",
    properties: { value: { $ref: rule.$ref } },
    $defs: rule.$defs,
  });
  expect(validate(parseJson(`{"value":${rounded}}`))).toBe(false);
});
it.each([
  { items: { type: "integer" } },
  { prefixItems: [{ type: "integer" }], items: false },
])("数组元素使用原词元 %#", (rule) => {
  const validate = createValidator().compile({ type: "array", ...rule });
  expect(validate(parseJson(`[${rounded}]`))).toBe(false);
});
it.each(["1.0", "1e0"])("能力整数接受等价表示 %s", (token) => {
  expect(
    validateParams(
      "cam",
      "camera_record",
      parseJson(`{"type":"fixed","value":${token}}`),
      caps,
    ),
  ).toEqual([]);
});
it.each([
  ["1.0000000000000001", "integer", false],
  ["-1e-400", "integer", false],
  ["1.0", "integer", true],
  ["1e0", "integer", true],
  ["1.0000000000000001", "number", true],
] as const)("根数字显式上下文保留整数性 %s %s", (token, type, expected) => {
  const validate = createValidator().compile({ type });
  expect(validate.call({ numberToken: token }, parseJson(token))).toBe(
    expected,
  );
});
it.each([
  ['{"n":"1"}', false],
  ['{"n":1.5}', false],
  ['{"n":-1}', false],
  ['{"n":3}', false],
  ["{}", false],
  ['{"n":1,"extra":true}', false],
  ['{"n":1}', true],
] as const)("追加整数规则保留原生类型范围及结构 %s", (text, expected) => {
  const validate = createValidator().compile({
    type: "object",
    required: ["n"],
    additionalProperties: false,
    properties: { n: { type: "integer", minimum: 0, maximum: 2 } },
  });
  expect(validate(parseJson(text))).toBe(expected);
});
it("依赖条件中的整数约束取得原词元", () => {
  const validate = createValidator().compile({
    type: "object",
    dependentSchemas: { enabled: { properties: { n: { type: "integer" } } } },
  });
  expect(validate(parseJson(`{"enabled":true,"n":${rounded}}`))).toBe(false);
  expect(validate(parseJson(`{"n":${rounded}}`))).toBe(true);
});
it.each(["req1", "0", "01", "9223372036854775808", 1, null])(
  "完整计划拒绝非法身份 %j",
  (id) => {
    expect(
      validatePlan({ ...plan(), request_id: id }, caps).some(
        (i) => i.path === "request_id",
      ),
    ).toBe(true);
  },
);
it.each(["1", "9223372036854775807"])("完整计划接受规范身份 %s", (id) => {
  expect(
    validatePlan({ ...plan(), request_id: id, last_report_id: id }, caps),
  ).toEqual([]);
});
it.each([
  "0001-01-01 00:00:00.1",
  "2026-02-29 00:00:00",
  `${time}Z`,
  ` ${time}`,
])("拒绝非规范或不真实时间 %s", (created_at) => {
  expect(
    validatePlan({ ...plan(), created_at }, caps).some(
      (i) => i.path === "created_at",
    ),
  ).toBe(true);
});
it.each(["obtain_action_outputs", "delete_action_outputs"])(
  "%s 校验本计划来源",
  (type) => {
    for (const source of [
      { action_name: "不存在" },
      { action_name: "报告" },
      { current_plan: true },
    ]) {
      const action = { ...obtain(source), type };
      const issues = validatePlan(
        plan([
          action,
          { name: "报告", type: "report_status", params: { scope: "full" } },
        ]),
        caps,
      );
      expect(
        issues.some(
          (i) =>
            i.code === "source_not_found" &&
            i.path.startsWith("actions[0].params.source"),
        ),
      ).toBe(true);
    }
  },
);
it.each(["obtain_action_outputs", "delete_action_outputs"])(
  "%s 保留跨计划未知引用",
  (type) => {
    for (const source of [
      { plan_instance_id: "9223372036854775807" },
      { action_instance_id: "2" },
    ])
      expect(validatePlan(plan([{ ...obtain(source), type }]), caps)).toEqual(
        [],
      );
  },
);
it("来源资格依完整集合和拍摄类型判断", () => {
  const source = { ...camera(), group: "组", params: { type: "missing" } };
  for (const reference of [
    { action_name: "拍摄" },
    { group: "组" },
    { current_plan: true },
  ])
    expect(
      validatePlan(plan([obtain(reference), source]), caps).filter(
        (i) => i.code === "source_not_found",
      ),
    ).toEqual([]);
});
it("显式自动取回要求能力与相同时间", () => {
  const actions = [camera(), obtain({ action_name: "拍摄" }, "自动", true)];
  expect(validatePlan(plan(actions), caps)).toEqual([]);
  expect(
    validatePlan(plan(actions), loadCapabilities(directory({}, false))).some(
      (i) => i.code === "preview_not_supported",
    ),
  ).toBe(true);
  expect(
    validatePlan(
      plan([camera(), { ...actions[1], scheduled_at: "2026-10-11 00:00:00" }]),
      caps,
    ).some((i) => i.path === "actions[1].scheduled_at"),
  ).toBe(true);
});
it("自动关联冲突包含另有参数错误的全部声明且与数组顺序无关", () => {
  const actions = [
    camera(),
    obtain({ action_name: "拍摄" }, "甲", true),
    {
      ...obtain({ action_name: "拍摄" }, "乙", true),
      params: {
        ...obtain({ action_name: "拍摄" }, "乙", true).params,
        output_ids: [],
      },
    },
    obtain({ action_name: "拍摄" }, "手动"),
  ];
  for (const ordered of [actions, [...actions].reverse()]) {
    const conflicts = validatePlan(plan(ordered), caps).filter(
      (i) => i.code === "duplicate_auto_preview",
    );
    expect(
      conflicts
        .map((i) => ordered[Number(/\[(\d+)\]/.exec(i.path)![1])].name)
        .sort(),
    ).toEqual(["乙", "甲"]);
    for (const issue of conflicts)
      for (const name of ["拍摄", "甲", "乙"])
        expect(issue.message).toContain(name);
  }
});
it("多个来源分别收集全部自动声明，不牵连唯一关联", () => {
  const actions = [
    camera(),
    { ...camera(), name: "第二拍摄" },
    ...["甲", "乙", "丙"].map((name) =>
      obtain({ action_name: "拍摄" }, name, true),
    ),
    obtain({ action_name: "第二拍摄" }, "唯一", true),
  ];
  expect(
    validatePlan(plan(actions), caps)
      .filter((i) => i.code === "duplicate_auto_preview")
      .map((i) => i.path),
  ).toEqual([
    "actions[2].params.source.action_name",
    "actions[3].params.source.action_name",
    "actions[4].params.source.action_name",
  ]);
});

it.each(["number", "json"] as const)(
  "%s 局部输入及后续改写保留非法整数事实",
  (kind) => {
    let content = { text: JSON.stringify(plan()) };
    content = editValue(
      content,
      kind === "number"
        ? ["actions", 0, "params", "value"]
        : ["actions", 0, "params"],
      kind === "number" ? rounded : `{"type":"fixed","value":${rounded}}`,
      kind,
    );
    content = setValue(content, ["name"], "改名");
    content = appendDraftAction(content, {
      name: "报告",
      type: "report_status",
      params: { scope: "full" },
    });
    content = removeAction(content, 1);
    content = switchActionType(content, 0, "report_status");
    content = JSON.parse(JSON.stringify(content));
    content = switchActionType(content, 0, "camera_record");
    expect(content.text).toContain(rounded);
    expect(
      validatePlan(parseJson(content.text), caps).some(
        (i) => i.path === "actions[0].params.value",
      ),
    ).toBe(true);
  },
);
it("普通 number 参数的原数值不会被整数规则额外拒绝", () => {
  const content = editValue(
    { text: JSON.stringify(plan()) },
    ["actions", 0, "params", "value"],
    rounded,
    "number",
  );
  expect(
    validatePlan(
      parseJson(content.text),
      loadCapabilities(directory({ type: "number" })),
    ),
  ).toEqual([]);
});
it("显式替换同一浮点值清除旧词元证据", () => {
  const text = JSON.stringify(plan()).replace(
    '"type":"fixed"',
    `"type":"fixed","value":${rounded}`,
  );
  const content = setValue({ text }, ["actions", 0, "params", "value"], 1);
  expect(validatePlan(parseJson(content.text), caps)).toEqual([]);
});
