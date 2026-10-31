import { expect, it } from "vitest";
import { fileMediaType } from "../../src/shared/media";
import type { StatusReport } from "../../src/shared/types";

function snapshot(originalName: string | undefined, mediaType?: string) {
  return {
    plans: [
      {
        actions: [
          {
            outputs: [
              {
                output_id: "1",
                original_name: originalName,
                media_type: mediaType,
              },
            ],
          },
          { deliveries: [{ output_id: "1", file_name: "1.bin" }] },
        ],
      },
    ],
  } as StatusReport;
}

it.each([
  ["clip.MP4", undefined],
  ["clip.mp4", "application/octet-stream"],
  ["clip.LRF", undefined],
  ["clip.lrf", "application/octet-stream"],
  ["clip.OSV", undefined],
  ["clip.oSv", "application/octet-stream"],
] as const)("%s 的通用或缺失类型可用于 MP4 预览", (name, type) => {
  expect(fileMediaType(snapshot(name, type), "1.bin")).toBe("video/mp4");
});

it.each(["1.Mp4", "1.LRF", "1.osv"])("缺少来源名称时使用已关联交付 %s", (name) => {
  const report = snapshot(undefined);
  report.plans![0].actions![1].deliveries![0].file_name = name;
  expect(fileMediaType(report, name)).toBe("video/mp4");
});

it.each(["image/png", "image/svg+xml", "video/webm"])(
  "明确的内容类型 %s 保持原义",
  (type) =>
    expect(fileMediaType(snapshot("clip.MP4", type), "1.bin")).toBe(type),
);

it.each(["clip.mp4.backup", "clip.lrf.backup", "clip.osv.txt", undefined])(
  "不从未登记的兼容名称 %s 推断类型",
  (name) => expect(fileMediaType(snapshot(name), "1.bin")).toBeUndefined(),
);

it("未关联报告的文件不因扩展名取得内容类型", () => {
  expect(fileMediaType(snapshot("clip.MP4"), "missing.MP4")).toBeUndefined();
});
