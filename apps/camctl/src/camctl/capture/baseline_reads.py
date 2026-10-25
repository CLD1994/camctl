"""一页原目录调用、实际返回及原保存申请的拥有者。"""
import asyncio
from dataclasses import dataclass,field

from camctl.capture.baseline_models import (
    BaselineReadCallView,BaselineReadIntent,BaselineReadReturn,BaselineReadUnconfirmedSave)
from camctl.capture.baseline_saves import BaselineSaveOwner
from camctl.contracts.values import ConsistencyError
from camctl.devices.directory import DirectoryRead
from camctl.operations.owned_calls import owned_tool_call
from camctl.operations.recovery import RecoveryBoundary
from camctl.persistence.models import DbOutcomeKind


class _DirectoryStop:
    async def requested(self):
        await asyncio.Event().wait()


@dataclass
class PendingBaselineRead:
    intent: BaselineReadIntent
    saves: BaselineSaveOwner=field(default_factory=BaselineSaveOwner)
    intent_view: BaselineReadCallView|None=None
    task: asyncio.Task|None=None
    actual_return: DirectoryRead|None=None
    returned_at: int|None=None
    return_request: BaselineReadReturn|None=None
    clock_attempted: bool=False
    result: BaselineReadCallView|None=None
    diagnostic: BaseException|None=None
    unconfirmed_request: BaselineReadUnconfirmedSave|None=None
    settlement_at: int|None=None
    settlement_clock_attempted: bool=False
    #: 只授予本会话新建的原意图；冷恢复意图不能据此重做目录调用。
    locally_created: bool=True
    aborted_before_call: bool=False


def resume_baseline_read_save(owner,*,capture,owned):
    """只保存原申请；没有目录端口和时钟，不重做实际调用。"""
    if owner.result is not None:
        return owner.result
    if owner.saves.pending is None:
        return None
    if (isinstance(owner.saves.pending.request,BaselineReadReturn)
            and owner.task is not None and not owner.task.done()):
        return None
    receipt=owner.saves.save(capture,owned)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        owner.diagnostic=receipt.error
        return None
    request=owner.saves.pending.request
    response=owner.saves.take()
    if isinstance(request,BaselineReadIntent):
        owner.intent_view=response
        return None
    owner.result=response
    return response


def abort_unstarted_baseline_read(owner, *, capture, owned, wall_us):
    """实际调用尚未创建时结束原意图，不伪造一次目录返回。"""
    if not owner.locally_created or owner.task is not None:
        return
    owner.aborted_before_call = True
    if owner.intent_view is None or owner.result is not None:
        return
    if owner.unconfirmed_request is None:
        owner.settlement_clock_attempted = True
        owner.settlement_at = wall_us()
        owner.unconfirmed_request = BaselineReadUnconfirmedSave(
            owner.intent_view.call_id, owner.settlement_at
        )
        owner.saves.begin(owner.unconfirmed_request, key=owner.intent.result_save_key)
    resume_baseline_read_save(owner, capture=capture, owned=owned)


@owned_tool_call
async def _read_and_freeze(owner,directory,wall_us,baseline=None):
    request=owner.intent.directory_request()
    actual=(await directory.read_directory(request,stop=_DirectoryStop()) if baseline is None
            else await baseline.read_directory(request,directory=directory,stop=_DirectoryStop()))
    owner.actual_return=actual
    if not isinstance(actual,DirectoryRead):
        raise ConsistencyError('目录调用没有交回真实页或实际错误')
    owner.clock_attempted=True
    owner.returned_at=wall_us()
    owner.return_request=BaselineReadReturn(owner.intent_view.call_id,owner.returned_at,actual.error,actual)
    owner.saves.begin(owner.return_request,key=owner.intent.result_save_key)
    owner.actual_return=None


