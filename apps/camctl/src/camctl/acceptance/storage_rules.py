"""受理创建时对动作和计划事实的业务规则。"""

from collections.abc import Mapping

from camctl.acceptance.definitions import read_action_spec
from camctl.contracts.workflow_errors import registered_error_spec, validate_error_details


def validate_admitted_action(values: Mapping) -> None:
    """校验一个新动作的已物化事实；不修改输入。"""
    read_action_spec(values)
    status = values.get("status")
    if status == 4:
        if values.get("execution_started") != 0 or values.get("error_code") is None:
            raise ValueError("受理失败动作必须 execution_started=0 且携带 admission 错误")
        if values.get("execution_spec_json") is not None:
            raise ValueError("受理失败动作不携带执行定义")
        try:
            code, spec = registered_error_spec("action_error_id", values["error_code"])
            if spec["stage"] != "admission":
                raise ValueError("首次受理失败必须使用受理阶段错误")
            validate_error_details(code, values["error_details_json"])
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError(str(error)) from error
    elif status == 1:
        if values.get("execution_spec_json") is None:
            raise ValueError("可受理动作必须携带执行定义")


def validate_initial_plan_status(status: object, action_statuses: tuple) -> None:
    """按创建时已物化的所属动作状态校验新计划状态。"""
    if not isinstance(action_statuses, tuple):
        raise TypeError("动作状态必须是已物化的元组")
    expected = 3 if action_statuses and set(action_statuses) == {4} else 1
    if status != expected:
        raise ValueError(f"计划状态与动作聚合不符: 期望 {expected}")


class AcceptanceStorageRuleError(ValueError):
    """受理保存事实违反对应业务约束。"""


def validate_fixed_source(values: Mapping) -> None:
    if values.get("source_resolution_state") == 2 and values.get("resolved_source_plan_id") is None:
        raise AcceptanceStorageRuleError("FIXED 来源必须保存真实来源计划")


def validate_admitted_source_owners(owner_ids: tuple, event_type: int, reason: int, has_reads: bool):
    if not owner_ids:
        if event_type == 3 and reason == 3:
            raise AcceptanceStorageRuleError("受理零成员无需空事件")
        return None
    if event_type != 3 or reason != 3 or not has_reads:
        raise AcceptanceStorageRuleError("来源成员缺少可靠执行申请或真实受理前序")
    owner_id = owner_ids[0]
    if any(identity != owner_id for identity in owner_ids):
        raise AcceptanceStorageRuleError("受理成员事件只能保存一个真实 owner 的完整成员")
    return owner_id


def validate_admitted_source_owner_type(action_type, allowed_types: frozenset) -> None:
    if action_type not in allowed_types:
        raise AcceptanceStorageRuleError("来源成员所属动作类型不适用")


def validate_admitted_source_member(actual, expected) -> None:
    if actual is None or expected is None or actual != expected:
        raise AcceptanceStorageRuleError("受理来源成员没有完整覆盖原 source 的真实前序拍摄动作")


def validate_diagnostic_key(present: bool) -> None:
    if not present:
        raise AcceptanceStorageRuleError("输入诊断必须携带 input_key 依据")


def validate_diagnostic_errors(errors) -> None:
    if not isinstance(errors, list) or not errors:
        raise AcceptanceStorageRuleError("输入诊断必须保存非空错误列表")


def validate_acknowledged_sync_status(after: Mapping, acknowledged_status) -> None:
    if after.get("status") != acknowledged_status:
        raise AcceptanceStorageRuleError("ACK 同步结束必须保存确认状态")


def validate_acknowledged_sync(sync_id, event_id, after: Mapping, sync, report) -> None:
    from camctl.reporting.ack import AckReport, SyncResponsibility, qualifies_sync

    try:
        basis = AckReport(after.get("ack_report_id"), report["from_wm"], report["to_wm"], report["frozen_event_id"])
        responsibility = SyncResponsibility(sync_id, sync["action_id"], sync["from_wm"], sync["started_boundary_event_id"])
    except (ValueError, KeyError) as error:
        raise AcceptanceStorageRuleError("同步确认依据无法可靠解释") from error
    if not qualifies_sync(basis, responsibility):
        raise AcceptanceStorageRuleError("ACK 报告未满足同步起点与开始历史边界")
    if after.get("ended_event_id") != event_id:
        raise AcceptanceStorageRuleError("同步结束依据必须是本事件")


def validate_acknowledged_watermark(before: Mapping, after: Mapping, report) -> None:
    report_id = after.get("acknowledged_report_id")
    new_wm = after.get("acknowledged_wm")
    if report is None:
        raise AcceptanceStorageRuleError(f"吸收的 ACK 引用不存在的报告: {report_id!r}")
    if report.get("to_wm") != new_wm:
        raise AcceptanceStorageRuleError("累计水位必须等于所吸收报告的覆盖终点")
    if not before.get("acknowledged_wm", 0) < new_wm:
        raise AcceptanceStorageRuleError("累计确认水位只严格提高")
