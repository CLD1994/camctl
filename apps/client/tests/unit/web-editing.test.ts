import { expect, it } from "vitest";
import {
  editValue,
  removeAction,
  resolveField,
  parseDraft,
  localToUtc,
  recordsFor,
  valueAt,
  setValue,
  editPlanText,
  appendDraftAction,
} from "../../src/web/editing";
import { switchActionType } from "../../src/web/action-drafts";
import * as editing from "../../src/web/editing";
import type { DraftContent, ExportedRequest } from "../../src/server/models";
import {
  stringifyJson,
  originalNumberToken,
  parseClientJson,
} from "../../src/shared/json";
const content = (): DraftContent => ({
  text: JSON.stringify({
    name: "拍摄",
    actions: [
      {
        name: "A",
        params: { type: "demo", count: 3, extra: { nested: [false, null] } },
      },
      { name: "B", params: { type: "fixed" } },
    ],
  }),
});
const paramsContent = (params: unknown): DraftContent => ({
  text: JSON.stringify({ name: "计划", actions: [{ name: "操作", params }] }),
});
const paramsPath = ["actions", 0, "params"];
it.each([
  ["action_name", { action_name: "" }],
  ["group", { group: "" }],
  ["action_instance_id", { action_instance_id: "" }],
  ["plan_group", { plan_instance_id: "", group: "" }],
  ["current_plan", { current_plan: true }],
  ["plan_instance_id", { plan_instance_id: "" }],
])("用户明确选择取回来源 %s 生成准确引用", (mode, want) => {
  const next = editing.changeBuiltinMode(
    paramsContent({
      source: { action_instance_id: "7" },
      output_ids: ["4"],
      purpose: "auto_preview",
      unknown: 2,
    }),
    paramsPath,
    "obtain_action_outputs",
    mode as any,
  );
  expect(parseDraft(next).actions[0].params).toEqual({
    source: want,
    purpose: "auto_preview",
    unknown: 2,
    ...(mode === "action_instance_id" ? { output_ids: ["4"] } : {}),
  });
});
it.each([
  ["output_ids", { output_ids: [] }],
  ["action_name", { source: { action_name: "" } }],
  ["action_instance_id", { source: { action_instance_id: "" } }],
  ["current_plan", { source: { current_plan: true } }],
  ["plan_instance_id", { source: { plan_instance_id: "" } }],
])("用户明确选择清理方式 %s 替换互斥选择并保留额外字段", (mode, want) => {
  const next = editing.changeBuiltinMode(
    paramsContent({
      source: { current_plan: false },
      output_ids: ["4"],
      unknown: 2,
    }),
    paramsPath,
    "delete_action_outputs",
    mode as any,
  );
  expect(parseDraft(next).actions[0].params).toEqual({ ...want, unknown: 2 });
});
it.each([
  "/actions/0",
  "/actions/0/params",
  "/actions/0/params/source/action_name",
])("范围转换不覆盖未完成路径 %s", (path) => {
  const initial = {
    ...paramsContent({ source: { action_name: "A" } }),
    pending: { [path]: { kind: "json" as const, text: "{" } },
  };
  expect(() =>
    editing.changeBuiltinMode(
      initial,
      paramsPath,
      "obtain_action_outputs",
      "current_plan",
    ),
  ).toThrow();
  expect(initial.pending[path].text).toBe("{");
});
it("明确精确和预览转换才移除互斥字段，保留用途和词元资料", () => {
  const initial: DraftContent = {
    text: '{"name":"计划","actions":[{"name":"操作","params":{"source":{"action_instance_id":"7"},"filter":"preview","purpose":"manual","unknown":1.0000000000000001}}]}',
    actionVariants: {
      "0": [
        {
          type: "other",
          fields: { params: { n: 1 } },
          fieldsText: '{"params":{"n":1.0000000000000001}}',
          pending: {},
        },
      ],
    },
  };
  const exact = editing.changeObtainSelection(initial, paramsPath, "exact");
  expect(parseDraft(exact).actions[0].params).toMatchObject({
    output_ids: [],
    purpose: "manual",
  });
  expect(parseDraft(exact).actions[0].params).not.toHaveProperty("filter");
  const preview = editing.changeObtainSelection(exact, paramsPath, "preview");
  expect(parseDraft(preview).actions[0].params).toMatchObject({
    filter: "preview",
    purpose: "manual",
  });
  expect(parseDraft(preview).actions[0].params).not.toHaveProperty(
    "output_ids",
  );
  expect(preview.text).toContain("1.0000000000000001");
  expect(preview.actionVariants).toEqual(initial.actionVariants);
});
it("类型切换隔离字段并恢复未完成输入，共用名称和时间", () => {
  const initial: DraftContent = {
    text: JSON.stringify({
      name: "计划",
      actions: [
        {
          name: "A",
          type: "camera_record",
          scheduled_at: "2026-09-16 04:00:00",
          device_id: "cam",
          group: "G",
          params: { type: "fixed" },
          policy: { max_delay_ms: 0 },
          extra: null,
        },
      ],
    }),
    pending: {
      "/actions/0/policy/max_delay_ms": { kind: "number", text: "1e" },
    },
  };
  let next = switchActionType(initial, 0, "report_status");
  expect(parseDraft(next).actions[0]).toEqual({
    name: "A",
    type: "report_status",
    scheduled_at: "2026-09-16 04:00:00",
  });
  expect(next.pending).toEqual({});
  next = setValue(next, ["actions", 0, "name"], "改名");
  next = setValue(next, ["actions", 0, "params"], { scope: "full" });
  next = switchActionType(next, 0, "camera_record");
  expect(parseDraft(next).actions[0]).toEqual({
    ...parseDraft(initial).actions[0],
    name: "改名",
  });
  expect(next.pending).toEqual(initial.pending);
  expect(
    parseDraft(switchActionType(next, 0, "report_status")).actions[0].params,
  ).toEqual({ scope: "full" });
});
it.each([undefined, "future", null, 42, { kind: "unknown" }])(
  "保留未选择或未知类型 %j 的独立输入",
  (type) => {
    const initial: DraftContent = {
      text: JSON.stringify({
        name: "P",
        actions: [
          { name: "A", ...(type === undefined ? {} : { type }), params: false },
        ],
      }),
    };
    const next = switchActionType(initial, 0, "report_status");
    expect(parseDraft(switchActionType(next, 0, type)).actions).toEqual(
      parseDraft(initial).actions,
    );
  },
);
it("选择当前类型不修改草稿", () => {
  const initial = content();
  expect(switchActionType(initial, 0, undefined)).toEqual(initial);
});
it("整个动作未完成时拒绝切换而不丢弃原文", () => {
  const initial = editValue(content(), ["actions", 0], "{", "json");
  expect(() => switchActionType(initial, 0, "report_status")).toThrow(
    /整个动作/,
  );
  expect(initial.pending?.["/actions/0"]?.text).toBe("{");
});
it("删除前项和追加新动作不使类型内容串位", () => {
  let next = switchActionType(content(), 1, "report_status");
  next = removeAction(next, 0);
  next = appendDraftAction(next, { name: "新动作" });
  expect(parseDraft(switchActionType(next, 0, undefined)).actions[0]).toEqual({
    name: "B",
    params: { type: "fixed" },
  });
  expect(
    parseDraft(switchActionType(next, 1, "report_status")).actions[1],
  ).toEqual({ name: "新动作", type: "report_status" });
});
it("整份 JSON 替换必须明确允许清除其他类型内容", () => {
  const initial = switchActionType(content(), 0, "report_status");
  expect(() => editPlanText(initial, '{"name":"替换","actions":[]}')).toThrow();
  expect(editPlanText(initial, '{"name":"替换","actions":[]}', true)).toEqual({
    text: '{"name":"替换","actions":[]}',
    pending: {},
  });
});
it("未完成数字保留原始文本并阻止旧值冒充当前输入", () => {
  const next = editValue(
    content(),
    ["actions", 0, "params", "count"],
    "1e",
    "number",
  );
  expect(next.pending?.["/actions/0/params/count"]).toEqual({
    kind: "number",
    text: "1e",
  });
  expect(parseDraft(next).actions[0].params.count).toBe(3);
});
it.each([
  ["0", 0],
  ["false", false],
  ["null", null],
  ['""', ""],
])("JSON值%s保留原类型", (input, want) => {
  expect(
    parseDraft(
      editValue(content(), ["actions", 0, "params", "count"], input, "json"),
    ).actions[0].params.count,
  ).toEqual(want);
});
it("参数重复键保留待修正文本", () => {
  const next = editValue(
    content(),
    ["actions", 0, "params"],
    '{"type":"a","type":"b"}',
    "json",
  );
  expect(next.pending?.["/actions/0/params"]?.text).toContain('"a"');
});
it("修改字段保留无法用普通控件表达的其他字段", () => {
  const next = editValue(
    content(),
    ["actions", 0, "params", "count"],
    "4",
    "number",
  );
  expect(parseDraft(next).actions[0].params).toEqual({
    type: "demo",
    count: 4,
    extra: { nested: [false, null] },
  });
});
it("删除动作后待完成输入仍属于原来的后续动作", () => {
  const c = editValue(content(), ["actions", 1, "params"], "{", "json");
  const next = removeAction(c, 0);
  expect(next.pending).toEqual({
    "/actions/0/params": { kind: "json", text: "{" },
  });
  expect(parseDraft(next).actions[0].name).toBe("B");
});
it("修正未完成控件后参数JSON修改type保持真实类型", () => {
  const c = editValue(
    content(),
    ["actions", 0, "params", "count"],
    "-",
    "number",
  );
  const next = editValue(
    editValue(c, ["actions", 0, "params", "count"], "30", "number"),
    ["actions", 0, "params"],
    '{"type":"unknown","count":"30"}',
    "json",
  );
  expect(parseDraft(next).actions[0].params).toEqual({
    type: "unknown",
    count: "30",
  });
  expect(next.pending).toEqual({});
});
it("本地Schema引用保留调用处说明及定义的整数枚举", () => {
  expect(
    resolveField(
      { $ref: "#/$defs/rate", title: "帧率" },
      { $defs: { rate: { type: "integer", enum: [30, 60] } } },
    ),
  ).toEqual({ type: "integer", enum: [30, 60], title: "帧率" });
});
it("未填写时间不生成执行安排", () => expect(localToUtc("")).toBeUndefined());
it("缺省字段只读取JSON对象自身成员", () =>
  expect(valueAt({ params: {} }, ["params", "toString"])).toBeUndefined());
