"""源产物清理的目标固定输入与结果类型。

精确清理按原请求逐项固定；范围清理等待全部来源固定并完成后固
定。缺失目标按 output_not_found 直接终态，全部不可解析时同事务
以 cleanup_items_failed 结束动作。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from camctl.contracts.values import ObjectId, UtcMicros

__all__ = [
    "CleanupItemDisposition",
    "CleanupItemSaved",
    "CleanupOutcomeChoice",
    "CleanupTargetsDisposition",
    "CleanupTargetsSaved",
    "FailCleanupItem",
    "FinishCleanupItem",
    "FixCleanupTargets",
    "ProgressCleanupItem",
    "RestrictCleanupItem",
]


@dataclass(frozen=True)
class FixCleanupTargets:
    """一次清理目标固定的申请输入。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CleanupTargetsDisposition(Enum):
    """目标固定事务的结果分类。"""

    SAVED = "saved"
    #: 已固定集合或原键重送：只读复用首次结果。
    ALREADY = "already"


@dataclass(frozen=True)
class CleanupTargetsSaved:
    """目标固定事务的保存结果；item_ids 按创建顺序排列。"""

    disposition: CleanupTargetsDisposition
    item_ids: tuple[int, ...]


class CleanupItemDisposition(Enum):
    """清理项事务的结果分类。"""

    SAVED = "saved"
    #: 已处于该阶段或原键重送：只读复用。
    ALREADY = "already"


@dataclass(frozen=True)
class CleanupItemSaved:
    """清理项事务的保存结果。"""

    disposition: CleanupItemDisposition
    item_id: int


def _item_timestamp(value: int) -> None:
    UtcMicros(value)


@dataclass(frozen=True)
class RestrictCleanupItem:
    """建立删除限制的申请输入（成员未解析 → 限制中）。"""

    item_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        _item_timestamp(self.occurred_at)


@dataclass(frozen=True)
class ProgressCleanupItem:
    """进入删除执行的申请输入（限制中 → 删除中）。"""

    item_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        _item_timestamp(self.occurred_at)


class CleanupOutcomeChoice(Enum):
    """清理成功的结果依据选择（登记枚举成员）。"""

    DELETED = 1
    ABSENCE_CONFIRMED = 3


@dataclass(frozen=True)
class FinishCleanupItem:
    """保存清理成功终态的申请输入。"""

    item_id: int
    outcome: CleanupOutcomeChoice
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        if not isinstance(self.outcome, CleanupOutcomeChoice):
            raise TypeError(f"清理结果依据必须使用 CleanupOutcomeChoice: {self.outcome!r}")
        _item_timestamp(self.occurred_at)


@dataclass(frozen=True)
class FailCleanupItem:
    """保存清理失败终态的申请输入；code 使用公共错误名称。"""

    item_id: int
    code: str
    details: dict
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        if not isinstance(self.code, str) or not self.code:
            raise ValueError(f"清理失败必须使用公共错误名称: {self.code!r}")
        if not isinstance(self.details, dict):
            raise TypeError("清理失败详情必须是对象")
        _item_timestamp(self.occurred_at)


# ---- 删除编排：限制 → 删除意图 → 设备调用 → 结果/核实 ----


class DeleteDriverPort:
    """设备删除与查询调用端口；契约替身与真实驱动同形。"""

    async def delete(self, request) -> object: ...

    async def query_state(self, request) -> object: ...


@dataclass(frozen=True)
class CleanupStep:
    """一次删除推进的结果分区。"""

    phase: str
    detail: str | None = None


@dataclass
class CleanupRuntime:
    """删除编排的端口集合；配置与设备绑定由装配层提供。"""

    owned: Any
    outputs: Any
    operations: Any
    driver: DeleteDriverPort
    evidence: Any
    binding_of: Callable[[int], Any]
    occurred_at: Callable[[], int]
    delete_config: Any
    query_config: Any


def _delete_observation(result) -> tuple[bool, bool]:
    """删除调用的可靠观察：返回（缺席确认, 调用错误）。"""
    absent = any(
        observation.type == "file_absent" for observation in result.observations)
    return absent, result.error is not None


def _presence_observation(result):
    """查询调用取得的在场事实；无法解释时为 None。"""
    for observation in result.observations:
        if observation.type == "file_presence":
            present = observation.data.get("present")
            if isinstance(present, bool):
                return present
    return None


