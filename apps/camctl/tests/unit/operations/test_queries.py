"""O4 独立查询责任与产物核实轮次的单元测试。

查询用途创建后不可改变；正常查询响应仍可能需要继续责任；一轮
分页只消耗一次核实轮次，重启未完成的轮次计新轮次；不支持状态
查询的任务不创建查询尝试；可靠停止证据已满足判断时不为流程形
式重复查询。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.operations.attempts import AttemptConfig, QueryPurpose, responsibility_key
from camctl.operations.models import AttemptStatus
from camctl.operations.queries import (
    CheckOutcome,
    CheckSet,
    QueryFacts,
    QueryFlowStatus,
    QueryNext,
    QueryResponsibility,
    ResultCheckRound,
    decide_query_next,
    finish_check_round,
    validate_query_scope,
)

_CONFIG = AttemptConfig(
    max_attempts=3, timeout_s=Decimal("5"), retry_interval_s=Decimal("1")
)


def _scope(purpose: QueryPurpose, *, action_id: int = 7, activity_id: int | None = 3):
    return QueryResponsibility(
        purpose=purpose,
        action_id=action_id,
        activity_id=activity_id,
        config=_CONFIG,
    )


class TestValidateQueryScope:
    @pytest.mark.parametrize("purpose,activity_id,expected", [
        (QueryPurpose.BEFORE_EXECUTION, None, "query/preflight/7"),
        (QueryPurpose.START_CONFIRMATION, 3, "query/start/7/3"),
        (QueryPurpose.ACTIVITY_OBSERVATION, 3, "query/activity/7/3"),
        (QueryPurpose.STOP_CONFIRMATION, 3, "query/stop/7/3"),
        (QueryPurpose.RESIDUAL_STOP_CONFIRMATION, 3, "query/residual/7/3"),
    ])
    def test_key_uses_fixed_query_identity(self, purpose, activity_id, expected) -> None:
        assert _scope(purpose, activity_id=activity_id).responsibility_key == expected

    def test_purpose_and_targets_are_immutable_identity(self) -> None:
        scope = _scope(QueryPurpose.STOP_CONFIRMATION)
        with pytest.raises(Exception):
            scope.purpose = QueryPurpose.START_CONFIRMATION  # type: ignore[misc]

    def test_preflight_has_no_activity(self) -> None:
        scope = _scope(QueryPurpose.BEFORE_EXECUTION, activity_id=None)
        validate_query_scope(scope)
        assert scope.responsibility_key == "query/preflight/7"

    def test_other_purposes_require_activity(self) -> None:
        for purpose in (
            QueryPurpose.START_CONFIRMATION,
            QueryPurpose.ACTIVITY_OBSERVATION,
            QueryPurpose.STOP_CONFIRMATION,
            QueryPurpose.RESIDUAL_STOP_CONFIRMATION,
        ):
            scope = _scope(purpose)
            validate_query_scope(scope)
            assert scope.responsibility_key.endswith("/7/3")
        with pytest.raises(ValueError):
            validate_query_scope(
                _scope(QueryPurpose.STOP_CONFIRMATION, activity_id=None)
            )

    def test_config_must_be_complete_for_queries(self) -> None:
        with pytest.raises(ValueError):
            validate_query_scope(
                QueryResponsibility(
                    purpose=QueryPurpose.STOP_CONFIRMATION,
                    action_id=7,
                    activity_id=3,
                    config=AttemptConfig(max_attempts=3, timeout_s=Decimal("5")),
                )
            )

    def test_scope_key_matches_registry_format(self) -> None:
        scope = _scope(QueryPurpose.STOP_CONFIRMATION)
        assert scope.responsibility_key == "query/stop/7/3"


def _facts(**overrides) -> QueryFacts:
    values = dict(
        purpose=QueryPurpose.STOP_CONFIRMATION,
        flow_status=QueryFlowStatus.ACTIVE,
        attempts_used=1,
        config=_CONFIG,
        retry_wait_required=False,
        observation_satisfied=False,
        observation_explicitly_unmet=False,
        other_evidence_satisfied=False,
        responsibility_ended=False,
        query_supported=True,
    )
    values.update(overrides)
    return QueryFacts(**values)


class TestDecideQueryNext:
    def test_normal_response_may_still_require_more(self) -> None:
        """停止核实观察到仍在录制：本次尝试成功但责任继续。"""
        decision = decide_query_next(
            _facts(observation_satisfied=False, attempts_used=1)
        )
        assert decision.next is QueryNext.WAIT_INTERVAL

    def test_satisfied_observation_ends_flow(self) -> None:
        decision = decide_query_next(_facts(observation_satisfied=True))
        assert decision.next is QueryNext.END_SUCCEEDED

    def test_other_reliable_evidence_ends_without_new_query(self) -> None:
        """可靠停止响应已满足判断：不为流程形式重复查询。"""
        decision = decide_query_next(_facts(other_evidence_satisfied=True))
        assert decision.next is QueryNext.END_SUCCEEDED
        assert decision.new_attempt is False

    def test_budget_exhausted_ends_unconfirmed(self) -> None:
        decision = decide_query_next(
            _facts(attempts_used=3, observation_satisfied=False)
        )
        assert decision.next is QueryNext.END_UNCONFIRMED

    def test_interval_not_yet_elapsed_waits_without_new_attempt(self) -> None:
        decision = decide_query_next(
            _facts(retry_wait_required=True, attempts_used=1)
        )
        assert decision.next is QueryNext.WAIT_INTERVAL
        assert decision.new_attempt is False

    def test_explicitly_unmet_ends_failed_not_unknown(self) -> None:
        """明确不满足不被当作暂时未知继续重试。"""
        decision = decide_query_next(_facts(observation_explicitly_unmet=True))
        assert decision.next is QueryNext.END_FAILED

    def test_ended_responsibility_not_reopened(self) -> None:
        decision = decide_query_next(
            _facts(responsibility_ended=True, observation_satisfied=True)
        )
        assert decision.next is QueryNext.END_SUCCEEDED
        assert decision.new_attempt is False

    def test_unsupported_query_creates_no_attempt(self) -> None:
        """不支持状态查询的任务不创建查询尝试。"""
        decision = decide_query_next(_facts(query_supported=False, attempts_used=0))
        assert decision.next is QueryNext.END_FAILED
        assert decision.attempts_used == 0

    def test_new_attempt_only_when_active_and_budget_left(self) -> None:
        decision = decide_query_next(
            _facts(attempts_used=1, observation_satisfied=False)
        )
        assert decision.next is QueryNext.WAIT_INTERVAL
        again = decide_query_next(
            _facts(
                purpose=QueryPurpose.START_CONFIRMATION,
                attempts_used=1,
                observation_satisfied=False,
            )
        )
        assert again.next is QueryNext.WAIT_INTERVAL


class TestResultCheckRound:
    def test_pages_share_result_check_round(self) -> None:
        """一轮三批文件查询：轮次仍为 1，不按批数增加次数。"""
        current = ResultCheckRound(round_no=1, batches_read=1, complete=False)
        for batch in range(2, 4):
            decision = finish_check_round(
                current, CheckSet(batches=[f"f{batch}"], complete=False)
            )
            assert decision.outcome is CheckOutcome.CONTINUE_ROUND
            assert decision.rounds_used == 1
            assert decision.next_round == current.round_no
            current = decision.round

    def test_incomplete_round_restarts_as_new_round(self) -> None:
        """重启未完整结束的轮次：重新检查计新轮次。"""
        interrupted = ResultCheckRound(round_no=1, batches_read=2, complete=False)
        decision = finish_check_round(
            interrupted, CheckSet(batches=[], complete=False, restarted=True)
        )
        assert decision.outcome is CheckOutcome.NEW_ROUND
        assert decision.rounds_used == 2
        assert decision.next_round == 2

    def test_complete_set_ends_round(self) -> None:
        decision = finish_check_round(
            ResultCheckRound(round_no=2, batches_read=3, complete=False),
            CheckSet(batches=["a", "b", "c"], complete=True),
        )
        assert decision.outcome is CheckOutcome.COMPLETE
        assert decision.rounds_used == 2

    def test_explicitly_unmet_is_final(self) -> None:
        decision = finish_check_round(
            ResultCheckRound(round_no=1, batches_read=1, complete=False),
            CheckSet(batches=["a"], explicitly_unmet=True),
        )
        assert decision.outcome is CheckOutcome.UNMET_FINAL
        assert decision.rounds_used == 1

    def test_read_error_keeps_partial_facts_for_new_round(self) -> None:
        decision = finish_check_round(
            ResultCheckRound(round_no=1, batches_read=1, complete=False),
            CheckSet(batches=["a"], error="read_failed"),
        )
        assert decision.outcome is CheckOutcome.NEW_ROUND
        assert decision.rounds_used == 2
        assert decision.kept_batches == ("a",)

    def test_unknown_commit_verifies_first(self) -> None:
        decision = finish_check_round(
            ResultCheckRound(round_no=1, batches_read=1, complete=False),
            CheckSet(batches=["a"], commit_unknown=True),
        )
        assert decision.outcome is CheckOutcome.VERIFY_FIRST
