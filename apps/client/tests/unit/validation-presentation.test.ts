import { it, expect } from "vitest";
import { presentIssue } from "../../src/web/validation-presentation";
import type { Capabilities } from "../../src/shared/types";

const plan = {
  name: "计划",
  actions: [
    {
      name: "录像 A",
      type: "camera_record",
      device_id: "demo",
      params: { type: "quality" },
    },
    { name: "取回 B" },
  ],
};
const capabilities: Capabilities = {
  devices: [
    {
      device_id: "demo",
      driver_id: "demo",
      actions: [
        {
          type: "camera_record",
          parameter_types: [
            {
              type: "quality",
              name: "画质",
              description: "测试",
              preview_supported: false,
              schema: {
                type: "object",
                properties: {
                  resolution: { title: "分辨率", type: "string" },
                  "a.b": { title: "带点的参数", type: "string" },
                  a: { type: "object" },
                },
              },
            },
          ],
        },
      ],
    },
  ],
};

it.each([
  ["actions[1].scheduled_at", "/actions/1/scheduled_at", "取回 B", "执行时间"],
  [
    "/actions/0/params/resolution",
    "/actions/0/params/resolution",
    "录像 A",
    "分辨率",
  ],
  [
    "actions[0].params.resolution",
    "/actions/0/params/resolution",
    "录像 A",
    "分辨率",
  ],
  ["/actions/0/params/a~1b~0c", "/actions/0/params/a~1b~0c", "录像 A", "a/b~c"],
])("呈现真实动作与字段且保留标准路径：%s", (path, target, action, field) => {
  const result = presentIssue(
    { path, code: "schema_required", message: "technical" },
    plan,
    capabilities,
  );
  expect(result.target).toBe(target);
  expect(result.location).toContain(action);
  expect(result.location).toContain(field);
  expect(result.message).toMatch(/必填/);
});

it.each([
  "actions[0].params.a.b",
  "/actions/0/params/bad~2key",
  "actions[01].name",
  "actions[0].params[bad]",
  "actions[0].params.",
])("有歧义或非法路径不猜测控件：%s", (path) => {
  expect(
    presentIssue(
      { path, code: "schema_type", message: "technical" },
      plan,
      capabilities,
    ).target,
  ).toBeNull();
});

it("JSON Pointer 中带点属性仍可精确定位", () => {
  const result = presentIssue(
    {
      path: "/actions/0/params/a.b",
      code: "schema_type",
      message: "technical",
    },
    plan,
    capabilities,
  );
  expect(result.target).toBe("/actions/0/params/a.b");
  expect(result.location).toContain("带点的参数");
});

it.each(["schema_minItems", "schema_maxLength", "schema_futureKeyword"])(
  "Schema 主要提示不泄露英文诊断：%s",
  (code) => {
    const result = presentIssue(
      { path: "actions", code, message: "must NOT match something" },
      plan,
    );
    expect(result.message).toMatch(/[\u4e00-\u9fff]/);
    expect(result.message).not.toContain("must");
  },
);

it("业务诊断的具体原因保留", () => {
  const result = presentIssue(
    {
      path: "actions[1].params.source.action_name",
      code: "source_not_found",
      message: "本计划内没有唯一的同名产物来源动作",
    },
    plan,
  );
  expect(result.location).toContain("来源动作名称");
  expect(result.message).toContain("没有唯一");
});
