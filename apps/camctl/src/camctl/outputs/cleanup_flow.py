"""源产物清理的目标固定输入、结果类型与会话推进。

精确清理按原请求逐项固定；范围清理等待全部来源固定并完成后按固
定来源枚举产物，来源可靠确认无产物时固定空集合并同事务按成功收
场。缺失目标按 output_not_found 直接终态，全部不可解析时动作的
失败终态由汇总事务（cleanup_items_failed）保存。

会话推进每轮依次开始到时的清理动作、固定执行中动作的目标集合、
经设备删除端口推进未终态成员，全部成员终态后保存汇总终态；删除
请求携带成员与目标文件身份供驱动定位。
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic_ns as _default_monotonic_ns
from typing import Any, Callable

from camctl.contracts.values import ConsistencyError, ObjectId, UtcMicros
from camctl.operations.attempts import RetryWaitGate

__all__ = [
    "CancelCleanupItem",
    "CleanupActionDisposition",
    "CleanupActionFinished",
    "CleanupCancelChoice",
    "CleanupCancelFacts",
    "CleanupEntryChoice",
    "CleanupEntryFacts",
    "CleanupItemDisposition",
    "CleanupItemSaved",
    "CleanupOutcomeChoice",
    "CleanupStartDisposition",
    "CleanupStartResult",
    "CleanupTargetsDisposition",
    "CleanupTargetsSaved",
    "FailCleanupItem",
    "FinishCleanupAction",
    "FinishCleanupItem",
    "FixCleanupTargets",
    "ProgressCleanupItem",
    "RestrictCleanupItem",
    "StartCleanupAction",
    "advance_cleanup",
    "decide_cleanup_cancel",
    "decide_cleanup_entry",
    "delete_source_file",
]


@dataclass(frozen=True)
class StartCleanupAction:
    """一次清理动作开始执行的输入；时间资格由调用入口判断。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CleanupStartDisposition(Enum):
    """清理动作开始事务的结果分区。"""

    SAVED = "saved"
    #: 动作已终态：只读恢复，不重复开始。
    ALREADY = "already"
    #: 未开始且不再普通执行（取消已生效），由取消链收场。
    REJECTED = "rejected"


@dataclass(frozen=True)
class CleanupStartResult:
    """清理动作开始事务的已保存事实。"""

    disposition: CleanupStartDisposition = CleanupStartDisposition.SAVED
    reason: str | None = None


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
    #: 范围来源尚未终态或适用处理未完成：只读等待，不产生事件。
    WAITING = "waiting"


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


@dataclass(frozen=True)
class FinishCleanupAction:
    """清理动作汇总终态的申请输入：全部成员终态后保存动作结果。"""

    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)


class CleanupActionDisposition(Enum):
    """清理动作汇总事务的结果分类。"""

    SAVED = "saved"
    #: 原键重送或动作已终态：只读复用首次结果。
    ALREADY = "already"


@dataclass(frozen=True)
class CleanupActionFinished:
    """清理动作汇总事务的保存结果。"""

    disposition: CleanupActionDisposition
    action_status: int
    plan_status: int
    succeeded: int
    failed: int


# ---- 取消分支与接手前置的决策表 ---------------------------------------


#: cleanup_items.status 的登记编号。
_ITEM_UNRESOLVED, _ITEM_PENDING_DELETE, _ITEM_DELETING = 1, 2, 3
_ITEM_SUCCEEDED, _ITEM_FAILED, _ITEM_CANCELED = 4, 5, 6


class CleanupCancelChoice(Enum):
    """清理项在动作取消下的处理分支（按取消收场规则分类）。"""

    #: 已有最终结果：保持原结果，不重新开启。
    ALREADY = "already"
    #: 可靠确认从未发出删除：保存取消并解除可撤销限制。
    RELEASE = "release"
    #: 在途调用尚未取得收场依据：继续跟踪实际结束，不提前保存取消。
    TRACKING = "tracking"
    #: 收场后取得清理完成依据：本项保存成功。
    SUCCEED = "succeed"
    #: 删除已经或可能发生但结果未知：保存取消并保留不可撤销限制。
    UNCONFIRMED = "unconfirmed"
    #: 可靠确认删除失败且文件仍存在：保存取消并携带删除失败错误。
    DELETE_FAILED = "delete_failed"


@dataclass(frozen=True)
class CleanupCancelFacts:
    """取消分支判定的输入事实。

    delete_issued 要求本项此前任何可能删除文件的操作，不只最后一
    次尝试；call_settled 表示已有调用取得适用收场依据。
    """

    status: int
    delete_issued: bool
    call_settled: bool
    file_absent: bool
    file_present: bool


