"""真实相对调焦后原 START 的恢复保留未知与原结果，不重放。"""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.handlers import capture_handler
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.devices.drivers.registry import DriverRegistry
from camctl.host_files.models import BoundDirectories
from camctl.operations.process import RawToolOutcome, LocalExit
from camctl.operations.recovery import RecoveryBoundary
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcomeKind
from camctl.contracts.values import ConsistencyError
from camctl.contracts.enums import enum_for
from camctl.contracts.workflow_errors import registered_error
from camctl.contracts.json_values import parse_exact_json
from .test_host_photo_flow import _world, _entry, _ManagedCaptureTransport

pytestmark = pytest.mark.asyncio


class FocusTransport(_ManagedCaptureTransport):
    interrupt_focus = False

    async def run(self, spec, stop):
        if spec.argv[3] in ('focus','autofocus'):
            self.calls.append(spec)
            raw = RawToolOutcome(LocalExit(exit_code=0),b'',None,None,stderr=b'')
            self.outcomes.append(raw)
            if self.interrupt_focus:
                raise OSError('进程在实际相对移动返回后中断')
            return raw
        return await super().run(spec,stop)


@pytest_asyncio.fixture
async def databases():
    opened = []
    yield opened
    for database in opened:
        await database.close()


async def focus_world(tmp_path, databases):
    transport = FocusTransport()
    cfg,owned,driver,runtime,clock = await _world(tmp_path,
        photo_focus={'mode':'MF','relative_move':5},
        driver_factory=lambda cfg: HasselbladDriver(cfg.devices,
            BoundDirectories(Path(cfg.paths.staging)),transport=transport,monotonic_ns=lambda:1))
    runtime.close()
    runtime = await resume(cfg,owned,driver,clock,databases,
                           boundary=RecoveryBoundary.UNCONFIRMED)
    return transport,cfg,owned,driver,runtime,clock


async def resume(cfg,owned,driver,clock,databases,*,boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,horizon=True):
    maximum = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    factory = session_capture_assembly(devices=cfg.devices,drivers=DriverRegistry((_entry(driver),)),
        staging=Path(cfg.paths.staging),wait_config=lambda _:None,wall_us=lambda:clock[0],
        monotonic_ns=lambda:1,recovery_boundary=boundary,
        recovery_max_event_id=lambda: maximum if horizon is True else horizon)
    database = await BusinessDatabase.open(lambda: open_existing(
        Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()))
    databases.append(database)
    return await factory(database,'cam-1')


