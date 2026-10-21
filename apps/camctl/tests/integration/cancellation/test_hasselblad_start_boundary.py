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


class HeldTransport(_ManagedCaptureTransport):
    def __init__(self, *, fail=False):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail

    async def run(self, spec, stop):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            self.calls.append(spec)
            return RawToolOutcome(LocalExit(exit_code=1), b"", None, None, stderr=b"failed")
        return await super().run(spec, stop)


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
        row = owned.connection.execute("SELECT result_json FROM operation_attempts").fetchone()
        observations = parse_exact_json(row[0])["observations"]
        assert [value["type"] for value in observations] == ["hasselblad_start_entered"]
    finally:
        owned.connection.close()


async def test_returned_no_photo_effect_does_not_reopen_start_cancellation(tmp_path, monkeypatch):
    transport = HeldTransport(fail=True)
    cfg, owned, driver, runtime, clock = await world(tmp_path, transport)
    transport.release.set()
    monkeypatch.setattr("camctl.capture.host_photo._failure_after_return", lambda *args: True)
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert decide_cancel_eligibility(load_eligibility_facts(owned.connection, 1)) is CancelEligibility.REJECT_UNSUPPORTED
    finally:
        owned.connection.close()


def fix_cancel(owned, runtime):
    from camctl.bootstrap.flows import _resolve_and_fix
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from camctl.cancellation.models import StartCancelAction
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    origin, raw = owned.connection.execute('SELECT id,input_fields_json FROM actions WHERE type=6 ORDER BY id DESC LIMIT 1').fetchone()
    repository = CancellationRepository()
    assert repository.start_cancel_action(StartCancelAction(origin, runtime.wall_us()), new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
    # 装配后的生产入口须读取同一会话许可；本测试不直接指定取消模式。
    repository.start_entries = runtime.start_entries
    return origin, repository, _resolve_and_fix(owned, repository, origin, raw, runtime.wall_us)


async def test_photo_cancel_before_entry_prevents_late_start(tmp_path, monkeypatch):
    transport = HeldTransport()
    cfg, owned, driver, runtime, clock = await _world(tmp_path, cancel_action=True,
        driver_factory=lambda cfg: HasselbladDriver(cfg.devices,
            BoundDirectories(Path(cfg.paths.staging)), monotonic_ns=lambda: 1, transport=transport))
    original = runtime.grant
    def grant(action):
        result = original(action)
        fix_cancel(owned, runtime)
        assert runtime.action(1)['cancel_requested'] == 1
        return result
    monkeypatch.setattr(runtime, 'grant', grant)
    try:
        await capture_handler('camera_take_photo')(1, runtime)
        assert runtime.action(1)['status'] == 6
        assert owned.connection.execute('SELECT activity_state,occupancy_state,started_at FROM device_activities').fetchone() == (1,2,None)
        assert transport.calls == []
        assert not runtime.start_entries.pending_cancels
        result, = owned.connection.execute('SELECT result_json FROM operation_attempts').fetchone()
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
        origin, repository, ids = fix_cancel(owned, runtime)
        result = await asyncio.wait_for(apply_cancel(ApplyCancel(origin, ids), CancellationRuntime(
            owned, repository, TargetSettlement(owned, OutputsRepository(), repository,
                lambda _: 'unknown', runtime.wall_us), runtime.wall_us)), 1)
        assert result.items[0].status == 4
        assert runtime.action(1)['cancel_requested'] == 0
        assert not task.done()
        transport.release.set()
        await task
        assert runtime.action(1)['status'] == 3
        assert sum(spec.argv[3] == 'capture' for spec in transport.calls) == 1
    finally:
        transport.release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


async def submit_cancel(cfg, owned, runtime, target):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.devices.catalog import build_catalog, DriverDefinitions
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from ..capture.test_host_photo_flow import _input_time
    accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput('cancel-boundary.json', {
        'request_id': '2', 'created_at': _input_time(), 'name': '取消单张边界',
        'actions': [{'name':'取消','type':'cancel_task','params':{'target':target}}]}),
        build_catalog(cfg, DriverDefinitions({DRIVER_ID:driver_definition()})), CommandMode.RUN,
        runtime.wall_us()), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    return fix_cancel(owned, runtime)


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
        progress = await apply_cancel(ApplyCancel(origin, ids), CancellationRuntime(owned, repository,
            TargetSettlement(owned, OutputsRepository(), repository, lambda _: 'unknown', runtime.wall_us), runtime.wall_us))
        assert runtime.action(1)['cancel_requested'] == 0
        assert owned.connection.execute('SELECT status FROM cancel_items WHERE target_action_id=1').fetchone() == (4,)
        expected = int(address in ('plan','request'))
        assert runtime.action(2)['cancel_requested'] == expected
        assert owned.connection.execute('SELECT COUNT(*) FROM cancel_items').fetchone() == (1+expected,)
        if expected:
            assert owned.connection.execute('SELECT selection_basis FROM cancel_items WHERE target_action_id=2').fetchone() == (1,)
        transport.release.set()
        await task
        assert runtime.action(1)['status'] == 3
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
        assert runtime.action(1)['status'] == 4
        assert owned.connection.execute('SELECT activity_state,occupancy_state,started_at FROM device_activities').fetchone() == (1,2,None)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_driver_entered_with_zero_tools_keeps_entry_and_rejects_cancel(tmp_path, monkeypatch):
    transport = HeldTransport()
    cfg, owned, driver, runtime, clock = await world(tmp_path, transport)
    original = driver._request_config
    def config(request):
        value = original(request)
        clock[0] += 5_000_001
        return value
    monkeypatch.setattr(driver, '_request_config', config)
    monkeypatch.setattr('camctl.capture.host_photo._failure_after_return', lambda *args: True)
    try:
        await capture_handler('camera_take_photo')(1, runtime)
        assert transport.calls == []
        assert decide_cancel_eligibility(load_eligibility_facts(owned.connection,1)) is CancelEligibility.REJECT_UNSUPPORTED
        result, = owned.connection.execute('SELECT result_json FROM operation_attempts').fetchone()
        value = parse_exact_json(result)
        assert value['settlement']['evidence']['type'] == 'dispatch_prevented'
        assert value['diagnostics']['calls'] == []
        assert [o['type'] for o in value['observations']] == ['hasselblad_start_entered']
    finally:
        owned.connection.close()


