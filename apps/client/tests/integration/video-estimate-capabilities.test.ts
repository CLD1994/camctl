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
it("加载当前删除声明、旧说明缺失字段和有效空目录", () => {
  const active = loadCapabilities(
    parseJson(
      JSON.stringify({
        devices: [
          {
            device_id: "camera-alpha",
            driver_id: "vendor.future_camera_v9",
            delete_supported: true,
            actions: [],
          },
          {
            device_id: "camera-beta",
            driver_id: "vendor.future_camera_v9",
            delete_supported: false,
            actions: [],
          },
        ],
      }),
    ),
  );
  expect(
    active.devices.map(({ device_id, driver_id, delete_supported, actions }) => [
      device_id,
      driver_id,
      delete_supported,
      actions,
    ]),
  ).toEqual([
    ["camera-alpha", "vendor.future_camera_v9", true, []],
    ["camera-beta", "vendor.future_camera_v9", false, []],
  ]);

  const legacy = loadCapabilities(
    parseJson(
      '{"devices":[{"device_id":"legacy-camera","driver_id":"any-driver","actions":[]}]}',
    ),
  );
  expect(legacy.devices[0]).not.toHaveProperty("delete_supported");
  expect(loadCapabilities(parseJson('{"devices":[]}'))).toEqual({
    devices: [],
  });
});
it.each([null, 0, "true", []])(
  "整份加载拒绝非布尔删除声明 %j",
  (delete_supported) => {
    expect(() =>
      loadCapabilities({
        devices: [
          {
            device_id: "camera",
            driver_id: "any-driver",
            delete_supported,
            actions: [],
          },
        ],
      }),
    ).toThrow();
  },
);
it("真实 JSON 解析链拒绝重复键和未知设备字段", () => {
  expect(() =>
    loadCapabilities(parseJson('{"devices":[],"devices":[]}')),
  ).toThrow();
  expect(() =>
    loadCapabilities({
      devices: [
        {
          device_id: "camera",
          driver_id: "any-driver",
          delete_supported: true,
          unexpected: true,
          actions: [],
        },
      ],
    }),
  ).toThrow();
});
it("删除动作不属于设备拍摄动作目录", () => {
  expect(() =>
    loadCapabilities({
      devices: [
        {
          device_id: "camera",
          driver_id: "any-driver",
          delete_supported: false,
          actions: [
            {
              type: "delete_action_outputs",
              parameter_types: [
                {
                  type: "deletion",
                  name: "deletion",
                  description: "deletion",
                  preview_supported: false,
                  schema: {
                    $schema:
                      "https://json-schema.org/draft/2020-12/schema",
                    type: "object",
                    properties: { type: { const: "deletion" } },
                    required: ["type"],
                    additionalProperties: false,
                  },
                },
              ],
            },
          ],
        },
      ],
    }),
  ).toThrow();
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
it("非法删除声明重载保留此前整体目录和版本", () => {
  const directory = mkdtempSync(join(tmpdir(), "camctl-delete-capability-"));
  const file = join(directory, "device-capabilities.json");
  const document = JSON.parse(demoText);
  document.devices[0].delete_supported = true;
  writeFileSync(file, JSON.stringify(document));
  const app = new Application(directory);
  try {
    app.store.initialize();
    const loaded = app.capabilities;
    expect(loaded.active?.devices[0].delete_supported).toBe(true);

    document.devices[0].delete_supported = null;
    writeFileSync(file, JSON.stringify(document));
    const failed = app.reloadCapabilities();
    expect(failed.error).toEqual(expect.any(String));
    expect(failed.active).toBe(loaded.active);
    expect(failed.generation).toBe(loaded.generation);
  } finally {
    app.store.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
it("首次文件含非法删除声明时不启用部分目录", () => {
  const directory = mkdtempSync(join(tmpdir(), "camctl-delete-capability-"));
  const file = join(directory, "device-capabilities.json");
  const document = JSON.parse(demoText);
  document.devices[0].delete_supported = null;
  writeFileSync(file, JSON.stringify(document));
  const app = new Application(directory);
  try {
    expect(app.capabilities.active).toBeNull();
    expect(app.capabilities.error).toEqual(expect.any(String));
    expect(app.capabilities.generation).toBe(0);
  } finally {
    app.store.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
it("只改变删除声明不修改草稿、预设或已导出的原请求", () => {
  const directory = mkdtempSync(join(tmpdir(), "camctl-delete-capability-"));
  const file = join(directory, "device-capabilities.json");
  const document = JSON.parse(demoText);
  document.devices[0].delete_supported = false;
  writeFileSync(file, JSON.stringify(document));
  const app = new Application(directory);
  try {
    app.store.initialize();
    const content = {
      text: JSON.stringify({
        name: "仅查询当前状态",
        actions: [
          {
            name: "完整同步",
            type: "report_status",
            params: { scope: "full" },
          },
        ],
      }),
    };
    const draft = app.createDraft(content);
    const requestDraft = app.createDraft(content);
    const request = app.exportDraft(
      requestDraft.id,
      requestDraft.revision,
      requestDraft.content,
    );
    const preset = app.savePreset({
      name: "常用录像",
      deviceId: "estimate_demo_cam0",
      actionType: "camera_record",
      params: {
        type: "example_record",
        bitrate_mode: "high",
        duration_s: 60,
      },
    });
    const beforeDraft = app.draft(draft.id);
    const beforeRequestDraft = app.draft(requestDraft.id);
    const beforePreset = app.store.get("presets", preset.id);
    const beforeRequest = app.request(request.id);

    document.devices[0].delete_supported = true;
    writeFileSync(file, JSON.stringify(document));
    const reloaded = app.reloadCapabilities();

    expect(reloaded.error).toBeNull();
    expect(reloaded.active?.devices[0].delete_supported).toBe(true);
    expect(app.draft(draft.id)).toEqual(beforeDraft);
    expect(app.draft(requestDraft.id)).toEqual(beforeRequestDraft);
    expect(app.store.get("presets", preset.id)).toEqual(beforePreset);
    expect(app.request(request.id)).toEqual(beforeRequest);
  } finally {
    app.store.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