async def advance_baseline_read(owner,*,directory,capture,owned,wall_us,can_read,baseline=None):
    """原实际Task收场、返回冻结和保存交接后才传播等待者取消。"""
    result=resume_baseline_read_save(owner,capture=capture,owned=owned)
    if result is not None:
        return result
    if owner.task is not None:
        if not owner.task.done() or owner.return_request is None:
            return None
        return resume_baseline_read_save(owner,capture=capture,owned=owned)
    if owner.saves.pending is not None:
        return None
    if owner.intent_view is None:
        if not can_read():
            abort_unstarted_baseline_read(owner, capture=capture, owned=owned, wall_us=wall_us)
            return None
        owner.saves.begin(owner.intent,key=owner.intent.call_key)
        resume_baseline_read_save(owner,capture=capture,owned=owned)
        if owner.intent_view is None:
            return None
    if not can_read():
        abort_unstarted_baseline_read(owner, capture=capture, owned=owned, wall_us=wall_us)
        return owner.result
    if not owner.locally_created:
        return None
    owner.task=asyncio.create_task(_read_and_freeze(owner,directory,wall_us,baseline))
    interrupted=None
    call_error=None
    while True:
        try:
            await asyncio.shield(owner.task)
            break
        except asyncio.CancelledError as error:
            if interrupted is None:
                interrupted=error
                if not owner.task.done():
                    owner.task.cancel()
            if owner.task.done():
                break
        except BaseException as error:
            call_error=error
            owner.diagnostic=error
            break
    result=resume_baseline_read_save(owner,capture=capture,owned=owned)
    if interrupted is not None:
        raise interrupted from call_error
    if call_error is not None:
        raise call_error
    return result


@dataclass(frozen=True)
class BaselineReadRecoveryContext:
    instance_id: str
    intent_view: BaselineReadCallView
    local_owners: object
    recovery_boundary: RecoveryBoundary
    startup_max_event_id: int|None


def resume_baseline_read_call(owner,context,*,capture,owned,reader,wall_us):
    """先接手原返回或原结果键，再依据既有主机边界处理不可恢复返回。"""
    intent=context.intent_view
    if (context.instance_id != owned.metadata.instance_id or context.instance_id != reader.metadata.instance_id
            or context.local_owners.get(intent.request.activity_id) is not owner
            or owner.intent != intent.request or owner.intent_view != intent):
        raise ConsistencyError('基准恢复上下文不属于原数据库和实际调用拥有者')
    try:
        actual=capture.read_baseline_call(intent.call_id,intent.boundary,reader)
    except Exception as error:
        owner.diagnostic=error
        raise
    if actual != intent:
        raise ConsistencyError('基准恢复上下文不是原完整意图事实')
    if owner.task is not None and not owner.task.done():
        return None
    if owner.return_request is not None:
        if owner.saves.pending is None and owner.result is None:
            owner.saves.begin(owner.return_request,key=owner.intent.result_save_key)
        return resume_baseline_read_save(owner,capture=capture,owned=owned)
    if owner.actual_return is not None:
        return None
    if owner.unconfirmed_request is not None:
        if owner.saves.pending is None and owner.result is None:
            owner.saves.begin(owner.unconfirmed_request,key=owner.intent.result_save_key)
        return resume_baseline_read_save(owner,capture=capture,owned=owned)
    try:
        saved=capture.read_baseline_call_result(intent.call_id,reader)
    except Exception as error:
        owner.diagnostic=error
        raise
    if saved is not None:
        owner.result=saved
        return saved
    local_settled=owner.task is not None and owner.task.cancelled()
    cold_settled=(owner.task is None
        and context.recovery_boundary in (RecoveryBoundary.HOST_LOCAL_SETTLED,RecoveryBoundary.HOST_POWER_CYCLE)
        and context.startup_max_event_id is not None
        and intent.intent_event_id<=context.startup_max_event_id)
    if not (local_settled or cold_settled) or owner.settlement_clock_attempted:
        return None
    owner.settlement_clock_attempted=True
    try:
        owner.settlement_at=wall_us()
        owner.unconfirmed_request=BaselineReadUnconfirmedSave(intent.call_id,owner.settlement_at)
        owner.saves.begin(owner.unconfirmed_request,key=owner.intent.result_save_key)
    except Exception as error:
        owner.diagnostic=error
        raise
    return resume_baseline_read_save(owner,capture=capture,owned=owned)