def decide_cleanup_cancel(facts: CleanupCancelFacts) -> CleanupCancelChoice:
    """按取消收场规则选择清理项的取消分支。"""
    if facts.file_absent and facts.file_present:
        raise ValueError("文件缺席与在场不能同时成立")
    if facts.status in (_ITEM_SUCCEEDED, _ITEM_FAILED, _ITEM_CANCELED):
        return CleanupCancelChoice.ALREADY
    if facts.status == _ITEM_DELETING:
        if not facts.call_settled:
            return CleanupCancelChoice.TRACKING
        if facts.file_absent:
            return CleanupCancelChoice.SUCCEED
        if facts.file_present:
            return CleanupCancelChoice.DELETE_FAILED
        return CleanupCancelChoice.UNCONFIRMED
    if facts.status in (_ITEM_UNRESOLVED, _ITEM_PENDING_DELETE):
        if facts.delete_issued:
            raise ValueError(
                f"未取得处理归属的成员不应有删除调用: status={facts.status}")
        return CleanupCancelChoice.RELEASE
    raise ValueError(f"未登记的清理成员状态: {facts.status!r}")


class CleanupEntryChoice(Enum):
    """清理项开始处理时的前置分支（按接手时的文件事实分类）。"""

    #: 已有可靠清理完成或文件缺席事实：直接保存成功，不发起调用。
    USE_COMPLETED = "use_completed"
    #: 本产物存在效果未确认的删除调用：先用自己的查询预算核实。
    VERIFY_FIRST = "verify_first"
    #: 不存在尚待核实的删除效果：直接进入删除。
    DIRECT_DELETE = "direct_delete"


@dataclass(frozen=True)
class CleanupEntryFacts:
    """接手前置判定的输入事实。

    unresolved_delete 覆盖同一产物任何成员的效果未确认删除调用；
    文件观察的有效性取决于观察之后的实际操作。
    """

    output_cleaned: bool
    unresolved_delete: bool
    file_absent: bool


def decide_cleanup_entry(facts: CleanupEntryFacts) -> CleanupEntryChoice:
    """按接手时的文件事实选择本项的首个处理步骤。"""
    if facts.output_cleaned or facts.file_absent:
        return CleanupEntryChoice.USE_COMPLETED
    if facts.unresolved_delete:
        return CleanupEntryChoice.VERIFY_FIRST
    return CleanupEntryChoice.DIRECT_DELETE


@dataclass(frozen=True)
class CancelCleanupItem:
    """保存清理取消终态的申请输入。

    code 为空表示未发出删除的解除限制取消；删除中成员的收场取消
    必须携带 delete_unconfirmed 或 file_delete_failed。
    """

    item_id: int
    occurred_at: int
    code: str | None = None
    details: dict | None = None

    def __post_init__(self) -> None:
        ObjectId(self.item_id)
        _item_timestamp(self.occurred_at)
        if self.code is None:
            if self.details is not None:
                raise TypeError("未发出删除的取消不携带错误详情")
        elif not isinstance(self.code, str) or not self.code:
            raise ValueError(f"清理取消必须使用公共错误名称: {self.code!r}")
        elif not isinstance(self.details, dict):
            raise TypeError("清理取消详情必须是对象")


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
    """删除编排的端口集合；配置与设备绑定由装配层提供。

    monotonic_ns 与 retry_gate 支撑删除和查询的重试间隔时间门槛：
    保存重试等待后按各自配置的间隔到时才允许下一次尝试（通信重试
    间隔与设备文件删除查询的计时），装配层会话共享同一门槛。
    """

    owned: Any
    outputs: Any
    operations: Any
    driver: DeleteDriverPort
    evidence: Any
    binding_of: Callable[[int], Any]
    occurred_at: Callable[[], int]
    delete_config: Any
    query_config: Any
    monotonic_ns: Callable[[], int] = _default_monotonic_ns
    retry_gate: RetryWaitGate = field(default_factory=RetryWaitGate)


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


def _cancel_requested(connection, action_id: int) -> bool:
    row = connection.execute(
        "SELECT cancel_requested FROM actions WHERE id = ?",
        (action_id,)).fetchone()
    return row is not None and bool(row[0])