it("同名请求保持独立记录且报告独有计划没有本地原请求", () => {
  const requests: ExportedRequest[] = [
    {
      id: "r1",
      draftId: "d1",
      body: { name: "同名" },
      exportedAt: "2026-01-01 00:00:00",
      handedAt: null,
    },
  ];
  const records = recordsFor(requests, [
    {
      request_id: "r2",
      plan_instance_id: "p2",
      name: "同名",
      created_at: "2026-01-01 00:00:00",
      status: "completed",
    },
  ]);
  expect(records.map((r) => [r.id, !!r.request, !!r.plan])).toEqual([
    ["r1", true, false],
    ["r2", false, true],
  ]);
});

it.each(["1.0000000000000001", "1e-999"])(
  "切走专属直属数值%s保留原事实，fieldsText与投影可以恢复",
  (token) => {
    const initial = {
      text: `{"actions":[{"name":"A","type":"camera_record","params":${token},"extra":${token}}]}`,
    };
    const next = switchActionType(initial, 0, "report_status");
    const saved = next.actionVariants!["0"][0];
    expect(saved.fieldsText).toBe(`{"params":${token},"extra":${token}}`);
    expect(stringifyJson(saved.fields)).toBe(
      `{"params":${token},"extra":${token}}`,
    );
    const restored = switchActionType(next, 0, "camera_record");
    expect(
      originalNumberToken(
        parseDraft(restored).actions[0],
        "params",
        Number(token),
      ),
    ).toBe(token);
    expect(
      originalNumberToken(
        parseDraft(restored).actions[0],
        "extra",
        Number(token),
      ),
    ).toBe(token);
  },
);
it("可靠fieldsText恢复直属原词元，不退回Number投影", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"name":"A","type":"report_status"}]}',
    actionVariants: {
      "0": [
        {
          type: "camera_record",
          fields: { params: 1, extra: 0 },
          fieldsText: '{"params":1.0000000000000001,"extra":1e-999}',
          pending: {},
        },
      ],
    },
  };
  const next = switchActionType(initial, 0, "camera_record"),
    action = parseDraft(next).actions[0];
  expect(originalNumberToken(action, "params", action.params)).toBe(
    "1.0000000000000001",
  );
  expect(originalNumberToken(action, "extra", action.extra)).toBe("1e-999");
});
it("类型切换保持未改共用字段的直属数学事实", () => {
  const next = switchActionType(
    {
      text: '{"actions":[{"name":1e-999,"scheduled_at":1.0000000000000001,"type":"camera_record","params":null}]}',
    },
    0,
    "report_status",
  );
  const action = parseDraft(next).actions[0];
  expect(originalNumberToken(action, "name", action.name)).toBe("1e-999");
  expect(originalNumberToken(action, "scheduled_at", action.scheduled_at)).toBe(
    "1.0000000000000001",
  );
});
it("业务非法直属type的停用记录保存其原数字事实", () => {
  const next = switchActionType(
    { text: '{"actions":[{"name":"A","type":1e-999,"params":false}]}' },
    0,
    "report_status",
  );
  const saved = next.actionVariants!["0"][0];
  expect(originalNumberToken(saved, "type", saved.type)).toBe("1e-999");
  expect(stringifyJson(saved)).toContain('"type":1e-999');
});
it("嵌套数值与专属pending切走切回保持原事实", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"name":"A","type":"camera_record","params":{"value":1.0000000000000001,"under":[1e-999]}}]}',
    pending: {
      "/actions/0/params/value": { kind: "number", text: "1e" },
      "/actions/0/name": { kind: "json", text: "{" },
    },
  };
  const switched = switchActionType(initial, 0, "report_status");
  expect(switched.pending).toEqual({
    "/actions/0/name": initial.pending!["/actions/0/name"],
  });
  const next = switchActionType(switched, 0, "camera_record");
  expect(next.pending).toEqual(initial.pending);
  expect(next.text).toContain("1.0000000000000001");
  expect(next.text).toContain("1e-999");
});
it.each([
  {},
  { params: null },
  { params: false },
  { params: 0 },
  { params: [0, false, null] },
  { params: { extra: null } },
])("旧资料没有原文按保存的存在性和JSON类型恢复：%j", (fields) => {
  const initial: DraftContent = {
    text: '{"actions":[{"name":"A","type":"report_status"}]}',
    actionVariants: { "0": [{ type: "camera_record", fields, pending: {} }] },
  };
  expect(
    parseDraft(switchActionType(initial, 0, "camera_record")).actions[0],
  ).toEqual({ name: "A", type: "camera_record", ...fields });
});
it.each([
  ["1.0000000000000001", "1"],
  ["1e-999", "0"],
])("显式替换同Number投影%s→%s，不重新附旧词元", (old, replacement) => {
  const initial = {
    text: `{"actions":[{"name":"A","type":"camera_record","params":${old}}]}`,
  };
  let next = editValue(initial, ["actions", 0, "params"], replacement, "json");
  next = switchActionType(
    switchActionType(next, 0, "report_status"),
    0,
    "camera_record",
  );
  expect(next.text).not.toContain(old);
  expect(parseDraft(next).actions[0].params).toBe(Number(replacement));
});
it.each(["", "/actions", "/actions/0"])(
  "祖先或整个动作pending %s拒绝且不修改输入",
  (path) => {
    const initial: DraftContent = {
      text: '{"actions":[{"type":"camera_record","params":1e-999}]}',
      pending: { [path]: { kind: "json", text: "{" } },
    };
    const before = stringifyJson(initial);
    expect(() => switchActionType(initial, 0, "report_status")).toThrow();
    expect(stringifyJson(initial)).toBe(before);
  },
);
it.each(["{", "[]", "null"])(
  "停用fieldsText %s无法形成字段对象时拒绝恢复而不改原资料",
  (fieldsText) => {
    const initial: DraftContent = {
      text: '{"actions":[{"type":"report_status"}]}',
      actionVariants: {
        "0": [
          {
            type: "camera_record",
            fields: { params: 1 },
            fieldsText,
            pending: {},
          },
        ],
      },
    };
    const before = stringifyJson(initial);
    expect(() => switchActionType(initial, 0, "camera_record")).toThrow();
    expect(stringifyJson(initial)).toBe(before);
  },
);
it.each([
  ["1e-999", 0],
  ["1.0000000000000001", 1],
])("type旧词元%s仅与裸值%s投影相同，不属于当前类型noop", (token, target) => {
  const initial = {
    text: `{"actions":[{"name":"A","type":${token},"params":false}]}`,
  };
  const next = switchActionType(initial, 0, target);
  expect(next).not.toBe(initial);
  expect(parseDraft(next).actions[0]).toEqual({ name: "A", type: target });
  expect(stringifyJson(next.actionVariants!["0"][0])).toContain(
    `"type":${token}`,
  );
});
it.each([
  ["1e-999", 0],
  ["1.0000000000000001", 1],
])("裸目标%s不能恢复或删除仅同投影的type%s候选", (token, target) => {
  const initial: DraftContent = {
    text: '{"actions":[{"name":"A","type":"report_status","params":{"scope":"full"}}]}',
    actionVariants: parseClientJson(
      `{"0":[{"type":${token},"fields":{"params":false},"pending":{"/params":{"kind":"json","text":"{"}}}]}`,
    ) as DraftContent["actionVariants"],
  };
  const next = switchActionType(initial, 0, target);
  expect(parseDraft(next).actions[0]).toEqual({ name: "A", type: target });
  expect(next.pending).toEqual({});
  expect(next.actionVariants!["0"]).toHaveLength(2);
  expect(stringifyJson(next.actionVariants!["0"][0])).toContain(
    `"type":${token}`,
  );
});
it("裸0恢复真正零候选时保留另一个非零下溢type候选", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"type":"report_status"}]}',
    actionVariants: parseClientJson(
      '{"0":[{"type":1e-999,"fields":{"params":"under"},"pending":{}},{"type":0,"fields":{"params":"zero"},"pending":{}}]}',
    ) as DraftContent["actionVariants"],
  };
  const next = switchActionType(initial, 0, 0);
  expect(parseDraft(next).actions[0]).toEqual({ type: 0, params: "zero" });
  expect(next.actionVariants!["0"]).toHaveLength(1);
  expect(stringifyJson(next.actionVariants!["0"][0])).toContain(
    '"type":1e-999',
  );
});
it("替换旧当前类型的候选时不能清掉仅同Number投影的另一type", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"type":1e-999,"params":"current"}]}',
    actionVariants: {
      "0": [{ type: 0, fields: { params: "zero" }, pending: {} }],
    },
  };
  const next = switchActionType(initial, 0, "report_status");
  expect(next.actionVariants!["0"]).toHaveLength(2);
  expect(next.actionVariants!["0"][0].fields.params).toBe("zero");
  expect(stringifyJson(next.actionVariants!["0"][1])).toContain(
    '"type":1e-999',
  );
});
it.each(["1", "1.0", "1e0"])(
  "type数学等价%s和裸1属于当前类型，原文保持",
  (token) => {
    const initial = { text: `{"actions":[{"type":${token},"params":false}]}` };
    expect(switchActionType(initial, 0, 1)).toBe(initial);
  },
);
it("数学等价的唯一候选恢复fields，但裸type不贴候选词元", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"type":"report_status"}]}',
    actionVariants: parseClientJson(
      '{"0":[{"type":1.0,"fields":{"params":false},"pending":{}}]}',
    ) as DraftContent["actionVariants"],
  };
  const next = switchActionType(initial, 0, 1);
  expect(parseDraft(next).actions[0]).toEqual({ type: 1, params: false });
  expect(next.text).not.toContain("1.0");
});
it("多个真正数学等价候选在恢复前拒绝，完整保留内容", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"type":"report_status","params":{"scope":"full"}}]}',
    actionVariants: parseClientJson(
      '{"0":[{"type":1,"fields":{"params":false},"pending":{}},{"type":1.0,"fields":{"params":true},"pending":{}}]}',
    ) as DraftContent["actionVariants"],
  };
  const before = stringifyJson(initial);
  expect(() => switchActionType(initial, 0, 1)).toThrow();
  expect(stringifyJson(initial)).toBe(before);
});
it("legacy type保存的0按实际事实恢复，不猜下溢原文", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"type":"report_status"}]}',
    actionVariants: {
      "0": [{ type: 0, fields: { params: false }, pending: {} }],
    },
  };
  const next = switchActionType(initial, 0, 0);
  expect(parseDraft(next).actions[0]).toEqual({ type: 0, params: false });
  expect(next.text).not.toContain("1e-999");
});
it("JSON类型对象内嵌数值也不能按Number投影折叠类型事实", () => {
  const initial = {
    text: '{"actions":[{"type":{"future":[1e-999]},"params":false}]}',
  };
  const next = switchActionType(initial, 0, { future: [0] });
  expect(parseDraft(next).actions[0]).toEqual({ type: { future: [0] } });
  expect(stringifyJson(next.actionVariants!["0"][0])).toContain("1e-999");
});
it.each(["1e-999", "1.0000000000000001"])(
  "没有专属内容时选择另一type替换%s，不记录空类型历史",
  (token) => {
    const next = switchActionType(
      { text: `{"actions":[{"name":"A","type":${token}}]}` },
      0,
      "report_status",
    );
    expect(parseDraft(next).actions[0]).toEqual({
      name: "A",
      type: "report_status",
    });
    expect(next.actionVariants).toEqual({});
  },
);
it("只有专属pending时仍保存旧type的原数字事实", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"type":1e-999}]}',
    pending: { "/actions/0/type": { kind: "json", text: "{" } },
  };
  const next = switchActionType(initial, 0, "report_status"),
    saved = next.actionVariants!["0"][0];
  expect(saved.fields).toEqual({});
  expect(saved.pending).toEqual({ "/type": { kind: "json", text: "{" } });
  expect(stringifyJson(saved)).toContain('"type":1e-999');
});
it("当前类型事实匹配时以当前正文为准，不恢复重复停用候选", () => {
  const initial: DraftContent = {
    text: '{"actions":[{"type":1.0,"params":"current"}]}',
    actionVariants: {
      "0": [
        { type: 1, fields: { params: false }, pending: {} },
        { type: 1, fields: { params: true }, pending: {} },
      ],
    },
  };
  expect(switchActionType(initial, 0, 1)).toBe(initial);
});
