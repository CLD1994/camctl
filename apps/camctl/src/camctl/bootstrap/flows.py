"""部署装配提供的业务流程构造。

每个流程是接在会话推进循环上的异步端口：接收会话上下文，每轮
被驱动一次，自行管理所需连接与协作者。capture_flow 把到期拍
摄工作推进一个事务批次：窗口内检查到的动作先保存首次观察，窗
口外仍未派发的动作保存过期终态，再把到期动作转入执行并登记设
备活动，最后把执行中的到期动作交给能力处理器；处理器内部按
已保存事实幂等推进，重复调度不产生重复副作用。report_flow
每轮推进报告责任：开始到期的同步动作、补齐已覆盖但未保存的本
地完成、冻结新的报告机会，并把进行中的报告推进到发布。
cancel_flow 推进取消动作：正常会话按可信墙钟执行到期或未排期的取
消动作，时钟异常受限会话只执行未排期取消（同一流程以
unscheduled_only 区分），完成寻址、固定、生效与逐目标收场；收场
未完成的成员保持处理中，等待后续会话。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
from camctl.capture.residual import residual_flow as _residual_flow_impl
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.scheduling import (
    ExpireActionRequest,
    ObserveWindowRequest,
    SchedulingRepository,
    StartActionRequest,
)
from camctl.session.service import StateDbFailure

__all__ = ["capture_flow", "cancel_flow", "report_flow", "residual_flow",
           "winddown_flow"]


def residual_flow(capture_factory):
    """残留收场推进流程；实现见 camctl.capture.residual。"""
    return _residual_flow_impl(capture_factory)


def _due_pending_actions(
    connection: Any, now_us: int,
) -> list[tuple[int, str, int, int]]:
    """从当前投影取到期拍摄动作及窗口事实：待执行、未取消且已到时间。"""
    with closing(connection.execute(
        "SELECT id, device_id, scheduled_at, max_delay_ms FROM actions"
        " WHERE status = 1 AND cancel_requested = 0"
        " AND type IN (1, 2, 3) AND scheduled_at <= ?"
        " ORDER BY plan_id, input_index", (now_us,)
    )) as cursor:
        return [(int(row[0]), row[1], int(row[2]), int(row[3]))
                for row in cursor.fetchall()]


def _ready_device_groups(
    connection: Any, descriptors: Iterable,
) -> list[tuple[str, list]]:
    """把就绪描述符按设备身份分组，保持原推进顺序。"""
    descriptor_list = list(descriptors)
    identities = [descriptor.action_id for descriptor in descriptor_list]
    device_of: dict[int, str] = {}
    if identities:
        placeholders = ",".join("?" * len(identities))
        with closing(connection.execute(
            f"SELECT id, device_id FROM actions WHERE id IN ({placeholders})",
            identities,
        )) as cursor:
            device_of = {int(row[0]): row[1] for row in cursor.fetchall()}
    groups: list[tuple[str, list]] = []
    members: dict[str, list] = {}
    for descriptor in descriptor_list:
        device_id = device_of.get(descriptor.action_id)
        if device_id is None:
            continue
        if device_id not in members:
            members[device_id] = []
            groups.append((device_id, members[device_id]))
        members[device_id].append(descriptor)
    return groups


def capture_flow(capture_factory: Callable[[Any, str], Any]) -> Callable[[Any], Any]:
    """构造推进拍摄工作的调度程序。

    capture_factory 接收本轮流量的数据库连接与设备身份，返回该设
    备组装好的 CaptureRuntime；返回 None 表示该设备本轮不推进（如
    驱动未登记），对应动作保持已保存状态等待后续会话。生产装配提
    供真实驱动端口，集成测试注入受契约约束的替身。
    """

    async def flow(context: Any) -> None:
        owned = context.open_connection()
        try:
            now = context.clock.utc_micros()
            scheduling = SchedulingRepository()
            for (action_id, device_id, scheduled_at,
                 max_delay_ms) in _due_pending_actions(owned.connection, now):
                if now > scheduled_at + max_delay_ms * 1000:
                    # 窗口外仍未派发：按持久化观察区分错过与耗尽。
                    outcome = scheduling.expire_action(
                        ExpireActionRequest(
                            action_id=action_id,
                            trusted_wall_now=now,
                            occurred_at=now,
                        ),
                        new_operation_key(),
                        owned,
                    )
                    if outcome.kind is not DbOutcomeKind.COMPLETED:
                        raise StateDbFailure(
                            f"动作过期事务未完成（{outcome.kind.value}）:"
                            f" {outcome.error}")
                    continue
                # 窗口内检查到动作：先保存首次观察，再判断开始资格。
                observation = scheduling.observe_window(
                    ObserveWindowRequest(
                        action_id=action_id,
                        trusted_wall_now=now,
                        occurred_at=now,
                    ),
                    new_operation_key(),
                    owned,
                )
                if observation.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(
                        f"窗口观察事务未完成（{observation.kind.value}）:"
                        f" {observation.error}")
                # 开始前的残留门：设备上有已结束动作留下的执行中活动
                # 时，按需查询并推进残留收场；未解除前动作保持待执行。
                runtime = capture_factory(owned, device_id)
                if runtime is not None:
                    from camctl.capture.residual import pass_residual_gate
                    if not await pass_residual_gate(
                            runtime, runtime.action(action_id)):
                        continue
                outcome = scheduling.start_action(
                    StartActionRequest(
                        action_id=action_id,
                        trusted_wall_now=now,
                        occurred_at=now,
                    ),
                    new_operation_key(),
                    owned,
                )
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(
                        f"动作开始事务未完成（{outcome.kind.value}）: {outcome.error}")
            descriptors: Iterable = ready_capture_actions(owned.connection, now)
            for device_id, group in _ready_device_groups(
                    owned.connection, descriptors):
                runtime = capture_factory(owned, device_id)
                if runtime is None:
                    continue
                await dispatch_ready(runtime, group)
        finally:
            owned.connection.close()

    return flow


@dataclass(frozen=True)
class _WinddownTarget:
    """受限收场流程选中的执行中录像。"""

    action_id: int


def winddown_flow(
    *, capture_factory: Callable[[Any, str], Any], wait_cap_s: Decimal,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> Callable[[Any], Any]:
    """构造时钟异常会话的录像保守收场流程。

    对既往会话确认启动、尚未停止的执行中录像，以本会话单调钟额
    外等待 min(目标时长, wait_cap_s) 后按原停止预算停止，停止确认
    后保存等待阶段（计时证据不足检查与源文件关联），不启动媒体
    链与正式产物登记，动作保持执行中等待取得正常执行资格的会话；
    取消已生效的录像不经计时立即停止收场。启动未确认或驱动未登
    记的录像本轮不推进，保持已保存状态等待各自责任链。
    """

    async def flow(context: Any) -> None:
        from camctl.capture.handlers import advance_winddown

        owned = context.open_connection()
        try:
            with closing(owned.connection.execute(
                    "SELECT id FROM actions"
                    " WHERE type = 2 AND status = 2"
                    " ORDER BY plan_id, input_index")) as cursor:
                targets = [_WinddownTarget(int(row[0]))
                           for row in cursor.fetchall()]
            first_seen: dict[int, int] = {}
            for device_id, group in _ready_device_groups(
                    owned.connection, targets):
                runtime = capture_factory(owned, device_id)
                if runtime is None:
                    continue
                for target in group:
                    await advance_winddown(
                        target.action_id, runtime, first_seen,
                        wait_cap_s=wait_cap_s, sleep=sleep)
        finally:
            owned.connection.close()

    return flow


def _due_cancel_actions(owned: Any, now_us: int | None) -> list[tuple[int, int, str]]:
    """本次推进的取消动作：已受理且自身未被取消。

    正常会话按可信墙钟包含到期或未排期的动作；受限会话传入
    now_us=None，只包含未排期动作，不依据不可信墙钟判断到时。
    """
    if now_us is None:
        condition = "scheduled_at IS NULL"
        params: tuple = ()
    else:
        condition = "(scheduled_at IS NULL OR scheduled_at <= ?)"
        params = (now_us,)
    with closing(owned.connection.execute(
        f"SELECT id, status, input_fields_json FROM actions"
        f" WHERE type = 6 AND {condition} AND status IN (1, 2)"
        f" AND cancel_requested = 0 ORDER BY id",
        params,
    )) as cursor:
        return [(int(row[0]), int(row[1]), row[2])
                for row in cursor.fetchall()]


def _cancel_target_from_input(spec_json: str):
    """从已受理取消动作的原始输入重建寻址对象；形态不可靠按状态库错误。"""
    from camctl.cancellation.models import CancelTarget

    try:
        params = json.loads(spec_json)["params"]["target"]
    except (ValueError, KeyError, TypeError) as error:
        raise StateDbFailure(f"取消动作目标不可读: {error}") from error
    if not isinstance(params, dict):
        raise StateDbFailure(f"取消动作目标不是对象: {params!r}")
    fields: dict[str, Any] = {}
    for key in ("request_id", "group"):
        if key in params:
            fields[key] = params[key]
    for key in ("plan_instance_id", "action_instance_id"):
        if key in params:
            try:
                fields[key] = int(params[key])
            except (TypeError, ValueError) as error:
                raise StateDbFailure(
                    f"取消动作目标身份不可解释: {key}={params[key]!r}"
                ) from error
    try:
        return CancelTarget(**fields)
    except ValueError as error:
        raise StateDbFailure(f"取消动作目标不构成寻址组合: {error}") from error


def _fixed_cancel_items(owned: Any, action_id: int) -> tuple[int, ...] | None:
    """已固定的取消成员；尚未固定时返回 None。"""
    with closing(owned.connection.execute(
            "SELECT target_selection_state FROM actions WHERE id = ?",
            (action_id,))) as cursor:
        row = cursor.fetchone()
    if row is None or row[0] is None or int(row[0]) != 2:
        return None
    with closing(owned.connection.execute(
            "SELECT id FROM cancel_items WHERE action_id = ? ORDER BY id",
            (action_id,))) as cursor:
        return tuple(int(item[0]) for item in cursor.fetchall())


def _withdrawal_position(owned: Any, ready: Path, processing: Path):
    """按交付文件名观察当前交接位置；观察不到按未知处理。"""
    def position(delivery_id: int) -> str:
        with closing(owned.connection.execute(
                "SELECT file_name FROM deliveries WHERE id = ?",
                (delivery_id,))) as cursor:
            row = cursor.fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            return "unknown"
        if (ready / row[0]).exists():
            return "ready"
        if (processing / row[0]).exists():
            return "processing"
        return "unknown"
    return position


def cancel_flow(
    *, ready: Path, processing: Path, unscheduled_only: bool = False,
    motor_permits: dict | None = None,
) -> Callable[[Any], Any]:
    """构造推进取消动作的会话流程。

    逐动作推进完整取消链：开始（未开始时）→ 寻址 → 固定 → 生效
    与逐目标收场 → 汇总终态。可靠不存在与自身包含按登记错误结束
    取消动作；查询失败按状态库错误停止；收场未完成的成员保持处
    理中，本次不等待设备工作。正常会话按可信墙钟包含到期的已排
    期取消；受限会话以 unscheduled_only=True 构造，只推进未排
    期动作，不依据不可信墙钟判断到时。
    """

    async def flow(context: Any) -> None:
        from camctl.cancellation.models import (
            CancelStartDisposition,
            FinishCancelAction,
            StartCancelAction,
        )
        from camctl.cancellation.service import ApplyCancel
        from camctl.cancellation.rules import (
            CancellationStatus,
            summarize_cancel,
        )
        from camctl.cancellation.service import (
            CancellationRuntime,
            apply_cancel,
        )
        from camctl.cancellation.settlement import TargetSettlement
        from camctl.persistence.repositories.cancellation import (
            CancellationRepository,
        )
        from camctl.persistence.repositories.outputs import OutputsRepository

        owned = context.open_connection()
        try:
            repository = CancellationRepository(motor_permits=motor_permits)
            occurred = context.clock.utc_micros
            now_us = None if unscheduled_only else occurred()
            for action_id, status, spec_json in _due_cancel_actions(owned, now_us):
                item_ids = _fixed_cancel_items(owned, action_id)
                if item_ids is None:
                    if status == 1:
                        started = repository.start_cancel_action(
                            StartCancelAction(
                                action_id=action_id, occurred_at=occurred()),
                            new_operation_key(), owned)
                        if started.kind is not DbOutcomeKind.COMPLETED:
                            raise StateDbFailure(
                                "取消动作开始事务未完成"
                                f"（{started.kind.value}）: {started.error}")
                        if started.value.disposition \
                                is CancelStartDisposition.REJECTED:
                            continue
                    item_ids = _resolve_and_fix(
                        owned, repository, action_id, spec_json, occurred, motor_permits)
                    if item_ids is None:
                        continue
                settlement = TargetSettlement(
                    owned=owned, outputs=OutputsRepository(),
                    cancellations=repository,
                    withdrawal_positions=_withdrawal_position(
                        owned, ready, processing),
                    occurred_at=occurred)
                progress = await apply_cancel(
                    ApplyCancel(origin_action_id=action_id, item_ids=item_ids),
                    CancellationRuntime(
                        owned=owned, repository=repository,
                        settlement=settlement, occurred_at=occurred,
                        motor_permits=motor_permits,
                        open_connection=context.open_connection))
                if summarize_cancel(progress).status is CancellationStatus.RUNNING:
                    # 收场未完成：保持执行中，等待后续会话推进。
                    continue
                finished = repository.finish_cancel_action(
                    FinishCancelAction(action_id, occurred()),
                    new_operation_key(), owned)
                if finished.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(
                        "取消动作终态事务未完成"
                        f"（{finished.kind.value}）: {finished.error}")
        finally:
            owned.connection.close()

    return flow


def _resolve_and_fix(
        owned: Any, repository: Any, action_id: int, spec_json: str,
        occurred: Callable[[], int], motor_permits: dict | None = None,
) -> tuple[int, ...] | None:
    """解析并固定取消目标；可靠不存在或自身包含时按登记错误结束。

    返回固定成员编号；取消动作已按解析失败终态时返回 None。
    """
    from camctl.cancellation.models import (
        FailCancelTargets,
        FixCancelTargets,
        ResolvedTargets,
        TargetFacts,
    )
    from camctl.cancellation.rules import (
        decide_cancel_eligibility,
        load_eligibility_facts,
        may_apply_cancel,
    )
    from camctl.cancellation.targets import (
        missing_target_error,
        prepare_cancel_set,
        resolve_cancel_target,
    )
    from camctl.persistence.repositories.cancellation import (
        SqliteCancelLookup,
        sqlite_auto_candidates,
    )

    target = _cancel_target_from_input(spec_json)
    resolution = resolve_cancel_target(
        target, SqliteCancelLookup(owned.connection))
    if resolution.lookup_failed is not None:
        raise StateDbFailure(
            f"取消目标查询失败: {resolution.lookup_failed}")
    if resolution.missing:
        _fail_cancel_targets(
            owned, repository, action_id,
            missing_target_error(target), occurred)
        return None
    connection = owned.connection
    with closing(connection.execute(
            "SELECT id FROM actions WHERE status IN (3, 4, 5, 6)")) as cursor:
        terminal = {int(row[0]) for row in cursor.fetchall()}
    direct = tuple(
        TargetFacts(
            action_id=identity,
            terminal=identity in terminal,
            may_cancel=may_apply_cancel(decide_cancel_eligibility(
                load_eligibility_facts(connection, identity, motor_permits=motor_permits))))
        for identity in resolution.action_ids)
    fixed = prepare_cancel_set(action_id, ResolvedTargets(
        direct=direct,
        auto_candidates=sqlite_auto_candidates(
            connection, resolution.action_ids)))
    from camctl.cancellation.models import CancelTargetError
    if isinstance(fixed, CancelTargetError):
        _fail_cancel_targets(owned, repository, action_id, fixed, occurred)
        return None
    saved = repository.fix_cancel_targets(
        FixCancelTargets(action_id, fixed, occurred()),
        new_operation_key(), owned)
    if saved.kind is not DbOutcomeKind.COMPLETED:
        raise StateDbFailure(
            f"取消目标固定事务未完成（{saved.kind.value}）: {saved.error}")
    return saved.value.item_ids


def _fail_cancel_targets(
        owned: Any, repository: Any, action_id: int, error, occurred) -> None:
    from camctl.cancellation.models import FailCancelTargets

    outcome = repository.fail_cancel_targets(
        FailCancelTargets(action_id, error, occurred()),
        new_operation_key(), owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise StateDbFailure(
            f"取消目标解析失败事务未完成（{outcome.kind.value}）"
            f": {outcome.error}")


def _report_status_params(spec_json: str) -> dict:
    """从已受理的报告动作读取同步参数；形态非法按状态库错误处理。"""
    try:
        params = json.loads(spec_json)["params"]
    except (ValueError, KeyError, TypeError) as error:
        raise StateDbFailure(f"报告动作参数不可读: {error}") from error
    if not isinstance(params, dict):
        raise StateDbFailure(f"报告动作参数不是对象: {params!r}")
    return params


def _start_due_report_actions(owned: Any, now_us: int) -> None:
    """开始到期的报告动作并建立同步责任；起点在同事务内确定。"""
    from camctl.reporting.models import SyncMode
    from camctl.reporting.policy import start_sync

    with closing(owned.connection.execute(
            "SELECT id, input_fields_json FROM actions"
            " WHERE status = 1 AND cancel_requested = 0 AND type = 7"
            " AND (scheduled_at IS NULL OR scheduled_at <= ?) ORDER BY id",
            (now_us,))) as cursor:
        rows = cursor.fetchall()
    for action_id, spec_json in rows:
        params = _report_status_params(spec_json)
        scope = params.get("scope")
        after_report_id = None
        if scope == "full":
            mode = SyncMode.FULL
        elif scope == "since":
            mode = SyncMode.INCREMENTAL
            try:
                after_report_id = int(params["after_report_id"])
            except (KeyError, TypeError, ValueError) as error:
                raise StateDbFailure(
                    f"报告动作 {action_id} 的同步起点不可解释: {error}") from error
        else:
            raise StateDbFailure(
                f"报告动作 {action_id} 的同步范围未知: {scope!r}")
        outcome = start_sync(
            new_operation_key(), owned, action_id=int(action_id),
            mode=mode, after_report_id=after_report_id, occurred_at=now_us)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise StateDbFailure(
                f"同步开始事务未完成（{outcome.kind.value}）: {outcome.error}")


def _settle_covered_local_syncs(owned: Any, now_us: int) -> None:
    """为已被已发布报告覆盖的同步动作补齐本地完成。

    覆盖条件：报告起点不晚于同步起点，且报告采用的记录已包含该
    同步的开始。保存失败保留责任，由后续机会再保存。
    """
    from camctl.reporting.policy import record_local_report

    with closing(owned.connection.execute(
            "SELECT s.action_id, MAX(r.id) FROM state_syncs s"
            " JOIN reports r ON r.status = 4 AND r.from_wm <= s.from_wm"
            " AND r.frozen_event_id >= s.started_boundary_event_id"
            " WHERE s.status = 1 AND s.local_report_id IS NULL AND s.action_id IN"
            " (SELECT id FROM actions WHERE status = 2)"
            " GROUP BY s.action_id ORDER BY s.action_id")) as cursor:
        rows = cursor.fetchall()
    for action_id, report_id in rows:
        record_local_report(
            new_operation_key(), owned, action_id=int(action_id),
            local_report_id=int(report_id), occurred_at=now_us)


def _covered_local_actions(owned: Any, from_wm: int, frozen_event_id: int) -> tuple[int, ...]:
    """一份报告发布成功后应保存本地完成的同步动作。"""
    with closing(owned.connection.execute(
            "SELECT a.id FROM actions a JOIN state_syncs s ON s.action_id = a.id"
            " WHERE a.status = 2 AND s.status = 1 AND s.local_report_id IS NULL"
            " AND s.from_wm >= ? AND s.started_boundary_event_id <= ?"
            " ORDER BY a.id", (from_wm, frozen_event_id))) as cursor:
        return tuple(int(row[0]) for row in cursor.fetchall())


def _in_flight_report_ids(owned: Any) -> tuple[int, ...]:
    """进行中的报告（已登记、已定字节或已保存发布意图）。"""
    with closing(owned.connection.execute(
            "SELECT id FROM reports WHERE status IN (1, 2, 3) ORDER BY id")) as cursor:
        return tuple(int(row[0]) for row in cursor.fetchall())


def _failed_report_ids(owned: Any) -> tuple[int, ...]:
    with closing(owned.connection.execute(
            "SELECT id FROM reports WHERE status = 5 ORDER BY id")) as cursor:
        return tuple(int(row[0]) for row in cursor.fetchall())


def report_flow(
    *,
    state_db: Path,
    staging: Path,
    ready: Path,
    processing: Path,
    history: Any,
    database: Any,
    supervisor: Any,
    start_actions: bool = True,
    on_recovered: Callable[[], Any] | None = None,
) -> Callable[[Any], Any]:
    """构造推进报告责任的维护流程。

    每轮先开始到期的同步动作并补齐已覆盖的本地完成，再把进行中
    的报告推进到发布；无进行中报告时冻结新的报告机会。报告生成
    经监督方派发给独立子进程；状态库或历史错误按会话错误收场，
    普通报告失败保留责任，不轮询重试。会话开始时的首轮允许重试
    此前失败的报告；同轮失败后不再重复，等待新的触发。受限会话
    的一次报告机会以 start_actions=False 构造：不启动新的同步
    动作，已保存的同步责任仍参与覆盖判断。

    on_recovered 在一轮报告处理全部可靠完成、没有进行中或失败
    遗留的报告责任时调用，用于结束日志副本的故障轮（删除故障标
    记）；调用失败不阻止报告职责。
    """

    instance_state: dict[str, str | None] = {"id": None}

    def _instance_id() -> str:
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

        if instance_state["id"] is None:
            reader = open_existing(
                state_db, DbOpenMode.EXISTING_RO,
                DbConfig(busy_timeout_ms=database.busy_timeout_ms))
            try:
                instance_state["id"] = reader.metadata.instance_id
            finally:
                reader.connection.close()
        return instance_state["id"]

    def _temporary_staging_path(report_id: int, job_id: str) -> Path:
        from camctl.reporting.publication import REPORTS_DIRECTORY

        return staging / REPORTS_DIRECTORY / f"report-{report_id}-{job_id}.tmp"

    def _record_failure(owned: Any, report_id: int, now_us: int, detail: str) -> None:
        from camctl.reporting.policy import record_report_failure

        outcome = record_report_failure(
            new_operation_key(), owned, report_id, {"error": detail},
            occurred_at=now_us)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise StateDbFailure(
                f"报告失败记录事务未完成（{outcome.kind.value}）: {outcome.error}")

    async def _recover_or_generate(
            report_id: int, owned: Any, now_us: int) -> bool:
        """推进一份报告；返回本次调用是否登记了报告失败。"""
        from camctl.contracts.history_values import BoundaryError
        from camctl.contracts.json_values import JsonParseError
        from camctl.contracts.public_projection import PublicProjectionError
        from camctl.contracts.values import ConsistencyError
        from camctl.history.events import HistoryEventError
        from camctl.history.replay import ReplayError
        from camctl.host_files.handoff import HandoffDirectories
        from camctl.persistence.repositories.history import HistoryRepository
        from camctl.persistence.runtime import DbConfig, StateDatabaseError
        from camctl.reporting.messages import JobMessage, new_job_id
        from camctl.reporting.publication import (
            DeliveryOutcome,
            DbPublicationSession,
            ReportDirectories,
            StagedReport,
            deliver_staged_report,
            observe_report_locations,
            parse_report_file_name,
        )
        from camctl.reporting.policy import record_recovered_publication
        from camctl.reporting.supervisor import GenerationOutcomeKind

        with closing(owned.connection.execute(
                "SELECT status FROM reports WHERE id = ?", (report_id,))) as cursor:
            status = int(cursor.fetchone()[0])
        if status in (2, 3):
            # 发布中断恢复：ready 中已有本报告文件时按观察证据补记
            # 发布，不重新生成。
            locations = observe_report_locations(ReportDirectories(
                staging=staging, ready=ready, processing=processing))
            if locations.ready.error is not None:
                raise StateDbFailure(
                    f"报告目录观察失败: {locations.ready.error}")
            recovered = next(
                (identity for identity in locations.ready.files
                 if identity.report_id == report_id), None)
            if recovered is not None:
                outcome = record_recovered_publication(
                    new_operation_key(), owned, report_id,
                    observed_sha256=recovered.sha256, occurred_at=now_us)
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(
                        f"发布恢复事务未完成（{outcome.kind.value}）:"
                        f" {outcome.error}")
                _settle_covered_local_syncs(owned, now_us)
                return False

        try:
            repository = HistoryRepository(
                state_db,
                config=DbConfig(busy_timeout_ms=database.busy_timeout_ms))
            registration = repository.frozen_registration(report_id)
        except (StateDatabaseError, ConsistencyError, PublicProjectionError,
                BoundaryError, HistoryEventError, ReplayError,
                JsonParseError) as error:
            raise StateDbFailure(
                f"报告 {report_id} 的冻结依据不可靠: {error}") from error
        local_actions = _covered_local_actions(
            owned, registration.from_wm, registration.boundary.last_event_id)
        job_id = new_job_id()
        job = JobMessage(
            job_id=job_id,
            report_id=report_id,
            from_wm=registration.from_wm,
            to_wm=registration.to_wm,
            frozen_event_id=registration.boundary.last_event_id,
            instance_id=_instance_id(),
            db_path=str(state_db),
            staging_path=str(_temporary_staging_path(report_id, job_id)),
            entity_batch_size=history.entity_batch_size,
            event_batch_size=history.event_batch_size,
            busy_timeout_ms=database.busy_timeout_ms,
        )
        generation = await supervisor.generate(job)
        if generation.kind is GenerationOutcomeKind.STATE_FAILURE:
            failure = generation.failure
            raise StateDbFailure(
                f"报告 {report_id} 生成遇到状态库错误: "
                f"{failure.error_code}: {failure.error_message}")
        if generation.kind is not GenerationOutcomeKind.SUCCESS:
            failure = generation.failure
            detail = (f"{failure.error_code}: {failure.error_message}"
                      if failure is not None else generation.detail)
            _record_failure(owned, report_id, now_us, f"生成失败: {detail}")
            return True
        success = generation.success
        identity = parse_report_file_name(Path(success.path).name)
        if (identity is None or identity.report_id != report_id
                or identity.sha256 != success.sha256):
            raise StateDbFailure(
                f"报告 {report_id} 的生成文件名与身份不符: {success.path}")
        delivery = await deliver_staged_report(
            report_id,
            StagedReport(name=Path(success.path).name,
                         size_bytes=success.size_bytes, sha256=success.sha256),
            HandoffDirectories(staging, ready),
            DbPublicationSession(owned),
            local_actions=local_actions,
        )
        if delivery.outcome is DeliveryOutcome.PUBLISHED:
            _settle_covered_local_syncs(owned, now_us)
            return False
        if delivery.outcome in (DeliveryOutcome.STORE_FAILED,
                                DeliveryOutcome.UNKNOWN):
            raise StateDbFailure(
                f"报告 {report_id} 发布记录失败: {delivery.error}")
        if delivery.outcome is DeliveryOutcome.HANDOFF_FAILED:
            # 交接不完整的实际错误已由发布编排按其规则保存。
            return False
        _record_failure(owned, report_id, now_us,
                        f"发布失败: {delivery.outcome.value}: {delivery.error}")
        return True

    flow_state: dict[str, bool] = {"started": False}

    async def flow(context: Any) -> None:
        from camctl.reporting.policy import ReportingRepository

        first_round = not flow_state["started"]
        flow_state["started"] = True
        owned = context.open_connection()
        try:
            now_us = context.clock.utc_micros()
            if start_actions:
                _start_due_report_actions(owned, now_us)
            _settle_covered_local_syncs(owned, now_us)
            pending = _in_flight_report_ids(owned)
            if not pending:
                outcome = ReportingRepository().freeze_report(
                    new_operation_key(), owned, occurred_at=now_us)
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(
                        f"报告冻结事务未完成（{outcome.kind.value}）:"
                        f" {outcome.error}")
                pending = _in_flight_report_ids(owned)
            # 会话首轮重试此前失败的报告；同轮失败后不再重复，等待
            # 新的会话触发。
            if first_round and not pending:
                pending = _failed_report_ids(owned)
            round_clean = True
            for report_id in pending:
                if await _recover_or_generate(report_id, owned, now_us):
                    round_clean = False
            if (on_recovered is not None and round_clean
                    and not _in_flight_report_ids(owned)
                    and not _failed_report_ids(owned)):
                # 本轮报告处理全部可靠完成且无遗留失败责任：报告本
                # 地处理已恢复，结束日志副本的故障轮。标记维护的失
                # 败不阻止报告职责。
                try:
                    await on_recovered()
                except Exception:
                    pass
        finally:
            owned.connection.close()

    return flow
