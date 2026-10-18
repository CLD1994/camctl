import { readFileSync } from "node:fs";
import { isAbsolute } from "node:path";
import type { Capabilities } from "../../src/shared/types";

const componentFixture: Capabilities = {
  devices: [
    {
      device_id: "metadata-test-device",
      driver_id: "metadata-fixture",
      actions: [
        {
          type: "camera_record",
          parameter_types: [
            {
              type: "metadata-test-record",
              name: "合成任务",
              description: "组件测试用的合成说明，只验证能力文本来源。",
              preview_supported: false,
              schema: {
                $schema: "https://json-schema.org/draft/2020-12/schema",
                type: "object",
                additionalProperties: false,
                required: ["type", "duration_s", "quality", "exposure"],
                properties: {
                  type: { const: "metadata-test-record" },
                  duration_s: {
                    type: "integer",
                    minimum: 1,
                    maximum: 3600,
                    title: "合成时长",
                    description: "组件测试用的时长字段。",
                  },
                  quality: {
                    enum: ["low", "high"],
                    title: "合成画质",
                    description: "组件测试用的顶层枚举。",
                  },
                  exposure: {
                    type: "object",
                    title: "合成曝光",
                    description: "组件测试用的嵌套分支。",
                    oneOf: [
                      {
                        type: "object",
                        properties: {
                          mode: {
                            const: "fixed",
                            title: "合成模式",
                            description: "组件测试用的固定分支。",
                          },
                          level: {
                            enum: [1, 2],
                            title: "合成级别",
                            description: "组件测试用的嵌套枚举。",
                          },
                        },
                        required: ["mode", "level"],
                        additionalProperties: false,
                      },
                      {
                        type: "object",
                        properties: {
                          mode: {
                            const: "adaptive",
                            title: "合成模式",
                            description: "组件测试用的自适应分支。",
                          },
                          offset: {
                            enum: [-1, 0, 1],
                            title: "合成偏移",
                            description: "组件测试用的另一个嵌套枚举。",
                          },
                        },
                        required: ["mode", "offset"],
                        additionalProperties: false,
                      },
                    ],
                  },
                },
              },
            },
          ],
        },
      ],
    },
  ],
};

componentFixture.devices.push({
  device_id: "mode-only-test-device",
  driver_id: "mode-only-fixture",
  actions: [
    {
      type: "camera_record",
      parameter_types: [
        {
          type: "mode-only-record",
          name: "仅选择曝光模式的合成任务",
          description: "固定曝光数值由驱动处理。",
          preview_supported: false,
          schema: {
            $schema: "https://json-schema.org/draft/2020-12/schema",
            type: "object",
            additionalProperties: false,
            required: ["type", "duration_s", "bitrate", "exposure"],
            properties: {
              type: { const: "mode-only-record" },
              duration_s: {
                type: "integer",
                minimum: 1,
                title: "时长",
                description: "目标秒数。",
              },
              bitrate: {
                enum: ["standard", "high"],
                title: "码率档位",
                description: "选择每个动作的档位。",
              },
              exposure: {
                type: "object",
                title: "曝光设置",
                description: "选择曝光模式。",
                oneOf: ["manual", "auto"].map((mode) => ({
                  type: "object",
                  additionalProperties: false,
                  properties: {
                    mode: {
                      const: mode,
                      title: "曝光模式",
                      description: "固定值见任务说明。",
                    },
                  },
                  required: ["mode"],
                })),
              },
            },
          },
        },
      ],
    },
  ],
});

export function clientTestCapabilitiesText(): string {
  const injectedPath = process.env.CAMCTL_TEST_CAPABILITIES_FILE;
  if (injectedPath === undefined || injectedPath.length === 0)
    return JSON.stringify(componentFixture);
  if (!isAbsolute(injectedPath))
    throw new Error("CAMCTL_TEST_CAPABILITIES_FILE must be an absolute path");
  return readFileSync(injectedPath, "utf8");
}

export function clientTestCapabilities(): Capabilities {
  return JSON.parse(clientTestCapabilitiesText()) as Capabilities;
}

export function clientTestTasks() {
  const document = clientTestCapabilities();
  return document.devices.flatMap((device) =>
    device.actions.flatMap((action) =>
      action.parameter_types.map((parameter) => ({
        device,
        action,
        parameter,
      })),
    ),
  );
}
