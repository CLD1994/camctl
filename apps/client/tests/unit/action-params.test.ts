import { expect, it } from "vitest";
import {
  builtinFields,
  sources,
  validateBuiltinParams,
} from "../../src/shared/action-params";
import notificationSchema from "../../../../protocol/schemas/host-notification.schema.json";

it("电机字段目录包含公共跨文档参数声明", () => {
  expect([...builtinFields.motor_control].sort()).toEqual(
    Object.keys(notificationSchema.$defs.motor_params.properties).sort(),
  );
});
it("电机合法字段与真实额外字段分别按完整协议验证", () => {
  const legal = { position: 0 };
  const extra = { ...legal, unexpected: "保留" };
  expect(validateBuiltinParams("motor_control", legal, true)).toEqual([]);
  expect(validateBuiltinParams("motor_control", extra, true)).toMatchObject([
    { path: "params", code: "invalid_params" },
  ]);
  expect(extra).toEqual({ position: 0, unexpected: "保留" });
});

it("内置字段识别使用公共参数定义的所有分支声明", () => {
  expect([...builtinFields.obtain_action_outputs].sort()).toEqual([
    "filter",
    "output_ids",
    "purpose",
    "source",
  ]);
  expect([...builtinFields.delete_action_outputs].sort()).toEqual([
    "output_ids",
    "source",
  ]);
});
it("取回表单提供六种来源且当前计划的初值是布尔常量", () => {
  expect(sources.map((s) => s.id)).toEqual([
    "action_name",
    "group",
    "action_instance_id",
    "plan_group",
    "current_plan",
    "plan_instance_id",
  ]);
  expect(sources.find((s) => s.id === "current_plan")).toMatchObject({
    constants: { current_plan: true },
    fields: {},
  });
});
it.each([
  { source: { action_name: "拍摄" }, filter: "default", purpose: "manual" },
  {
    source: { action_name: "拍摄" },
    filter: "preview",
    purpose: "auto_preview",
  },
  { source: { action_name: "拍摄" }, output_ids: ["4"] },
])("合法取回字段仍由组合校验判断 %j", (params) => {
  expect(validateBuiltinParams("obtain_action_outputs", params, true)).toEqual(
    [],
  );
});
it("已声明字段的非法组合仍被拒绝", () => {
  expect(
    validateBuiltinParams(
      "obtain_action_outputs",
      {
        source: { action_instance_id: "7" },
        filter: "preview",
        output_ids: ["4"],
      },
      true,
    ),
  ).not.toEqual([]);
});
it("清理允许来源但拒绝未知字段及精确范围混用", () => {
  expect(
    validateBuiltinParams(
      "delete_action_outputs",
      { source: { current_plan: true } },
      true,
    ),
  ).toEqual([]);
  for (const params of [
    { source: { current_plan: true }, output_ids: ["4"] },
    { source: { current_plan: true }, unknown: 1 },
  ])
    expect(
      validateBuiltinParams("delete_action_outputs", params, true),
    ).not.toEqual([]);
});