async def test_cancel_before_any_start_does_not_fabricate_attempt(tmp_path):
    transport = HeldTransport()
    cfg, owned, driver, runtime, clock = await _world(tmp_path, cancel_action=True)
    try:
        fix_cancel(owned, runtime)
        assert runtime.action(1)['cancel_requested'] == 1
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == (0,)
        await capture_handler('camera_take_photo')(1, runtime)
        assert runtime.action(1)['status'] == 6
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
        origin, repository, ids = fix_cancel(owned,runtime)
        result = repository.apply_cancel_target(ApplyCancelTarget(ids[0],CancelApplyMode(mode),runtime.wall_us()),
            new_operation_key(),owned)
        assert result.kind is not DbOutcomeKind.COMPLETED
        assert runtime.action(1)['cancel_requested'] == 0
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
        assert runtime.action(1)['cancel_requested'] == 1
        assert owned.connection.execute('SELECT target_action_id,selection_basis FROM cancel_items ORDER BY id').fetchall() == [(1,1),(2,3)]
        assert len(ids) == 2
        await capture_handler('camera_take_photo')(1,runtime)
        assert runtime.action(1)['status'] == 6
    finally:
        owned.connection.close()


@pytest.mark.parametrize('phase', ['result','fixed','apply','committed_unknown'])
async def test_original_entry_cancel_transaction_rolls_back_or_reuses_exact_request(tmp_path, monkeypatch, phase):
    from camctl.persistence import transaction
    from camctl.persistence.repositories import cancellation as module
    from camctl.persistence.repositories import capture as capture_module
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from camctl.session.service import StateDbFailure
    from camctl.devices.start_entry import EntryPhase
    cfg, owned, driver, runtime, clock = await _world(tmp_path, cancel_action=True)
    runtime.grant(runtime.action(1))
    actual_fix = module.CancellationRepository.fix_cancel_targets
    actual_commit = module.commit_operation
    actual_validate = transaction.validate_event
    held = {}
    def remember(self, request, key, owner):
        held.update(repository=self, request=request, key=key, before=tuple(owner.connection.iterdump()))
        return actual_fix(self,request,key,owner)
    monkeypatch.setattr(module.CancellationRepository,'fix_cancel_targets',remember)
    if phase == 'committed_unknown':
        def unknown(command,key,owner):
            receipt = actual_commit(command,key,owner)
            if isinstance(command,module._FixPhotoEntriesCommand):
                return replace(receipt,kind='unknown',error=ConsistencyError('lost receipt'))
            return receipt
        monkeypatch.setattr(module,'commit_operation',unknown)
    else:
        wanted = {'result':capture_module._ATTEMPT_RESULT_EVENT,
            'fixed':module._TARGETS_FIXED_EVENT,'apply':module._CANCEL_CHANGED_EVENT}[phase]
        def fail(event,context):
            value = actual_validate(event,context)
            if event.event_type == wanted:
                raise ConsistencyError('injected transaction segment failure')
            return value
        monkeypatch.setattr(transaction,'validate_event',fail)
    try:
        with pytest.raises(StateDbFailure):
            fix_cancel(owned,runtime)
        assert runtime.start_entries.pending_cancels[2] == (held['request'],held['key'])
        permit = runtime.start_entries.get(owned.metadata.instance_id,1)
        assert permit.phase is EntryPhase.RESERVED
        with pytest.raises(ConsistencyError):
            permit.enter(permit.ticket)
        if phase != 'committed_unknown':
            assert tuple(owned.connection.iterdump()) == held['before']
            assert runtime.action(1)['cancel_requested'] == 0
        else:
            assert runtime.action(1)['cancel_requested'] == 1
        monkeypatch.setattr(transaction,'validate_event',actual_validate)
        monkeypatch.setattr(module,'commit_operation',actual_commit)
        result = actual_fix(held['repository'],held['request'],held['key'],owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert not runtime.start_entries.pending_cancels
        assert permit.phase is EntryPhase.BLOCKED
        frozen = tuple(owned.connection.iterdump())
        replay = actual_fix(held['repository'],held['request'],held['key'],owned)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert tuple(owned.connection.iterdump()) == frozen
        bad = replace(held['request'],occurred_at=held['request'].occurred_at+1)
        assert actual_fix(held['repository'],bad,held['key'],owned).kind is not DbOutcomeKind.COMPLETED
        block, = held['request'].entry_blocks
        wrong_database = replace(held['request'],entry_blocks=(replace(block,database_id='other-state-db'),))
        assert actual_fix(held['repository'],wrong_database,held['key'],owned).kind is not DbOutcomeKind.COMPLETED
        bad_block = replace(block,ticket=replace(block.ticket,target_id='999'))
        bad = replace(held['request'],entry_blocks=(bad_block,))
        assert actual_fix(held['repository'],bad,held['key'],owned).kind is not DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump()) == frozen
    finally:
        owned.connection.close()


