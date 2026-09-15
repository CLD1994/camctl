import { expect, it } from "vitest";
import {
  editValue,
  removeAction,
  resolveField,
  parseDraft,
  localToUtc,
  recordsFor,
  valueAt,
} from "../../src/web/editing";
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
