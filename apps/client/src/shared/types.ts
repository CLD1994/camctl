import type { ActionType } from './status-report.generated';
export type { Camctl as StatusReport, Plan as ReportPlan, Action as ReportAction, Output, Delivery, ActionType } from './status-report.generated';
export interface Issue { path: string; code: string; message: string }
export interface ParameterType { type: string; name: string; description: string; schema: Record<string, unknown> }
export interface Capabilities { devices: Array<{ device_id: string; driver_id: string; actions: Array<{ type: string; parameter_types: ParameterType[] }> }> }
export type Source = { action_instance_id: string } | { plan_instance_id: string; group: string } | { action_name: string } | { group: string };
export type Target = { request_id: string } | { plan_instance_id: string; group?: string } | { action_instance_id: string };
export interface Action { name: string; type: ActionType; device_id?: string; scheduled_at?: string; group?: string; params?: Record<string, unknown>; policy?: Record<string, unknown> }
export interface Plan { request_id: string; created_at: string; name: string; actions: Action[]; last_report_id?: number }
export interface ValidationContext { reports?: Array<{ report_id: number; to_wm: number }>; coverage?: number }
