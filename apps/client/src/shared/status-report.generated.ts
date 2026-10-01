/* 从公共 status-report.schema.json 生成；请勿手工修改。 */

export type EntityId =
  | string
  | {
      [k: string]: unknown;
    };
export type EntityId1 = string;
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "uint".
 */
export type Uint = number;
/**
 * 固定到秒的 UTC 时间字面量 YYYY-MM-DD HH:mm:ss；不接受小数秒，日期和时间的实际有效性另按语义校验。
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
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  action_instance_id: EntityId & EntityId1;
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
  expiration_reason?: "window_missed" | "window_exhausted";
  error?: Error;
  result?: {
    [k: string]: unknown;
  };
  outputs?: Output[];
  deliveries?: Delivery[];
  automation?: Automation;
  device_execution?: DeviceExecution;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "action_type".
 */
export type ActionType =
  CameraActionType | ("obtain_action_outputs" | "delete_action_outputs" | "cancel_task" | "report_status");
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "camera_action_type".
 */
export type CameraActionType = "camera_take_photo" | "camera_record" | "camera_timelapse";
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
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  output_id: EntityId & EntityId1;
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  source_action_instance_id: EntityId & EntityId1;
  kind: "original" | "repaired" | "preview";
  original_name?: Text;
  media_type?: Text;
  size?: Uint;
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  derived_from_output_id?: EntityId & EntityId1;
  availability: "available" | "restricted" | "cleaned" | "missing" | "unknown";
  cleanup: Cleanup;
  checksum: Checksum;
  media: Media;
  error?: Error;
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  preview_of_output_id?: EntityId & EntityId1;
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
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  delivery_id: EntityId & EntityId1;
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  output_id: EntityId & EntityId1;
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  source_action_instance_id: EntityId & EntityId1;
  file_name: string;
  display_name: Text;
  size?: Uint;
  sha256?: Sha256;
  status: "pending" | "preparing" | "prepared" | "publishing" | "published" | "failed" | "canceled" | "withdrawn";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "diagnostic".
 */
export type Diagnostic = {
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  diagnostic_id: EntityId & EntityId1;
  file_name: Text;
  /**
   * 调用方提供的正整数请求身份，使用规范十进制字符串，范围 1～9223372036854775807；用于幂等关联，不是主机分配的计划 ID。
   */
  request_id?: (
    | string
    | {
        [k: string]: unknown;
      }
  ) &
    string;
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
 * via the `definition` "id".
 */
export type Id = string;
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "repair".
 */
export type Repair = {
  status: "not_needed" | "succeeded" | "failed" | "canceled";
  error?: Error;
};
/**
 * 取消要求涉及的拍摄内容处理结果；状态与错误来自实际处理事实。
 *
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "discard_cleanup".
 */
export type DiscardCleanup = {
  status: "not_needed" | "pending" | "running" | "completed" | "failed" | "unknown";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "delete_item".
 */
export type DeleteItem = {
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  output_id: EntityId & EntityId1;
  status: "pending" | "running" | "succeeded" | "failed" | "canceled";
  outcome?: "deleted" | "already_cleaned" | "absence_confirmed";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "withdrawal".
 */
export type Withdrawal = {
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  delivery_id: EntityId & EntityId1;
  status: "pending" | "withdrawn" | "not_retractable" | "failed";
  error?: Error;
};
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "cancel_item".
 */
export type CancelItem = {
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  action_instance_id: EntityId & EntityId1;
  status: "pending" | "running" | "succeeded" | "failed" | "canceled";
  outcome?: "canceled" | "already_terminal";
  withdrawals?: Withdrawal[];
  error?: Error;
  cancellation_effect?: "not_applied" | "applied" | "not_required";
};
export type RequestId =
  | string
  | {
      [k: string]: unknown;
    };
export type RequestId1 = string;

/**
 * 第一版业务状态报告：计划和动作结果、产物、交付、逐项失败及动作结束后的设备执行情况。跨实体语义、历史和累计覆盖检查见 report-format.md。
 */
export interface Camctl {
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  report_id: EntityId & EntityId1;
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
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  plan_instance_id: EntityId & EntityId1;
  /**
   * 调用方提供的正整数请求身份，使用规范十进制字符串，范围 1～9223372036854775807；用于幂等关联，不是主机分配的计划 ID。
   */
  request_id: (
    | string
    | {
        [k: string]: unknown;
      }
  ) &
    string;
  created_at: Timestamp;
  name: Name;
  status: PlanStatus;
  actions?: Action[];
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
 * 自动预览的展示关系；来源可靠确认后提供 source_action_instance_id。
 *
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "automation".
 */
export interface Automation {
  purpose: "auto_preview";
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  source_action_instance_id?: EntityId & EntityId1;
}
/**
 * 动作结束后，设备仍在执行该动作要求的工作，或尚未确认执行结束。只表达冻结历史中的可靠事实。
 *
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "device_execution".
 */
export interface DeviceExecution {
  status: "still_running" | "end_unconfirmed";
  error?: Error;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "camera_result".
 */
export interface CameraResult {
  check?: Media & {
    check_status?: "completed" | "failed" | "unconfirmed";
    [k: string]: unknown;
  };
  repair?: Repair;
  discard_cleanup?: DiscardCleanup;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "obtain_failure".
 */
export interface ObtainFailure {
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  source_action_instance_id: EntityId & EntityId1;
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  output_id?: EntityId & EntityId1;
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  delivery_id?: EntityId & EntityId1;
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
  /**
   * 数据库对象 ID 的规范十进制字符串，整数范围为 1～9223372036854775807；19 位值另按上界约束校验。
   *
   * This interface was referenced by `Camctl`'s JSON-Schema
   * via the `definition` "entity_id".
   */
  report_id: EntityId & EntityId1;
}
/**
 * This interface was referenced by `Camctl`'s JSON-Schema
 * via the `definition` "admission_failure".
 */
export interface AdmissionFailure {
  status: "failed";
  error: {
    stage: "admission";
    [k: string]: unknown;
  };
  [k: string]: unknown;
}
