"""真实相对调焦后原 START 的恢复保留未知与原结果，不重放。"""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.handlers import capture_handler, _original_ticket
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.devices.drivers.registry import DriverRegistry
from camctl.host_files.models import BoundDirectories
from camctl.operations.process import RawToolOutcome, LocalExit
from camctl.operations.recovery import RecoveryBoundary
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.persistence.models import DbOutcomeKind
from camctl.contracts.values import ConsistencyError
from camctl.contracts.enums import enum_for
from camctl.contracts.workflow_errors import registered_error
from camctl.contracts.json_values import parse_exact_json
from .test_host_photo_flow import _world, _entry, _ManagedCaptureTransport

pytestmark = pytest.mark.asyncio


class FocusTransport(_ManagedCaptureTransport):
    async def run(self, spec, stop):
        if spec.argv[3] in ('focus','autofocus'):
            self.calls.append(spec)
            raw = RawToolOutcome(LocalExit(exit_code=0),b'',None,None,stderr=b'')
            self.outcomes.append(raw)
            return raw
        return await super().run(spec,stop)


async def focus_world(tmp_path):
    transport = FocusTransport()
    cfg,owned,driver,runtime,clock = await _world(tmp_path,
        photo_focus={'mode':'MF','relative_move':5},
        driver_factory=lambda cfg: HasselbladDriver(cfg.devices,
            BoundDirectories(Path(cfg.paths.staging)),transport=transport,monotonic_ns=lambda:1))
    return transport,cfg,owned,driver,runtime,clock


def resume(cfg,owned,driver,clock,*,boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,horizon=True):
    maximum = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    factory = session_capture_assembly(devices=cfg.devices,drivers=DriverRegistry((_entry(driver),)),
        staging=Path(cfg.paths.staging),wait_config=lambda _:None,wall_us=lambda:clock[0],
        monotonic_ns=lambda:1,recovery_boundary=boundary,
        recovery_max_event_id=lambda: maximum if horizon is True else horizon)
    return factory(owned,'cam-1')


