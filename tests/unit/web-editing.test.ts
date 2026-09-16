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
import type { DraftContent, ExportedRequest } from "../../src/server/models";
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
      plan_seq: 2,
      created_at: "2026-01-01 00:00:00",
      status: "completed",
    },
  ]);
  expect(records.map((r) => [r.id, !!r.request, !!r.plan])).toEqual([
    ["r1", true, false],
    ["r2", false, true],
  ]);
});
