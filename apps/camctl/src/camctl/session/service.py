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
from typing import Any, Callable, Mapping, Protocol

from camctl.acceptance.input import InputDiagnostic, ParsedInput
from camctl.acceptance.notification import notify_acceptance
from camctl.acceptance.service import AcceptanceResult, CommandMode, accept_input
from camctl.contracts.clock import ClockPort
from camctl.contracts.values import OperationKey, new_operation_key
from camctl.devices.bindings import DeviceConfigurationError
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
    #: 进程内工作唤醒通知；等待新工作时由生产者触发。
    wake: Any = None
    #: 外部输入感知的轮询上限（秒）；部署目标为 0.5 秒量级。
    poll_interval_s: float = 0.5
    #: run 取得会话锁并可靠打开状态库后，业务事务前固定恢复输入。
    on_session_open: Callable[[OwnedConnection], None] | None = None
    #: 实际文件调用、结果保存和独立连接关闭均完成后才结束的责任。
    local_work: LocalWorkPort | None = None

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

    async def drive() -> SessionOutcome:
        nonlocal session_lease, admission_lease, owned
        try:
            session_lease = context.acquire_session()
        except Exception as error:
            return _outcome_error("state_db_error", {"error": f"会话锁取得失败: {error}"})
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
            return await _restricted_session(context)

        try:
            admission_lease = context.acquire_admission()
        except Exception as error:
            return _outcome_error("state_db_error", {"error": f"接纳锁取得失败: {error}"})

        while True:
            try:
                fatal = await _drive_flows(context)
            except ClockBecameUntrusted as clock_error:
                try:
                    await _settle_local_work(context)
                except Exception as error:
                    local_errors.append(error)
                    # 收场结果不能可靠保存后，受限工作也不能继续使用
                    # 状态库；接纳及会话锁留到统一的实际收场结束。
                    return _outcome_error(
                        "clock_invalid", {"error": str(clock_error)})
                admission_lease.close()
                admission_lease = None
                return await _restricted_session(context)
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
                try:
                    await _settle_local_work(context, stop_new=False)
                except Exception as error:
                    local_errors.append(error)
                    # 收场前提失效后不继续关闭事务；保留原退出分类，
                    # 最终结果统一附加本地收场的真实诊断。
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
            original = error
        try:
            await _settle_local_work(context)
        except BaseException as error:
            if isinstance(error, Exception):
                if not any(error is saved for saved in local_errors):
                    local_errors.append(error)
            elif original is None:
                original = error
            else:
                # 后续取消只改变收场等待，原取消仍是传播对象；
                # 实际结果保存失败的诊断须随原异常一起保留。
                for note in getattr(error, "__notes__", ()):
                    if note not in getattr(original, "__notes__", ()):
                        original.add_note(note)
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


def _run_work_facts(context: SessionContext, connection: Any) -> WorkFacts:
    """只为正常 run 补入本次已开始的实际收场，持久化未知保持。"""
    facts = context.facts_query(connection)
    if context.local_work is None or facts.required_settlements is None:
        return facts
    pending = context.local_work.required_settlements()
    if isinstance(pending, bool) or not isinstance(pending, int) or pending < 0:
        raise StateDbFailure("本地实际责任数量不可可靠核实")
    return replace(facts, required_settlements=facts.required_settlements + pending)


async def _settle_local_work(context: SessionContext, *, stop_new: bool = True) -> None:
    """重复取消只取消等待；同一个拥有者始终继续消费实际结果。"""
    if context.local_work is None:
        return
    if stop_new:
        stop = getattr(context.local_work, "stop_new_work", None)
        if stop is not None:
            stop()
    task = asyncio.create_task(context.local_work.settle())
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.done() and task.cancelled():
                raise
            if cancellation is None:
                cancellation = error
        except Exception as error:
            if cancellation is not None:
                cancellation.add_note(f"本地责任收场失败: {type(error).__name__}: {error}")
                raise cancellation from error
            raise
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
    try:
        await context.failure_log.on_report_failure(
            context.copy_request_factory(error))
    except Exception:
        # 副本交付失败按其自身规则处理，不改写本次报告错误。
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
    if context.once_report is not None:
        try:
            await context.once_report(context)
        except ClockBecameUntrusted:
            raise
        except (DirectoryBindingError, DeviceConfigurationError) as error:
            return _outcome_error("configuration_error", {"error": str(error)})
        except StateDbFailure as error:
            return _outcome_error("state_db_error", {"error": str(error)})
        except Exception:
            # 一次报告机会失败：保留诊断与责任，本次不循环重试，
            # 仍按时钟异常退出。
            pass
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
