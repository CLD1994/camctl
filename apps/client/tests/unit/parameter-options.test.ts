import { expect, it } from "vitest";
import {
  parameterOptions,
  compatibleValues,
  optionLabel,
  parameterRequired,
  parameterChoiceBlocked,
  sameValueAt,
  parameterFieldOptions,
  parameterFieldIndependent,
} from "../../src/web/parameter-options";
import { createValidator, DIALECT } from "../../src/shared/validation";
import type { ParameterType } from "../../src/shared/types";
import {
  parseJson,
  originalNumberToken,
  exactJsonValueIdentity,
  exactJsonIdentity,
  stringifyJson,
} from "../../src/shared/json";

const independent = (): ParameterType => ({
  type: "example_record",
  name: "普通录像",
  description: "独立字段",
  preview_supported: true,
  schema: {
    $schema: DIALECT,
    type: "object",
    additionalProperties: false,
    properties: {
      type: { const: "example_record" },
      bitrate_mode: { type: "string", enum: ["standard", "high"] },
      duration_s: { type: "integer", minimum: 1, maximum: 3600 },
    },
    required: ["type", "bitrate_mode", "duration_s"],
  },
});
it("投影核对入口保持既有语义，不隐式读取父容器原词元", () => {
  expect(exactJsonIdentity(parseJson('{"value":1e-999}'))).toBe(
    exactJsonIdentity({ value: 0 }),
  );
});
it("封闭的普通录像规则为独立码率提供选项，不枚举时长区间", () => {
  expect(parameterOptions(independent())).toMatchObject({
    kind: "independent",
    fields: [
      { name: "type" },
      { name: "bitrate_mode", values: ["standard", "high"] },
    ],
  });
});
it.each([
  { type: "object", properties: { mode: { enum: ["manual", "auto"] } } },
  { oneOf: [{ type: "object" }, { type: "array" }] },
  { type: "array", items: { type: "number" } },
  { $ref: "#/properties/duration_s" },
])("复杂兄弟字段保留 JSON，不遮蔽独立枚举 %#", (exposure) => {
  const p = independent();
  (p.schema.properties as any).exposure = exposure;
  expect(parameterOptions(p)).toMatchObject({
    kind: "independent",
    fields: [
      { name: "type" },
      { name: "bitrate_mode", values: ["standard", "high"] },
    ],
  });
});
it.each([
  '{"type":"integer","minimum":1,"maximum":9223372036854775}',
  '{"type":"number","minimum":1e-999}',
  '{"enum":[1.0000000000000001]}',
  '{"type":"object","properties":{"n":{"enum":[1e-999]}}}',
])("其他字段数值精度不改变当前独立枚举资格 %s", (text) => {
  const p = independent();
  (p.schema.properties as any).other = parseJson(text);
  const before = stringifyJson(p.schema);
  expect(parameterOptions(p)).toMatchObject({
    kind: "independent",
    fields: [
      { name: "type" },
      { name: "bitrate_mode", values: ["standard", "high"] },
    ],
  });
  expect(stringifyJson(p.schema)).toBe(before);
});
it.each(['{"enum":[1.0000000000000001]}', '{"enum":[0,1],"minimum":1e-999}'])(
  "当前字段数值事实无法准确生成时只保留该字段 JSON %s",
  (text) => {
    const p = independent();
    (p.schema.properties as any).bitrate_mode = parseJson(text);
    (p.schema.properties as any).quality = { enum: ["a", "b"] };
    expect(parameterOptions(p)).toMatchObject({
      kind: "independent",
      fields: [{ name: "type" }, { name: "quality", values: ["a", "b"] }],
    });
  },
);
it("复杂兄弟字段不改变局部空允许集合的含义", () => {
  const p = independent();
  (p.schema.properties as any).exposure = { oneOf: [{ type: "object" }] };
  (p.schema.properties as any).bitrate_mode = { enum: ["a"], pattern: "^b" };
  expect(parameterOptions(p)).toMatchObject({
    kind: "independent",
    fields: [{ name: "type" }, { name: "bitrate_mode", values: [] }],
  });
});
it.each(["default", "examples"])(
  "根说明元数据 %s 的数值精度不改变独立控件资格",
  (keyword) => {
    const p = independent();
    const schemaText = stringifyJson(p.schema);
    p.schema = parseJson(
      schemaText.slice(0, -1) +
        `,"${keyword}":${keyword === "examples" ? "[1.0000000000000001]" : "1.0000000000000001"}}`,
    ) as Record<string, unknown>;
    const before = stringifyJson(p.schema);
    expect(parameterOptions(p).kind).toBe("independent");
    expect(stringifyJson(p.schema)).toBe(before);
  },
);
it.each(["default", "examples"])(
  "字段说明元数据 %s 的数值精度不改变局部选择",
  (keyword) => {
    const p = independent();
    (p.schema.properties as any).bitrate_mode = parseJson(
      `{"type":"string","enum":["standard","high"],"${keyword}":${keyword === "examples" ? "[1e-999]" : "1e-999"}}`,
    );
    expect(parameterOptions(p)).toMatchObject({
      kind: "independent",
      fields: [
        { name: "type" },
        { name: "bitrate_mode", values: ["standard", "high"] },
      ],
    });
  },
);
it.each(["enum", "const"])(
  "业务对象内名为 default/examples 的成员仍参与 %s 数值资格",
  (keyword) => {
    const p = parameter();
    (p.schema.properties as any).config = parseJson(
      `{"${keyword}":${keyword === "enum" ? "[" : ""}{"default":1e-999,"examples":1.0000000000000001}${keyword === "enum" ? "]" : ""}}`,
    );
    expect(parameterOptions(p)).toEqual({
      kind: "unavailable",
      reason: "numeric",
    });
  },
);
it("名为 default 的属性与局部定义保留其验证断言", () => {
  const p = parameter();
  (p.schema.properties as any).default = parseJson('{"enum":[1e-999]}');
  expect(parameterOptions(p)).toEqual({
    kind: "unavailable",
    reason: "numeric",
  });
  const referenced = parameter();
  (referenced.schema.$defs as any).default = parseJson(
    '{"enum":[1.0000000000000001]}',
  );
  (referenced.schema.properties as any).fps = { $ref: "#/$defs/default" };
  expect(parameterOptions(referenced)).toEqual({
    kind: "unavailable",
    reason: "numeric",
  });
});
it("条件中的真实数字断言仍阻止生成舍入候选", () => {
  const p = parameter();
  p.schema.then = parseJson(
    '{"properties":{"fps":{"const":30.0000000000000001}}}',
  );
  expect(parameterOptions(p)).toEqual({
    kind: "unavailable",
    reason: "numeric",
  });
});
it("局部数值边界的原数学事实仍使候选保守降级", () => {
  const p = parameter();
  (p.schema.properties as any).value = parseJson(
    '{"enum":[0,1],"minimum":1e-999}',
  );
  expect(parameterOptions(p)).toEqual({
    kind: "unavailable",
    reason: "numeric",
  });
});
it("条件与引用 Schema 节点上的说明元数据不影响完整目录", () => {
  const p = parameter();
  (p.schema.$defs as any).fps = parseJson(
    '{"type":"integer","enum":[30,60],"examples":[1e-999]}',
  );
  p.schema.then = parseJson(
    '{"properties":{"fps":{"const":30,"default":1.0000000000000001}}}',
  );
  expect(parameterOptions(p)).toMatchObject({
    kind: "finite",
    rows: [
      { type: "demo", resolution: "4K", fps: 30 },
      { type: "demo", resolution: "1080p", fps: 30 },
      { type: "demo", resolution: "1080p", fps: 60 },
    ],
  });
});
it("属性名称不影响独立选择的分类", () => {
  const p = independent();
  p.schema.properties = {
    type: { const: p.type },
    quality: { type: "string", enum: ["a", "b"] },
    seconds: { type: "number" },
  };
  p.schema.required = ["type", "quality"];
  expect(parameterOptions(p)).toMatchObject({
    kind: "independent",
    fields: [{ name: "type" }, { name: "quality", values: ["a", "b"] }],
  });
});
it("独立选项取所有局部断言的交集", () => {
  const p = independent();
  (p.schema.properties as any).bitrate_mode = {
    enum: ["a", "aa", "b", 1],
    type: "string",
    minLength: 2,
    pattern: "^a",
  };
  expect(parameterOptions(p)).toMatchObject({
    kind: "independent",
    fields: [{ name: "type" }, { name: "bitrate_mode", values: ["aa"] }],
  });
});
it("独立字段的空允许集不折叠为无法推导", () => {
  const p = independent();
  (p.schema.properties as any).bitrate_mode = {
    enum: ["a"],
    type: "string",
    pattern: "^b",
  };
  expect(parameterOptions(p)).toMatchObject({
    kind: "independent",
    fields: [{ name: "type" }, { name: "bitrate_mode", values: [] }],
  });
});
it.each([
  { if: {}, then: {} },
  { allOf: [{}] },
  { anyOf: [{}] },
  { oneOf: [{}] },
  { not: {} },
  { dependentRequired: { bitrate_mode: ["duration_s"] } },
  { dependentSchemas: { bitrate_mode: {} } },
  { minProperties: 1 },
  { maxProperties: 10 },
  { propertyNames: { type: "string" } },
  { patternProperties: {} },
  { unevaluatedProperties: false },
  { $ref: "#/$defs/p", $defs: { p: {} } },
  { additionalProperties: true },
])("不为未证明独立的根断言提供局部选择 %#", (assertion) => {
  const p = independent();
  Object.assign(p.schema, assertion);
  expect(parameterOptions(p).kind).toBe("unavailable");
});
it.each([
  { $ref: "#/$defs/value" },
  { allOf: [{ enum: ["a"] }] },
  { type: "object", properties: {} },
  { type: "array", items: { type: "string" } },
  { $id: "https://example.test/field", enum: ["a"] },
])("不把引用或复杂字段投影用作独立证明 %#", (definition) => {
  const p = independent();
  (p.schema.properties as any).bitrate_mode = definition;
  p.schema.$defs = { value: { enum: ["a"] } };
  expect(parameterOptions(p).kind).toBe("unavailable");
});
it("独立字段保留 null、false、零和空字符串的不同候选", () => {
  const p = independent();
  (p.schema.properties as any).bitrate_mode = { enum: [null, false, 0, ""] };
  expect(parameterOptions(p)).toMatchObject({
    kind: "independent",
    fields: [
      { name: "type" },
      { name: "bitrate_mode", values: [null, false, 0, ""] },
    ],
  });
});
it("过多的独立候选保留 JSON 入口，不创建过量选项", () => {
  const p = independent();
  (p.schema.properties as any).bitrate_mode = {
    enum: Array.from({ length: 4097 }, (_, i) => String(i)),
  };
  expect(parameterOptions(p)).toMatchObject({
    kind: "independent",
    fields: [{ name: "type" }],
  });
});
it.each(["1e-999", "1.0000000000000001"])(
  "完整目录兼容判断不把原数学值 %s 当作 Number 投影",
  (token) => {
    const p = parameter();
    (p.schema.properties as any).value = { enum: [0, 1] };
    const params = parseJson(`{"type":"demo","value":${token}}`) as Record<
      string,
      unknown
    >;
    expect(compatibleValues(parameterOptions(p), params, "fps")).toEqual([]);
  },
);
it.each(["1", "1.0", "1e0"])("数值候选保留数学等价的原值 %s", (token) => {
  const p = parameter();
  (p.schema.properties as any).value = { enum: [1] };
  expect(
    compatibleValues(
      parameterOptions(p),
      parseJson(`{"type":"demo","value":${token}}`) as Record<string, unknown>,
      "fps",
    ),
  ).toEqual([30, 60]);
});
it.each(['{"a":1e-999}', "[1.0000000000000001]"])(
  "嵌套枚举的数学事实仍由实际父容器比较 %s",
  (text) => {
    const p = parameter();
    (p.schema.properties as any).value = {
      enum: [text.startsWith("{") ? { a: 0 } : [1]],
    };
    expect(
      compatibleValues(
        parameterOptions(p),
        parseJson(`{"type":"demo","value":${text}}`) as Record<string, unknown>,
        "fps",
      ),
    ).toEqual([]);
  },
);
it("对象顺序不改变精确身份，数组顺序仍参与身份", () => {
  expect(exactJsonValueIdentity(parseJson('{"a":1e0,"b":[false,null]}'))).toBe(
    exactJsonValueIdentity({ b: [false, null], a: 1 }),
  );
  expect(exactJsonValueIdentity([0, 1])).not.toBe(
    exactJsonValueIdentity([1, 0]),
  );
  expect(sameValueAt({}, "value", { value: null }, "value")).toBe(false);
});
it("候选积与过滤后的选项保持源数值词元", () => {
  const p = parameter();
  (p.schema.properties as any).value = parseJson('{"enum":[1e0]}');
  const catalog = parameterOptions(p);
  if (catalog.kind !== "finite") throw new Error("应为有限目录");
  const row = catalog.rows.find((row) => Object.hasOwn(row, "value"))!;
  expect(originalNumberToken(row, "value", row.value)).toBe("1e0");
  const values = compatibleValues(catalog, { type: "demo" }, "value");
  expect(originalNumberToken(values, 0, values[0])).toBe("1e0");
});
it.each(["1e-999", "1.0000000000000001"])(
  "不能准确验证的候选数学事实 %s 只将该字段交给 JSON",
  (token) => {
    const p = independent();
    (p.schema.properties as any).bitrate_mode = parseJson(
      `{"enum":[${token}]}`,
    );
    expect(parameterOptions(p)).toMatchObject({
      kind: "independent",
      fields: [{ name: "type" }],
    });
  },
);

