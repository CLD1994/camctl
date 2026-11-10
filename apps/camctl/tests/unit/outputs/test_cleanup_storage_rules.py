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
