"""来源固定和开始初始化的实际有界事务与独立守卫。"""

from dataclasses import replace
import json

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.transactions import BoundedCommandPlan
from camctl.outputs.sources import SourceSpec

from .test_sources import _NOW, _seed_action, _six_form_environment
from ..support.output_source_cases import commit_source, source_observation, accept_body, SourceSequence, captured_source, OperationFailure, QueryProbe

pytestmark=pytest.mark.asyncio


def _request(owned, spec, *, count=0):
    raw = {name: value for name, value in vars(spec).items()
           if value is not None and value is not False}
    for name in ("action_instance_id", "plan_instance_id"):
        if name in raw:
            raw[name] = str(raw[name])
    owned.connection.execute("UPDATE actions SET input_fields_json=? WHERE id=30",
        (json.dumps({"params": {"source": raw}}),))
    for index in range(count):
        _seed_action(owned.connection, 100 + index, 2, action_type=2)
    owned.connection.commit()
    return outputs.ResolveSources(30, spec, _NOW)


async def test_source_resolution_streams_bounded_events_and_returns_finite_result(source_observation, tmp_path, monkeypatch):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, SourceSpec(plan_instance_id=2), count=140)
        result = outputs.OutputsRepository(4, storage=storage).resolve_sources(request, new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.member_count == 141
        assert result.value.selection_count == 141
        assert not hasattr(result.value, "member_action_ids")
        assert not hasattr(result.value, "selection_ids")
        bodies = owned.connection.execute("SELECT body_json FROM history_events WHERE id>1")
        assert max(len(json.loads(row[0])["rows"]) for row in bodies) <= 128
        assert owned.connection.execute("SELECT COUNT(*) FROM action_dependencies WHERE action_id=30").fetchone() == (141,)
        assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections").fetchone() == (141,)
    finally:
        owned.connection.close()


async def test_bounded_source_write_uses_its_business_storage_rules(source_observation, tmp_path):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.validation import BusinessStorage

    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, SourceSpec(plan_instance_id=2), count=3)
        rejection = EventValidationError("test-specific source rejection")
        def reject_source(event, context):
            raise rejection
        blocked = outputs.OutputsRepository(1, storage=BusinessStorage({"source_members": reject_source}))
        normal = outputs.OutputsRepository(1, storage=BusinessStorage())
        before = tuple(owned.connection.iterdump())
        key = new_operation_key()
        failed = blocked.resolve_sources(request, key, owned)
        assert failed.kind is DbOutcomeKind.ROLLED_BACK and failed.error is rejection
        assert tuple(owned.connection.iterdump()) == before
        saved = normal.resolve_sources(request, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert saved.value.fixed and saved.value.member_count == 4
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM action_dependencies WHERE action_id=30").fetchone() == (4,)
    finally:
        owned.connection.close()


async def test_source_resolution_rejects_request_different_from_saved_input(source_observation, tmp_path):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, SourceSpec(plan_instance_id=2))
        before = tuple(owned.connection.iterdump())
        result = outputs.OutputsRepository(storage=storage).resolve_sources(
            replace(request, spec=SourceSpec(current_plan=True)), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


async def test_obtain_initialization_streams_all_fixed_dependencies(source_observation, tmp_path, monkeypatch):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, SourceSpec(plan_instance_id=2), count=140)
        # 受理时固定的真实关系；开始命令必须共同初始化全部来源。
        owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=2,resolved_source_plan_id=2 WHERE id=30")
        members = (21, *range(100, 240))
        owned.connection.executemany("INSERT INTO action_dependencies(id,action_id,depends_on_action_id) VALUES(?,30,?)",
            enumerate(members, 1))
        owned.connection.commit()
        result = outputs.OutputsRepository(4, storage=storage).start_obtain_action(
            outputs.StartObtainAction(30, _NOW), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections").fetchone() == (141,)
        assert owned.connection.execute("SELECT status,execution_started FROM actions WHERE id=30").fetchone() == (2, 1)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("form,members,plan", [
    (SourceSpec(action_instance_id=21), (21,), 2),
    (SourceSpec(plan_instance_id=2, group="晨拍"), (21,), 2),
    (SourceSpec(action_name="录像A"), (11,), 1),
    (SourceSpec(group="上午"), (11,), 1),
    (SourceSpec(current_plan=True), (11,13,14), 1),
    (SourceSpec(plan_instance_id=2), (21,), 2),
])
@pytest.mark.parametrize("action_type", [4,5])
async def test_six_original_forms_publish_exact_members_without_delete_selections(source_observation, tmp_path, form, members, plan, action_type):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, form)
        owned.connection.execute("UPDATE actions SET type=? WHERE id=30", (action_type,))
        owned.connection.commit()
        result = outputs.OutputsRepository(1, storage=storage).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.member_count == len(members)
        assert result.value.source_plan_id == plan
        assert result.value.selection_count == (len(members) if action_type == 4 else 0)
        assert tuple(row[0] for row in owned.connection.execute("SELECT depends_on_action_id FROM action_dependencies WHERE action_id=30 ORDER BY id")) == members
    finally:
        owned.connection.close()