const pendingContent = (pendingPath: string) => ({
  text: '{"actions":[{"type":"camera_record","device_id":"cam","params":{"type":"demo","quality":"a","seconds":60}},{"type":"camera_record","params":{}}]}',
  pending: { [pendingPath]: { kind: "json" as const, text: "?" } },
});
it.each([
  "",
  "/actions",
  "/actions/0",
  "/actions/0/params",
  "/actions/0/params/quality",
  "/actions/0/params/quality/x",
  "/actions/0/device_id",
  "/actions/0/type",
  "/actions/0/params/type",
  "/bad~3",
  "/actions/01/params/x",
  "/actions/999/params/x",
])("相关或无法判断的 pending 暂停选择 %s", (pendingPath) => {
  expect(
    parameterChoiceBlocked(
      pendingContent(pendingPath),
      ["actions", 0, "params", "quality"],
      "finite",
    ),
  ).toBe(true);
  expect(
    parameterChoiceBlocked(
      pendingContent(pendingPath),
      ["actions", 0, "params", "quality"],
      "independent",
    ),
  ).toBe(true);
});
it("只有完整联动目录受其他参数的 pending 影响", () => {
  const content = pendingContent("/actions/0/params/seconds");
  expect(
    parameterChoiceBlocked(
      content,
      ["actions", 0, "params", "quality"],
      "finite",
    ),
  ).toBe(true);
  expect(
    parameterChoiceBlocked(
      content,
      ["actions", 0, "params", "quality"],
      "independent",
    ),
  ).toBe(false);
});
it.each([
  "/name",
  "/actions/0/name",
  "/actions/0/scheduled_at",
  "/actions/0/policy/max_delay_ms",
  "/actions/1/params/value",
])("无关公共字段和其他动作的 pending 不影响当前选择 %s", (pendingPath) => {
  expect(
    parameterChoiceBlocked(
      pendingContent(pendingPath),
      ["actions", 0, "params", "quality"],
      "finite",
    ),
  ).toBe(false);
  expect(
    parameterChoiceBlocked(
      pendingContent(pendingPath),
      ["actions", 0, "params", "quality"],
      "independent",
    ),
  ).toBe(false);
});
it("转义后的属性名与普通字段边界严格区分", () => {
  expect(
    parameterChoiceBlocked(
      pendingContent("/actions/0/params/a~1b~0c"),
      ["actions", 0, "params", "a/b~c"],
      "independent",
    ),
  ).toBe(true);
  expect(
    parameterChoiceBlocked(
      pendingContent("/actions/0/params/quality_more"),
      ["actions", 0, "params", "quality"],
      "independent",
    ),
  ).toBe(false);
});

