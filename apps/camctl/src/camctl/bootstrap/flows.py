"""部署装配提供的业务流程构造。

每个流程是接在会话推进循环上的异步端口：接收会话上下文，每轮
被驱动一次，自行管理所需连接与协作者。capture_flow 为每台设备
保留跨轮推进任务及独立连接：窗口内检查到的动作先保存首次观察，窗
口外仍未派发的动作保存过期终态，再把到期动作转入执行并登记设
备活动，最后把执行中的到期动作交给能力处理器；处理器内部按
已保存事实幂等推进，重复调度不产生重复副作用。长调用不阻塞其
他设备下一轮推进，实际任务及保存责任交付后才关闭连接。report_flow
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
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
from camctl.capture.wakeups import RecordingWakeups
from camctl.capture.residual import residual_flow as _residual_flow_impl
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbEnqueueTimeoutError, DbExecutorError, DbOutcomeKind
from camctl.persistence.business import settle_required
from camctl.persistence.repositories.scheduling import (
    ExpireActionRequest,
    ObserveWindowRequest,
    SchedulingRepository,
    StartActionRequest,
)
from camctl.session.service import StateDbFailure, _RequiredPhaseWaiter
from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork
from camctl.outputs.read_discovery import DEFAULT_BATCH_SIZE, ReadDiscoveryOwner
from camctl.scheduling.notifications import WakeReason

__all__ = ["CaptureFlow", "capture_flow", "cancel_flow", "report_flow", "residual_flow",
           "winddown_flow"]


async def _query_rows(database, sql, parameters=()):
    def read(owned):
        with closing(owned.connection.execute(sql, parameters)) as cursor:
            return tuple(cursor.fetchall())
    return await database.read(read)


async def _save_command(database, method, command, *, key=None):
    actual_key = new_operation_key() if key is None else key
    return await database.write(
        lambda owned: method(command, actual_key, owned), key=actual_key)


async def _policy_write(database, method, *args, **kwargs):
    key = new_operation_key()
    return await database.write(
        lambda owned: method(key, owned, *args, **kwargs), key=key)


def residual_flow(capture_factory, *, resume_media_results=None, resume_file_observations=None,
                  resume_read_results=None, owns_device=None, read_discovery_batch_size=DEFAULT_BATCH_SIZE):
    """残留收场推进流程；实现见 camctl.capture.residual。"""
    async def resume_actual_file_facts(database):
        await _resume_actual_file_facts(
            database, resume_file_observations, resume_media_results, resume_read_results)

    return _residual_flow_impl(
        capture_factory, resume_actual_file_facts=resume_actual_file_facts, owns_device=owns_device,
        read_discovery_batch_size=read_discovery_batch_size)


async def _resume_actual_file_facts(database, resume_file_observations, resume_media_results,
                                   resume_read_results=None, resume_capture_completions=None):
    """已有文件与媒体事实先保存；失败保留各自责任并停止业务步骤。"""
    try:
        if resume_read_results is not None:
            await resume_read_results(database)
        if resume_capture_completions is not None:
            await resume_capture_completions(database)
        if resume_file_observations is not None:
            await resume_file_observations(database)
        if resume_media_results is not None:
            await resume_media_results(database)
    except (sqlite3.Error, ConsistencyError) as error:
        raise StateDbFailure(f"原文件事实未可靠保存: {error}") from error


def _due_pending_actions(
    connection: Any, now_us: int, *, device_id: str | None = None,
    action_ids=None, limit=DEFAULT_BATCH_SIZE,
) -> list[tuple[int, str, int, int, int | None]]:
    """从当前投影取到期拍摄动作及窗口事实：待执行、未取消且已到时间。"""
    query = ("SELECT id, device_id, scheduled_at, max_delay_ms, first_window_observed_at FROM actions"
        " WHERE status = 1 AND cancel_requested = 0"
        " AND type IN (1, 2, 3) AND scheduled_at <= ?")
    parameters = (now_us,)
    if device_id is not None:
        query += " AND device_id=?"
        parameters += (device_id,)
    if action_ids is not None:
        if not action_ids:
            return []
        query += " AND id IN (" + ",".join("?" for _ in action_ids) + ")"
        parameters += tuple(action_ids)
    with closing(connection.execute(query + " ORDER BY plan_id, input_index LIMIT ?", (*parameters, limit))) as cursor:
        return [(int(row[0]), row[1], int(row[2]), int(row[3]), row[4])
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


class CaptureFlow:
    """每设备持有一项跨轮任务，原结果沿会话数据库端口保存。"""

    def __init__(self, capture_factory, *, resume_media_results=None,
                 resume_file_observations=None, resume_read_results=None,
                 read_discovery_batch_size=DEFAULT_BATCH_SIZE):
        self._factory = capture_factory
        self._resume_media = resume_media_results
        self._resume_files = resume_file_observations
        self._resume_reads = resume_read_results
        self._devices: dict[str, BackgroundFlow] = {}
        self._stopping = False
        self._discovery = ReadDiscoveryOwner(read_discovery_batch_size)
        self._recording_wakeups = RecordingWakeups()
        self._notification_tasks: dict[str, asyncio.Task] = {}
        leads = getattr(capture_factory, "preparation_leads", None)
        self._preparation_leads = dict(leads) if isinstance(leads, Mapping) else {}
        self._preparation_at = None
        self._device_candidates: dict[str, tuple[int, ...]] = {}

    def next_wakeup_at(self, now, margin_s):
        recording = self._recording_wakeups.next_at(now, margin_s)
        deadlines = [at for at in (recording, self._preparation_at) if at is not None]
        return min(deadlines, default=None)

    def owns_device(self, device_id):
        owner = self._devices.get(device_id)
        return owner is not None and owner.required_settlements() != 0

    def required_settlements(self):
        return sum(owner.required_settlements() for owner in self._devices.values())

    def stop_new_work(self):
        self._stopping = True
        self._recording_wakeups.clear()
        self._preparation_at = None
        CombinedLocalWork(tuple(self._devices.values())).stop_new_work()

    def check_completed(self):
        for device_id, owner in tuple(self._devices.items()):
            try:
                owner.check_completed()
            except BaseException as error:
                try:
                    self.stop_new_work()
                except BaseException as stop_error:
                    _RequiredPhaseWaiter.attach_exception_notes(error, "拍摄停止入口失败", stop_error)
                raise
            if owner.required_settlements() == 0:
                del self._devices[device_id]
                self._notification_tasks.pop(device_id, None)

    async def settle(self):
        try:
            await CombinedLocalWork(tuple(self._devices.values())).settle()
        finally:
            self._devices = {device: owner for device, owner in self._devices.items()
                             if owner.required_settlements() != 0}
            self._notification_tasks = {
                device: task for device, task in self._notification_tasks.items()
                if device in self._devices and self._devices[device].unconsumed_task is task}

    async def __call__(self, context):
        self.check_completed()
        if self._stopping:
            return
        database = context.database
        try:
            await _resume_actual_file_facts(database, self._resume_files, self._resume_media, self._resume_reads)
            now = context.clock.utc_micros()
            await self._discovery.begin_round(database, now)
            if self._preparation_leads:
                from camctl.outputs.read_discovery import next_preparation_at
                page = await self._discovery.captures(database, now, self._preparation_leads)
                at = await database.read(
                    lambda owned: next_preparation_at(owned.connection, now, self._preparation_leads))
                self._preparation_at = (None if at is None else
                    asyncio.get_running_loop().time() + max(0, at - now) / 1_000_000)
            else:
                page = await self._discovery.captures(database, now)
            await self._observe_windows(context, database, tuple(item[0] for item in page.items))
            device_ids = {}
            for action_id, device_id, *_ in page.items:
                device_ids.setdefault(device_id, []).append(action_id)
            if self._discovery.pending and getattr(context, "wake", None) is not None:
                context.wake.mark_changed(WakeReason.READ_OPPORTUNITY)
        except (sqlite3.Error, ConsistencyError) as error:
            raise StateDbFailure(f"拍摄流程状态库前提失效: {error}") from error

        for device_id, action_ids in device_ids.items():
            self._device_candidates[device_id] = tuple(action_ids)
            self.check_completed()
            if self._stopping:
                return
            owner = self._devices.get(device_id)
            if owner is None:
                async def advance(context, device_id=device_id):
                    candidates = self._device_candidates[device_id]
                    await self._advance_device(context, device_id, candidates)
                owner = BackgroundFlow(advance)
                self._devices[device_id] = owner
            await owner(context)
            task = owner.unconsumed_task
            if task is not None and getattr(context, "wake", None) is not None:
                # 每项任务只登记一次；通知不消费任务及其原结果。
                if self._notification_tasks.get(device_id) is not task:
                    self._notification_tasks[device_id] = task
                    def completed(actual, device_id=device_id, owner=owner):
                        if (not self._stopping
                                and self._notification_tasks.get(device_id) is actual
                                and owner.unconsumed_task is actual and actual.done()):
                            context.wake.mark_changed(WakeReason.ACTIVITY_FINISHED)
                    task.add_done_callback(completed)

    async def _observe_windows(self, context, database, action_ids):
        """中央流程保存窗口事实，不等待同设备已有任务交付。"""
        scheduling = SchedulingRepository()
        now = context.clock.utc_micros()
        pending = await database.read(lambda owned: _due_pending_actions(
            owned.connection, now, action_ids=action_ids,
            limit=self._discovery.batch_size))
        for action_id, _, scheduled_at, max_delay_ms, first_observed_at in pending:
            current = context.clock.utc_micros()
            if current > scheduled_at + max_delay_ms * 1000:
                outcome = await _save_command(database, scheduling.expire_action,
                    ExpireActionRequest(action_id, current, current))
                operation = "动作过期"
            elif first_observed_at is None:
                outcome = await _save_command(database, scheduling.observe_window,
                    ObserveWindowRequest(action_id, current, current))
                operation = "窗口观察"
            else:
                continue
            if outcome.kind is not DbOutcomeKind.COMPLETED:
                raise StateDbFailure(f"{operation}事务未完成（{outcome.kind.value}）: {outcome.error}")

    async def _advance_device(self, context, device_id, action_ids):
        database = context.database
        try:
            scheduling = SchedulingRepository()
            runtime = await self._factory(database, device_id)
            if runtime is not None:
                def recording_deadline(action_id, target_ns):
                    if target_ns is None:
                        changed = self._recording_wakeups.discard(device_id, action_id)
                    elif not self._stopping:
                        changed = self._recording_wakeups.schedule(
                            device_id, action_id, target_ns,
                            now_ns=runtime.monotonic_ns(), now_s=asyncio.get_running_loop().time())
                    else:
                        changed = False
                    if changed and getattr(context, "wake", None) is not None:
                        context.wake.mark_changed(WakeReason.ACTIVITY_FINISHED)
                runtime.on_recording_deadline = recording_deadline
            preparation_ready = None
            if runtime is not None:
                from camctl.capture.preparation import recover_preparation
                for action_id in action_ids:
                    if (await runtime.action(action_id))["status"] in (3, 4, 5, 6):
                        await recover_preparation(runtime, action_id)
            if runtime is not None and runtime.capture_preparation_supported is True:
                from camctl.capture.preparation import advance_preparation
                from camctl.capture.baseline import PreparationPhase
                preparation_ready = set()
                for action_id in action_ids:
                    if self._stopping:
                        return
                    from camctl.capture.residual import _original_binding_failure
                    if await _original_binding_failure(runtime, await runtime.action(action_id)) is not None:
                        continue
                    prepared = await advance_preparation(runtime, action_id)
                    if prepared.phase is PreparationPhase.READY:
                        preparation_ready.add(action_id)
            now = context.clock.utc_micros()
            pending = await database.read(lambda owned: _due_pending_actions(owned.connection, now, device_id=device_id,
                action_ids=action_ids, limit=self._discovery.batch_size))
            for action_id, _, scheduled_at, max_delay_ms, _ in pending:
                if self._stopping:
                    return
                current = context.clock.utc_micros()
                if current > scheduled_at + max_delay_ms * 1000:
                    outcome = await _save_command(database, scheduling.expire_action,
                        ExpireActionRequest(action_id, current, current))
                    if outcome.kind is not DbOutcomeKind.COMPLETED:
                        raise StateDbFailure(f"动作过期事务未完成（{outcome.kind.value}）: {outcome.error}")
                    continue
                if runtime is not None:
                    from camctl.capture.residual import pass_residual_gate, _original_binding_failure
                    if not await pass_residual_gate(runtime, await runtime.action(action_id)):
                        continue
                    if (preparation_ready is not None and action_id not in preparation_ready
                            and await _original_binding_failure(runtime, await runtime.action(action_id)) is None):
                        prepared = await advance_preparation(runtime, action_id)
                        if prepared.phase is not PreparationPhase.READY:
                            continue
                if self._stopping:
                    return
                # 执行前查询可能等待；建立准备活动时重新使用实际窗口时刻。
                current = context.clock.utc_micros()
                outcome = await _save_command(database, scheduling.start_action,
                    StartActionRequest(action_id, current, current))
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(f"动作开始事务未完成（{outcome.kind.value}）: {outcome.error}")
            if runtime is None or self._stopping:
                return
            now = context.clock.utc_micros()
            group = await database.read(lambda owned: ready_capture_actions(owned.connection, now, device_id=device_id,
                action_ids=action_ids, limit=self._discovery.batch_size))
            for action_id, outcome in await dispatch_ready(runtime, group):
                if isinstance(outcome, BaseException):
                    raise outcome
            if runtime.capture_preparation_supported is True:
                from camctl.capture.preparation import discard_terminal_preparation
                for action_id in action_ids:
                    await discard_terminal_preparation(runtime, action_id)
        except Exception as error:
            self._stopping = True
            try:
                CombinedLocalWork(tuple(owner for other, owner in self._devices.items()
                                        if other != device_id)).stop_new_work()
            except BaseException as stop_error:
                _RequiredPhaseWaiter.attach_exception_notes(error, "拍摄停止入口失败", stop_error)
            if isinstance(error, (sqlite3.Error, ConsistencyError)):
                raise StateDbFailure(f"拍摄流程状态库前提失效: {error}") from error
            raise



def capture_flow(capture_factory: Callable[[Any, str], Any], *, resume_media_results=None,
                 resume_file_observations=None, resume_read_results=None,
                 read_discovery_batch_size=DEFAULT_BATCH_SIZE) -> CaptureFlow:
    """构造按设备独立推进的拍摄拥有者；工厂返回 None 表示能力未装配。"""
    return CaptureFlow(capture_factory, resume_media_results=resume_media_results,
                       resume_file_observations=resume_file_observations, resume_read_results=resume_read_results,
                       read_discovery_batch_size=read_discovery_batch_size)


@dataclass(frozen=True)
class _WinddownTarget:
    """受限收场流程选中的执行中录像。"""

    action_id: int


def winddown_flow(
    *, capture_factory: Callable[[Any, str], Any], wait_cap_s: Decimal,
    sleep: Callable[[float], Any] = asyncio.sleep,
    resume_media_results=None,
    resume_file_observations=None,
    resume_read_results=None,
    owns_device=None,
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

        database = context.database
        await _resume_actual_file_facts(database, resume_file_observations, resume_media_results,
                                        resume_read_results)
        from camctl.capture.preparation_discovery import preparation_responsibility_where
        preparations = await _query_rows(database,
            "SELECT a.id,a.device_id FROM actions a JOIN device_activities d ON d.action_id=a.id"
            " WHERE " + preparation_responsibility_where(include_pending=True) + " ORDER BY a.id")
        for action_id, device_id in preparations:
            if owns_device is not None and owns_device(device_id):
                continue
            runtime = await capture_factory(database, device_id)
            if runtime is not None:
                from camctl.capture.preparation import recover_preparation
                await recover_preparation(runtime, action_id)
        rows = await _query_rows(database,
            "SELECT id FROM actions WHERE type = 2 AND status = 2 ORDER BY plan_id, input_index")
        targets = [_WinddownTarget(int(row[0])) for row in rows]
        groups = await database.read(lambda owned: _ready_device_groups(owned.connection, targets))
        first_seen: dict[int, int] = {}
        for device_id, group in groups:
            if owns_device is not None and owns_device(device_id):
                continue
            runtime = await capture_factory(database, device_id)
            if runtime is None:
                continue
            for target in group:
                await advance_winddown(target.action_id, runtime, first_seen,
                    wait_cap_s=wait_cap_s, sleep=sleep)

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


def _withdrawal_position(database, ready: Path, processing: Path):
    """数据库只交付名称；文件位置由主协程观察。"""
    async def position(delivery_id: int) -> str:
        rows = await _query_rows(database,
            "SELECT file_name FROM deliveries WHERE id = ?", (delivery_id,))
        if not rows or not isinstance(rows[0][0], str) or not rows[0][0]:
            return "unknown"
        name = rows[0][0]
        if (ready / name).exists():
            return "ready"
        if (processing / name).exists():
            return "processing"
        return "unknown"
    return position


async def _fix_cancel_request(database, repository, request, key, entries):
    """入口许可在主协程封住，工作线程只消费固定的许可事实。"""
    from camctl.devices.start_entry import EntryPhase
    snapshots = []
    for block in request.entry_blocks:
        permit = None if entries is None else entries.get(block.database_id, block.action_id)
        if permit is not None and permit.ticket == block.ticket:
            snapshots.append((block, permit.phase))
    async def save_and_apply():
        outcome = await database.write(lambda owned: repository.fix_cancel_targets(
            request, key, owned, reserved_entries=tuple(snapshots)), key=key)
        if outcome.kind is DbOutcomeKind.COMPLETED and entries is not None:
            for block in request.entry_blocks:
                permit = entries.get(block.database_id, block.action_id)
                if permit is not None and permit.ticket == block.ticket:
                    permit.phase = EntryPhase.BLOCKED
            entries.pending_cancels.pop(request.action_id, None)
        return outcome
    return await settle_required(save_and_apply())


def cancel_flow(
    *, ready: Path, processing: Path, unscheduled_only: bool = False,
    motor_permits: dict | None = None,
    start_entries=None,
    work_files: Any = None,
    resume_media_results=None,
    resume_file_observations=None,
    resume_capture_completions=None,
    cleanup_pending=None,
    preparation_pending=None,
) -> Callable[[Any], Any]:
    """推进原取消集合；设备及文件等待不占用数据库线程。"""
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor
    from camctl.outputs.withdrawal import WithdrawalContext, resume_withdrawals, withdraw_delivery

    executor = work_files.executor if work_files is not None else FileTaskExecutor(Supervisor())
    pending_withdrawals = work_files.pending_withdrawals if work_files is not None else {}
    pending_origin_settlements = {}

    async def flow(context: Any) -> None:
        from camctl.cancellation.models import CancelStartDisposition, FinishCancelAction, StartCancelAction
        from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
        from camctl.cancellation.rules import CancellationStatus, summarize_cancel
        from camctl.cancellation.settlement import TargetSettlement
        from camctl.persistence.repositories.cancellation import CancellationRepository
        from camctl.persistence.repositories.outputs import OutputsRepository

        database = context.database
        if any(identity[0] != database.metadata.instance_id for identity in pending_origin_settlements):
            raise StateDbFailure("原取消收场申请属于另一状态库")
        await _resume_actual_file_facts(database, resume_file_observations, resume_media_results,
                                        resume_capture_completions=resume_capture_completions)
        repository = CancellationRepository()
        if start_entries is not None:
            for request, key in tuple(start_entries.pending_cancels.values()):
                resumed = await _fix_cancel_request(database, repository, request, key, start_entries)
                if resumed.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(f"原入口取消复合申请仍未完成: {resumed.error}")
        occurred = context.clock.utc_micros
        withdrawal_context = WithdrawalContext(database, OutputsRepository(), ready,
            processing, occurred, executor, pending_withdrawals)

        async def execute_withdrawal(delivery_id):
            try:
                return await withdraw_delivery(delivery_id, withdrawal_context)
            except ConsistencyError as error:
                raise StateDbFailure(str(error)) from error

        async def resume_withdrawal(action_id):
            try:
                return await resume_withdrawals(action_id, withdrawal_context)
            except ConsistencyError as error:
                raise StateDbFailure(str(error)) from error

        settlement = TargetSettlement(
            database=database, outputs=OutputsRepository(), cancellations=repository,
            withdrawal_positions=_withdrawal_position(database, ready, processing),
            occurred_at=occurred, work_files=work_files,
            withdrawal_execute=execute_withdrawal, withdrawal_resume=resume_withdrawal,
            cleanup_pending=cleanup_pending, preparation_pending=preparation_pending,
            pending_origin_settlements=pending_origin_settlements)
        cancellation_runtime = CancellationRuntime(
            database=database, repository=repository, settlement=settlement,
            occurred_at=occurred, motor_permits={} if motor_permits is None else motor_permits, start_entries=start_entries,
            pending_origin_settlements=pending_origin_settlements)
        from camctl.cancellation.service import OriginSettle, settle_origin_cancel
        for _, origin_id in tuple(pending_origin_settlements):
            resumed = await settle_origin_cancel(OriginSettle(origin_id), cancellation_runtime)
            if resumed.kind is not DbOutcomeKind.COMPLETED:
                raise StateDbFailure(f"原取消收场申请仍未完成: {resumed.error}")
        now_us = None if unscheduled_only else occurred()
        due = await database.read(lambda owned: _due_cancel_actions(owned, now_us))
        for action_id, status, spec_json in due:
            item_ids = await database.read(lambda owned: _fixed_cancel_items(owned, action_id))
            if item_ids is None:
                if status == 1:
                    started = await _save_command(database, repository.start_cancel_action,
                        StartCancelAction(action_id=action_id, occurred_at=occurred()))
                    if started.kind is not DbOutcomeKind.COMPLETED:
                        raise StateDbFailure(
                            f"取消动作开始事务未完成（{started.kind.value}）: {started.error}")
                    if started.value.disposition is CancelStartDisposition.REJECTED:
                        continue
                item_ids = await _resolve_and_fix(database, repository, action_id, spec_json,
                    occurred, motor_permits, start_entries)
                if item_ids is None:
                    continue
            progress = await apply_cancel(
                ApplyCancel(origin_action_id=action_id, item_ids=item_ids), cancellation_runtime)
            if summarize_cancel(progress).status is CancellationStatus.RUNNING:
                continue
            finished = await _save_command(database, repository.finish_cancel_action,
                FinishCancelAction(action_id, occurred()))
            if finished.kind is not DbOutcomeKind.COMPLETED:
                raise StateDbFailure(
                    f"取消动作终态事务未完成（{finished.kind.value}）: {finished.error}")
    return flow


async def _resolve_and_fix(
        database: Any, repository: Any, action_id: int, spec_json: str,
        occurred: Callable[[], int], motor_permits: dict | None = None, start_entries=None,
) -> tuple[int, ...] | None:
    """只向数据库操作交付本次已经取得的有限许可事实。"""
    from camctl.cancellation.models import FixCancelTargets, ResolvedTargets, TargetFacts, CancelTargetError
    from camctl.cancellation.rules import (
        current_eligibility_facts, decide_cancel_eligibility,
        load_eligibility_inputs, may_link_auto_preview,
    )
    from camctl.cancellation.targets import missing_target_error, prepare_cancel_set, resolve_cancel_target
    from camctl.persistence.repositories.cancellation import SqliteCancelLookup, sqlite_auto_candidates
    from camctl.devices.start_entry import EntryBlock, is_hasselblad_photo
    from camctl.persistence.repositories.capture_facts import load_start_facts

    target = _cancel_target_from_input(spec_json)
    resolution = await database.read(lambda owned: resolve_cancel_target(target, SqliteCancelLookup(owned.connection)))
    if resolution.lookup_failed is not None:
        raise StateDbFailure(f"取消目标查询失败: {resolution.lookup_failed}")
    if resolution.missing:
        await _fail_cancel_targets(database, repository, action_id, missing_target_error(target), occurred)
        return None
    permits = {identity: (motor_permits or {}).get(identity) for identity in resolution.action_ids}
    def read(owned):
        connection = owned.connection
        inputs = []
        candidates = []
        for identity in resolution.action_ids:
            loaded = load_eligibility_inputs(
                connection, identity, motor_permit=permits[identity])
            inputs.append((identity, loaded))
            original = loaded.action
            if (is_hasselblad_photo(original) and original["status"] not in (3,4,5,6)
                    and not original["cancel_requested"]):
                facts = load_start_facts(connection, original)
                candidates.append((identity, facts.attempts_used == 0))
        return (tuple(inputs),
                sqlite_auto_candidates(connection, resolution.action_ids),
                tuple(candidates))
    inputs, auto_candidates, candidates = await database.read(read)
    direct = []
    for identity, loaded in inputs:
        entry = None if start_entries is None else start_entries.get(
            database.metadata.instance_id, identity)
        facts = current_eligibility_facts(
            loaded, None if entry is None else entry.phase)
        direct.append(TargetFacts(
            action_id=identity, terminal=facts.terminal,
            may_cancel=may_link_auto_preview(
                loaded.action, decide_cancel_eligibility(facts))))
    fixed = prepare_cancel_set(action_id, ResolvedTargets(
        direct=tuple(direct), auto_candidates=auto_candidates))
    if isinstance(fixed, CancelTargetError):
        await _fail_cancel_targets(database, repository, action_id, fixed, occurred)
        return None
    blocks = []
    selected = {target.action_id for target in fixed.targets}
    if start_entries is not None:
        for identity, no_intent in candidates:
            if identity not in selected:
                continue
            permit = start_entries.reserve(database.metadata.instance_id, identity, no_intent=no_intent)
            if permit is not None:
                blocks.append(EntryBlock(permit.database_id, permit.action_id, permit.ticket))
    request = FixCancelTargets(action_id, fixed, occurred(), tuple(blocks))
    key = new_operation_key()
    if blocks:
        start_entries.pending_cancels[action_id] = (request, key)
    saved = await _fix_cancel_request(database, repository, request, key, start_entries)
    if saved.kind is not DbOutcomeKind.COMPLETED:
        raise StateDbFailure(f"取消目标固定事务未完成（{saved.kind.value}）: {saved.error}")
    return saved.value.item_ids


async def _fail_cancel_targets(database, repository, action_id, error, occurred):
    from camctl.cancellation.models import FailCancelTargets
    outcome = await _save_command(database, repository.fail_cancel_targets,
        FailCancelTargets(action_id, error, occurred()))
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise StateDbFailure(f"取消目标解析失败事务未完成（{outcome.kind.value}）: {outcome.error}")


def _report_status_params(spec_json: str) -> dict:
    """从已受理的报告动作读取同步参数；形态非法按状态库错误处理。"""
    try:
        params = json.loads(spec_json)["params"]
    except (ValueError, KeyError, TypeError) as error:
        raise StateDbFailure(f"报告动作参数不可读: {error}") from error
    if not isinstance(params, dict):
        raise StateDbFailure(f"报告动作参数不是对象: {params!r}")
    return params


async def _start_due_report_actions(database: Any, now_us: int) -> None:
    """开始到期的报告动作并建立同步责任；起点在同事务内确定。"""
    from camctl.reporting.models import SyncMode
    from camctl.reporting.policy import start_sync

    rows = await _query_rows(database,
        "SELECT id, input_fields_json FROM actions"
        " WHERE status = 1 AND cancel_requested = 0 AND type = 7"
        " AND (scheduled_at IS NULL OR scheduled_at <= ?) ORDER BY id", (now_us,))
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
        outcome = await _policy_write(database, start_sync, action_id=int(action_id),
            mode=mode, after_report_id=after_report_id, occurred_at=now_us)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise StateDbFailure(
                f"同步开始事务未完成（{outcome.kind.value}）: {outcome.error}")


async def _settle_canceled_sync_actions(database: Any, now_us: int) -> None:
    """按目标自身标记完成取消收场，不依赖原取消发起者继续等待。"""
    from camctl.reporting.policy import finish_canceled_sync_action

    rows = await _query_rows(database, "SELECT id FROM actions WHERE type = 7 AND status = 2"
        " AND cancel_requested = 1 ORDER BY id")
    action_ids = tuple(int(row[0]) for row in rows)
    for action_id in action_ids:
        outcome = await _policy_write(database, finish_canceled_sync_action, action_id=action_id, occurred_at=now_us)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise StateDbFailure(
                f"报告取消收场事务未完成（{outcome.kind.value}）: {outcome.error}")


async def _settle_covered_local_syncs(database: Any, now_us: int) -> None:
    """为已被已发布报告覆盖的同步动作补齐本地完成。

    覆盖条件：报告起点不晚于同步起点，且报告采用的记录已包含该
    同步的开始。保存失败保留责任，由后续机会再保存。
    """
    from camctl.reporting.policy import record_local_report

    rows = await _query_rows(database,
        "SELECT s.action_id, MAX(r.id) FROM state_syncs s"
        " JOIN reports r ON r.status = 4 AND r.from_wm <= s.from_wm"
        " AND r.frozen_event_id >= s.started_boundary_event_id"
        " WHERE s.status IN (1, 2) AND s.local_report_id IS NULL AND s.action_id IN"
        " (SELECT id FROM actions WHERE status = 2 AND cancel_requested = 0)"
        " GROUP BY s.action_id ORDER BY s.action_id")
    for action_id, report_id in rows:
        outcome = await _policy_write(database, record_local_report, action_id=int(action_id),
            local_report_id=int(report_id), occurred_at=now_us)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise StateDbFailure(
                f"同步本地完成事务未完成（{outcome.kind.value}）: {outcome.error}")


def _covered_local_actions(owned: Any, from_wm: int, frozen_event_id: int) -> tuple[int, ...]:
    """一份报告发布成功后应保存本地完成的同步动作。"""
    with closing(owned.connection.execute(
            "SELECT a.id FROM actions a JOIN state_syncs s ON s.action_id = a.id"
            " WHERE a.status = 2 AND a.cancel_requested = 0"
            " AND s.status IN (1, 2) AND s.local_report_id IS NULL"
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


@dataclass(frozen=True)
class _ReportMaintenanceFlow:
    advance: Callable[[Any], Any]
    should_advance: Callable[[Any], Any]

    async def __call__(self, context: Any) -> None:
        await self.advance(context)


@dataclass
class _ReportMaintenanceProgress:
    started: bool = False
    checked_to_wm: int = 0
    next_due_at: int | None = None
    remaining_reports: tuple[int, ...] = ()


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
    on_failure: Callable[[Any, Exception], Any] | None = None,
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
    记）；调用失败不阻止报告职责。普通报告文件失败已可靠记入状态
    库后，立即经 on_failure 交给会话的共同副本入口，再推进本轮下一份报告；
    回调执行时不持有状态库写事务。
    """

    def _temporary_staging_path(report_id: int, job_id: str) -> Path:
        from camctl.reporting.publication import REPORTS_DIRECTORY

        return staging / REPORTS_DIRECTORY / f"report-{report_id}-{job_id}.tmp"

    async def _record_failure(business: Any, report_id: int, now_us: int, detail: str) -> None:
        from camctl.devices.bindings import DeviceConfigurationError
        from camctl.persistence.runtime import DirectoryBindingError
        from camctl.reporting.policy import record_report_failure
        from camctl.session.clock import ClockBecameUntrusted

        try:
            outcome = await _policy_write(business, record_report_failure, report_id, {"error": detail},
                occurred_at=now_us)
        except (StateDbFailure, DirectoryBindingError, DeviceConfigurationError,
                ClockBecameUntrusted):
            raise
        except Exception as error:
            # 失败事实也必须可靠保存，才能把本次责任交给后续机会。
            raise StateDbFailure(f"报告失败记录无法可靠保存: {error}") from error
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise StateDbFailure(
                f"报告失败记录事务未完成（{outcome.kind.value}）: {outcome.error}")

    async def _recover_or_generate(
            report_id: int, business: Any, now_us: int, clock) -> str | None:
        """推进一份报告；失败时返回已确认的实际失败摘要。"""
        from camctl.contracts.history_values import BoundaryError
        from camctl.contracts.json_values import JsonParseError
        from camctl.contracts.public_projection import PublicProjectionError
        from camctl.contracts.schemas import SchemaRuleError
        from camctl.contracts.values import ConsistencyError
        from camctl.history.events import HistoryEventError
        from camctl.devices.bindings import DeviceConfigurationError
        from camctl.history.replay import ReplayError
        from camctl.host_files.handoff import HandoffDirectories
        from camctl.persistence.repositories.history import HistoryRepository
        from camctl.persistence.runtime import DbConfig, DirectoryBindingError, StateDatabaseError
        from camctl.reporting.messages import ErrorKind, JobMessage, new_job_id
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
        from camctl.session.clock import ClockBecameUntrusted

        prerequisite_errors = (
            StateDbFailure, DirectoryBindingError, DeviceConfigurationError, ClockBecameUntrusted,
        )
        state_errors = (
            StateDatabaseError, sqlite3.Error, ConsistencyError,
            PublicProjectionError, BoundaryError, HistoryEventError, ReplayError,
            JsonParseError, SchemaRuleError,
        )

        status, attempted_wm = (await _query_rows(business,
            "SELECT status, to_wm FROM reports WHERE id = ?", (report_id,)))[0]
        from camctl.reporting.ack import validate_watermark
        validate_watermark(attempted_wm, "attempted_to_wm")
        progress.checked_to_wm = max(progress.checked_to_wm, attempted_wm)
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
                outcome = await _policy_write(business, record_recovered_publication, report_id,
                    observed_sha256=recovered.sha256, occurred_at=now_us)
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(
                        f"发布恢复事务未完成（{outcome.kind.value}）:"
                        f" {outcome.error}")
                await _settle_covered_local_syncs(business, now_us)
                return None

        try:
            from camctl.persistence.repositories.history import read_frozen_registration
            registration = await business.read(
                lambda owned: read_frozen_registration(owned.connection, report_id))
        except (StateDatabaseError, ConsistencyError, PublicProjectionError,
                BoundaryError, HistoryEventError, ReplayError,
                JsonParseError) as error:
            raise StateDbFailure(
                f"报告 {report_id} 的冻结依据不可靠: {error}") from error
        local_actions = await business.read(lambda owned: _covered_local_actions(
            owned, registration.from_wm, registration.boundary.last_event_id))
        job_id = new_job_id()
        job = JobMessage(
            job_id=job_id,
            report_id=report_id,
            from_wm=registration.from_wm,
            to_wm=registration.to_wm,
            frozen_event_id=registration.boundary.last_event_id,
            instance_id=business.metadata.instance_id,
            db_path=str(state_db),
            staging_path=str(_temporary_staging_path(report_id, job_id)),
            staging_root=str(staging), ready_root=str(ready), processing_root=str(processing),
            entity_batch_size=history.entity_batch_size,
            event_batch_size=history.event_batch_size,
            busy_timeout_ms=database.busy_timeout_ms,
        )
        try:
            generation = await supervisor.generate(job)
        except prerequisite_errors:
            raise
        except state_errors as error:
            raise StateDbFailure(f"报告 {report_id} 生成遇到状态库错误: {error}") from error
        except Exception as error:
            # 监督结果之外的普通生成错误也结束本次已冻结责任；
            # 保留原错误身份交给报告边界，不因下次轮询重新派发。
            await _record_failure(business, report_id, now_us, f"报告生成失败: {error}")
            raise
        if generation.kind is GenerationOutcomeKind.STATE_FAILURE:
            failure = generation.failure
            raise StateDbFailure(
                f"报告 {report_id} 生成遇到状态库错误: "
                f"{failure.error_code}: {failure.error_message}")
        if (generation.failure is not None
                and generation.failure.error_kind is ErrorKind.REPORT
                and generation.failure.error_code == "configuration_error"):
            from camctl.persistence.runtime import DirectoryBindingError

            raise DirectoryBindingError(generation.failure.error_message)
        if generation.kind is not GenerationOutcomeKind.SUCCESS:
            failure = generation.failure
            detail = (f"{failure.error_code}: {failure.error_message}"
                      if failure is not None else generation.detail)
            failure_detail = f"报告生成失败: {detail}"
            await _record_failure(business, report_id, now_us, failure_detail)
            return f"report_id={report_id}; {failure_detail}"
        success = generation.success
        identity = parse_report_file_name(Path(success.path).name)
        if (identity is None or identity.report_id != report_id
                or identity.sha256 != success.sha256):
            raise StateDbFailure(
                f"报告 {report_id} 的生成文件名与身份不符: {success.path}")
        try:
            delivery = await deliver_staged_report(
                report_id,
                StagedReport(name=Path(success.path).name,
                             size_bytes=success.size_bytes, sha256=success.sha256),
                HandoffDirectories(staging, ready),
                DbPublicationSession(business, occurred_at=clock),
                local_actions=local_actions,
            )
        except prerequisite_errors:
            raise
        except state_errors as error:
            raise StateDbFailure(f"报告 {report_id} 文件交接遇到状态库错误: {error}") from error
        except Exception as error:
            await _record_failure(business, report_id, now_us, f"报告文件交接失败: {error}")
            raise
        if delivery.outcome is DeliveryOutcome.PUBLISHED:
            await _settle_covered_local_syncs(business, now_us)
            return None
        if delivery.outcome in (DeliveryOutcome.STORE_FAILED,
                                DeliveryOutcome.UNKNOWN):
            raise StateDbFailure(
                f"报告 {report_id} 发布记录失败: {delivery.error}")
        if delivery.outcome is DeliveryOutcome.HANDOFF_FAILED:
            # 交接不完整的实际错误已由发布编排按其规则保存。
            return (
                f"report_id={report_id}; 报告文件交接失败: "
                f"{delivery.error or delivery.outcome.value}")
        failure_detail = (
            f"报告发布失败: {delivery.outcome.value}: {delivery.error}")
        await _record_failure(business, report_id, now_us, failure_detail)
        return f"report_id={report_id}; {failure_detail}"

    progress = _ReportMaintenanceProgress()

    async def should_advance(context: Any) -> bool:
        observer = getattr(context, "report_change_wm", None)
        if not start_actions or observer is None:
            return True
        try:
            latest = await observer()
            from camctl.reporting.ack import validate_watermark
            validate_watermark(latest, "latest_change_wm")
        except (sqlite3.Error, ConsistencyError, DbEnqueueTimeoutError, DbExecutorError) as error:
            raise StateDbFailure(f"报告维护无法可靠观察业务水位: {error}") from error
        return (not progress.started or bool(progress.remaining_reports)
                or latest > progress.checked_to_wm
                or (progress.next_due_at is not None
                    and context.clock.utc_micros() >= progress.next_due_at))

    async def flow(context: Any) -> None:
        from camctl.persistence.runtime import DirectoryBindingError, StateDatabaseError
        from camctl.reporting.policy import ReportingRepository

        first_round = not progress.started
        progress.started = True
        business = context.database
        try:
            now_us = context.clock.utc_micros()
            await _settle_canceled_sync_actions(business, now_us)
            if start_actions:
                await _start_due_report_actions(business, now_us)
            await _settle_covered_local_syncs(business, now_us)
            if start_actions and getattr(context, "report_change_wm", None) is not None:
                rows = await _query_rows(business,
                    "SELECT MIN(COALESCE(scheduled_at, ?)) FROM actions"
                    " WHERE status = 1 AND cancel_requested = 0 AND type = 7", (now_us,))
                progress.next_due_at = rows[0][0]
            in_flight = await business.read(_in_flight_report_ids)
            pending = tuple(dict.fromkeys((*in_flight, *progress.remaining_reports)))
            if not pending:
                async def freeze_and_apply():
                    outcome = await _policy_write(business, ReportingRepository().freeze_report, occurred_at=now_us)
                    if outcome.kind is not DbOutcomeKind.COMPLETED:
                        raise StateDbFailure(
                            f"报告冻结事务未完成（{outcome.kind.value}）: {outcome.error}")
                    if getattr(context, "report_change_wm", None) is not None:
                        progress.checked_to_wm = max(progress.checked_to_wm, outcome.value.checked_to_wm)
                    return outcome
                await settle_required(freeze_and_apply())
                pending = await business.read(_in_flight_report_ids)
            if first_round and not pending:
                pending = await business.read(_failed_report_ids)
            round_clean = True
            progress.remaining_reports = ()
            for index, report_id in enumerate(pending):
                progress.remaining_reports = pending[index + 1:]
                failure_detail = await _recover_or_generate(
                    report_id, business, now_us, context.clock.utc_micros)
                if failure_detail is not None:
                    round_clean = False
                    if on_failure is not None:
                        try:
                            await on_failure(context, RuntimeError(failure_detail))
                        except Exception:
                            pass
            if on_recovered is not None and round_clean:
                remaining = await business.read(lambda owned: (
                    _in_flight_report_ids(owned), _failed_report_ids(owned)))
                if not any(remaining):
                    try:
                        await on_recovered()
                    except Exception:
                        pass
        except DirectoryBindingError:
            raise
        except (StateDatabaseError, sqlite3.Error, ConsistencyError,
                DbEnqueueTimeoutError, DbExecutorError) as error:
            raise StateDbFailure(f"报告维护无法可靠读取或保存状态库事实: {error}") from error

    return _ReportMaintenanceFlow(flow, should_advance)