async def test_atomic_cancel_of_all_pending_photo_sources_completes_original_plan(tmp_path):
    from ..capture.test_host_photo_flow import _input_time
    second = {'name':'第二张','type':'camera_take_photo','device_id':'cam-1',
        'scheduled_at':'2025-06-15 15:10:00','params':{'type':'hasselblad_photo',
            'capture_format':'raw','exposure':{'mode':'P','iso':100},
            'white_balance':{'mode':'auto'},'focus':{'mode':'MF'}},'policy':{'max_delay_ms':5000}}
    cfg, owned, driver, runtime, clock = await _world(tmp_path, extra_actions=[second], start_first=False)
    try:
        await submit_cancel(cfg,owned,runtime,{'plan_instance_id':'1'})
        assert owned.connection.execute('SELECT status FROM actions WHERE plan_id=1 ORDER BY id').fetchall() == [(6,),(6,)]
        assert owned.connection.execute('SELECT status FROM plans WHERE id=1').fetchone() == (3,)
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
    actual=driver._run_step
    async def after_focus(**kwargs):
        raw=await actual(**kwargs)
        if kwargs['name']=='focus':
            result=await asyncio.wait_for(_cancel_through_service(owned,runtime),1)
            assert result.items[0].status==4
            assert runtime.action(1)['cancel_requested']==0
            if remaining=='window': clock[0]+=5_000_001
            if remaining=='budget': ticks[0]+=181_000_000_000
        return raw
    monkeypatch.setattr(driver,'_run_step',after_focus)
    try:
        await capture_handler('camera_take_photo')(1,runtime)
        assert sum(call.argv[3]=='focus' for call in transport.calls)==1
        assert sum(call.argv[3]=='capture' for call in transport.calls)==int(remaining=='valid')
        assert runtime.action(1)['status']==(3 if remaining=='valid' else 4)
        saved_json,error_json=owned.connection.execute('SELECT result_json,error_json FROM operation_attempts').fetchone()
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
        original,repository,ids=fix_cancel(owned,runtime)
        assert runtime.action(1)['cancel_requested']==1
        origin,repository,other_ids=await submit_cancel(cfg,owned,runtime,{'action_instance_id':str(original)})
        result=await apply_cancel(ApplyCancel(origin,other_ids),CancellationRuntime(
            owned,repository,TargetSettlement(owned,OutputsRepository(),repository,
                lambda _:'unknown',runtime.wall_us),runtime.wall_us))
        assert result.items[0].status==3
        assert runtime.action(original)['status']==6
        assert owned.connection.execute('SELECT status,cancellation_effect FROM cancel_items WHERE id=?',(ids[0],)).fetchone()==(5,2)
        assert runtime.action(1)['cancel_requested']==1
        await capture_handler('camera_take_photo')(1,runtime)
        assert runtime.action(1)['status']==6 and not driver.requests
    finally:
        owned.connection.close()


