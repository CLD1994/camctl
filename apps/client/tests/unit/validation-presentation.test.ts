import { it, expect } from "vitest";
import {
  presentIssue,
  presentPendingInput,
} from "../../src/web/validation-presentation";
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

it("未完成参数使用实际能力字段名称和所属动作", () => {
  const result = presentPendingInput(
    "/actions/0/params/resolution",
    1,
    plan,
    capabilities,
  );
  expect(result).toContain("录像 A");
  expect(result).toContain("分辨率");
  expect(result).not.toContain("/actions/");
});

it.each([
  ["/name", "计划名称"],
  ["/actions", "动作列表"],
  ["/actions/0/policy/max_delay_ms", "最大允许延迟"],
  ["/actions/1/group", "动作组"],
  ["", "计划"],
])("未完成公共字段显示已识别名称：%s", (path, field) => {
  expect(presentPendingInput(path, 1, plan, capabilities)).toContain(field);
});

it.each([
  "bad",
  "/actions/~2bad",
  "actions[0].name",
  "/unknown/name",
  "/actions/99/name",
  "/actions/0/source",
  "/actions/0/max_delay_ms",
  "/actions/0/position",
  "/actions/0/params/unknown",
  "/actions/0/params/a/name",
])("未识别的未完成输入只按序号区分，不猜测归属：%s", (path) => {
  const first = presentPendingInput(path, 1, plan, capabilities);
  const second = presentPendingInput(path, 2, plan, capabilities);
  expect(first).toContain("1");
  expect(second).toContain("2");
  expect(first).not.toBe(second);
  expect(first).not.toContain(path);
  expect(first).not.toContain("录像 A");
});

it("未完成动作缺少可解释正文时不沿用其他动作上下文", () => {
  const result = presentPendingInput(
    "/actions/0/name",
    1,
    undefined,
    capabilities,
  );
  expect(result).not.toContain("动作名称");
  expect(result).toContain("1");
});

it.each([
  { type: "motor_control", path: "position", field: "位置" },
  { type: "report_status", path: "scope", field: "报告范围" },
  {
    type: "obtain_action_outputs",
    path: "source/action_name",
    field: "来源动作名称",
  },
  { type: "cancel_task", path: "target/request_id", field: "请求 ID" },
])("内置动作的未完成参数保留已定义名称：$type", ({ type, path, field }) => {
  const value = { actions: [{ name: "内置动作", type, params: {} }] };
  const result = presentPendingInput(`/actions/0/params/${path}`, 1, value);
  expect(result).toContain("内置动作");
  expect(result).toContain(field);
});

it("引用字段出现在不声明引用的内置动作中时不猜测其含义", () => {
  const result = presentPendingInput(
    "/actions/0/params/source/action_name",
    1,
    {
      actions: [{ name: "同步", type: "report_status", params: {} }],
    },
  );
  expect(result).not.toContain("来源动作名称");
  expect(result).not.toContain("同步");
});

it.each(["camera_record", "camera_timelapse"])(
  "拍摄动作可识别参数类型的未完成输入：%s",
  (type) => {
    const result = presentPendingInput("/actions/0/params/type", 1, {
      actions: [{ name: "当前拍摄", type, params: {} }],
    });
    expect(result).toContain("当前拍摄");
    expect(result).toContain("参数类型");
  },
);

it.each(["motor_control", "obtain_action_outputs", "unknown_type"])(
  "没有参数类型声明的动作不按已知字段名猜测归属：%s",
  (type) => {
    const result = presentPendingInput("/actions/0/params/type", 2, {
      actions: [{ name: "其他动作", type, params: {} }],
    });
    expect(result).toContain("2");
    expect(result).not.toContain("其他动作");
    expect(result).not.toContain("参数类型");
  },
);

it("能力 Schema 对参数类型声明的可阅读名称保留", () => {
  const titled = structuredClone(capabilities);
  titled.devices[0].actions[0].parameter_types[0].schema.properties = {
    ...titled.devices[0].actions[0].parameter_types[0].schema.properties!,
    type: { title: "拍摄设置", const: "quality" },
  };
  const result = presentPendingInput("/actions/0/params/type", 1, plan, titled);
  expect(result).toContain("录像 A");
  expect(result).toContain("拍摄设置");
});

it("未知动作的已定义公共类型字段仍有名称", () => {
  const result = presentPendingInput("/actions/0/type", 1, {
    actions: [{ name: "未知动作", type: "unknown_type", params: {} }],
  });
  expect(result).toContain("未知动作");
  expect(result).toContain("动作类型");
});

it("未完成输入识别收紧不改变原校验展示和机器定位", () => {
  const issue = {
    path: "/actions/0/params/type",
    code: "schema_type",
    message: "technical",
  };
  const result = presentIssue(issue, {
    actions: [{ name: "取回动作", type: "obtain_action_outputs", params: {} }],
  });
  expect(result.target).toBe(issue.path);
  expect(result.location).toContain("取回动作");
  expect(result.location).toContain("参数类型");
});

it.each([
  "camera_record",
  "camera_timelapse",
  "camera_take_photo",
  "motor_control",
])("有策略契约的动作可识别最大延迟输入：%s", (type) => {
  const result = presentPendingInput("/actions/0/policy/max_delay_ms", 1, {
    actions: [{ name: "设备动作", type, policy: {} }],
  });
  expect(result).toContain("设备动作");
  expect(result).toContain("最大允许延迟");
});

it.each(["report_status", "obtain_action_outputs", "unknown_type"])(
  "没有策略字段声明的动作不猜测最大延迟输入：%s",
  (type) => {
    const result = presentPendingInput("/actions/0/policy/max_delay_ms", 2, {
      actions: [{ name: "其他动作", type, policy: {} }],
    });
    expect(result).toContain("2");
    expect(result).not.toContain("其他动作");
    expect(result).not.toContain("最大允许延迟");
  },
);

it("策略字段的未完成输入识别收紧不改变原校验展示", () => {
  const issue = {
    path: "/actions/0/policy/max_delay_ms",
    code: "schema_type",
    message: "technical",
  };
  const result = presentIssue(issue, {
    actions: [{ name: "同步动作", type: "report_status", policy: {} }],
  });
  expect(result.target).toBe(issue.path);
  expect(result.location).toContain("同步动作");
  expect(result.location).toContain("最大允许延迟");
});
