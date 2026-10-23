"""按命令装配运行资源与有序关闭。

不建立全局数据库、驱动或配置单例：每次调用创建本次命令所需的
协作者，流程只依赖端口。run 会话默认装配生产报告流程与生成子
进程监督方，会话结束后监督方先收场；接纳/会话句柄由会话流程自
身释放，装配层最后有序关闭业务日志链与报告失败日志副本通道。
"""

from __future__ import annotations

from camctl.devices.start_entry import StartEntryCoordinator

import os
import stat
from dataclasses import dataclass, field, replace
from uuid import uuid4
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from camctl.capture.handlers import (
        PendingCallResult, PendingFileObservation, PendingCaptureCompletion, PendingResultCheckClose,
    )

from camctl.acceptance.input import InputDiagnostic, ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.clocks import SystemClock
from camctl.bootstrap.config import ConfigSnapshot
from camctl.capture.recovery import RecoveryBoundary
from camctl.operations.attempts import RetryWaitGate
from camctl.persistence.runtime import (
    DbConfig, DbOpenMode, DirectoryBindingError, OwnedConnection,
    RuntimeLibraryError, StateDatabaseError, configured_directory_binding,
    open_existing, verify_directory_binding,
)
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.repositories.session import SessionRepository
from camctl.reporting.messages import ResultFailureMessage
from camctl.session.clock import ClockCheckInput, register_clock_guard
from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths
from camctl.session.outcome import SessionOutcome
from camctl.session.service import (
    SessionContext, SessionPaths, _trigger_failure_log, run_session,
)
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
    #: stderr 诊断曾发生写入错误后停止再次尝试。
    stderr_diagnostic_failed: bool = False
    #: 本命令内共享文件日志 I/O 或协调失败后不再使用该路径。
    log_path_unsafe: bool = False
    #: 业务日志链运行时；由 build_runtime 创建并在会话结束时有序关闭。
    log_runtime: Any = None
    #: CLI 拥有的单向通知写端；装配只借用，CLI 在全部退出路径关闭。
    host_notifications: Any = None
    motor_permits: dict = field(default_factory=dict)
    start_entries: Any = field(default_factory=StartEntryCoordinator)
    closed: bool = field(default=False)
    #: 装配前提未成立时保留机器结果，不启动报告协作者。
    startup_error: SessionOutcome | None = None
    #: 状态库路径预检查得到的存在事实；None 表示无法可靠确认。
    state_db_present: bool | None = None
    #: 状态库目标已确认是普通文件；其他文件类型不能进入坏库收场。
    state_db_regular: bool | None = None
    #: 来自本次调用方的旧本地执行收场前提，不由进程重启推导。
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED
    #: 会话锁内首次可靠打开后固定，排除本会话后来建立的意图。
    recovery_max_event_id: int | None = None
    #: 正常运行共用的文件实际拥有者及固定维护范围；submit 不装配。
    work_files: Any = None
    #: 独立推进的拍摄及取回流程；实际任务和连接在会话收场前交付。
    background_flows: tuple[Any, ...] = ()
    #: 原 await 拥有者的实际结果；普通、残留与受限工厂共用同一集合。
    capture_call_results: dict[tuple[int, int], PendingCallResult] = field(default_factory=dict)
    #: 拍摄完整终态及附属读取收尾申请，三种工厂共用。
    capture_completions: dict[int, PendingCaptureCompletion] = field(default_factory=dict)
    #: 有限核实耗尽的原完整决定，独立于设备返回和录像终态申请。
    capture_result_closes: dict[int, PendingResultCheckClose] = field(default_factory=dict)
    #: 文件发现及其派生事实具有独立生命周期，三种工厂共用。
    capture_file_observations: dict[tuple[int, str], PendingFileObservation] = field(default_factory=dict)
    #: 基准准备及原页保存由本会话持有，普通、残留与受限工厂共用。
    capture_baselines: dict = field(default_factory=dict)
    #: 原结果页保存申请由普通、残留和受限工厂共享。
    capture_result_scans: dict = field(default_factory=dict)
    capture_segment_writes: dict = field(default_factory=dict)
    #: 已确认录像的原单调锚点及停止目标；仅在本次会话内有效。
    capture_recording_anchors: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: 已保存等待的原返回锚点；由流程行与剩余预算判定适用性。
    capture_retry_gate: RetryWaitGate = field(default_factory=RetryWaitGate)
    #: 本会话已经取得的媒体原申请；保存核实不依赖当前驱动能力。
    capture_media_results: dict = field(default_factory=dict)
    #: 四个 READ 集合保留本会话原实际结束、完整保存申请和续传身份。
    capture_read_results: dict = field(default_factory=dict)
    #: 本进程会话的固定身份；应急补记的责任键组成部分。
    session_key: str = field(default_factory=lambda: uuid4().hex)
    capture_read_business: dict = field(default_factory=dict)
    capture_read_ends: dict = field(default_factory=dict)
    capture_continuing_reads: dict = field(default_factory=dict)
    cleanup_intents: dict = field(default_factory=dict)
    cleanup_deletes: dict = field(default_factory=dict)
    cleanup_queries: dict = field(default_factory=dict)
    cleanup_companions: dict = field(default_factory=dict)
    cleanup_saves: dict = field(default_factory=dict)
    cleanup_consumed_queries: dict = field(default_factory=dict)
    cleanup_active_calls: set = field(default_factory=set)


def _resume_read_requests(deps: RuntimeDeps, owned: OwnedConnection) -> None:
    """候选读取前保存原 READ、有限耗尽及拍摄申请，不取得设备资格。"""
    from camctl.capture.media_flow import resume_prepared_internal_reads

    resume_prepared_internal_reads(owned,
        pending_read_results=deps.capture_read_results,
        pending_read_business=deps.capture_read_business,
        pending_read_ends=deps.capture_read_ends,
        continuing_read_tickets=deps.capture_continuing_reads)
    _resume_capture_requests(deps, owned)


