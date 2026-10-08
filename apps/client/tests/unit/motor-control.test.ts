import { expect, it } from "vitest";
import { validatePlan } from "../../src/shared/plan";
import { validateBuiltinParams } from "../../src/shared/action-params";
import { deviceOptions } from "../../src/web/device-options";
import { editValue, parseDraft } from "../../src/web/editing";
import { resultNotes, actionIssueText } from "../../src/web/result-model";
import type { ReportAction } from "../../src/shared/types";
import { parseJson } from "../../src/shared/json";
it.each(["1.0000000000000000001", "1e-999", "9007199254740991.0000001"])(
  "电机延迟表单和策略 JSON 不舍入 %s",
  (token) => {
    const content = { text: JSON.stringify(plan()) };
    for (const [path, text, kind] of [
      [["actions", 0, "policy", "max_delay_ms"], token, "number"],
      [["actions", 0, "policy"], `{"max_delay_ms":${token}}`, "json"],
    ] as const) {
      const edited = editValue(content, [...path], text, kind);
      expect(Object.values(edited.pending ?? {}).map((p) => p.text)).toContain(
        text,
      );
    }
  },
);
it.each(["0", "1.0", "1e2", "9007199254740991"])(
  "合法电机延迟 %s 在表单和策略 JSON 中一致",
  (token) => {
    const content = { text: JSON.stringify(plan()) };
    const form = editValue(
      content,
      ["actions", 0, "policy", "max_delay_ms"],
      token,
      "number",
    );
    const json = editValue(
      content,
      ["actions", 0, "policy"],
      `{"max_delay_ms":${token}}`,
      "json",
    );
    expect(parseDraft(form)).toEqual(parseDraft(json));
    expect(validatePlan(parseDraft(form), null)).toEqual([]);
  },
);

const action = () => ({
  name: "定位",
  type: "motor_control",
  scheduled_at: "2026-10-08 12:00:00",
  params: { position: 0 },
  policy: { max_delay_ms: 0 },
});
const plan = (a: object = action()) => ({
  request_id: "1",
  name: "电机",
  created_at: "2026-10-08 11:00:00",
  actions: [a],
});
it("没有相机能力时仍允许编辑电机动作且不选择设备", () => {
  expect(deviceOptions(null, "motor_control")).toMatchObject({
    requiresDevice: false,
    devices: [],
  });
  expect(deviceOptions(null, "motor_control").actions).toContain(
    "motor_control",
  );
});
it.each([-2147483648, -1, 0, 2147483647])(
  "位置 %s 按公共表示范围校验，无需相机能力",
  (position) => {
    expect(
      validatePlan(plan({ ...action(), params: { position } }), null),
    ).toEqual([]);
  },
);
it.each([
  {},
  { position: null },
  { position: true },
  { position: "0" },
  { position: 0.5 },
  { position: -2147483649 },
  { position: 2147483648 },
  { position: 0, type: "fixed" },
])("非法位置参数 %j 不可导出", (params) => {
  expect(
    validateBuiltinParams("motor_control" as never, params, true),
  ).not.toEqual([]);
});
it.each(["scheduled_at", "policy", "params"])(
  "电机缺少 %s 时拒绝导出",
  (key) => {
    const a: Record<string, unknown> = action();
    delete a[key];
    expect(
      validatePlan(plan(a), null).some((i) =>
        i.path.startsWith(`actions[0].${key}`),
      ),
    ).toBe(true);
  },
);
it.each([
  {},
  { max_delay_ms: -1 },
  { max_delay_ms: 0.5 },
  { max_delay_ms: "0" },
  { max_delay_ms: 0, retry: 1 },
  null,
])("电机策略 %j 必须符合窗口契约", (policy) => {
  expect(
    validatePlan(plan({ ...action(), policy }), null).some((i) =>
      i.path.startsWith("actions[0].policy"),
    ),
  ).toBe(true);
});
it("电机设备字段即使为空也必须省略", () => {
  expect(
    validatePlan(plan({ ...action(), device_id: "" }), null).some(
      (i) => i.path === "actions[0].device_id",
    ),
  ).toBe(true);
});
it.each(["1.0", "1e2", "-2147483648.0"])(
  "表单位置 %s 与 JSON 使用同一个数学值",
  (text) => {
    const content = { text: JSON.stringify(plan()) };
    const form = editValue(
      content,
      ["actions", 0, "params", "position"],
      text,
      "number",
    );
    const json = editValue(
      content,
      ["actions", 0, "params"],
      `{"position":${text}}`,
      "json",
    );
    expect(parseDraft(form)).toEqual(parseDraft(json));
    expect(validatePlan(parseDraft(form), null)).toEqual([]);
  },
);
it.each(["2147483647.00000001", "1.0000000000000000001", "1e-999"])(
  "位置 %s 不因浮点舍入成为整数",
  (text) => {
    const content = { text: JSON.stringify(plan()) };
    const edited = editValue(
      content,
      ["actions", 0, "params", "position"],
      text,
      "number",
    );
    expect(edited.pending?.["/actions/0/params/position"]?.text).toBe(text);
  },
);
it("电机整数精度保护不改变相机小数参数", () => {
  expect(
    parseJson(
      '{"actions":[{"type":"camera_record","params":{"position":1.0000000000000000001,"duration":0.5}}]}',
    ),
  ).toEqual({
    actions: [
      { type: "camera_record", params: { position: 1, duration: 0.5 } },
    ],
  });
});
it("电机成功摘要只表达控制通知已发送", () => {
  const notes = resultNotes({
    action_instance_id: "1",
    ...action(),
    input_params: { position: 0 },
    status: "succeeded",
  } as unknown as ReportAction);
  expect(notes).toHaveLength(1);
  expect(notes[0].error).toBe(false);
  expect(notes[0].text).toContain("通知");
});
it.each([
  ["motor_channel_unavailable", "通道"],
  ["motor_notification_failed", "未完整"],
  ["motor_notification_unconfirmed", "不确定"],
])("电机错误 %s 展示其独立失败含义", (code, fact) => {
  const a = {
    action_instance_id: "1",
    ...action(),
    status: "failed",
    error: { code, stage: "execution", details: {} },
  } as unknown as ReportAction;
  expect(actionIssueText(a)).toContain(fact);
});
