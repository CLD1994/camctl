"""会话流程与有序收尾。

run 与 submit 共用本入口：受理、时钟资格、接纳与退出检查按既定
顺序组织。run 逐个驱动已注册流程：报告失败不阻塞设备工作并触发
一次日志副本，状态库错误停止依赖已失效条件的工作；流程完成后在
关闭事务内重查工作并按结果释放接纳。受限会话执行规定收场与一次
报告机会后按时钟异常退出，不取得普通接纳。submit 对提交结果未
知以同一请求幂等核实，不宣称受理成功或失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from camctl.acceptance.input import InputDiagnostic, ParsedInput
from camctl.acceptance.notification import notify_acceptance
from camctl.acceptance.service import AcceptanceResult, CommandMode, accept_input
from camctl.contracts.clock import ClockPort
from camctl.contracts.values import OperationKey, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.session.clock import (
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
from camctl.session.work import WorkDecisionKind, WorkFacts

__all__ = ["SessionContext", "StateDbFailure", "run_session"]


class StateDbFailure(RuntimeError):
    """流程遭遇状态库错误：停止依赖已失效条件的工作。"""


class AcceptanceRepositoryPort(Protocol):
    def process_input(self, command, key: OperationKey, owned: OwnedConnection): ...


class SessionRepositoryPort(Protocol):
    def close_admission(self, command, key: OperationKey, owned: OwnedConnection): ...

    def update_lower_bound(self, check, key: OperationKey, owned: OwnedConnection): ...


@dataclass(frozen=True)
class SessionPaths:
    """会话锁与接纳锁文件路径。"""

    session_lock: Any
    admission_lock: Any


@dataclass
class SessionContext:
    """一次会话的协作者与运行参数。

    flows 是已注册的业务流程端口（调度、设备、报告），按名称接
    入；名为 "report" 的流程失败按报告错误处理并触发日志副本，
    其余流程异常按状态库错误收口。restricted_flows 与 once_report
    是受限会话的规定收场和一次报告端口。facts_query 在关闭事务内
    重查工作事实。
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
    try:
        try:
            session_lease = context.acquire_session()
        except Exception as error:
            return _outcome_error("state_db_error", {"error": f"会话锁取得失败: {error}"})
        try:
            owned = context.open_connection()
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

        fatal = await _drive_flows(context)
        if fatal is not None:
            # 状态库错误：停止依赖已失效条件的工作，接纳随进程退出。
            return _outcome_error("state_db_error", {"error": fatal})
        return await _close_and_finish(context, owned, admission_lease)
    finally:
        # 收尾次序：释放接纳与会话句柄后关闭数据库连接。
        if admission_lease is not None:
            admission_lease.close()
        if session_lease is not None:
            session_lease.close()
        if owned is not None:
            owned.connection.close()


async def _drive_flows(context: SessionContext) -> str | None:
    """逐个驱动已注册流程；单个失败不中断其余流程。

    报告流程（"report"）的失败保留责任并触发一次日志副本，设备
    等其他工作继续；状态库错误立即停止后续流程。返回致命错误描
    述或 None。
    """
    for name, flow in context.flows.items():
        try:
            await flow(context)
        except StateDbFailure as error:
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


async def _close_and_finish(
    context: SessionContext, owned: OwnedConnection,
    admission_lease: AdmissionLease,
) -> SessionOutcome:
    """在关闭事务内重查工作并按结果释放接纳，组织最终结果。"""
    from camctl.persistence.repositories.session import CloseAdmission

    try:
        outcome = context.session_repository.close_admission(
            CloseAdmission(
                facts_query=context.facts_query,
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
    return SessionOutcome(succeeded=True)


async def _restricted_session(context: SessionContext) -> SessionOutcome:
    """受限会话：规定收场后处理一次报告，按时钟异常退出。"""
    for name, flow in context.restricted_flows.items():
        try:
            await flow(context)
        except Exception as error:
            # 期间的状态库等会话错误保留为实际主错误，不再执行
            # 依赖已失效条件的报告工作。
            return _outcome_error("state_db_error",
                                  {"error": f"受限收场流程 {name} 失败: {error}"})
    if context.once_report is not None:
        try:
            await context.once_report(context)
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