@pytest.mark.parametrize('boundary',[RecoveryBoundary.HOST_LOCAL_SETTLED,RecoveryBoundary.HOST_POWER_CYCLE])
@pytest.mark.parametrize('stage',['after_focus','saved_return','complete_unknown'])
async def test_photo_restart_after_relative_focus_never_replays_start(tmp_path,monkeypatch,stage,boundary):
    from camctl.capture import host_photo
    transport,cfg,owned,driver,runtime,clock = await focus_world(tmp_path)
    actual_step = driver._run_step
    actual_archive = host_photo.archive_capture_file
    actual_finish = runtime.capture.finish_capture
    if stage == 'after_focus':
        async def interrupted(**kwargs):
            result = await actual_step(**kwargs)
            if kwargs['name'] == 'focus':
                raise OSError('进程在实际相对移动返回后中断')
            return result
        monkeypatch.setattr(driver,'_run_step',interrupted)
    elif stage == 'saved_return':
        async def interrupted(*args,**kwargs):
            raise OSError('原结果已保存，归档前中断')
        monkeypatch.setattr(host_photo,'archive_capture_file',interrupted)
    else:
        def interrupted(request,key,owner):
            saved = actual_finish(request,key,owner)
            assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
            return replace(saved,kind='unknown',error=OSError('完成提交回执未知'))
        monkeypatch.setattr(runtime.capture,'finish_capture',interrupted)
    try:
        with pytest.raises((OSError,ConsistencyError)):
            await capture_handler('camera_take_photo')(1,runtime)
        assert sum(call.argv[3]=='focus' for call in transport.calls) == 1
        original_calls = tuple(transport.calls)
        ticket = _original_ticket(runtime,'start/1','control')
        intent = owned.connection.execute('SELECT intent_event_id FROM operation_attempts').fetchone()[0]
        monkeypatch.setattr(driver,'_run_step',actual_step)
        monkeypatch.setattr(host_photo,'archive_capture_file',actual_archive)
        owned.connection.close()
        owned = open_existing(Path(cfg.paths.state_db),DbOpenMode.EXISTING_RW,DbConfig())
        recovered = resume(cfg,owned,driver,clock,boundary=boundary)
        await capture_handler('camera_take_photo')(1,recovered)
        await capture_handler('camera_take_photo')(1,recovered)
        assert tuple(transport.calls) == original_calls
        assert _original_ticket(recovered,'start/1','control') == ticket
        status,effect,result,event = owned.connection.execute(
            'SELECT status,effect_state,result_json,intent_event_id FROM operation_attempts').fetchone()
        result = parse_exact_json(result)
        assert event == intent
        if stage == 'after_focus':
            assert status == int(enum_for("operation_attempts.status").UNKNOWN)
            assert effect == int(enum_for("operation_attempts.effect_state").UNKNOWN)
            assert result['settlement'] == {'basis':'assumed','evidence':{'type':'local_control_recovery','version':1,'data':{}}}
            assert result['observations'] == [] and 'diagnostics' not in result and 'call_info' not in result
            assert recovered.action(1)['status'] == 4
            assert recovered.action(1)['error_code'] == registered_error('capture_result_unconfirmed')['action_error_id']
            assert owned.connection.execute('SELECT started_at,activity_state,occupancy_state FROM device_activities').fetchone() == (None,1,1)
        else:
            assert [item['type'] for item in result['observations']] == ['photo_taken','hasselblad_start_entered']
            assert recovered.action(1)['status'] == 3
            assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (2,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('condition,reason',[('boundary','UNCONFIRMED_BOUNDARY'),('horizon','MISSING_HORIZON'),('outside','INTENT_OUTSIDE_HORIZON')])
async def test_missing_control_result_requires_original_reliable_recovery_boundary(tmp_path,condition,reason):
    transport,cfg,owned,driver,runtime,clock = await focus_world(tmp_path)
    try:
        ticket,_ = runtime.grant(runtime.action(1))
        recovery = resume(cfg,owned,driver,clock,
            boundary=RecoveryBoundary.UNCONFIRMED if condition=='boundary' else RecoveryBoundary.HOST_LOCAL_SETTLED,
            horizon=None if condition=='horizon' else (0 if condition=='outside' else True))
        original = tuple(owned.connection.iterdump())
        assert recovery.recover_attempt(ticket) is False
        assert recovery.last_recovery_diagnostic.reason.name == reason
        assert tuple(owned.connection.iterdump()) == original
        assert not transport.calls
    finally:
        owned.connection.close()


async def test_recovery_does_not_end_control_still_executing_in_same_session(tmp_path):
    from ..cancellation.test_hasselblad_start_boundary import HeldTransport
    transport = HeldTransport()
    cfg,owned,driver,runtime,clock = await _world(tmp_path,driver_factory=lambda cfg:HasselbladDriver(
        cfg.devices,BoundDirectories(Path(cfg.paths.staging)),transport=transport,monotonic_ns=lambda:1))
    task = asyncio.create_task(capture_handler('camera_take_photo')(1,runtime))
    try:
        await asyncio.wait_for(transport.entered.wait(),1)
        runtime.recovery_boundary = RecoveryBoundary.HOST_POWER_CYCLE
        runtime.recovery_max_event_id = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        ticket = _original_ticket(runtime,'start/1','control')
        assert not runtime.recover_attempt(ticket)
        assert runtime.last_recovery_diagnostic.reason.name == 'ACTIVE_LOCAL_CONTROL'
        assert owned.connection.execute('SELECT status,result_json FROM operation_attempts').fetchone() == (1,None)
        transport.release.set()
        await task
    finally:
        transport.release.set()
        await asyncio.gather(task,return_exceptions=True)
        owned.connection.close()


async def test_unknown_control_recovery_retains_original_request_key(tmp_path,monkeypatch):
    transport,cfg,owned,driver,runtime,clock = await focus_world(tmp_path)
    try:
        ticket,_ = runtime.grant(runtime.action(1))
        recovery = resume(cfg,owned,driver,clock)
        actual = recovery.capture.finish_start_result
        calls=[]
        def unknown(finish,observation,key,owner,**kwargs):
            calls.append((finish,observation,key,kwargs))
            result=actual(finish,observation,key,owner,**kwargs)
            assert result.kind is DbOutcomeKind.COMPLETED,result.error
            return replace(result,kind='unknown',error=OSError('原恢复结果提交回执未知'))
        monkeypatch.setattr(recovery.capture,'finish_start_result',unknown)
        with pytest.raises(ConsistencyError):
            recovery.recover_attempt(ticket)
        pending = recovery.pending_start_results[(ticket.run_id,ticket.attempt_id)]
        monkeypatch.setattr(recovery.capture,'finish_start_result',actual)
        assert recovery.recover_attempt(ticket)
        assert len(calls)==2 and calls[0]==calls[1]
        assert calls[0][2]==pending.key
        assert not recovery.pending_start_results and not transport.calls
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == (1,)
    finally:
        owned.connection.close()
