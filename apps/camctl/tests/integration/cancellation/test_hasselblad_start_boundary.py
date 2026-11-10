"""真实 Hasselblad START 的入口事实与取消协作。"""

import asyncio
from dataclasses import replace
from pathlib import Path
from camctl.host_files.models import BoundDirectories
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.cancellation.rules import load_eligibility_facts, decide_cancel_eligibility, CancelEligibility
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.operations.process import RawToolOutcome, LocalExit
from camctl.contracts.json_values import parse_exact_json

from ..capture.test_host_photo_flow import _world, _ManagedCaptureTransport


pytestmark = pytest.mark.asyncio


from .hasselblad_support import (HeldPhotoTransport as HeldTransport, action as read_action,
    cancel, accept_origin, grant_unknown, interrupt_grant_delivery)


async def world(tmp_path, transport):
    return await _world(tmp_path, driver_factory=lambda cfg: HasselbladDriver(
        {"cam-1": cfg.devices["cam-1"]}, transport=transport,
        roots=BoundDirectories(staging=Path(cfg.paths.staging)),
        monotonic_ns=lambda: 9_000_000_000))


async def test_real_photo_start_saves_entry_even_when_first_tool_fails(tmp_path):
    transport = HeldTransport(fail=True)
    cfg, owned, driver, runtime, clock = await world(tmp_path, transport)
    transport.release.set()
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        row = owned.connection.execute("SELECT a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=1").fetchone()
        observations = parse_exact_json(row[0])["observations"]
        assert [value["type"] for value in observations] == ["hasselblad_start_entered"]
    finally:
        owned.connection.close()


async def test_returned_no_photo_effect_does_not_reopen_start_cancellation(tmp_path, monkeypatch):
    from .hasselblad_support import world as saved_world, close
    cfg,owned,driver,runtime,transport,task,repository=await saved_world(tmp_path,'no_effect',monkeypatch)
    try:
        assert decide_cancel_eligibility(load_eligibility_facts(owned.connection,1)) is CancelEligibility.REJECT_UNSUPPORTED
    finally:
        await close(owned,transport,task)


async def fix_cancel(cfg, owned, runtime):
    from camctl.persistence.repositories.cancellation import CancellationRepository
    origin, = owned.connection.execute('SELECT id FROM actions WHERE type=6 ORDER BY id DESC LIMIT 1').fetchone()
    await cancel(cfg,runtime)
    ids=tuple(row[0] for row in owned.connection.execute(
        'SELECT id FROM cancel_items WHERE action_id=? ORDER BY id',(origin,)))
    return origin,CancellationRepository(),ids


async def test_photo_cancel_before_entry_prevents_late_start(tmp_path, monkeypatch):
    transport = HeldTransport()
    cfg, owned, driver, runtime, clock = await _world(tmp_path, cancel_action=True,
        driver_factory=lambda cfg: HasselbladDriver(cfg.devices,
            BoundDirectories(Path(cfg.paths.staging)), monotonic_ns=lambda: 1, transport=transport))
    permit=await interrupt_grant_delivery(owned,runtime,monkeypatch)
    await fix_cancel(cfg,owned,runtime)
    assert read_action(owned,1)['cancel_requested']==1
    try:
        await capture_handler('camera_take_photo')(1, runtime)
        assert read_action(owned,1)['status'] == 6
        assert owned.connection.execute('SELECT activity_state,occupancy_state,started_at FROM device_activities').fetchone() == (1,2,None)
        assert all(spec.argv[3] != "capture" for spec in transport.calls)
        result, = owned.connection.execute('SELECT a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=1').fetchone()
        assert parse_exact_json(result)['settlement']['evidence']['type'] == 'start_call_prevented'
    finally:
        transport.release.set()
        owned.connection.close()


