"""清理取消分支与接手前置判定的决策表单元测试。

取消分支覆盖《取消后删除结果与错误的保存》分类表：已有终态保持、
未发出删除解除限制、在途调用先跟踪收场、收场后按文件结论保存成
功/未知/仍在。接手前置覆盖《接手时的文件事实与后续操作》：已清
理完成直接复用、前次删除效果未知先核实、否则直接删除。
"""

from __future__ import annotations

import pytest

from camctl.outputs.cleanup_flow import (
    CleanupCancelChoice,
    CleanupCancelFacts,
    CleanupEntryChoice,
    CleanupEntryFacts,
    decide_cleanup_cancel,
    decide_cleanup_entry,
)

# cleanup_items.status 的登记编号。
_UNRESOLVED, _PENDING_DELETE, _DELETING = 1, 2, 3
_SUCCEEDED, _FAILED, _CANCELED = 4, 5, 6


class TestDecideCleanupCancel:
    @pytest.mark.parametrize("status", [_SUCCEEDED, _FAILED, _CANCELED])
    def test_terminal_members_keep_original_result(self, status):
        assert decide_cleanup_cancel(CleanupCancelFacts(
            status=status, delete_issued=True, call_settled=True,
            file_absent=False, file_present=False,
        )) is CleanupCancelChoice.ALREADY

    @pytest.mark.parametrize("status", [_UNRESOLVED, _PENDING_DELETE])
    def test_issued_never_releases_reversible_restriction(self, status):
        assert decide_cleanup_cancel(CleanupCancelFacts(
            status=status, delete_issued=False, call_settled=True,
            file_absent=False, file_present=False,
        )) is CleanupCancelChoice.RELEASE

    def test_unissued_but_delete_recorded_is_inconsistent(self):
        with pytest.raises(ValueError):
            decide_cleanup_cancel(CleanupCancelFacts(
                status=_PENDING_DELETE, delete_issued=True,
                call_settled=True, file_absent=False, file_present=False))

    def test_in_flight_call_is_tracked_to_settlement(self):
        assert decide_cleanup_cancel(CleanupCancelFacts(
            status=_DELETING, delete_issued=True, call_settled=False,
            file_absent=False, file_present=False,
        )) is CleanupCancelChoice.TRACKING

    def test_settled_with_completion_evidence_succeeds(self):
        assert decide_cleanup_cancel(CleanupCancelFacts(
            status=_DELETING, delete_issued=True, call_settled=True,
            file_absent=True, file_present=False,
        )) is CleanupCancelChoice.SUCCEED

    def test_settled_with_file_still_present_reports_delete_failed(self):
        assert decide_cleanup_cancel(CleanupCancelFacts(
            status=_DELETING, delete_issued=True, call_settled=True,
            file_absent=False, file_present=True,
        )) is CleanupCancelChoice.DELETE_FAILED

    def test_settled_with_unknown_effect_keeps_restriction(self):
        assert decide_cleanup_cancel(CleanupCancelFacts(
            status=_DELETING, delete_issued=True, call_settled=True,
            file_absent=False, file_present=False,
        )) is CleanupCancelChoice.UNCONFIRMED

    def test_absent_and_present_simultaneously_is_rejected(self):
        with pytest.raises(ValueError):
            decide_cleanup_cancel(CleanupCancelFacts(
                status=_DELETING, delete_issued=True, call_settled=True,
                file_absent=True, file_present=True))


class TestDecideCleanupEntry:
    def test_reliable_completion_is_reused_without_calls(self):
        assert decide_cleanup_entry(CleanupEntryFacts(
            output_cleaned=True, unresolved_delete=True, file_absent=True,
        )) is CleanupEntryChoice.USE_COMPLETED

    def test_absent_file_confirms_without_new_calls(self):
        assert decide_cleanup_entry(CleanupEntryFacts(
            output_cleaned=False, unresolved_delete=True, file_absent=True,
        )) is CleanupEntryChoice.USE_COMPLETED

    def test_unresolved_prior_delete_verifies_before_deleting(self):
        assert decide_cleanup_entry(CleanupEntryFacts(
            output_cleaned=False, unresolved_delete=True, file_absent=False,
        )) is CleanupEntryChoice.VERIFY_FIRST

    def test_clean_state_deletes_directly(self):
        assert decide_cleanup_entry(CleanupEntryFacts(
            output_cleaned=False, unresolved_delete=False, file_absent=False,
        )) is CleanupEntryChoice.DIRECT_DELETE
