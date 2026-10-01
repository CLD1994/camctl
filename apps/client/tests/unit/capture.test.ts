import { expect, it } from "vitest";
import { validatePlan } from "../../src/shared/plan";
import {
  validateReport,
  validateReportAgainstHistory,
} from "../../src/domain/reports";
import { deviceOptions } from "../../src/web/device-options";
import type { Capabilities, StatusReport } from "../../src/shared/types";

const capabilities: Capabilities = {
  devices: ["camera_take_photo", "camera_timelapse"].map((type, i) => ({
    device_id: `cam${i}`,
    driver_id: "demo",
    actions: [
      {
        type,
        parameter_types: [
          {
            type: "fixed",
            name: "固定",
            description: "样例",
            preview_supported: false,
            schema: {
              $schema: "https://json-schema.org/draft/2020-12/schema",
              type: "object",
              properties: { type: { const: "fixed" } },
              required: ["type"],
              additionalProperties: false,
            },
          },
        ],
      },
    ],
  })),
};
const action = (type = "camera_take_photo") => ({
  name: "拍摄",
  type,
  device_id: type === "camera_take_photo" ? "cam0" : "cam1",
  scheduled_at: "2026-09-16 00:00:00",
  policy: { max_delay_ms: 0 },
  params: { type: "fixed" },
});
const plan = (actions: unknown[]) => ({
  request_id: "1",
  name: "计划",
  created_at: "2026-09-16 00:00:00",
  actions,
});
const report = (status = "canceled"): unknown => ({
  report_id: "1",
  from_wm: 0,
  to_wm: 10,
  plans: [
    {
      plan_instance_id: "1",
      request_id: "1",
      name: "计划",
      created_at: "2026-09-16 00:00:00",
      status: "completed",
      actions: [
        {
          ...action(),
          params: undefined,
          action_instance_id: "1",
          input_params: { type: "fixed" },
          effective_params: { type: "fixed" },
          status,
          outputs: [
            {
              output_id: "1",
              source_action_instance_id: "1",
              kind: "original",
              media_type: "image/png",
              availability: "available",
              cleanup: { status: "not_requested" },
              checksum: { status: "not_obtained" },
              media: {
                check_status: "not_performed",
                duration: { status: "unknown" },
              },
            },
          ],
        },
      ],
    },
  ],
});

it.each(["camera_take_photo", "camera_timelapse"])(
  "接受设备支持的 %s 及本计划取回",
  (type) => {
    expect(
      validatePlan(
        plan([
          action(type),
          {
            name: "取回",
            type: "obtain_action_outputs",
            scheduled_at: "2026-09-16 01:00:00",
            params: { source: { action_name: "拍摄" } },
          },
        ]),
        capabilities,
      ),
    ).toEqual([]);
  },
);
it("动作选择只列出已部署拍摄能力", () => {
  expect(
    deviceOptions(capabilities, "camera_take_photo").devices.map(
      (d) => d.device_id,
    ),
  ).toEqual(["cam0"]);
  expect(deviceOptions(capabilities, "camera_record").actions).not.toContain(
    "camera_record",
  );
});
it("取消单张拍摄可报告已保留图片", () =>
  expect(() =>
    validateReport(JSON.parse(JSON.stringify(report()))),
  ).not.toThrow());
