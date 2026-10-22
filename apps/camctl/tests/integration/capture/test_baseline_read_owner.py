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


@pytest.mark.parametrize('boundary_name,horizon,qualified',[
    ('HOST_LOCAL_SETTLED','equal',True),('HOST_POWER_CYCLE','equal',True),
    ('HOST_LOCAL_SETTLED','later',True),('HOST_LOCAL_SETTLED','before',False),
    ('HOST_LOCAL_SETTLED','none',False),('UNCONFIRMED','equal',False)])
async def test_cold_recovery_uses_original_fixed_host_boundary(owned,tmp_path,boundary_name,horizon,qualified):
    from camctl.capture import baseline_reads as reads
    from camctl.operations.recovery import RecoveryBoundary
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    owner=reads.PendingBaselineRead(intent,intent_view=first.value)
    owners={intent.activity_id:owner}
    maximum={'equal':first.value.intent_event_id,'later':first.value.intent_event_id+1,
        'before':first.value.intent_event_id-1,'none':None}[horizon]
    context=reads.BaselineReadRecoveryContext(owned.metadata.instance_id,first.value,owners,
        RecoveryBoundary[boundary_name],maximum)
    calls=[]
    def clock():
        calls.append(1)
        return _SCHEDULED+5
    result=reads.resume_baseline_read_call(owner,context,capture=capture,owned=owned,
        reader=owned,wall_us=clock)
    if qualified:
        assert result.state.name=='UNCONFIRMED' and result.returned_at is None and calls==[1]
        assert reads.resume_baseline_read_call(owner,context,capture=capture,owned=owned,
            reader=owned,wall_us=clock)==result and calls==[1]
    else:
        assert result is None and calls==[]
        assert owned.connection.execute('SELECT state FROM baseline_read_calls').fetchone()==(1,)


async def test_current_task_prevents_cold_recovery_until_actual_handoff(owned,tmp_path):
    from camctl.capture import baseline_reads as reads
    from camctl.operations.recovery import RecoveryBoundary
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    owner=reads.PendingBaselineRead(intent)
    directory=BlockingDirectory()
    task=asyncio.create_task(reads.advance_baseline_read(owner,directory=directory,capture=capture,
        owned=owned,wall_us=lambda:_SCHEDULED+1,can_read=lambda:True))
    try:
        await directory.started.wait()
        context=reads.BaselineReadRecoveryContext(owned.metadata.instance_id,owner.intent_view,
            {intent.activity_id:owner},RecoveryBoundary.HOST_POWER_CYCLE,owner.intent_view.intent_event_id)
        def no_clock():
            pytest.fail('在途责任不能取得未知结果时刻')
        assert reads.resume_baseline_read_call(owner,context,capture=capture,owned=owned,
            reader=owned,wall_us=no_clock) is None
        directory.release.set()
        result=await task
        assert result.state.name=='RETURNED'
    finally:
        directory.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.parametrize('kind',['empty','nonempty','error','unknown'])
async def test_cold_recovery_prefers_committed_return_even_with_no_cold_qualification(owned,tmp_path,kind):
    from camctl.capture import baseline_reads as reads
    from camctl.capture.baseline_models import BaselineReadReturn,BaselineReadUnconfirmedSave
    from camctl.operations.models import ErrorValue
    from camctl.operations.recovery import RecoveryBoundary
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from .test_baseline_history import _entries
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    error=ErrorValue('transport_timeout','directory unavailable') if kind=='error' else None
    returned=BaselineReadReturn(first.value.call_id,_SCHEDULED+1,error,DirectoryRead(None,error,
        None if error else Page(_entries('a.OSV') if kind=='nonempty' else (),
            DirectoryCursor(intent.binding,intent.directories,1,None))))
    saved=(capture.save_baseline_read_unconfirmed(
        BaselineReadUnconfirmedSave(first.value.call_id,_SCHEDULED+2),intent.result_save_key,owned)
        if kind=='unknown' else capture.save_baseline_read_return(returned,intent.result_save_key,owned))
    assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
    owner=reads.PendingBaselineRead(intent,intent_view=first.value)
    context=reads.BaselineReadRecoveryContext(owned.metadata.instance_id,first.value,
        {intent.activity_id:owner},RecoveryBoundary.UNCONFIRMED,None)
    def no_clock():
        pytest.fail('已有实际返回不能取新的未知时刻')
    fresh=open_existing(tmp_path/'state.db',DbOpenMode.EXISTING_RW,DbConfig())
    try:
        assert reads.resume_baseline_read_call(owner,context,capture=capture,owned=owned,
            reader=fresh,wall_us=no_clock)==saved.value
    finally:
        fresh.connection.close()


async def test_unconfirmed_save_retry_keeps_original_settlement_time(owned,tmp_path):
    from camctl.capture import baseline_reads as reads
    from camctl.operations.recovery import RecoveryBoundary
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    owner=reads.PendingBaselineRead(intent,intent_view=first.value)
    context=reads.BaselineReadRecoveryContext(owned.metadata.instance_id,first.value,
        {intent.activity_id:owner},RecoveryBoundary.HOST_LOCAL_SETTLED,first.value.intent_event_id)
    requests=[]
    class Repository(CaptureRepository):
        def save_baseline_read_unconfirmed(self,request,key,owned):
            requests.append((request,key))
            receipt=super().save_baseline_read_unconfirmed(request,key,owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN,error=OSError('lost receipt')) if len(requests)==1 else receipt
    clock=[]
    def wall_us():
        clock.append(1)
        return _SCHEDULED+5
    repository=Repository()
    assert reads.resume_baseline_read_call(owner,context,capture=repository,owned=owned,
        reader=owned,wall_us=wall_us) is None
    result=reads.resume_baseline_read_call(owner,context,capture=repository,owned=owned,
        reader=owned,wall_us=wall_us)
    assert result.state.name=='UNCONFIRMED' and clock==[1] and requests[0]==requests[1]