const parameter = (): ParameterType => ({
  type: "demo",
  name: "测试录像",
  description: "组合测试",
  preview_supported: false,
  schema: {
    $schema: DIALECT,
    type: "object",
    additionalProperties: false,
    properties: {
      type: { const: "demo" },
      resolution: { type: "string", enum: ["4K", "1080p"] },
      fps: { $ref: "#/$defs/fps" },
    },
    required: ["type", "resolution", "fps"],
    $defs: { fps: { type: "integer", enum: [30, 60] } },
    if: {
      properties: { resolution: { const: "4K" } },
      required: ["resolution"],
    },
    then: { properties: { fps: { const: 30 } } },
  },
});
it("完整条件规则只生成三个合法组合", () => {
  const result = parameterOptions(parameter());
  expect(result.kind).toBe("finite");
  if (result.kind === "finite")
    expect(result.rows).toEqual([
      { type: "demo", resolution: "4K", fps: 30 },
      { type: "demo", resolution: "1080p", fps: 30 },
      { type: "demo", resolution: "1080p", fps: 60 },
    ]);
});
it.each([
  [{ type: "demo" }, "fps", [30, 60]],
  [{ type: "demo", resolution: "4K" }, "fps", [30]],
  [{ type: "demo", fps: 60 }, "resolution", ["1080p"]],
  [{ type: "demo", resolution: "4K", fps: 60 }, "resolution", ["1080p"]],
  [{ type: "demo", resolution: "4K", fps: 60 }, "fps", [30]],
  [{ type: "demo", fps: "30" }, "resolution", []],
  [{ type: "demo", extra: true }, "fps", []],
] as const)("保留其他已填值计算兼容选项 %j %s", (params, field, expected) => {
  const before = structuredClone(params);
  expect(
    compatibleValues(parameterOptions(parameter()), params, field),
  ).toEqual(expected);
  expect(params).toEqual(before);
});
it("可选参数的缺省、null、false、0和空字符串分别保存", () => {
  const p = parameter();
  p.schema = {
    $schema: DIALECT,
    type: "object",
    additionalProperties: false,
    properties: {
      type: { const: "demo" },
      value: { enum: [null, false, 0, ""] },
    },
    required: ["type"],
  };
  const result = parameterOptions(p);
  expect(result.kind).toBe("finite");
  if (result.kind === "finite")
    expect(result.rows).toEqual([
      { type: "demo" },
      { type: "demo", value: null },
      { type: "demo", value: false },
      { type: "demo", value: 0 },
      { type: "demo", value: "" },
    ]);
});
it("引用旁的约束仍按交集校验", () => {
  const p = parameter();
  (p.schema.properties as any).fps = { $ref: "#/$defs/fps", enum: [60, 120] };
  expect(
    compatibleValues(parameterOptions(p), { type: "demo" }, "fps"),
  ).toEqual([60]);
});
it("互斥分支和依赖约束使用完整规则", () => {
  const p = parameter();
  p.schema.oneOf = [
    { properties: { fps: { const: 30 } } },
    { properties: { resolution: { const: "1080p" } } },
  ];
  p.schema.dependentRequired = { fps: ["resolution"] };
  const result = parameterOptions(p);
  if (result.kind !== "finite") throw new Error("应为有限目录");
  expect(result.rows).toEqual([
    { type: "demo", resolution: "4K", fps: 30 },
    { type: "demo", resolution: "1080p", fps: 60 },
  ]);
});
it("合法规则但没有合法组合与不可枚举区分", () => {
  const p = parameter();
  p.schema.not = {};
  expect(parameterOptions(p)).toMatchObject({ kind: "finite", rows: [] });
});
it("不可枚举目录不被解释为没有兼容选项或非必填", () => {
  const p = parameter();
  delete p.schema.additionalProperties;
  const catalog = parameterOptions(p);
  expect(() => compatibleValues(catalog, { type: "demo" }, "fps")).toThrow();
  expect(() => parameterRequired(catalog, { type: "demo" }, "fps")).toThrow();
});
it.each(["open", "unbounded", "limit"])(
  "不能完整生成时明确返回%s",
  (reason) => {
    const p = parameter();
    if (reason === "open") delete p.schema.additionalProperties;
    if (reason === "unbounded")
      (p.schema.properties as any).free = { type: "string" };
    if (reason === "limit")
      (p.schema.properties as any).many = {
        enum: Array.from({ length: 1500 }, (_, i) => i),
      };
    expect(parameterOptions(p)).toMatchObject({ kind: "unavailable", reason });
  },
);
it("对象枚举按JSON值比较，不依赖对象键顺序", () => {
  const p = parameter();
  (p.schema.properties as any).config = { enum: [{ a: 1, b: 2 }] };
  expect(
    compatibleValues(
      parameterOptions(p),
      { type: "demo", config: { b: 2, a: 1 } },
      "fps",
    ),
  ).toEqual([30, 60]);
});
it("条件必填随其他选择变化，无法兼容时仍保留根必填要求", () => {
  const p = parameter();
  p.schema.required = ["type", "resolution"];
  p.schema.then = { required: ["fps"], properties: { fps: { const: 30 } } };
  const catalog = parameterOptions(p);
  expect(
    parameterRequired(catalog, { type: "demo", resolution: "4K" }, "fps"),
  ).toBe(true);
  expect(
    parameterRequired(catalog, { type: "demo", resolution: "1080p" }, "fps"),
  ).toBe(false);
  expect(
    parameterRequired(catalog, { type: "demo", fps: "invalid" }, "resolution"),
  ).toBe(true);
});
it.each([
  ["4K", "4K"],
  [30, "30"],
  [false, "否"],
  [null, "null"],
  ["", "空字符串"],
])("选项标签保持值含义 %j", (value, expected) => {
  expect(optionLabel(value)).toBe(expected);
});

