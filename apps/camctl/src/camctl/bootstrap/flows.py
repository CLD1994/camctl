"""部署装配提供的业务流程构造。

每个流程是接在会话推进循环上的异步端口：接收会话上下文，每轮
被驱动一次，自行管理所需连接与协作者。capture_flow 把到期拍
摄工作推进一个事务批次：先开始取得时间资格的 pending 动作并
登记设备活动，再把执行中的到期动作交给能力处理器；处理器内
部按已保存事实幂等推进，重复调度不产生重复副作用。report_flow
每轮推进报告责任：开始到期的同步动作、补齐已覆盖但未保存的本
地完成、冻结新的报告机会，并把进行中的报告推进到发布。
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable

from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.scheduling import (
    SchedulingRepository,
    StartActionRequest,
)
from camctl.session.service import StateDbFailure

__all__ = ["capture_flow", "report_flow"]


def _due_pending_actions(connection: Any, now_us: int) -> list[int]:
    """从当前投影取可开始执行的拍摄动作：待执行、未取消且已到时间。"""
    with closing(connection.execute(
        "SELECT id FROM actions WHERE status = 1 AND cancel_requested = 0"
        " AND type IN (1, 2, 3) AND scheduled_at <= ?"
        " ORDER BY plan_id, input_index", (now_us,)
    )) as cursor:
        return [int(row[0]) for row in cursor.fetchall()]


def capture_flow(capture_factory: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """构造推进拍摄工作的调度流程。

    capture_factory 接收本轮流量的数据库连接，返回组装好的
    CaptureRuntime；生产装配提供真实驱动端口，集成测试注入受
    契约约束的替身。
    """

    async def flow(context: Any) -> None:
        owned = context.open_connection()
        try:
            now = context.clock.utc_micros()
            scheduling = SchedulingRepository()
            for action_id in _due_pending_actions(owned.connection, now):
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
            runtime = capture_factory(owned)
            await dispatch_ready(runtime, descriptors)
        finally:
            owned.connection.close()

    return flow


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
) -> Callable[[Any], Any]:
    """构造推进报告责任的维护流程。

    每轮先开始到期的同步动作并补齐已覆盖的本地完成，再把进行中
    的报告推进到发布；无进行中报告时冻结新的报告机会。报告生成
    经监督方派发给独立子进程；状态库或历史错误按会话错误收场，
    普通报告失败保留责任，不轮询重试。会话开始时的首轮允许重试
    此前失败的报告；同轮失败后不再重复，等待新的触发。
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

    async def _recover_or_generate(report_id: int, owned: Any, now_us: int) -> None:
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
                return

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
            return
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
            return
        if delivery.outcome in (DeliveryOutcome.STORE_FAILED,
                                DeliveryOutcome.UNKNOWN):
            raise StateDbFailure(
                f"报告 {report_id} 发布记录失败: {delivery.error}")
        if delivery.outcome is DeliveryOutcome.HANDOFF_FAILED:
            # 交接不完整的实际错误已由发布编排按其规则保存。
            return
        _record_failure(owned, report_id, now_us,
                        f"发布失败: {delivery.outcome.value}: {delivery.error}")

    flow_state: dict[str, bool] = {"started": False}

    async def flow(context: Any) -> None:
        from camctl.reporting.policy import ReportingRepository

        first_round = not flow_state["started"]
        flow_state["started"] = True
        owned = context.open_connection()
        try:
            now_us = context.clock.utc_micros()
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
            for report_id in pending:
                await _recover_or_generate(report_id, owned, now_us)
        finally:
            owned.connection.close()

    return flow
