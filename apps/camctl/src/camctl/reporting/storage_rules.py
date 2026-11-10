"""报告与同步保存所需的纯事实规则。"""

from collections.abc import Mapping

from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.reporting.ack import AckReport
from camctl.reporting.models import validate_report_management


class ReportStorageRuleError(ValueError):
    """报告或同步的直接业务事实不成立。"""


def validate_report_creation(report_id, after: Mapping, first_event_id: int) -> None:
    validate_report_management(after)
    AckReport(report_id, after.get("from_wm"), after.get("to_wm"), after.get("frozen_event_id"))
    version = after.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ReportStorageRuleError("报告格式版本不受支持")
    if after["frozen_event_id"] >= first_event_id:
        raise ReportStorageRuleError("冻结依据必须早于本次登记事务")
    if after.get("status") != 1:
        raise ReportStorageRuleError("冻结创建的状态必须是 REGISTERED")
    if after["last_error_json"] is not None:
        raise ReportStorageRuleError("新登记报告尚无文件处理错误")


def validate_report_change(reason, event_id, before: Mapping, after: Mapping,
                           old_bytes, new_bytes, *, intent_reason, publish_reason) -> None:
    if old_bytes is not None and new_bytes != old_bytes:
        raise ConsistencyError("报告首次确定的字节依据不能改变")
    if reason == intent_reason and not json_equal(after["last_error_json"], before["last_error_json"]):
        raise ConsistencyError("发布意图不能提前清除文件处理错误")
    if reason == publish_reason:
        if after["publication_count"] != before["publication_count"] + 1:
            raise ConsistencyError("每次可靠发布必须恰好增加一次成功计数")
        if after["last_published_event_id"] != event_id:
            raise ConsistencyError("本次发布依据必须引用当前事件")


def validate_sync_end_reference(ended_event_id, event_id) -> None:
    if ended_event_id != event_id:
        raise ReportStorageRuleError("同步结束依据必须是本事件")


def validate_sync_action_started(action_id, facts) -> None:
    if facts is None:
        raise ReportStorageRuleError(f"同步建立缺少动作事实: actions#{action_id!r}")
    if (facts.get("status") != 2 or facts.get("execution_started") != 1
            or facts.get("cancel_requested") != 0):
        raise ReportStorageRuleError(
            "同步责任要求发起动作已进入运行且未取消:"
            f" actions#{action_id} status={facts.get('status')!r}")
