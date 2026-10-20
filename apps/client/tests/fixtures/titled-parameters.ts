import { parseJson } from "../../src/shared/json";
import type { ParameterType } from "../../src/shared/types";
import { DIALECT } from "../../src/shared/validation";

/** 通用控件的有限示例，不维护任何设备的完整参数目录。 */
export function titledParameter(): ParameterType {
  const iso = { type: "integer", title: "ISO 感光度", enum: [64, 100] };
  const aperture = {
    type: "number",
    title: "光圈",
    oneOf: [
      { const: 4, title: "f/4" },
      { const: 4.8, title: "f/4.8" },
    ],
  };
  const shutter = parseJson(
    '{"type":"number","title":"快门","oneOf":[{"const":0.000667,"title":"1/1500 秒"},{"const":0.000333,"title":"1/3000 秒"},{"const":0.00025,"title":"1/4000 秒"}]}',
  );
  const branch = (
    mode: string,
    title: string,
    fields: Record<string, unknown>,
    required = Object.keys(fields),
    modeTitle = "曝光模式",
  ) => ({
    type: "object",
    title,
    additionalProperties: false,
    properties: {
      mode: { type: "string", const: mode, title: modeTitle },
      ...fields,
    },
    required: ["mode", ...required],
  });
  return {
    type: "titled_photo",
    name: "命名参数示例",
    description: "通用命名标量及独立对象编辑",
    preview_supported: false,
    schema: {
      $schema: DIALECT,
      type: "object",
      additionalProperties: false,
      properties: {
        type: { const: "titled_photo" },
        capture_format: {
          type: "string",
          title: "图片格式",
          oneOf: [
            { const: "raw", title: "RAW（3FR）" },
            { const: "jpeg", title: "JPEG" },
          ],
        },
        exposure: {
          title: "曝光设置",
          oneOf: [
            branch("M", "手动曝光", { iso, aperture, shutter_s: shutter }),
            branch("A", "光圈优先", { iso, aperture }),
            branch("S", "快门优先", { iso, shutter_s: shutter }),
            branch("P", "程序曝光", { iso }),
          ],
        },
        white_balance: {
          title: "白平衡设置",
          oneOf: [
            branch("auto", "自动白平衡", {}, [], "白平衡模式"),
            branch(
              "manual",
              "手动白平衡",
              {
                temperature_k: {
                  type: "integer",
                  title: "色温",
                  minimum: 2000,
                  maximum: 10000,
                },
              },
              ["temperature_k"],
              "白平衡模式",
            ),
          ],
        },
        focus: {
          title: "对焦设置",
          oneOf: [
            branch(
              "MF",
              "手动对焦",
              {
                relative_move: {
                  type: "integer",
                  title: "手动调焦相对移动量",
                  enum: [-5, 5],
                },
              },
              [],
              "对焦模式",
            ),
            branch("AF-S", "单次自动对焦", {}, [], "对焦模式"),
          ],
        },
      },
      required: [
        "type",
        "capture_format",
        "exposure",
        "white_balance",
        "focus",
      ],
    },
  };
}