async def test_entered_start_rejection_finishes_without_waiting_for_photo(tmp_path):
    from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.persistence.repositories.outputs import OutputsRepository
    transport = HeldTransport()
    cfg, owned, driver, runtime, clock = await _world(tmp_path, cancel_action=True,
        driver_factory=lambda cfg: HasselbladDriver(cfg.devices,
            BoundDirectories(Path(cfg.paths.staging)), monotonic_ns=lambda: 1, transport=transport))
    task = asyncio.create_task(capture_handler('camera_take_photo')(1, runtime))
    try:
        await asyncio.wait_for(transport.entered.wait(), 1)
        origin, repository, ids = await fix_cancel(cfg, owned, runtime)
        result = await asyncio.wait_for(apply_cancel(ApplyCancel(origin, ids), CancellationRuntime(
            runtime.database, repository, TargetSettlement(runtime.database, OutputsRepository(), repository,
                lambda _: 'unknown', runtime.wall_us), runtime.wall_us)), 1)
        assert result.items[0].status == 4
        assert read_action(owned,1)['cancel_requested'] == 0
        assert not task.done()
        transport.release.set()
        await task
        assert read_action(owned,1)['status'] == 3
        assert sum(spec.argv[3] == 'capture' for spec in transport.calls) == 1
    finally:
        transport.release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


async def submit_cancel(cfg, owned, runtime, target):
    await accept_origin(cfg,owned,runtime,target)
    return await fix_cancel(cfg,owned,runtime)


@pytest.mark.parametrize('address', ['action','group','plan','request'])
async def test_photo_cancel_rejection_respects_preview_addressing(tmp_path, address):
    from ..capture.test_host_photo_flow import _input_time
    from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.persistence.repositories.outputs import OutputsRepository
    transport = HeldTransport()
    preview = {'name':'预览','type':'obtain_action_outputs','scheduled_at':_input_time(),
        'params':{'source':{'action_name':'单张 RAW'},'filter':'preview','purpose':'auto_preview'}}
    cfg, owned, driver, runtime, clock = await _world(tmp_path, extra_actions=[preview], photo_group='g',
        driver_factory=lambda cfg: HasselbladDriver(cfg.devices, BoundDirectories(Path(cfg.paths.staging)),
            monotonic_ns=lambda: 1, transport=transport))
    task = asyncio.create_task(capture_handler('camera_take_photo')(1, runtime))
    try:
        await asyncio.wait_for(transport.entered.wait(), 1)
        target = {'action': {'action_instance_id':'1'}, 'group':{'plan_instance_id':'1','group':'g'},
            'plan':{'plan_instance_id':'1'}, 'request':{'request_id':'1'}}[address]
        origin, repository, ids = await submit_cancel(cfg, owned, runtime, target)
        progress = await apply_cancel(ApplyCancel(origin, ids), CancellationRuntime(runtime.database, repository,
            TargetSettlement(runtime.database, OutputsRepository(), repository, lambda _: 'unknown', runtime.wall_us), runtime.wall_us))
        assert read_action(owned,1)['cancel_requested'] == 0
        assert owned.connection.execute('SELECT status FROM cancel_items WHERE target_action_id=1').fetchone() == (4,)
        expected = int(address in ('plan','request'))
        assert read_action(owned,2)['cancel_requested'] == expected
        assert owned.connection.execute('SELECT COUNT(*) FROM cancel_items').fetchone() == (1+expected,)
        if expected:
            assert owned.connection.execute('SELECT selection_basis FROM cancel_items WHERE target_action_id=2').fetchone() == (1,)
        transport.release.set()
        await task
        assert read_action(owned,1)['status'] == 3
    finally:
        transport.release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


async def test_setup_failure_releases_occupancy_without_claiming_activity_started(tmp_path):
    transport = HeldTransport(fail=True)
    cfg, owned, driver, runtime, clock = await world(tmp_path, transport)
    transport.release.set()
    try:
        await capture_handler('camera_take_photo')(1, runtime)
        assert read_action(owned,1)['status'] == 4
        assert owned.connection.execute('SELECT activity_state,occupancy_state,started_at FROM device_activities').fetchone() == (1,2,None)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_driver_entered_with_zero_tools_keeps_entry_and_rejects_cancel(tmp_path, monkeypatch):
    from .hasselblad_support import world as saved_world, close
    cfg,owned,driver,runtime,transport,task,repository=await saved_world(tmp_path,'zero_tools',monkeypatch)
    try:
        assert len(transport.calls)==8 and all(spec.argv[3]!='capture' for spec in transport.calls)
        assert decide_cancel_eligibility(load_eligibility_facts(owned.connection,1)) is CancelEligibility.REJECT_UNSUPPORTED
        result,=owned.connection.execute('SELECT a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=1').fetchone()
        value=parse_exact_json(result)
        assert value['settlement']['evidence']['type']=='dispatch_prevented'
        assert value['diagnostics']['calls']==[]
        assert [o['type'] for o in value['observations']]==['hasselblad_start_entered']
    finally:
        await close(owned,transport,task)