@pytest.mark.parametrize('changed',['ticket','time','source','missing'])
async def test_independent_fixed_event_guard_rejects_false_entry_request(tmp_path,monkeypatch,changed):
    from copy import deepcopy
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import cancellation as module
    from camctl.session.service import StateDbFailure
    cfg,owned,driver,runtime,clock=await _world(tmp_path,cancel_action=True)
    runtime.grant(runtime.action(1))
    actual=module._FixPhotoEntriesCommand._request
    def wrong(self):
        request=deepcopy(actual(self))
        if changed=='ticket': request['blocks'][0]['ticket']['run_id']+=100
        if changed=='time': request['occurred_at']+=1
        if changed=='source': request['blocks'][0]['action_id']=self.command.action_id
        if changed=='missing': del request['blocks'][0]['ticket']
        return request
    monkeypatch.setattr(module._FixPhotoEntriesCommand,'_request',wrong)
    try:
        with pytest.raises(StateDbFailure): fix_cancel(owned,runtime)
        assert owned.connection.execute('SELECT result_json FROM operation_attempts').fetchone()==(None,)
        assert owned.connection.execute('SELECT COUNT(*) FROM cancel_items').fetchone()==(0,)
        assert runtime.action(1)['cancel_requested']==0
        permit=runtime.start_entries.get(owned.metadata.instance_id,1)
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
        permit=runtime.start_entries.get(owned.metadata.instance_id,1)
        assert permit.phase is EntryPhase.UNKNOWN and not driver.requests
        with pytest.raises(ConsistencyError): fix_cancel(owned,runtime)
        assert runtime.action(1)['cancel_requested']==0
        assert owned.connection.execute('SELECT COUNT(*) FROM cancel_items').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('missing',['lost','unknown'])
async def test_original_grant_without_ready_session_permit_never_enters_driver(tmp_path,monkeypatch,missing):
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.start_entry import EntryPhase
    cfg,owned,driver,runtime,clock=await _world(tmp_path)
    actual=runtime.grant
    def lose(action):
        granted=actual(action)
        identity=owned.metadata.instance_id,action['id']
        if missing=='lost': del runtime.start_entries.permits[identity]
        else: runtime.start_entries.permits[identity].phase=EntryPhase.UNKNOWN
        return granted
    monkeypatch.setattr(runtime,'grant',lose)
    try:
        with pytest.raises(ConsistencyError): await capture_handler('camera_take_photo')(1,runtime)
        assert not driver.requests
        assert owned.connection.execute('SELECT status,result_json FROM operation_attempts').fetchone()==(1,None)
    finally:
        owned.connection.close()
