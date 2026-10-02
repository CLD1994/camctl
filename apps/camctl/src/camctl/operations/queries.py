"""独立查询责任与产物核实轮次。

查询用途、目标组合及责任键在创建时固定，此后不可改变；正常查询
响应仍可能需要继续责任，一次响应成功不结束尚未完成的核实。产物
结果核实一轮只消耗一次轮次，轮内分页沿用同一轮次；明确不满足是
最终结论，不被当作暂时未知反复核实。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple

from camctl.operations.attempts import (
    AttemptConfig,
    QueryPurpose,
    query_responsibility_key,
)

__all__ = [
    "CheckOutcome",
    "CheckSet",
    "QueryFacts",
    "QueryFlowStatus",
    "QueryNext",
    "QueryResponsibility",
    "ResultCheckRound",
    "decide_query_next",
    "finish_check_round",
    "validate_query_scope",
]


class QueryFlowStatus(Enum):
    """查询流程在判定时的状态。"""

    ACTIVE = "active"
    ENDED = "ended"


class QueryNext(Enum):
    """查询责任的下一步分区。"""

    WAIT_INTERVAL = "wait_interval"
    END_SUCCEEDED = "end_succeeded"
    END_FAILED = "end_failed"
    END_UNCONFIRMED = "end_unconfirmed"


@dataclass(frozen=True)
class QueryResponsibility:
    """一项查询责任的固定身份：用途、目标、责任键及配置。

    用途与目标组合创建后不可改变；配置在创建时完整保存，后续运行
    采用新配置时另存依据，不刷新原责任。
    """

    purpose: QueryPurpose
    action_id: int
    activity_id: int | None
    config: AttemptConfig

    @property
    def responsibility_key(self) -> str:
        return query_responsibility_key(self.action_id, self.purpose, self.activity_id)


def validate_query_scope(scope: QueryResponsibility) -> None:
    """核对用途、目标组合、责任键格式及配置完整性。"""
    if scope.purpose is QueryPurpose.BEFORE_EXECUTION:
        if scope.activity_id is not None:
            raise ValueError("执行前检查不得指向具体活动")
    elif scope.activity_id is None:
        raise ValueError(f"{scope.purpose.value} 用途必须指向目标活动")
    if scope.config.timeout_s is None or scope.config.retry_interval_s is None:
        raise ValueError("查询责任必须保存完整的超时与间隔配置")
    if scope.config.max_attempts < 1:
        raise ValueError("查询责任必须保存正的次数上限")


@dataclass(frozen=True)
class QueryFacts:
    """一次查询责任判定的事实输入。"""

    purpose: QueryPurpose
    flow_status: QueryFlowStatus
    attempts_used: int
    config: AttemptConfig
    retry_wait_required: bool = False
    observation_satisfied: bool = False
    observation_explicitly_unmet: bool = False
    other_evidence_satisfied: bool = False
    responsibility_ended: bool = False
    query_supported: bool = True


@dataclass(frozen=True)
class QueryDecision:
    """查询责任的判定结果。"""

    next: QueryNext
    new_attempt: bool
    attempts_used: int


def decide_query_next(facts: QueryFacts) -> QueryDecision:
    """按可靠观察与继续条件决定查询责任的处理。

    可靠停止响应或其他操作已保存的证据足以满足判断时直接结束，
    不为流程形式追加查询；不支持状态查询的任务不创建查询尝试。
    """
    if facts.observation_explicitly_unmet:
        return QueryDecision(
            next=QueryNext.END_FAILED,
            new_attempt=False,
            attempts_used=facts.attempts_used,
        )
    if facts.observation_satisfied or facts.other_evidence_satisfied:
        return QueryDecision(
            next=QueryNext.END_SUCCEEDED,
            new_attempt=False,
            attempts_used=facts.attempts_used,
        )
    if facts.responsibility_ended or facts.flow_status is QueryFlowStatus.ENDED:
        return QueryDecision(
            next=QueryNext.END_UNCONFIRMED,
            new_attempt=False,
            attempts_used=facts.attempts_used,
        )
    if not facts.query_supported:
        return QueryDecision(
            next=QueryNext.END_FAILED,
            new_attempt=False,
            attempts_used=0,
        )
    if facts.attempts_used >= facts.config.max_attempts:
        return QueryDecision(
            next=QueryNext.END_UNCONFIRMED,
            new_attempt=False,
            attempts_used=facts.attempts_used,
        )
    return QueryDecision(
        next=QueryNext.WAIT_INTERVAL,
        new_attempt=not facts.retry_wait_required and facts.attempts_used == 0,
        attempts_used=facts.attempts_used,
    )


class CheckOutcome(Enum):
    """一轮产物核实的结果分区。"""

    CONTINUE_ROUND = "continue_round"
    NEW_ROUND = "new_round"
    COMPLETE = "complete"
    UNMET_FINAL = "unmet_final"
    VERIFY_FIRST = "verify_first"


@dataclass(frozen=True)
class ResultCheckRound:
    """一轮结果核实的进行状态：轮次编号与本轮已读批次数。"""

    round_no: int
    batches_read: int
    complete: bool


@dataclass(frozen=True)
class CheckSet:
    """本轮已取得的检查事实。

    batches 是本轮可靠读取的批次；complete 表示适用结果要求全部
   满足；explicitly_unmet 表示明确不满足；error 表示本轮读取失败；
    restarted 表示轮次中断后重启；commit_unknown 表示整轮结果提交
    结果未知。
    """

    batches: Tuple[str, ...] = ()
    complete: bool = False
    explicitly_unmet: bool = False
    error: str | None = None
    restarted: bool = False
    commit_unknown: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "batches", tuple(self.batches))


@dataclass(frozen=True)
class CheckDecision:
    """一轮核实的判定：轮次消耗与下一状态。"""

    outcome: CheckOutcome
    rounds_used: int
    next_round: int
    round: ResultCheckRound
    kept_batches: Tuple[str, ...] = ()


def finish_check_round(current: ResultCheckRound, facts: CheckSet) -> CheckDecision:
    """判定一轮核实的结果与轮次消耗。

    同轮后续批次沿用原轮次；中断后重新检查计新轮次；明确不满足
    是最终结论；整轮结果提交未知时先核实原事务。
    """
    if facts.commit_unknown:
        return CheckDecision(
            outcome=CheckOutcome.VERIFY_FIRST,
            rounds_used=current.round_no,
            next_round=current.round_no,
            round=current,
            kept_batches=facts.batches,
        )
    if facts.explicitly_unmet:
        return CheckDecision(
            outcome=CheckOutcome.UNMET_FINAL,
            rounds_used=current.round_no,
            next_round=current.round_no,
            round=current,
        )
    if facts.complete:
        finished = ResultCheckRound(
            round_no=current.round_no,
            batches_read=current.batches_read + len(facts.batches),
            complete=True,
        )
        return CheckDecision(
            outcome=CheckOutcome.COMPLETE,
            rounds_used=current.round_no,
            next_round=current.round_no,
            round=finished,
        )
    if facts.restarted:
        # 中断后缺少完整结果：保留部分可靠事实，重新检查计新轮次。
        return CheckDecision(
            outcome=CheckOutcome.NEW_ROUND,
            rounds_used=current.round_no + 1,
            next_round=current.round_no + 1,
            round=ResultCheckRound(
                round_no=current.round_no + 1, batches_read=0, complete=False
            ),
            kept_batches=facts.batches,
        )
    if facts.error is not None:
        return CheckDecision(
            outcome=CheckOutcome.NEW_ROUND,
            rounds_used=current.round_no + 1,
            next_round=current.round_no + 1,
            round=ResultCheckRound(
                round_no=current.round_no + 1, batches_read=0, complete=False
            ),
            kept_batches=facts.batches,
        )
    continued = ResultCheckRound(
        round_no=current.round_no,
        batches_read=current.batches_read + len(facts.batches),
        complete=False,
    )
    return CheckDecision(
        outcome=CheckOutcome.CONTINUE_ROUND,
        rounds_used=current.round_no,
        next_round=current.round_no,
        round=continued,
        kept_batches=facts.batches,
    )