async def test_cancel_before_any_start_does_not_fabricate_attempt(tmp_path):
    transport = HeldTransport()
    cfg, owned, driver, runtime, clock = await _world(tmp_path, cancel_action=True)
    try:
        await fix_cancel(cfg, owned, runtime)
        assert read_action(owned,1)['cancel_requested'] == 1
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == (0,)
        await capture_handler('camera_take_photo')(1, runtime)
        assert read_action(owned,1)['status'] == 6
    finally:
        owned.connection.close()


@pytest.mark.parametrize('mode', ['pre_start','with_stop'])
async def test_direct_cancel_mode_cannot_bypass_entered_start(tmp_path, mode):
    from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    transport = HeldTransport()
    cfg, owned, driver, runtime, clock = await _world(tmp_path, cancel_action=True,
        driver_factory=lambda cfg: HasselbladDriver(cfg.devices, BoundDirectories(Path(cfg.paths.staging)),
            monotonic_ns=lambda: 1, transport=transport))
    task = asyncio.create_task(capture_handler('camera_take_photo')(1,runtime))
    try:
        await asyncio.wait_for(transport.entered.wait(),1)
        origin, repository, ids = await fix_cancel(cfg,owned,runtime)
        result = repository.apply_cancel_target(ApplyCancelTarget(ids[0],CancelApplyMode(mode),runtime.wall_us()),
            new_operation_key(),owned)
        assert result.kind is not DbOutcomeKind.COMPLETED
        assert read_action(owned,1)['cancel_requested'] == 0
    finally:
        transport.release.set()
        await asyncio.gather(task,return_exceptions=True)
        owned.connection.close()


async def test_preview_both_is_deduplicated_when_source_cancel_is_applied(tmp_path):
    from ..capture.test_host_photo_flow import _input_time
    preview = {'name':'预览','type':'obtain_action_outputs','scheduled_at':_input_time(),
        'params':{'source':{'action_name':'单张 RAW'},'filter':'preview','purpose':'auto_preview'}}
    cfg, owned, driver, runtime, clock = await _world(tmp_path, extra_actions=[preview])
    try:
        origin, repository, ids = await submit_cancel(cfg,owned,runtime,{'plan_instance_id':'1'})
        assert read_action(owned,1)['cancel_requested'] == 1
        assert owned.connection.execute('SELECT target_action_id,selection_basis FROM cancel_items ORDER BY id').fetchall() == [(1,1),(2,3)]
        assert len(ids) == 2
        await capture_handler('camera_take_photo')(1,runtime)
        assert read_action(owned,1)['status'] == 6
    finally:
        owned.connection.close()


