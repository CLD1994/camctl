"""启动前完整目录基准的有界收集与原保存责任恢复。"""
from dataclasses import dataclass, field
from enum import Enum
import asyncio

from camctl.capture.baseline_models import BaselineChunkSave, BaselineFixSave, BaselineState
from camctl.capture.baseline_saves import BaselineSaveOwner
from camctl.capture.models import ActivityReleaseSave
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRead, DirectoryRequest, DirectoryScan
from camctl.operations.models import ErrorValue
from camctl.operations.owned_calls import check_call_interruption
from camctl.persistence.business import SharedSave, settle_required
from camctl.persistence.models import DbOutcomeKind, db_outcome_for
from camctl.persistence.transaction import row_facts


class PreparationPhase(Enum):
    READY = "ready"
    FAILED = "failed"
    WAITING = "waiting"
    PENDING = "pending"
    ABORTED = "aborted"


class _DirectoryStop:
    async def requested(self):
        await asyncio.Event().wait()


@dataclass(frozen=True)
class BaselinePreparation:
    phase: PreparationPhase
    error: ErrorValue | None = None
    database_error: object | None = None


@dataclass
class PendingBaseline:
    """原活动独占清单缓存；内存只保留当前页及原保存申请。"""
    activity_id: int
    binding: DeviceBinding
    directories: tuple[str, ...]
    cursor: DirectoryCursor | None = None
    next_cursor: DirectoryCursor | None = None
    chunks: int = 0
    entries: int = 0
    exhausted: bool = False
    in_call: bool = False
    error: ErrorValue | None = None
    saves: BaselineSaveOwner = field(default_factory=BaselineSaveOwner)
    release: tuple[ActivityReleaseSave, OperationKey] | None = None
    release_save: SharedSave = field(default_factory=SharedSave, repr=False)
    read_owner: object | None = None
    read_no: int = 0
    aborted: bool = False
    directory_scan: DirectoryScan | None = None
    completed: bool = False
    closed: bool = False

    async def read_directory(self, request, *, directory, stop):
        if self.closed or self.completed:
            raise ConsistencyError("已结束的基准拥有者不能重新读取目录")
        if (request.binding != self.binding
                or request.directories != DirectoryRequest(
                    self.binding, self.directories, request.cursor,
                    request.batch, request.timeout_s).directories):
            raise ConsistencyError("基准目录读取不能更换原绑定及完整范围")
        factory = getattr(directory, "directory_scan", None)
        if self.directory_scan is None and callable(factory):
            self.directory_scan = factory(request)
        reader = directory if self.directory_scan is None else self.directory_scan
        return await reader.read_directory(request, stop=stop)

    def close(self):
        """真实调用结束后释放清单；失败时保留原清单供再次关闭。"""
        if self.in_call or (self.read_owner is not None
                and self.read_owner.task is not None and not self.read_owner.task.done()):
            raise ConsistencyError("基准目录调用尚未结束，不能关闭读取缓存")
        if self.directory_scan is not None:
            self.directory_scan.close()
            self.directory_scan = None
        self.closed = True


def close_baseline(action_id, *, runtime):
    """可靠收场后删除拥有者；关闭失败仍保留其清理责任。"""
    pending = runtime.pending_baselines.get(action_id)
    if pending is not None:
        pending.close()
        del runtime.pending_baselines[action_id]