def _output_delete_facts(connection, output_id: int) -> dict:
    """本产物删除调用的可靠事实：缺席、未决删除与晚于删除的在场确认。

    尝试编号按保存顺序单调递增，观察的有效性取决于其后的实际操
    作：晚于全部未决删除的可靠查询使其失去直接删除资格。
    """
    file_row = connection.execute(
        "SELECT device_file_id, intermediate_file_id FROM outputs"
        " WHERE id = ?", (output_id,)).fetchone()
    file_absent = False
    if file_row is not None:
        device_id, intermediate_id = file_row
        if device_id is not None:
            presence = connection.execute(
                "SELECT presence_state FROM device_files WHERE id = ?",
                (device_id,)).fetchone()
            file_absent = presence is not None and presence[0] == 3
        elif intermediate_id is not None:
            cleanup = connection.execute(
                "SELECT cleanup_state FROM intermediate_files WHERE id = ?",
                (intermediate_id,)).fetchone()
            file_absent = cleanup is not None and cleanup[0] == 4
    unresolved_delete = 0
    confirmed_check = 0
    for kind, effect_state, attempt_id in connection.execute(
            "SELECT r.kind, a.effect_state, MAX(a.id)"
            " FROM operation_attempts a"
            " JOIN operation_runs r ON a.run_id = r.id"
            " JOIN cleanup_items c ON r.cleanup_item_id = c.id"
            " WHERE c.output_id = ? AND r.kind IN (4, 5)"
            " GROUP BY r.kind, a.effect_state", (output_id,)):
        if kind == 4 and effect_state != 3:
            unresolved_delete = max(unresolved_delete, attempt_id)
        elif kind == 5 and effect_state == 3:
            confirmed_check = max(confirmed_check, attempt_id)
    return {
        "file_absent": file_absent,
        "unresolved_delete": unresolved_delete > confirmed_check,
        "confirmed_present_after": confirmed_check > unresolved_delete,
    }


def _attempts_used(connection, responsibility_key: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a"
        " JOIN operation_runs r ON a.run_id = r.id"
        " WHERE r.responsibility_key = ?", (responsibility_key,)).fetchone()
    return int(row[0]) if row is not None else 0


def _retry_wait_step(runtime: CleanupRuntime, responsibility: str,
                     interval_s, phase: str) -> CleanupStep | None:
    """开始新尝试前的间隔门槛；允许开始时返回 None。

    以责任链保存的重试等待标志与累计次数为权威：首次尝试不预先
    等待，预算耗尽即时交还意图事务按耗尽收场；处于重试等待时按
    本次配置的间隔到时才放行。
    """
    row = runtime.owned.connection.execute(
        "SELECT attempts_used, retry_wait_required, max_attempts_used"
        " FROM operation_runs WHERE responsibility_key = ?",
        (responsibility,)).fetchone()
    if row is None:
        return None
    remaining = runtime.retry_gate.remaining(
        responsibility,
        attempts_used=int(row[0]),
        retry_wait_required=int(row[1]) == 1,
        max_attempts_used=int(row[2]),
        interval_s=interval_s,
        now_ns=runtime.monotonic_ns())
    if remaining is None:
        return None
    return CleanupStep(phase, f"{remaining}s")


def _exhaustion_details(
        connection, item_id: int, output_id, operation: str, config) -> dict:
    return {"output_id": str(output_id), "max_attempts": config.max_attempts,
            "attempts_used": _attempts_used(connection, f"{operation}/{item_id}")}


def _settle_companion_runs(
        runtime: CleanupRuntime, item_id: int, status, error=None) -> str | None:
    """成员终态后统一收场删除与存在性查询两条伴随流程。

    两条流程的后续尝试都由本成员的推进驱动，成员终态即不再有后
    续尝试；仍开放的流程行按成员的最终结果结束并清除重试等待，
    否则会话的流程收尾计数无法归零。已结束的行不受影响。
    """
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import StaleRunFinish
    from camctl.persistence.models import DbOutcomeKind

    settled = runtime.operations.finish_stale_runs(
        StaleRunFinish(
            responsibility_keys=(f"delete/{item_id}", f"exists/{item_id}"),
            status=status,
            error=error,
            occurred_at=runtime.occurred_at()),
        new_operation_key(), runtime.owned)
    runtime.retry_gate.cleared(f"delete/{item_id}")
    runtime.retry_gate.cleared(f"exists/{item_id}")
    if settled.kind is not DbOutcomeKind.COMPLETED:
        return str(settled.error)
    return None


def _terminal_companion(status: int, error_code) -> tuple:
    """终态成员的伴随流程收场映射：成功、失败、取消或结果未知。

    成员失败与删除中的收场取消都保存了公共错误编号；按编号还原
    流程错误。成员行是收场结果的权威来源，此处不重复推导删除或
    查询的具体次数事实。
    """
    from camctl.contracts.workflow_errors import registered_error_spec
    from camctl.operations.attempts import RunOutcome
    from camctl.operations.models import ErrorValue

    if status == 4:
        return RunOutcome.SUCCEEDED, None
    if status == 6 and error_code is None:
        return RunOutcome.CANCELED, None
    if error_code is None:
        raise ConsistencyError(
            f"终态清理成员缺少错误编号: status={status}")
    name, _ = registered_error_spec(
        "item_error_ids.cleanup_items", int(error_code))
    if status == 5:
        stage = "delete" if name == "delete_attempts_exhausted" else "query"
        return RunOutcome.FAILED, ErrorValue(code=name, stage=stage)
    if name == "file_delete_failed":
        return RunOutcome.FAILED, ErrorValue(code=name, stage="delete")
    return RunOutcome.UNCONFIRMED, ErrorValue(code=name, stage="delete")