const grouped = (): ParameterType => ({
  ...independent(),
  schema: {
    $schema: DIALECT,
    type: "object",
    additionalProperties: false,
    properties: {
      type: { const: "example_record" },
      pace: { type: "number", enum: [0.5, 1, 2] },
      span: { type: "integer", enum: [1, 2] },
      note: { type: "string" },
      profile: {
        oneOf: [
          {
            type: "object",
            properties: { mode: { const: "auto" } },
            required: ["mode"],
            additionalProperties: false,
          },
          {
            type: "object",
            properties: {
              mode: { const: "manual" },
              gain: { type: "integer" },
            },
            required: ["mode", "gain"],
            additionalProperties: false,
          },
        ],
      },
    },
    required: ["type", "pace", "span", "profile"],
    allOf: [
      {
        if: { required: ["span"], properties: { span: { const: 1 } } },
        then: { properties: { pace: { exclusiveMaximum: 1 } } },
        else: { properties: { pace: { exclusiveMaximum: 2 } } },
      },
    ],
  },
});
const groupCatalog = (p = grouped()) => {
  const result = parameterOptions(p);
  expect(result.kind).toBe("grouped");
  if (result.kind !== "grouped") throw new Error("应为关联组");
  return result;
};
it("受限根合取生成局部关联目录并证明其余字段独立", () => {
  const p = grouped(),
    before = stringifyJson(p.schema);
  const result = groupCatalog(p);
  expect(result.group.members).toEqual(["pace", "span"]);
  expect(result.independent).toEqual(["type", "note", "profile"]);
  expect(result.group.catalog).toMatchObject({
    kind: "finite",
    rows: [
      { pace: 0.5, span: 1 },
      { pace: 0.5, span: 2 },
      { pace: 1, span: 2 },
    ],
  });
  expect(parameterFieldIndependent(result, "profile")).toBe(true);
  expect(parameterFieldIndependent(result, "pace")).toBe(false);
  expect(parameterFieldIndependent(result, "missing")).toBe(false);
  expect(parameterFieldOptions(result, "pace")).toBe(result.group.catalog);
  expect(parameterFieldOptions(result, "type")).toMatchObject({
    kind: "independent",
  });
  expect(stringifyJson(p.schema)).toBe(before);
});
it("局部兼容只读取其他组成员，保留非法独立输入与完整输入", () => {
  const result = groupCatalog();
  const params = parseJson(
    '{"pace":2,"span":2,"profile":false,"extra":true,"note":null} ',
  ) as Record<string, unknown>;
  const before = stringifyJson(params);
  expect(compatibleValues(result, params, "pace")).toEqual([0.5, 1]);
  expect(compatibleValues(result, { pace: 1 }, "span")).toEqual([2]);
  expect(compatibleValues(result, { pace: null }, "span")).toEqual([]);
  expect(compatibleValues(result, params, "type")).toEqual(["example_record"]);
  expect(parameterRequired(result, params, "pace")).toBe(true);
  expect(parameterRequired(result, params, "profile")).toBe(true);
  expect(parameterRequired(result, params, "note")).toBe(false);
  expect(stringifyJson(params)).toBe(before);
});
it("条件必填、else 和多个原条件共同过滤候选", () => {
  const p = grouped();
  p.schema.required = ["type", "pace"];
  p.schema.allOf = [
    {
      if: { required: ["pace"], properties: { pace: { const: 0.5 } } },
      then: { required: ["span"], properties: { span: { const: 1 } } },
      else: { properties: { span: { const: 2 } } },
    },
    {
      if: { required: ["span"] },
      then: { properties: { pace: { maximum: 1 } } },
    },
  ];
  const result = groupCatalog(p);
  expect(result.group.catalog).toMatchObject({
    kind: "finite",
    rows: [
      { pace: 0.5, span: 1 },
      { pace: 1 },
      { pace: 1, span: 2 },
      { pace: 2 },
    ],
  });
  expect(parameterRequired(result, { pace: 0.5 }, "span")).toBe(true);
  expect(parameterRequired(result, { pace: 1 }, "span")).toBe(false);
});
it("条件的对象类型断言不能被删除后误判为可分解", () => {
  const p = grouped();
  p.schema.allOf = [
    { if: { type: "array", properties: { span: { const: 1 } } }, then: false },
  ];
  expect(parameterOptions(p).kind).toBe("unavailable");
});
it("完整有限目录先于局部组，继续按全部参数过滤", () => {
  const p = grouped(),
    properties = p.schema.properties as Record<string, unknown>;
  delete properties.note;
  delete properties.profile;
  p.schema.required = ["type", "pace", "span"];
  const result = parameterOptions(p);
  expect(result.kind).toBe("finite");
  expect(compatibleValues(result, { span: 2, extra: true }, "pace")).toEqual(
    [],
  );
});
it("独立字段查询保留自身必填证明，不依赖其他字段", () => {
  const result = parameterOptions(independent());
  expect(parameterFieldIndependent(result, "duration_s")).toBe(true);
  expect(parameterFieldIndependent(result, "missing")).toBe(false);
  expect(parameterRequired(result, { duration_s: null }, "duration_s")).toBe(
    true,
  );
  expect(
    compatibleValues(result, { duration_s: null }, "bitrate_mode"),
  ).toEqual(["standard", "high"]);
});
it("组外精度损失与注释不影响当前有限组", () => {
  const p = grouped(),
    properties = p.schema.properties as Record<string, unknown>;
  properties.profile = parseJson('{"type":"number","minimum":1e-999}');
  p.schema.examples = parseJson("[1.0000000000000001]");
  const result = groupCatalog(p);
  expect(result.group.catalog).toMatchObject({
    kind: "finite",
    rows: [
      { pace: 0.5, span: 1 },
      { pace: 0.5, span: 2 },
      { pace: 1, span: 2 },
    ],
  });
});
it("布尔条件叶节点仍参与过滤，空合法集合有明确状态", () => {
  const p = grouped();
  (p.schema.allOf as unknown[]).push({ if: true, then: false });
  expect(groupCatalog(p).group.catalog).toMatchObject({
    kind: "finite",
    rows: [],
  });
});
it.each([
  { if: true, then: false },
  { if: false, then: false, else: true },
  { if: { type: "object" }, then: {} },
])("不为无引用字段的条件集合建立空组 %#", (condition) => {
  const p = grouped();
  p.schema.allOf = [condition];
  expect(parameterOptions(p).kind).toBe("unavailable");
});
it.each([
  { $id: "https://example.test/rules" },
  { $defs: { value: {} } },
  { $ref: "#/$defs/value", $defs: { value: {} } },
  { anyOf: [{}] },
  { oneOf: [{}] },
  { not: false },
  { dependentRequired: { pace: ["span"] } },
  { dependentSchemas: { pace: {} } },
  { minProperties: 1 },
  { maxProperties: 20 },
  { propertyNames: { type: "string" } },
  { patternProperties: {} },
  { unevaluatedProperties: false },
  { if: true, then: true },
  { unknownAssertion: true },
  { additionalProperties: true },
])("未覆盖根结构不提供分解证明 %#", (change) => {
  const p = grouped();
  Object.assign(p.schema, change);
  expect(parameterOptions(p).kind).toBe("unavailable");
});
it.each([
  { if: {}, then: {}, anyOf: [{}] },
  { if: {}, then: {}, $id: "https://example.test/condition" },
  { if: { $ref: "#/properties/type" }, then: {} },
  { if: { allOf: [{}] }, then: {} },
  { if: { properties: { absent: { const: 1 } } }, then: {} },
  { if: { required: ["absent"] }, then: {} },
  { if: { additionalProperties: false }, then: {} },
  {
    if: { properties: { span: { type: "object", properties: {} } } },
    then: {},
  },
  { if: { properties: { span: { $ref: "#/properties/type" } } }, then: {} },
  { if: { properties: { span: { allOf: [{}] } } }, then: {} },
  { if: {}, then: { properties: { span: { enum: [{ nested: 1 }] } } } },
])("未覆盖条件不能删去断言继续推导 %#", (condition) => {
  const p = grouped();
  p.schema.allOf = [condition];
  expect(parameterOptions(p).kind).toBe("unavailable");
});
it.each(["then", "else"])("条件 %s 分支中的未覆盖断言也拒绝分解", (keyword) => {
  const p = grouped();
  (p.schema.allOf as Record<string, unknown>[])[0][keyword] = {
    properties: { pace: { enum: [1] } },
    minProperties: 1,
  };
  expect(parameterOptions(p).kind).toBe("unavailable");
});
it.each(["unbounded", "limit", "numeric"] as const)(
  "已证明组不可枚举仍保留独立性 %s",
  (reason) => {
    const p = grouped(),
      properties = p.schema.properties as Record<string, unknown>;
    if (reason === "unbounded") properties.pace = { type: "number" };
    if (reason === "limit")
      properties.pace = { enum: Array.from({ length: 2049 }, (_, i) => i) };
    if (reason === "numeric")
      properties.pace = parseJson('{"enum":[1.0000000000000001]}');
    const result = groupCatalog(p);
    expect(result.group.catalog).toEqual({ kind: "unavailable", reason });
    expect(parameterFieldIndependent(result, "profile")).toBe(true);
    expect(compatibleValues(result, {}, "type")).toEqual(["example_record"]);
    expect(() => compatibleValues(result, {}, "pace")).toThrow();
  },
);
it("参与条件的精确数字事实无法可靠枚举时只停用组", () => {
  const p = grouped();
  p.schema.allOf = parseJson(
    '[{"if":{"properties":{"span":{"const":1.0000000000000001}}},"then":{"properties":{"pace":{"const":1}}}}]',
  );
  expect(groupCatalog(p).group.catalog).toEqual({
    kind: "unavailable",
    reason: "numeric",
  });
});
it.each([4096, 4097])("关联组在分配行前执行候选积上限 %s", (size) => {
  const p = grouped(),
    properties = p.schema.properties as Record<string, unknown>;
  properties.pace = { enum: Array.from({ length: size }, (_, i) => i) };
  properties.span = { const: 1 };
  p.schema.allOf = [
    { if: { required: ["span"] }, then: { required: ["pace"] } },
  ];
  const group = groupCatalog(p).group.catalog;
  if (size === 4096) {
    expect(group.kind).toBe("finite");
    if (group.kind === "finite") expect(group.rows).toHaveLength(4096);
  } else expect(group).toEqual({ kind: "unavailable", reason: "limit" });
});
it("可选缺省、null、false、零和空字符串在局部组中分别参与原条件", () => {
  const p = grouped(),
    properties = p.schema.properties as Record<string, unknown>;
  properties.pace = { enum: [null, false, 0, ""] };
  properties.span = { const: 1 };
  p.schema.required = ["type", "span"];
  p.schema.allOf = [
    {
      if: { required: ["span"] },
      then: { properties: { pace: { enum: [null, false, 0, ""] } } },
    },
  ];
  expect(groupCatalog(p).group.catalog).toMatchObject({
    kind: "finite",
    rows: [
      { span: 1 },
      { pace: null, span: 1 },
      { pace: false, span: 1 },
      { pace: 0, span: 1 },
      { pace: "", span: 1 },
    ],
  });
});
it.each(["5e-1", "0.50", "0.50000000000000001"])(
  "局部查询按精确数学身份比较 %s",
  (token) => {
    const result = groupCatalog();
    const params = parseJson(`{"pace":${token}}`) as Record<string, unknown>;
    expect(compatibleValues(result, params, "span")).toEqual(
      token === "0.50000000000000001" ? [] : [1, 2],
    );
  },
);
it("局部候选复制保留源数字词元且不修改输入规则", () => {
  const p = grouped();
  (p.schema.properties as Record<string, unknown>).pace = parseJson(
    '{"enum":[5e-1,1e0]}',
  );
  const before = stringifyJson(p.schema),
    result = groupCatalog(p);
  const values = compatibleValues(result, { span: 2 }, "pace");
  expect(originalNumberToken(values, 0, values[0])).toBe("5e-1");
  expect(originalNumberToken(values, 1, values[1])).toBe("1e0");
  expect(stringifyJson(p.schema)).toBe(before);
});
it.each([
  "/actions/0/params/pace",
  "/actions/0/params/span",
  "/actions/0/params/span/member",
  "/actions/0/params/type",
  "/actions/0/device_id",
  "/actions/0/params",
  "/bad~3",
])("局部组被成员或身份 pending 阻塞 %s", (pendingPath) => {
  const result = groupCatalog();
  expect(
    parameterChoiceBlocked(
      pendingContent(pendingPath),
      ["actions", 0, "params", "pace"],
      result,
    ),
  ).toBe(true);
});
it.each([
  "/actions/0/params/profile",
  "/actions/0/params/note",
  "/actions/0/name",
  "/actions/1/params/value",
])("独立字段或其他作用域 pending 不阻塞局部组 %s", (pendingPath) => {
  const result = groupCatalog();
  expect(
    parameterChoiceBlocked(
      pendingContent(pendingPath),
      ["actions", 0, "params", "pace"],
      result,
    ),
  ).toBe(false);
});
it("关联组 pending 不阻塞独立字段，但自身 pending 仍阻塞", () => {
  const result = groupCatalog();
  expect(
    parameterChoiceBlocked(
      pendingContent("/actions/0/params/pace"),
      ["actions", 0, "params", "profile"],
      result,
    ),
  ).toBe(false);
  expect(
    parameterChoiceBlocked(
      pendingContent("/actions/0/params/profile/mode"),
      ["actions", 0, "params", "profile"],
      result,
    ),
  ).toBe(true);
});
it("不可枚举组仍按成员范围判断 pending，不使用组外旧值", () => {
  const p = grouped();
  (p.schema.properties as Record<string, unknown>).pace = { type: "number" };
  const result = groupCatalog(p);
  expect(
    parameterChoiceBlocked(
      pendingContent("/actions/0/params/span"),
      ["actions", 0, "params", "pace"],
      result,
    ),
  ).toBe(true);
  expect(
    parameterChoiceBlocked(
      pendingContent("/actions/0/params/profile"),
      ["actions", 0, "params", "pace"],
      result,
    ),
  ).toBe(false);
});
it.each([
  { properties: { pace: { type: 3 } } },
  { properties: { pace: { $ref: "#/$defs/missing" } } },
])("规则错误不能因无法生成完整目录而变成表单回退 %#", (change) => {
  const p = grouped();
  Object.assign(p.schema.properties as object, change.properties);
  expect(() => parameterOptions(p)).toThrow();
});

