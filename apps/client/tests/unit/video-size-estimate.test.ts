import { expect, it } from "vitest";
import {
  loadCapabilities,
  validateParams,
} from "../../src/shared/capabilities";
import { parseJson, stringifyJson } from "../../src/shared/json";
import {
  estimateVideoAction,
  formatVideoSize,
} from "../../src/shared/video-size-estimate";
import type {
  EstimateQuantity,
  VideoSizeEstimateDefinition,
} from "../../src/shared/types";
import { capabilityDocument, capsFor, record } from "./video-estimate-fixtures";

const constant = (value: number): EstimateQuantity => ({
  source: "constant",
  value,
});
const parameter = (path: string): EstimateQuantity => ({
  source: "parameter",
  path,
});
const direct = (
  seconds: EstimateQuantity = constant(60),
  bitrate_mbps: VideoSizeEstimateDefinition["bitrate_mbps"] = 130,
): VideoSizeEstimateDefinition => ({
  bitrate_mbps,
  duration: { method: "direct", seconds },
});

it("整份加载保留合法估算声明", () => {
  const definition = {
    bitrate_mbps: 130,
    duration: {
      method: "direct" as const,
      seconds: { source: "constant" as const, value: 60 },
    },
  };
  const result = loadCapabilities(capabilityDocument(definition));
  expect(
    result.devices[0].actions[0].parameter_types[0].video_size_estimate,
  ).toEqual(definition);
});

it("录像依据参考码率和播放秒数返回十进制字节数", () => {
  const caps = capsFor(
    direct(parameter("/duration_s"), {
      by: "/bitrate_mode",
      values: { standard: 95, high: 130 },
    }),
  );
  expect(
    estimateVideoAction(record({ bitrate_mode: "high", duration_s: 60 }), caps),
  ).toEqual({
    kind: "ready",
    sizeBytes: 975_000_000,
    playbackSeconds: 60,
    bitrateMbps: 130,
  });
});
it("参数引用码率允许小数 Mbps", () => {
  const caps = capsFor(direct(constant(8), { from: "/mbps" }), {
    additionalProperties: true,
  });
  expect(estimateVideoAction(record({ mbps: 0.5 }), caps)).toEqual({
    kind: "ready",
    sizeBytes: 500_000,
    playbackSeconds: 8,
    bitrateMbps: 0.5,
  });
});
it("间隔延时使用成片时长计算大小", () => {
  const caps = capsFor(
    {
      bitrate_mbps: 175,
      duration: {
        method: "timelapse_interval",
        capture_seconds: parameter("/capture"),
        interval_seconds: parameter("/interval"),
        playback_fps: constant(30),
      },
    },
    { additionalProperties: true },
    "camera_timelapse",
  );
  expect(
    estimateVideoAction(
      { ...record({ capture: 6000, interval: 25 }), type: "camera_timelapse" },
      caps,
    ),
  ).toEqual({
    kind: "ready",
    playbackSeconds: 8,
    bitrateMbps: 175,
    sizeBytes: 175_000_000,
  });
});
it("非整除的采集比例不取整", () => {
  const caps = capsFor({
    bitrate_mbps: 8,
    duration: {
      method: "timelapse_interval",
      capture_seconds: constant(10),
      interval_seconds: constant(3),
      playback_fps: constant(30),
    },
  });
  const result = estimateVideoAction(record(), caps);
  expect(result.kind).toBe("ready");
  if (result.kind === "ready") {
    expect(result.playbackSeconds).toBeCloseTo(1 / 9, 14);
    expect(result.sizeBytes).toBeCloseTo(111_111.11111111111, 8);
  }
});
it("预计帧数除以成片播放帧率", () => {
  const caps = capsFor(
    {
      bitrate_mbps: 175,
      duration: {
        method: "timelapse_frames",
        frames: parameter("/frames"),
        playback_fps: constant(30),
      },
    },
    { additionalProperties: true },
  );
  expect(estimateVideoAction(record({ frames: 240 }), caps)).toEqual({
    kind: "ready",
    playbackSeconds: 8,
    bitrateMbps: 175,
    sizeBytes: 175_000_000,
  });
});
it("延时摄影的 direct 使用成片时长", () => {
  const caps = capsFor(direct(constant(8), 175), {}, "camera_timelapse");
  expect(
    estimateVideoAction({ ...record(), type: "camera_timelapse" }, caps),
  ).toEqual({
    kind: "ready",
    playbackSeconds: 8,
    bitrateMbps: 175,
    sizeBytes: 175_000_000,
  });
});
it("数量查表接受对应数值", () => {
  const caps = capsFor(
    {
      bitrate_mbps: 175,
      duration: {
        method: "timelapse_frames",
        frames: {
          source: "lookup",
          path: "/frames_mode",
          values: { short: 240 },
        },
        playback_fps: {
          source: "lookup",
          path: "/fps_mode",
          values: { normal: 30 },
        },
      },
    },
    { additionalProperties: true },
  );
  expect(
    estimateVideoAction(
      record({ frames_mode: "short", fps_mode: "normal" }),
      caps,
    ),
  ).toEqual({
    kind: "ready",
    playbackSeconds: 8,
    bitrateMbps: 175,
    sizeBytes: 175_000_000,
  });
});
it("间隔法每个数量都能独立查表", () => {
  const caps = capsFor(
    {
      bitrate_mbps: 175,
      duration: {
        method: "timelapse_interval",
        capture_seconds: {
          source: "lookup",
          path: "/capture",
          values: { long: 6000 },
        },
        interval_seconds: {
          source: "lookup",
          path: "/interval",
          values: { slow: 25 },
        },
        playback_fps: parameter("/fps"),
      },
    },
    { additionalProperties: true },
  );
  expect(
    estimateVideoAction(
      record({ capture: "long", interval: "slow", fps: 30 }),
      caps,
    ),
  ).toEqual({
    kind: "ready",
    playbackSeconds: 8,
    bitrateMbps: 175,
    sizeBytes: 175_000_000,
  });
});
it.each([
  "camera_take_photo",
  "obtain_action_outputs",
  "motor_control",
  undefined,
])("非视频动作不显示区域 %s", (type) =>
  expect(estimateVideoAction({ type }, null)).toEqual({ kind: "hidden" }),
);
it("未启用能力时返回能力不可用", () =>
  expect(estimateVideoAction(record(), null)).toEqual({
    kind: "unavailable",
    reason: "capabilities_unavailable",
  }));