def _resume_capture_requests(deps: RuntimeDeps, owned: OwnedConnection) -> None:
    """核实原拍摄终态及附属收尾，取消入口不取得新的 READ 业务资格。"""
    from camctl.capture.handlers import (
        resume_canceled_recording_results, resume_capture_completions, resume_result_check_closes,
    )
    from types import SimpleNamespace
    from camctl.capture.baseline import PreparationPhase, resume_baseline_save, resume_baseline_settlement
    from camctl.capture.result_scans import resume_result_page_saves
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories.capture import CaptureRepository

    runtime = SimpleNamespace(owned=owned, capture=CaptureRepository(), pending_baselines=deps.capture_baselines,
                              wall_us=lambda: SystemClock().utc_micros())
    for action_id in tuple(deps.capture_baselines):
        result = resume_baseline_save(action_id, runtime=runtime)
        if result is not None and result.phase is PreparationPhase.PENDING:
            raise ConsistencyError(f"原基准保存仍未可靠完成: {result.database_error}")

    resume_result_page_saves(owned, pending_scans=deps.capture_result_scans, repository=runtime.capture)
    from camctl.capture.segment_completion import resume_segment_writes

    resume_segment_writes(owned, deps.capture_segment_writes)

    resume_result_check_closes(owned,
        pending_result_closes=deps.capture_result_closes,
        retry_gate=deps.capture_retry_gate)
    resume_capture_completions(owned,
        pending_capture_completions=deps.capture_completions,
        retry_gate=deps.capture_retry_gate)
    resume_canceled_recording_results(owned,
        pending_capture_completions=deps.capture_completions,
        retry_gate=deps.capture_retry_gate)
    for action_id in tuple(deps.capture_baselines):
        result = resume_baseline_settlement(action_id, runtime=runtime)
        if result is not None and result.phase is PreparationPhase.PENDING:
            raise ConsistencyError(f"原准备收场仍未可靠保存: {result.database_error}")


def _resume_normal_read_requests(deps: RuntimeDeps, owned: OwnedConnection) -> None:
    """普通与残留入口先核原申请，再处理实际完成且必要摘要绑定失效的读取。"""
    from camctl.capture.media_flow import resume_binding_internal_read_ends
    from camctl.devices.bindings import check_binding

    _resume_read_requests(deps, owned)
    resume_binding_internal_read_ends(owned,
        pending_read_results=deps.capture_read_results,
        pending_read_business=deps.capture_read_business,
        pending_read_ends=deps.capture_read_ends,
        continuing_read_tickets=deps.capture_continuing_reads,
        binding_check=lambda saved: check_binding(saved, deps.config),
        occurred_at=SystemClock().utc_micros)


def _default_catalog(config: ConfigSnapshot):
    """从 describe 使用的同一部署定义构建受理目录。"""
    from camctl.devices.catalog import build_catalog, default_driver_definitions
    from camctl.devices.drivers.builtin import register_builtin_drivers

    register_builtin_drivers(config)
    return build_catalog(config, default_driver_definitions())


def build_runtime(
    command_mode: CommandMode,
    config: ConfigSnapshot,
    *,
    catalog: Any = None,
    notifier: Any = None,
    host_notifications: Any = None,
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED,
) -> RuntimeDeps:
    """按命令模式创建运行协作者；不隐式创建状态库。"""
    from camctl.persistence.repositories.capture import register_capture_guards
    from camctl.persistence.repositories.cancellation import (
        register_cancellation_guards,
    )
    from camctl.persistence.repositories.operations import register_operation_guards
    from camctl.persistence.repositories.outputs import register_outputs_guards
    from camctl.persistence.repositories.scheduling import register_window_guard
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
    register_window_guard()
    from camctl.persistence.repositories.motor import register_motor_guards
    register_motor_guards()
    startup_error = None
    try:
        # 装配边界独立校验目录绑定：配置可能不经 ConfigAdapter 进入
        # （如测试替身或新入口）。校验不改值，规范文本只在库内保存
        # 与比对边界生成。
        for name in ("staging", "ready", "processing"):
            configured_directory_binding(getattr(config.paths, name), name)
    except DirectoryBindingError as error:
        startup_error = SessionOutcome(succeeded=False, reason="configuration_error",
                                       details={"error": str(error)})
    state_db = Path(config.paths.state_db).expanduser().resolve()
    state_db_present: bool | None = None
    state_db_regular: bool | None = None
    if startup_error is None:
        try:
            state_db_info = state_db.stat()
        except FileNotFoundError as error:
            state_db_present = False
            state_db_regular = False
            startup_error = SessionOutcome(
                succeeded=False, reason="state_db_error",
                details={"error": f"状态库不存在: {state_db}: {error}"},
            )
        except OSError as error:
            # 不能把 stat 权限错误推断成缺失；没有可靠存在事实时，
            # run 也不能取得会话锁或扫描日志目录。
            state_db_present = None
            state_db_regular = None
            startup_error = SessionOutcome(
                succeeded=False, reason="state_db_error",
                details={"error": f"状态库存在性无法确认: {state_db}: {error}"},
            )
        else:
            state_db_present = True
            state_db_regular = stat.S_ISREG(state_db_info.st_mode)
            if not state_db_regular:
                startup_error = SessionOutcome(
                    succeeded=False, reason="state_db_error",
                    details={"error": f"状态库目标不是普通文件: {state_db}"},
                )
            else:
                try:
                    owned = open_existing(
                        state_db, DbOpenMode.EXISTING_RO,
                        DbConfig(busy_timeout_ms=config.database.busy_timeout_ms),
                    )
                    try:
                        verify_directory_binding(
                            owned.metadata, config.paths.staging,
                            config.paths.ready, config.paths.processing,
                        )
                    finally:
                        owned.connection.close()
                except (DirectoryBindingError, RuntimeLibraryError) as error:
                    startup_error = SessionOutcome(
                        succeeded=False, reason="configuration_error",
                        details={"error": str(error)},
                    )
                except (StateDatabaseError, OSError) as error:
                    # stat 已确认是普通文件；后续损坏、权限或打开错误
                    # 不能抹去这个事实。run 会在同一会话锁内最小收场。
                    startup_error = SessionOutcome(
                        succeeded=False, reason="state_db_error",
                        details={"error": str(error)},
                    )
    session_lock, admission_lock = lock_file_paths(state_db)
    deps = RuntimeDeps(
        mode=command_mode,
        config=config,
        state_db=state_db,
        session_lock=session_lock,
        admission_lock=admission_lock,
        catalog=catalog if catalog is not None else _default_catalog(config),
        notifier=notifier,
        host_notifications=host_notifications,
        startup_error=startup_error,
        state_db_present=state_db_present,
        state_db_regular=state_db_regular,
        recovery_boundary=recovery_boundary,
    )
    if startup_error is None or startup_error.reason == "state_db_error":
        deps.log_runtime = _log_wiring(
            config,
            on_file_failure=lambda: _mark_log_path_unsafe(deps),
            on_stderr_diagnostic=lambda message: _write_stderr_diagnostic(
                deps, message),
        )
    return deps