async def test_same_session_cancelled_actual_task_can_save_unknown_without_cold_boundary(owned,tmp_path):
    from camctl.capture import baseline_reads as reads
    from camctl.operations.recovery import RecoveryBoundary
    intent=await _intent(owned,tmp_path)
    owner=reads.PendingBaselineRead(intent)
    capture=CaptureRepository()
    started=asyncio.Event()
    class Directory:
        async def read_directory(self,request,*,stop):
            started.set()
            await asyncio.Event().wait()
    task=asyncio.create_task(reads.advance_baseline_read(owner,directory=Directory(),capture=capture,
        owned=owned,wall_us=lambda:_SCHEDULED+1,can_read=lambda:True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.task.cancelled() and owner.actual_return is None
    context=reads.BaselineReadRecoveryContext(owned.metadata.instance_id,owner.intent_view,
        {intent.activity_id:owner},RecoveryBoundary.UNCONFIRMED,None)
    result=reads.resume_baseline_read_call(owner,context,capture=capture,owned=owned,
        reader=owned,wall_us=lambda:_SCHEDULED+5)
    assert result.state.name=='UNCONFIRMED'


@pytest.mark.parametrize('lookup',['intent','result'])
async def test_failed_result_lookup_keeps_recovery_responsibility(owned,tmp_path,lookup):
    from camctl.capture import baseline_reads as reads
    from camctl.operations.recovery import RecoveryBoundary
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    owner=reads.PendingBaselineRead(intent,intent_view=first.value)
    context=reads.BaselineReadRecoveryContext(owned.metadata.instance_id,first.value,
        {intent.activity_id:owner},RecoveryBoundary.HOST_LOCAL_SETTLED,first.value.intent_event_id)
    class Repository(CaptureRepository):
        def read_baseline_call(self,*args,**kwargs):
            if lookup=='intent':
                raise OSError('intent unavailable')
            return super().read_baseline_call(*args,**kwargs)
        def read_baseline_call_result(self,*args,**kwargs):
            raise OSError('read unavailable')
    def no_clock():
        pytest.fail('读取失败不能解释为可靠缺键')
    with pytest.raises(OSError) as caught:
        reads.resume_baseline_read_call(owner,context,capture=Repository(),owned=owned,
            reader=owned,wall_us=no_clock)
    assert owner.diagnostic is caught.value
    assert owner.unconfirmed_request is None and owner.result is None
    assert owned.connection.execute('SELECT state FROM baseline_read_calls').fetchone()==(1,)


@pytest.mark.parametrize('failure',['clock','encoding'])
async def test_unknown_clock_or_encoding_failure_keeps_one_actual_attempt(owned,tmp_path,failure):
    from camctl.capture import baseline_reads as reads
    from camctl.operations.recovery import RecoveryBoundary
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    owner=reads.PendingBaselineRead(intent,intent_view=first.value)
    context=reads.BaselineReadRecoveryContext(owned.metadata.instance_id,first.value,
        {intent.activity_id:owner},RecoveryBoundary.HOST_LOCAL_SETTLED,first.value.intent_event_id)
    calls=[]
    def clock():
        calls.append(1)
        if failure=='clock':
            raise OSError('clock unavailable')
        return True
    with pytest.raises((OSError,TypeError,ValueError)):
        reads.resume_baseline_read_call(owner,context,capture=capture,owned=owned,
            reader=owned,wall_us=clock)
    assert owner.diagnostic is not None and owner.unconfirmed_request is None
    assert owner.settlement_at is (None if failure=='clock' else True)
    assert reads.resume_baseline_read_call(owner,context,capture=capture,owned=owned,
        reader=owned,wall_us=clock) is None
    assert calls==[1] and owner.result is None
    assert owned.connection.execute('SELECT state FROM baseline_read_calls').fetchone()==(1,)


@pytest.mark.parametrize('changed',['instance','owner','intent'])
async def test_recovery_rejects_context_from_another_original_responsibility(owned,tmp_path,changed):
    from dataclasses import replace
    from camctl.capture import baseline_reads as reads
    from camctl.contracts.values import ConsistencyError
    from camctl.operations.recovery import RecoveryBoundary
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    owner=reads.PendingBaselineRead(intent,intent_view=first.value)
    context=reads.BaselineReadRecoveryContext(owned.metadata.instance_id,first.value,
        {intent.activity_id:owner},RecoveryBoundary.HOST_LOCAL_SETTLED,first.value.intent_event_id)
    if changed=='instance':
        context=replace(context,instance_id='f'*32)
    elif changed=='owner':
        context=replace(context,local_owners={intent.activity_id:reads.PendingBaselineRead(intent,intent_view=first.value)})
    else:
        bad=replace(first.value,request=replace(intent,occurred_at=_SCHEDULED+7))
        context=replace(context,intent_view=bad)
    with pytest.raises(ConsistencyError):
        reads.resume_baseline_read_call(owner,context,capture=capture,owned=owned,reader=owned,
            wall_us=lambda:pytest.fail('错责任不能取新的收场时刻'))