it.each([
  { ...record(), device_id: "other" },
  { ...record(), device_id: undefined },
  { ...record(), params: null },
  { ...record(), params: [] },
  record({ type: "other" }),
  { ...record(), params: {} },
])("选择没有精确匹配时不可估算 %#", (action) =>
  expect(estimateVideoAction(action, capsFor(direct()))).toMatchObject({
    kind: "unavailable",
    reason: "selection_invalid",
  }),
);
it("动作必须在当前设备目录中匹配", () =>
  expect(
    estimateVideoAction(
      { ...record(), type: "camera_timelapse" },
      capsFor(direct()),
    ),
  ).toMatchObject({ kind: "unavailable", reason: "selection_invalid" }));
it("没有声明先返回未提供信息，即使其他参数不合法", () =>
  expect(estimateVideoAction(record({ duration_s: 0 }), capsFor())).toEqual({
    kind: "unavailable",
    reason: "not_provided",
  }));
it.each([null, 0, "", "60", true])(
  "完整参数 Schema 优先拒绝非法参数 %j",
  (duration_s) =>
    expect(
      estimateVideoAction(
        record({ duration_s }),
        capsFor(direct(parameter("/duration_s"))),
      ),
    ).toMatchObject({ kind: "unavailable", reason: "params_invalid" }),
);
it.each([{ mode: "limited", duration_s: 11 }, { mode: "limited" }])(
  "完整参数的条件分支阻止估算 %#",
  (params) => {
    const caps = capsFor(direct(), {
      additionalProperties: true,
      allOf: [
        {
          if: {
            properties: { mode: { const: "limited" } },
            required: ["mode"],
          },
          then: {
            required: ["duration_s"],
            properties: { duration_s: { maximum: 10 } },
          },
        },
      ],
    });
    expect(estimateVideoAction(record(params), caps)).toMatchObject({
      kind: "unavailable",
      reason: "params_invalid",
    });
  },
);
it("可选值省略后不注入 Schema default", () => {
  const caps = capsFor(direct(parameter("/duration_s")), {
    properties: {
      type: { const: "record" },
      duration_s: { type: "number", default: 60 },
    },
  });
  const action = record();
  expect(validateParams("cam-1", "camera_record", action.params, caps)).toEqual(
    [],
  );
  expect(estimateVideoAction(action, caps)).toEqual({
    kind: "unavailable",
    reason: "value_missing",
    path: "/duration_s",
  });
  expect(action.params).toEqual({ type: "record" });
});
it.each([null, 0, "", "60", true, -1, 1.5, NaN, Infinity])(
  "引用帧数不转换类型或非法数值 %j",
  (frames) => {
    const caps = capsFor(
      {
        bitrate_mbps: 175,
        duration: {
          method: "timelapse_frames",
          frames: parameter("/frames"),
          playback_fps: constant(30),
        },
      },
      { additionalProperties: true },
    );
    expect(estimateVideoAction(record({ frames }), caps)).toEqual({
      kind: "unavailable",
      reason: "value_invalid",
      path: "/frames",
    });
  },
);
it("被浮点舍入为整数的引用帧数仍按原数学值拒绝", () => {
  const caps = capsFor(
    {
      bitrate_mbps: 175,
      duration: {
        method: "timelapse_frames",
        frames: parameter("/frames"),
        playback_fps: constant(30),
      },
    },
    { additionalProperties: true },
  );
  expect(
    estimateVideoAction(
      parseJson(
        '{"type":"camera_record","device_id":"cam-1","params":{"type":"record","frames":1.00000000000000000001}}',
      ),
      caps,
    ),
  ).toEqual({ kind: "unavailable", reason: "value_invalid", path: "/frames" });
});
it("数学上为整数的指数帧数可计算", () => {
  const caps = capsFor(
    {
      bitrate_mbps: 175,
      duration: {
        method: "timelapse_frames",
        frames: parameter("/frames"),
        playback_fps: constant(30),
      },
    },
    { additionalProperties: true },
  );
  expect(
    estimateVideoAction(
      parseJson(
        '{"type":"camera_record","device_id":"cam-1","params":{"type":"record","frames":2.4e2}}',
      ),
      caps,
    ),
  ).toEqual({
    kind: "ready",
    playbackSeconds: 8,
    bitrateMbps: 175,
    sizeBytes: 175_000_000,
  });
});
it.each([undefined, null, 0, "", true])("码率引用区分缺失和非法 %#", (mbps) => {
  const caps = capsFor(direct(constant(8), { from: "/mbps" }), {
    additionalProperties: true,
  });
  expect(
    estimateVideoAction(record(mbps === undefined ? {} : { mbps }), caps),
  ).toEqual({
    kind: "unavailable",
    reason: mbps === undefined ? "value_missing" : "value_invalid",
    path: "/mbps",
  });
});
it.each([
  "high",
  "STANDARD",
  " standard",
  "standard ",
  "toString",
  "constructor",
  "__proto__",
])("查表精确匹配且不读取原型 %s", (mode) => {
  const caps = capsFor(
    direct(constant(60), { by: "/mode", values: { standard: 95 } }),
    { additionalProperties: true },
  );
  expect(estimateVideoAction(record({ mode }), caps)).toEqual({
    kind: "unavailable",
    reason: "lookup_missing",
    path: "/mode",
  });
});
it.each([7, true, null])("查表输入必须为字符串 %j", (mode) => {
  const caps = capsFor(
    direct({ source: "lookup", path: "/mode", values: { standard: 60 } }),
    { additionalProperties: true },
  );
  expect(estimateVideoAction(record({ mode }), caps)).toEqual({
    kind: "unavailable",
    reason: "value_invalid",
    path: "/mode",
  });
});
it("查表允许自身原型名称成员", () => {
  const caps = capsFor(
    direct(constant(8), {
      by: "/mode",
      values: Object.fromEntries([["__proto__", 175]]),
    }),
    { additionalProperties: true },
  );
  expect(estimateVideoAction(record({ mode: "__proto__" }), caps)).toEqual({
    kind: "ready",
    playbackSeconds: 8,
    bitrateMbps: 175,
    sizeBytes: 175_000_000,
  });
});
it("参数指针解码特殊成员与数组位置", () => {
  const caps = capsFor(direct(parameter("/a~1b/~0x/0")), {
    additionalProperties: true,
  });
  expect(estimateVideoAction(record({ "a/b": { "~x": [60] } }), caps)).toEqual({
    kind: "ready",
    playbackSeconds: 60,
    bitrateMbps: 130,
    sizeBytes: 975_000_000,
  });
});
it("自身空成员名可以作为估算来源", () => {
  const caps = capsFor(direct(parameter("/")), { additionalProperties: true });
  expect(estimateVideoAction(record({ "": 60 }), caps)).toEqual({
    kind: "ready",
    playbackSeconds: 60,
    bitrateMbps: 130,
    sizeBytes: 975_000_000,
  });
});
it("相同参数类型在设备和动作作用域精确选择", () => {
  const doc = capabilityDocument(direct(constant(8), 175));
  const another = capabilityDocument(direct(constant(60), 130)).devices[0];
  another.device_id = "cam-2";
  doc.devices.push(another);
  doc.devices[0].actions.push(
    capabilityDocument(direct(constant(4), 8), {}, "camera_timelapse")
      .devices[0].actions[0],
  );
  const caps = loadCapabilities(doc);
  expect(
    estimateVideoAction({ ...record(), device_id: "cam-2" }, caps),
  ).toEqual({
    kind: "ready",
    playbackSeconds: 60,
    bitrateMbps: 130,
    sizeBytes: 975_000_000,
  });
  expect(
    estimateVideoAction({ ...record(), type: "camera_timelapse" }, caps),
  ).toEqual({
    kind: "ready",
    playbackSeconds: 4,
    bitrateMbps: 8,
    sizeBytes: 4_000_000,
  });
});
it.each([
  direct(constant(2), 1e308),
  direct(constant(1e-300), 1e-300),
  direct(constant(1e308), 1),
  {
    bitrate_mbps: 8,
    duration: {
      method: "timelapse_frames" as const,
      frames: constant(1e308),
      playback_fps: constant(1e-308),
    },
  },
  {
    bitrate_mbps: 8,
    duration: {
      method: "timelapse_interval" as const,
      capture_seconds: constant(1e308),
      interval_seconds: constant(1e-308),
      playback_fps: constant(1e308),
    },
  },
  {
    bitrate_mbps: 8,
    duration: {
      method: "timelapse_interval" as const,
      capture_seconds: constant(1e-300),
      interval_seconds: constant(1e300),
      playback_fps: constant(1e-300),
    },
  },
])("有限输入不能产生无效计算结果 %#", (definition) => {
  expect(estimateVideoAction(record(), capsFor(definition))).toEqual({
    kind: "unavailable",
    reason: "calculation_invalid",
  });
});
it("计算不改变能力声明或输入", () => {
  const caps = capsFor(direct(parameter("/duration_s")));
  const action = record({ duration_s: 60 });
  const before = stringifyJson({ caps, action });
  estimateVideoAction(action, caps);
  expect(stringifyJson({ caps, action })).toBe(before);
});
it("有效输入转为缺失后撤下全部数字", () => {
  const caps = capsFor(direct(parameter("/duration_s")));
  expect(estimateVideoAction(record({ duration_s: 60 }), caps)).toMatchObject({
    kind: "ready",
  });
  expect(estimateVideoAction(record(), caps)).toEqual({
    kind: "unavailable",
    reason: "value_missing",
    path: "/duration_s",
  });
});
it.each([
  [975_000_000, "975 MB"],
  [1_000_000_000, "1 GB"],
  [1_234_567_890, "1.23 GB"],
  [123_456, "0.12 MB"],
  [1, "<0.01 MB"],
])("大小格式只在显示时舍入 %s", (bytes, expected) =>
  expect(formatVideoSize(bytes as number)).toBe(expected),
);
it("能力复制保留非整数帧数的原始词元并整份拒绝", () => {
  const document = JSON.stringify(
    capabilityDocument({
      bitrate_mbps: 130,
      duration: {
        method: "timelapse_frames",
        frames: { source: "constant", value: 1 },
        playback_fps: { source: "constant", value: 30 },
      },
    }),
  ).replace('"value":1', '"value":1.00000000000000000001');
  expect(() => loadCapabilities(parseJson(document))).toThrow();
});