it("titled scalar constants preserve exact machine values", () => {
  const p = independent();
  (p.schema.properties as any).bitrate_mode = parseJson(
    '{"type":"number","oneOf":[{"const":0.000667,"title":"短档"},{"const":0.000333,"title":"短档"},{"const":0.00025}]}',
  );
  const catalog = parameterOptions(p);
  expect(catalog.kind).toBe("independent");
  const values = compatibleValues(catalog, {}, "bitrate_mode");
  expect(stringifyJson(values)).toBe("[0.000667,0.000333,0.00025]");
  expect(sameValueAt(values, 0, parseJson("[0.0006670]") as unknown[], 0)).toBe(
    true,
  );
});
it.each([
  '{"type":"number","oneOf":[{"const":1},{"const":1.0}]}',
  '{"type":"number","oneOf":[{"const":1},{"const":"2"}]}',
  '{"type":"string","oneOf":[{"const":"a"},{"const":"b","not":{}}]}',
  '{"type":"number","oneOf":[{"const":1.0000000000000001},{"const":2}]}',
])("命名选项无法准确解释时保留该字段 JSON %s", (text) => {
  const p = independent();
  (p.schema.properties as any).bitrate_mode = parseJson(text);
  const catalog = parameterOptions(p);
  expect(catalog.kind).toBe("independent");
  if (catalog.kind !== "unavailable")
    expect(catalog.fields.some((f) => f.name === "bitrate_mode")).toBe(false);
});
it.each([4096, 4097])("命名标量选项遵守单字段候选上限 %s", (size) => {
  const p = independent();
  (p.schema.properties as any).bitrate_mode = {
    type: "string",
    oneOf: Array.from({ length: size }, (_, i) => ({
      const: String(i),
      title: `档位 ${i}`,
    })),
  };
  const catalog = parameterOptions(p);
  expect(catalog.kind).toBe("independent");
  if (catalog.kind !== "unavailable")
    expect(
      catalog.fields.find((f) => f.name === "bitrate_mode")?.values.length,
    ).toBe(size === 4096 ? 4096 : undefined);
  expect(
    createValidator().compile(p.schema)({
      type: "example_record",
      bitrate_mode: String(size - 1),
      duration_s: 1,
    }),
  ).toBe(true);
});
it("本地引用中的命名常量参与完整目录并保留数字上下文", () => {
  const p = parameter();
  p.schema = parseJson(
    '{"type":"object","additionalProperties":false,"$defs":{"choice":{"type":"number","oneOf":[{"const":0.000667,"title":"短档"},{"const":0.00025,"title":"更短"}]}},"properties":{"value":{"$ref":"#/$defs/choice"}},"required":["value"]}',
  ) as any;
  const catalog = parameterOptions(p);
  expect(catalog.kind).toBe("finite");
  expect(stringifyJson(compatibleValues(catalog, {}, "value"))).toBe(
    "[0.000667,0.00025]",
  );
});

