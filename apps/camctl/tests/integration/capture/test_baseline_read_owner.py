"""真实调用及数据库返回保存由同一页拥有者持有。"""
import asyncio

import pytest

from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository

from ..persistence.test_baseline_read_facts import _intent
from ..scheduling.test_start_action import _SCHEDULED, owned

pytestmark=pytest.mark.asyncio


class BlockingDirectory:
    def __init__(self,error=None):
        self.started=asyncio.Event()
        self.cancelled=asyncio.Event()
        self.release=asyncio.Event()
        self.calls=[]
        self.error=error

    async def read_directory(self,request,*,stop):
        self.calls.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        if self.error is not None:
            return DirectoryRead(None,self.error,None)
        cursor=DirectoryCursor(request.binding,request.directories,1,None)
        return DirectoryRead(None,None,Page((),cursor))


@pytest.mark.parametrize('failed',[False,True])
async def test_actual_return_is_saved_before_cancellation_propagates(owned,tmp_path,failed):
    from camctl.capture import baseline_reads as reads
    from camctl.operations.models import ErrorValue
    intent=await _intent(owned,tmp_path)
    owner=reads.PendingBaselineRead(intent)
    error=ErrorValue('directory_failed','baseline',{'reason':'actual'}) if failed else None
    directory=BlockingDirectory(error)
    capture=CaptureRepository()
    clock=[]
    def wall_us():
        clock.append(_SCHEDULED+1)
        return _SCHEDULED+1
    task=asyncio.create_task(reads.advance_baseline_read(owner,directory=directory,
        capture=capture,owned=owned,wall_us=wall_us,can_read=lambda:True))
    try:
        await directory.started.wait()
        assert owned.connection.execute('SELECT state FROM baseline_read_calls').fetchone()==(1,)
        task.cancel()
        await directory.cancelled.wait()
        assert not task.done()
        task.cancel()
        directory.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(directory.calls)==1 and clock==[_SCHEDULED+1]
        assert owned.connection.execute('SELECT state,returned_at FROM baseline_read_calls').fetchone()==(2,_SCHEDULED+1)
        result=reads.resume_baseline_read_save(owner,capture=capture,owned=owned)
        assert result.state.name=='RETURNED' and result.error==error
    finally:
        directory.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)


async def test_unknown_save_keeps_actual_return_and_does_not_read_again(owned,tmp_path):
    from camctl.capture import baseline_reads as reads
    intent=await _intent(owned,tmp_path)
    owner=reads.PendingBaselineRead(intent)
    directory=BlockingDirectory()
    directory.release.set()
    calls=[]
    class Repository(CaptureRepository):
        def save_baseline_read_return(self,request,key,owned):
            calls.append((request,key))
            receipt=super().save_baseline_read_return(request,key,owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
            if len(calls)==1:
                return DbOutcome(DbOutcomeKind.UNKNOWN,error=OSError('lost receipt'))
            return receipt
    capture=Repository()
    assert await reads.advance_baseline_read(owner,directory=directory,capture=capture,
        owned=owned,wall_us=lambda:_SCHEDULED+1,can_read=lambda:True) is None
    assert len(directory.calls)==1
    def no_clock():
        pytest.fail('原返回不能重取时刻')
    result=await reads.advance_baseline_read(owner,directory=directory,capture=capture,
        owned=owned,wall_us=no_clock,can_read=lambda:True)
    assert result.state.name=='RETURNED' and len(directory.calls)==1
    assert calls[0]==calls[1]


async def test_intent_saved_but_cancelled_before_call_has_no_fabricated_return(owned,tmp_path):
    from camctl.capture import baseline_reads as reads
    intent=await _intent(owned,tmp_path)
    owner=reads.PendingBaselineRead(intent)
    directory=BlockingDirectory()
    allowed=iter((True,False))
    result=await reads.advance_baseline_read(owner,directory=directory,capture=CaptureRepository(),
        owned=owned,wall_us=lambda:_SCHEDULED+1,can_read=lambda:next(allowed))
    assert result is None and directory.calls==[]
    assert owner.task is None
    assert owned.connection.execute('SELECT state,result_event_id FROM baseline_read_calls').fetchone()==(1,None)


async def test_return_handoff_waits_for_actual_task_to_finish(owned,tmp_path):
    from camctl.capture import baseline_reads as reads
    from camctl.capture.baseline_models import BaselineReadReturn
    from camctl.capture.baseline_saves import BaselineSaveOwner
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    seen=[]
    owner=reads.PendingBaselineRead(intent)
    class SaveOwner(BaselineSaveOwner):
        def begin(self,request,*,key=None):
            result=super().begin(request,key=key)
            if isinstance(request,BaselineReadReturn):
                assert not owner.task.done()
                seen.append(reads.resume_baseline_read_save(owner,capture=capture,owned=owned))
                assert owned.connection.execute('SELECT state FROM baseline_read_calls').fetchone()==(1,)
            return result
    owner.saves=SaveOwner()
    directory=BlockingDirectory()
    directory.release.set()
    result=await reads.advance_baseline_read(owner,directory=directory,capture=capture,
        owned=owned,wall_us=lambda:_SCHEDULED+1,can_read=lambda:True)
    assert seen==[None] and result.state.name=='RETURNED'


async def test_directory_receives_the_production_stop_port(owned,tmp_path):
    from camctl.capture import baseline_reads as reads
    intent=await _intent(owned,tmp_path)
    class Directory:
        async def read_directory(self,request,*,stop):
            # 实际execute_tool通过requested等待停止请求；此替身使用相同接口。
            waiter=asyncio.create_task(stop.requested())
            waiter.cancel()
            await asyncio.gather(waiter,return_exceptions=True)
            return DirectoryRead(None,None,Page((),DirectoryCursor(
                request.binding,request.directories,1,None)))
    result=await reads.advance_baseline_read(reads.PendingBaselineRead(intent),directory=Directory(),
        capture=CaptureRepository(),owned=owned,wall_us=lambda:_SCHEDULED+1,can_read=lambda:True)
    assert result.state.name=='RETURNED'


async def test_clock_failure_keeps_actual_return_without_repeating_the_call(owned,tmp_path):
    from camctl.capture import baseline_reads as reads
    intent=await _intent(owned,tmp_path)
    owner=reads.PendingBaselineRead(intent)
    directory=BlockingDirectory()
    directory.release.set()
    calls=[]
    def failing_clock():
        calls.append(1)
        raise OSError('clock unavailable')
    with pytest.raises(OSError):
        await reads.advance_baseline_read(owner,directory=directory,capture=CaptureRepository(),
            owned=owned,wall_us=failing_clock,can_read=lambda:True)
    assert owner.actual_return is not None and owner.return_request is None
    assert await reads.advance_baseline_read(owner,directory=directory,capture=CaptureRepository(),
        owned=owned,wall_us=failing_clock,can_read=lambda:True) is None
    assert calls==[1] and len(directory.calls)==1
    assert owned.connection.execute('SELECT state,result_event_id FROM baseline_read_calls').fetchone()==(1,None)