def _log_wiring(
    config: ConfigSnapshot,
    *,
    on_file_failure: Callable[[], None] | None = None,
    on_stderr_diagnostic: Callable[[str], None] | None = None,
):
    """业务日志链装配：文件通道、接纳通道与关闭运行时。

    追加失败经 ChannelWriteError 交给接纳通道按通道故障禁用；轮换
    失败但仍追加成功时通道继续。监听线程在此启动，收场由
    execute_command 的 close_logging 完成。
    """
    from camctl.logging_runtime.clh_adapter import (
        ChannelWriteError, FileChannel, WriteFailureStage,
    )
    from camctl.logging_runtime.lifecycle import LogRuntime
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogChannel

    log_config = config.log
    # 配置层以大写名称校验级别；日志运行时枚举以小写值为键。
    level = LogLevel(log_config.level.lower())
    file_channel = FileChannel(
        Path(config.paths.log_file).expanduser(),
        max_bytes=log_config.max_size_bytes,
        file_count=log_config.file_count,
    )
    rotation_warning_reported = False

    def writer(record):
        nonlocal rotation_warning_reported
        result = file_channel.write_record(record)
        failure_stage = result.failure_stage
        details = tuple(
            f"{label}={detail}" for label, detail in (
                ("append_error", result.append_error),
                ("coordination_error", result.coordination_error),
                ("close_error", result.close_error),
                ("rotation_error", result.rotation_error),
            ) if detail is not None
        )
        if failure_stage is WriteFailureStage.FORMAT:
            report_writer_failure(
                f"普通日志写入诊断：stage=format; appended=false; "
                + "; ".join(details))
            return False
        if result.disables_file_logging:
            stage = failure_stage.value if failure_stage is not None else "unknown"
            report_writer_failure(
                f"普通日志写入诊断：stage={stage}; "
                f"appended={str(result.appended).lower()}; "
                + "; ".join(details))
            if on_file_failure is None:
                raise ChannelWriteError(
                    result.append_error or result.coordination_error
                    or "共享文件日志通道失效")
            on_file_failure()
            return result.appended
        if result.rotation_error is not None:
            if not rotation_warning_reported:
                rotation_warning_reported = True
                report_writer_failure(
                    "普通日志写入诊断：stage=rotation; "
                    f"appended={str(result.appended).lower()}; "
                    f"rotation_error={result.rotation_error}")
        return result.appended

    stderr_failed = False

    def report_writer_failure(message: str) -> None:
        nonlocal stderr_failed
        if on_stderr_diagnostic is not None:
            on_stderr_diagnostic(message)
            return
        if stderr_failed:
            return
        import sys

        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except Exception:
            stderr_failed = True

    channel = LogChannel(
        capacity=log_config.queue_capacity,
        low_watermark=log_config.queue_low_watermark,
        high_watermark=log_config.queue_high_watermark,
        level=level,
        sample_probability=float(log_config.info_sample_probability),
        writer=writer,
    )
    channel.start()
    return LogRuntime(
        channel=channel,
        level=level,
        file_closer=file_channel.close,
        identity=f"pid={os.getpid()}",
    )


async def _emit_session_error(deps: RuntimeDeps, message: str) -> None:
    """会话失败事实经异步入口记 ERROR；日志失败不改变业务结果。"""
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogRecord

    if deps.log_runtime is None:
        return
    if deps.log_path_unsafe:
        _write_stderr_diagnostic(deps, message)
        return
    await deps.log_runtime.channel.alog(
        LogRecord(level=LogLevel.ERROR, message=message)
    )


async def _emit_log_diagnostic(
    deps: RuntimeDeps, message: str, *, stderr_only: bool = False,
) -> None:
    """等待普通日志实际写入；不可用时写一次 stderr，不触发副本。"""
    if (not stderr_only and not deps.log_path_unsafe
            and deps.log_runtime is not None):
        from camctl.logging_runtime.models import LogLevel
        from camctl.logging_runtime.service import LogRecord

        try:
            result = await deps.log_runtime.channel.deliver_important(
                LogRecord(level=LogLevel.ERROR, message=message))
            if result.delivered is True:
                return
        except Exception:
            # deliver_important 等待监听线程凭据，异常代表文件通道不
            # 可用；不能仅凭 alog 入队结果宣称诊断已写出。
            pass
    _write_stderr_diagnostic(deps, message)


def _mark_log_path_unsafe(deps: RuntimeDeps) -> None:
    """禁用本命令的共享文件日志，后续会话诊断改走 stderr。"""
    deps.log_path_unsafe = True
    runtime = deps.log_runtime
    if runtime is None:
        return
    from camctl.logging_runtime.service import ChannelError

    runtime.path_unsafe = True
    runtime.channel.disable_channel(
        ChannelError("共享文件日志 I/O 或协调失败"))


def _write_stderr_diagnostic(deps: RuntimeDeps, message: str) -> None:
    """stderr 失败后停止重复尝试，且不写入 stdout。"""
    if deps.stderr_diagnostic_failed:
        return
    import sys

    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:
        deps.stderr_diagnostic_failed = True


def _attach_runtime_close_note(
    target: BaseException,
    label: str,
    error: BaseException,
) -> None:
    if target is error:
        return
    notes = tuple(getattr(error, "__notes__", ()))
    existing = set(tuple(getattr(target, "__notes__", ())))
    messages = [f"{label}: {type(error).__name__}: {error}"]
    messages.extend(f"{label}附注: {note}" for note in notes)
    for message in messages:
        if message not in existing:
            target.add_note(message)
            existing.add(message)


async def _close_log_runtime(
    deps: RuntimeDeps,
    *,
    primary_error: BaseException | None = None,
) -> None:
    from camctl.logging_runtime.lifecycle import close_logging

    try:
        result = await close_logging(deps.log_runtime)
    except BaseException as error:
        notes = "; ".join(tuple(getattr(error, "__notes__", ())))
        diagnostic = (
            "共享日志关闭诊断：stage=log_close; outcome=unknown; "
            f"error={type(error).__name__}: {error}"
        )
        if notes:
            diagnostic += f"; notes={notes}"
        try:
            _write_stderr_diagnostic(deps, diagnostic)
        except BaseException as diagnostic_error:
            if (isinstance(error, Exception)
                    and not isinstance(diagnostic_error, Exception)):
                _attach_runtime_close_note(
                    diagnostic_error, "此前日志关闭错误", error)
                raise
            _attach_runtime_close_note(
                error, "日志关闭诊断写入错误", diagnostic_error)
        raise
    if result.close_error is not None:
        diagnostic = (
            "共享日志处理器关闭诊断：stage=log_handler_close; "
            f"outcome=unknown; error={result.close_error}"
        )
        _write_stderr_diagnostic(deps, diagnostic)
        if primary_error is not None:
            note = f"后续日志关闭错误: {result.close_error}"
            if note not in tuple(getattr(primary_error, "__notes__", ())):
                primary_error.add_note(note)