it("修改选项标题只影响展示，合法性与机器值保持", async () => {
  const { scalarChoices } = await import("../../src/web/scalar-options");
  const first = parseJson(
    '{"type":"number","oneOf":[{"const":0.000667,"title":"原档位"}]}',
  ) as Record<string, unknown>;
  const second = parseJson(
    '{"type":"number","oneOf":[{"const":0.000667,"title":"新档位"}]}',
  ) as Record<string, unknown>;
  expect(scalarChoices(first)?.[0].label).toBe("原档位");
  expect(scalarChoices(second)?.[0].label).toBe("新档位");
  expect(createValidator().compile(first)(0.000667)).toBe(true);
  expect(createValidator().compile(second)(0.000667)).toBe(true);
  expect(createValidator().compile(second)(0.000666)).toBe(false);
  expect(stringifyJson(first)).toContain('"const":0.000667');
});

it.each([4096, 4097])(
  "整节点本地引用的 %s 个命名常量保持候选上限与完整校验",
  (size) => {
    const p = independent();
    p.schema = {
      type: "object",
      additionalProperties: false,
      $defs: {
        choice: {
          type: "string",
          oneOf: Array.from({ length: size }, (_, index) => ({
            const: String(index),
            title: `档位 ${index}`,
          })),
        },
      },
      properties: { value: { $ref: "#/$defs/choice" } },
      required: ["value"],
    };
    const catalog = parameterOptions(p);
    expect(catalog.kind).toBe(size === 4096 ? "finite" : "unavailable");
    if (catalog.kind === "unavailable") expect(catalog.reason).toBe("limit");
    else if (catalog.kind === "finite") expect(catalog.rows).toHaveLength(4096);
    expect(
      createValidator().compile(p.schema)({ value: String(size - 1) }),
    ).toBe(true);
  },
);
