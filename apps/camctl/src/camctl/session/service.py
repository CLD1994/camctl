"""会话流程与有序收尾（第一版阶段 1 切片）。

run 与 submit 共用本入口：受理、时钟资格、接纳与退出检查按既定
顺序组织；设备、调度与报告流程通过注册的流程端口接入（当前阶
段无已注册流程，待执行责任保持持久化等待后续会话）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from camctl.acceptance.input import InputDiagnostic, ParsedInput
from camctl.acceptance.notification import notify_acceptance
from camctl.acceptance.service import AcceptanceResult, CommandMode, accept_input
from camctl.contracts.clock import ClockPort
from camctl.contracts.values import OperationKey, new_operation_key
from camctl.persistence.runtime import OwnedConnection
from camctl.session.clock import (
    ClockCheckInput,
    ExecutionMode,
    check_clock,
    enter_execution,
)
from camctl.session.handoff import decide_handoff
from camctl.session.locks import (
    AdmissionLease,
    SessionLease,
    probe_admission,
)
from camctl.session.outcome import SessionOutcome
from camctl.session.work import WorkFacts, classify_work, WorkDecisionKind

__all__ = ["SessionContext", "run_session"]


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
    入；当前阶段可为空。facts_query 在关闭事务内重查工作事实。
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
        except Exception as error:
            return _outcome_error("state_db_error", {"error": str(error)})
        if context.notifier is not None:
            notify_acceptance(accepted, context.notifier)
        try:
            facts = context.facts_query(owned.connection)
            work = classify_work(facts)
            probe = probe_admission(context.paths.admission_lock)
            decision = decide_handoff(work, probe)
        except Exception as error:
            return _outcome_error("state_db_error", {"error": str(error)})
        return SessionOutcome(succeeded=True, needs_run=decision.needs_run)
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
            # 有限收场与一次报告机会：当前无已注册流程，按规则返回
            # 时钟异常，不进入普通调度。
            return _outcome_error("clock_invalid")

        try:
            admission_lease = context.acquire_admission()
        except Exception as error:
            return _outcome_error("state_db_error", {"error": f"接纳锁取得失败: {error}"})

        # 已注册流程的驱动阶段（调度、设备、报告）：当前为空集合，
        # 待执行责任保持持久化，由后续会话按当时条件驱动。
        for flow in context.flows.values():
            await flow(context)

        return SessionOutcome(succeeded=True)
    finally:
        # 收尾次序：释放接纳与会话句柄后关闭数据库连接。
        if admission_lease is not None:
            admission_lease.close()
        if session_lease is not None:
            session_lease.close()
        if owned is not None:
            owned.connection.close()


def _acceptance_context(context: SessionContext, owned: OwnedConnection):
    from camctl.acceptance.service import AcceptanceContext

    return AcceptanceContext(
        mode=context.mode,
        catalog=context.catalog,
        repository=context.acceptance_repository,
        clock=context.clock,
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