async def delete_source_file(runtime: CleanupRuntime, item_id: int) -> CleanupStep:
    """推进一个清理成员的删除：意图先行，结果或核实后终态。

    删除与存在性查询使用各自流程的独立预算；效果未知只能用查询额
    核实，查询确认仍在时等待预算内重试，查询预算耗尽按公共错误终
    态失败。接手前先按本产物已有删除事实决定复用完成、先核实或直
    接删除。动作取消生效后停止新增普通调用：未发出删除的成员解除
    限制，删除中的成员跟踪已有调用并按实际结论收场。
    """
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import RunOutcome
    from camctl.persistence.models import DbOutcomeKind

    connection = runtime.owned.connection
    occurred = runtime.occurred_at()
    row = connection.execute(
        "SELECT action_id, status, output_id, error_code"
        " FROM cleanup_items WHERE id = ?",
        (item_id,)).fetchone()
    if row is None:
        return CleanupStep("missing_item")
    action_id, status, output_id, error_code = row
    if status in (4, 5, 6):
        # 成员已终态但伴随流程仍未收场（此前事务之间中断）：按已
        # 保存终态补齐收场，否则会话的流程收尾计数无法归零。
        outcome, error = _terminal_companion(int(status), error_code)
        rejected = _settle_companion_runs(runtime, item_id, outcome, error)
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("already_terminal")
    # 取消已生效时不再建立新的普通处理责任：未发出删除直接取消，
    # 删除中的成员按已有调用的实际结论收场。
    if _cancel_requested(connection, action_id):
        return await _cancel_member(runtime, item_id, status, output_id)
    restrict = runtime.outputs.restrict_cleanup_item(
        RestrictCleanupItem(item_id, occurred), new_operation_key(), runtime.owned)
    if restrict.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("restrict_rejected", str(restrict.error))
    row = connection.execute(
        "SELECT action_id, status, output_id FROM cleanup_items WHERE id = ?",
        (item_id,)).fetchone()
    action_id, status, output_id = row

    entry = decide_cleanup_entry(_entry_facts(connection, output_id))
    if entry is CleanupEntryChoice.USE_COMPLETED:
        cleaned = connection.execute(
            "SELECT availability, cleanup_status FROM outputs WHERE id = ?",
            (output_id,)).fetchone()
        choice = (CleanupOutcomeChoice.ALREADY_CLEANED
                  if cleaned is not None and cleaned[0] == 3
                  else CleanupOutcomeChoice.ABSENCE_CONFIRMED)
        done = runtime.outputs.finish_cleanup_item(
            FinishCleanupItem(item_id, choice, runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        rejected = _settle_companion_runs(runtime, item_id, RunOutcome.SUCCEEDED)
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("succeeded", choice.name)
    # 设备调用目标：主机派生成品或未装配设备没有绑定，等待对应链
    # 路接入，不解释为失败。
    if runtime.binding_of(item_id) is None:
        return CleanupStep("target_unbound")
    if entry is CleanupEntryChoice.VERIFY_FIRST:
        verified = await _verify_before_delete(
            runtime, item_id, action_id, output_id)
        if verified is not None:
            return verified
    return await _delete_once(runtime, item_id, action_id, output_id)


def _entry_facts(connection, output_id: int) -> CleanupEntryFacts:
    output = connection.execute(
        "SELECT availability, cleanup_status FROM outputs WHERE id = ?",
        (output_id,)).fetchone()
    facts = _output_delete_facts(connection, output_id)
    return CleanupEntryFacts(
        output_cleaned=(
            output is not None and output[0] == 3 and output[1] == 4),
        unresolved_delete=facts["unresolved_delete"],
        file_absent=facts["file_absent"])


async def _verify_before_delete(
        runtime: CleanupRuntime, item_id: int, action_id: int,
        output_id: int) -> CleanupStep | None:
    """先用自己的查询预算核实本产物未决的删除效果。

    确认缺席直接成功；确认仍在时返回 None 落入删除路径；查询额度
    耗尽按公共错误终态失败，不以删除代替核实。
    """
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import RunOutcome
    from camctl.persistence.models import DbOutcomeKind

    connection = runtime.owned.connection
    waiting = _retry_wait_step(
        runtime, f"exists/{item_id}",
        runtime.query_config.retry_interval_s, "query_retry_wait")
    if waiting is not None:
        return waiting
    ticket = _begin_query_attempt(runtime, item_id, action_id)
    if ticket.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("query_rejected", str(ticket.error))
    if ticket.value.ticket is None:
        if ticket.value.reason == "budget_exhausted":
            return _fail_exhausted(
                runtime, item_id, output_id, "exists",
                runtime.query_config, "file_query_attempts_exhausted")
        return CleanupStep("query_budget_rejected", ticket.value.reason)
    progress = runtime.outputs.progress_cleanup_item(
        ProgressCleanupItem(item_id, runtime.occurred_at()),
        new_operation_key(), runtime.owned)
    if progress.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("progress_rejected", str(progress.error))
    present = await _run_query(runtime, ticket.value.ticket, item_id)
    if present is False:
        done = runtime.outputs.finish_cleanup_item(
            FinishCleanupItem(
                item_id, CleanupOutcomeChoice.ABSENCE_CONFIRMED,
                runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        rejected = _settle_companion_runs(runtime, item_id, RunOutcome.SUCCEEDED)
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("succeeded", "ABSENCE_CONFIRMED")
    if present is True:
        # 文件确认仍在：转入本项删除路径（预算独立核对）。
        return None
    return CleanupStep("query_unknown")


async def _delete_once(
        runtime: CleanupRuntime, item_id: int, action_id: int,
        output_id: int) -> CleanupStep:
    """发起一次删除调用并保存结果；效果未知转入查询核实。"""
    from camctl.contracts.values import new_operation_key
    from camctl.devices.ports import ControlRequest
    from camctl.operations.attempts import (
        AttemptFinish, AttemptIntent, AttemptTarget, OperationKind, RunOutcome)
    from camctl.operations.models import (
        AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
        Settlement, SettlementBasis)
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.models import DbOutcomeKind

    connection = runtime.owned.connection
    occurred = runtime.occurred_at()
    waiting = _retry_wait_step(
        runtime, f"delete/{item_id}",
        runtime.delete_config.retry_interval_s, "delete_retry_wait")
    if waiting is not None:
        return waiting
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
            return _fail_exhausted(
                runtime, item_id, output_id, "delete",
                runtime.delete_config, "delete_attempts_exhausted")
        return CleanupStep("delete_budget_rejected", ticket.value.reason)
    progress = runtime.outputs.progress_cleanup_item(
        ProgressCleanupItem(item_id, occurred), new_operation_key(), runtime.owned)
    if progress.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("progress_rejected", str(progress.error))
    binding = runtime.binding_of(item_id)
    result = await runtime.driver.delete(ControlRequest(
        operation="delete", binding=binding,
        params=_target_params(connection, item_id)))
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
            evidence=EvidenceValue(type="delete_returned", version=1, data={})),
        observations=result.observations)
    finish = runtime.operations.finish_attempt(
        AttemptFinish(
            ticket=ticket.value.ticket,
            outcome=validate_outcome(ticket.value.ticket, outcome, runtime.evidence),
            occurred_at=runtime.occurred_at(),
            # 效果未知保持流程继续并建立重试等待；流程终态由成员
            # 终态时的伴随收场统一保存。
            retry_wait=not absent),
        new_operation_key(), runtime.owned)
    if finish.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("finish_rejected", str(finish.error))
    if not absent:
        # 删除效果未知：保存了重试等待，登记间隔锚点。
        runtime.retry_gate.established(
            f"delete/{item_id}", runtime.monotonic_ns())
    if absent:
        choice = (CleanupOutcomeChoice.DELETED if not errored
                  else CleanupOutcomeChoice.ABSENCE_CONFIRMED)
        done = runtime.outputs.finish_cleanup_item(
            FinishCleanupItem(item_id, choice, runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        rejected = _settle_companion_runs(runtime, item_id, RunOutcome.SUCCEEDED)
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("succeeded", choice.name)
    # 在途调用已结束：取消若在此期间生效，按实际结论收场。
    if _cancel_requested(runtime.owned.connection, action_id):
        return await _settle_canceling_member(runtime, item_id, output_id)
    return await _verify_after_delete(runtime, item_id, action_id, output_id)


async def _verify_after_delete(
        runtime: CleanupRuntime, item_id: int, action_id: int,
        output_id: int) -> CleanupStep:
    """删除效果未知时用查询预算核实；确认仍在时等待预算内重试。"""
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import RunOutcome
    from camctl.persistence.models import DbOutcomeKind

    waiting = _retry_wait_step(
        runtime, f"exists/{item_id}",
        runtime.query_config.retry_interval_s, "query_retry_wait")
    if waiting is not None:
        return waiting
    ticket = _begin_query_attempt(runtime, item_id, action_id)
    if ticket.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("query_rejected", str(ticket.error))
    if ticket.value.ticket is None:
        if ticket.value.reason == "budget_exhausted":
            return _fail_exhausted(
                runtime, item_id, output_id, "exists",
                runtime.query_config, "file_query_attempts_exhausted")
        return CleanupStep("query_budget_rejected", ticket.value.reason)
    present = await _run_query(runtime, ticket.value.ticket, item_id)
    if present is False:
        done = runtime.outputs.finish_cleanup_item(
            FinishCleanupItem(
                item_id, CleanupOutcomeChoice.ABSENCE_CONFIRMED,
                runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        rejected = _settle_companion_runs(runtime, item_id, RunOutcome.SUCCEEDED)
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("succeeded", "ABSENCE_CONFIRMED")
    if present is True:
        # 删除未生效且文件仍在：等待预算内重试，本次不判定失败。
        return CleanupStep("still_present")
    return CleanupStep("query_unknown")


async def _cancel_member(
        runtime: CleanupRuntime, item_id: int, status: int,
        output_id) -> CleanupStep:
    """取消已生效的成员处理：未发出删除解除限制，删除中按结论收场。"""
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import RunOutcome
    from camctl.persistence.models import DbOutcomeKind

    if status in (1, 2):
        canceled = runtime.outputs.cancel_cleanup_item(
            CancelCleanupItem(item_id, runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if canceled.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("cancel_rejected", str(canceled.error))
        rejected = _settle_companion_runs(runtime, item_id, RunOutcome.CANCELED)
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("canceled")
    return await _settle_canceling_member(runtime, item_id, output_id)


async def _settle_canceling_member(
        runtime: CleanupRuntime, item_id: int, output_id,
) -> CleanupStep:
    """删除中成员的取消收场：按可靠文件事实保存成功或取消错误。

    已有可靠缺席事实保存成功；晚于未决删除的在场确认按删除失败收
    场；无可靠结论时用自己的查询预算核实，额度耗尽或仍未知按结果
    未知保存 delete_unconfirmed，不补造查询次数耗尽或文件仍在结论。
    """
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import RunOutcome
    from camctl.persistence.models import DbOutcomeKind

    connection = runtime.owned.connection
    facts = _output_delete_facts(connection, output_id)
    if facts["file_absent"]:
        done = runtime.outputs.finish_cleanup_item(
            FinishCleanupItem(
                item_id, CleanupOutcomeChoice.ABSENCE_CONFIRMED,
                runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if done.kind is not DbOutcomeKind.COMPLETED:
            return CleanupStep("result_rejected", str(done.error))
        rejected = _settle_companion_runs(runtime, item_id, RunOutcome.SUCCEEDED)
        if rejected is not None:
            return CleanupStep("companion_rejected", rejected)
        return CleanupStep("succeeded", "ABSENCE_CONFIRMED")
    if facts["confirmed_present_after"]:
        return _cancel_with_error(runtime, item_id, output_id, "file_delete_failed")
    ticket = _begin_query_attempt(
        runtime, item_id, _item_action(connection, item_id))
    if ticket.kind is DbOutcomeKind.COMPLETED and ticket.value.ticket is not None:
        present = await _run_query(runtime, ticket.value.ticket, item_id)
        if present is False:
            done = runtime.outputs.finish_cleanup_item(
                FinishCleanupItem(
                    item_id, CleanupOutcomeChoice.ABSENCE_CONFIRMED,
                    runtime.occurred_at()),
                new_operation_key(), runtime.owned)
            if done.kind is not DbOutcomeKind.COMPLETED:
                return CleanupStep("result_rejected", str(done.error))
            rejected = _settle_companion_runs(
                runtime, item_id, RunOutcome.SUCCEEDED)
            if rejected is not None:
                return CleanupStep("companion_rejected", rejected)
            return CleanupStep("succeeded", "ABSENCE_CONFIRMED")
        if present is True:
            return _cancel_with_error(
                runtime, item_id, output_id, "file_delete_failed")
    return _cancel_with_error(runtime, item_id, output_id, "delete_unconfirmed")


def _item_action(connection, item_id: int) -> int:
    row = connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id = ?",
        (item_id,)).fetchone()
    if row is None:
        raise ValueError(f"清理成员不存在: {item_id}")
    return int(row[0])


def _cancel_with_error(
        runtime: CleanupRuntime, item_id: int, output_id, code: str,
) -> CleanupStep:
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import RunOutcome
    from camctl.operations.models import ErrorValue
    from camctl.persistence.models import DbOutcomeKind

    canceled = runtime.outputs.cancel_cleanup_item(
        CancelCleanupItem(item_id, runtime.occurred_at(), code,
                          {"output_id": str(output_id)}),
        new_operation_key(), runtime.owned)
    if canceled.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("cancel_rejected", str(canceled.error))
    outcome = (RunOutcome.FAILED if code == "file_delete_failed"
               else RunOutcome.UNCONFIRMED)
    rejected = _settle_companion_runs(
        runtime, item_id, outcome,
        ErrorValue(code=code, stage="delete"))
    if rejected is not None:
        return CleanupStep("companion_rejected", rejected)
    return CleanupStep("canceled", code)


def _fail_exhausted(
        runtime: CleanupRuntime, item_id: int, output_id, operation: str,
        config, code: str) -> CleanupStep:
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import RunOutcome
    from camctl.operations.models import ErrorValue
    from camctl.persistence.models import DbOutcomeKind

    failed = runtime.outputs.fail_cleanup_item(
        FailCleanupItem(
            item_id, code,
            _exhaustion_details(
                runtime.owned.connection, item_id, output_id, operation, config),
            runtime.occurred_at()),
        new_operation_key(), runtime.owned)
    if failed.kind is not DbOutcomeKind.COMPLETED:
        return CleanupStep("fail_rejected", str(failed.error))
    stage = "delete" if operation == "delete" else "query"
    rejected = _settle_companion_runs(
        runtime, item_id, RunOutcome.FAILED,
        ErrorValue(code=code, stage=stage))
    if rejected is not None:
        return CleanupStep("companion_rejected", rejected)
    return CleanupStep("failed", code)


def _begin_query_attempt(runtime: CleanupRuntime, item_id: int, action_id: int):
    """登记一次存在性查询尝试；预算独立于删除流程。"""
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import (
        AttemptIntent, AttemptTarget, OperationKind)

    return runtime.operations.begin_attempt(
        AttemptIntent(
            operation="query",
            action_id=action_id,
            kind=OperationKind.CHECK_FILE_EXISTS,
            target=AttemptTarget(cleanup_item_id=item_id),
            query_purpose=None,
            config=runtime.query_config,
            occurred_at=runtime.occurred_at(),
        ),
        new_operation_key(), runtime.owned)


async def _run_query(runtime: CleanupRuntime, ticket, item_id: int) -> bool | None:
    """执行查询调用并保存尝试结果；返回可靠在场事实或 None。"""
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.models import (
        AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
        Settlement, SettlementBasis)
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.models import DbOutcomeKind

    query_result = await runtime.driver.query_state(
        _control_request(runtime, item_id, "query"))
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
    finish = runtime.operations.finish_attempt(
        AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(
                ticket, query_outcome, runtime.evidence),
            occurred_at=runtime.occurred_at(),
            # 确认缺席由调用方收场终态；确认仍在与无可靠事实都保持
            # 流程继续，不终局的轮次建立重试等待，否则流程被锁死。
            retry_wait=present is not False),
        new_operation_key(), runtime.owned)
    if present is not False and finish.kind is DbOutcomeKind.COMPLETED:
        # 保存了重试等待：登记查询间隔锚点。
        runtime.retry_gate.established(
            f"exists/{item_id}", runtime.monotonic_ns())
    return present


def _control_request(runtime: CleanupRuntime, item_id: int, operation: str):
    from camctl.devices.ports import ControlRequest

    return ControlRequest(
        operation=operation, binding=runtime.binding_of(item_id),
        params=_target_params(runtime.owned.connection, item_id))


def _target_params(connection, item_id: int) -> dict:
    """删除/查询请求的目标身份与设备文件定位。

    驱动按成员身份回填观察、按设备文件身份定位目标；目标不是设
    备文件属于装配不一致，明确拒绝。
    """
    row = connection.execute(
        "SELECT f.id, f.identity_key, f.locator_json"
        " FROM cleanup_items c"
        " JOIN outputs o ON o.id = c.output_id"
        " JOIN device_files f ON f.id = o.device_file_id"
        " WHERE c.id = ?", (item_id,)).fetchone()
    if row is None:
        raise ConsistencyError(f"清理成员缺少设备文件目标: {item_id}")
    device_file_id, identity_key, locator_json = row
    locator = json.loads(locator_json) \
        if isinstance(locator_json, str) else {}
    return {
        "cleanup_item_id": str(item_id),
        "file_id": str(device_file_id),
        "identity_key": identity_key,
        "locator": locator,
    }


# ---- 会话推进：开始、目标固定、成员删除与汇总 ----


#: 清理成员的终态集合（成功、失败、取消）。
_MEMBER_TERMINAL = (4, 5, 6)

#: 推进结果中指示事务未完成或事实不一致的分区。
_MEMBER_ERROR_PHASES = (
    "missing_item", "restrict_rejected", "query_rejected",
    "progress_rejected", "result_rejected", "delete_budget_rejected",
    "query_budget_rejected", "finish_rejected", "fail_rejected",
    "cancel_rejected", "companion_rejected")


async def advance_cleanup(runtime: CleanupRuntime) -> None:
    """推进一个轮次的清理执行链；各步按已保存事实幂等。

    到时的待执行动作先开始，执行中的动作按目标集合状态推进：集
    合未固定先固定（范围来源未就绪保持等待），集合已固定逐成员
    推进删除，全部成员终态后保存汇总终态。
    """
    now = runtime.occurred_at()
    _start_due(runtime, now)
    for action_id in _running_cleanup_actions(runtime):
        await _advance_cleanup_action(runtime, action_id, now)


def _start_due(runtime: CleanupRuntime, now: int) -> None:
    """开始到时的待执行清理动作；未到时与已取消动作不进入。"""
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind

    with closing(runtime.owned.connection.execute(
        "SELECT id FROM actions"
        " WHERE status = 1 AND cancel_requested = 0 AND type = 5"
        " AND scheduled_at <= ? ORDER BY plan_id, input_index", (now,),
    )) as cursor:
        due = [int(row[0]) for row in cursor.fetchall()]
    for action_id in due:
        outcome = runtime.outputs.start_cleanup_action(
            StartCleanupAction(action_id=action_id, occurred_at=now),
            new_operation_key(), runtime.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(
                f"清理开始事务未完成（{outcome.kind.value}）:"
                f" {outcome.error}")


def _running_cleanup_actions(runtime: CleanupRuntime) -> list[int]:
    """执行中且未请求取消的清理动作。"""
    with closing(runtime.owned.connection.execute(
        "SELECT id FROM actions"
        " WHERE status = 2 AND cancel_requested = 0 AND type = 5"
        " ORDER BY plan_id, input_index",
    )) as cursor:
        return [int(row[0]) for row in cursor.fetchall()]


async def _advance_cleanup_action(
        runtime: CleanupRuntime, action_id: int, now: int) -> None:
    """按已保存事实推进一个执行中的清理动作的下一个阶段。"""
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind

    connection = runtime.owned.connection
    with closing(connection.execute(
        "SELECT target_selection_state FROM actions WHERE id = ?",
        (action_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"清理动作不存在: {action_id}")
    if row[0] == 1:
        # 目标集合待固定；范围来源未就绪或零产物收场时本动作结束
        # 本轮推进。
        if not _fix_targets(runtime, action_id, now):
            return
    elif row[0] != 2:
        return
    with closing(connection.execute(
        # 终态成员仍选中仅当伴随流程未收场：中断后由 already_terminal
        # 分支按已保存终态补齐，收尾计数才能归零。
        "SELECT id FROM cleanup_items"
        " WHERE action_id = ? AND (status NOT IN (4, 5, 6)"
        " OR EXISTS(SELECT 1 FROM operation_runs r"
        " WHERE r.cleanup_item_id = cleanup_items.id"
        " AND r.status IN (1, 2))) ORDER BY id",
        (action_id,),
    )) as cursor:
        pending = [int(row[0]) for row in cursor.fetchall()]
    for item_id in pending:
        step = await delete_source_file(runtime, item_id)
        if step.phase in _MEMBER_ERROR_PHASES:
            raise ConsistencyError(
                f"清理成员推进未完成: {item_id}"
                f" {step.phase} {step.detail}")
    with closing(connection.execute(
        "SELECT COUNT(*) FROM cleanup_items"
        " WHERE action_id = ? AND status NOT IN (4, 5, 6)", (action_id,),
    )) as cursor:
        unfinished = int(cursor.fetchone()[0])
    if unfinished:
        return
    outcome = runtime.outputs.finish_cleanup_action(
        FinishCleanupAction(action_id=action_id, occurred_at=now),
        new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"清理汇总事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}")


def _fix_targets(runtime: CleanupRuntime, action_id: int, now: int) -> bool:
    """固定目标集合；来源未就绪或零产物已收场时返回假。"""
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind

    outcome = runtime.outputs.fix_cleanup_targets(
        FixCleanupTargets(action_id=action_id, occurred_at=now),
        new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"清理目标固定事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}")
    if outcome.value.disposition is CleanupTargetsDisposition.WAITING:
        return False
    if not outcome.value.item_ids:
        # 范围来源可靠确认无产物：空集合已同事务按成功收场。
        return False
    return True
