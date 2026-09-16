import { expect, it } from "vitest";
import { deviceOptions } from "../../src/web/device-options";
import type { Capabilities } from "../../src/shared/types";

const capabilities: Capabilities = {
  devices: [
    {
      device_id: "record",
      driver_id: "a",
      actions: [
        {
          type: "camera_record",
          parameter_types: [
            {
              type: "fixed",
              name: "固定任务",
              description: "使用固定拍摄设置",
              schema: {
                $schema: "https://json-schema.org/draft/2020-12/schema",
                type: "object",
                required: ["type"],
                properties: { type: { const: "fixed" } },
                additionalProperties: false,
              },
            },
          ],
        },
      ],
    },
    { device_id: "empty", driver_id: "b", actions: [] },
  ],
};
it.each([
  [undefined, [], false],
  ["", [], false],
  ["unknown", [], false],
  ["report_status", [], false],
  ["cancel_task", [], false],
  ["delete_action_outputs", [], false],
  ["camera_record", ["record"], true],
] as const)("动作 %s 决定设备栏及其候选", (action, devices, requiresDevice) => {
  const result = deviceOptions(capabilities, action);
  expect(result.devices.map((d) => d.device_id)).toEqual(devices);
  expect(result.requiresDevice).toBe(requiresDevice);
  expect(result.actions).toEqual([
    "camera_record",
    "obtain_action_outputs",
    "delete_action_outputs",
    "cancel_task",
    "report_status",
  ]);
});
it.each([null, { devices: [] }, { devices: [capabilities.devices[1]] }])(
  "无可用拍摄能力时仍提供内置动作",
  (value) => {
    const result = deviceOptions(value, undefined);
    expect(result.actions).toEqual([
      "obtain_action_outputs",
      "delete_action_outputs",
      "cancel_task",
      "report_status",
    ]);
  },
);
