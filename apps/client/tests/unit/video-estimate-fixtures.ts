import { loadCapabilities } from "../../src/shared/capabilities";
import type { VideoSizeEstimateDefinition } from "../../src/shared/types";

export function capabilityDocument(
  estimate?: VideoSizeEstimateDefinition,
  schema: Record<string, unknown> = {},
  actionType = "camera_record",
) {
  return {
    devices: [
      {
        device_id: "cam-1",
        driver_id: "demo",
        actions: [
          {
            type: actionType,
            parameter_types: [
              {
                type: "record",
                name: "演示录像",
                description: "虚构参数类型",
                preview_supported: false,
                ...(estimate === undefined
                  ? {}
                  : { video_size_estimate: estimate }),
                schema: {
                  $schema: "https://json-schema.org/draft/2020-12/schema",
                  type: "object",
                  properties: {
                    type: { const: "record" },
                    duration_s: { type: "number", exclusiveMinimum: 0 },
                    bitrate_mode: {
                      type: "string",
                      enum: ["standard", "high"],
                    },
                  },
                  required: ["type"],
                  additionalProperties: false,
                  ...schema,
                },
              },
            ],
          },
        ],
      },
    ],
  };
}
export function capsFor(
  estimate?: VideoSizeEstimateDefinition,
  schema: Record<string, unknown> = {},
  actionType = "camera_record",
) {
  return loadCapabilities(capabilityDocument(estimate, schema, actionType));
}
export const record = (params: Record<string, unknown> = {}) => ({
  type: "camera_record",
  device_id: "cam-1",
  params: { type: "record", ...params },
});
