import { expect, it } from "vitest";
import {
  validateReport,
  validateReportAgainstHistory,
} from "../../src/domain/reports";
import { resultNotes } from "../../src/web/result-model";
import { cloneProtocolJson } from "../../src/shared/json";
import {
  sourceActionFailedActions,
  sourceActionFailedReport,
} from "../helpers/source-action-failed";

it("accepts source_action_failed for a visible failed source with an explicitly empty output list", () => {
  expect(() => validateReport(sourceActionFailedReport())).not.toThrow();
});

it.each([
  "details_source_mismatch",
  "extra_source_details",
  "wrong_stage",
  "missing_source_details",
  "source_succeeded",
  "source_outputs_missing",
  "source_has_outputs",
  "explicit_ids",
  "misplaced_action_error",
])("rejects invalid source_action_failed facts: %s", (change) => {
  const report = sourceActionFailedReport();
  const { source, preview } = sourceActionFailedActions(report);
  const failure = (preview.result as any).failures[0];
  if (change === "details_source_mismatch")
    failure.error.details.source_action_instance_id = "30";
  if (change === "extra_source_details") failure.error.details.reason = "extra";
  if (change === "wrong_stage") failure.error.stage = "execution";
  if (change === "missing_source_details")
    delete failure.error.details.source_action_instance_id;
  if (change === "source_succeeded") {
    source.status = "succeeded";
    source.error = undefined;
  }
  if (change === "source_outputs_missing") delete source.outputs;
  if (change === "source_has_outputs") {
    source.outputs = [
      {
        output_id: "50",
        source_action_instance_id: source.action_instance_id,
        kind: "original",
        media_type: "video/mp4",
        availability: "available",
        cleanup: { status: "not_requested" },
        checksum: { status: "not_obtained" },
        media: {
          check_status: "not_performed",
          duration: { status: "unknown" },
        },
      },
    ];
  }
  if (change === "explicit_ids")
    preview.input_params = {
      source: { action_instance_id: source.action_instance_id },
      output_ids: ["50"],
    };
  if (change === "misplaced_action_error") {
    source.error = cloneProtocolJson(failure.error);
    failure.error = {
      code: "output_not_found",
      stage: "output_selection",
      details: { requested_output_id: "50" },
    };
  }
  expect(() => validateReport(report)).toThrow();
});

it("allows a source failure item when its source action is absent from the same report", () => {
  const report = sourceActionFailedReport();
  const { preview } = sourceActionFailedActions(report);
  report.plans![0].actions = [preview];
  expect(() => validateReport(report)).not.toThrow();
});

it("does not treat a prior boundary's omitted output list as an empty source at the new boundary", () => {
  const current = sourceActionFailedReport();
  const { source } = sourceActionFailedActions(current);
  delete source.outputs;
  current.plans![0].actions = [source];
  const incoming = sourceActionFailedReport();
  const { preview } = sourceActionFailedActions(incoming);
  incoming.from_wm = current.to_wm;
  incoming.to_wm = current.to_wm + 10;
  incoming.plans![0].actions = [preview];

  expect(() => validateReportAgainstHistory(current, incoming)).not.toThrow();
});

it("requires a source visible across imports at the same report boundary to be explicitly empty and failed", () => {
  const current = sourceActionFailedReport();
  const { source } = sourceActionFailedActions(current);
  current.plans![0].actions = [source];

  const incoming = sourceActionFailedReport();
  const { preview } = sourceActionFailedActions(incoming);
  incoming.plans![0].actions = [preview];
  incoming.from_wm = current.to_wm;
  incoming.to_wm = current.to_wm;

  expect(() => validateReportAgainstHistory(current, incoming)).not.toThrow();
  delete source.outputs;
  expect(() => validateReportAgainstHistory(current, incoming)).toThrow();
});

it("explains source_action_failed without segment-range language and locates the source", () => {
  const report = sourceActionFailedReport();
  const { source, preview } = sourceActionFailedActions(report);
  const text = resultNotes(preview, [source])
    .map((note) => note.text)
    .join(" ");

  expect(text).toContain("来源动作失败");
  expect(text).toContain("未生成可取回产物或预览");
  expect(text).toContain(source.name);
  expect(text).toContain(source.action_instance_id);
  expect(text).toContain("device_prepare_failed");
  expect(text).not.toMatch(/缺口|默认范围未确认/);
});

it("still locates source_action_failed by ID if the source action is not yet reported", () => {
  const report = sourceActionFailedReport();
  const { preview } = sourceActionFailedActions(report);
  const text = resultNotes(preview)
    .map((note) => note.text)
    .join(" ");

  expect(text).toContain("10");
  expect(text).toContain("来源动作失败");
  expect(text).not.toMatch(/缺口|默认范围未确认/);
});
