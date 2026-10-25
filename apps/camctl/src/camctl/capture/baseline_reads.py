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
from camctl.persistence.business import settle_required
from camctl.persistence.models import DbOutcomeKind, db_outcome_for


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


async def resume_baseline_read_save(owner,*,capture,database):
    """只保存原申请；没有目录端口和时钟，不重做实际调用。"""
    return await settle_required(_resume_baseline_read_save(owner, capture=capture, database=database))


async def _resume_baseline_read_save(owner, *, capture, database):
    if owner.result is not None:
        return owner.result
    pending = owner.saves.pending
    if pending is None:
        return None
    if (isinstance(pending.request, BaselineReadReturn)
            and owner.task is not None and not owner.task.done()):
        return None
    async def save_and_apply():
        notification_error = None
        try:
            receipt = await owner.saves.resume_save(capture, database)
        except BaseException as error:
            receipt = db_outcome_for(error)
            if receipt is None or receipt.kind is not DbOutcomeKind.COMPLETED:
                raise
            notification_error = error
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            owner.diagnostic=receipt.error
            raise _PendingReadSave()
        request=pending.request
        response=owner.saves.take()
        if isinstance(request,BaselineReadIntent):
            owner.intent_view=response
            if notification_error is not None:
                raise notification_error
            return None
        owner.result=response
        if notification_error is not None:
            raise notification_error
        return response
    pending.application.retry_failed()
    try:
        return await pending.application.run(save_and_apply)
    except _PendingReadSave:
        return None


class _PendingReadSave(Exception):
    pass


async def abort_unstarted_baseline_read(owner, *, capture, database, wall_us):
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
    (await resume_baseline_read_save(owner, capture=capture, database=database))


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


async def advance_baseline_read(owner,*,directory,capture,database,wall_us,can_read,baseline=None):
    """原实际Task收场、返回冻结和保存交接后才传播等待者取消。"""
    result=(await resume_baseline_read_save(owner,capture=capture,database=database))
    if result is not None:
        return result
    if owner.task is not None:
        if not owner.task.done() or owner.return_request is None:
            return None
        return (await resume_baseline_read_save(owner,capture=capture,database=database))
    if owner.saves.pending is not None:
        return None
    if owner.intent_view is None:
        if not await can_read():
            (await abort_unstarted_baseline_read(owner, capture=capture, database=database, wall_us=wall_us))
            return None
        owner.saves.begin(owner.intent,key=owner.intent.call_key)
        (await resume_baseline_read_save(owner,capture=capture,database=database))
        if owner.intent_view is None:
            return None
    if not await can_read():
        (await abort_unstarted_baseline_read(owner, capture=capture, database=database, wall_us=wall_us))
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
    result=(await resume_baseline_read_save(owner,capture=capture,database=database))
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


async def resume_baseline_read_call(owner,context,*,capture,database,wall_us):
    """先接手原返回或原结果键，再依据既有主机边界处理不可恢复返回。"""
    intent=context.intent_view
    if (context.instance_id != database.metadata.instance_id
            or context.local_owners.get(intent.request.activity_id) is not owner
            or owner.intent != intent.request or owner.intent_view != intent):
        raise ConsistencyError('基准恢复上下文不属于原数据库和实际调用拥有者')
    try:
        actual=await database.read(lambda owned: capture.read_baseline_call(intent.call_id,intent.boundary,owned))
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
        return (await resume_baseline_read_save(owner,capture=capture,database=database))
    if owner.actual_return is not None:
        return None
    if owner.unconfirmed_request is not None:
        if owner.saves.pending is None and owner.result is None:
            owner.saves.begin(owner.unconfirmed_request,key=owner.intent.result_save_key)
        return (await resume_baseline_read_save(owner,capture=capture,database=database))
    try:
        saved=await database.read(lambda owned: capture.read_baseline_call_result(intent.call_id,owned))
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
    return (await resume_baseline_read_save(owner,capture=capture,database=database))