async def _on_report_recovered(
        deps: RuntimeDeps, failure_log: Any,
        logging_owner: Any = None) -> None:
    """结束副本轮次，并诊断未完成的正式标记删除。"""
    from camctl.logging_runtime.copies import (
        DeleteOutcome, _exception_description,
    )

    async def diagnose(result, failure: BaseException | None) -> None:
        if failure is not None:
            await _emit_log_diagnostic(
                deps,
                "报告日志标记恢复删除诊断：stage=marker_delete; "
                f"outcome=unknown; error={_exception_description(failure)}",
                stderr_only=deps.log_path_unsafe,
            )
            return
        if (result.outcome in (DeleteOutcome.DELETED, DeleteOutcome.NOT_PRESENT)
                and result.error is None):
            return
        await _emit_log_diagnostic(
            deps,
            "报告日志标记恢复删除诊断："
            f"stage=marker_delete; outcome={result.outcome.value}; "
            f"error={result.error or 'none'}",
            stderr_only=deps.log_path_unsafe,
        )

    async def operation() -> None:
        try:
            if logging_owner is None:
                result = await failure_log.on_report_recovered()
                await diagnose(result, None)
            else:
                await failure_log.on_report_recovered(diagnose=diagnose)
        except Exception as error:
            if logging_owner is None:
                await diagnose(None, error)

    if logging_owner is None:
        await operation()
    else:
        await logging_owner.run_service_operation("report_recovered", operation)


def _open_connection(state_db: Path) -> OwnedConnection:
    return open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())


def _open_runtime_connection(deps: RuntimeDeps) -> OwnedConnection:
    owned = _open_connection(deps.state_db)
    try:
        verify_directory_binding(owned.metadata, deps.config.paths.staging,
                                 deps.config.paths.ready, deps.config.paths.processing)
    except BaseException:
        owned.connection.close()
        raise
    return owned


