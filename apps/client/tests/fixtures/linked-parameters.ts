import type { ParameterType } from "../../src/shared/types";

/** 小型通用条件与独立对象夹具，不维护设备允许值目录。 */
export function linkedParameter(): ParameterType {
  return {
    type: "linked_capture",
    name: "关联参数测试",
    description: "软件控件验证",
    preview_supported: false,
    schema: {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      type: "object",
      additionalProperties: false,
      properties: {
        type: { const: "linked_capture" },
        interval_s: { title: "拍摄间隔", enum: [1, 2, 3] },
        duration_s: { title: "采集时长", enum: [2, 3, 4] },
        exposure: {
          oneOf: [
            {
              type: "object",
              additionalProperties: false,
              properties: {
                mode: { const: "manual" },
                iso: { enum: [100, 800] },
              },
              required: ["mode", "iso"],
            },
            {
              type: "object",
              additionalProperties: false,
              properties: { mode: { const: "auto" } },
              required: ["mode"],
            },
          ],
        },
        output: { enum: ["video", "raw"] },
      },
      required: ["type", "interval_s", "duration_s", "exposure"],
      allOf: [
        {
          if: {
            properties: { duration_s: { const: 2 } },
            required: ["duration_s"],
          },
          then: { properties: { interval_s: { exclusiveMaximum: 2 } } },
        },
        {
          if: {
            properties: { duration_s: { const: 3 } },
            required: ["duration_s"],
          },
          then: { properties: { interval_s: { exclusiveMaximum: 3 } } },
        },
      ],
    },
  };
}