async def recover_terminal_baselines(*, runtime, limit=None):
    """接手已结束目录调用的未派发终态范围，不据空内存推断调用已结束。"""
    from contextlib import closing
    from camctl.capture.baseline_models import BaselineReadState
    from camctl.capture.baseline_reads import PendingBaselineRead
    from camctl.persistence.repositories.segment_result_sets import _boundary
    from camctl.outputs.read_discovery import DEFAULT_BATCH_SIZE, positive_limit

    limit = positive_limit(DEFAULT_BATCH_SIZE if limit is None else limit)
    after = (0, 0)
    while True:
        def read(owned):
            with closing(owned.connection.execute(
            "SELECT d.action_id,d.id,b.id FROM device_activities d INDEXED BY activities_occupancy"
            " JOIN actions a ON a.id=d.action_id JOIN baseline_read_calls b ON b.id=("
            "SELECT MAX(last.id) FROM baseline_read_calls last"
            " WHERE last.action_id=a.id AND last.activity_id=d.id)"
            " WHERE d.occupancy_state=1 AND d.baseline_state=2 AND d.dispatch_state=1"
            " AND a.status IN (3,4,5,6) AND b.state IN (?,?)"
            " AND (d.action_id,d.id)>(?,?) ORDER BY d.action_id,d.id LIMIT ?",
            (int(BaselineReadState.RETURNED), int(BaselineReadState.UNCONFIRMED), *after, limit),
        )) as cursor:
                return cursor.fetchall()
        candidates = await runtime.database.read(read)
        if not candidates:
            return
        for action_id, activity_id, call_id in candidates:
            after = (action_id, activity_id)
            if action_id in runtime.pending_baselines:
                continue
            action = (await runtime.database.read(lambda owned: row_facts(owned.connection, "actions", action_id)))
            activity = (await runtime.database.read(lambda owned: row_facts(owned.connection, "device_activities", activity_id)))
            view = (await runtime.database.read(lambda owned, inputs=(call_id, (await runtime.database.read(lambda owned: _boundary(owned.connection)))), options=dict(): runtime.capture.read_baseline_call(*inputs, owned, **options), description="读取已保存拍摄事实"))
            scope = activity["output_scope_json"]
            if isinstance(scope, str):
                scope = parse_exact_json(scope)
            runtime.pending_baselines[action_id] = PendingBaseline(
                activity_id, DeviceBinding(action["device_id"], action["driver_id"]),
                tuple(scope["directories"]),
                read_owner=PendingBaselineRead(
                    view.request, intent_view=view, result=view, locally_created=False,
                ),
            )
            result = (await resume_baseline_settlement(action_id, runtime=runtime))
            if result is not None and result.phase is PreparationPhase.PENDING:
                raise ConsistencyError(f"原基准收场仍未可靠完成: {result.database_error}")


async def resume_baseline_save(action_id, *, runtime) -> BaselinePreparation | None:
    """恢复原完整申请，不依赖当前驱动、绑定或动作是否已有终态。"""
    return await settle_required(_resume_baseline_save(action_id, runtime=runtime))


async def _resume_baseline_save(action_id, *, runtime):
    pending = runtime.pending_baselines.get(action_id)
    if pending is None:
        return None
    if pending.completed:
        close_baseline(action_id, runtime=runtime)
        return BaselinePreparation(PreparationPhase.READY)
    if pending.in_call:
        # 原 await 拥有者尚未交付调用结果；其他流程只观察，不接管或报保存失败。
        return BaselinePreparation(PreparationPhase.WAITING)
    if pending.read_owner is not None:
        from camctl.capture.segment_baseline import consume_read

        if not (await consume_read(pending, runtime)):
            if pending.read_owner.saves.pending is None:
                return BaselinePreparation(PreparationPhase.WAITING)
            return BaselinePreparation(
                PreparationPhase.PENDING, database_error=pending.read_owner.diagnostic
            )
    held = pending.saves.pending
    if pending.release is None and held is None:
        return None

    async def save_and_apply():
        notification_error = None
        if pending.release is not None:
            from camctl.persistence.repositories.capture import ReleaseOutcome
            request, key = pending.release
            try:
                receipt = await runtime.database.write(
                    lambda owned: runtime.capture.release_occupancy(request, key, owned),
                    key=key, description="保存拍摄业务事实")
            except BaseException as error:
                receipt = db_outcome_for(error)
                if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                    raise
                notification_error = error
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                raise _PendingSave(receipt.error)
            if receipt.value.outcome is ReleaseOutcome.REJECTED:
                raise ConsistencyError(f"原准备收场释放条件未成立: {receipt.value.reason}")
            pending.completed = True
            close_baseline(action_id, runtime=runtime)
            if notification_error is not None:
                raise notification_error
            return BaselinePreparation(PreparationPhase.READY)
        original = held.request
        try:
            receipt = await pending.saves.resume_save(runtime.capture, runtime.database)
        except BaseException as error:
            receipt = db_outcome_for(error)
            if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                raise
            notification_error = error
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise _PendingSave(receipt.error)
        pending.saves.take()
        if isinstance(original, BaselineFixSave):
            pending.completed = True
            close_baseline(action_id, runtime=runtime)
            if notification_error is not None:
                raise notification_error
            return BaselinePreparation(PreparationPhase.READY)
        pending.chunks = original.chunk_no
        pending.entries += len(original.entries)
        pending.cursor = pending.next_cursor
        if notification_error is not None:
            raise notification_error
        return None
    shared = pending.release_save if pending.release is not None else held.application
    shared.retry_failed()
    try:
        return await shared.run(save_and_apply)
    except _PendingSave as incomplete:
        return BaselinePreparation(PreparationPhase.PENDING, database_error=incomplete.error)