@pytest.mark.parametrize('phase', ['result','fixed','apply','committed_unknown'])
async def test_original_entry_cancel_transaction_rolls_back_or_reuses_exact_request(tmp_path, monkeypatch, phase):
    import sqlite3
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from camctl.session.service import StateDbFailure
    cfg,owned,driver,runtime,clock=await _world(tmp_path,cancel_action=True)
    permit=await interrupt_grant_delivery(owned,runtime,monkeypatch)
    actual=CancellationRepository.fix_cancel_targets
    held={}
    def remember(repository,request,key,owner,**options):
        held.update(repository=repository,request=request,key=key,before=tuple(owner.connection.iterdump()))
        result=actual(repository,request,key,owner,**options)
        if phase=='committed_unknown' and result.kind is DbOutcomeKind.COMPLETED:
            return replace(result,kind=DbOutcomeKind.UNKNOWN,error=ConsistencyError('lost receipt'))
        return result
    monkeypatch.setattr(CancellationRepository,'fix_cancel_targets',remember)
    denied={'result':(sqlite3.SQLITE_UPDATE,'operation_attempts','status'),
        'fixed':(sqlite3.SQLITE_INSERT,'cancel_items',None),
        'apply':(sqlite3.SQLITE_UPDATE,'actions','cancel_requested')}
    if phase!='committed_unknown':
        operation,table,column=denied[phase]
        await runtime.database.read(lambda db: db.connection.set_authorizer(
            lambda op,name,col,*_: sqlite3.SQLITE_DENY
            if op==operation and name==table and (column is None or col==column) else sqlite3.SQLITE_OK))
    try:
        with pytest.raises(StateDbFailure):
            await fix_cancel(cfg,owned,runtime)
        with pytest.raises(ConsistencyError): permit.enter(permit.ticket)
        if phase!='committed_unknown':
            assert tuple(owned.connection.iterdump())==held['before']
            assert read_action(owned,1)['cancel_requested']==0
        else:
            assert read_action(owned,1)['cancel_requested']==1
        await runtime.database.read(lambda db: db.connection.set_authorizer(None))
        monkeypatch.setattr(CancellationRepository,'fix_cancel_targets',actual)
        await cancel(cfg,runtime)
        with pytest.raises(ConsistencyError): permit.enter(permit.ticket)
        assert read_action(owned,1)['cancel_requested']==1
        frozen=tuple(owned.connection.iterdump())
        replay=actual(held['repository'],held['request'],held['key'],owned)
        assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
        assert tuple(owned.connection.iterdump())==frozen
        bad=replace(held['request'],occurred_at=held['request'].occurred_at+1)
        assert actual(held['repository'],bad,held['key'],owned).kind is not DbOutcomeKind.COMPLETED
        block,=held['request'].entry_blocks
        wrong_database=replace(held['request'],entry_blocks=(replace(block,database_id='other-state-db'),))
        assert actual(held['repository'],wrong_database,held['key'],owned).kind is not DbOutcomeKind.COMPLETED
        bad_block=replace(block,ticket=replace(block.ticket,target_id='999'))
        assert actual(held['repository'],replace(held['request'],entry_blocks=(bad_block,)),held['key'],owned).kind is not DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump())==frozen
    finally:
        await runtime.database.read(lambda db: db.connection.set_authorizer(None))
        owned.connection.close()


@pytest.mark.parametrize('all_targets', [False, True], ids=['one-target', 'all-targets'])
async def test_atomic_cancel_of_all_pending_photo_sources_completes_original_plan(tmp_path, all_targets):
    from ..capture.test_host_photo_flow import _input_time
    second = {'name':'第二张','type':'camera_take_photo','device_id':'cam-1',
        'scheduled_at':'2025-06-15 15:10:00','params':{'type':'hasselblad_photo',
            'capture_format':'raw','exposure':{'mode':'P','iso':100},
            'white_balance':{'mode':'auto'},'focus':{'mode':'MF'}},'policy':{'max_delay_ms':5000}}
    cfg, owned, driver, runtime, clock = await _world(tmp_path, extra_actions=[second], start_first=False)
    try:
        plan_before = owned.connection.execute(
            'SELECT status,last_event_id FROM plans WHERE id=1').fetchone()
        assert plan_before[0] == 1
        target = {'plan_instance_id': '1'} if all_targets else {'action_instance_id': '1'}
        await submit_cancel(cfg,owned,runtime,target)
        assert owned.connection.execute('SELECT status FROM actions WHERE plan_id=1 ORDER BY id').fetchall() == [
            (6,), (6 if all_targets else 1,)]
        plan_after = owned.connection.execute(
            'SELECT status,last_event_id FROM plans WHERE id=1').fetchone()
        assert plan_after[0] == (3 if all_targets else plan_before[0])
        if all_targets:
            events = owned.connection.execute(
                'SELECT e.event_type,e.transaction_id FROM actions a'
                ' JOIN history_events e ON e.id=a.last_event_id'
                ' WHERE a.plan_id=1 ORDER BY a.id').fetchall()
            plan_event = owned.connection.execute(
                'SELECT event_type,transaction_id FROM history_events WHERE id=?',
                (plan_after[1],)).fetchone()
            assert [event[0] for event in events] == [25, 25]
            assert plan_event[0] == 9
            assert {event[1] for event in events} == {plan_event[1]}
        else:
            assert plan_after == plan_before
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('remaining',['valid','window','budget'])
async def test_cancel_after_actual_relative_focus_rejects_and_keeps_original_limits(tmp_path,monkeypatch,remaining):
    from ..capture.test_hasselblad_start_recovery import FocusTransport
    from ..capture.test_host_photo_flow import _cancel_through_service
    transport = FocusTransport()
    ticks=[1]
    cfg,owned,driver,runtime,clock = await _world(tmp_path,cancel_action=True,
        photo_focus={'mode':'MF','relative_move':5},driver_factory=lambda cfg:HasselbladDriver(
            cfg.devices,BoundDirectories(Path(cfg.paths.staging)),transport=transport,monotonic_ns=lambda:ticks[0]))
    actual=transport.run
    async def after_focus(spec,stop):
        raw=await actual(spec,stop)
        if spec.argv[3]=='focus':
            result=await asyncio.wait_for(_cancel_through_service(owned,runtime),1)
            assert result[0]==4
            assert read_action(owned,1)['cancel_requested']==0
            if remaining=='window': clock[0]+=5_000_001
            if remaining=='budget': ticks[0]+=181_000_000_000
        return raw
    monkeypatch.setattr(transport,'run',after_focus)
    try:
        await capture_handler('camera_take_photo')(1,runtime)
        assert sum(call.argv[3]=='focus' for call in transport.calls)==1
        assert sum(call.argv[3]=='capture' for call in transport.calls)==int(remaining=='valid')
        assert read_action(owned,1)['status']==(3 if remaining=='valid' else 4)
        saved_json,error_json=owned.connection.execute('SELECT a.result_json,a.error_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=1').fetchone()
        saved=parse_exact_json(saved_json)
        steps=(next(item['data']['steps'] for item in saved['observations'] if item['type']=='photo_taken')
            if remaining=='valid' else parse_exact_json(error_json)['details']['steps'])
        assert next(step for step in steps if step['name']=='focus')['lens_operation_returned'] is True
        assert any(item['type']=='hasselblad_start_entered' for item in saved['observations'])
    finally:
        owned.connection.close()