@pytest.mark.parametrize("finite", [False,True])
@pytest.mark.parametrize("mutation", ["missing_header","wrong_owner","wrong_member","wrong_branch","missing_final","replace_final","changed_time","fake_header_count"])
async def test_source_kernel_rejects_entire_malformed_group_in_both_entries(source_observation, tmp_path,monkeypatch,finite,mutation):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned,SourceSpec(plan_instance_id=2),count=3)
        before = tuple(owned.connection.iterdump())
        result=commit_source(owned,request,finite=finite,mutation=mutation, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, (mutation,result)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("finite", [False,True])
async def test_both_entries_accept_the_same_complete_canonical_source_group(source_observation, tmp_path,monkeypatch,finite):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned,SourceSpec(plan_instance_id=2),count=3)
        result=commit_source(owned,request,finite=finite, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.member_count == 4
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mutation", ["missing_context","fake_reads","fake_context","reset_and_replace_final"])
async def test_source_kernel_rejects_missing_or_fake_sql_port(source_observation, tmp_path,monkeypatch,mutation):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned,SourceSpec(plan_instance_id=2),count=3)
        before = tuple(owned.connection.iterdump())
        result=commit_source(owned,request,mutation=mutation, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("source_state", [1,2])
async def test_start_with_reliable_zero_initialization_preserves_source_state(source_observation, tmp_path,source_state):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned = _six_form_environment(tmp_path)
    try:
        _request(owned,SourceSpec(plan_instance_id=2))
        owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=?,resolved_source_plan_id=? WHERE id=30", (source_state,2 if source_state==2 else None))
        owned.connection.commit()
        result = outputs.OutputsRepository(1, storage=storage).start_obtain_action(outputs.StartObtainAction(30,_NOW),new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert owned.connection.execute("SELECT status,execution_started,source_resolution_state FROM actions WHERE id=30").fetchone() == (2,1,source_state)
        assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections").fetchone() == (0,)
    finally:
        owned.connection.close()


async def _real_environment(tmp_path, *, storage=None, cleanup=False, source=None, captures=3):
    """实际受理和实际开始建立完整主对象目录，供 H/原键核实。"""
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode, ProcessInput
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..persistence.test_runtime import _create_valid_database
    from ..acceptance.test_acceptance import Catalog, _plan_body
    target = tmp_path / "real.db"
    _create_valid_database(target)
    owned = open_existing(target,DbOpenMode.EXISTING_RW,DbConfig())
    shots = [{"name":f"shot-{i}","type":"camera_take_photo","device_id":"cam-1",
              "scheduled_at":"2026-01-15 09:00:00","params":{"type":"single_shot"},"policy":{"max_delay_ms":1000}}
             for i in range(captures)]
    async def accept(body):
        return (await accept_body(owned,body,occurred_at=_NOW)).plan_id
    source_plan = await accept(_plan_body(request_id="501",actions=shots))
    source = source or {"plan_instance_id":str(source_plan)}
    owner = {"name":"fetch","type":"delete_action_outputs" if cleanup else "obtain_action_outputs",
             "scheduled_at":"2026-01-15 09:00:00","params":{"source":source}}
    if not cleanup:
        owner["params"]["purpose"]="manual"
    owner_plan = await accept(_plan_body(request_id="502",actions=[owner]))
    owner_id = owned.connection.execute("SELECT id FROM actions WHERE plan_id=?",(owner_plan,)).fetchone()[0]
    repository = outputs.OutputsRepository(1, storage=storage)
    if cleanup:
        from camctl.outputs.cleanup_flow import StartCleanupAction
        result = repository.start_cleanup_action(StartCleanupAction(owner_id,_NOW),new_operation_key(),owned)
    else:
        result = repository.start_obtain_action(outputs.StartObtainAction(owner_id,_NOW),new_operation_key(),owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    from camctl.outputs.sources import read_source_spec
    request = outputs.ResolveSources(owner_id,read_source_spec(json.dumps({"params":{"source":source}})),_NOW)
    return target,owned,request


async def test_original_source_key_rechecks_original_h_after_later_work_and_new_ids(source_observation, tmp_path):
    observation, callbacks = source_observation
    storage = observation.storage
    _, owned, request = await _real_environment(tmp_path, storage=storage)
    repository = outputs.OutputsRepository(1, storage=storage)
    key = new_operation_key()
    try:
        first = repository.resolve_sources(request,key,owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        before = tuple(owned.connection.iterdump())
        probe=QueryProbe(owned.connection,lambda reads,columns,args:
            ('history_transactions','operation_key') in reads and ('history_events','body_json') in reads)
        replay=repository.resolve_sources(request,key,replace(owned,connection=probe))
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert replay.value == first.value
        assert tuple(owned.connection.iterdump()) == before
        assert probe.queries==[]
        wrong = repository.resolve_sources(replace(request,occurred_at=_NOW+1),key,owned)
        assert wrong.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start",[False,True])
@pytest.mark.parametrize("finite",[False,True])
@pytest.mark.parametrize("mutation",["missing_header","missing_final","replace_final","changed_time"])
async def test_initialization_and_failure_streams_reject_partial_groups(source_observation, tmp_path,monkeypatch,start,finite,mutation):
    observation, callbacks = source_observation
    storage = observation.storage
    _,owned=_six_form_environment(tmp_path)
    try:
        request=_request(owned,SourceSpec(action_instance_id=999))
        if start:
            owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=2,resolved_source_plan_id=2 WHERE id=30")
            owned.connection.execute("INSERT INTO action_dependencies VALUES(1,30,21)")
        else:
            owned.connection.execute("UPDATE actions SET status=3 WHERE id IN (11,12,13,14)")
        owned.connection.commit()
        before=tuple(owned.connection.iterdump())
        outcome=commit_source(owned,outputs.StartObtainAction(30,_NOW) if start else request,
            start=start,finite=finite,mutation=mutation, storage=storage, observation=observation)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("finite",[False,True])
async def test_canonical_failure_binds_original_error_and_parent_completion(source_observation, tmp_path,monkeypatch,finite):
    observation, callbacks = source_observation
    storage = observation.storage
    _,owned=_six_form_environment(tmp_path)
    try:
        request=_request(owned,SourceSpec(action_instance_id=999))
        owned.connection.execute("UPDATE actions SET status=3 WHERE id IN (11,12,13,14)")
        owned.connection.commit()
        outcome=commit_source(owned,request,finite=finite, storage=storage, observation=observation)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert not outcome.value.fixed
        assert outcome.value.error_details["reason"] == "action_not_found"
        assert owned.connection.execute("SELECT status FROM plans WHERE id=1").fetchone() == (3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM action_dependencies").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections").fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("source_state",[1,2])
@pytest.mark.parametrize("violation",["existing_selection","pending_with_dependency","cancel"])
async def test_start_rejects_nonempty_pending_already_initialized_or_canceled(source_observation, tmp_path,source_state,violation):
    observation, callbacks = source_observation
    storage = observation.storage
    _,owned=_six_form_environment(tmp_path)
    try:
        _request(owned,SourceSpec(plan_instance_id=2))
        owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=?,resolved_source_plan_id=? WHERE id=30", (source_state,2 if source_state==2 else None))
        if violation != "cancel":
            owned.connection.execute("INSERT INTO action_dependencies VALUES(1,30,21)")
            if violation == "existing_selection":
                owned.connection.execute("INSERT INTO obtain_source_selections VALUES(1,1,1,NULL,NULL)")
        else:
            owned.connection.execute("UPDATE actions SET status=2,execution_started=1,cancel_requested=1 WHERE id=30")
        owned.connection.commit()
        before=tuple(owned.connection.iterdump())
        result=outputs.OutputsRepository(1, storage=storage).start_obtain_action(outputs.StartObtainAction(30,_NOW),new_operation_key(),owned)
        if source_state==2 and violation=="pending_with_dependency":
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
        else:
            if violation=="cancel":
                assert result.kind is DbOutcomeKind.COMPLETED
                assert result.value.disposition is outputs.ObtainStartDisposition.REJECTED
            else:
                assert result.kind is DbOutcomeKind.ROLLED_BACK
            assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


async def test_bound_port_from_another_connection_is_rejected(source_observation, tmp_path,monkeypatch):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from camctl.persistence.event_reads import SqlEventReads
    path,owned=_six_form_environment(tmp_path)
    other=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    try:
        request=_request(owned,SourceSpec(plan_instance_id=2))
        before=tuple(owned.connection.iterdump())
        from camctl.persistence.repositories.transactions import commit_operation
        proposal,key,result=captured_source(owned,request, storage=storage)
        def foreign(context,*_):
            return replace(context,current_reads=SqlEventReads(other.connection,context.current_reads.scope))
        result=storage.commit_operation(SourceSequence(proposal,request,result,change_context=foreign),key,owned)
        assert result.kind=="rolled_back"
        assert tuple(owned.connection.iterdump())==before
    finally:
        other.connection.close()
        owned.connection.close()


@pytest.mark.parametrize("cleanup",[False,True])
@pytest.mark.parametrize("committed",[False,True])
async def test_unknown_commit_recovers_same_source_request_and_ids_after_reopen(source_observation, tmp_path,cleanup,committed):
    observation, callbacks = source_observation
    storage = observation.storage
    import sqlite3
    from camctl.persistence.runtime import DbConfig,DbOpenMode,OwnedConnection,open_existing
    path,owned,request=await _real_environment(tmp_path,cleanup=cleanup, storage=storage)
    key=new_operation_key()
    db=owned.connection
    class LoseCommit:
        @property
        def in_transaction(self):
            return db.in_transaction
        @property
        def total_changes(self):
            return db.total_changes
        def execute(self,sql,args=()):
            if sql=="COMMIT":
                if committed:
                    db.execute(sql,args)
                raise sqlite3.OperationalError("commit response lost")
            return db.execute(sql,args)
    result=outputs.OutputsRepository(1, storage=storage).resolve_sources(request,key,OwnedConnection(LoseCommit(),None))
    assert result.kind is DbOutcomeKind.UNKNOWN
    db.close()
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    try:
        before=tuple(owned.connection.execute("SELECT id,action_id,depends_on_action_id FROM action_dependencies"))
        recovered=outputs.OutputsRepository(1, storage=storage).resolve_sources(request,key,owned)
        assert recovered.kind is DbOutcomeKind.COMPLETED,recovered.error
        assert recovered.value.member_count==3
        after=tuple(owned.connection.execute("SELECT id,action_id,depends_on_action_id FROM action_dependencies"))
        if committed:
            assert after==before
        assert len(after)==3
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",(str(key),)).fetchone()==(1,)
    finally:
        owned.connection.close()


async def test_non_obtain_start_cannot_smuggle_obtain_rows(source_observation, tmp_path,monkeypatch):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.outputs.cleanup_flow import StartCleanupAction
    from camctl.persistence.repositories.transactions import row_change
    _,owned=_six_form_environment(tmp_path)
    try:
        _request(owned,SourceSpec(plan_instance_id=2))
        owned.connection.execute("UPDATE actions SET type=5,status=1,execution_started=0,execution_spec_json='{}',target_selection_state=1 WHERE id=30")
        owned.connection.execute("INSERT INTO action_dependencies VALUES(1,30,21)")
        owned.connection.commit()
        before=tuple(owned.connection.iterdump())
        from ..support.output_planning import capture_operation,EventSequence
        from camctl.persistence.repositories.transactions import commit_operation
        key=new_operation_key()
        proposal=capture_operation(owned,lambda copy:outputs.OutputsRepository(storage=storage).start_cleanup_action(
            StartCleanupAction(30,_NOW),key,copy))
        event=proposal.events[0]
        extra=row_change('obtain_source_selections',1,dict(dependency_id=1,status=1,error_code=None,error_details_json=None))
        events=(replace(event,rows=(*event.rows,extra)),*proposal.events[1:])
        result=storage.commit_operation(EventSequence(events,proposal),key,owned)
        assert result.kind=="rolled_back"
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


async def test_source_original_key_uses_original_ids_when_other_owners_add_rows(source_observation, tmp_path):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode,ProcessInput
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from ..acceptance.test_acceptance import Catalog,_plan_body
    _,owned,request=await _real_environment(tmp_path, storage=storage)
    try:
        repo=outputs.OutputsRepository(1, storage=storage)
        key=new_operation_key()
        first=repo.resolve_sources(request,key,owned)
        assert first.kind is DbOutcomeKind.COMPLETED,first.error
        original_ids=tuple(owned.connection.execute("SELECT id,dependency_id FROM obtain_source_selections ORDER BY id"))
        new={"name":"other","type":"obtain_action_outputs","scheduled_at":"2026-01-15 09:00:00","params":{"source":{"plan_instance_id":"1"},"purpose":"manual"}}
        accepted=await accept_body(owned,_plan_body(request_id="503",actions=[new]),occurred_at=_NOW+1)
        other=owned.connection.execute("SELECT id FROM actions WHERE plan_id=?",(accepted.plan_id,)).fetchone()[0]
        assert repo.start_obtain_action(outputs.StartObtainAction(other,_NOW+1),new_operation_key(),owned).kind is DbOutcomeKind.COMPLETED
        later=repo.resolve_sources(outputs.ResolveSources(other,SourceSpec(plan_instance_id=1),_NOW+1),new_operation_key(),owned)
        assert later.kind is DbOutcomeKind.COMPLETED,later.error
        before=tuple(owned.connection.iterdump())
        replay=repo.resolve_sources(request,key,owned)
        assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
        assert replay.value==first.value
        assert tuple(owned.connection.iterdump())==before
        assert tuple(owned.connection.execute("SELECT id,dependency_id FROM obtain_source_selections WHERE id<=3 ORDER BY id"))==original_ids
    finally:
        owned.connection.close()


@pytest.mark.parametrize("sql",[
    "DELETE FROM obtain_source_selections WHERE id=1",
    "UPDATE obtain_source_selections SET id=999 WHERE id=1",
    "UPDATE action_dependencies SET action_id=2 WHERE id=1",
])
async def test_original_source_key_requires_current_immutable_rows_to_remain(source_observation, tmp_path,sql):
    observation, callbacks = source_observation
    storage = observation.storage
    _,owned,request=await _real_environment(tmp_path, storage=storage)
    try:
        repository=outputs.OutputsRepository(1, storage=storage)
        key=new_operation_key()
        first=repository.resolve_sources(request,key,owned)
        assert first.kind is DbOutcomeKind.COMPLETED,first.error
        owned.connection.execute(sql)
        owned.connection.commit()
        before=tuple(owned.connection.iterdump())
        replay=repository.resolve_sources(request,key,owned)
        assert replay.kind is DbOutcomeKind.ROLLED_BACK,replay
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


async def test_original_start_key_survives_source_fixed_after_start(source_observation, tmp_path):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import OperationKey
    _,owned,request=await _real_environment(tmp_path, storage=storage)
    try:
        raw=owned.connection.execute("SELECT t.operation_key,e.occurred_at FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id WHERE e.event_type=5 AND json_extract(e.body_json,'$.evidence.obtain_initialization_request.action_id')=? ORDER BY e.id LIMIT 1",(request.action_id,)).fetchone()
        result=outputs.OutputsRepository(1, storage=storage).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        before=tuple(owned.connection.iterdump())
        replay=outputs.OutputsRepository(1, storage=storage).start_obtain_action(outputs.StartObtainAction(request.action_id,raw[1]),OperationKey(raw[0]),owned)
        assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed",[False,True])
async def test_fixed_initialization_unknown_commit_reuses_exact_original_selection_ids(source_observation, tmp_path,committed):
    observation, callbacks = source_observation
    storage = observation.storage
    import sqlite3
    from camctl.persistence.runtime import DbConfig,DbOpenMode,OwnedConnection,open_existing
    from ..acceptance.test_source_member_proof import _body
    from ..persistence.test_runtime import _create_valid_database
    path=tmp_path/"fixed.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    assert (await accept_body(owned,_body(),occurred_at=_NOW)).plan_id is not None
    db=owned.connection
    command=outputs.StartObtainAction(4,_NOW)
    key=new_operation_key()
    class LoseCommit:
        @property
        def in_transaction(self):
            return db.in_transaction
        @property
        def total_changes(self):
            return db.total_changes
        def execute(self,sql,args=()):
            if sql=="COMMIT":
                if committed:
                    db.execute(sql,args)
                raise sqlite3.OperationalError("start commit response lost")
            return db.execute(sql,args)
    first=outputs.OutputsRepository(1, storage=storage).start_obtain_action(command,key,OwnedConnection(LoseCommit(),None))
    assert first.kind is DbOutcomeKind.UNKNOWN
    db.close()
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    try:
        replay=outputs.OutputsRepository(1, storage=storage).start_obtain_action(command,key,owned)
        assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
        assert tuple(owned.connection.execute("SELECT id,dependency_id,status FROM obtain_source_selections ORDER BY id"))==((1,1,1),(2,2,1))
        before=tuple(owned.connection.iterdump())
        same=outputs.OutputsRepository(1, storage=storage).start_obtain_action(command,key,owned)
        assert same.kind is DbOutcomeKind.COMPLETED,same.error
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("scope_name",["SourceResolutionReadScope","ObtainInitializationReadScope"])
@pytest.mark.parametrize("table",["outputs","device_files","obtain_items","output_origins"])
async def test_source_scope_excludes_catalog_and_file_fact_ports(source_observation, tmp_path,scope_name,table):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    from camctl.history import reads as scopes
    from camctl.persistence.event_reads import SqlEventReads
    _,owned=_six_form_environment(tmp_path)
    try:
        owned.connection.execute("BEGIN IMMEDIATE")
        reads=SqlEventReads(owned.connection,getattr(scopes,scope_name)(1,4,30))
        with pytest.raises(ConsistencyError):
            reads.optional(table,1)
    finally:
        owned.connection.rollback()
        owned.connection.close()


@pytest.mark.parametrize("finite",[False,True])
async def test_both_entries_accept_complete_fixed_source_initialization(source_observation, tmp_path,monkeypatch,finite):
    observation, callbacks = source_observation
    storage = observation.storage
    _,owned=_six_form_environment(tmp_path)
    try:
        _request(owned,SourceSpec(plan_instance_id=2))
        owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=2,resolved_source_plan_id=2 WHERE id=30")
        owned.connection.execute("INSERT INTO action_dependencies VALUES(17,30,21)")
        owned.connection.commit()
        result=commit_source(owned,outputs.StartObtainAction(30,_NOW),start=True,finite=finite, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert tuple(owned.connection.execute("SELECT id,dependency_id,status FROM obtain_source_selections"))==((1,17,1),)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("prefix,skip",[
    ("INSERT INTO history_events",1),("INSERT INTO action_dependencies",1),
    ("INSERT INTO obtain_source_selections",1),("INSERT INTO entity_event_links",1),
    ("INSERT INTO report_entity_changes",0),("INSERT INTO entity_snapshot_progress",1),
    ("UPDATE actions",1),
])
async def test_sql_failure_rolls_back_rows_history_directories_and_snapshot_progress(source_observation, tmp_path,prefix,skip):
    observation, callbacks = source_observation
    storage = observation.storage
    import sqlite3
    from camctl.persistence.runtime import OwnedConnection
    _,owned=_six_form_environment(tmp_path)
    try:
        request=_request(owned,SourceSpec(plan_instance_id=2),count=3)
        before=tuple(owned.connection.iterdump())
        db=owned.connection
        parts=prefix.split()
        fault=OperationFailure(db,sqlite3.SQLITE_INSERT if parts[0]=='INSERT' else sqlite3.SQLITE_UPDATE,
            parts[-1],after=skip)
        result=outputs.OutputsRepository(1, storage=storage).resolve_sources(request,new_operation_key(),replace(owned,connection=fault))
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result
        assert result.error is fault.error
        assert fault.matches>=skip+1
        assert tuple(db.iterdump())==before
    finally:
        owned.connection.close()


async def test_source_write_and_original_key_keep_bounded_pages(source_observation, tmp_path):
    """不同规模下的真实写入及原键核实保持分页边界；进程组预算另行验证。"""
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.persistence.runtime import OwnedConnection
    records=[]
    for count in (20,200,600):
        directory=tmp_path/str(count)
        directory.mkdir()
        _,owned,request=await _real_environment(directory,captures=count, storage=storage)
        db=owned.connection
        stats={"max_fetch":0,"fetched":0,"sql_calls":0,"group_checks":0,"body_rows":0,"first_body_limit":None}
        txn=db.execute("SELECT MAX(id)+1 FROM history_transactions").fetchone()[0]
        class Cursor:
            def __init__(self,cursor,body=False):
                self.cursor=cursor
                self.body=body
            def __getattr__(self,name):
                return getattr(self.cursor,name)
            def fetchall(self):
                rows=self.cursor.fetchall()
                assert len(rows)<=128
                stats['max_fetch']=max(stats['max_fetch'],len(rows))
                stats['fetched']+=len(rows)
                if self.body:stats['body_rows']+=len(rows)
                return rows
            def fetchmany(self,size):
                assert size<=128
                rows=self.cursor.fetchmany(size)
                stats["max_fetch"]=max(stats["max_fetch"],len(rows))
                stats["fetched"]+=len(rows)
                if self.body:
                    stats["body_rows"]+=len(rows)
                return rows
            def fetchone(self):
                row=self.cursor.fetchone()
                stats["fetched"]+=int(row is not None)
                return row
        class Measured:
            def __getattr__(self,name):
                return getattr(db,name)
            def execute(self,sql,args=()):
                from ..support.outputs import observed_query
                stats['sql_calls']+=1
                cursor,columns,reads=observed_query(db,sql,args)
                if ('history_events','transaction_id') in reads and args==(txn,) and len(columns)==3:
                    stats['group_checks']+=1
                body=('history_events','body_json') in reads and len(args)==4 and 'event_type' in columns
                if body and stats['first_body_limit'] is None:stats['first_body_limit']=args[-1]
                if ('action_dependencies','id') in reads and any(table=='entity_event_links' for table,column in reads):
                    if 'original_id_query' not in stats:stats['original_id_query']=(sql,args)
                return Cursor(cursor,body)
        try:
            measured=OwnedConnection(Measured(),None)
            repository=outputs.OutputsRepository(4, storage=storage)
            key=new_operation_key()
            written=repository.resolve_sources(request,key,measured)
            assert written.kind is DbOutcomeKind.COMPLETED,written.error
            reused=repository.resolve_sources(request,key,measured)
            assert reused.kind is DbOutcomeKind.COMPLETED,reused.error
            assert reused.value==written.value
            assert stats["max_fetch"]<=128 and stats["group_checks"]<=3
            event_count=db.execute("SELECT last_event_id-first_event_id+1 FROM history_transactions WHERE operation_key=?",(str(key),)).fetchone()[0]
            assert stats["first_body_limit"] == 1
            assert stats["body_rows"] == event_count + 1  # 头窥读一个，正文完整一遍。
            query,parameters=stats.pop('original_id_query')
            explain=[row[-1] for row in db.execute('EXPLAIN QUERY PLAN '+query,parameters)]
            assert any("entity_event_links" in row and "INDEX" in row for row in explain),explain
            records.append({"N":count,"B":4,"original_id_plan":explain,**stats})
        finally:
            db.close()
    print("P5_SOURCE_BATCHES "+json.dumps(records))


async def test_bounded_context_cannot_carry_the_whole_source_plan(source_observation, tmp_path,monkeypatch):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.persistence.repositories.transactions import row_facts
    _,owned=_six_form_environment(tmp_path)
    try:
        request=_request(owned,SourceSpec(plan_instance_id=2),count=20)
        before=tuple(owned.connection.iterdump())
        result=commit_source(owned,request,mutation='whole_source_plan', storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("scope_name", ["SourceResolutionReadScope", "ObtainInitializationReadScope"])
@pytest.mark.parametrize("table,identity", [("actions", 21), ("plans", 1), ("action_dependencies", 1), ("obtain_source_selections", 1)])
async def test_unbound_source_scope_cannot_grant_point_fact_roles(source_observation, tmp_path, scope_name, table, identity):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    from camctl.history import reads as scopes
    from camctl.persistence.event_reads import SqlEventReads
    _, owned = _six_form_environment(tmp_path)
    try:
        owned.connection.execute("BEGIN IMMEDIATE")
        reads = SqlEventReads(owned.connection, getattr(scopes, scope_name)(1, 1, 30))
        with pytest.raises(ConsistencyError):
            reads.optional(table, identity)
    finally:
        owned.connection.rollback()
        owned.connection.close()


@pytest.mark.parametrize("start", [False, True])
async def test_bound_source_scope_only_exposes_actual_canonical_page_roles(source_observation, tmp_path, monkeypatch, start):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    _, owned, request = await _real_environment(tmp_path, storage=storage)
    try:
        # 另一 owner 的真实已提交依赖/selection 不能借同表点读取进入当前角色。
        from camctl.acceptance.input import ParsedInput
        from camctl.acceptance.service import CommandMode, ProcessInput
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        from ..acceptance.test_acceptance import Catalog, _plan_body
        raw={"name":"other","type":"obtain_action_outputs","scheduled_at":"2026-01-15 09:00:00","params":{"source":{"plan_instance_id":"1"},"purpose":"manual"}}
        accepted=await accept_body(owned,_plan_body(request_id="503",actions=[raw]),occurred_at=_NOW)
        other=owned.connection.execute("SELECT id FROM actions WHERE plan_id=?",(accepted.plan_id,)).fetchone()[0]
        repo=outputs.OutputsRepository(1, storage=storage)
        assert repo.start_obtain_action(outputs.StartObtainAction(other,_NOW),new_operation_key(),owned).kind is DbOutcomeKind.COMPLETED
        assert repo.resolve_sources(outputs.ResolveSources(other,SourceSpec(plan_instance_id=1),_NOW),new_operation_key(),owned).kind is DbOutcomeKind.COMPLETED
        if start:
            owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=2,resolved_source_plan_id=1 WHERE id=?", (request.action_id,))
            owned.connection.executemany("INSERT INTO action_dependencies VALUES(?,?,?)", [(17,request.action_id,1),(18,request.action_id,2)])
            owned.connection.commit()
        checked = []
        def inspect(db, event, context, reads, q):
            if checked:
                return
            checked.append(event.event_id)
            assert reads.required("actions", request.action_id)["id"] == request.action_id
            assert reads.required("plans", q["action"]["plan_id"])["id"] == q["action"]["plan_id"]
            assert reads.required("actions", 1)["id"] == 1
            for table in ('outputs','device_files','obtain_items','output_origins'):
                with pytest.raises(ConsistencyError):reads.optional(table,1)
            # 合法 source plan 中的下一批，以及任意已有关系，都不是当前角色。
            for table, identity in (("actions",2),("actions",3),("actions",other),("plans",1),("action_dependencies",1),("action_dependencies",18),("obtain_source_selections",1)):
                with pytest.raises(ConsistencyError):
                    reads.optional(table, identity)
            for page in reads.iter_members("actions","plan_id",q["action"]["plan_id"]):
                assert all(row["plan_id"] == q["action"]["plan_id"] for _, row in page)
            if start:
                assert reads.required("action_dependencies",17)["depends_on_action_id"] == 1
            else:
                assert reads.optional("action_dependencies",q["dependency_first"]) is None
        callbacks["inspect"] = inspect
        observation.active = True
        repository = outputs.OutputsRepository(1, storage=storage)
        result = repository.start_obtain_action(outputs.StartObtainAction(request.action_id,_NOW),new_operation_key(),owned) if start else repository.resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert checked
    finally:
        owned.connection.close()




async def _aggregate_role_world(tmp_path, start, *, storage):
    """真实三计划：目标 owner=8/main plan=3，源页与数值碰撞 action 分开。"""
    from ..acceptance.test_source_member_proof import _body
    _, owned, _ = await _real_environment(tmp_path, storage=storage)
    body = _body()
    if not start:
        body["actions"][-1]["params"]["source"] = {"plan_instance_id":"1"}
        local = _body()["actions"][-1]
        local["name"] = "local-fetch"
        body["actions"].append(local)
    accepted = await accept_body(owned,body,occurred_at=_NOW)
    assert accepted.plan_id == 3
    request = outputs.StartObtainAction(8,_NOW) if start else outputs.ResolveSources(8,SourceSpec(plan_instance_id=1),_NOW)
    if not start:
        local_started = outputs.OutputsRepository(1, storage=storage).start_obtain_action(outputs.StartObtainAction(9,_NOW),new_operation_key(),owned)
        assert local_started.kind is DbOutcomeKind.COMPLETED, local_started.error
        started = outputs.OutputsRepository(1, storage=storage).start_obtain_action(outputs.StartObtainAction(8,_NOW),new_operation_key(),owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
    return owned, request


def _run_aggregate_role_command(owned, request, monkeypatch, *, storage, observation, start, finite, batch_size=1):
    return commit_source(owned,request,start=start,finite=finite,batch_size=batch_size, storage=storage, observation=observation)


@pytest.mark.parametrize("start", [False,True])
@pytest.mark.parametrize("finite", [False,True])
@pytest.mark.parametrize("table", ["plans","action_dependencies","obtain_source_selections"])
async def test_point_iteration_cannot_relabel_its_numeric_ids_as_actions(source_observation, tmp_path,monkeypatch,start,finite,table):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    owned,request = await _aggregate_role_world(tmp_path,start, storage=storage)
    checked = []
    try:
        def inspect(db,event,context,reads,q):
            if checked:
                return
            checked.append(event.event_id)
            identity = 3 if table == "plans" or not start else 1
            with pytest.raises(ConsistencyError):
                reads.optional("actions",identity)
            iterator = reads.iter_members(table,"id",identity)
            for page in iterator:
                assert all(key == identity for key,_ in page)
                with pytest.raises(ConsistencyError):
                    reads.optional("actions",identity)
            with pytest.raises(ConsistencyError):
                reads.optional("actions",identity)
            assert reads.required("actions",8)["plan_id"] == 3
            assert reads.required("plans",3)["id"] == 3
        callbacks["inspect"] = inspect
        observation.active = True
        result = _run_aggregate_role_command(owned,request,monkeypatch,start=start,finite=finite, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start", [False,True])
@pytest.mark.parametrize("finite", [False,True])
@pytest.mark.parametrize("failure", ["optional","sql"])
async def test_aggregate_role_is_revoked_when_page_assembly_fails(source_observation, tmp_path,monkeypatch,start,finite,failure):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    from ..support.output_source_cases import QueryProbe
    import sqlite3
    owned,request=await _aggregate_role_world(tmp_path,start, storage=storage)
    target=6 if start else 5
    fault=sqlite3.DatabaseError('page SQL failed') if failure=='sql' else RuntimeError('page assembly failed')
    active={'value':False}
    probe=QueryProbe(owned.connection,lambda reads,columns,args: active['value'] and args==(target,)
        and any(table=='actions' for table,column in reads),
        'execute' if failure=='sql' else 'fetch',error=fault)
    owned=replace(owned,connection=probe)
    checked=[]
    try:
        def inspect(db,event,context,reads,q):
            if checked:return
            checked.append(event.event_id)
            active['value']=True
            iterator=reads.iter_members('actions','plan_id',3,columns=reads.SOURCE_COLUMNS)
            try:
                with pytest.raises(type(fault)) as caught:
                    while True:next(iterator)
                assert caught.value is fault
            finally:active['value']=False
            with pytest.raises(ConsistencyError):reads.optional('actions',target)
            iterator.close()
            assert reads.required('actions',8)['plan_id']==3
        callbacks["inspect"] = inspect
        observation.active = True
        result=_run_aggregate_role_command(owned,request,monkeypatch,start=start,finite=finite, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
        for cursor in probe.cursors:cursor.close.assert_called_once_with()
    finally:owned.connection.close()


@pytest.mark.parametrize("start", [False,True])
@pytest.mark.parametrize("finite", [False,True])
@pytest.mark.parametrize("exit_kind", ["exhaust","close","throw","consumer_error"])
async def test_real_plan_aggregate_page_role_ends_with_its_iterator(source_observation, tmp_path,monkeypatch,start,finite,exit_kind):
    observation, callbacks = source_observation
    storage = observation.storage
    from contextlib import closing
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    owned,request = await _aggregate_role_world(tmp_path,start, storage=storage)
    checked = []
    try:
        def inspect(db,event,context,reads,q):
            if checked:
                return
            checked.append(event.event_id)
            target = 6 if start else 5
            with pytest.raises(ConsistencyError):
                reads.optional("actions",target)
            assert list(reads.iter_members('plans','id',3))
            with pytest.raises(ConsistencyError):
                reads.optional('actions',target)
            with pytest.raises(ConsistencyError):
                next(reads.iter_members('actions','plan_id',1))
            iterator = reads.iter_members("actions","plan_id",3)
            page = next(iterator)
            while target not in {key for key,_ in page}:
                page = next(iterator)
            assert reads.required("actions",target)["plan_id"] == 3
            with pytest.raises(ConsistencyError):
                reads.optional("actions",3)
            if exit_kind == "exhaust":
                for _ in iterator:
                    pass
            elif exit_kind == "close":
                iterator.close()
            elif exit_kind == "throw":
                with pytest.raises(RuntimeError):
                    iterator.throw(RuntimeError("consumer stopped"))
            else:
                with pytest.raises(RuntimeError):
                    with closing(iterator):
                        raise RuntimeError("consumer stopped")
            with pytest.raises(ConsistencyError):
                reads.optional("actions",target)
            assert reads.required("actions",8)["plan_id"] == 3
        callbacks["inspect"] = inspect
        observation.active = True
        result = _run_aggregate_role_command(owned,request,monkeypatch,start=start,finite=finite, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("finite", [False,True])
async def test_plan_aggregate_keeps_real_accepted_prefix_fields(source_observation, tmp_path,monkeypatch,finite):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.persistence.repositories import source_transactions as streams
    owned,request = await _aggregate_role_world(tmp_path,True, storage=storage)
    checked = []
    try:
        def inspect(db,event,context,reads,q):
            if event.event_id != context.transaction.last_event_id:
                return
            # START owner 已经是实际已接受前序；finite 尚未写 SQL，Bounded 已写。
            assert any(row.table == "plans" and row.row_id == 3 for row in event.rows)
            rows = [facts for page in reads.iter_members("actions","plan_id",3) for _,facts in page]
            owner = next(row for row in rows if row["id"] == 8)
            assert (owner["status"],owner["execution_started"]) == (2,1)
            checked.append(event.event_id)
        callbacks["inspect"] = inspect
        observation.active = True
        result = _run_aggregate_role_command(owned,request,monkeypatch,start=True,finite=finite, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize("finite", [False, True])
@pytest.mark.parametrize("variant", ["same", "columns", "limit", "later_page"])
@pytest.mark.parametrize("end_first", ["a", "b"])
async def test_source_aggregate_overlap_rejects_before_sql_without_clearing_owner(source_observation, tmp_path, monkeypatch, start, finite, variant, end_first):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    owned, request = await _aggregate_role_world(tmp_path, start, storage=storage)
    checked = []
    try:
        def inspect(db, event, context, reads, q):
            if checked:
                return
            checked.append(event.event_id)
            target = (7 if variant == "later_page" else 5) if start else 5
            sql = []
            db.set_trace_callback(sql.append)
            try:
                a = reads.iter_members("actions", "plan_id", 3)
                c = reads.iter_members("actions", "plan_id", 3)
                b = reads.iter_members("actions", "plan_id", 3,
                    columns=reads.SOURCE_COLUMNS if variant == "columns" else None,
                    limit=1 if variant == "limit" else None)
                assert sql == []
                # 未开始的退出不得获得或撤销页权限。
                unused = reads.iter_members("actions", "plan_id", 3)
                unused.close()
                if not start or target == 7:
                    with pytest.raises(ConsistencyError):
                        reads.optional("actions", target)
                page = next(a)
                while target not in dict(page):
                    page = next(a)
                if variant == "later_page" and not start:
                    page = next(a)
                    target = page[0][0]
                before = len(sql)
                with pytest.raises(ConsistencyError):
                    next(b)
                assert len(sql) == before
                assert reads.required("actions", target)["plan_id"] == 3
                if end_first == "a":
                    a.close()
                    b.close()
                else:
                    b.close()
                    assert reads.required("actions", target)["plan_id"] == 3
                    a.close()
                with pytest.raises(RuntimeError):
                    b.throw(RuntimeError("late rejected iterator"))
                if not start or target == 7:
                    with pytest.raises(ConsistencyError):
                        reads.optional("actions", target)
                else:
                    assert reads.required("actions", target)["plan_id"] == 3
                # 尚未推进的 C 在 A 之前已创建；A 结束后仍可正常开始。
                page = next(c)
                while 7 not in dict(page):
                    page = next(c)
                a.close()
                assert reads.required("actions", 7)["plan_id"] == 3
                c.close()
                with pytest.raises(ConsistencyError):
                    reads.optional("actions", 7)
            finally:
                db.set_trace_callback(None)
        callbacks["inspect"] = inspect
        observation.active = True
        result = _run_aggregate_role_command(owned, request, monkeypatch, start=start, finite=finite, batch_size=2, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize("finite", [False, True])
@pytest.mark.parametrize("new_page", ["none", "active", "ended"])
@pytest.mark.parametrize("old_exit", ["next", "close", "throw"])
async def test_source_events_keep_independent_ports_until_transaction_ends(source_observation, tmp_path, monkeypatch, start, finite, new_page, old_exit):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    owned,request=await _aggregate_role_world(tmp_path,start, storage=storage)
    old=[];checked=[];ports=[]
    try:
        def inspect(db,event,context,reads,q):
            if not old:
                a=reads.iter_members('actions','plan_id',3)
                page=next(a)
                old.extend((reads,a,event.event_id))
                ports.append(reads)
                return
            if checked:return
            checked.append(event.event_id)
            prior,a,first_event=old
            assert event.event_id>first_event and reads is not prior
            ports.append(reads)
            b=reads.iter_members('actions','plan_id',3)
            current=None
            if new_page!='none':
                current=next(b)[0][0]
                assert reads.required('actions',current)['plan_id']==3
                if new_page=='ended':b.close()
            # 每个真实事件取得自己的端口；同事务旧端口只保留原范围。
            if old_exit=='next':
                page=next(a)
                assert page and len(page)<=prior.scope.batch_size
                assert all(row['plan_id']==3 for _,row in page)
                a.close()
            elif old_exit=='throw':
                with pytest.raises(RuntimeError):a.throw(RuntimeError('old event consumer'))
            else:a.close()
            if new_page=='active':assert reads.required('actions',current)['plan_id']==3
            for port in (prior,reads):
                with pytest.raises(ConsistencyError):port.optional('actions',3)
            b.close()
        callbacks["inspect"] = inspect
        observation.active = True
        result=_run_aggregate_role_command(owned,request,monkeypatch,start=start,finite=finite, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
        for port in ports:
            with pytest.raises(ConsistencyError):port.required('actions',8)
    finally:
        if old:old[1].close()
        owned.connection.close()


@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize("finite", [False, True])
async def test_source_aggregate_pages_have_exact_current_ids_and_keep_point_roles(source_observation, tmp_path, monkeypatch, start, finite):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.history.reads import ProductReadScope
    owned, request = await _aggregate_role_world(tmp_path, start, storage=storage)
    active={'value':False}
    probe=QueryProbe(owned.connection,lambda reads,columns,args: active['value']
        and ('actions','plan_id') in reads and columns==frozenset({'id'}) and len(args)==3)
    owned=replace(owned,connection=probe)
    checked = []
    try:
        def inspect(db, event, context, reads, q):
            if checked:
                return
            checked.append(event.event_id)
            a = reads.iter_members("actions", "plan_id", 3)
            assert [key for key, _ in next(a)] == [5, 6]
            assert reads.required("actions", 6)["plan_id"] == 3
            # 原点角色在活动聚合中照常工作；非法 query 仍按原资格拒绝。
            assert list(reads.iter_members("plans", "id", 3))[0][0][0] == 3
            if start:
                assert list(reads.iter_members("action_dependencies", "id", 1))[0][0][0] == 1
            for table, column, identity in (("actions", "plan_id", 1), ("actions", "id", 7), ("plans", "plan_id", 3)):
                with pytest.raises(ConsistencyError):
                    next(reads.iter_members(table, column, identity))
            with pytest.raises(ConsistencyError):
                next(reads.iter_members('actions','plan_id',3,limit=2))
            assert reads.required("actions", 6)["plan_id"] == 3
            # 不同 reads 独立；ProductReadScope 的正常迭代不受 source busy 限制。
            product = SqlEventReads(db, ProductReadScope(8, 1, _NOW, 2))
            p = product.iter_members("actions", "plan_id", 3)
            r = product.iter_members("actions", "plan_id", 3)
            assert next(p) and next(r)
            p.close()
            r.close()
            assert reads.required("actions", 6)["plan_id"] == 3
            assert [key for key, _ in next(a)] == [7, 8]
            # init 的当前规范源 5/6 保留点角色；source 上页的 6 则失效。
            if start:
                assert reads.required("actions", 6)["plan_id"] == 3
            else:
                with pytest.raises(ConsistencyError):
                    reads.optional("actions", 6)
            active['value']=True
            try:
                if not start:
                    assert [key for key, _ in next(a)] == [9]
                    with pytest.raises(ConsistencyError):
                        reads.optional("actions", 7)
                with pytest.raises(StopIteration):
                    next(a)
            finally:
                active['value']=False
            assert len(probe.queries)==1
            with pytest.raises(ConsistencyError):
                reads.optional("actions", 7)
            # 恰好整页后的可靠空、末尾小页都已退出，可以立即重新开始。
            c = reads.iter_members("actions", "plan_id", 3)
            assert next(c)
            c.close()
        callbacks["inspect"] = inspect
        observation.active = True
        result = _run_aggregate_role_command(owned, request, monkeypatch, start=start, finite=finite, batch_size=2, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize("old_exit", ["next", "close", "throw"])
async def test_bounded_source_events_keep_independent_page_owners(source_observation, tmp_path, monkeypatch, start, old_exit):
    observation, callbacks = source_observation
    storage = observation.storage
    from camctl.contracts.values import ConsistencyError
    owned,request=await _aggregate_role_world(tmp_path,start, storage=storage)
    old=[];checked=[];ports=[]
    try:
        def inspect(db,event,context,reads,q):
            if not old:
                a=reads.iter_members('actions','plan_id',3)
                assert next(a)
                dormant=reads.iter_members('actions','plan_id',3)
                old.extend((reads,a,dormant,event.event_id))
                ports.append(reads)
                return
            if checked:return
            checked.append(event.event_id)
            prior,a,dormant,first_event=old
            assert reads is not prior and event.event_id>first_event
            ports.append(reads)
            b=reads.iter_members('actions','plan_id',3)
            page=next(b)
            while 7 not in dict(page):page=next(b)
            # 不同真实事件的端口独立；原端口自己的重叠读取仍受限。
            with pytest.raises(ConsistencyError):next(dormant)
            if old_exit=='next':
                assert all(row['plan_id']==3 for _,row in next(a))
                a.close()
            elif old_exit=='throw':
                with pytest.raises(RuntimeError):a.throw(RuntimeError('prior actual event'))
            else:a.close()
            replacement=prior.iter_members('actions','plan_id',3)
            assert next(replacement)
            replacement.close()
            assert reads.required('actions',7)['plan_id']==3
            b.close()
            with pytest.raises(ConsistencyError):reads.optional('actions',7)
        callbacks["inspect"] = inspect
        observation.active = True
        result=_run_aggregate_role_command(owned,request,monkeypatch,start=start,finite=False, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
        for port in ports:
            with pytest.raises(ConsistencyError):port.required('actions',8)
    finally:
        if old:
            old[1].close();old[2].close()
        owned.connection.close()


@pytest.mark.parametrize("start", [False, True])
@pytest.mark.parametrize("finite", [False, True])
@pytest.mark.parametrize("failure", ["query_sql", "projection"])
async def test_source_aggregate_first_page_failure_releases_reservation(source_observation, tmp_path, monkeypatch, start, finite, failure):
    observation, callbacks = source_observation
    storage = observation.storage
    import sqlite3
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    owned, request = await _aggregate_role_world(tmp_path, start, storage=storage)
    checked = []
    try:
        def inspect(db, event, context, reads, q):
            if checked:
                return
            checked.append(event.event_id)
            a = reads.iter_members("actions", "plan_id", 3,
                columns=("id",) if failure == "projection" else None)
            if failure == "query_sql":
                db.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and table == "actions" else sqlite3.SQLITE_OK)
            try:
                with pytest.raises(sqlite3.DatabaseError if failure == "query_sql" else ConsistencyError):
                    next(a)
            finally:
                db.set_authorizer(None)
                a.close()
            b = reads.iter_members("actions", "plan_id", 3)
            page = next(b)
            target = 6 if start else 5
            while target not in dict(page):
                page = next(b)
            dormant = reads.iter_members("actions", "plan_id", 3)
            with pytest.raises(RuntimeError):
                dormant.throw(RuntimeError("never started"))
            dormant.close()
            assert reads.required("actions", target)["plan_id"] == 3
            b.close()
            with pytest.raises(ConsistencyError):
                reads.optional("actions", target)
        callbacks["inspect"] = inspect
        observation.active = True
        result = _run_aggregate_role_command(owned, request, monkeypatch, start=start, finite=finite, storage=storage, observation=observation)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert checked
    finally:
        owned.connection.close()