def _initialize_recovery(deps: RuntimeDeps, owned: OwnedConnection) -> None:
    """在会话锁内固定旧历史边界，业务事务不得提前发生。"""
    from contextlib import closing
    from camctl.devices.drivers.adb_cameras.registration import begin_builtin_camera_session

    begin_builtin_camera_session()
    if owned.metadata.format_version >= 3:
        from camctl.devices.drivers.runtime import current_registry
        from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
        with closing(owned.connection.execute("SELECT DISTINCT a.device_id,a.driver_id FROM software_recoveries r"
                " JOIN device_activities d ON d.id=r.activity_id JOIN actions a ON a.id=r.action_id"
                " WHERE d.occupancy_state=1")) as recoveries:
            for device_id, driver_id in recoveries:
                entry = current_registry().entry(driver_id)
                if entry is not None and isinstance(entry.driver, AdbCameraDriver):
                    entry.driver.hold_recovery(device_id)

    with closing(owned.connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM history_events")) as cursor:
        deps.recovery_max_event_id = int(cursor.fetchone()[0])
    if deps.work_files is not None:
        deps.work_files.initialize(owned)


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
    from camctl.logging_runtime.clh_adapter import ChannelWriteError, FileChannel
    from camctl.logging_runtime.copies import (
        CopyRequest, FailureLogService, MarkerStore,
    )
    from camctl.logging_runtime.models import LogLevel
    from camctl.logging_runtime.service import LogRecord

    staging = Path(deps.config.paths.staging).expanduser()
    logs_dir = staging / "logs"
    channel_box: dict = {}
    marker = MarkerStore(logs_dir)

    def copy_request_factory(error: Exception) -> CopyRequest:
        if deps.log_path_unsafe:
            raise ChannelWriteError("本命令已停用共享文件日志")
        channel = channel_box.get("channel")
        if channel is None:
            channel = FileChannel(
                Path(deps.config.paths.log_file).expanduser(),
                max_bytes=deps.config.log.max_size_bytes,
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
            marker=marker,
        )

    def on_write_result(result) -> None:
        if result.disables_file_logging:
            _mark_log_path_unsafe(deps)

    return FailureLogService(
        marker, on_write_result=on_write_result), copy_request_factory


def _report_assembly(
        deps: RuntimeDeps, failure_log: Any,
        logging_owner: Any = None) -> tuple[dict[str, Any], Any]:
    """run 会话的生产流程与生成子进程监督方。

    注册报告事务守卫后组装维护流程：到期的同步动作、报告冻结、
    子进程生成与发布编排都由流程按轮推进；监督方惰性启动，由
    execute_command 在会话结束后收场。拍摄推进经进程驱动登记解析
    设备端口（未登记驱动的设备本轮不推进）；取消动作按排期或立即
    执行。受限会话的取消流程、一次报告机会与保守收场由
    execute_command 在此基础上另行装配。failure_log 的恢复入口接
    入报告流程：报告本地处理可靠恢复时结束日志副本故障轮。
    """
    from camctl.bootstrap.capture_assembly import (
        execution_wait_config,
        session_capture_assembly,
    )
    from camctl.bootstrap.cleanup_assembly import (
        cleanup_flow, session_cleanup_assembly,
    )
    from camctl.bootstrap.flows import (
        cancel_flow, capture_flow, report_flow, residual_flow,
    )
    from camctl.capture.handlers import resume_file_observations
    from camctl.bootstrap.obtain_assembly import (
        obtain_flow, session_obtain_assembly,
    )
    from camctl.bootstrap.recovery_logging import recovery_diagnostics_logger
    from camctl.devices.drivers.runtime import current_registry
    from camctl.reporting.maintenance import MaintenanceLimits
    from camctl.reporting.supervisor import WorkerSupervisor
    from camctl.reporting.worker import report_lock_path

    supervisor = WorkerSupervisor(
        limits=MaintenanceLimits.defaults(),
        lock_path=str(report_lock_path(deps.state_db)),
    )
    staging = Path(deps.config.paths.staging)
    ready = Path(deps.config.paths.ready)
    processing = Path(deps.config.paths.processing)
    drivers = current_registry()
    from camctl.bootstrap.work_file_assembly import WorkFileRuntime
    from camctl.bootstrap.download_cleanup import ReadDownloadCleanup
    from camctl.outputs.work_files import WorkFileLimits
    deps.work_files = WorkFileRuntime(
        staging=staging, pending_media_results=deps.capture_media_results,
        download_cleanup=ReadDownloadCleanup(staging,
            limit=deps.config.cleanup.work_file_limit_per_run, boundary=deps.recovery_boundary),
        limits=WorkFileLimits(
            batch_size=deps.config.cleanup.work_file_batch_size,
            limit_per_run=deps.config.cleanup.work_file_limit_per_run))
    resume_files = partial(resume_file_observations,
        pending_file_observations=deps.capture_file_observations,
        pending_call_results=deps.capture_call_results)
    resume_reads = partial(_resume_normal_read_requests, deps)
    recovery_logger = (None if deps.log_runtime is None else
                       recovery_diagnostics_logger(
                           deps.log_runtime.channel,
                           path_unsafe=lambda: deps.log_path_unsafe,
                           fallback=lambda message: _write_stderr_diagnostic(
                               deps, message)))
    from camctl.bootstrap.motor_assembly import motor_flow
    from camctl.motor.notification import NotificationWriter
    writer = deps.host_notifications
    if writer is None:
        writer = NotificationWriter(None)
    flows = {
        "report": report_flow(
            state_db=deps.state_db,
            staging=staging,
            ready=ready,
            processing=processing,
            history=deps.config.history,
            database=deps.config.database,
            supervisor=supervisor,
            on_recovered=(
                _recovery_callback(deps, failure_log, logging_owner)
                if failure_log is not None
                else None
            ),
            on_failure=_trigger_failure_log,
        ),
        # 取消动作按排期或立即执行；墙钟可信由会话进入路径保证。
        "cancel": cancel_flow(
            ready=ready,
            processing=processing,
            start_entries=deps.start_entries,
            motor_permits=deps.motor_permits,
            work_files=deps.work_files,
            resume_media_results=deps.work_files.resume_media_results,
            resume_file_observations=resume_files,
            resume_capture_completions=partial(_resume_capture_requests, deps),
            cleanup_pending=partial(_cleanup_pending_for_action, deps),
        ),
        "motor": motor_flow(writer, deps.motor_permits),
        # 拍摄推进：按设备声明与进程驱动登记组装运行时，等待配置读
        # 首次固定的执行定义。
        "scheduling": capture_flow(
            session_capture_assembly(
                start_entries=deps.start_entries,
                devices=deps.config.devices,
                drivers=drivers,
                staging=staging,
                wait_config=execution_wait_config,
                pending_call_results=deps.capture_call_results,
                pending_capture_completions=deps.capture_completions,
                pending_result_closes=deps.capture_result_closes,
                pending_baselines=deps.capture_baselines,
                pending_result_scans=deps.capture_result_scans,
                pending_segment_writes=deps.capture_segment_writes,
                pending_file_observations=deps.capture_file_observations,
                pending_media_results=deps.capture_media_results,
                pending_read_results=deps.capture_read_results,
                pending_read_business=deps.capture_read_business,
                pending_read_ends=deps.capture_read_ends,
                continuing_read_tickets=deps.capture_continuing_reads,
                recording_anchors=deps.capture_recording_anchors,
                retry_wait_gate=deps.capture_retry_gate,
                recovery_boundary=deps.recovery_boundary,
                recovery_max_event_id=lambda: deps.recovery_max_event_id,
                on_recovery_diagnostic=recovery_logger,
                file_executor=deps.work_files.executor,
                segment_size=deps.config.copy.segment_size_bytes,
                read_discovery_batch_size=deps.config.session.read_discovery_batch_size,
            ),
            read_discovery_batch_size=deps.config.session.read_discovery_batch_size,
            resume_media_results=deps.work_files.resume_media_results,
            resume_file_observations=resume_files,
            resume_read_results=resume_reads,
        ),
        # 残留收场推进：触发动作终态后接管已建立的收场流程，使用剩
        # 余次数完成停止并收场其查询责任。
        "residual": residual_flow(
            session_capture_assembly(
                start_entries=deps.start_entries,
                devices=deps.config.devices,
                drivers=drivers,
                staging=staging,
                wait_config=execution_wait_config,
                pending_call_results=deps.capture_call_results,
                pending_capture_completions=deps.capture_completions,
                pending_result_closes=deps.capture_result_closes,
                pending_baselines=deps.capture_baselines,
                pending_result_scans=deps.capture_result_scans,
                pending_segment_writes=deps.capture_segment_writes,
                pending_file_observations=deps.capture_file_observations,
                pending_media_results=deps.capture_media_results,
                pending_read_results=deps.capture_read_results,
                pending_read_business=deps.capture_read_business,
                pending_read_ends=deps.capture_read_ends,
                continuing_read_tickets=deps.capture_continuing_reads,
                recording_anchors=deps.capture_recording_anchors,
                retry_wait_gate=deps.capture_retry_gate,
                recovery_boundary=deps.recovery_boundary,
                recovery_max_event_id=lambda: deps.recovery_max_event_id,
                on_recovery_diagnostic=recovery_logger,
                file_executor=deps.work_files.executor,
                segment_size=deps.config.copy.segment_size_bytes,
                read_discovery_batch_size=deps.config.session.read_discovery_batch_size,
            ),
            read_discovery_batch_size=deps.config.session.read_discovery_batch_size,
            resume_media_results=deps.work_files.resume_media_results,
            resume_file_observations=resume_files,
            resume_read_results=resume_reads,
            owns_device=lambda device: capture_owner.owns_device(device),
        ),
        # 取回推进：与拍摄共用统一设备工作计划，读取在拍摄空闲轮次
        # 推进；拷贝段大小取自 copy 配置。
        "obtain": obtain_flow(
            session_obtain_assembly(
                devices=deps.config.devices,
                drivers=drivers,
                max_query_attempts=deps.config.cleanup.max_query_attempts,
                staging=staging,
                ready=ready,
                processing=processing,
                segment_size=deps.config.copy.segment_size_bytes,
                read_discovery_batch_size=deps.config.session.read_discovery_batch_size,
                recovery_boundary=deps.recovery_boundary,
                recovery_max_event_id=lambda: deps.recovery_max_event_id,
                on_recovery_diagnostic=recovery_logger,
                file_executor=deps.work_files.executor,
            )
        ),
        # 清理推进：删除与查询端口按设备声明解析，尝试上限取自
        # cleanup 配置；主机派生成品经 staging 工作根本地删除。
        "cleanup": cleanup_flow(
            session_cleanup_assembly(
                devices=deps.config.devices,
                drivers=drivers,
                max_delete_attempts=deps.config.cleanup.max_delete_attempts,
                max_query_attempts=deps.config.cleanup.max_query_attempts,
                staging=staging,
                file_executor=deps.work_files.executor,
                pending_intents=deps.cleanup_intents,
                pending_deletes=deps.cleanup_deletes,
                pending_queries=deps.cleanup_queries,
                pending_companions=deps.cleanup_companions,
                pending_saves=deps.cleanup_saves,
                consumed_queries=deps.cleanup_consumed_queries,
                active_calls=deps.cleanup_active_calls,
                recovery_boundary=deps.recovery_boundary,
                recovery_max_event_id=lambda: deps.recovery_max_event_id,
                on_recovery_diagnostic=recovery_logger,
                discovery_batch_size=deps.config.session.read_discovery_batch_size,
            ),
            state_db_errors=True,
        ),
        "work_files": deps.work_files.flow,
    }
    from camctl.bootstrap.background_flow import BackgroundFlow
    capture_owner = flows["scheduling"]
    flows["obtain"] = BackgroundFlow(flows["obtain"])
    deps.background_flows = (capture_owner, flows["obtain"])
    return flows, supervisor


async def execute_command(
    deps: RuntimeDeps,
    source: ParsedInput | InputDiagnostic | None,
    *,
    flows: Mapping[str, Any] | None = None,
    restricted_flows: Mapping[str, Any] | None = None,
    wake: Any = None,
    poll_interval_s: float | None = None,
) -> SessionOutcome:
    """执行一次 run/submit 会话；调用方负责运行事件循环。

    flows、restricted_flows、wake 与 poll_interval_s 是业务流程装配
    的注入点：显式注入的流程映射整体替换生产装配；run 会话未注入
    时使用生产流程（报告生成子进程、取消与拍摄推进，受限会话另有
    取消、一次报告与保守收场），submit 会话不驱动业务流程。进程内
    唤醒与轮询上限随流程一起接入。
    """
    from camctl.bootstrap.application import query_work_facts

    minimal_bad_database_run = (
        deps.mode is CommandMode.RUN
        and deps.startup_error is not None
        and deps.startup_error.reason == "state_db_error"
        and deps.state_db_present is True
        and deps.state_db_regular is True
    )
    if deps.startup_error is not None and not minimal_bad_database_run:
        if deps.log_runtime is not None:
            try:
                await _emit_session_error(deps, f"{deps.mode.value} 会话失败: {deps.startup_error.reason}")
            finally:
                await _close_log_runtime(deps)
        return deps.startup_error

    failure_log, copy_request_factory = _failure_log_wiring(deps)
    logging_owner = None
    if deps.mode is CommandMode.RUN:
        from camctl.logging_runtime.ownership import LoggingOwner
        from camctl.session.supervision import Supervisor

        logging_owner = LoggingOwner(
            logs_dir=failure_log.logs_dir,
            ownership=failure_log.ownership,
            supervisor=Supervisor(),
            owner_settled=(
                deps.recovery_boundary is RecoveryBoundary.HOST_LOCAL_SETTLED),
            ordinary_log_path=Path(deps.config.paths.log_file).expanduser(),
            diagnose=lambda message: _emit_log_diagnostic(
                deps, message, stderr_only=deps.log_path_unsafe),
        )
        failure_log.bind_owner(logging_owner)
    supervisor: Any = None
    restricted_supervisor: Any = None
    overrides: dict[str, Any] = {
        "flows": flows if flows is not None else {},
        "wake": wake,
        "poll_interval_s": (deps.config.session.plan_poll_interval_ms / 1000
                            if poll_interval_s is None else poll_interval_s),
    }
    if deps.mode is CommandMode.RUN and wake is None:
        from camctl.scheduling.notifications import WorkNotifier
        overrides["wake"] = WorkNotifier()
    assemble_production_run = (
        deps.startup_error is None
        and deps.mode is CommandMode.RUN
        and flows is None
    )
    if assemble_production_run:
        flows, supervisor = _report_assembly(
            deps, failure_log, logging_owner)
        # 受限会话装配：墙钟检查失败时消费规定取消流程、一次报告机
        # 会与录像保守收场（不装配媒体链）。
        from camctl.bootstrap.capture_assembly import (
            execution_wait_config,
            session_capture_assembly,
        )
        from camctl.bootstrap.flows import (
            cancel_flow,
            report_flow,
            winddown_flow,
        )
        from camctl.capture.handlers import resume_file_observations
        from camctl.bootstrap.recovery_logging import recovery_diagnostics_logger
        from camctl.devices.drivers.runtime import current_registry
        from camctl.reporting.maintenance import MaintenanceLimits
        from camctl.reporting.supervisor import WorkerSupervisor
        from camctl.reporting.worker import report_lock_path

        restricted_supervisor = WorkerSupervisor(
            limits=MaintenanceLimits.defaults(),
            lock_path=str(report_lock_path(deps.state_db)),
        )
        staging = Path(deps.config.paths.staging)
        ready = Path(deps.config.paths.ready)
        processing = Path(deps.config.paths.processing)
        resume_files = partial(resume_file_observations,
            pending_file_observations=deps.capture_file_observations,
            pending_call_results=deps.capture_call_results)
        resume_reads = partial(_resume_read_requests, deps)
        overrides.update(
            flows=flows,
            restricted_flows={
                "cancel": cancel_flow(
                    start_entries=deps.start_entries,
                    ready=ready,
                    processing=processing,
                    unscheduled_only=True,
                    motor_permits=deps.motor_permits,
                    work_files=deps.work_files,
                    resume_media_results=deps.work_files.resume_media_results,
                    resume_file_observations=resume_files,
                    resume_capture_completions=partial(_resume_capture_requests, deps),
                ),
                # 保守收场：额外等待上限取 clock.recovery_wait_cap_s。
                "winddown": winddown_flow(
                    capture_factory=session_capture_assembly(
                        start_entries=deps.start_entries,
                        devices=deps.config.devices,
                        drivers=current_registry(),
                        staging=staging,
                        wait_config=execution_wait_config,
                        pending_call_results=deps.capture_call_results,
                        pending_capture_completions=deps.capture_completions,
                        pending_result_closes=deps.capture_result_closes,
                        pending_baselines=deps.capture_baselines,
                        pending_result_scans=deps.capture_result_scans,
                        pending_segment_writes=deps.capture_segment_writes,
                        pending_file_observations=deps.capture_file_observations,
                        pending_media_results=deps.capture_media_results,
                        pending_read_results=deps.capture_read_results,
                        pending_read_business=deps.capture_read_business,
                        pending_read_ends=deps.capture_read_ends,
                        continuing_read_tickets=deps.capture_continuing_reads,
                        recording_anchors=deps.capture_recording_anchors,
                        retry_wait_gate=deps.capture_retry_gate,
                        media_enabled=False,
                        file_executor=deps.work_files.executor,
                        recovery_boundary=deps.recovery_boundary,
                        recovery_max_event_id=lambda: deps.recovery_max_event_id,
                        on_recovery_diagnostic=(
                            None
                            if deps.log_runtime is None
                            else recovery_diagnostics_logger(
                                deps.log_runtime.channel,
                                path_unsafe=lambda: deps.log_path_unsafe,
                                fallback=lambda message: _write_stderr_diagnostic(
                                    deps, message
                                ),
                            )
                        ),
                    ),
                    wait_cap_s=deps.config.clock.recovery_wait_cap_s,
                    resume_media_results=deps.work_files.resume_media_results,
                    resume_file_observations=resume_files,
                    resume_read_results=resume_reads,
                    owns_device=flows["scheduling"].owns_device,
                ),
            },
            once_report=report_flow(
                state_db=deps.state_db,
                staging=staging,
                ready=ready,
                processing=processing,
                history=deps.config.history,
                database=deps.config.database,
                supervisor=restricted_supervisor,
                start_actions=False,
                on_recovered=(
                    _recovery_callback(deps, failure_log, logging_owner)
                    if failure_log is not None
                    else None
                ),
                on_failure=_trigger_failure_log,
            ),
        )
    elif restricted_flows is not None:
        overrides["restricted_flows"] = restricted_flows
    if assemble_production_run:
        # 生产 run 装配：非时钟致命错误收场时对会话内录像执行有
        # 限应急停止并补记；submit 不执行该流程。
        overrides["emergency_settlement"] = _emergency_settlement_of(deps)
    context = SessionContext(
        mode=deps.mode,
        catalog=deps.catalog,
        clock=SystemClock(),
        open_connection=lambda: _open_runtime_connection(deps),
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
        on_log_diagnostic=lambda message: _emit_log_diagnostic(deps, message),
        on_stderr_diagnostic=lambda message: _emit_log_diagnostic(
            deps, message, stderr_only=True),
        on_log_path_unsafe=lambda: _mark_log_path_unsafe(deps),
        on_session_open=lambda owned: _initialize_recovery(deps, owned),
        on_session_locked=(None if logging_owner is None else
                           logging_owner.start_session),
        logging_owner=logging_owner,
        local_work=_local_work(deps, overrides["flows"], logging_owner),
        wakeup_margin_s=deps.config.session.wakeup_margin_ms / 1000,
        startup_outcome=(deps.startup_error
                         if minimal_bad_database_run else None),
        **overrides,
    )
    outcome: SessionOutcome | None = None
    primary_error: BaseException | None = None
    try:
        outcome = await run_session(context, source)
    except BaseException as error:
        primary_error = error
        if isinstance(error, Exception):
            await _emit_session_error(deps, f"{type(error).__name__}: {error}")
        raise
    finally:
        selected_control = (
            primary_error
            if primary_error is not None and not isinstance(primary_error, Exception)
            else None
        )
        cleanup_errors: list[tuple[str, BaseException]] = []
        seen_cleanup_errors: set[int] = set()
        noted_cleanup_errors: set[int] = (
            set() if selected_control is None else {id(selected_control)}
        )

        def attach_cleanup_error(
                target: BaseException, label: str, error: BaseException) -> None:
            if target is error or id(error) in noted_cleanup_errors:
                return
            _attach_runtime_close_note(target, label, error)
            noted_cleanup_errors.add(id(error))

        def remember_cleanup_error(
                label: str, error: BaseException) -> None:
            """保存首个收场 control，并在全部资源结束后选择传播对象。"""
            nonlocal selected_control
            if id(error) not in seen_cleanup_errors:
                seen_cleanup_errors.add(id(error))
                cleanup_errors.append((label, error))
            if isinstance(error, Exception):
                if selected_control is not None:
                    attach_cleanup_error(selected_control, label, error)
                return
            if selected_control is None:
                selected_control = error
                noted_cleanup_errors.add(id(error))
                if primary_error is not None and primary_error is not error:
                    attach_cleanup_error(
                        selected_control, "此前会话异常", primary_error,
                    )
                if outcome is not None and not outcome.succeeded:
                    note = (
                        "此前会话结果："
                        f"reason={outcome.reason}; details={outcome.details!r}"
                    )
                    if note not in tuple(getattr(selected_control, "__notes__", ())):
                        selected_control.add_note(note)
                for earlier_label, earlier_error in cleanup_errors:
                    if earlier_error is not error:
                        attach_cleanup_error(
                            selected_control, earlier_label, earlier_error,
                        )
            else:
                attach_cleanup_error(selected_control, label, error)

        # 会话失败结论先于监督方收场与日志关闭记录。
        if outcome is not None and not outcome.succeeded:
            await _emit_session_error(
                deps, f"{deps.mode.value} 会话失败: {outcome.reason}"
            )
        async def stop_worker(worker):
            nonlocal outcome
            if worker is None:
                return
            stopped = await worker.stop()
            if stopped.state_failure is not None:
                if outcome is not None:
                    outcome = _shutdown_state_outcome(outcome, stopped.state_failure)
                await _emit_session_error(deps, stopped.state_failure.error_message)
            elif stopped.configuration_failure is not None:
                if outcome is not None:
                    outcome = _shutdown_configuration_outcome(outcome, stopped.configuration_failure)
                await _emit_session_error(deps, stopped.configuration_failure.error_message)

        for worker in (supervisor, restricted_supervisor):
            try:
                await stop_worker(worker)
            except BaseException as error:
                remember_cleanup_error("报告监督收场异常", error)
        if deps.log_runtime is not None:
            try:
                await _close_log_runtime(
                    deps,
                    primary_error=selected_control,
                )
            except BaseException as error:
                remember_cleanup_error("共享日志收场异常", error)
        if selected_control is not None and selected_control is not primary_error:
            raise selected_control.with_traceback(
                selected_control.__traceback__)
        if selected_control is None and cleanup_errors:
            selected_error = primary_error or cleanup_errors[0][1]
            for label, error in cleanup_errors:
                attach_cleanup_error(selected_error, label, error)
            if selected_error is not primary_error:
                raise selected_error.with_traceback(selected_error.__traceback__)
    return outcome


def _recovery_callback(
        deps: RuntimeDeps, failure_log: Any, logging_owner: Any = None):
    async def on_recovered() -> None:
        await _on_report_recovered(deps, failure_log, logging_owner)

    return on_recovered


def _cleanup_pending_for_action(deps, connection, action_id):
    """取消等待同一会话的原保存责任，不能仅按成员终态完成。"""
    item_ids = set(deps.cleanup_intents) | set(deps.cleanup_deletes) | set(deps.cleanup_companions)
    item_ids.update(value.item_id for value in deps.cleanup_queries.values())
    for value in deps.cleanup_saves.values():
        if getattr(value.request, "action_id", None) == action_id:
            return True
        if hasattr(value.request, "item_id"):
            item_ids.add(value.request.item_id)
    return any(connection.execute("SELECT 1 FROM cleanup_items WHERE id=? AND action_id=?",
        (item_id, action_id)).fetchone() is not None for item_id in item_ids)


def _local_work(deps, flows, logging_owner=None):
    from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork
    from camctl.bootstrap.flows import CaptureFlow
    from camctl.bootstrap.cleanup_assembly import CleanupFlow
    owners = list(deps.background_flows)
    owners.extend(flow for flow in flows.values() if isinstance(flow, (BackgroundFlow, CaptureFlow, CleanupFlow)))
    if deps.work_files is not None:
        owners.append(deps.work_files)
    if logging_owner is not None:
        owners.append(logging_owner)
    owners = tuple({id(owner): owner for owner in owners}.values())
    return CombinedLocalWork(owners) if owners else None


def _emergency_settlement_of(deps: RuntimeDeps):
    """构建生产应急收场：锚点、停止配置与驱动登记来自本会话。"""
    from camctl.bootstrap.capture_assembly import (
        _DEFAULT_RETRY_INTERVAL_S, _device_attempts, _device_seconds,
    )
    from camctl.capture.emergency_flow import EmergencySettlement
    from camctl.devices.drivers.runtime import current_registry
    from decimal import Decimal

    stop_config = {}
    for device_id, declaration in deps.config.devices.items():
        stop_config[device_id] = {
            "max_attempts": _device_attempts(declaration, "recording", 3, "max_stop_attempts"),
            "timeout_s": _device_seconds(
                declaration, "recording", "stop_timeout_s", Decimal("10")),
            "retry_interval_s": _device_seconds(
                declaration, "recording", "stop_retry_interval_s",
                _DEFAULT_RETRY_INTERVAL_S),
        }
    settlement = EmergencySettlement(
        session_key=deps.session_key,
        anchors=deps.capture_recording_anchors,
        stop_config=stop_config,
        registry=current_registry(),
        open_owned=lambda: _open_runtime_connection(deps),
        wall_us=lambda: SystemClock().utc_micros(),
    )
    return settlement.settle


def _shutdown_state_outcome(
    outcome: SessionOutcome, failure: ResultFailureMessage,
) -> SessionOutcome:
    """收场的状态库错误主导普通报告错误，其他已有致命错误保持并附加诊断。"""
    details = {"stage": "shutdown",
               "message": f"{failure.error_code}: {failure.error_message}"}
    if outcome.succeeded:
        return SessionOutcome(succeeded=False, reason="state_db_error", details=details)
    previous = dict(outcome.details)
    secondary = list(previous.pop("secondary_errors", ()))
    if outcome.reason == "report_error":
        details["secondary_errors"] = [{"reason": outcome.reason, "details": previous}, *secondary]
        return SessionOutcome(succeeded=False, reason="state_db_error", details=details)
    previous["secondary_errors"] = [*secondary, {"reason": "state_db_error", "details": details}]
    return SessionOutcome(succeeded=False, reason=outcome.reason, details=previous)


def _shutdown_configuration_outcome(
    outcome: SessionOutcome, failure: ResultFailureMessage,
) -> SessionOutcome:
    """配置前提错误主导普通报告错误，保留已有致命主错误及诊断。"""
    details = {"stage": "shutdown", "message": failure.error_message}
    if outcome.succeeded:
        return SessionOutcome(succeeded=False, reason="configuration_error", details=details)
    previous = dict(outcome.details)
    secondary = list(previous.pop("secondary_errors", ()))
    if outcome.reason == "report_error":
        details["secondary_errors"] = [{"reason": outcome.reason, "details": previous}, *secondary]
        return SessionOutcome(succeeded=False, reason="configuration_error", details=details)
    previous["secondary_errors"] = [*secondary, {"reason": "configuration_error", "details": details}]
    return SessionOutcome(succeeded=False, reason=outcome.reason, details=previous)


def close_runtime(
    deps: RuntimeDeps,
    *,
    primary_error: BaseException | None = None,
) -> None:  # noqa: D401 - 见函数体
    """关闭装配层持有的资源；重复关闭不重复处理。

    会话内部资源（锁与连接）由会话流程按收尾次序自行释放；装配
    层持有报告失败日志副本的文件通道。业务日志链正常由
    execute_command 的 close_logging 收场；这里只兜底尚未关闭的
    残留（如会话早期异常），兜底不补写丢弃摘要。
    """
    if deps.closed:
        return
    deps.closed = True

    primary_control: BaseException | None = None
    if primary_error is not None and not isinstance(primary_error, Exception):
        primary_control = primary_error

    def record_close_error(label: str, error: BaseException) -> None:
        nonlocal primary_control
        _write_stderr_diagnostic(
            deps,
            f"{label}: outcome=unknown; "
            f"error={type(error).__name__}: {error}",
        )
        if primary_control is None and not isinstance(error, Exception):
            primary_control = error
            if primary_error is not None and primary_error is not error:
                _attach_runtime_close_note(
                    primary_control, "此前命令异常", primary_error,
                )
        elif primary_control is not None and error is not primary_control:
            _attach_runtime_close_note(primary_control, label, error)

    if deps.failure_channel is not None:
        try:
            deps.failure_channel.close()
        except BaseException as error:
            record_close_error(
                "日志副本处理器关闭诊断：stage=copy_handler_close", error,
            )
    runtime = deps.log_runtime
    if runtime is not None and runtime.close_attempts == 0:
        runtime.close_attempts = 1
        if deps.log_path_unsafe or runtime.path_unsafe:
            from camctl.logging_runtime.service import ChannelError

            runtime.path_unsafe = True
            try:
                runtime.channel.disable_channel(
                    ChannelError("共享文件日志 I/O 或协调不可用"))
            except BaseException as error:
                record_close_error(
                    "共享日志通道停用诊断：stage=log_channel_disable", error,
                )
            try:
                runtime.listener_stopped = runtime.channel.stop_listener(None)
            except BaseException as error:
                runtime.listener_stopped = False
                record_close_error(
                    "共享日志监听线程关闭诊断：stage=log_listener_close", error,
                )
            try:
                if runtime.listener_stopped and runtime.file_closer is not None:
                    runtime.file_closer()
            except BaseException as error:
                record_close_error(
                    "共享日志处理器关闭诊断：stage=log_handler_close", error,
                )
            try:
                runtime.channel.async_close()
            except BaseException as error:
                record_close_error(
                    "共享日志通道关闭诊断：stage=log_channel_close", error,
                )
        else:
            try:
                runtime.listener_stopped = runtime.channel.stop_listener(0.5)
            except BaseException as error:
                runtime.listener_stopped = False
                record_close_error(
                    "共享日志监听线程关闭诊断：stage=log_listener_close", error,
                )
            try:
                if runtime.file_closer is not None:
                    runtime.file_closer()
            except BaseException as error:
                record_close_error(
                    "共享日志处理器关闭诊断：stage=log_handler_close", error,
                )
            try:
                runtime.channel.async_close()
            except BaseException as error:
                record_close_error(
                    "共享日志通道关闭诊断：stage=log_channel_close", error,
                )
    if primary_control is not None and primary_control is not primary_error:
        raise primary_control.with_traceback(primary_control.__traceback__)
