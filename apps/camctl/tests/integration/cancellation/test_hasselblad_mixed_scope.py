"""HB 和其他相机的各自预览复用原资格，SQL 与完整原行判定一致。"""
import asyncio
from pathlib import Path

import pytest

from camctl.acceptance.ports import ParameterDefinition
from camctl.contracts.values import new_operation_key
from camctl.devices.catalog import build_catalog,DriverDefinitions
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID,driver_definition
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.host_files.models import BoundDirectories
from camctl.capture.handlers import capture_handler
from camctl.persistence.models import DbOutcomeKind
from ..capture.test_host_photo_flow import _world,_input_time
from .test_hasselblad_preview_fixed_boundary import close,accept_origin
from .test_hasselblad_start_boundary import HeldTransport
from .test_hasselblad_fixed_request_scope import fixed,context_for

pytestmark=pytest.mark.asyncio


class MixedCatalog:
    """真实静态目录端口：HB 原定义与无停止的普通单张参数。"""
    def __init__(self,cfg): self.base=build_catalog(cfg,DriverDefinitions({DRIVER_ID:driver_definition()}))
    def action_types(self): return self.base.action_types()
    def device_exists(self,identity): return identity=='cam-2' or self.base.device_exists(identity)
    def driver_id(self,identity): return 'generic-photo' if identity=='cam-2' else self.base.driver_id(identity)
    def device_supports(self,identity,kind): return kind=='camera_take_photo' if identity=='cam-2' else self.base.device_supports(identity,kind)
    def parameter_definition(self,identity,kind,parameter):
        if identity!='cam-2': return self.base.parameter_definition(identity,kind,parameter)
        if kind=='camera_take_photo' and parameter=='generic_photo':
            return ParameterDefinition(schema={'$schema':'https://json-schema.org/draft/2020-12/schema','type':'object','properties':{'type':{'const':'generic_photo'}},
                'required':['type'],'additionalProperties':False},defaults={},preview_supported=True)


@pytest.mark.parametrize('generic_state,allowed',[('unstarted',True),('confirming',False),('running_no_stop',False),('running_with_stop',True)])
@pytest.mark.parametrize('add_denied_hb_preview',[False,True])
async def test_each_mixed_camera_keeps_its_original_preview_permission(tmp_path,generic_state,allowed,add_denied_hb_preview):
    from camctl.persistence.repositories import cancellation as module
    from camctl.cancellation.rules import load_eligibility_facts,decide_cancel_eligibility,may_apply_cancel
    from camctl.persistence.repositories.scheduling import SchedulingRepository,ObserveWindowRequest,StartActionRequest
    from camctl.history.validators import EventValidationError
    generic={'name':'普通单张','type':'camera_take_photo','device_id':'cam-2','group':'g','scheduled_at':_input_time(),
        'params':{'type':'generic_photo'},'policy':{'max_delay_ms':5000}}
    previews=[{'name':name,'type':'obtain_action_outputs','scheduled_at':_input_time(),
        'params':{'source':{'action_name':source},'filter':'preview','purpose':'auto_preview'}}
        for name,source in [('HB预览','单张 RAW'),('普通预览','普通单张')]]
    transport=HeldTransport()
    cfg,owned,driver,runtime,clock=await _world(tmp_path,extra_actions=[generic,*previews],photo_group='g',
        catalog_factory=MixedCatalog,driver_factory=lambda cfg:HasselbladDriver(cfg.devices,
            BoundDirectories(Path(cfg.paths.staging)),transport=transport,monotonic_ns=lambda:1))
    task=None
    try:
        assert owned.connection.execute('SELECT id,status FROM actions ORDER BY id').fetchall()==[(1,2),(2,1),(3,1),(4,1)]
        if generic_state!='unstarted':
            scheduler=SchedulingRepository()
            for method,command in ((scheduler.observe_window,ObserveWindowRequest(2,runtime.wall_us(),runtime.wall_us())),
                                  (scheduler.start_action,StartActionRequest(2,runtime.wall_us(),runtime.wall_us()))):
                outcome=method(command,new_operation_key(),owned)
                assert outcome.kind is DbOutcomeKind.COMPLETED,outcome.error
            # 普通相机已有原派发事实；没有调用任何设备。
            owned.connection.execute('UPDATE device_activities SET dispatch_state=?,stop_supported=?,state_query_supported=? WHERE action_id=2',
                (1 if generic_state=='confirming' else 2,int(allowed),int(generic_state=='confirming')))
            owned.connection.commit()
            if generic_state=='confirming':
                from decimal import Decimal
                from .test_start_scope_facts import save_generic_no_effect_start
                activity_id=owned.connection.execute('SELECT id FROM device_activities WHERE action_id=2').fetchone()[0]
                save_generic_no_effect_start(owned,2,activity_id,runtime.wall_us())
                from camctl.operations.attempts import AttemptIntent,AttemptTarget,AttemptConfig,OperationKind,QueryPurpose
                from camctl.persistence.repositories.operations import OperationRepository
                result=OperationRepository().begin_attempt(AttemptIntent('query',2,OperationKind.QUERY_ACTIVITY,
                    AttemptTarget(activity_id=activity_id),QueryPurpose.START_CONFIRMATION,
                    AttemptConfig(2,Decimal(10),Decimal(1)),runtime.wall_us()),new_operation_key(),owned)
                assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert may_apply_cancel(decide_cancel_eligibility(load_eligibility_facts(owned.connection,2))) is allowed
        task=asyncio.create_task(capture_handler('camera_take_photo')(1,runtime))
        await asyncio.wait_for(transport.entered.wait(),1)
        origin,repository=accept_origin(cfg,owned,runtime,{'plan_instance_id':'1','group':'g'})
        entries=[(1,1,1),(2,1,1)]+([(4,2,1)] if allowed else [])
        if add_denied_hb_preview: entries.append((3,2,1))
        entries.sort()
        before=tuple(owned.connection.iterdump())
        event,context=context_for(owned,runtime,origin,entries)
        if add_denied_hb_preview:
            with pytest.raises(EventValidationError): module._cancel_targets_guard(event,context)
        else: module._cancel_targets_guard(event,context)
        result=repository.fix_cancel_targets(fixed(origin,runtime,entries),new_operation_key(),owned)
        assert (result.kind is DbOutcomeKind.COMPLETED) is (not add_denied_hb_preview),result.error
        if add_denied_hb_preview: assert tuple(owned.connection.iterdump())==before
        else:
            assert owned.connection.execute('SELECT target_action_id,selection_basis,cancellation_effect FROM cancel_items WHERE action_id=? ORDER BY target_action_id',(origin,)).fetchall()==entries
        assert not task.done() and runtime.action(1)['cancel_requested']==0
    finally:
        await close(owned,transport,task)
