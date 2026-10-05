"""按命令装配运行资源与有序关闭。

不建立全局数据库、驱动或配置单例：每次调用创建本次命令所需的
协作者，流程只依赖端口。run 会话默认装配生产报告流程与生成子
进程监督方，会话结束后监督方先收场；接纳/会话句柄由会话流程自
身释放，装配层最后关闭报告失败日志副本通道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from camctl.acceptance.input import InputDiagnostic, ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.clocks import SystemClock
from camctl.bootstrap.config import ConfigSnapshot
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.repositories.session import SessionRepository
from camctl.session.clock import ClockCheckInput, register_clock_guard
from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths
from camctl.session.outcome import SessionOutcome
from camctl.session.service import SessionContext, SessionPaths, run_session
from camctl.session.work import WorkFacts

__all__ = ["RuntimeDeps", "build_runtime", "close_runtime", "execute_command"]


@dataclass
class RuntimeDeps:
    """一次命令的协作者集合；由 build_runtime 创建、close_runtime 关闭。"""

    mode: CommandMode
    config: ConfigSnapshot
    state_db: Path
    session_lock: Path
    admission_lock: Path
    catalog: Any
    notifier: Any = None
    #: 报告失败日志副本的文件通道；首次触发时创建，关闭时释放。
    failure_channel: Any = None
    closed: bool = field(default=False)


def _default_catalog(config: ConfigSnapshot):
    """从 describe 使用的同一部署定义构建受理目录。"""
    from camctl.devices.catalog import build_catalog, default_driver_definitions

    return build_catalog(config, default_driver_definitions())


def build_runtime(
    command_mode: CommandMode,
    config: ConfigSnapshot,
    *,
    catalog: Any = None,
    notifier: Any = None,
) -> RuntimeDeps:
    """按命令模式创建运行协作者；不隐式创建状态库。"""
    from camctl.persistence.repositories.capture import register_capture_guards
    from camctl.persistence.repositories.cancellation import (
        register_cancellation_guards,
    )
    from camctl.persistence.repositories.operations import register_operation_guards
    from camctl.persistence.repositories.outputs import register_outputs_guards
    from camctl.persistence.repositories.timelapse import register_timelapse_guards
    from camctl.reporting.policy import register_report_guards

    # 生产装配统一注册全部正式业务守卫：运行会话内会发出受理、
    # 动作开始与终态、输出、报告与同步等事务。注册幂等，测试替
    # 身按需覆盖同名守卫。
    register_acceptance_guards()
    register_clock_guard()
    register_operation_guards()
    register_capture_guards()
    register_timelapse_guards()
    register_outputs_guards()
    register_cancellation_guards()
    register_report_guards()
    state_db = Path(config.paths.state_db).expanduser().resolve()
    if not state_db.exists():
        raise FileNotFoundError(f"状态库不存在，日常入口不创建: {state_db}")
    session_lock, admission_lock = lock_file_paths(state_db)
    return RuntimeDeps(
        mode=command_mode,
        config=config,
        state_db=state_db,
        session_lock=session_lock,
        admission_lock=admission_lock,
        catalog=catalog if catalog is not None else _default_catalog(config),
        notifier=notifier,
    )


def _open_connection(state_db: Path) -> OwnedConnection:
    return open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())


def _clock_policy_factory(config: ConfigSnapshot) -> Callable[[Any], ClockCheckInput]:
    from camctl.contracts.values import to_utc_micros

    min_plausible = to_utc_micros(f"{config.clock.min_plausible_date} 00:00:00")

    def build(connection) -> ClockCheckInput:
        row = connection.execute(
            "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1"
        ).fetchone()
        lower = int(row[0]) if row is not None and row[0] is not None else None
        return ClockCheckInput(
            lower_bound_micros=lower,
            min_plausible_micros=min_plausible,
            recheck_delay_s=float(config.clock.recheck_delay_s),
            recheck_count=config.clock.recheck_count,
        )

    return build


def _failure_log_wiring(deps: RuntimeDeps):
    """报告失败日志副本的触发装配：标记、通道与请求工厂。"""
    from camctl.logging_runtime.clh_adapter import FileChannel
    from camctl.logging_runtime.copies import (
        CopyRequest, FailureLogService, MarkerStore,
    )
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogRecord

    staging = Path(deps.config.paths.staging).expanduser()
    logs_dir = staging / "logs"
    channel_box: dict = {}

    def copy_request_factory(error: Exception) -> CopyRequest:
        channel = channel_box.get("channel")
        if channel is None:
            channel = FileChannel(
                Path(deps.config.paths.log_file).expanduser(),
                max_bytes=deps.config.log.max_size,
                file_count=deps.config.log.file_count,
            )
            channel_box["channel"] = channel
            deps.failure_channel = channel
        return CopyRequest(
            trigger=LogRecord(
                level=LogLevel.ERROR,
                message=f"报告处理失败: {type(error).__name__}: {error}",
            ),
            staging_root=staging,
            ready_dir=Path(deps.config.paths.ready).expanduser(),
            channel=channel,
            marker=MarkerStore(logs_dir),
        )

    return FailureLogService(MarkerStore(logs_dir)), copy_request_factory


def _report_assembly(deps: RuntimeDeps) -> tuple[dict[str, Any], Any]:
    """run 会话的生产报告流程与生成子进程监督方。

    注册报告事务守卫后组装维护流程：到期的同步动作、报告冻结、
    子进程生成与发布编排都由流程按轮推进；监督方惰性启动，由
    execute_command 在会话结束后收场。
    """
    from camctl.bootstrap.flows import report_flow
    from camctl.reporting.maintenance import MaintenanceLimits
    from camctl.reporting.supervisor import WorkerSupervisor
    from camctl.reporting.worker import report_lock_path

    supervisor = WorkerSupervisor(
        limits=MaintenanceLimits.defaults(),
        lock_path=str(report_lock_path(deps.state_db)),
    )
    flow = report_flow(
        state_db=deps.state_db,
        staging=Path(deps.config.paths.staging).expanduser().resolve(),
        ready=Path(deps.config.paths.ready).expanduser().resolve(),
        processing=Path(deps.config.paths.processing).expanduser().resolve(),
        history=deps.config.history,
        database=deps.config.database,
        supervisor=supervisor,
    )
    return {"report": flow}, supervisor


async def execute_command(
    deps: RuntimeDeps,
    source: ParsedInput | InputDiagnostic | None,
    *,
    flows: Mapping[str, Any] | None = None,
    wake: Any = None,
    poll_interval_s: float | None = None,
) -> SessionOutcome:
    """执行一次 run/submit 会话；调用方负责运行事件循环。

    flows、wake 与 poll_interval_s 是业务流程装配的注入点：显式
    注入的流程映射整体替换生产装配；run 会话未注入时使用生产报
    告流程（含生成子进程），submit 会话不驱动业务流程。进程内唤
    醒与轮询上限随流程一起接入。
    """
    from camctl.bootstrap.application import query_work_facts

    failure_log, copy_request_factory = _failure_log_wiring(deps)
    supervisor: Any = None
    if flows is None and deps.mode is CommandMode.RUN:
        flows, supervisor = _report_assembly(deps)
    overrides: dict[str, Any] = {
        "flows": flows if flows is not None else {},
        "wake": wake,
    }
    if poll_interval_s is not None:
        overrides["poll_interval_s"] = poll_interval_s
    context = SessionContext(
        mode=deps.mode,
        catalog=deps.catalog,
        clock=SystemClock(),
        open_connection=lambda: _open_connection(deps.state_db),
        acceptance_repository=AcceptanceRepository(),
        session_repository=SessionRepository(),
        paths=SessionPaths(
            session_lock=deps.session_lock, admission_lock=deps.admission_lock
        ),
        clock_policy=_clock_policy_factory(deps.config),
        acquire_session=lambda: acquire_session(deps.session_lock),
        acquire_admission=lambda: acquire_admission(deps.admission_lock),
        facts_query=query_work_facts,
        notifier=deps.notifier,
        failure_log=failure_log,
        copy_request_factory=copy_request_factory,
        **overrides,
    )
    try:
        return await run_session(context, source)
    finally:
        if supervisor is not None:
            await supervisor.stop()


def close_runtime(deps: RuntimeDeps) -> None:  # noqa: D401 - 见函数体
    """关闭装配层持有的资源；重复关闭不重复处理。

    会话内部资源（锁与连接）由会话流程按收尾次序自行释放；装配
    层持有报告失败日志副本的文件通道。
    """
    if deps.closed:
        return
    deps.closed = True
    if deps.failure_channel is not None:
        deps.failure_channel.close()