async def test_cancel_origin_itself_canceled_keeps_already_applied_source_responsibility(tmp_path):
    from camctl.cancellation.service import ApplyCancel,CancellationRuntime,apply_cancel
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.persistence.repositories.outputs import OutputsRepository
    cfg,owned,driver,runtime,clock=await _world(tmp_path,cancel_action=True)
    try:
        original,repository,ids=await fix_cancel(cfg,owned,runtime)
        assert read_action(owned,1)['cancel_requested']==1
        origin,repository,other_ids=await submit_cancel(cfg,owned,runtime,{'action_instance_id':str(original)})
        result=await apply_cancel(ApplyCancel(origin,other_ids),CancellationRuntime(
            runtime.database,repository,TargetSettlement(runtime.database,OutputsRepository(),repository,
                lambda _:'unknown',runtime.wall_us),runtime.wall_us))
        assert result.items[0].status==3
        assert read_action(owned,original)['status']==6
        assert owned.connection.execute('SELECT status,cancellation_effect FROM cancel_items WHERE id=?',(ids[0],)).fetchone()==(5,2)
        assert read_action(owned,1)['cancel_requested']==1
        await capture_handler('camera_take_photo')(1,runtime)
        assert read_action(owned,1)['status']==6 and not driver.requests
    finally:
        owned.connection.close()


@pytest.mark.parametrize('changed',['ticket','time','source','missing'])
async def test_independent_fixed_event_guard_rejects_false_entry_request(tmp_path,monkeypatch,changed):
    from copy import deepcopy
    from camctl.contracts.values import ConsistencyError,new_operation_key
    from camctl.persistence.models import DbOutcome,DbOutcomeKind
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from camctl.persistence.repositories.transactions import commit_operation
    from camctl.history.reads import PlanReadScope
    from camctl.session.service import StateDbFailure
    from ..support.output_planning import capture_operation,EventSequence
    cfg,owned,driver,runtime,clock=await _world(tmp_path,cancel_action=True)
    permit=await interrupt_grant_delivery(owned,runtime,monkeypatch)
    actual=CancellationRepository.fix_cancel_targets
    captured={}
    def propose(repository,request,key,owner,**options):
        captured['proposal']=capture_operation(owner,lambda copy: actual(repository,request,key,copy,**options))
        return DbOutcome(DbOutcomeKind.ROLLED_BACK,error=ConsistencyError('仅取得独立的正式提案'))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(CancellationRepository,'fix_cancel_targets',propose)
            with pytest.raises(StateDbFailure): await fix_cancel(cfg,owned,runtime)
        proposal=captured['proposal']
        events=[]
        for event in proposal.events:
            if event.event_type==4:
                header=deepcopy(event.evidence)
                request=header['photo_entry_request']
                if changed=='ticket': request['blocks'][0]['ticket']['run_id']+=100
                if changed=='time': request['occurred_at']+=1
                if changed=='source': request['blocks'][0]['action_id']=request['action_id']
                if changed=='missing': del request['blocks'][0]['ticket']
                event=replace(event,evidence=header)
            events.append(event)
        before=tuple(owned.connection.iterdump())
        receipt=commit_operation(EventSequence(tuple(events),proposal,
            plan_read_scope=PlanReadScope(1,128)),new_operation_key(),owned)
        assert receipt.kind!='completed',receipt
        assert tuple(owned.connection.iterdump())==before
        assert owned.connection.execute('SELECT result_json FROM operation_attempts').fetchone()==(None,)
        assert owned.connection.execute('SELECT COUNT(*) FROM cancel_items').fetchone()==(0,)
        assert read_action(owned,1)['cancel_requested']==0
        with pytest.raises(ConsistencyError): permit.enter(permit.ticket)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('committed',[False,True])