def start_attempt(owned):
    return owned.connection.execute(
        "SELECT a.run_id,a.attempt_no,a.intent_event_id,a.status,a.effect_state,a.result_json"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.responsibility_key='start/1'").fetchone()


@pytest.mark.parametrize('boundary',[RecoveryBoundary.HOST_LOCAL_SETTLED,RecoveryBoundary.HOST_POWER_CYCLE])
@pytest.mark.parametrize('stage',['after_focus','saved_return','complete_unknown'])
async def test_photo_restart_after_relative_focus_never_replays_start(tmp_path,monkeypatch,stage,boundary,databases):
    from camctl.capture import host_photo
    transport,cfg,owned,driver,runtime,clock = await focus_world(tmp_path,databases)
    actual_archive = host_photo.archive_capture_file
    actual_finish = runtime.capture.finish_capture
    if stage == 'after_focus':
        transport.interrupt_focus = True
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
        identity = start_attempt(owned)[:3]
        transport.interrupt_focus = False
        monkeypatch.setattr(host_photo,'archive_capture_file',actual_archive)
        owned.connection.close()
        owned = open_existing(Path(cfg.paths.state_db),DbOpenMode.EXISTING_RW,DbConfig())
        recovered = await resume(cfg,owned,driver,clock,databases,boundary=boundary)
        await capture_handler('camera_take_photo')(1,recovered)
        await capture_handler('camera_take_photo')(1,recovered)
        assert tuple(transport.calls) == original_calls
        attempt = start_attempt(owned)
        assert attempt[:3] == identity
        status,effect,result = attempt[3:]
        result = parse_exact_json(result)
        if stage == 'after_focus':
            assert status == int(enum_for("operation_attempts.status").UNKNOWN)
            assert effect == int(enum_for("operation_attempts.effect_state").UNKNOWN)
            assert result['settlement'] == {'basis':'assumed','evidence':{'type':'local_control_recovery','version':1,'data':{}}}
            assert result['observations'] == [] and 'diagnostics' not in result and 'call_info' not in result
            assert (await recovered.action(1))['status'] == 4
            assert (await recovered.action(1))['error_code'] == registered_error('capture_result_unconfirmed')['action_error_id']
            assert owned.connection.execute('SELECT started_at,activity_state,occupancy_state FROM device_activities').fetchone() == (None,1,1)
        else:
            assert [item['type'] for item in result['observations']] == ['photo_taken','hasselblad_start_entered']
            assert (await recovered.action(1))['status'] == 3
            assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (2,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('condition,reason',[('boundary','UNCONFIRMED_BOUNDARY'),('horizon','MISSING_HORIZON'),('outside','INTENT_OUTSIDE_HORIZON')])
async def test_missing_control_result_requires_original_reliable_recovery_boundary(tmp_path,condition,reason,databases):
    transport,cfg,owned,driver,runtime,clock = await focus_world(tmp_path,databases)
    try:
        transport.interrupt_focus = True
        with pytest.raises(OSError):
            await capture_handler('camera_take_photo')(1,runtime)
        calls = tuple(transport.calls)
        recovery = await resume(cfg,owned,driver,clock,databases,
            boundary=RecoveryBoundary.UNCONFIRMED if condition=='boundary' else RecoveryBoundary.HOST_LOCAL_SETTLED,
            horizon=None if condition=='horizon' else (0 if condition=='outside' else True))
        original = tuple(owned.connection.iterdump())
        await capture_handler('camera_take_photo')(1,recovery)
        assert recovery.last_recovery_diagnostic.reason.name == reason
        assert tuple(owned.connection.iterdump()) == original
        assert tuple(transport.calls) == calls
    finally:
        owned.connection.close()


async def test_recovery_does_not_end_control_still_executing_in_same_session(tmp_path,databases):
    class HeldFocus(FocusTransport):
        def __init__(self):
            super().__init__()
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def run(self, spec, stop):
            if spec.argv[3] == 'focus':
                self.entered.set()
                await self.release.wait()
            return await super().run(spec, stop)

    transport = HeldFocus()
    cfg,owned,driver,runtime,clock = await _world(tmp_path,driver_factory=lambda cfg:HasselbladDriver(
        cfg.devices,BoundDirectories(Path(cfg.paths.staging)),transport=transport,monotonic_ns=lambda:1),
        photo_focus={'mode':'MF','relative_move':5})
    runtime.close()
    runtime = await resume(cfg,owned,driver,clock,databases,boundary=RecoveryBoundary.UNCONFIRMED)
    task = asyncio.create_task(capture_handler('camera_take_photo')(1,runtime))
    try:
        await asyncio.wait_for(transport.entered.wait(),1)
        runtime.recovery_boundary = RecoveryBoundary.HOST_POWER_CYCLE
        runtime.recovery_max_event_id = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        await capture_handler('camera_take_photo')(1,runtime)
        assert runtime.last_recovery_diagnostic.reason.name == 'ACTIVE_LOCAL_CONTROL'
        assert start_attempt(owned)[3:] == (1,1,None)
        transport.release.set()
        await task
    finally:
        transport.release.set()
        await asyncio.gather(task,return_exceptions=True)
        owned.connection.close()


async def test_unknown_control_recovery_retains_original_request_key(tmp_path,monkeypatch,databases):
    transport,cfg,owned,driver,runtime,clock = await focus_world(tmp_path,databases)
    try:
        transport.interrupt_focus = True
        with pytest.raises(OSError):
            await capture_handler('camera_take_photo')(1,runtime)
        original_calls = tuple(transport.calls)
        identity = start_attempt(owned)[:3]
        recovery = await resume(cfg,owned,driver,clock,databases)
        actual = recovery.capture.finish_start_result
        calls=[]
        def unknown(finish,observation,key,owner,**kwargs):
            calls.append((finish,observation,key,kwargs))
            result=actual(finish,observation,key,owner,**kwargs)
            assert result.kind is DbOutcomeKind.COMPLETED,result.error
            return (replace(result,kind='unknown',error=OSError('原恢复结果提交回执未知'))
                    if len(calls) <= 2 else result)
        monkeypatch.setattr(recovery.capture,'finish_start_result',unknown)
        with pytest.raises(ConsistencyError):
            await capture_handler('camera_take_photo')(1,recovery)
        await capture_handler('camera_take_photo')(1,recovery)
        assert len(calls)==3 and all(call == calls[0] for call in calls)
        assert tuple(transport.calls) == original_calls
        assert start_attempt(owned)[:3] == identity
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/1'").fetchone() == (1,)
    finally:
        owned.connection.close()