class _PendingSave(Exception):
    def __init__(self, error):
        self.error = error


async def release_preparation(action_id, *, runtime):
    """实际拥有者确认准备已收场后，持有原释放申请直到可靠回执。"""
    pending = runtime.pending_baselines.get(action_id)
    if pending is None:
        return None
    if not (await runtime.database.read(lambda owned, inputs=(pending.activity_id,), options=dict(): runtime.capture.preparation_calls_resolved(*inputs, owned, **options), description="读取已保存拍摄事实")):
        return BaselinePreparation(PreparationPhase.WAITING)
    if pending.in_call or pending.saves.pending is not None or pending.read_owner is not None:
        raise ConsistencyError("准备调用或原页保存仍未完成，不能形成释放申请")
    if pending.release is None:
        pending.release = (ActivityReleaseSave(action_id, runtime.wall_us(), preparation_resolved=True,
                                               preparation_error=pending.error),
                           new_operation_key())
    return (await resume_baseline_save(action_id, runtime=runtime))


async def resume_baseline_settlement(action_id, *, runtime):
    """先核原保存，再为已有终态且实际收场的准备解除适用占用。"""
    result = (await resume_baseline_save(action_id, runtime=runtime))
    if result is not None and result.phase in (PreparationPhase.PENDING, PreparationPhase.WAITING):
        return result
    pending = runtime.pending_baselines.get(action_id)
    if pending is None or pending.in_call:
        return result
    action = (await runtime.database.read(lambda owned, inputs=("actions", action_id), options=dict(): row_facts(owned.connection, inputs[0], inputs[1], **options), description="读取拍摄直接事实"))
    if action is None:
        raise ConsistencyError("原基准准备缺少所属动作")
    if action["status"] not in (3, 4, 5, 6):
        return result
    from camctl.persistence.repositories.capture_facts import load_start_facts
    facts = (await runtime.database.read(lambda owned, inputs=(action,), options=dict(): load_start_facts(owned.connection, inputs[0], **options), description="读取拍摄直接事实"))
    if (facts.not_started and facts.activity is not None and facts.activity["ownership_mode"] == 2
            and facts.activity["occupancy_state"] == 1):
        return (await release_preparation(action_id, runtime=runtime))
    close_baseline(action_id, runtime=runtime)
    return result


