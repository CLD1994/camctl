# 数据库整数编号一览

[定义与检查规则](common.md#状态与类型的整数枚举) · [数据库目录](../database-schema.md)

本页由 `scripts/check-database-spec.py --write-enum-doc` 从权威定义生成，不单独修改编号。成员的业务含义、空值和转换条件以各表的正式章节为准。

## 普通列

### `actions.expiration_reason`

[行为说明](plans-actions.md)

| 编号 | 成员 |
| --- | --- |
| 1 | `WINDOW_MISSED` |
| 2 | `WINDOW_EXHAUSTED` |

### `actions.source_resolution_state`

[行为说明](plans-actions.md)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `FIXED` |
| 3 | `FAILED` |

### `actions.status`

[行为说明](plans-actions.md)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `RUNNING` |
| 3 | `SUCCEEDED` |
| 4 | `FAILED` |
| 5 | `EXPIRED` |
| 6 | `CANCELED` |

### `actions.target_selection_state`

[行为说明](plans-actions.md)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `FIXED` |
| 3 | `FAILED` |

### `actions.type`

[行为说明](plans-actions.md)

| 编号 | 成员 |
| --- | --- |
| 1 | `CAMERA_TAKE_PHOTO` |
| 2 | `CAMERA_RECORD` |
| 3 | `CAMERA_TIMELAPSE` |
| 4 | `OBTAIN_ACTION_OUTPUTS` |
| 5 | `DELETE_ACTION_OUTPUTS` |
| 6 | `CANCEL_TASK` |
| 7 | `REPORT_STATUS` |
| 8 | `MOTOR_CONTROL` |

### `cancel_delivery_items.status`

[行为说明](workflow-fields.md#取消目标与交付项)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `WITHDRAWN` |
| 3 | `NOT_RETRACTABLE` |
| 4 | `FAILED` |

### `cancel_items.cancellation_effect`

[行为说明](workflow-fields.md#取消目标与交付项)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_APPLIED` |
| 2 | `APPLIED` |
| 3 | `NOT_REQUIRED` |

### `cancel_items.outcome`

[行为说明](workflow-fields.md#取消目标与交付项)

| 编号 | 成员 |
| --- | --- |
| 1 | `CANCELED` |
| 2 | `ALREADY_TERMINAL` |

### `cancel_items.selection_basis`

[行为说明](workflow-fields.md#取消目标与交付项)

| 编号 | 成员 |
| --- | --- |
| 1 | `DIRECT` |
| 2 | `AUTO_PREVIEW` |
| 3 | `BOTH` |

### `cancel_items.status`

[行为说明](workflow-fields.md#取消目标与交付项)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `RUNNING` |
| 3 | `SUCCEEDED` |
| 4 | `FAILED` |
| 5 | `CANCELED` |

### `cleanup_items.outcome`

[行为说明](workflow-fields.md#清理目标字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `DELETED` |
| 2 | `ALREADY_CLEANED` |
| 3 | `ABSENCE_CONFIRMED` |

### `cleanup_items.restriction_state`

[行为说明](workflow-fields.md#清理目标字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_ESTABLISHED` |
| 2 | `ACTIVE` |
| 3 | `RELEASED` |
| 4 | `IRREVERSIBLE` |

### `cleanup_items.status`

[行为说明](workflow-fields.md#清理目标字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNRESOLVED` |
| 2 | `PENDING_DELETE` |
| 3 | `DELETING` |
| 4 | `SUCCEEDED` |
| 5 | `FAILED` |
| 6 | `CANCELED` |

### `deliveries.status`

[行为说明](file-fields.md#普通交付)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `PREPARING` |
| 3 | `PREPARED` |
| 4 | `PUBLISHING` |
| 5 | `PUBLISHED` |
| 6 | `FAILED` |
| 7 | `CANCELED` |
| 8 | `WITHDRAWN` |

### `deliveries.withdrawal_state`

[行为说明](file-fields.md#普通交付)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_REQUESTED` |
| 2 | `PENDING` |
| 3 | `WITHDRAWN` |
| 4 | `NOT_RETRACTABLE` |
| 5 | `FAILED` |
| 6 | `UNKNOWN` |

### `device_activities.activity_state`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNKNOWN` |
| 2 | `ACTIVE` |
| 3 | `ENDED` |

### `device_activities.baseline_state`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_REQUIRED` |
| 2 | `COLLECTING` |
| 3 | `FIXED` |

### `device_activities.completion_basis`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNDETERMINED` |
| 2 | `DEVICE_EVIDENCE` |
| 3 | `TIME_AND_OUTPUTS` |
| 4 | `KNOWN_FAILURE` |

### `device_activities.completion_mode`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `DEVICE_EVIDENCE` |
| 2 | `TIME_AND_OUTPUTS` |

### `device_activities.dispatch_state`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_DISPATCHED` |
| 2 | `MAY_HAVE_DISPATCHED` |
| 3 | `SUCCESS_RETURNED` |
| 4 | `REJECTED_WITHOUT_EFFECT` |

### `device_activities.occupancy_state`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `HELD` |
| 2 | `RELEASED` |

### `device_activities.ownership_mode`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `TASK_SCOPE` |
| 2 | `BASELINE_COMPARISON` |

### `device_activities.result_set_state`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNEXAMINED` |
| 2 | `CHECKING` |
| 3 | `COMPLETE` |
| 4 | `UNCONFIRMED` |

### `device_activities.start_return_meaning`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `SENT` |
| 2 | `STARTED` |
| 3 | `COMPLETED` |

### `device_files.checksum_support`

[行为说明](file-fields.md#设备文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNDETERMINED` |
| 2 | `SUPPORTED` |
| 3 | `UNSUPPORTED` |

### `device_files.completion_state`

[行为说明](file-fields.md#设备文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNKNOWN` |
| 2 | `WRITING` |
| 3 | `COMPLETE` |
| 4 | `UNCONFIRMED` |

### `device_files.presence_state`

[行为说明](file-fields.md#设备文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNKNOWN` |
| 2 | `PRESENT` |
| 3 | `ABSENT` |

### `device_files.role`

[行为说明](file-fields.md#设备文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNDETERMINED` |
| 2 | `ORIGINAL` |
| 3 | `PREVIEW` |

### `file_copies.reset_state`

[行为说明](file-fields.md#文件拷贝)

| 编号 | 成员 |
| --- | --- |
| 1 | `READY` |
| 2 | `RESET_PENDING` |

### `file_copies.verification_state`

[行为说明](file-fields.md#文件拷贝)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_PERFORMED` |
| 2 | `RUNNING` |
| 3 | `MATCHED` |
| 4 | `MISMATCHED` |
| 5 | `SOURCE_CHECKSUM_UNAVAILABLE` |
| 6 | `FAILED` |

### `history_events.clock_status`

[行为说明](history-formats.md#历史事务与事件字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNCHECKED` |
| 2 | `TRUSTED` |
| 3 | `INVALID` |

### `intermediate_files.cleanup_state`

[行为说明](file-fields.md#中间文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_NEEDED` |
| 2 | `PENDING` |
| 3 | `RUNNING` |
| 4 | `COMPLETED` |
| 5 | `FAILED` |
| 6 | `UNKNOWN` |

### `intermediate_files.purpose`

[行为说明](file-fields.md#中间文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `DELIVERY_COPY` |
| 2 | `RECORDING_INPUT` |
| 3 | `PROCESSING_TEMP` |
| 4 | `REPAIR_OUTPUT` |

### `intermediate_files.retention_state`

[行为说明](file-fields.md#中间文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `REQUIRED` |
| 2 | `RELEASABLE` |
| 3 | `PROMOTED` |
| 4 | `HANDED_OFF` |

### `obtain_items.basis`

[行为说明](workflow-fields.md#取回目标字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `ORIGINAL` |
| 2 | `REPAIRED` |
| 3 | `PREVIEW` |
| 4 | `REPAIRED_NOT_LARGER` |
| 5 | `EXPLICIT` |

### `obtain_items.status`

[行为说明](workflow-fields.md#取回目标字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNRESOLVED` |
| 2 | `SELECTED` |
| 3 | `DELIVERY_CREATED` |
| 4 | `FAILED` |
| 5 | `CANCELED` |

### `obtain_source_selections.status`

[行为说明](workflow-fields.md#每个来源的文件选择)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `FIXED` |

### `operation_attempts.effect_state`

[行为说明](operation-fields.md#尝试字段与结果)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNKNOWN` |
| 2 | `NO_EFFECT` |
| 3 | `CONFIRMED` |

### `operation_attempts.status`

[行为说明](operation-fields.md#尝试字段与结果)

| 编号 | 成员 |
| --- | --- |
| 1 | `RUNNING` |
| 2 | `SUCCEEDED` |
| 3 | `FAILED` |
| 4 | `UNKNOWN` |

### `operation_runs.kind`

[行为说明](operation-fields.md#操作流程的身份与参数)

| 编号 | 成员 |
| --- | --- |
| 1 | `START` |
| 2 | `STOP` |
| 3 | `READ_FILE` |
| 4 | `DELETE_FILE` |
| 5 | `CHECK_FILE_EXISTS` |
| 6 | `QUERY_ACTIVITY` |
| 7 | `CHECK_CAPTURE_RESULTS` |
| 8 | `STOP_RESIDUAL` |
| 9 | `EMERGENCY_STOP` |

### `operation_runs.query_purpose`

[行为说明](operation-fields.md#操作流程的身份与参数)

| 编号 | 成员 |
| --- | --- |
| 1 | `BEFORE_EXECUTION` |
| 2 | `START_CONFIRMATION` |
| 3 | `ACTIVITY_OBSERVATION` |
| 4 | `STOP_CONFIRMATION` |
| 5 | `RESIDUAL_STOP_CONFIRMATION` |

### `operation_runs.status`

[行为说明](operation-fields.md#操作流程的身份与参数)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `ACTIVE` |
| 3 | `SUCCEEDED` |
| 4 | `FAILED` |
| 5 | `CANCELED` |
| 6 | `UNCONFIRMED` |
| 7 | `EXPIRED` |

### `outputs.availability`

[行为说明](file-fields.md#正式产物与来源)

| 编号 | 成员 |
| --- | --- |
| 1 | `AVAILABLE` |
| 2 | `RESTRICTED` |
| 3 | `CLEANED` |
| 4 | `MISSING` |
| 5 | `UNKNOWN` |

### `outputs.cleanup_status`

[行为说明](file-fields.md#正式产物与来源)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_REQUESTED` |
| 2 | `PENDING` |
| 3 | `RUNNING` |
| 4 | `COMPLETED` |
| 5 | `INCOMPLETE` |
| 6 | `CANCELED` |

### `outputs.kind`

[行为说明](file-fields.md#正式产物与来源)

| 编号 | 成员 |
| --- | --- |
| 1 | `ORIGINAL` |
| 2 | `REPAIRED` |
| 3 | `PREVIEW` |

### `plans.status`

[行为说明](plans-actions.md#计划与动作的运行状态)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `RUNNING` |
| 3 | `COMPLETED` |

### `recording_processing.check_decision`

[行为说明](operation-fields.md#录像内部处理)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNDETERMINED` |
| 2 | `NOT_NEEDED` |
| 3 | `REQUIRED` |

### `recording_processing.check_state`

[行为说明](operation-fields.md#录像内部处理)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_PERFORMED` |
| 2 | `RUNNING` |
| 3 | `COMPLETED` |
| 4 | `FAILED` |
| 5 | `UNCONFIRMED` |

### `recording_processing.discard_state`

[行为说明](operation-fields.md#录像内部处理)

| 编号 | 成员 |
| --- | --- |
| 1 | `NOT_NEEDED` |
| 2 | `PENDING` |
| 3 | `RUNNING` |
| 4 | `COMPLETED` |
| 5 | `FAILED` |
| 6 | `UNKNOWN` |

### `recording_processing.repair_state`

[行为说明](operation-fields.md#录像内部处理)

| 编号 | 成员 |
| --- | --- |
| 1 | `UNDETERMINED` |
| 2 | `NOT_NEEDED` |
| 3 | `PENDING` |
| 4 | `RUNNING` |
| 5 | `SUCCEEDED` |
| 6 | `FAILED` |
| 7 | `CANCELED` |

### `reports.status`

[行为说明](reports-runtime.md#报告字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `REGISTERED` |
| 2 | `PREPARED` |
| 3 | `PUBLISHING` |
| 4 | `PUBLISHED` |
| 5 | `FAILED` |

### `state_syncs.mode`

[行为说明](reports-runtime.md#同步字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `FULL` |
| 2 | `INCREMENTAL` |

### `state_syncs.status`

[行为说明](reports-runtime.md#同步字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `OUTSTANDING` |
| 2 | `ACKNOWLEDGED` |
| 3 | `CANCELED` |

### `motor_notifications.outcome`

[行为说明](workflow-fields.md#电机发送事实)

| 编号 | 成员 |
| --- | --- |
| 1 | `PENDING` |
| 2 | `NOT_SENT` |
| 3 | `WRITTEN` |
| 4 | `FAILED` |
| 5 | `UNCONFIRMED` |

## JSON 中的整数分类

### `device_files.ownership_evidence_json.method`

[行为说明](file-fields.md#设备文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `TASK_SCOPE` |
| 2 | `BASELINE_DIFFERENCE` |
| 3 | `DRIVER_TASK_ASSOCIATION` |

### `device_files.completion_evidence_json.basis`

[行为说明](file-fields.md#设备文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `DEVICE_GUARANTEE` |
| 2 | `TIME_AND_OUTPUTS` |

### `device_files.pairing_evidence_json.method`

[行为说明](file-fields.md#设备文件)

| 编号 | 成员 |
| --- | --- |
| 1 | `DRIVER_PAIRING` |

### `device_activities.result_check_json.outcome`

[行为说明](operation-fields.md#设备活动字段)

| 编号 | 成员 |
| --- | --- |
| 1 | `SATISFIED` |
| 2 | `NOT_SATISFIED` |
| 3 | `UNCONFIRMED` |

### `recording_processing.check_basis_json.reason`

[行为说明](operation-fields.md#录像内部处理)

| 编号 | 成员 |
| --- | --- |
| 1 | `CONTINUOUS_CONTROL_COMPLETE` |
| 2 | `INSUFFICIENT_TIMING` |
| 3 | `EXCESS_DURATION_CHECK` |

### `recording_processing.repair_basis_json.reason`

[行为说明](operation-fields.md#录像内部处理)

| 编号 | 成员 |
| --- | --- |
| 1 | `BELOW_THRESHOLD` |
| 2 | `THRESHOLD_REACHED` |
| 3 | `NO_USABLE_INPUT` |
| 4 | `CANCELED` |

## 动作及逐项错误

编号、公共错误码、阶段与详情共用[公共错误定义](../../../protocol/errors/workflow-codes.json)。详情由对应条目的 `details_schema` 定义。

### `actions.error_code`

| 编号 | 公共错误码 | 阶段 |
| --- | --- | --- |
| 1 | `action_validation_failed` | `admission` |
| 2 | `source_action_not_found` | `admission` |
| 3 | `duplicate_auto_preview` | `admission` |
| 4 | `preview_not_supported` | `admission` |
| 10 | `start_attempts_exhausted` | `device_start` |
| 11 | `device_start_failed` | `execution` |
| 12 | `capture_result_unconfirmed` | `execution` |
| 13 | `capture_failed` | `execution` |
| 14 | `recording_stop_failed` | `device_stop` |
| 15 | `recording_too_short` | `execution` |
| 16 | `recording_processing_failed` | `execution` |
| 17 | `device_binding_unavailable` | `execution` |
| 18 | `device_activity_unresolved` | `execution` |
| 20 | `source_resolution_failed` | `execution` |
| 21 | `obtain_items_failed` | `execution` |
| 22 | `cleanup_items_failed` | `execution` |
| 23 | `cancel_items_failed` | `execution` |
| 24 | `cancel_self_target` | `execution` |
| 25 | `cancel_target_not_found` | `execution` |
| 26 | `sync_report_not_found` | `execution` |
| 27 | `motor_channel_unavailable` | `execution` |
| 28 | `motor_notification_failed` | `execution` |
| 29 | `motor_notification_unconfirmed` | `execution` |

### `cancel_delivery_items.error_code`

| 编号 | 公共错误码 | 阶段 |
| --- | --- | --- |
| 1 | `delivery_withdrawal_failed` | `publication` |
| 2 | `delivery_position_unconfirmed` | `publication` |

### `cancel_items.error_code`

| 编号 | 公共错误码 | 阶段 |
| --- | --- | --- |
| 1 | `task_cancel_unsupported` | `execution` |
| 2 | `target_cleanup_failed` | `execution` |
| 3 | `cancel_withdrawal_failed` | `execution` |
| 4 | `device_binding_unavailable` | `execution` |
| 5 | `cancel_result_unconfirmed` | `execution` |

### `cleanup_items.error_code`

| 编号 | 公共错误码 | 阶段 |
| --- | --- | --- |
| 1 | `output_not_found` | `output_selection` |
| 2 | `delete_attempts_exhausted` | `cleanup` |
| 3 | `file_query_attempts_exhausted` | `cleanup` |
| 4 | `delete_unconfirmed` | `cleanup` |
| 5 | `device_binding_unavailable` | `execution` |
| 6 | `file_delete_failed` | `cleanup` |

### `obtain_items.error_code`

| 编号 | 公共错误码 | 阶段 |
| --- | --- | --- |
| 1 | `output_not_found` | `output_selection` |
| 2 | `output_source_mismatch` | `output_selection` |
| 3 | `output_unavailable` | `output_selection` |
| 4 | `output_cleanup_started` | `output_selection` |
| 5 | `device_binding_unavailable` | `execution` |
| 6 | `source_file_unconfirmed` | `source_read` |
| 8 | `preview_missing` | `output_selection` |

### `obtain_source_selections.error_code`

| 编号 | 公共错误码 | 阶段 |
| --- | --- | --- |
| 1 | `no_outputs` | `output_selection` |
| 2 | `preview_missing` | `output_selection` |

## 其他编号来源

事件类型、事件版本及分支编号见[事件种类与分支](history-formats.md#事件正文版本-1)。历史对象类型及资格见[对象目录与归属](history-formats.md#对象目录与归属)。布尔列和固定列的分类见[整数编号定义](enum-registry.json)；它们不作为业务状态枚举。
