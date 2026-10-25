"""会话流程与有序收尾。

run 与 submit 共用本入口：受理、时钟资格、接纳与退出检查按既定
顺序组织。run 的推进循环每轮驱动已注册流程，无进展时按真实责任
分类判断是否关闭：可退出才在关闭事务内重查并释放接纳，仍需驱动
则等待下一计划截止、进程内通知或轮询上限后继续。报告失败不阻塞
设备工作并触发一次日志副本；报告流程因状态库错误失败时同样先触
发一次日志副本，再按状态库错误停止依赖已失效条件的工作。受限会
话执行规定收场与一次报告机会后按时钟异常退出，不取得普通接纳。
submit 对提交结果未知以同一请求幂等核实，不宣称受理成功或失败。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Protocol

from camctl.acceptance.input import InputDiagnostic, ParsedInput
from camctl.acceptance.notification import notify_acceptance
from camctl.acceptance.service import AcceptanceResult, CommandMode, accept_input
from camctl.contracts.clock import ClockPort
from camctl.contracts.values import OperationKey, new_operation_key
from camctl.devices.bindings import DeviceConfigurationError
from camctl.logging_runtime.copies import (
    CopyOutcome,
    CopyReceipt,
    DeleteOutcome,
    MarkerPresence,
    MarkerSave,
    SaveOutcome,
    _describe,
)
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.persistence.models import DbOutcomeKind, DirectoryBindingError
from camctl.persistence.runtime import OwnedConnection, RuntimeLibraryError
from camctl.persistence.transaction import write_receipt_for
from camctl.session.clock import (
    ClockBecameUntrusted,
    ClockCheckInput,
    ExecutionMode,
    check_clock,
    enter_execution,
)
from camctl.session.handoff import SubmitHandoff
from camctl.session.locks import (
    AdmissionLease,
    LockError,
    SessionLease,
    lock_error_facts,
    probe_admission,
)
from camctl.session.outcome import SessionOutcome
from camctl.session.work import WorkDecisionKind, WorkFacts, classify_work

__all__ = ["SessionContext", "StateDbFailure", "run_session"]

#: 空闲等待下限：到期工作立即再推进，同时避免无界忙转。
_MIN_IDLE_WAIT_S = 0.01


@dataclass(frozen=True)
class _RequiredPhaseFact:
    value: Any = None
    error: BaseException | None = None


class _RequiredPhaseWaiter:
    """让必要业务阶段与父协程取消分别交付实际事实。"""

    def __init__(self, context: SessionContext) -> None:
        self._context = context
        self.parent_cancel: asyncio.CancelledError | None = None
        self._phase_errors: list[BaseException] = []
        self.first_control: BaseException | None = None
        self._stop_attempted = False
        self._sweep_cancel_attempted = False
        self._restricted_logging_permit = False
        self._stop_errors: list[BaseException] = []
        self._delivered_stop_errors: set[int] = set()

    async def run(self, name: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        async def capture() -> _RequiredPhaseFact:
            try:
                return _RequiredPhaseFact(value=await operation())
            except BaseException as error:
                self.record_phase_error(error)
                return _RequiredPhaseFact(error=error)

        task = asyncio.create_task(capture(), name=f"camctl-required-{name}")
        while True:
            try:
                fact = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                self.record_parent_cancel(error)
        if fact.error is not None:
            raise fact.error.with_traceback(fact.error.__traceback__)
        return fact.value

    def record_phase_error(self, error: BaseException) -> None:
        if not any(error is saved for saved in self._phase_errors):
            self._phase_errors.append(error)
        self.record_sync_control(error)

    def record_sync_control(self, error: BaseException) -> None:
        """在实际来源处登记首个可信同步控制，包括原异常组的叶对象。"""
        if (self.first_control is None and not isinstance(error, Exception)
                and (lock_error_facts(error) is not None
                     or write_receipt_for(error) is not None)):
            self.first_control = error
            return
        for source, _facts in _ordered_error_sources(error):
            if (not isinstance(source, Exception)
                    and _is_typed_sync_control(source)
                    and self.first_control is None):
                self.first_control = source

    def _stop_error(self, error: BaseException) -> None:
        if not any(error is saved for saved in self._stop_errors):
            self._stop_errors.append(error)
        self.record_phase_error(error)

    def request_stop(self) -> None:
        """一次尝试普通停止；受限日志新取得的许可独立撤回。"""
        if not self._stop_attempted:
            self._stop_attempted = True
            try:
                stop = getattr(self._context.local_work, "stop_new_work", None)
                if stop is not None:
                    stop()
            except BaseException as error:
                self._stop_error(error)
            if self._context.snapshot_owner is not None:
                try:
                    self._context.snapshot_owner.stop_new_work()
                except BaseException as error:
                    self._stop_error(error)
        if self._restricted_logging_permit:
            self._restricted_logging_permit = False
            try:
                self._context.logging_owner.stop_new_work()
            except BaseException as error:
                self._stop_error(error)

    def resumed_restricted_logging(self) -> None:
        self._restricted_logging_permit = True

    def request_sweep_cancel(self) -> None:
        if self._sweep_cancel_attempted:
            return
        self._sweep_cancel_attempted = True
        try:
            cancel = getattr(self._context.logging_owner, "cancel_startup_sweep", None)
            if cancel is not None:
                cancel()
        except BaseException as error:
            self._stop_error(error)

    def take_stop_errors(self) -> tuple[BaseException, ...]:
        errors = tuple(error for error in self._stop_errors
                       if id(error) not in self._delivered_stop_errors)
        self._delivered_stop_errors.update(id(error) for error in errors)
        return errors

    def is_phase_error(self, error: BaseException) -> bool:
        return any(error is phase_error for phase_error in self._phase_errors)

    def record_parent_cancel(self, error: asyncio.CancelledError) -> None:
        if self.parent_cancel is not None:
            return
        self.parent_cancel = error
        if self.first_control is None:
            self.first_control = error
        self.request_stop()
        self.request_sweep_cancel()

    def add_note(self, message: str) -> None:
        if self.parent_cancel is not None:
            self.parent_cancel.add_note(message)

    def add_exception_notes(self, label: str, error: BaseException) -> None:
        if self.parent_cancel is not None:
            self.attach_exception_notes(self.parent_cancel, label, error)

    @staticmethod
    def attach_exception_notes(
            target: BaseException, label: str, error: BaseException) -> None:
        if target is error:
            return
        notes = tuple(getattr(error, "__notes__", ()))
        diagnostic = _diagnostic_note(label, error)
        target.add_note(f"{label}: {type(error).__name__}: {error}")
        for note in notes:
            target.add_note(f"{label}附注: {note}")
        if isinstance(error, BaseExceptionGroup):
            _attach_ordered_error_notes(
                target, label, _ordered_error_sources(error), diagnostic_error=error,
                diagnostic_note=diagnostic,
            )
        else:
            target.add_note(diagnostic)


class StateDbFailure(RuntimeError):
    """流程遭遇状态库错误：停止依赖已失效条件的工作。"""


class AcceptanceRepositoryPort(Protocol):
    def process_input(self, command, key: OperationKey, owned: OwnedConnection): ...


class SessionRepositoryPort(Protocol):
    def acquire_admission(self, command, key: OperationKey, owned: OwnedConnection): ...

    def close_admission(self, command, key: OperationKey, owned: OwnedConnection): ...

    def update_lower_bound(self, check, key: OperationKey, owned: OwnedConnection): ...


class LocalWorkPort(Protocol):
    """本次已经开始的实际本地责任，不替代持久化工作事实。"""

    def required_settlements(self) -> int: ...

    async def settle(self) -> None: ...


class SnapshotOwnerPort(Protocol):
    """派生维护的会话生命周期，不参与业务责任计数。"""
    async def start(self) -> None: ...
    async def discover(self) -> None: ...
    def stop_new_work(self) -> None: ...
    async def settle(self) -> None: ...


@dataclass(frozen=True)
class SessionPaths:
    """会话锁与接纳锁文件路径。"""

    session_lock: Any
    admission_lock: Any


@dataclass
class SessionContext:
    """一次会话的协作者与运行参数。

    flows 是已注册的业务流程端口（调度、设备、报告），推进循环每
    轮全部驱动；名为 "report" 的流程失败按报告错误处理并触发日志
    副本，其余流程异常按状态库错误收口。restricted_flows 与
    once_report 是受限会话的规定收场和一次报告端口。facts_query
    在推进循环和关闭事务内重查工作事实。wake 是进程内唤醒通知；
    poll_interval_s 是外部输入的感知轮询上限。
    """

    mode: CommandMode
    catalog: Any
    clock: ClockPort
    open_database: Callable[[], Awaitable[Any]]
    acceptance_repository: AcceptanceRepositoryPort
    session_repository: SessionRepositoryPort
    paths: SessionPaths
    #: 时钟检查输入在会话内按已提交下界构建（含部署时间策略）。
    clock_policy: Callable[[Any], ClockCheckInput]
    acquire_session: Callable[[], SessionLease]
    acquire_admission: Callable[[], AdmissionLease]
    facts_query: Callable[[Any], WorkFacts]
    notifier: Any = None
    flows: Mapping[str, Any] = field(default_factory=dict)
    #: 受限会话的规定收场流程（未定时取消、必要收场）。
    restricted_flows: Mapping[str, Any] = field(default_factory=dict)
    #: 受限会话的一次报告处理端口；恰好调用一次，不持接纳。
    once_report: Any = None
    #: 报告失败的日志副本服务；缺省不触发副本。
    failure_log: Any = None
    #: 报告失败触发副本时的请求工厂：(error) -> CopyRequest。
    copy_request_factory: Callable[[Any], Any] | None = None
    #: 报告副本诊断的普通日志出口；装配层保证实际写入并处理回退。
    on_log_diagnostic: Callable[[str], Awaitable[None]] | None = None
    #: 文件协调状态不明时使用的剩余 stderr 出口。
    on_stderr_diagnostic: Callable[[str], Awaitable[None]] | None = None
    #: 文件协调及关闭结果均未知后，通知装配停止使用共享日志路径。
    on_log_path_unsafe: Callable[[], None] | None = None
    #: 会话锁已取得且尚未打开状态库；只用于 run 锁内日志启动责任。
    on_session_locked: Callable[[], Awaitable[None]] | None = None
    #: 预检查确认存在坏库时保留原结果，并沿同一 run 锁收场路径退出。
    startup_outcome: SessionOutcome | None = None
    #: 本 run 的日志文件责任 owner；报告／恢复等待取消时立即交接。
    logging_owner: Any = None
    #: 进程内工作唤醒通知；等待新工作时由生产者触发。
    wake: Any = None
    #: 外部输入感知的轮询上限（秒）；部署目标为 0.5 秒量级。
    poll_interval_s: float = 0.5
    #: 内部截止提前检查余量；不允许实际业务操作提前执行。
    wakeup_margin_s: float = 0.05
    #: run 取得会话锁并可靠打开状态库后，业务事务前固定恢复输入。
    on_session_open: Callable[[Any], Awaitable[None]] | None = None
    #: 实际文件调用、结果保存和独立连接关闭均完成后才结束的责任。
    local_work: LocalWorkPort | None = None
    #: 非时钟致命错误收场时的录像应急停止端口；返回诊断条目。
    emergency_settlement: Callable[[], Awaitable[list[dict]]] | None = None
    #: 正常 run 的报告轻量观察；只在会话连接存活期间绑定。
    report_change_wm: Callable[[], Awaitable[int]] | None = field(default=None, init=False, repr=False)
    #: 只在会话锁内可靠打开后发布，不交付线程所属连接。
    database: Any = field(default=None, init=False, repr=False)
    #: 正常运行的派生快照维护，不计入业务待处理责任。
    snapshot_owner: SnapshotOwnerPort | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.poll_interval_s, (int, float)) or (
                isinstance(self.poll_interval_s, bool)
                or self.poll_interval_s <= 0):
            raise ValueError(
                f"poll_interval_s 必须是有限正秒数: {self.poll_interval_s!r}")


def _outcome_error(reason: str, details: Mapping[str, Any] | None = None) -> SessionOutcome:
    from types import MappingProxyType

    return SessionOutcome(
        succeeded=False, reason=reason, details=details or MappingProxyType({})
    )


def _transaction_status(kind: DbOutcomeKind | None) -> str | None:
    if kind is DbOutcomeKind.NOT_EXECUTED:
        return "not_started"
    if kind is DbOutcomeKind.ROLLED_BACK:
        return "not_committed"
    if kind is DbOutcomeKind.UNKNOWN:
        return "unknown"
    if kind is DbOutcomeKind.COMPLETED:
        return "committed"
    return None


def _exception_leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _exception_leaves(child)]
    return [error]


def _ordered_error_sources(
    error: BaseException,
) -> list[tuple[BaseException, Any]]:
    """按异常组和锁事实记录的实际顺序展开诊断来源。"""
    sources: list[tuple[BaseException, Any]] = []
    seen_errors: set[int] = set()
    seen_facts: set[int] = set()

    def add_facts(facts: Any) -> None:
        if id(facts) in seen_facts:
            return
        seen_facts.add(id(facts))
        source = facts.original_error
        if source is not None:
            add_error(source, facts)
        else:
            for secondary in facts.secondary_errors:
                add_facts(secondary)

    def add_error(source: BaseException, facts_override: Any = None) -> None:
        facts = (
            facts_override
            if facts_override is not None
            else lock_error_facts(source)
        )
        if isinstance(source, BaseExceptionGroup):
            if id(source) in seen_errors:
                if facts is not None:
                    for secondary in facts.secondary_errors:
                        add_facts(secondary)
                return
            seen_errors.add(id(source))
            for child in source.exceptions:
                add_error(child)
            if facts is not None:
                for secondary in facts.secondary_errors:
                    add_facts(secondary)
            return
        if id(source) in seen_errors:
            if facts is not None:
                for secondary in facts.secondary_errors:
                    add_facts(secondary)
            return
        seen_errors.add(id(source))
        sources.append((source, facts))
        if facts is not None:
            for secondary in facts.secondary_errors:
                add_facts(secondary)

    add_error(error)
    return sources


def _error_identity_objects(error: BaseException) -> list[BaseException]:
    """列出异常组与锁事实引用的对象，用于识别租约重复交付。"""
    objects: list[BaseException] = []
    seen_errors: set[int] = set()
    seen_facts: set[int] = set()

    def visit_facts(facts: Any) -> None:
        if id(facts) in seen_facts:
            return
        seen_facts.add(id(facts))
        source = facts.original_error
        if source is not None:
            visit_error(source)
        for secondary in facts.secondary_errors:
            visit_facts(secondary)

    def visit_error(error: BaseException) -> None:
        if id(error) not in seen_errors:
            seen_errors.add(id(error))
            objects.append(error)
            if isinstance(error, BaseExceptionGroup):
                for child in error.exceptions:
                    visit_error(child)
        facts = lock_error_facts(error)
        if facts is not None:
            visit_facts(facts)

    visit_error(error)
    return objects


def _exception_details(
    error: BaseException,
    *,
    stage: str,
    transaction_status: str | None = None,
    fallback_reason: str = "state_db_error",
    facts_override: Any = None,
) -> tuple[str, dict[str, Any]]:
    """保留既有分类和摘要，并附上原异常的完整机器诊断。"""
    facts = (
        facts_override
        if facts_override is not None
        else lock_error_facts(error)
    )
    if isinstance(error, LockError) or (
            facts is not None and isinstance(error, OSError)):
        reason = "lock_error"
    elif facts is not None:
        reason = "internal_error"
    elif isinstance(error, (DirectoryBindingError, DeviceConfigurationError)):
        reason = "configuration_error"
    else:
        reason = fallback_reason
    message = f"{type(error).__name__}: {error}"
    if facts is not None:
        message = f"{facts.operation.value}: {message}"
    details: dict[str, Any] = {
        "stage": stage,
        "message": message,
        "exception": exception_diagnostic(error),
    }
    if facts is not None:
        details["path"] = str(facts.path)
        if facts.errno is not None:
            details["errno"] = facts.errno
    if transaction_status is not None:
        details["transaction_status"] = transaction_status
    return reason, details


def _transaction_error_outcome(
    error: BaseException | None,
    kind: DbOutcomeKind | None,
    *,
    stage: str,
    fallback_reason: str = "state_db_error",
    include_unknown_commit: bool = False,
) -> SessionOutcome:
    if error is None:
        error = RuntimeError("事务未交付错误对象")
    sources = _ordered_error_sources(error)
    return _ordered_sources_outcome(
        sources,
        stage=stage,
        fallback_reason=fallback_reason,
        transaction_status=_transaction_status(kind),
        include_unknown_commit=(
            include_unknown_commit and kind is DbOutcomeKind.UNKNOWN
        ),
        diagnostic_error=error,
    )


def _ordered_sources_outcome(
    sources: list[tuple[BaseException, Any]],
    *,
    stage: str,
    fallback_reason: str,
    transaction_status: str | None = None,
    include_unknown_commit: bool = False,
    diagnostic_error: BaseException | None = None,
) -> SessionOutcome:
    """把按实际顺序筛出的错误来源汇总为扁平诊断。"""
    assert sources
    primary, primary_facts = sources[0]
    reason, details = _exception_details(
        primary,
        stage=stage,
        transaction_status=transaction_status,
        fallback_reason=fallback_reason,
        facts_override=primary_facts,
    )
    if include_unknown_commit:
        details["unknown_commit"] = True
    secondary: list[dict[str, Any]] = []
    for leaf, facts in sources[1:]:
        secondary_reason, secondary_details = _exception_details(
            leaf,
            stage=stage,
            fallback_reason=fallback_reason,
            facts_override=facts,
        )
        secondary.append({"reason": secondary_reason, "details": secondary_details})
    if secondary:
        details["secondary_errors"] = secondary
    if diagnostic_error is not None:
        details["exception"] = exception_diagnostic(diagnostic_error)
    return _outcome_error(reason, details)


def _is_typed_sync_control(error: BaseException) -> bool:
    if lock_error_facts(error) is not None or write_receipt_for(error) is not None:
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_is_typed_sync_control(source)
                   for source, _facts in _ordered_error_sources(error))
    return False


def _secondary_outcomes(outcome: SessionOutcome) -> list[dict[str, Any]]:
    details = dict(outcome.details)
    existing = list(details.pop("secondary_errors", ()))
    entries = [{"reason": outcome.reason, "details": details}]
    entries.extend(existing)
    return entries


def _merge_failure_outcome(
    current: SessionOutcome,
    added: SessionOutcome,
) -> SessionOutcome:
    """合并后续普通错误；首个致命结果保持为主，报告错误让位给致命错误。"""
    if current.succeeded:
        return added
    if current.reason == "report_error":
        details = dict(added.details)
        secondary = list(details.pop("secondary_errors", ()))
        secondary.extend(_secondary_outcomes(current))
        details["secondary_errors"] = secondary
        return _outcome_error(added.reason or "state_db_error", details)
    details = dict(current.details)
    secondary = list(details.pop("secondary_errors", ()))
    secondary.extend(_secondary_outcomes(added))
    details["secondary_errors"] = secondary
    return _outcome_error(current.reason or "state_db_error", details)


def _remember_error(errors: list[BaseException], error: BaseException | None) -> None:
    if error is None:
        return
    for source in _error_identity_objects(error):
        if not any(source is saved for saved in errors):
            errors.append(source)


def _diagnostic_note(label: str, error: BaseException) -> str:
    return f"{label}诊断: " + json.dumps(exception_diagnostic(error), ensure_ascii=False)


def _attach_ordered_error_notes(
    target: BaseException,
    label: str,
    sources: list[tuple[BaseException, Any]],
    *,
    diagnostic_error: BaseException | None = None,
    diagnostic_note: str | None = None,
) -> None:
    """按实际错误顺序附注，保留目标已有说明并跳过自身。"""
    existing_notes = set(tuple(getattr(target, "__notes__", ())))
    for source, facts in sources:
        if source is target:
            continue
        description = f"{type(source).__name__}: {source}"
        if facts is not None:
            description = f"{facts.operation.value}: {description}"
        note = f"{label}: {description}"
        if note not in existing_notes:
            target.add_note(note)
            existing_notes.add(note)
        for source_note in tuple(getattr(source, "__notes__", ())):
            attached_note = f"{label}附注: {source_note}"
            if attached_note not in existing_notes:
                target.add_note(attached_note)
                existing_notes.add(attached_note)
    diagnostic_sources = (
        [diagnostic_error] if diagnostic_error is not None
        else [source for source, _facts in sources]
    )
    for source in diagnostic_sources:
        if source is target:
            continue
        note = diagnostic_note or _diagnostic_note(label, source)
        if note not in existing_notes:
            target.add_note(note)
            existing_notes.add(note)


def _merge_cleanup_errors(
    outcome: SessionOutcome | None,
    original: BaseException | None,
    failures: list[tuple[BaseException, str, str]],
    observed_errors: list[BaseException] | None = None,
) -> tuple[SessionOutcome | None, BaseException | None]:
    """合并同步资源收场结果，控制对象优先且每个原错误只交付一次。"""
    seen: set[int] = set()
    if observed_errors is not None:
        seen.update(id(error) for error in observed_errors)
    if original is not None:
        seen.add(id(original))
    for error, stage, fallback_reason in failures:
        sources = _ordered_error_sources(error)
        unobserved_sources = [
            (source, facts)
            for source, facts in sources
            if id(source) not in seen
        ]
        if not unobserved_sources:
            continue
        seen.update(
            id(source) for source in _error_identity_objects(error)
        )
        if not isinstance(error, Exception):
            if original is None or isinstance(original, Exception):
                previous = original
                if previous is not None:
                    _RequiredPhaseWaiter.attach_exception_notes(
                        error, "此前会话异常", previous,
                    )
                if outcome is not None and not outcome.succeeded:
                    error.add_note(
                        "此前会话结果: "
                        f"reason={outcome.reason}; details={dict(outcome.details)!r}"
                    )
                original = error
                _attach_ordered_error_notes(
                    original, "同步资源收场异常", unobserved_sources,
                    diagnostic_error=error,
                )
            else:
                _attach_ordered_error_notes(
                    original,
                    "后续同步资源收场控制异常",
                    unobserved_sources,
                    diagnostic_error=error,
                )
            continue
        if original is not None:
            _attach_ordered_error_notes(
                original, "同步资源收场异常", unobserved_sources,
                diagnostic_error=error,
            )
            continue
        if len(unobserved_sources) == len(sources):
            added = _transaction_error_outcome(
                error,
                None,
                stage=stage,
                fallback_reason=fallback_reason,
            )
        else:
            added = _ordered_sources_outcome(
                unobserved_sources,
                stage=stage,
                fallback_reason=fallback_reason,
                diagnostic_error=error,
            )
        outcome = added if outcome is None else _merge_failure_outcome(outcome, added)
    return outcome, original


async def run_session(
    context: SessionContext,
    source: ParsedInput | InputDiagnostic | None,
) -> SessionOutcome:
    """执行一次 run 或 submit 会话并返回机器结果。

    submit 不做墙钟检查、不取得会话或接纳锁，在事务内探测接纳并
    给出 needs_run；run 取得会话锁、完成受理后检查时钟并取得接
    纳，退出前在同一事务内重查工作并释放接纳。
    """
    if context.mode is CommandMode.SUBMIT:
        return await _submit_session(context, source)
    return await _run_session(context, source)


async def _submit_session(
    context: SessionContext, source: ParsedInput | InputDiagnostic | None
) -> SessionOutcome:
    from camctl.acceptance.service import AcceptanceStateError

    if source is None:
        return _outcome_error("input_missing", {"reason": "submit 需要计划输入文件"})
    database: Any = None
    outcome: SessionOutcome | None = None
    original: BaseException | None = None
    try:
        try:
            database = await context.open_database()
            context.database = database
        except (RuntimeLibraryError, DirectoryBindingError) as error:
            # 本进程运行库不满足部署要求：配置环境问题，不是状态库数据问题。
            outcome = _outcome_error(
                "configuration_error",
                {"error": str(error)},
            )
        except Exception as error:
            outcome = _outcome_error(
                "state_db_error",
                {"error": str(error)},
            )
        if database is not None:
            try:
                accepted = await accept_input(
                    source,
                    _acceptance_context(context),
                    new_operation_key(),
                    database,
                )
            except AcceptanceStateError as error:
                kind = getattr(error, "outcome_kind", None)
                if not isinstance(kind, DbOutcomeKind):
                    kind = DbOutcomeKind.UNKNOWN
                outcome = _transaction_error_outcome(
                    error.__cause__ or error,
                    kind,
                    stage="handoff",
                    include_unknown_commit=True,
                )
            else:
                if context.notifier is not None:
                    notify_acceptance(accepted, context.notifier)
                if type(accepted.needs_run) is not bool:
                    outcome = _outcome_error(
                        "state_db_error",
                        {
                            "stage": "handoff",
                            "transaction_status": "committed",
                            "message": "已完成的 submit 缺少接管结果",
                        },
                    )
                else:
                    outcome = SessionOutcome(
                        succeeded=True, needs_run=accepted.needs_run,
                    )
    except BaseException as error:
        original = error

    cleanup_failures: list[tuple[BaseException, str, str]] = []
    if database is not None:
        try:
            await database.close()
        except BaseException as error:
            cleanup_failures.append((error, "shutdown", "state_db_error"))
    outcome, original = _merge_cleanup_errors(
        outcome, original, cleanup_failures,
    )
    if original is not None:
        raise original.with_traceback(original.__traceback__)
    assert outcome is not None
    return outcome


async def _run_session(
    context: SessionContext, source: ParsedInput | InputDiagnostic | None
) -> SessionOutcome:
    # 裸 run：无输入文件；带计划启动的 run 在会话锁内受理，
    # 受理事务在时钟检查前结束。
    session_lease: SessionLease | None = None
    admission_lease: AdmissionLease | None = None
    database: Any = None
    local_errors: list[Exception] = []
    observed_errors: list[BaseException] = []
    waiter = _RequiredPhaseWaiter(context)
    previous_report_observer = context.report_change_wm

    async def drive() -> SessionOutcome:
        nonlocal session_lease, admission_lease, database
        try:
            session_lease = context.acquire_session()
        except Exception as error:
            return _transaction_error_outcome(
                error,
                None,
                stage="locking",
                fallback_reason="state_db_error",
            )
        if context.on_session_locked is not None:
            try:
                await context.on_session_locked()
            except Exception as error:
                return _outcome_error(
                    "state_db_error", {"error": f"日志启动收场未能启动: {error}"})
        if context.startup_outcome is not None:
            return context.startup_outcome
        try:
            database = await context.open_database()
            context.database = database
        except (RuntimeLibraryError, DirectoryBindingError) as error:
            # 同 submit：运行库不满足部署要求归配置错误，先于业务库打开拒绝。
            return _outcome_error("configuration_error", {"error": str(error)})
        except Exception as error:
            return _outcome_error("state_db_error", {"error": str(error)})
        context.report_change_wm = lambda: database.read(
            lambda owned: _read_report_change_wm(owned.connection),
            description="报告业务水位")
        if context.on_session_open is not None:
            try:
                await context.on_session_open(database)
            except Exception as error:
                return _outcome_error("state_db_error", {"error": str(error)})
        if source is not None:
            try:
                await accept_input(
                    source,
                    _acceptance_context(context),
                    new_operation_key(),
                    database,
                )
            except Exception as error:
                from camctl.acceptance.service import AcceptanceStateError

                if isinstance(error, AcceptanceStateError):
                    cause = error.__cause__ or error
                    _remember_error(observed_errors, cause)
                    kind = getattr(error, "outcome_kind", None)
                    if not isinstance(kind, DbOutcomeKind):
                        kind = DbOutcomeKind.UNKNOWN
                    return _transaction_error_outcome(
                        cause,
                        kind,
                        stage="input_handling",
                        include_unknown_commit=True,
                    )
                return _outcome_error("state_db_error", {"error": str(error)})

        try:
            policy = await database.read(
                lambda owned: context.clock_policy(owned.connection), description="启动时钟依据")
        except Exception as error:
            return _outcome_error("state_db_error", {"error": str(error)})
        check = check_clock(policy, context.clock)
        try:
            mode = await enter_execution(
                check,
                _clock_context(context, database),
            )
        except Exception as error:
            return _outcome_error("state_db_error", {"error": str(error)})
        if mode is ExecutionMode.FATAL:
            return _outcome_error("state_db_error", {"error": "可信时间下界保存失败"})
        if mode is ExecutionMode.RESTRICTED:
            # 有限安全收场与一次报告机会：不取得普通接纳资格。
            return await waiter.run(
                "initial-restricted",
                lambda: _initial_restricted_phase(context, waiter),
            )

        from camctl.persistence.repositories.session import AcquireAdmission

        acquired: list[AdmissionLease] = []

        def acquire_and_register() -> None:
            acquired.append(context.acquire_admission())

        try:
            key = new_operation_key()
            async def acquire_phase():
                nonlocal admission_lease
                try:
                    return await database.write(
                        lambda owned: context.session_repository.acquire_admission(
                            AcquireAdmission(acquire_and_register), key, owned),
                        key=key, description="取得接纳资格")
                finally:
                    if acquired:
                        admission_lease = acquired[0]
            admission_outcome = await waiter.run("acquire-admission", acquire_phase)
        except Exception as error:
            _remember_error(observed_errors, error)
            receipt = write_receipt_for(error)
            kind = None
            if receipt is not None:
                kind = {
                    "completed": DbOutcomeKind.COMPLETED,
                    "rolled_back": DbOutcomeKind.ROLLED_BACK,
                    "unknown": DbOutcomeKind.UNKNOWN,
                }.get(receipt.kind)
            if kind is None:
                kind = DbOutcomeKind.UNKNOWN
            facts = lock_error_facts(error)
            stage = "locking" if isinstance(error, LockError) or facts is not None else "handoff"
            return _transaction_error_outcome(
                error, kind, stage=stage,
            )
        if admission_outcome.kind is not DbOutcomeKind.COMPLETED:
            error = admission_outcome.error
            _remember_error(observed_errors, error)
            leaves = _exception_leaves(error) if error is not None else []
            lock_failure = any(
                isinstance(item, LockError) or lock_error_facts(item) is not None
                for item in leaves
            )
            return _transaction_error_outcome(
                error,
                admission_outcome.kind,
                stage="locking" if lock_failure else "handoff",
            )

        if context.snapshot_owner is not None:
            try:
                await context.snapshot_owner.start()
            except Exception as error:
                return _outcome_error("state_db_error", {"error": str(error)})
        while True:
            observed_wake = None if context.wake is None else context.wake.snapshot()
            try:
                if context.snapshot_owner is not None:
                    try:
                        await context.snapshot_owner.discover()
                    except Exception as error:
                        return _outcome_error("state_db_error", {"error": str(error)})
                fatal = await _drive_flows(context)
            except ClockBecameUntrusted as clock_error:
                async def transition_to_restricted() -> SessionOutcome:
                    nonlocal admission_lease
                    try:
                        await _settle_local_work(
                            context, exclude_owner=context.logging_owner, waiter=waiter)
                    except Exception as error:
                        local_errors.append(error)
                        # 前置保存失败后，受限停止和报告不能依赖失效状态。
                        return _outcome_error(
                            "clock_invalid", {"error": str(clock_error)})
                    try:
                        admission_lease.close()
                    except Exception as error:
                        _remember_error(observed_errors, error)
                        outcome = _transaction_error_outcome(
                            error,
                            None,
                            stage="handoff",
                            fallback_reason="internal_error",
                        )
                        details = dict(outcome.details)
                        details["clock_error"] = str(clock_error)
                        return _outcome_error(outcome.reason, details)
                    except BaseException as error:
                        if _is_typed_sync_control(error):
                            waiter.record_sync_control(error)
                            _RequiredPhaseWaiter.attach_exception_notes(
                                error, "时钟转受限阶段", clock_error,
                            )
                        raise
                    admission_lease = None
                    restricted_failure = await _run_restricted_flows(context)
                    if restricted_failure is not None:
                        return restricted_failure
                    if context.logging_owner is not None:
                        try:
                            await context.logging_owner.settle()
                        except Exception as error:
                            local_errors.append(error)
                            return _outcome_error(
                                "clock_invalid", {"error": str(clock_error)})
                        if waiter.parent_cancel is None:
                            context.logging_owner.resume_restricted_logging()
                            waiter.resumed_restricted_logging()
                    if waiter.parent_cancel is not None:
                        return _outcome_error("clock_invalid")
                    return await _restricted_once_report(context)

                return await waiter.run(
                    "late-clock-restricted-transition", transition_to_restricted)
            except (DirectoryBindingError, DeviceConfigurationError) as error:
                return _outcome_error("configuration_error", {"error": str(error)})
            if fatal is not None:
                # 状态库错误：停止依赖已失效条件的工作，接纳随进程退出。
                return fatal
            try:
                decision = classify_work(await _run_work_facts(context))
            except Exception as error:
                return _outcome_error("state_db_error", {"error": str(error)})
            if decision.kind is not WorkDecisionKind.NEEDS_DRIVER:
                async def settle_before_close() -> Exception | None:
                    try:
                        await _settle_local_work(context, stop_new=False, waiter=waiter)
                    except Exception as error:
                        return error
                    return None

                settle_error = await waiter.run(
                    "normal-local-settlement", settle_before_close)
                if settle_error is not None:
                    local_errors.append(settle_error)
                    # 收场前提失效后不继续关闭事务；保留原退出分类，
                    # 最终结果统一附加本地收场的真实诊断。
                    return (_outcome_error("report_error")
                            if decision.kind is WorkDecisionKind.EXIT_REPORT_ERROR
                            else SessionOutcome(succeeded=True))
                if waiter.parent_cancel is not None:
                    # 已取消时不开始下一项普通关闭步骤；finally 释放句柄。
                    return (_outcome_error("report_error")
                            if decision.kind is WorkDecisionKind.EXIT_REPORT_ERROR
                            else SessionOutcome(succeeded=True))
                outcome = await waiter.run("close-admission", lambda: _try_close(
                    context, database, admission_lease, observed_errors))
                if outcome is not None:
                    return outcome
                # 关闭事务观察到新工作：继续推进，不释放接纳。
            try:
                await _wait_for_more_work(context, database, observed=observed_wake)
            except Exception as error:
                return _outcome_error("state_db_error", {"error": str(error)})

    outcome: SessionOutcome | None = None
    original: BaseException | None = None
    try:
        outcome = await drive()
    except BaseException as error:
        if (not isinstance(error, Exception)
                and _is_typed_sync_control(error)):
            waiter.record_sync_control(error)
        if isinstance(error, asyncio.CancelledError):
            if _is_typed_sync_control(error) or waiter.is_phase_error(error):
                if waiter.first_control is error or waiter.parent_cancel is None:
                    original = error
                else:
                    waiter.add_exception_notes("后到同步控制异常", error)
            elif waiter.parent_cancel is None:
                waiter.record_parent_cancel(error)
            elif error is not waiter.parent_cancel:
                waiter.add_exception_notes("必要收场实际控制异常", error)
        elif waiter.parent_cancel is not None:
            waiter.add_exception_notes("会话实际控制异常", error)
        else:
            original = error
    try:
        fatal_outcome = (
            outcome is not None
            and not outcome.succeeded
            and outcome.reason in (
                "state_db_error", "configuration_error", "lock_error",
                "internal_error",
            )
        )
        preserve_startup_work = (
            waiter.parent_cancel is None
            and original is None
            and context.startup_outcome is not None)
        if (fatal_outcome or original is not None) and not preserve_startup_work:
            # 先尝试全部普通停止，再进入任何已取得资格的应急等待。
            # 原入口错误延后到实际 settle 完成后交付。
            waiter.request_stop()
        if fatal_outcome and waiter.parent_cancel is not None:
            waiter.add_note(
                "会话保留实际致命结果："
                f"reason={outcome.reason}; details={outcome.details!r}")
        if (session_lease is not None and original is None and fatal_outcome
                and context.emergency_settlement is not None):
            # 非时钟致命错误：先对会话内录像执行有限应急停止，再继
            # 续本地收场；应急收场失败不改写原错误分类。
            try:
                outcome = await waiter.run(
                    "emergency-settlement",
                    lambda: _merge_emergency_stops(context, outcome),
                )
            except BaseException as error:
                if waiter.parent_cancel is not None:
                    if error is not waiter.parent_cancel:
                        waiter.add_exception_notes("应急收场实际控制异常", error)
                else:
                    original = error
            if waiter.parent_cancel is not None and outcome is not None:
                waiter.add_note(
                    "必要收场交付应急结果："
                    f"reason={outcome.reason}; details={outcome.details!r}")
        try:
            # 可靠存在但无法使用的状态库仍须沿同一会话锁完成一次
            # 启动日志扫描；这是正常预检查结果，不是取消要求停止
            # 后续步骤。若调用本身被取消，仍立即停止新增本地工作。
            preserve_startup_work = (
                waiter.parent_cancel is None
                and original is None
                and context.startup_outcome is not None)
            stop_new = (
                waiter.parent_cancel is None and not preserve_startup_work)
            await waiter.run(
                "final-local-settlement",
                lambda: _settle_local_work(context, stop_new=stop_new, waiter=waiter),
            )
        except BaseException as error:
            if isinstance(error, Exception):
                if not any(error is saved for saved in local_errors):
                    local_errors.append(error)
            elif waiter.parent_cancel is not None:
                if error is not waiter.parent_cancel:
                    waiter.add_exception_notes("最终本地责任控制异常", error)
            elif original is None:
                original = error
            else:
                # 后续内部控制异常不替换已保存的原对象。
                waiter.attach_exception_notes(
                    original, "最终本地责任控制异常", error)
    except BaseException as error:
        if waiter.parent_cancel is not None:
            if error is not waiter.parent_cancel:
                waiter.add_exception_notes("必要会话收场异常", error)
        elif original is None:
            original = error
        elif error is not original:
            _RequiredPhaseWaiter.attach_exception_notes(
                original, "后续会话收场异常", error,
            )

    for error in waiter.take_stop_errors():
        if isinstance(error, Exception):
            local_errors.append(error)
        elif original is None:
            original = error
        elif error is not original:
            waiter.attach_exception_notes(original, "本地停止入口异常", error)

    if waiter.first_control is not None:
        primary_control = waiter.first_control
        if original is not None and original is not primary_control:
            _RequiredPhaseWaiter.attach_exception_notes(
                primary_control, "后续会话异常", original,
            )
        if (waiter.parent_cancel is not None
                and waiter.parent_cancel is not primary_control):
            _RequiredPhaseWaiter.attach_exception_notes(
                primary_control, "后到父取消", waiter.parent_cancel,
            )
        original = primary_control
    elif waiter.parent_cancel is not None:
        if original is not None and original is not waiter.parent_cancel:
            if isinstance(original, Exception):
                waiter.add_exception_notes("会话原始异常", original)
                original = waiter.parent_cancel
            else:
                _RequiredPhaseWaiter.attach_exception_notes(
                    original, "后到父取消", waiter.parent_cancel,
                )
        elif original is None:
            original = waiter.parent_cancel
    outcome, original = _merge_cleanup_errors(
        outcome, original,
        [(error, "shutdown", "state_db_error") for error in local_errors],
        observed_errors,
    )
    # 后面的句柄关闭沿同一原错误身份去重，包括已通过控制附注交付的错误。
    for error in (*local_errors, *waiter._stop_errors):
        _remember_error(observed_errors, error)

    cleanup_failures: list[tuple[BaseException, str, str]] = []
    if context.snapshot_owner is not None:
        try:
            context.snapshot_owner.stop_new_work()
            await waiter.run("snapshot-settlement", context.snapshot_owner.settle)
        except BaseException as error:
            cleanup_failures.append((error, "shutdown", "state_db_error"))
    context.report_change_wm = previous_report_observer
    if admission_lease is not None:
        try:
            admission_lease.close()
        except BaseException as error:
            cleanup_failures.append((error, "shutdown", "internal_error"))
    if session_lease is not None:
        try:
            session_lease.close()
        except BaseException as error:
            cleanup_failures.append((error, "shutdown", "internal_error"))
    if database is not None:
        try:
            await database.close()
        except BaseException as error:
            cleanup_failures.append((error, "shutdown", "state_db_error"))
    outcome, original = _merge_cleanup_errors(
        outcome, original, cleanup_failures, observed_errors,
    )
    if original is not None:
        raise original.with_traceback(original.__traceback__)
    assert outcome is not None
    return outcome


def _read_report_change_wm(connection: Any) -> int:
    """以业务序号索引观察已提交变化，不取得写事务或新建连接。"""
    from camctl.reporting.ack import validate_watermark

    with closing(connection.execute("SELECT MAX(change_seq) FROM history_events")) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise StateDbFailure("报告业务水位查询没有返回结果")
    watermark = 0 if row[0] is None else row[0]
    validate_watermark(watermark, "latest_change_wm")
    return watermark


async def _merge_emergency_stops(
        context: SessionContext, outcome: SessionOutcome) -> SessionOutcome:
    """执行应急停止并把诊断并入原错误；收场失败不掩盖原错误。"""
    from dataclasses import replace as _replace

    try:
        stops = list(await context.emergency_settlement())
    except Exception as error:
        stops = [{"outcome": "not_attempted", "attempts_used": 0,
                  "record_status": "not_recorded",
                  "error": f"应急收场失败: {error}"}]
    if not stops:
        return outcome
    details = dict(outcome.details)
    existing = list(details.get("emergency_stops", ()))
    details["emergency_stops"] = existing + stops
    return _replace(outcome, details=details)


def _work_facts_query(context: SessionContext):
    """在主协程取得本地责任，再将不可变计数交给数据库操作。"""
    pending = 0 if context.local_work is None else context.local_work.required_settlements()
    if isinstance(pending, bool) or not isinstance(pending, int) or pending < 0:
        raise StateDbFailure("本地实际责任数量不可可靠核实")
    def query(connection):
        facts = context.facts_query(connection)
        if facts.required_settlements is None:
            return facts
        return replace(facts, required_settlements=facts.required_settlements + pending)
    return query


async def _run_work_facts(context: SessionContext) -> WorkFacts:
    query = _work_facts_query(context)
    return await context.database.read(
        lambda owned: query(owned.connection), description="会话工作事实")


async def _settle_local_work(
        context: SessionContext, *, stop_new: bool = True,
        exclude_owner: Any = None, waiter: _RequiredPhaseWaiter | None = None) -> None:
    """停止失败也等待原实际收场；观察者的重复取消不重发请求。"""
    records = waiter if waiter is not None else _RequiredPhaseWaiter(context)
    if stop_new:
        records.request_stop()

    async def collect() -> _RequiredPhaseFact:
        try:
            if context.local_work is not None:
                if exclude_owner is None:
                    await context.local_work.settle()
                else:
                    settle = getattr(context.local_work, "settle_excluding", None)
                    if settle is None:
                        raise TypeError("本地责任组合不支持按 owner 分阶段收场")
                    await settle(exclude_owner)
        except BaseException as error:
            records.record_phase_error(error)
            return _RequiredPhaseFact(error=error)
        return _RequiredPhaseFact()

    task = asyncio.create_task(collect(), name="camctl-local-work-fact")
    cancellation: asyncio.CancelledError | None = None
    control_before_cancellation: BaseException | None = None
    while True:
        try:
            fact = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
                control_before_cancellation = records.first_control
                records.request_stop()
                records.request_sweep_cancel()

    errors = list(records.take_stop_errors())
    if fact.error is not None and not any(fact.error is error for error in errors):
        errors.append(fact.error)
    if cancellation is not None:
        # 仅本观察者的取消在此交付；共享waiter的父取消留给run边界。
        control = control_before_cancellation
        if (control is not None and control is not records.parent_cancel
                and any(any(control is item for item in _error_identity_objects(error))
                        for error in errors)):
            _RequiredPhaseWaiter.attach_exception_notes(control, "后到本地收场等待取消", cancellation)
            primary = control
        else:
            primary = cancellation
        others = [error for error in errors if error is not primary]
        for error in others:
            _attach_ordered_error_notes(
                primary, "本地实际责任收场异常", _ordered_error_sources(error),
                diagnostic_error=error,
            )
        def references_primary(error: BaseException) -> bool:
            # 原组、锁事实及其已有cause都不能形成返回主对象的环。
            pending = [error]
            seen: set[int] = set()
            while pending:
                for item in _error_identity_objects(pending.pop()):
                    if item is primary:
                        return True
                    if id(item) not in seen:
                        seen.add(id(item))
                        if item.__cause__ is not None:
                            pending.append(item.__cause__)
            return False

        # 已有cause保持；不能反向链接到能够回到主对象的诊断。
        causes = [error for error in others if not references_primary(error)]
        if primary.__cause__ is None and causes:
            cause = causes[0] if len(causes) == 1 else BaseExceptionGroup("本地停止及实际收场失败", causes)
            raise primary from cause
        raise primary
    if not errors:
        return
    control = next((error for error in errors if not isinstance(error, Exception)), None)
    if control is not None:
        for error in errors:
            if error is not control:
                _attach_ordered_error_notes(
                    control, "本地实际责任收场异常", _ordered_error_sources(error),
                    diagnostic_error=error,
                )
        raise control.with_traceback(control.__traceback__)
    if len(errors) == 1:
        raise errors[0].with_traceback(errors[0].__traceback__)
    raise ExceptionGroup("本地停止及实际收场失败", errors)



async def _drive_flows(context: SessionContext) -> SessionOutcome | None:
    """逐个驱动已注册流程；单个失败不中断其余流程。

    报告流程（"report"）的失败保留责任并触发一次日志副本，设备
    等其他工作继续；报告流程因状态库错误失败同样属于报告处理失
    败，先触发日志副本再停止后续流程，会话按状态库错误收场。其
    余流程的状态库错误立即停止后续流程。返回含原诊断的致命结果或 None。
    """
    for name, flow in context.flows.items():
        # 后台保存错误属于原设备责任，不能套用即将调用的报告失败规则。
        try:
            check = getattr(getattr(context, "local_work", None), "check_completed", None)
            if check is not None:
                check()
        except (ClockBecameUntrusted, DirectoryBindingError, DeviceConfigurationError):
            raise
        except StateDbFailure as error:
            return _outcome_error("state_db_error", {
                "error": str(error), "exception": exception_diagnostic(error),
            })
        except Exception as error:
            return _outcome_error("state_db_error", {
                "error": f"后台流程异常: {error}",
                "exception": exception_diagnostic(error),
            })
        try:
            if name == "report" and not getattr(flow, "owns_report_error_boundary", False):
                await _advance_report_flow(context, flow)
            else:
                await flow(context)
        except ClockBecameUntrusted:
            raise
        except (DirectoryBindingError, DeviceConfigurationError):
            raise
        except StateDbFailure as error:
            return _outcome_error("state_db_error", {
                "error": str(error), "exception": exception_diagnostic(error),
            })
        except Exception as error:
            return _outcome_error("state_db_error", {
                "error": f"流程 {name} 异常: {error}",
                "exception": exception_diagnostic(error),
            })
    return None


async def _advance_report_flow(context: SessionContext, flow, *, on_fatal=None) -> None:
    """报告调用及后台实际任务共用同一失败分类和日志入口。"""
    try:
        await flow(context)
    except ClockBecameUntrusted:
        raise
    except (DirectoryBindingError, DeviceConfigurationError, StateDbFailure) as error:
        if on_fatal is not None:
            on_fatal(error)
        try:
            await _trigger_failure_log(context, error)
        except BaseException as diagnostic_error:
            _RequiredPhaseWaiter.attach_exception_notes(
                error, "报告失败日志交付异常", diagnostic_error)
        raise
    except Exception as error:
        await _trigger_failure_log(context, error)


async def _trigger_failure_log(context: SessionContext, error: Exception) -> None:
    """报告首次失败触发一次日志副本交付；副本失败不扩大错误。"""
    if context.failure_log is None or context.copy_request_factory is None:
        return

    async def operation() -> None:
        await _trigger_failure_log_owned(context, error)

    owner = getattr(context, "logging_owner", None)
    if owner is not None:
        await owner.run_service_operation("report_failure", operation)
        return
    await _trigger_failure_log_owned(context, error)


async def _trigger_failure_log_owned(
        context: SessionContext, error: Exception) -> None:

    owner = getattr(context, "logging_owner", None)
    stage = "request_factory"

    def request_factory() -> CopyRequest:
        nonlocal stage
        request = context.copy_request_factory(error)
        stage = "delivery_call"
        return request

    def consume_unsafe_write_result(write_result: Any) -> bool:
        """保留 ownerless/custom delivery 的共享日志停用回执消费。"""
        if (write_result is None
                or not write_result.disables_file_logging):
            return False
        callback = getattr(context, "on_log_path_unsafe", None)
        if callback is not None:
            try:
                callback()
            except Exception:
                pass
        return True

    async def diagnose(receipt, failure: BaseException | None) -> None:
        if failure is not None:
            from camctl.logging_runtime.copies import (
                _copy_receipt_from_exception,
                _write_result_from_exception,
            )

            error_receipt = _copy_receipt_from_exception(failure)
            if error_receipt is not None:
                message, stderr_only = _copy_receipt_diagnostic(
                    error, error_receipt)
                stderr_only = (
                    consume_unsafe_write_result(error_receipt.write_result)
                    or stderr_only
                )
                if message is not None:
                    await _deliver_log_diagnostic(
                        context, message, stderr_only=stderr_only)
                return

            write_result = _write_result_from_exception(failure)
            stderr_only = consume_unsafe_write_result(write_result)
            await _deliver_log_diagnostic(
                context,
                _failure_copy_exception(
                    stage, error, failure, write_result=write_result),
                stderr_only=stderr_only,
            )
            return
        message, stderr_only = _copy_receipt_diagnostic(error, receipt)
        stderr_only = (
            consume_unsafe_write_result(
                receipt.write_result if receipt is not None else None)
            or stderr_only
        )
        if message is not None:
            await _deliver_log_diagnostic(
                context, message, stderr_only=stderr_only)

    try:
        if owner is None:
            receipt = await context.failure_log.on_report_failure_with_factory(
                request_factory)
        else:
            await context.failure_log.on_report_failure_with_factory(
                request_factory, diagnose=diagnose)
    except Exception as failure:
        # 调用异常可能发生在文件操作之后；保持副本位置未知并保留
        # 主报告结果，不重试或另行推测文件事实。
        if owner is None:
            await diagnose(None, failure)
        return
    if owner is None:
        await diagnose(receipt, None)


def _failure_copy_exception(
    stage: str, report_error: Exception, failure: BaseException,
    *, write_result: Any = None,
) -> str:
    result_detail = ""
    if write_result is not None:
        facts = [
            f"appended={write_result.appended}",
            f"disables_file_logging={write_result.disables_file_logging}",
        ]
        if write_result.failure_stage is not None:
            facts.append(f"failure_stage={write_result.failure_stage.value}")
        for label, value in (
                ("rotation_error", write_result.rotation_error),
                ("append_error", write_result.append_error),
                ("coordination_error", write_result.coordination_error),
                ("close_error", write_result.close_error)):
            if value is not None:
                facts.append(f"{label}={value}")
        if write_result.copy_handles_closed is not True:
            facts.append(
                "copy_handles_closed="
                f"{write_result.copy_handles_closed!r}")
        for label, value in (
                ("copy_source_close_error", write_result.copy_source_close_error),
                ("copy_target_close_error", write_result.copy_target_close_error)):
            if value is not None:
                facts.append(f"{label}={value}")
        result_detail = "; write_result: " + "; ".join(facts)
    return (
        "报告失败的日志副本诊断："
        f"{_describe('report_error', report_error)}; "
        f"stage={stage}; outcome=unknown; "
        f"{_describe('error', failure)}{result_detail}"
    )


def _marker_save_diagnostic(stage: str, save: MarkerSave) -> str | None:
    cleanup = save.temp_cleanup
    cleanup_problem = (
        cleanup is not None
        and (cleanup.outcome is not DeleteOutcome.NOT_PRESENT
             or cleanup.error is not None)
    )
    cleanup_unattempted = save.temp_name is not None and cleanup is None
    if (save.outcome is SaveOutcome.SAVED and save.error is None
            and not cleanup_problem and not cleanup_unattempted):
        return None
    return (
        f"{stage}: outcome={save.outcome.value}; "
        f"error={save.error or 'none'}; "
        f"temp_name={save.temp_name or 'none'}; "
        f"temp_cleanup={cleanup.outcome.value if cleanup else 'not_attempted'}; "
        f"temp_cleanup_error={cleanup.error if cleanup and cleanup.error else 'none'}"
    )


def _copy_receipt_diagnostic(
    report_error: Exception, receipt: CopyReceipt | None,
) -> tuple[str | None, bool]:
    """把回执中实际保存的各阶段错误汇成一条附属诊断。"""
    if receipt is None:
        return None, False
    issues: list[str] = []
    check = receipt.marker_check
    if (check is not None
            and (check.error is not None
                 or check.presence is MarkerPresence.CHECK_ERROR)):
        issues.append(
            f"marker_check: presence={check.presence.value}; "
            f"error={check.error or 'check outcome unknown'}"
        )
    marker_create = receipt.marker_create
    if marker_create is not None:
        detail = _marker_save_diagnostic("marker_create", marker_create)
        if detail is not None:
            issues.append(detail)
    for stage, save in receipt.marker_progress:
        detail = _marker_save_diagnostic(
            f"marker_progress[{stage.value}]", save)
        if detail is not None:
            issues.append(detail)
        check = save.marker_check
        if (check is not None
                and (check.error is not None
                     or check.presence is MarkerPresence.CHECK_ERROR)):
            issues.append(
                f"marker_progress[{stage.value}].marker_check: "
                f"presence={check.presence.value}; "
                f"error={check.error or 'check outcome unknown'}"
            )
    write_result = receipt.write_result
    if write_result is not None:
        if write_result.failure_stage is not None:
            issues.append(
                f"copy_write_stage={write_result.failure_stage.value}")
        for label, detail in (
                ("rotation_error", write_result.rotation_error),
                ("append_error", write_result.append_error),
                ("coordination_error", write_result.coordination_error),
                ("close_error", write_result.close_error)):
            if detail is not None:
                issues.append(f"copy_{label}: {detail}")
        if write_result.copy_handles_closed is not True:
            status = (
                "not_confirmed" if write_result.copy_handles_closed is None
                else "failed_or_unknown")
            issues.append(f"copy_handles_closed: {status}")
        for label, detail in (
                ("copy_source_close_error", write_result.copy_source_close_error),
                ("copy_target_close_error", write_result.copy_target_close_error)):
            if detail is not None:
                issues.append(f"{label}: {detail}")
    for label, detail in (
            ("copy_error", receipt.copy_error),
            ("publish_error", receipt.publish_error),
            ("cleanup_error", receipt.cleanup_error)):
        if detail is not None:
            issues.append(f"{label}: {detail}")
    legacy_error = receipt.error
    if (legacy_error is not None
            and not _legacy_error_is_stage_duplicate(
                receipt, write_result, legacy_error)):
        issues.append(f"error: {legacy_error}")
    if (receipt.outcome in {
            CopyOutcome.MARKER_UNAVAILABLE, CopyOutcome.MARKER_CREATE_FAILED,
            CopyOutcome.COPY_FAILED, CopyOutcome.COPY_UNKNOWN,
    } and not issues):
        issues.append(
            f"copy_outcome={receipt.outcome.value}; "
            f"error={legacy_error or 'none'}")
    if not issues:
        return None, False
    if receipt.file_stage is not None:
        issues.append(f"file_stage={receipt.file_stage.value}")
    if receipt.copy_location is not None:
        issues.append(f"copy_location={receipt.copy_location.value}")
    if receipt.publish_result is not None:
        publish = receipt.publish_result
        issues.append(
            "publish_result: "
            f"stage={publish.stage.value}; "
            f"directory={publish.directory.value}; "
            f"source_removed={publish.source_removed!r}; "
            f"error={publish.error or 'none'}"
        )
    issues.append(f"cleanup_attempted={receipt.cleanup_attempted}")
    stage = receipt.stage.value if receipt.stage is not None else "unknown"
    identity = (
        f"round_id={receipt.round_id or 'unknown'}; "
        f"copy_name={receipt.copy_name or 'unknown'}; "
        f"marker_stage={stage}"
    )
    message = (
        "报告失败的日志副本诊断："
        f"{_describe('report_error', report_error)}; "
        f"outcome={receipt.outcome.value}; {identity}; "
        + "; ".join(issues)
    )
    # 文件 I/O 或共享协调失败后停用本进程后续文件日志；
    # 直接写 stderr，不再触碰同一路径。
    stderr_only = _copy_receipt_log_path_unsafe(receipt)
    return message, stderr_only


def _legacy_error_is_stage_duplicate(
    receipt: CopyReceipt, write_result: Any, legacy_error: str,
) -> bool:
    """只省略生产者明确重复到 legacy 字段的同一步错误。"""
    if receipt.outcome in (CopyOutcome.MARKER_UNAVAILABLE,
                           CopyOutcome.SUPPRESSED):
        return (receipt.marker_check is not None
                and legacy_error == receipt.marker_check.error)
    if receipt.outcome is CopyOutcome.MARKER_CREATE_FAILED:
        return (receipt.marker_create is not None
                and legacy_error == receipt.marker_create.error)
    if receipt.outcome is CopyOutcome.COPY_FAILED:
        expected = "; ".join(part for part in (
            receipt.copy_error,
            write_result.append_error if write_result is not None else None,
            receipt.cleanup_error,
        ) if part)
        return bool(expected) and legacy_error == expected
    if receipt.outcome is CopyOutcome.COPY_UNKNOWN:
        return (
            receipt.copy_error is not None
            and legacy_error == receipt.copy_error
        ) or (
            receipt.publish_error is not None
            and legacy_error == receipt.publish_error
        )
    return False


def _copy_receipt_log_path_unsafe(receipt: CopyReceipt | None) -> bool:
    result = receipt.write_result if receipt is not None else None
    return result is not None and result.disables_file_logging


async def _deliver_log_diagnostic(
    context: SessionContext,
    message: str,
    *,
    stderr_only: bool = False,
) -> None:
    """写一次附属诊断；出口故障不递归触发报告副本或改写会话。"""
    if stderr_only:
        callback = context.on_stderr_diagnostic
        if callback is not None:
            try:
                await callback(message)
            except Exception:
                pass
        return
    callback = context.on_log_diagnostic
    if callback is not None:
        try:
            await callback(message)
            return
        except Exception:
            pass
    fallback = context.on_stderr_diagnostic
    if fallback is not None:
        try:
            await fallback(message)
        except Exception:
            pass


async def _try_close(
    context: SessionContext, database: Any,
    admission_lease: AdmissionLease,
    observed_errors: list[BaseException] | None = None,
) -> SessionOutcome | None:
    """在关闭事务内重查工作并按结果释放接纳；仍需驱动时返回 None。"""
    from camctl.persistence.repositories.session import CloseAdmission

    try:
        key = new_operation_key()
        query = _work_facts_query(context)
        outcome = await database.write(
            lambda owned: context.session_repository.close_admission(
                CloseAdmission(facts_query=query, release_admission=admission_lease.close),
                key, owned), key=key, description="原子关闭接纳")
    except Exception as error:
        if observed_errors is not None:
            _remember_error(observed_errors, error)
        receipt = write_receipt_for(error)
        kind = None
        if receipt is not None:
            kind = {
                "completed": DbOutcomeKind.COMPLETED,
                "rolled_back": DbOutcomeKind.ROLLED_BACK,
                "unknown": DbOutcomeKind.UNKNOWN,
            }.get(receipt.kind)
        return _transaction_error_outcome(
            error,
            kind or DbOutcomeKind.UNKNOWN,
            stage="handoff",
        )
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        if observed_errors is not None:
            _remember_error(observed_errors, outcome.error)
        return _transaction_error_outcome(
            outcome.error, outcome.kind, stage="handoff",
        )
    decision = outcome.value.work
    if decision.kind is WorkDecisionKind.EXIT_REPORT_ERROR:
        return _outcome_error("report_error")
    if decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS:
        return SessionOutcome(succeeded=True)
    return None


def _pending_deadline_seconds(
    now: int, owned: OwnedConnection,
) -> float | None:
    """最近一个待执行动作的下一生命周期时刻距现在的秒数。

    计划时间之前等待到计划时间；已到期、有启动窗口的拍摄动作等
    待到窗口结束（过期判定时刻）。没有待执行动作或都没有下一时
    刻时为 None。
    """
    try:
        with closing(owned.connection.execute(
                "SELECT 1 FROM actions WHERE status=1 AND scheduled_at IS NOT NULL LIMIT 1")) as cursor:
            pending = cursor.fetchone()
    except Exception:
        # 截止查询失败不改变分类：责任检查仍按各自错误规则处理。
        return None
    if pending is None:
        return None
    try:
        with closing(owned.connection.execute(
                "SELECT MIN(CASE WHEN scheduled_at>? THEN scheduled_at"
                " WHEN type IN (1,2,3,8) AND max_delay_ms IS NOT NULL"
                " THEN scheduled_at+max_delay_ms*1000 END)"
                " FROM actions WHERE status=1 AND scheduled_at IS NOT NULL", (now,))) as cursor:
            next_time = cursor.fetchone()[0]
    except Exception:
        # 聚合查询同样只影响本轮等待；责任检查仍负责报告状态库错误。
        return None
    if next_time is None:
        return None
    return max((next_time - now) / 1_000_000, 0.0)


async def _wait_for_more_work(
    context: SessionContext, database: Any, *, observed=None,
) -> None:
    """等待下一轮推进：最近待执行截止、进程内通知或轮询上限。

    外部 submit 是独立进程，新计划按轮询上限发现；进程内生产者经
    唤醒通知立即触发。等待期间保持接纳资格，取消按收尾路径释放。
    """
    wait_s = context.poll_interval_s
    now_us = context.clock.utc_micros()
    deadline = await database.read(
        lambda owned: _pending_deadline_seconds(now_us, owned), description="下一业务截止")
    if deadline is not None:
        wait_s = min(wait_s, deadline)
    loop = asyncio.get_running_loop()
    now = loop.time()
    for flow in context.flows.values():
        next_wakeup = getattr(flow, "next_wakeup_at", None)
        if next_wakeup is not None:
            at = next_wakeup(now, context.wakeup_margin_s)
            if at is not None:
                wait_s = min(wait_s, max(0, at - now))
    wait_s = max(wait_s, _MIN_IDLE_WAIT_S)
    if context.wake is None:
        await asyncio.sleep(wait_s)
        return
    # 本轮查询前的版本覆盖流程内安排的下一批，以及查询后到等待前的通知。
    # 通知已到达时仍交回事件循环，避免连续页面挤占其他设备或取消收场。
    await asyncio.sleep(0)
    await context.wake.wait_changed(context.wake.snapshot() if observed is None else observed,
                                    loop.time() + wait_s)


async def _restricted_session(context: SessionContext) -> SessionOutcome:
    """受限会话：规定收场后处理一次报告，按时钟异常退出。"""
    failure = await _run_restricted_flows(context)
    if failure is not None:
        return failure
    return await _restricted_once_report(context)


async def _initial_restricted_phase(
        context: SessionContext, waiter: _RequiredPhaseWaiter) -> SessionOutcome:
    """保护原受限收场；取消后不创建尚未开始的一次报告责任。"""
    failure = await _run_restricted_flows(context)
    if failure is not None:
        return failure
    if waiter.parent_cancel is not None:
        return _outcome_error("clock_invalid")
    return await _restricted_once_report(context)


async def _run_restricted_flows(
        context: SessionContext) -> SessionOutcome | None:
    """按原顺序推进受限取消与必要设备收场，不取得普通资格。"""
    for name, flow in context.restricted_flows.items():
        try:
            await flow(context)
        except (DirectoryBindingError, DeviceConfigurationError) as error:
            return _outcome_error("configuration_error", {"error": str(error)})
        except Exception as error:
            # 期间的状态库等会话错误保留为实际主错误，不再执行
            # 依赖已失效条件的报告工作。
            return _outcome_error("state_db_error",
                                  {"error": f"受限收场流程 {name} 失败: {error}"})
    return None


async def _restricted_once_report(context: SessionContext) -> SessionOutcome:
    """执行既有唯一受限报告机会，不启动普通动作。"""
    if context.once_report is not None:
        try:
            await context.once_report(context)
        except ClockBecameUntrusted:
            raise
        except (DirectoryBindingError, DeviceConfigurationError) as error:
            await _trigger_failure_log(context, error)
            return _outcome_error("configuration_error", {"error": str(error)})
        except StateDbFailure as error:
            await _trigger_failure_log(context, error)
            return _outcome_error("state_db_error", {"error": str(error)})
        except Exception as error:
            # 一次报告机会失败：保留诊断与责任，本次不循环重试，
            # 仍按时钟异常退出。
            await _trigger_failure_log(context, error)
    return _outcome_error("clock_invalid")


def _acceptance_context(context: SessionContext):
    from camctl.acceptance.service import AcceptanceContext

    return AcceptanceContext(
        mode=context.mode,
        catalog=context.catalog,
        repository=context.acceptance_repository,
        clock=context.clock,
        submit_handoff=SubmitHandoff(
            facts_query=context.facts_query,
            probe=lambda: probe_admission(context.paths.admission_lock),
        ) if context.mode is CommandMode.SUBMIT else None,
    )


def _clock_context(context: SessionContext, database: Any):
    return type(
        "ClockExecutionContext",
        (),
        {
            "repository": context.session_repository,
            "operation_key": new_operation_key(),
            "database": database,
        },
    )()
