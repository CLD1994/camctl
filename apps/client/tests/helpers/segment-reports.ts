import type {
  ReportAction,
  StatusReport,
  Output,
} from "../../src/shared/types";
import { cloneProtocolJson } from "../../src/shared/json";

export function segmentOutput(id: string): Output {
  return {
    output_id: id,
    source_action_instance_id: "10",
    kind: "original",
    original_name: `CAM_reverse_${id}.OSV`,
    media_type: "video/mp4",
    availability: "available",
    cleanup: { status: "not_requested" },
    checksum: { status: "not_obtained" },
    media: { check_status: "not_performed", duration: { status: "unknown" } },
  };
}

export function segmentDetails() {
  const first = {
    kind: "missing_position",
    start_key: { directory_number: 1, index: 3 },
    end_key: { directory_number: 1, index: 3 },
    left: { state: "known", file_identity: "observed-left", output_id: "10" },
    right: { state: "known", file_identity: "observed-right", output_id: "11" },
  };
  return {
    source_action_instance_id: "10",
    description: "来源动作 10 存在缺口，保留原片与默认范围分别表达。",
    gap_count: 1 as number | null,
    first_gap: cloneProtocolJson(first),
    gaps: [first],
    default_scope: {
      state: "nonempty",
      member_count: 2 as number | null,
      output_ids: ["2", "10"],
    },
    retained_original_count: 4,
    retained_output_ids: ["2", "10", "11", "12"],
  };
}

export function segmentReport(): StatusReport {
  const source: ReportAction = {
    action_instance_id: "10",
    name: "分段录像",
    type: "camera_record",
    device_id: "test-camera",
    scheduled_at: "2026-01-01 00:00:00",
    policy: { max_delay_ms: 0 },
    input_params: { type: "historical_record" },
    effective_params: { type: "historical_record" },
    status: "failed",
    error: {
      code: "capture_segment_gap",
      stage: "execution",
      details: segmentDetails(),
    },
    outputs: ["2", "10", "11", "12"].map(segmentOutput),
  };
  return {
    report_id: "1",
    from_wm: 0,
    to_wm: 10,
    plans: [
      {
        plan_instance_id: "1",
        request_id: "1",
        name: "分段结果",
        created_at: "2026-01-01 00:00:00",
        status: "completed",
        actions: [source],
      },
    ],
  };
}

export function scopeReport(state: "empty" | "unknown"): StatusReport {
  const report = segmentReport();
  const details = segmentDetails();
  details.default_scope = {
    state,
    member_count: state === "empty" ? 0 : null,
    output_ids: [],
  };
  if (state === "unknown") {
    details.gap_count = null;
    details.gaps = [];
    delete (details as Partial<typeof details>).first_gap;
  }
  report.plans![0].actions![0].error =
    state === "empty"
      ? {
          code: "capture_segment_gap",
          stage: "execution",
          details: cloneProtocolJson(details),
        }
      : { code: "capture_unconfirmed", stage: "execution", details: {} };
  report.plans![0].actions!.push({
    action_instance_id: "20",
    name: "默认取回",
    type: "obtain_action_outputs",
    status: "failed",
    scheduled_at: "2026-01-01 00:00:00",
    input_params: { source: { action_instance_id: "10" } },
    error: { code: "obtain_items_failed", stage: "execution", details: {} },
    result: {
      failures: [
        {
          source_action_instance_id: "10",
          error: {
            code:
              state === "empty"
                ? "default_segment_scope_empty"
                : "default_segment_scope_unconfirmed",
            stage: "output_selection",
            details,
          },
        },
      ],
    },
  });
  return report;
}
