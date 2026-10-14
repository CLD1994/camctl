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
from camctl.persistence.models import DbOutcomeKind, DirectoryBindingError
from camctl.persistence.runtime import OwnedConnection, RuntimeLibraryError
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
    SessionLease,
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

    async def run(self, name: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        async def capture() -> _RequiredPhaseFact:
            try:
                return _RequiredPhaseFact(value=await operation())
            except BaseException as error:
                return _RequiredPhaseFact(error=error)

        task = asyncio.create_task(capture(), name=f"camctl-required-{name}")
        while True:
            try:
                fact = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                self.record_parent_cancel(error)
        if fact.error is not None:
            self._phase_errors.append(fact.error)
            raise fact.error.with_traceback(fact.error.__traceback__)
        return fact.value

    def is_phase_error(self, error: BaseException) -> bool:
        return any(error is phase_error for phase_error in self._phase_errors)

    def record_parent_cancel(self, error: asyncio.CancelledError) -> None:
        if self.parent_cancel is not None:
            return
        self.parent_cancel = error
        local_work = self._context.local_work
        stop_new = getattr(local_work, "stop_new_work", None)
        if stop_new is not None:
            try:
                stop_new()
            except BaseException as stop_error:
                self.add_note(
                    "父取消时停止本地新增工作的入口失败: "
                    f"{type(stop_error).__name__}: {stop_error}")
        cancel_sweep = getattr(
            self._context.logging_owner, "cancel_startup_sweep", None)
        if cancel_sweep is not None:
            try:
                cancel_sweep()
            except BaseException as stop_error:
                self.add_note(
                    "父取消时停止启动日志检查的入口失败: "
                    f"{type(stop_error).__name__}: {stop_error}")

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
        target.add_note(f"{label}: {type(error).__name__}: {error}")
        for note in notes:
            target.add_note(f"{label}附注: {note}")


class StateDbFailure(RuntimeError):
    """流程遭遇状态库错误：停止依赖已失效条件的工作。"""


class AcceptanceRepositoryPort(Protocol):
    def process_input(self, command, key: OperationKey, owned: OwnedConnection): ...


class SessionRepositoryPort(Protocol):
    def close_admission(self, command, key: OperationKey, owned: OwnedConnection): ...

    def update_lower_bound(self, check, key: OperationKey, owned: OwnedConnection): ...


class LocalWorkPort(Protocol):
    """本次已经开始的实际本地责任，不替代持久化工作事实。"""

    def required_settlements(self) -> int: ...

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
    open_connection: Callable[[], OwnedConnection]
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
    #: run 取得会话锁并可靠打开状态库后，业务事务前固定恢复输入。
    on_session_open: Callable[[OwnedConnection], None] | None = None
    #: 实际文件调用、结果保存和独立连接关闭均完成后才结束的责任。
    local_work: LocalWorkPort | None = None
    #: 非时钟致命错误收场时的录像应急停止端口；返回诊断条目。
    emergency_settlement: Callable[[], Awaitable[list[dict]]] | None = None

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
    try:
        owned = context.open_connection()
    except (RuntimeLibraryError, DirectoryBindingError) as error:
        # 本进程运行库不满足部署要求：配置环境问题，不是状态库数据问题。
        return _outcome_error("configuration_error", {"error": str(error)})
    except Exception as error:
        return _outcome_error("state_db_error", {"error": str(error)})
    try:
        try:
            accepted = await accept_input(
                source,
                _acceptance_context(context, owned),
                new_operation_key(),
                owned,
            )
        except AcceptanceStateError as error:
            unknown = getattr(error, "outcome_kind", None) is DbOutcomeKind.UNKNOWN
            details = {"error": str(error)}
            if unknown:
                # 提交结果未知：不宣称受理失败或成功，保留未知事
                # 实；同一请求的下一次重送按实际持久化状态幂等核
                # 实（受理复用或重新处理）。
                details["unknown_commit"] = True
            return _outcome_error("state_db_error", details)
        if context.notifier is not None:
            notify_acceptance(accepted, context.notifier)
        if type(accepted.needs_run) is not bool:
            return _outcome_error("state_db_error", {"error": "已完成的 submit 缺少接管结果"})
        return SessionOutcome(succeeded=True, needs_run=accepted.needs_run)
    finally:
        owned.connection.close()


async def _run_session(
    context: SessionContext, source: ParsedInput | InputDiagnostic | None
) -> SessionOutcome:
    # 裸 run：无输入文件；带计划启动的 run 在会话锁内受理，
    # 受理事务在时钟检查前结束。
    session_lease: SessionLease | None = None
    admission_lease: AdmissionLease | None = None
    owned: OwnedConnection | None = None
    local_errors: list[Exception] = []
    waiter = _RequiredPhaseWaiter(context)

    async def drive() -> SessionOutcome:
        nonlocal session_lease, admission_lease, owned
        try:
            session_lease = context.acquire_session()
        except Exception as error:
            return _outcome_error("state_db_error", {"error": f"会话锁取得失败: {error}"})
        if context.on_session_locked is not None:
            try:
                await context.on_session_locked()
            except Exception as error:
                return _outcome_error(
                    "state_db_error", {"error": f"日志启动收场未能启动: {error}"})
        if context.startup_outcome is not None:
            return context.startup_outcome
        try:
            owned = context.open_connection()
        except (RuntimeLibraryError, DirectoryBindingError) as error:
            # 同 submit：运行库不满足部署要求归配置错误，先于业务库打开拒绝。
            return _outcome_error("configuration_error", {"error": str(error)})
        except Exception as error:
            return _outcome_error("state_db_error", {"error": str(error)})
        if context.on_session_open is not None:
            try:
                context.on_session_open(owned)
            except Exception as error:
                return _outcome_error("state_db_error", {"error": str(error)})
        if source is not None:
            try:
                await accept_input(
                    source,
                    _acceptance_context(context, owned),
                    new_operation_key(),
                    owned,
                )
            except Exception as error:
                return _outcome_error("state_db_error", {"error": str(error)})

        policy = context.clock_policy(owned.connection)
        check = check_clock(policy, context.clock)
        mode = await enter_execution(
            check,
            _clock_context(context, owned),
        )
        if mode is ExecutionMode.FATAL:
            return _outcome_error("state_db_error", {"error": "可信时间下界保存失败"})
        if mode is ExecutionMode.RESTRICTED:
            # 有限安全收场与一次报告机会：不取得普通接纳资格。
            return await waiter.run(
                "initial-restricted",
                lambda: _initial_restricted_phase(context, waiter),
            )

        try:
            admission_lease = context.acquire_admission()
        except Exception as error:
            return _outcome_error("state_db_error", {"error": f"接纳锁取得失败: {error}"})

        while True:
            try:
                fatal = await _drive_flows(context)
            except ClockBecameUntrusted as clock_error:
                async def transition_to_restricted() -> SessionOutcome:
                    nonlocal admission_lease
                    try:
                        await _settle_local_work(
                            context, exclude_owner=context.logging_owner)
                    except Exception as error:
                        local_errors.append(error)
                        # 前置保存失败后，受限停止和报告不能依赖失效状态。
                        return _outcome_error(
                            "clock_invalid", {"error": str(clock_error)})
                    admission_lease.close()
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
                    if waiter.parent_cancel is not None:
                        return _outcome_error("clock_invalid")
                    return await _restricted_once_report(context)

                return await waiter.run(
                    "late-clock-restricted-transition", transition_to_restricted)
            except (DirectoryBindingError, DeviceConfigurationError) as error:
                return _outcome_error("configuration_error", {"error": str(error)})
            if fatal is not None:
                # 状态库错误：停止依赖已失效条件的工作，接纳随进程退出。
                return _outcome_error("state_db_error", {"error": fatal})
            try:
                decision = classify_work(_run_work_facts(context, owned.connection))
            except Exception as error:
                return _outcome_error("state_db_error", {"error": str(error)})
            if decision.kind is not WorkDecisionKind.NEEDS_DRIVER:
                async def settle_before_close() -> Exception | None:
                    try:
                        await _settle_local_work(context, stop_new=False)
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
                outcome = await _try_close(context, owned, admission_lease)
                if outcome is not None:
                    return outcome
                # 关闭事务观察到新工作：继续推进，不释放接纳。
            await _wait_for_more_work(context, owned)

    outcome: SessionOutcome | None = None
    original: BaseException | None = None
    try:
        try:
            outcome = await drive()
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                if waiter.is_phase_error(error):
                    original = error
                elif waiter.parent_cancel is None:
                    waiter.record_parent_cancel(error)
                elif error is not waiter.parent_cancel:
                    waiter.add_exception_notes("必要收场实际控制异常", error)
            elif waiter.parent_cancel is not None:
                waiter.add_exception_notes("会话实际控制异常", error)
            else:
                original = error
        fatal_outcome = (
            outcome is not None
            and not outcome.succeeded
            and outcome.reason in ("state_db_error", "configuration_error"))
        if fatal_outcome and waiter.parent_cancel is not None:
            waiter.add_note(
                "会话保留实际致命结果："
                f"reason={outcome.reason}; details={outcome.details!r}")
        if (original is None and fatal_outcome
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
                lambda: _settle_local_work(context, stop_new=stop_new),
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
        if waiter.parent_cancel is not None:
            if original is not None and original is not waiter.parent_cancel:
                waiter.add_exception_notes("会话原始异常", original)
            for error in local_errors:
                waiter.add_note(
                    f"本地责任收场失败: {type(error).__name__}: {error}")
            raise waiter.parent_cancel
        if original is not None:
            for error in local_errors:
                original.add_note(f"本地责任收场失败: {type(error).__name__}: {error}")
            raise original
        assert outcome is not None
        for error in local_errors:
            outcome = _local_state_outcome(outcome, error)
        return outcome
    finally:
        # 已开始的本地责任先完成实际结果、保存及独立连接关闭。
        if admission_lease is not None:
            admission_lease.close()
        if session_lease is not None:
            session_lease.close()
        if owned is not None:
            owned.connection.close()


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


def _run_work_facts(context: SessionContext, connection: Any) -> WorkFacts:
    """只为正常 run 补入本次已开始的实际收场，持久化未知保持。"""
    facts = context.facts_query(connection)
    if context.local_work is None or facts.required_settlements is None:
        return facts
    pending = context.local_work.required_settlements()
    if isinstance(pending, bool) or not isinstance(pending, int) or pending < 0:
        raise StateDbFailure("本地实际责任数量不可可靠核实")
    return replace(facts, required_settlements=facts.required_settlements + pending)


async def _settle_local_work(
        context: SessionContext, *, stop_new: bool = True,
        exclude_owner: Any = None) -> None:
    """重复取消只取消等待；同一个拥有者始终继续消费实际结果。"""
    if context.local_work is None:
        return
    if stop_new:
        stop = getattr(context.local_work, "stop_new_work", None)
        if stop is not None:
            stop()
    if exclude_owner is None:
        settle = context.local_work.settle
    else:
        settle_excluding = getattr(context.local_work, "settle_excluding", None)
        if settle_excluding is None:
            raise TypeError("本地责任组合不支持按 owner 分阶段收场")

        async def settle() -> None:
            await settle_excluding(exclude_owner)

    async def collect() -> _RequiredPhaseFact:
        try:
            await settle()
        except BaseException as error:
            return _RequiredPhaseFact(error=error)
        return _RequiredPhaseFact()

    task = asyncio.create_task(collect(), name="camctl-local-work-fact")
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            fact = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.done() and task.cancelled():
                raise
            if cancellation is None:
                cancellation = error
                if not stop_new:
                    stop = getattr(context.local_work, "stop_new_work", None)
                    if stop is not None:
                        try:
                            stop()
                        except BaseException as stop_error:
                            cancellation.add_note(
                                "取消收场时停止本地新增工作的入口失败: "
                                f"{type(stop_error).__name__}: {stop_error}")
                cancel_sweep = getattr(
                    context.logging_owner, "cancel_startup_sweep", None)
                if cancel_sweep is not None:
                    try:
                        cancel_sweep()
                    except BaseException as stop_error:
                        cancellation.add_note(
                            "取消收场时停止启动日志检查失败: "
                            f"{type(stop_error).__name__}: {stop_error}")
    if fact.error is not None:
        if cancellation is not None:
            _RequiredPhaseWaiter.attach_exception_notes(
                cancellation, "本地实际责任收场异常", fact.error)
            if fact.error is cancellation:
                raise cancellation
            raise cancellation from fact.error
        raise fact.error.with_traceback(fact.error.__traceback__)
    if cancellation is not None:
        raise cancellation


def _local_state_outcome(outcome: SessionOutcome, error: Exception) -> SessionOutcome:
    """本地收场错误主导普通成功或报告失败，保留已有致命主错误。"""
    details = {"stage": "shutdown", "message": f"{type(error).__name__}: {error}"}
    if outcome.succeeded:
        return _outcome_error("state_db_error", details)
    previous = dict(outcome.details)
    secondary = list(previous.pop("secondary_errors", ()))
    if outcome.reason == "report_error":
        details["secondary_errors"] = [{"reason": outcome.reason, "details": previous}, *secondary]
        return _outcome_error("state_db_error", details)
    previous["secondary_errors"] = [*secondary, {"reason": "state_db_error", "details": details}]
    return _outcome_error(outcome.reason, previous)


async def _drive_flows(context: SessionContext) -> str | None:
    """逐个驱动已注册流程；单个失败不中断其余流程。

    报告流程（"report"）的失败保留责任并触发一次日志副本，设备
    等其他工作继续；报告流程因状态库错误失败同样属于报告处理失
    败，先触发日志副本再停止后续流程，会话按状态库错误收场。其
    余流程的状态库错误立即停止后续流程。返回致命错误描述或 None。
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
            return str(error)
        except Exception as error:
            return f"后台流程异常: {error}"
        try:
            await flow(context)
        except ClockBecameUntrusted:
            raise
        except (DirectoryBindingError, DeviceConfigurationError) as error:
            if name == "report":
                await _trigger_failure_log(context, error)
            raise
        except StateDbFailure as error:
            if name == "report":
                await _trigger_failure_log(context, error)
            return str(error)
        except Exception as error:
            if name != "report":
                return f"流程 {name} 异常: {error}"
            await _trigger_failure_log(context, error)
    return None


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
    context: SessionContext, owned: OwnedConnection,
    admission_lease: AdmissionLease,
) -> SessionOutcome | None:
    """在关闭事务内重查工作并按结果释放接纳；仍需驱动时返回 None。"""
    from camctl.persistence.repositories.session import CloseAdmission

    try:
        outcome = context.session_repository.close_admission(
            CloseAdmission(
                facts_query=lambda connection: _run_work_facts(context, connection),
                release_admission=admission_lease.close,
            ),
            new_operation_key(),
            owned,
        )
    except Exception as error:
        return _outcome_error("state_db_error", {"error": f"接纳关闭事务失败: {error}"})
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        detail = f"接纳关闭未完成（{outcome.kind.value}）: {outcome.error}"
        return _outcome_error("state_db_error", {"error": detail})
    decision = outcome.value.work
    if decision.kind is WorkDecisionKind.EXIT_REPORT_ERROR:
        return _outcome_error("report_error")
    if decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS:
        return SessionOutcome(succeeded=True)
    return None


def _pending_deadline_seconds(
    context: SessionContext, owned: OwnedConnection,
) -> float | None:
    """最近一个待执行动作的下一生命周期时刻距现在的秒数。

    计划时间之前等待到计划时间；已到期、有启动窗口的拍摄动作等
    待到窗口结束（过期判定时刻）。没有待执行动作或都没有下一时
    刻时为 None。
    """
    try:
        with closing(owned.connection.execute(
                "SELECT type, scheduled_at, max_delay_ms FROM actions"
                " WHERE status = 1 AND scheduled_at IS NOT NULL")) as cursor:
            rows = cursor.fetchall()
    except Exception:
        # 截止查询失败不改变分类：责任检查仍按各自错误规则处理。
        return None
    if not rows:
        return None
    now = context.clock.utc_micros()
    next_times: list[int] = []
    for kind, scheduled_at, max_delay_ms in rows:
        scheduled = int(scheduled_at)
        if now < scheduled:
            next_times.append(scheduled)
        elif int(kind) in (1, 2, 3, 8) and max_delay_ms is not None:
            next_times.append(scheduled + int(max_delay_ms) * 1000)
    if not next_times:
        return None
    return max((min(next_times) - now) / 1_000_000, 0.0)


async def _wait_for_more_work(
    context: SessionContext, owned: OwnedConnection,
) -> None:
    """等待下一轮推进：最近待执行截止、进程内通知或轮询上限。

    外部 submit 是独立进程，新计划按轮询上限发现；进程内生产者经
    唤醒通知立即触发。等待期间保持接纳资格，取消按收尾路径释放。
    """
    wait_s = context.poll_interval_s
    deadline = _pending_deadline_seconds(context, owned)
    if deadline is not None:
        wait_s = min(wait_s, deadline)
    wait_s = max(wait_s, _MIN_IDLE_WAIT_S)
    if context.wake is None:
        await asyncio.sleep(wait_s)
        return
    loop = asyncio.get_running_loop()
    await context.wake.wait_changed(context.wake.snapshot(),
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


def _acceptance_context(context: SessionContext, owned: OwnedConnection):
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


def _clock_context(context: SessionContext, owned: OwnedConnection):
    return type(
        "ClockExecutionContext",
        (),
        {
            "repository": context.session_repository,
            "operation_key": new_operation_key(),
            "owned": owned,
        },
    )()