async def prepare_baseline(action_id: int, activity_id: int, *, runtime) -> BaselinePreparation:
    resumed = (await resume_baseline_save(action_id, runtime=runtime))
    if resumed is not None:
        return resumed
    activity = (await runtime.database.read(lambda owned, inputs=("device_activities", activity_id), options=dict(): row_facts(owned.connection, inputs[0], inputs[1], **options), description="读取拍摄直接事实"))
    action = (await runtime.database.read(lambda owned, inputs=("actions", action_id), options=dict(): row_facts(owned.connection, inputs[0], inputs[1], **options), description="读取拍摄直接事实"))
    if activity is None or action is None or activity["action_id"] != action_id:
        raise ConsistencyError("基准准备缺少原动作及活动身份")
    ownership = enum_for("device_activities.ownership_mode")
    if activity["ownership_mode"] != int(ownership.BASELINE_COMPARISON):
        return BaselinePreparation(PreparationPhase.READY)
    ref = (await runtime.database.read(lambda owned, inputs=(activity_id,), options=dict(): runtime.capture.baseline_ref(*inputs, owned, **options), description="读取已保存拍摄事实"))
    if ref.state is BaselineState.FIXED:
        close_baseline(action_id, runtime=runtime)
        return BaselinePreparation(PreparationPhase.READY)
    dispatch = enum_for("device_activities.dispatch_state")
    if activity["dispatch_state"] != int(dispatch.NOT_DISPATCHED):
        raise ConsistencyError("已派发或可能派发的活动不能重新收集基准")
    if activity["occupancy_state"] != int(enum_for("device_activities.occupancy_state").HELD):
        raise ConsistencyError("基准准备要求仍持有原输出范围")
    pending = runtime.pending_baselines.get(action_id)
    if pending is None:
        scope = activity["output_scope_json"]
        if isinstance(scope, str):
            scope = parse_exact_json(scope)
        pending = PendingBaseline(activity_id, DeviceBinding(action["device_id"], action["driver_id"]),
                                  tuple(scope["directories"]))
        runtime.pending_baselines[action_id] = pending
    if pending.activity_id != activity_id:
        raise ConsistencyError("基准准备拥有者不得更换原活动")
    if pending.in_call:
        return BaselinePreparation(PreparationPhase.WAITING)
    if pending.error is not None:
        return BaselinePreparation(PreparationPhase.FAILED, error=pending.error)
    if runtime.baseline_directory is None:
        return BaselinePreparation(PreparationPhase.WAITING)
    while True:
        check_call_interruption()
        from camctl.scheduling.rules import LaunchWindow, WindowPhase, window_phase
        current = (await runtime.database.read(lambda owned, inputs=("actions", action_id), options=dict(): row_facts(owned.connection, inputs[0], inputs[1], **options), description="读取拍摄直接事实"))
        window = LaunchWindow(current["scheduled_at"], current["scheduled_at"] + current["max_delay_ms"] * 1000)
        if (current["cancel_requested"] or current["status"] in (3, 4, 5, 6)
                or window_phase(window, runtime.wall_us()) is WindowPhase.AFTER_WINDOW):
            return BaselinePreparation(PreparationPhase.ABORTED)
        if pending.exhausted:
            pending.saves.begin(BaselineFixSave(activity_id, pending.chunks, pending.entries, runtime.wall_us()))
        else:
            request = DirectoryRequest(pending.binding, pending.directories, pending.cursor, 128,
                                       runtime.baseline_timeout_s)
            pending.in_call = True
            try:
                result = await pending.read_directory(
                    request, directory=runtime.baseline_directory, stop=_DirectoryStop())
            finally:
                pending.in_call = False
            if not isinstance(result, DirectoryRead):
                raise ConsistencyError("目录端口必须返回完整的可靠页或实际错误")
            if result.error is not None:
                pending.error = result.error
                return BaselinePreparation(PreparationPhase.FAILED, error=result.error)
            page = result.page
            pending.next_cursor = page.next_cursor
            pending.exhausted = page.next_cursor is None
            if not page.items:
                if page.next_cursor == pending.cursor and not pending.exhausted:
                    raise ConsistencyError("空目录页的后续游标必须推进")
                pending.cursor = page.next_cursor
                continue
            pending.saves.begin(BaselineChunkSave(activity_id, pending.chunks + 1,
                                                 page.items, runtime.wall_us()))
        resumed = (await resume_baseline_save(action_id, runtime=runtime))
        if resumed is not None:
            return resumed
