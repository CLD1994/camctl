/* 从公共 status-report.schema.json 生成；请勿手工修改。 */

/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "positive_integer".
 */
export type PositiveInteger = number;
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "uint".
 */
export type Uint = number;
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "id".
 */
export type Id = string;
/**
 * UTC 时间字面量；日期和时间的实际有效性另按语义校验。
 *
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "timestamp".
 */
export type Timestamp = string;
/**
 * 首尾 Unicode 空白按输入契约另作语义校验，不依赖不同正则引擎对空白的不同解释。
 *
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "name".
 */
export type Name = string;
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "plan_status".
 */
export type PlanStatus = "pending" | "running" | "completed";
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "action".
 */
export type Action = {
  action_instance_id: Id;
  name: Name;
  type: ActionType;
  device_id?: unknown;
  scheduled_at?: unknown;
  group?: unknown;
  policy?: unknown;
  input_params?: unknown;
  extra_input_fields?: {
    [k: string]: unknown;
  };
  effective_params?: {
    type: Text;
    [k: string]: unknown;
  };
  status: ActionStatus;
  execution: {
    started: boolean;
  };
  expiration_reason?: "window_missed" | "window_exhausted";
  /**
   * @minItems 1
   */
  waiting?: [Waiting, ...Waiting[]];
  error?: Error;
  result?: {
    [k: string]: unknown;
  };
  outputs?: Output[];
  deliveries?: Delivery[];
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "action_type".
 */
export type ActionType =
  "camera_record" | "obtain_action_outputs" | "delete_action_outputs" | "cancel_task" | "report_status";
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "text".
 */
export type Text = string;
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "action_status".
 */
export type ActionStatus = "pending" | "running" | "succeeded" | "failed" | "expired" | "canceled";
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "output".
 */
export type Output = {
  output_id: Id;
  source_action_instance_id: Id;
  kind: "original" | "repaired";
  original_name?: Text;
  media_type?: Text;
  size?: Uint;
  derived_from_output_id?: Id;
  availability: "available" | "restricted" | "cleaned" | "missing" | "unknown";
  cleanup: Cleanup;
  checksum: Checksum;
  media: Media;
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "cleanup".
 */
export type Cleanup = {
  status: "not_requested" | "pending" | "running" | "completed" | "incomplete" | "canceled";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "checksum".
 */
export type Checksum =
  | {
      status: "not_obtained";
    }
  | {
      status: "available";
      sha256: Sha256;
    };
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "sha256".
 */
export type Sha256 = string;
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "media".
 */
export type Media = {
  check_status: "not_performed" | "running" | "completed" | "failed" | "unconfirmed";
  duration: MediaDuration;
  issues?: Error[];
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "media_duration".
 */
export type MediaDuration =
  | {
      status: "unknown";
    }
  | {
      status: "available";
      seconds: NonnegativeNumber;
    };
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "nonnegative_number".
 */
export type NonnegativeNumber = number;
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "delivery".
 */
export type Delivery = {
  delivery_id: Id;
  output_id: Id;
  source_action_instance_id: Id;
  file_name: string;
  display_name: Text;
  size?: Uint;
  sha256?: Sha256;
  status: "pending" | "preparing" | "prepared" | "publishing" | "published" | "failed" | "canceled" | "withdrawn";
  copy: Copy;
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "attempt".
 */
export type Attempt = {
  attempt_no: PositiveInteger;
  status: "running" | "succeeded" | "failed" | "unknown";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "verification".
 */
export type Verification = {
  status: "not_performed" | "running" | "matched" | "mismatched" | "source_checksum_unavailable" | "failed";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "work_file_cleanup".
 */
export type WorkFileCleanup = {
  status: "not_needed" | "pending" | "running" | "completed" | "failed" | "unknown";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "diagnostic".
 */
export type Diagnostic = {
  diagnostic_id: Id;
  file_name: Text;
  request_id?: Id;
  /**
   * @minItems 1
   */
  errors: [
    Error & {
      stage?: "input_read" | "input_parse" | "admission" | "ack";
      [k: string]: unknown;
    },
    ...(Error & {
      stage?: "input_read" | "input_parse" | "admission" | "ack";
      [k: string]: unknown;
    })[]
  ];
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "effect".
 */
export type Effect = {
  status: "registered" | "compensating" | "compensated" | "compensation_failed";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "emergency_stop".
 */
export type EmergencyStop = {
  flow_id: Id;
  session_id: Id;
  max_attempts?: PositiveInteger;
  attempts_used: Uint;
  outcome: "stopped" | "unconfirmed" | "not_attempted";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "followup_stop".
 */
export type FollowupStop = {
  flow_id: Id;
  trigger_action_instance_id: Id;
  max_attempts: PositiveInteger;
  attempts: Attempt[];
  status: "pending" | "running" | "stopped" | "failed" | "canceled" | "expired";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "repair".
 */
export type Repair = {
  status: "undetermined" | "not_needed" | "pending" | "running" | "succeeded" | "failed" | "canceled";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "delete_item".
 */
export type DeleteItem = {
  output_id: Id;
  status: "pending" | "running" | "succeeded" | "failed" | "canceled";
  outcome?: "deleted" | "already_cleaned" | "absence_confirmed";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "withdrawal".
 */
export type Withdrawal = {
  delivery_id: Id;
  status: "pending" | "withdrawn" | "not_retractable" | "failed";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "cancel_item".
 */
export type CancelItem = {
  action_instance_id: Id;
  status: "pending" | "running" | "succeeded" | "failed";
  outcome?: "canceled" | "already_terminal";
  withdrawals?: Withdrawal[];
  error?: Error;
};

/**
 * 定义报告字段与局部结构约束；跨字段、跨实体、历史及累计覆盖检查见 report-format.md。输入原值与错误 details 的内容按各自契约解释。
 */
export interface Camctl {
  report_id: PositiveInteger;
  from_wm: Uint;
  to_wm: Uint;
  plans?: Plan[];
  plan_file_diagnostics?: Diagnostic[];
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "plan".
 */
export interface Plan {
  plan_instance_id: Id;
  request_id: Id;
  plan_seq: PositiveInteger;
  created_at: Timestamp;
  name: Name;
  status: PlanStatus;
  actions?: Action[];
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "waiting".
 */
export interface Waiting {
  code:
    | "scheduled_time"
    | "device_busy"
    | "device_reserved"
    | "retry_delay"
    | "source_actions"
    | "copy_slot"
    | "readers"
    | "clock_untrusted"
    | "report_publication";
  details: {
    [k: string]: unknown;
  };
}
/**
 * 错误码及阶段为生产者登记的标识；驱动可提供具体原因。未知码保留展示，不改变实体状态或合并规则。details 按对应错误契约解释。
 *
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "error".
 */
export interface Error {
  code: Text;
  stage: Text;
  details: {
    [k: string]: unknown;
  };
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "copy".
 */
export interface Copy {
  max_read_attempts: PositiveInteger;
  read_idle_timeout_s: number;
  max_recopies: Uint;
  recopies_used: Uint;
  round: PositiveInteger;
  committed_bytes: Uint;
  source_size?: Uint;
  read_attempts: Attempt[];
  verification: Verification;
  work_file_cleanup: WorkFileCleanup;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "attempts".
 */
export interface Attempts {
  max_attempts: PositiveInteger;
  attempts: Attempt[];
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "residual".
 */
export interface Residual {
  status: "possibly_recording" | "stopped";
  error?: Error;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "recording".
 */
export interface Recording {
  start: Attempts;
  stop: Attempts;
  control_elapsed_s?: NonnegativeNumber;
  effect?: Effect;
  residual?: Residual;
  emergency_stops?: EmergencyStop[];
  followup_stops?: FollowupStop[];
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "camera_result".
 */
export interface CameraResult {
  recording?: Recording;
  check?: Media;
  repair?: Repair;
  source_copy?: Copy;
  discard_cleanup?: WorkFileCleanup;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "obtain_failure".
 */
export interface ObtainFailure {
  source_action_instance_id: Id;
  output_id?: Id;
  delivery_id?: Id;
  error: Error;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "obtain_result".
 */
export interface ObtainResult {
  failures: ObtainFailure[];
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "delete_result".
 */
export interface DeleteResult {
  items: DeleteItem[];
  minItems?: 0;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "cancel_result".
 */
export interface CancelResult {
  items: CancelItem[];
  minItems?: 0;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "report_result".
 */
export interface ReportResult {
  report_id: PositiveInteger;
}
