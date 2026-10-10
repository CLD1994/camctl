import { readFile } from "node:fs/promises";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  loadCapabilities,
  validateParams,
} from "../../src/shared/capabilities";
import {
  parseJson,
  parseClientJson,
  stringifyJson,
} from "../../src/shared/json";
import { estimateVideoAction } from "../../src/shared/video-size-estimate";
import { Application } from "../../src/server/application";

const root = join(
  import.meta.dirname,
  "../../../../protocol/examples/video-size-estimate",
);
const cases = JSON.parse(
  await readFile(join(root, "cases.json"), "utf8"),
) as Array<{
  name: string;
  json: string;
  load_valid: boolean;
}>;
const demoText = await readFile(join(root, "capabilities.json"), "utf8");

describe("视频估算共同原始文本的整份加载", () => {
  it.each(cases)("$name", ({ json, load_valid }) => {
    const load = () => loadCapabilities(parseJson(json));
    if (load_valid) expect(load().devices).toBeInstanceOf(Array);
    else expect(load).toThrow();
  });
});
it.each([
  {
    type: "camera_record",
    params: { type: "example_record", bitrate_mode: "high", duration_s: 60 },
    sizeBytes: 975_000_000,
    playbackSeconds: 60,
    bitrateMbps: 130,
  },
  {
    type: "camera_record",
    params: { type: "example_record_from", bitrate_mbps: 0.5, duration_s: 8 },
    sizeBytes: 500_000,
    playbackSeconds: 8,
    bitrateMbps: 0.5,
  },
  {
    type: "camera_timelapse",
    params: {
      type: "example_timelapse",
      capture_duration_s: 6000,
      interval_s: 25,
    },
    sizeBytes: 175_000_000,
    playbackSeconds: 8,
    bitrateMbps: 175,
  },
  {
    type: "camera_timelapse",
    params: { type: "example_timelapse_frames", frames: 240 },
    sizeBytes: 175_000_000,
    playbackSeconds: 8,
    bitrateMbps: 175,
  },
])(
  "真实共同能力文件校验并计算 $params.type",
  ({ type, params, sizeBytes, playbackSeconds, bitrateMbps }) => {
    const caps = loadCapabilities(parseJson(demoText));
    const device_id = "estimate_demo_cam0";
    expect(validateParams(device_id, type, params, caps)).toEqual([]);
    expect(estimateVideoAction({ type, device_id, params }, caps)).toEqual({
      kind: "ready",
      sizeBytes,
      playbackSeconds,
      bitrateMbps,
    });
  },
);
it("浏览器经实际 JSON 交接启用与后端相同的合法说明", () => {
  const backend = loadCapabilities(parseJson(demoText));
  const response = parseClientJson(stringifyJson({ active: backend })) as {
    active: unknown;
  };
  const browser = loadCapabilities(response.active);
  const action = {
    type: "camera_record",
    device_id: "estimate_demo_cam0",
    params: { type: "example_record", bitrate_mode: "high", duration_s: 60 },
  };
  expect(estimateVideoAction(action, browser)).toEqual({
    kind: "ready",
    sizeBytes: 975_000_000,
    playbackSeconds: 60,
    bitrateMbps: 130,
  });
});
it("重载含非法估算声明的文件保留此前整体目录和诊断", () => {
  const directory = mkdtempSync(join(tmpdir(), "camctl-video-estimate-"));
  const app = new Application(directory);
  try {
    app.store.initialize();
    const file = join(directory, "device-capabilities.json");
    writeFileSync(file, demoText);
    const loaded = app.reloadCapabilities();
    expect(loaded.error).toBeNull();
    const invalid = JSON.parse(demoText);
    invalid.devices[0].actions[0].parameter_types[0].video_size_estimate.bitrate_mbps.values.high = 0;
    writeFileSync(file, JSON.stringify(invalid));
    const failed = app.reloadCapabilities();
    expect(failed.error).toEqual(expect.any(String));
    expect(failed.active).toBe(loaded.active);
    expect(failed.generation).toBe(loaded.generation);
    expect(
      estimateVideoAction(
        {
          type: "camera_record",
          device_id: "estimate_demo_cam0",
          params: {
            type: "example_record",
            bitrate_mode: "high",
            duration_s: 60,
          },
        },
        failed.active,
      ),
    ).toEqual({
      kind: "ready",
      sizeBytes: 975_000_000,
      playbackSeconds: 60,
      bitrateMbps: 130,
    });
  } finally {
    app.store.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
