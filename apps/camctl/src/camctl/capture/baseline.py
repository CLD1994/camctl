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
from camctl.devices.directory import DirectoryCursor, DirectoryRead, DirectoryRequest
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.transaction import row_facts


class PreparationPhase(Enum):
    READY = "ready"
    FAILED = "failed"
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
    """原页保存前保留下一游标；每次最多持有一页，不保留完整目录。"""
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


def resume_baseline_save(action_id, *, runtime) -> BaselinePreparation | None:
    """恢复原完整申请，不依赖当前驱动、绑定或动作是否已有终态。"""
    pending = runtime.pending_baselines.get(action_id)
    if pending is None:
        return None
    if pending.release is not None:
        from camctl.persistence.repositories.capture import ReleaseOutcome
        request, key = pending.release
        receipt = runtime.capture.release_occupancy(request, key, runtime.owned)
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            return BaselinePreparation(PreparationPhase.PENDING, database_error=receipt.error)
        if receipt.value.outcome is ReleaseOutcome.REJECTED:
            raise ConsistencyError(f"原准备收场释放条件未成立: {receipt.value.reason}")
        del runtime.pending_baselines[action_id]
        return BaselinePreparation(PreparationPhase.READY)
    if pending.saves.pending is None:
        return None
    original = pending.saves.pending.request
    receipt = pending.saves.save(runtime.capture, runtime.owned)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        return BaselinePreparation(PreparationPhase.PENDING, database_error=receipt.error)
    pending.saves.take()
    if isinstance(original, BaselineFixSave):
        del runtime.pending_baselines[action_id]
        return BaselinePreparation(PreparationPhase.READY)
    pending.chunks = original.chunk_no
    pending.entries += len(original.entries)
    pending.cursor = pending.next_cursor
    return None


def release_preparation(action_id, *, runtime):
    """实际拥有者确认准备已收场后，持有原释放申请直到可靠回执。"""
    pending = runtime.pending_baselines.get(action_id)
    if pending is None:
        return None
    if pending.in_call or pending.saves.pending is not None:
        raise ConsistencyError("准备调用或原页保存仍未完成，不能形成释放申请")
    if pending.release is None:
        pending.release = (ActivityReleaseSave(action_id, runtime.wall_us(), preparation_resolved=True,
                                               preparation_error=pending.error),
                           new_operation_key())
    return resume_baseline_save(action_id, runtime=runtime)


def resume_baseline_settlement(action_id, *, runtime):
    """先核原保存，再为已有终态且实际收场的准备解除适用占用。"""
    result = resume_baseline_save(action_id, runtime=runtime)
    if result is not None and result.phase is PreparationPhase.PENDING:
        return result
    pending = runtime.pending_baselines.get(action_id)
    if pending is None or pending.in_call:
        return result
    action = row_facts(runtime.owned.connection, "actions", action_id)
    if action is None:
        raise ConsistencyError("原基准准备缺少所属动作")
    if action["status"] not in (3, 4, 5, 6):
        return result
    from camctl.persistence.repositories.capture_facts import load_start_facts
    facts = load_start_facts(runtime.owned.connection, action)
    if (facts.not_started and facts.activity is not None and facts.activity["ownership_mode"] == 2
            and facts.activity["occupancy_state"] == 1):
        return release_preparation(action_id, runtime=runtime)
    runtime.pending_baselines.pop(action_id, None)
    return result


async def prepare_baseline(action_id: int, activity_id: int, *, runtime) -> BaselinePreparation:
    resumed = resume_baseline_save(action_id, runtime=runtime)
    if resumed is not None:
        return resumed
    activity = row_facts(runtime.owned.connection, "device_activities", activity_id)
    action = row_facts(runtime.owned.connection, "actions", action_id)
    if activity is None or action is None or activity["action_id"] != action_id:
        raise ConsistencyError("基准准备缺少原动作及活动身份")
    ownership = enum_for("device_activities.ownership_mode")
    if activity["ownership_mode"] != int(ownership.BASELINE_COMPARISON):
        return BaselinePreparation(PreparationPhase.READY)
    ref = runtime.capture.baseline_ref(activity_id, runtime.owned)
    if ref.state is BaselineState.FIXED:
        runtime.pending_baselines.pop(action_id, None)
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
        return BaselinePreparation(PreparationPhase.PENDING)
    if pending.error is not None:
        return BaselinePreparation(PreparationPhase.FAILED, error=pending.error)
    if runtime.baseline_directory is None:
        return BaselinePreparation(PreparationPhase.PENDING)
    while True:
        from camctl.scheduling.rules import LaunchWindow, WindowPhase, window_phase
        current = row_facts(runtime.owned.connection, "actions", action_id)
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
                result = await runtime.baseline_directory.read_directory(request, stop=_DirectoryStop())
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
        resumed = resume_baseline_save(action_id, runtime=runtime)
        if resumed is not None:
            return resumed
