import type { ActionType } from './status-report.generated';
export type { Camctl as StatusReport, Plan as ReportPlan, Action as ReportAction, Output, Delivery, ActionType } from './status-report.generated';
export interface Issue {
  path: string;
  code: string;
  message: string;
  /** 客户端内部的精确数据路径；path 仍保留既有机器诊断表示。 */
  pointer?: string;
  schema?: {
    instancePath: string;
    schemaPath: string;
    keyword: string;
    params: Record<string, unknown>;
  };
}
export type EstimateQuantity =
  | { source: "constant"; value: number }
  | { source: "parameter"; path: string }
  | { source: "lookup"; path: string; values: Record<string, number> };
export type VideoSizeEstimateDefinition = {
  bitrate_mbps:
    number | { from: string } | { by: string; values: Record<string, number> };
  duration:
    | { method: "direct"; seconds: EstimateQuantity }
    | {
        method: "timelapse_frames";
        frames: EstimateQuantity;
        playback_fps: EstimateQuantity;
      }
    | {
        method: "timelapse_interval";
        capture_seconds: EstimateQuantity;
        interval_seconds: EstimateQuantity;
        playback_fps: EstimateQuantity;
      };
};
export type EstimateReason =
  | "capabilities_unavailable"
  | "rules_updating"
  | "reload_unconfirmed"
  | "input_unfinished"
  | "pending_scope_unknown"
  | "selection_invalid"
  | "not_provided"
  | "params_invalid"
  | "value_missing"
  | "value_invalid"
  | "lookup_missing"
  | "calculation_invalid";
export type VideoEstimate =
  | { kind: "hidden" }
  | {
      kind: "unavailable";
      reason: EstimateReason;
      path?: string;
      label?: string;
      diagnostic?: string;
      warning?: string;
    }
  | {
      kind: "ready";
      sizeBytes: number;
      playbackSeconds: number;
      bitrateMbps: number;
      warning?: string;
    };
export interface ParameterType {
  type: string;
  name: string;
  description: string;
  preview_supported: boolean;
  schema: Record<string, unknown>;
  video_size_estimate?: VideoSizeEstimateDefinition;
}
export interface Capabilities { devices: Array<{ device_id: string; driver_id: string; actions: Array<{ type: string; parameter_types: ParameterType[] }> }> }
export type Source = { action_instance_id: string } | { plan_instance_id: string; group: string } | { action_name: string } | { group: string };
export type Target = { request_id: string } | { plan_instance_id: string; group?: string } | { action_instance_id: string };
export interface Action { name: string; type: ActionType; device_id?: string; scheduled_at?: string; group?: string; params?: Record<string, unknown>; policy?: Record<string, unknown> }
export interface Plan { request_id: string; created_at: string; name: string; actions: Action[]; last_report_id?: string }
export interface ValidationContext { reports?: Array<{ report_id: string; to_wm: number }>; coverage?: number }
