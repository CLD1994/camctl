"""目录切换资格判定与提交观察的单元测试。

SwitchFacts 与提交观察是纯决策：数据库责任分类按阻止规则给出首
个原因，提交结果未知后的三路径重读按完整绑定分类，不混用新旧
目录。
"""

from __future__ import annotations

import pytest

from camctl.persistence.directory_switch import (
    SwitchCommitObservation,
    SwitchFacts,
    classify_commit_observation,
    classify_switch_eligibility,
)


def _facts(**overrides) -> SwitchFacts:
    base = dict(
        required_intermediates=0,
        pending_cleanups=0,
        promoted_pending_output_cleanup=0,
        unfinished_runs=0,
        open_deliveries=0,
        open_withdrawals=0,
        open_reports=0,
    )
    base.update(overrides)
    return SwitchFacts(**base)


class TestClassifySwitchEligibility:
    def test_no_liability_allows_switch(self) -> None:
        assert classify_switch_eligibility(_facts()) is None

    @pytest.mark.parametrize(
        "field,keyword",
        [
            ("required_intermediates", "REQUIRED"),
            ("pending_cleanups", "清理未完成"),
            ("promoted_pending_output_cleanup", "尚未清理"),
            ("unfinished_runs", "运行尚未结束"),
            ("open_deliveries", "交付"),
            ("open_withdrawals", "撤回"),
            ("open_reports", "报告"),
        ],
    )
    def test_each_liability_class_blocks(self, field: str, keyword: str) -> None:
        reason = classify_switch_eligibility(_facts(**{field: 1}))
        assert reason is not None
        assert keyword in reason

    def test_first_blocking_class_names_the_diagnostics(self) -> None:
        # 多个责任并存时任一阻止；诊断按检查顺序报告首个原因。
        reason = classify_switch_eligibility(
            _facts(required_intermediates=2, open_reports=1))
        assert reason is not None
        assert "REQUIRED" in reason


class TestClassifyCommitObservation:
    OLD = ("s1", "r1", "p1")
    NEW = ("s2", "r2", "p2")

    def test_all_new_paths_mean_completed(self) -> None:
        assert (classify_commit_observation(
            self.NEW, self.OLD, self.NEW)
            is SwitchCommitObservation.COMPLETED)

    def test_all_old_paths_mean_not_completed(self) -> None:
        assert (classify_commit_observation(
            self.OLD, self.OLD, self.NEW)
            is SwitchCommitObservation.NOT_COMPLETED)

    def test_mixed_combination_is_inconsistent(self) -> None:
        assert (classify_commit_observation(
            ("s2", "r1", "p1"), self.OLD, self.NEW)
            is SwitchCommitObservation.INCONSISTENT)
