"""清理成员进入删除及保存成功结果的业务条件。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.outputs.cleanup_flow import (
    CleanupItemDisposition, CleanupOutcomeChoice, decide_cleanup_progress, validate_cleanup_success,
)

STATUS = enum_for("cleanup_items.status")
RESTRICTION = enum_for("cleanup_items.restriction_state")
AVAILABILITY = enum_for("outputs.availability")


@pytest.mark.parametrize("status,restriction,irreversible,expected", [
    (STATUS.PENDING_DELETE, RESTRICTION.ACTIVE, False, CleanupItemDisposition.SAVED),
    (STATUS.DELETING, RESTRICTION.ACTIVE, False, CleanupItemDisposition.ALREADY),
    (STATUS.DELETING, RESTRICTION.ACTIVE, True, CleanupItemDisposition.SAVED),
    (STATUS.DELETING, RESTRICTION.IRREVERSIBLE, True, CleanupItemDisposition.ALREADY),
])
def test_cleanup_progress_keeps_original_phase_and_restriction(status, restriction, irreversible, expected):
    assert decide_cleanup_progress(item={"status": int(status), "restriction_state": int(restriction)},
                                   irreversible=irreversible) is expected


def test_cleanup_progress_checks_status_before_restriction():
    with pytest.raises(ConsistencyError, match="不可撤销事实要求成员处于删除中"):
        decide_cleanup_progress(item={"status": int(STATUS.PENDING_DELETE)}, irreversible=True)
    with pytest.raises(ConsistencyError, match="不可撤销事实要求本项限制仍可撤销"):
        decide_cleanup_progress(item={"status": int(STATUS.DELETING), "restriction_state": 1}, irreversible=True)
    assert decide_cleanup_progress(item={"status": int(STATUS.DELETING)}, irreversible=False) is CleanupItemDisposition.ALREADY


def test_pending_cleanup_success_requires_prior_completion_or_absence():
    item = {"status": int(STATUS.PENDING_DELETE), "restriction_state": int(RESTRICTION.ACTIVE)}
    assert validate_cleanup_success(item=item, outcome=CleanupOutcomeChoice.ALREADY_CLEANED,
                                    output={"availability": int(AVAILABILITY.CLEANED)})
    assert validate_cleanup_success(item=item, outcome=CleanupOutcomeChoice.ABSENCE_CONFIRMED, output={})
    with pytest.raises(ConsistencyError, match="没有本项删除调用"):
        validate_cleanup_success(item=item, outcome=CleanupOutcomeChoice.DELETED, output={})
    with pytest.raises(ConsistencyError, match="产物已可靠清理"):
        validate_cleanup_success(item=item, outcome=CleanupOutcomeChoice.ALREADY_CLEANED,
                                 output={"availability": int(AVAILABILITY.AVAILABLE)})


@pytest.mark.parametrize("restriction", [RESTRICTION.ACTIVE, RESTRICTION.IRREVERSIBLE])
def test_deleting_cleanup_can_finish_with_actual_delete(restriction):
    item = {"status": int(STATUS.DELETING), "restriction_state": int(restriction)}
    assert not validate_cleanup_success(item=item, outcome=CleanupOutcomeChoice.DELETED, output={})


def test_cleanup_success_rejects_wrong_phase_before_reading_restriction():
    with pytest.raises(ConsistencyError, match="清理成功要求成员处于删除中"):
        validate_cleanup_success(item={"status": int(STATUS.UNRESOLVED)},
                                 outcome=CleanupOutcomeChoice.DELETED, output={})


def test_device_absence_requires_prior_file_fact_for_waiting_item():
    from camctl.outputs.cleanup_flow import cleanup_device_absence_needs_record
    presence = enum_for("device_files.presence_state")
    assert cleanup_device_absence_needs_record(item_status=int(STATUS.DELETING), presence_state=int(presence.PRESENT))
    assert not cleanup_device_absence_needs_record(item_status=int(STATUS.PENDING_DELETE), presence_state=int(presence.ABSENT))
    with pytest.raises(ConsistencyError, match="缺席事实先行"):
        cleanup_device_absence_needs_record(item_status=int(STATUS.PENDING_DELETE), presence_state=int(presence.PRESENT))
    with pytest.raises(ConsistencyError, match="文件存在性未知"):
        cleanup_device_absence_needs_record(item_status=int(STATUS.DELETING), presence_state=int(presence.UNKNOWN))


@pytest.mark.parametrize("state_name", ["NOT_NEEDED", "PENDING"])
def test_local_absence_requires_prior_file_fact_for_waiting_item(state_name):
    from camctl.outputs.cleanup_flow import cleanup_local_absence_needs_record
    cleanup = enum_for("intermediate_files.cleanup_state")
    state = int(cleanup[state_name])
    assert cleanup_local_absence_needs_record(item_status=int(STATUS.DELETING), cleanup_state=state)
    with pytest.raises(ConsistencyError, match="缺席事实先行"):
        cleanup_local_absence_needs_record(item_status=int(STATUS.PENDING_DELETE), cleanup_state=state)
    assert not cleanup_local_absence_needs_record(item_status=int(STATUS.PENDING_DELETE), cleanup_state=int(cleanup.COMPLETED))
    with pytest.raises(ConsistencyError, match="清理状态未决"):
        cleanup_local_absence_needs_record(item_status=int(STATUS.DELETING), cleanup_state=int(cleanup.UNKNOWN))
