"""一页原目录调用、实际返回及原保存申请的拥有者。"""
import asyncio
from dataclasses import dataclass,field

from camctl.capture.baseline_models import BaselineReadCallView,BaselineReadIntent,BaselineReadReturn
from camctl.capture.baseline_saves import BaselineSaveOwner
from camctl.contracts.values import ConsistencyError
from camctl.devices.directory import DirectoryRead
from camctl.operations.owned_calls import owned_tool_call
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


@owned_tool_call
async def _read_and_freeze(owner,directory,wall_us):
    actual=await directory.read_directory(owner.intent.directory_request(),stop=_DirectoryStop())
    owner.actual_return=actual
    if not isinstance(actual,DirectoryRead):
        raise ConsistencyError('目录调用没有交回真实页或实际错误')
    owner.clock_attempted=True
    owner.returned_at=wall_us()
    owner.return_request=BaselineReadReturn(owner.intent_view.call_id,owner.returned_at,actual.error,actual)
    owner.saves.begin(owner.return_request,key=owner.intent.result_save_key)
    owner.actual_return=None


async def advance_baseline_read(owner,*,directory,capture,owned,wall_us,can_read):
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
            return None
        owner.saves.begin(owner.intent,key=owner.intent.call_key)
        resume_baseline_read_save(owner,capture=capture,owned=owned)
        if owner.intent_view is None:
            return None
    if not can_read():
        return None
    owner.task=asyncio.create_task(_read_and_freeze(owner,directory,wall_us))
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
