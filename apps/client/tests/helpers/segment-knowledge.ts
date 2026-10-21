import { cloneProtocolJson } from "../../src/shared/json";
import type { StatusReport } from "../../src/shared/types";
import type { SegmentDetails } from "../../src/shared/segment-report-errors";
import { segmentDetails, segmentReport } from "./segment-reports";

/** 两个缺口的四个可靠邻接方向；可选引用与实际文件身份独立。 */
export function knowledgeDetails(
  ids: (string | undefined)[] = [],
): SegmentDetails {
  const d = segmentDetails() as SegmentDetails;
  d.default_scope = { state: "unknown", member_count: null, output_ids: [] };
  d.gap_count = 2;
  d.gaps = [3, 6].map((index, i) => ({
    kind: "missing_position",
    start_key: { directory_number: 1, index },
    end_key: { directory_number: 1, index },
    left: {
      state: "known",
      file_identity: `left-${i}`,
      ...(ids[i * 2] === undefined ? {} : { output_id: ids[i * 2] }),
    },
    right: {
      state: "known",
      file_identity: `right-${i}`,
      ...(ids[i * 2 + 1] === undefined ? {} : { output_id: ids[i * 2 + 1] }),
    },
  }));
  d.first_gap = cloneProtocolJson(d.gaps[0]);
  return d;
}

export function knowledgeReport(
  copies: (string | undefined)[][] = [[]],
): StatusReport {
  const r = segmentReport();
  const source = r.plans![0].actions![0];
  source.error!.details = knowledgeDetails(copies[0]) as any;
  for (let i = 1; i < copies.length; i++)
    r.plans![0].actions!.push({
      action_instance_id: String(20 + i),
      name: `默认取回${i}`,
      type: "obtain_action_outputs",
      scheduled_at: "2026-01-01 00:00:00",
      status: "failed",
      input_params: { source: { action_instance_id: "10" } },
      error: { code: "obtain_items_failed", stage: "execution", details: {} },
      result: {
        failures: [
          {
            source_action_instance_id: "10",
            error: {
              code: "default_segment_scope_unconfirmed",
              stage: "output_selection",
              details: knowledgeDetails(copies[i]),
            },
          },
        ],
      },
    });
  return r;
}
export function knowledgeCopies(r: StatusReport): SegmentDetails[] {
  return r.plans![0].actions!.map(
    (a, i) =>
      (i === 0
        ? a.error!.details
        : (a.result as any).failures[0].error.details) as SegmentDetails,
  );
}
export function atWatermark(
  r: StatusReport,
  id: string,
  from: number,
  to: number,
) {
  r.report_id = id;
  r.from_wm = from;
  r.to_wm = to;
  return r;
}
export function permutations<T>(values: T[]): T[][] {
  return values.length === 0
    ? [[]]
    : values.flatMap((v, i) =>
        permutations(values.filter((_, j) => i !== j)).map((tail) => [
          v,
          ...tail,
        ]),
      );
}
