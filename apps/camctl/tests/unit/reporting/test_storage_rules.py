"""报告及同步保存规则只消费明确的业务事实。"""

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.reporting.storage_rules import (
    ReportStorageRuleError, validate_report_change, validate_report_creation,
    validate_sync_action_started, validate_sync_end_reference,
)


def _registered():
    return {"status": 1, "size_bytes": None, "sha256": None,
            "publication_count": 0, "last_published_event_id": None,
            "last_error_json": None, "from_wm": 0, "to_wm": 5,
            "frozen_event_id": 8, "format_version": 1}


def test_report_creation_checks_fixed_prior_boundary():
    report = _registered()
    validate_report_creation(3, report, 9)
    with pytest.raises(ReportStorageRuleError, match="冻结依据必须早于"):
        validate_report_creation(3, report, 8)
    with pytest.raises(ReportStorageRuleError, match="格式版本不受支持"):
        validate_report_creation(3, {**report, "format_version": True}, 9)


def test_report_change_preserves_bytes_and_counts_actual_publication():
    before = {"last_error_json": None, "publication_count": 1}
    after = {"last_error_json": None, "publication_count": 2, "last_published_event_id": 12}
    validate_report_change(4, 12, before, after, "same", "same", intent_reason=3, publish_reason=4)
    with pytest.raises(ConsistencyError, match="字节依据不能改变"):
        validate_report_change(4, 12, before, after, "old", "new", intent_reason=3, publish_reason=4)
    with pytest.raises(ConsistencyError, match="恰好增加一次"):
        validate_report_change(4, 12, before, {**after, "publication_count": 3}, "same", "same", intent_reason=3, publish_reason=4)
    with pytest.raises(ConsistencyError, match="引用当前事件"):
        validate_report_change(4, 13, before, after, "same", "same", intent_reason=3, publish_reason=4)


def test_sync_start_and_end_use_actual_action_and_event():
    validate_sync_action_started(9, {"status": 2, "execution_started": 1, "cancel_requested": 0})
    validate_sync_end_reference(12, 12)
    with pytest.raises(ReportStorageRuleError, match="缺少动作事实"):
        validate_sync_action_started(9, None)
    with pytest.raises(ReportStorageRuleError, match="进入运行且未取消"):
        validate_sync_action_started(9, {"status": 2, "execution_started": 1, "cancel_requested": 1})
    with pytest.raises(ReportStorageRuleError, match="必须是本事件"):
        validate_sync_end_reference(11, 12)