async def test_unknown_grant_never_authorizes_driver_or_guesses_cancel_success(tmp_path,monkeypatch,committed):
    from camctl.persistence.models import DbOutcome,DbOutcomeKind
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.start_entry import EntryPhase
    cfg,owned,driver,runtime,clock=await _world(tmp_path,cancel_action=True)
    actual=runtime.scheduling.grant_start
    def unknown(request,key,owner):
        if committed:
            result=actual(request,key,owner)
            assert result.kind is DbOutcomeKind.COMPLETED,result.error
        return DbOutcome(DbOutcomeKind.UNKNOWN,error=OSError('授予回执未知'))
    monkeypatch.setattr(runtime.scheduling,'grant_start',unknown)
    try:
        with pytest.raises(ConsistencyError): await capture_handler('camera_take_photo')(1,runtime)
        assert not driver.requests
        with pytest.raises(ConsistencyError): await fix_cancel(cfg,owned,runtime)
        assert read_action(owned,1)['cancel_requested']==0
        assert owned.connection.execute('SELECT COUNT(*) FROM cancel_items').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('missing',['lost','unknown'])
async def test_original_grant_without_ready_session_permit_never_enters_driver(tmp_path,monkeypatch,missing):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome,DbOutcomeKind
    from camctl.devices.drivers.registry import DriverRegistry
    from ..capture.test_host_photo_flow import _entry
    from ..support.capture import capture_test_session
    from ..session.support import assert_database_closed
    from camctl.persistence.repositories.capture_facts import load_start_facts
    async with capture_test_session() as session:
        cfg,owned,driver,runtime,clock=await _world(tmp_path)
        try:
            if missing=='lost':
                await interrupt_grant_delivery(owned,runtime,monkeypatch)
                original_database=runtime.database
                database=await session.restart_database(owned)
                await assert_database_closed(original_database)
                # 新 factory 使用新会话许可集合，不能沿旧会话内存授权设备。
                entry=_entry(driver)
                runtime=await session.runtime(database,driver=driver,evidence=entry.evidence,
                    devices=cfg.devices,drivers=DriverRegistry((entry,)),
                    staging=Path(cfg.paths.staging),wait_config=lambda _action:None,
                    wall_us=lambda:clock[0],monotonic_ns=lambda:9_000_000_000)
            else:
                actual=runtime.scheduling.grant_start
                def unknown(request,key,owner):
                    saved=actual(request,key,owner)
                    assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
                    return DbOutcome(DbOutcomeKind.UNKNOWN,error=OSError('授予回执未知'))
                with monkeypatch.context() as patch:
                    patch.setattr(runtime.scheduling,'grant_start',unknown)
                    with pytest.raises(ConsistencyError): await capture_handler('camera_take_photo')(1,runtime)
            original=owned.connection.execute('SELECT id,run_id,status,result_json FROM operation_attempts').fetchone()
            assert await capture_handler('camera_take_photo')(1,runtime) is None
            assert not driver.requests
            assert owned.connection.execute('SELECT id,run_id,status,result_json FROM operation_attempts').fetchone()==original
            assert original[2:]==(1,None)
            pending=load_start_facts(owned.connection,read_action(owned,1))
            assert pending.attempts_used==1 and not pending.not_started
            assert read_action(owned,1)['status']==2 and read_action(owned,1)['cancel_requested']==0
        finally:
            owned.connection.close()
