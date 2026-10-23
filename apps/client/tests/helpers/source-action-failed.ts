import type { ReportAction, StatusReport } from "../../src/shared/types";
import { scopeReport } from "./segment-reports";

export function sourceActionFailedReport(): StatusReport {
  const report = scopeReport("unknown");
  const [source, preview] = report.plans![0].actions!;
  source.name = "准备失败录像";
  source.status = "failed";
  source.error = {
    code: "device_prepare_failed",
    stage: "execution",
    details: {},
  };
  source.outputs = [];

  preview.name = "自动预览";
  preview.automation = {
    purpose: "auto_preview",
    source_action_instance_id: source.action_instance_id,
  };
  preview.input_params = {
    source: { action_name: source.name },
    filter: "preview",
    purpose: "auto_preview",
  };
  preview.result = {
    failures: [
      {
        source_action_instance_id: source.action_instance_id,
        error: {
          code: "source_action_failed",
          stage: "output_selection",
          details: { source_action_instance_id: source.action_instance_id },
        },
      },
    ],
  };
  preview.status = "failed";
  preview.error = {
    code: "obtain_items_failed",
    stage: "execution",
    details: {},
  };
  preview.deliveries = undefined;
  return report;
}

export function sourceActionFailedActions(report: StatusReport): {
  source: ReportAction;
  preview: ReportAction;
} {
  const [source, preview] = report.plans![0].actions!;
  return { source, preview };
}
