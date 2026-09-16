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
  [undefined, undefined, ["record", "empty"], true],
  ["record", undefined, ["record", "empty"], true],
  ["empty", undefined, ["record", "empty"], false],
  ["missing", undefined, ["record", "empty"], false],
  [undefined, "camera_record", ["record"], true],
  ["empty", "camera_record", ["record"], false],
  ["empty", "report_status", ["record", "empty"], true],
] as const)(
  "设备 %s 与动作 %s 的候选遵守能力归属",
  (device, action, devices, recording) => {
    const result = deviceOptions(capabilities, device, action);
    expect(result.devices.map((d) => d.device_id)).toEqual(devices);
    expect(result.actions.includes("camera_record")).toBe(recording);
    expect(result.actions).toEqual(
      recording
        ? [
            "camera_record",
            "obtain_action_outputs",
            "delete_action_outputs",
            "cancel_task",
            "report_status",
          ]
        : [
            "obtain_action_outputs",
            "delete_action_outputs",
            "cancel_task",
            "report_status",
          ],
    );
  },
);
it.each([null, { devices: [] }, { devices: [capabilities.devices[1]] }])(
  "无可用拍摄能力时仍提供内置动作",
  (value) => {
    const result = deviceOptions(value, undefined, undefined);
    expect(result.actions).toEqual([
      "obtain_action_outputs",
      "delete_action_outputs",
      "cancel_task",
      "report_status",
    ]);
  },
);
