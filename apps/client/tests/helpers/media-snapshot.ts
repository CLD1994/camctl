import { createHash } from "node:crypto";
import type { StatusReport } from "../../src/shared/types";

/** 构造已保存的客户端媒体快照，供文件读取与网页协作测试使用。 */
export function mediaSnapshot(
  bytes: Buffer,
  fileName: string,
  mediaType?: string,
): StatusReport {
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  return {
    report_id: "1",
    from_wm: 0,
    to_wm: 20,
    plans: [
      {
        plan_instance_id: "1",
        request_id: "1",
        created_at: "2026-10-25 00:00:00",
        name: "媒体查看",
        status: "completed",
        actions: [
          {
            action_instance_id: "1",
            name: "拍摄",
            type: "camera_record",
            device_id: "cam0",
            scheduled_at: "2026-10-25 00:00:00",
            input_params: { type: "timed", duration_s: 1 },
            effective_params: { type: "timed", duration_s: 1 },
            policy: { max_delay_ms: 0 },
            status: "succeeded",
            result: { repair: { status: "not_needed" } },
            outputs: [
              {
                output_id: "1",
                source_action_instance_id: "1",
                kind: "original",
                original_name: fileName,
                media_type: mediaType,
                size: bytes.length,
                availability: "available",
                cleanup: { status: "not_requested" },
                checksum: { status: "available", sha256 },
                media: {
                  check_status: "not_performed",
                  duration: { status: "unknown" },
                },
              },
            ],
          },
          {
            action_instance_id: "2",
            name: "取回",
            type: "obtain_action_outputs",
            scheduled_at: "2026-10-25 00:00:01",
            input_params: { source: { action_instance_id: "1" } },
            status: "succeeded",
            result: { failures: [] },
            deliveries: [
              {
                delivery_id: "1",
                output_id: "1",
                source_action_instance_id: "1",
                file_name: fileName,
                display_name: fileName,
                status: "published",
                size: bytes.length,
                sha256,
              },
            ],
          },
        ],
      },
    ],
  };
}
