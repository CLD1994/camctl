import { expect, it } from "vitest";
import {
  parameterOptions,
  compatibleValues,
  optionLabel,
  parameterRequired,
  parameterChoiceBlocked,
  sameValueAt,
} from "../../src/web/parameter-options";
import { DIALECT } from "../../src/shared/validation";
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
  "不能准确验证的候选数学事实 %s 保守使用 JSON",
  (token) => {
    const p = independent();
    (p.schema.properties as any).bitrate_mode = parseJson(
      `{"enum":[${token}]}`,
    );
    expect(parameterOptions(p)).toEqual({
      kind: "unavailable",
      reason: "numeric",
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