async def delete_source_file(runtime: CleanupRuntime, item_id: int) -> CleanupStep:
    """推进一个清理成员的删除：意图先行，结果或核实后终态。

    删除与存在性查询使用各自流程的独立预算；效果未知只能用查询额
    核实，查询确认仍在时等待预算内重试，不判定失败。
    """
    from camctl.contracts.values import new_operation_key
    from camctl.devices.ports import ControlRequest
    from camctl.operations.attempts import (
        AttemptFinish,
        AttemptIntent,
        AttemptTarget,
        OperationKind,
    )
    from camctl.operations.models import (
        AttemptStatus,
        CallOutcome,
        EffectState,
        ErrorValue,
        EvidenceValue,
        Settlement,
        SettlementBasis,
    )
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.models import DbOutcomeKind

    occurred = runtime.occurred_at()
    restrict = runtime.outputs.restrict_cleanup_item(
        RestrictCleanupItem(item_id, occurred), new_operation_key(), runtime.owned)
    if restrict.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("restrict_rejected", str(restrict.error))
    item = runtime.owned.connection.execute(
        "SELECT action_id, status FROM cleanup_items WHERE id = ?",
        (item_id,)).fetchone()
    if item is None:
        return CleanupStep("missing_item")
    if item[1] in (4, 5, 6):
        return CleanupStep("already_terminal")
    action_id = item[0]
    intent = AttemptIntent(
        operation="delete",
        action_id=action_id,
        kind=OperationKind.DELETE_FILE,
        target=AttemptTarget(cleanup_item_id=item_id),
        query_purpose=None,
        config=runtime.delete_config,
        occurred_at=occurred,
    )
    ticket = runtime.operations.begin_attempt(
        intent, new_operation_key(), runtime.owned)
    if ticket.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("delete_budget_rejected", str(ticket.error))
    if ticket.value.ticket is None:
        if ticket.value.reason == "budget_exhausted":
            output_id = runtime.owned.connection.execute(
                "SELECT output_id FROM cleanup_items WHERE id = ?",
                (item_id,)).fetchone()[0]
            failed = runtime.outputs.fail_cleanup_item(
                FailCleanupItem(item_id, "delete_attempts_exhausted",
                                {"output_id": str(output_id),
                                 "max_attempts": runtime.delete_config.max_attempts,
                                 "attempts_used": runtime.delete_config.max_attempts},
                                occurred),
                new_operation_key(), runtime.owned)
            if failed.kind is not DbOutcomeKind.COMPLETED:
                return CleanupStep("fail_rejected", str(failed.error))
            return CleanupStep("failed", "delete_attempts_exhausted")
        return CleanupStep("delete_budget_rejected", ticket.value.reason)
    progress = runtime.outputs.progress_cleanup_item(
        ProgressCleanupItem(item_id, occurred), new_operation_key(), runtime.owned)
    if progress.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("progress_rejected", str(progress.error))
    binding = runtime.binding_of(item_id)
    result = await runtime.driver.delete(ControlRequest(
        operation="delete", binding=binding, params={}))
    absent, errored = _delete_observation(result)
    if errored:
        status, effect = AttemptStatus.FAILED, (
            EffectState.CONFIRMED if absent else EffectState.UNKNOWN)
        error = ErrorValue(code="device_error", stage="delete")
    elif absent:
        status, effect, error = AttemptStatus.SUCCEEDED, EffectState.CONFIRMED, None
    else:
        status, effect, error = AttemptStatus.SUCCEEDED, EffectState.UNKNOWN, None
    outcome = CallOutcome(
        status=status, error=error, effect=effect,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(type="operation_returned", version=1, data={})),
        observations=result.observations)
    finish = runtime.operations.finish_attempt(
        AttemptFinish(
            ticket=ticket.value.ticket,
            outcome=validate_outcome(ticket.value.ticket, outcome, runtime.evidence),
            occurred_at=runtime.occurred_at(),
            retry_wait=not absent),
        new_operation_key(), runtime.owned)
    if finish.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("finish_rejected", str(finish.error))
    if absent:
        choice = (CleanupOutcomeChoice.DELETED if not errored
                  else CleanupOutcomeChoice.ABSENCE_CONFIRMED)
        done = runtime.outputs.finish_cleanup_item(
            FinishCleanupItem(item_id, choice, runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        return CleanupStep("succeeded", choice.name)
    # 效果未知：只能用查询预算核实，不能重发删除替代。
    query_intent = AttemptIntent(
        operation="query",
        action_id=action_id,
        kind=OperationKind.CHECK_FILE_EXISTS,
        target=AttemptTarget(cleanup_item_id=item_id),
        query_purpose=None,
        config=runtime.query_config,
        occurred_at=runtime.occurred_at(),
    )
    granted = runtime.operations.begin_attempt(
        query_intent, new_operation_key(), runtime.owned)
    if granted.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("query_rejected", str(granted.error))
    if granted.value.ticket is None:
        runtime.outputs.fail_cleanup_item(
            FailCleanupItem(item_id, "delete_unconfirmed",
                            {"cleanup_item_id": str(item_id)},
                            runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        return CleanupStep("failed", "delete_unconfirmed")
    query_result = await runtime.driver.query_state(ControlRequest(
        operation="query", binding=binding, params={}))
    present = _presence_observation(query_result)
    query_outcome = CallOutcome(
        status=AttemptStatus.SUCCEEDED if query_result.error is None
        else AttemptStatus.FAILED,
        error=None if query_result.error is None
        else ErrorValue(code="device_error", stage="query"),
        effect=EffectState.CONFIRMED if present is not None
        else EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(type="file_presence", version=1, data={})),
        observations=query_result.observations)
    runtime.operations.finish_attempt(
        AttemptFinish(
            ticket=granted.value.ticket,
            outcome=validate_outcome(
                granted.value.ticket, query_outcome, runtime.evidence),
            occurred_at=runtime.occurred_at(),
            retry_wait=present is None),
        new_operation_key(), runtime.owned)
    if present is False:
        done = runtime.outputs.finish_cleanup_item(
            FinishCleanupItem(
                item_id, CleanupOutcomeChoice.ABSENCE_CONFIRMED,
                runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        return CleanupStep("succeeded", "ABSENCE_CONFIRMED")
    if present is True:
        # 删除未生效且文件仍在：等待预算内重试，本次不判定失败。
        return CleanupStep("still_present")
    return CleanupStep("query_unknown")
