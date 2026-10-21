"""来源固定和开始初始化的实际有界事务与独立守卫。"""

from dataclasses import replace
import json

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.transaction import BoundedCommandPlan
from camctl.outputs.sources import SourceSpec

from .test_sources import _NOW, _seed_action, _six_form_environment

register_acceptance_guards()
register_operation_guards()
register_capture_guards()
outputs.register_outputs_guards()


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


def test_source_resolution_streams_bounded_events_and_returns_finite_result(tmp_path, monkeypatch):
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, SourceSpec(plan_instance_id=2), count=140)
        seen = []
        original = outputs._ResolveSourcesCommand.plan

        def inspect(command, scope):
            plan = original(command, scope)
            seen.append(isinstance(plan, BoundedCommandPlan))
            return plan

        monkeypatch.setattr(outputs._ResolveSourcesCommand, "plan", inspect)
        result = outputs.OutputsRepository(4).resolve_sources(request, new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert seen == [True]
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


def test_source_resolution_rejects_request_different_from_saved_input(tmp_path):
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, SourceSpec(plan_instance_id=2))
        before = tuple(owned.connection.iterdump())
        result = outputs.OutputsRepository().resolve_sources(
            replace(request, spec=SourceSpec(current_plan=True)), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


def test_obtain_initialization_streams_all_fixed_dependencies(tmp_path, monkeypatch):
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, SourceSpec(plan_instance_id=2), count=140)
        # 受理时固定的真实关系；开始命令必须共同初始化全部来源。
        owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=2,resolved_source_plan_id=2 WHERE id=30")
        members = (21, *range(100, 240))
        owned.connection.executemany("INSERT INTO action_dependencies(id,action_id,depends_on_action_id) VALUES(?,30,?)",
            enumerate(members, 1))
        owned.connection.commit()
        seen = []
        original = outputs._StartObtainCommand.plan

        def inspect(command, scope):
            plan = original(command, scope)
            seen.append(isinstance(plan, BoundedCommandPlan))
            return plan

        monkeypatch.setattr(outputs._StartObtainCommand, "plan", inspect)
        result = outputs.OutputsRepository(4).start_obtain_action(
            outputs.StartObtainAction(30, _NOW), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert seen == [True]
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
def test_six_original_forms_publish_exact_members_without_delete_selections(tmp_path, form, members, plan, action_type):
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned, form)
        owned.connection.execute("UPDATE actions SET type=? WHERE id=30", (action_type,))
        owned.connection.commit()
        result = outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.member_count == len(members)
        assert result.value.source_plan_id == plan
        assert result.value.selection_count == (len(members) if action_type == 4 else 0)
        assert tuple(row[0] for row in owned.connection.execute("SELECT depends_on_action_id FROM action_dependencies WHERE action_id=30 ORDER BY id")) == members
    finally:
        owned.connection.close()


def _rewrite_plan(original, mutation, finite):
    """攻击实际生产计划；有限入口和 Bounded 入口使用相同事件语言。"""
    from camctl.persistence.transaction import CommandPlan
    from camctl.contracts.history_values import TransactionRange
    events = list(original.events)
    tx = TransactionRange(events[0].transaction_id, events[0].event_id, events[-1].event_id)
    context = original.context
    if mutation == "missing_header":
        events[0] = replace(events[0], evidence={})
    elif mutation == "wrong_owner":
        row = events[0].rows[0]
        events[0] = replace(events[0], rows=(replace(row, after=replace(row.after,values={**row.after.values,"action_id":11})), *events[0].rows[1:]))
    elif mutation == "wrong_member":
        row = events[0].rows[0]
        events[0] = replace(events[0], rows=(replace(row, after=replace(row.after,values={**row.after.values,"depends_on_action_id":11})), *events[0].rows[1:]))
    elif mutation == "wrong_branch":
        events[0] = replace(events[0],reason=3,evidence={})
    elif mutation == "missing_final":
        events.pop()
    elif mutation in ("replace_final","reset_and_replace_final"):
        events[-1] = replace(events[0],event_id=events[-1].event_id)
    elif mutation == "changed_time":
        events[-1] = replace(events[-1],occurred_at=events[-1].occurred_at+1)
    elif mutation == "fake_header_count":
        key = next(iter(events[0].evidence))
        events[0] = replace(events[0],evidence={key:{**events[0].evidence[key],"member_count":True}})
    if finite:
        state, owners = {}, {}
        for event in events:
            actual = context(event,tx)
            for table, rows in actual.state_rows.items():
                state.setdefault(table,{}).update(rows)
            owners.update(actual.owners)
        scope = context(events[0],tx).current_reads.scope
        return CommandPlan(tuple(events),owners,state,result=original.result,source_read_scope=scope)
    if mutation == "missing_context":
        def context(event, tx):
            return replace(original.context(event,tx),current_reads=None)
    elif mutation == "fake_context":
        def context(event,tx):
            actual = original.context(event,tx)
            state = {table:dict(rows) for table,rows in actual.state_rows.items()}
            owner = next(iter(state["actions"]))
            if event.event_id != tx.first_event_id:
                return actual
            state["actions"][owner] = {**state["actions"][owner],"status":3,"source_resolution_state":2}
            return replace(actual,state_rows=state)
    elif mutation == "fake_reads":
        from camctl.persistence.event_reads import SqlEventReads
        class FakeReads(SqlEventReads):
            pass
        def context(event,tx):
            actual = original.context(event,tx)
            return replace(actual,current_reads=FakeReads(actual.current_reads.connection,actual.current_reads.scope))
    elif mutation in ("clear_progress","reset_and_replace_final"):
        def context(event,tx):
            actual = original.context(event,tx)
            # 处理器丢弃自己的上下文与分段进度；内核来源封口不可随之重启。
            actual.current_reads._segment_guard_state = {}
            actual.current_reads._source_stream = None
            return actual
    return replace(original,events=iter(events),context=context,complete=lambda db:None)


@pytest.mark.parametrize("finite", [False,True])
@pytest.mark.parametrize("mutation", ["missing_header","wrong_owner","wrong_member","wrong_branch","missing_final","replace_final","changed_time","fake_header_count"])
def test_source_kernel_rejects_entire_malformed_group_in_both_entries(tmp_path,monkeypatch,finite,mutation):
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned,SourceSpec(plan_instance_id=2),count=3)
        before = tuple(owned.connection.iterdump())
        plan = outputs._ResolveSourcesCommand.plan
        monkeypatch.setattr(outputs._ResolveSourcesCommand,"plan",lambda self,scope:_rewrite_plan(plan(self,scope),mutation,finite))
        result = outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, (mutation,result)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("finite", [False,True])
def test_both_entries_accept_the_same_complete_canonical_source_group(tmp_path,monkeypatch,finite):
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned,SourceSpec(plan_instance_id=2),count=3)
        plan = outputs._ResolveSourcesCommand.plan
        monkeypatch.setattr(outputs._ResolveSourcesCommand,"plan",lambda self,scope:_rewrite_plan(plan(self,scope),"clear_progress",finite))
        result = outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.member_count == 4
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mutation", ["missing_context","fake_reads","fake_context","reset_and_replace_final"])
def test_source_kernel_rejects_missing_or_fake_sql_port(tmp_path,monkeypatch,mutation):
    _, owned = _six_form_environment(tmp_path)
    try:
        request = _request(owned,SourceSpec(plan_instance_id=2),count=3)
        before = tuple(owned.connection.iterdump())
        plan = outputs._ResolveSourcesCommand.plan
        monkeypatch.setattr(outputs._ResolveSourcesCommand,"plan",lambda self,scope:_rewrite_plan(plan(self,scope),mutation,False))
        result = outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("source_state", [1,2])
def test_start_with_reliable_zero_initialization_preserves_source_state(tmp_path,source_state):
    _, owned = _six_form_environment(tmp_path)
    try:
        _request(owned,SourceSpec(plan_instance_id=2))
        owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=?,resolved_source_plan_id=? WHERE id=30", (source_state,2 if source_state==2 else None))
        owned.connection.commit()
        result = outputs.OutputsRepository(1).start_obtain_action(outputs.StartObtainAction(30,_NOW),new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert owned.connection.execute("SELECT status,execution_started,source_resolution_state FROM actions WHERE id=30").fetchone() == (2,1,source_state)
        assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections").fetchone() == (0,)
    finally:
        owned.connection.close()


def _real_environment(tmp_path, *, cleanup=False, source=None, captures=3):
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
    def accept(body):
        result = AcceptanceRepository().process_input(ProcessInput(ParsedInput("real.json",body),Catalog(),CommandMode.RUN,_NOW),new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        return result.value.plan_id
    source_plan = accept(_plan_body(request_id="501",actions=shots))
    source = source or {"plan_instance_id":str(source_plan)}
    owner = {"name":"fetch","type":"delete_action_outputs" if cleanup else "obtain_action_outputs",
             "scheduled_at":"2026-01-15 09:00:00","params":{"source":source}}
    if not cleanup:
        owner["params"]["purpose"]="manual"
    owner_plan = accept(_plan_body(request_id="502",actions=[owner]))
    owner_id = owned.connection.execute("SELECT id FROM actions WHERE plan_id=?",(owner_plan,)).fetchone()[0]
    repository = outputs.OutputsRepository(1)
    if cleanup:
        from camctl.outputs.cleanup_flow import StartCleanupAction
        result = repository.start_cleanup_action(StartCleanupAction(owner_id,_NOW),new_operation_key(),owned)
    else:
        result = repository.start_obtain_action(outputs.StartObtainAction(owner_id,_NOW),new_operation_key(),owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    from camctl.outputs.sources import read_source_spec
    request = outputs.ResolveSources(owner_id,read_source_spec(json.dumps({"params":{"source":source}})),_NOW)
    return target,owned,request


def test_original_source_key_rechecks_original_h_after_later_work_and_new_ids(tmp_path):
    _, owned, request = _real_environment(tmp_path)
    repository = outputs.OutputsRepository(1)
    key = new_operation_key()
    try:
        first = repository.resolve_sources(request,key,owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        before = tuple(owned.connection.iterdump())
        statements = []
        owned.connection.set_trace_callback(statements.append)
        replay = repository.resolve_sources(request,key,owned)
        owned.connection.set_trace_callback(None)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert replay.value == first.value
        assert tuple(owned.connection.iterdump()) == before
        assert not any("WHERE operation_key" in sql and "body_json" in sql for sql in statements)
        wrong = repository.resolve_sources(replace(request,occurred_at=_NOW+1),key,owned)
        assert wrong.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start",[False,True])
@pytest.mark.parametrize("finite",[False,True])
@pytest.mark.parametrize("mutation",["missing_header","missing_final","replace_final","changed_time"])
def test_initialization_and_failure_streams_reject_partial_groups(tmp_path,monkeypatch,start,finite,mutation):
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
        cls=outputs._StartObtainCommand if start else outputs._ResolveSourcesCommand
        original=cls.plan
        monkeypatch.setattr(cls,"plan",lambda self,scope:_rewrite_plan(original(self,scope),mutation,finite))
        repo=outputs.OutputsRepository(1)
        outcome=(repo.start_obtain_action(outputs.StartObtainAction(30,_NOW),new_operation_key(),owned) if start else repo.resolve_sources(request,new_operation_key(),owned))
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("finite",[False,True])
def test_canonical_failure_binds_original_error_and_parent_completion(tmp_path,monkeypatch,finite):
    _,owned=_six_form_environment(tmp_path)
    try:
        request=_request(owned,SourceSpec(action_instance_id=999))
        owned.connection.execute("UPDATE actions SET status=3 WHERE id IN (11,12,13,14)")
        owned.connection.commit()
        original=outputs._ResolveSourcesCommand.plan
        monkeypatch.setattr(outputs._ResolveSourcesCommand,"plan",lambda self,scope:_rewrite_plan(original(self,scope),"clear_progress",finite))
        outcome=outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),owned)
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
def test_start_rejects_nonempty_pending_already_initialized_or_canceled(tmp_path,source_state,violation):
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
        result=outputs.OutputsRepository(1).start_obtain_action(outputs.StartObtainAction(30,_NOW),new_operation_key(),owned)
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


def test_bound_port_from_another_connection_is_rejected(tmp_path,monkeypatch):
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from camctl.persistence.event_reads import SqlEventReads
    path,owned=_six_form_environment(tmp_path)
    other=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    try:
        request=_request(owned,SourceSpec(plan_instance_id=2))
        before=tuple(owned.connection.iterdump())
        original=outputs._ResolveSourcesCommand.plan
        def plan(self,scope):
            result=original(self,scope)
            def context(event,tx):
                actual=result.context(event,tx)
                return replace(actual,current_reads=SqlEventReads(other.connection,actual.current_reads.scope))
            return replace(result,context=context)
        monkeypatch.setattr(outputs._ResolveSourcesCommand,"plan",plan)
        result=outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump())==before
    finally:
        other.connection.close()
        owned.connection.close()


@pytest.mark.parametrize("cleanup",[False,True])
@pytest.mark.parametrize("committed",[False,True])
def test_unknown_commit_recovers_same_source_request_and_ids_after_reopen(tmp_path,cleanup,committed):
    import sqlite3
    from camctl.persistence.runtime import DbConfig,DbOpenMode,OwnedConnection,open_existing
    path,owned,request=_real_environment(tmp_path,cleanup=cleanup)
    key=new_operation_key()
    db=owned.connection
    class LoseCommit:
        @property
        def in_transaction(self):
            return db.in_transaction
        def execute(self,sql,args=()):
            if sql=="COMMIT":
                if committed:
                    db.execute(sql,args)
                raise sqlite3.OperationalError("commit response lost")
            return db.execute(sql,args)
    result=outputs.OutputsRepository(1).resolve_sources(request,key,OwnedConnection(LoseCommit(),None))
    assert result.kind is DbOutcomeKind.UNKNOWN
    db.close()
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    try:
        before=tuple(owned.connection.execute("SELECT id,action_id,depends_on_action_id FROM action_dependencies"))
        recovered=outputs.OutputsRepository(1).resolve_sources(request,key,owned)
        assert recovered.kind is DbOutcomeKind.COMPLETED,recovered.error
        assert recovered.value.member_count==3
        after=tuple(owned.connection.execute("SELECT id,action_id,depends_on_action_id FROM action_dependencies"))
        if committed:
            assert after==before
        assert len(after)==3
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",(str(key),)).fetchone()==(1,)
    finally:
        owned.connection.close()


def test_non_obtain_start_cannot_smuggle_obtain_rows(tmp_path,monkeypatch):
    from camctl.outputs.cleanup_flow import StartCleanupAction
    from camctl.persistence.transaction import row_change
    _,owned=_six_form_environment(tmp_path)
    try:
        _request(owned,SourceSpec(plan_instance_id=2))
        owned.connection.execute("UPDATE actions SET type=5,status=1,execution_started=0,execution_spec_json='{}',target_selection_state=1 WHERE id=30")
        owned.connection.execute("INSERT INTO action_dependencies VALUES(1,30,21)")
        owned.connection.commit()
        before=tuple(owned.connection.iterdump())
        original=outputs._StartCleanupCommand.plan
        def plan(self,scope):
            result=original(self,scope)
            event=result.events[0]
            extra=row_change("obtain_source_selections",1,dict(dependency_id=1,status=1,error_code=None,error_details_json=None))
            return replace(result,events=(replace(event,rows=(*event.rows,extra)),*result.events[1:]))
        monkeypatch.setattr(outputs._StartCleanupCommand,"plan",plan)
        result=outputs.OutputsRepository().start_cleanup_action(StartCleanupAction(30,_NOW),new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


def test_source_original_key_uses_original_ids_when_other_owners_add_rows(tmp_path):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode,ProcessInput
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from ..acceptance.test_acceptance import Catalog,_plan_body
    _,owned,request=_real_environment(tmp_path)
    try:
        repo=outputs.OutputsRepository(1)
        key=new_operation_key()
        first=repo.resolve_sources(request,key,owned)
        assert first.kind is DbOutcomeKind.COMPLETED,first.error
        original_ids=tuple(owned.connection.execute("SELECT id,dependency_id FROM obtain_source_selections ORDER BY id"))
        new={"name":"other","type":"obtain_action_outputs","scheduled_at":"2026-01-15 09:00:00","params":{"source":{"plan_instance_id":"1"},"purpose":"manual"}}
        accepted=AcceptanceRepository().process_input(ProcessInput(ParsedInput("later.json",_plan_body(request_id="503",actions=[new])),Catalog(),CommandMode.RUN,_NOW+1),new_operation_key(),owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED,accepted.error
        other=owned.connection.execute("SELECT id FROM actions WHERE plan_id=?",(accepted.value.plan_id,)).fetchone()[0]
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
def test_original_source_key_requires_current_immutable_rows_to_remain(tmp_path,sql):
    _,owned,request=_real_environment(tmp_path)
    try:
        repository=outputs.OutputsRepository(1)
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


def test_original_start_key_survives_source_fixed_after_start(tmp_path):
    from camctl.contracts.values import OperationKey
    _,owned,request=_real_environment(tmp_path)
    try:
        raw=owned.connection.execute("SELECT t.operation_key,e.occurred_at FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id WHERE e.event_type=5 AND json_extract(e.body_json,'$.evidence.obtain_initialization_request.action_id')=? ORDER BY e.id LIMIT 1",(request.action_id,)).fetchone()
        result=outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        before=tuple(owned.connection.iterdump())
        replay=outputs.OutputsRepository(1).start_obtain_action(outputs.StartObtainAction(request.action_id,raw[1]),OperationKey(raw[0]),owned)
        assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed",[False,True])
def test_fixed_initialization_unknown_commit_reuses_exact_original_selection_ids(tmp_path,committed):
    import sqlite3
    from camctl.persistence.runtime import DbConfig,DbOpenMode,OwnedConnection,open_existing
    from ..acceptance.test_source_member_proof import _accept,_body
    from ..persistence.test_runtime import _create_valid_database
    path=tmp_path/"fixed.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    assert _accept(_body(),owned).kind is DbOutcomeKind.COMPLETED
    db=owned.connection
    command=outputs.StartObtainAction(4,_NOW)
    key=new_operation_key()
    class LoseCommit:
        @property
        def in_transaction(self):
            return db.in_transaction
        def execute(self,sql,args=()):
            if sql=="COMMIT":
                if committed:
                    db.execute(sql,args)
                raise sqlite3.OperationalError("start commit response lost")
            return db.execute(sql,args)
    first=outputs.OutputsRepository(1).start_obtain_action(command,key,OwnedConnection(LoseCommit(),None))
    assert first.kind is DbOutcomeKind.UNKNOWN
    db.close()
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    try:
        replay=outputs.OutputsRepository(1).start_obtain_action(command,key,owned)
        assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
        assert tuple(owned.connection.execute("SELECT id,dependency_id,status FROM obtain_source_selections ORDER BY id"))==((1,1,1),(2,2,1))
        before=tuple(owned.connection.iterdump())
        same=outputs.OutputsRepository(1).start_obtain_action(command,key,owned)
        assert same.kind is DbOutcomeKind.COMPLETED,same.error
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("scope_name",["SourceResolutionReadScope","ObtainInitializationReadScope"])
@pytest.mark.parametrize("table",["outputs","device_files","obtain_items","output_origins"])
def test_source_scope_excludes_catalog_and_file_fact_ports(tmp_path,scope_name,table):
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
def test_both_entries_accept_complete_fixed_source_initialization(tmp_path,monkeypatch,finite):
    _,owned=_six_form_environment(tmp_path)
    try:
        _request(owned,SourceSpec(plan_instance_id=2))
        owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=2,resolved_source_plan_id=2 WHERE id=30")
        owned.connection.execute("INSERT INTO action_dependencies VALUES(17,30,21)")
        owned.connection.commit()
        original=outputs._StartObtainCommand.plan
        monkeypatch.setattr(outputs._StartObtainCommand,"plan",lambda self,scope:_rewrite_plan(original(self,scope),"clear_progress",finite))
        result=outputs.OutputsRepository(1).start_obtain_action(outputs.StartObtainAction(30,_NOW),new_operation_key(),owned)
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
def test_sql_failure_rolls_back_rows_history_directories_and_snapshot_progress(tmp_path,prefix,skip):
    import sqlite3
    from camctl.persistence.runtime import OwnedConnection
    _,owned=_six_form_environment(tmp_path)
    try:
        request=_request(owned,SourceSpec(plan_instance_id=2),count=3)
        before=tuple(owned.connection.iterdump())
        db=owned.connection
        class Fault:
            count=0
            def __getattr__(self,name):
                return getattr(db,name)
            def execute(self,sql,args=()):
                if sql.startswith(prefix):
                    self.count+=1
                    if self.count==skip+1:
                        raise sqlite3.OperationalError("source transaction fault")
                return db.execute(sql,args)
        fault=Fault()
        result=outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),OwnedConnection(fault,None))
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result
        assert isinstance(result.error,sqlite3.OperationalError),result.error
        assert fault.count>=skip+1
        assert tuple(db.iterdump())==before
    finally:
        owned.connection.close()


def test_source_write_and_original_key_keep_bounded_residency(tmp_path):
    import gc,tracemalloc
    from camctl.persistence.runtime import OwnedConnection
    records=[]
    for count in (20,200,600):
        directory=tmp_path/str(count)
        directory.mkdir()
        _,owned,request=_real_environment(directory,captures=count)
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
                raise AssertionError("来源增长路径不得 fetchall")
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
                stats["sql_calls"]+=1
                if "SELECT COUNT(*), MIN(id), MAX(id) FROM history_events" in sql and args==(txn,):
                    stats["group_checks"]+=1
                body = "FROM history_events WHERE transaction_id=? AND id>? AND id<=?" in sql
                if body and stats["first_body_limit"] is None:
                    stats["first_body_limit"] = args[-1]
                return Cursor(db.execute(sql,args),body)
        try:
            measured=OwnedConnection(Measured(),None)
            repository=outputs.OutputsRepository(4)
            key=new_operation_key()
            gc.collect();tracemalloc.start();tracemalloc.reset_peak()
            written=repository.resolve_sources(request,key,measured)
            write_peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            assert written.kind is DbOutcomeKind.COMPLETED,written.error
            gc.collect();tracemalloc.start();tracemalloc.reset_peak()
            reused=repository.resolve_sources(request,key,measured)
            reuse_peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            assert reused.kind is DbOutcomeKind.COMPLETED,reused.error
            assert reused.value==written.value
            assert stats["max_fetch"]<=128 and stats["group_checks"]<=3
            event_count=db.execute("SELECT last_event_id-first_event_id+1 FROM history_transactions WHERE operation_key=?",(str(key),)).fetchone()[0]
            assert stats["first_body_limit"] == 1
            assert stats["body_rows"] == event_count + 1  # 头窥读一个，正文完整一遍。
            from camctl.persistence.repositories.source_transactions import _created_before
            original_f=db.execute("SELECT first_event_id FROM history_transactions WHERE operation_key=?",(str(key),)).fetchone()[0]
            query="SELECT t.id FROM action_dependencies t WHERE "+_created_before("action_dependencies","t",original_f)+" ORDER BY t.id DESC LIMIT 1"
            explain=[row[-1] for row in db.execute("EXPLAIN QUERY PLAN "+query)]
            assert any("entity_event_links" in row and "INDEX" in row for row in explain),explain
            records.append({"N":count,"B":4,"write_peak":write_peak,"reuse_peak":reuse_peak,"original_id_plan":explain,**stats})
        finally:
            tracemalloc.stop()
            db.close()
    print("P5_SOURCE_GROWTH "+json.dumps(records))
    assert records[-1]["write_peak"]<records[0]["write_peak"]+2_000_000
    assert records[-1]["reuse_peak"]<records[0]["reuse_peak"]+2_000_000


def test_bounded_context_cannot_carry_the_whole_source_plan(tmp_path,monkeypatch):
    from camctl.persistence.transaction import row_facts
    _,owned=_six_form_environment(tmp_path)
    try:
        request=_request(owned,SourceSpec(plan_instance_id=2),count=20)
        before=tuple(owned.connection.iterdump())
        original=outputs._ResolveSourcesCommand.plan
        def plan(self,scope):
            result=original(self,scope)
            def context(event,tx):
                actual=result.context(event,tx)
                if event.event_id != tx.first_event_id:
                    return actual
                state={table:dict(rows) for table,rows in actual.state_rows.items()}
                # 合法事实也不能全部塞入 Bounded 页；该完整映射模拟旧读取路径。
                state["actions"].update({identity:row_facts(scope.connection,"actions",identity)
                    for identity, in scope.connection.execute("SELECT id FROM actions WHERE plan_id=2")})
                return replace(actual,state_rows=state)
            return replace(result,context=context)
        monkeypatch.setattr(outputs._ResolveSourcesCommand,"plan",plan)
        result=outputs.OutputsRepository(1).resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("scope_name", ["SourceResolutionReadScope", "ObtainInitializationReadScope"])
@pytest.mark.parametrize("table,identity", [("actions", 21), ("plans", 1), ("action_dependencies", 1), ("obtain_source_selections", 1)])
def test_unbound_source_scope_cannot_grant_point_fact_roles(tmp_path, scope_name, table, identity):
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
def test_bound_source_scope_only_exposes_actual_canonical_page_roles(tmp_path, monkeypatch, start):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    _, owned, request = _real_environment(tmp_path)
    try:
        # 另一 owner 的真实已提交依赖/selection 不能借同表点读取进入当前角色。
        from camctl.acceptance.input import ParsedInput
        from camctl.acceptance.service import CommandMode, ProcessInput
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        from ..acceptance.test_acceptance import Catalog, _plan_body
        raw={"name":"other","type":"obtain_action_outputs","scheduled_at":"2026-01-15 09:00:00","params":{"source":{"plan_instance_id":"1"},"purpose":"manual"}}
        accepted=AcceptanceRepository().process_input(ProcessInput(ParsedInput("other.json",_plan_body(request_id="503",actions=[raw])),Catalog(),CommandMode.RUN,_NOW),new_operation_key(),owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED,accepted.error
        other=owned.connection.execute("SELECT id FROM actions WHERE plan_id=?",(accepted.value.plan_id,)).fetchone()[0]
        repo=outputs.OutputsRepository(1)
        assert repo.start_obtain_action(outputs.StartObtainAction(other,_NOW),new_operation_key(),owned).kind is DbOutcomeKind.COMPLETED
        assert repo.resolve_sources(outputs.ResolveSources(other,SourceSpec(plan_instance_id=1),_NOW),new_operation_key(),owned).kind is DbOutcomeKind.COMPLETED
        if start:
            owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=2,resolved_source_plan_id=1 WHERE id=?", (request.action_id,))
            owned.connection.executemany("INSERT INTO action_dependencies VALUES(?,?,?)", [(17,request.action_id,1),(18,request.action_id,2)])
            owned.connection.commit()
        verify = streams._verify_context
        checked = []
        def inspect(db, event, context, reads, q):
            verify(db, event, context, reads, q)
            if checked:
                return
            checked.append(event.event_id)
            assert reads.required("actions", request.action_id)["id"] == request.action_id
            assert reads.required("plans", q["action"]["plan_id"])["id"] == q["action"]["plan_id"]
            assert reads.required("actions", 1)["id"] == 1
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
        monkeypatch.setattr(streams,"_verify_context",inspect)
        repository = outputs.OutputsRepository(1)
        result = repository.start_obtain_action(outputs.StartObtainAction(request.action_id,_NOW),new_operation_key(),owned) if start else repository.resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start", [False, True])
def test_handler_cannot_prefill_aggregate_page_to_grant_foreign_points(tmp_path, monkeypatch, start):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    _, owned, request = _real_environment(tmp_path)
    try:
        if start:
            owned.connection.execute("UPDATE actions SET status=1,execution_started=0,source_resolution_state=2,resolved_source_plan_id=1 WHERE id=?",(request.action_id,))
            owned.connection.execute("INSERT INTO action_dependencies VALUES(17,?,1)",(request.action_id,))
            owned.connection.commit()
        original_context = streams._context
        verify = streams._verify_context
        checked = []
        def context(*args):
            actual = original_context(*args)
            actual.current_reads._source_aggregate_page = frozenset({2})
            return actual
        def inspect(db,event,actual,reads,q):
            verify(db,event,actual,reads,q)
            if checked:
                return
            checked.append(event.event_id)
            with pytest.raises(ConsistencyError):
                reads.optional("actions",2)
        monkeypatch.setattr(streams,"_context",context)
        monkeypatch.setattr(streams,"_verify_context",inspect)
        repository=outputs.OutputsRepository(1)
        result=repository.start_obtain_action(outputs.StartObtainAction(request.action_id,_NOW),new_operation_key(),owned) if start else repository.resolve_sources(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
    finally:
        owned.connection.close()


def _aggregate_role_world(tmp_path, start):
    """真实三计划：目标 owner=8/main plan=3，源页与数值碰撞 action 分开。"""
    from ..acceptance.test_source_member_proof import _accept, _body
    _, owned, _ = _real_environment(tmp_path)
    body = _body()
    if not start:
        body["actions"][-1]["params"]["source"] = {"plan_instance_id":"1"}
        local = _body()["actions"][-1]
        local["name"] = "local-fetch"
        body["actions"].append(local)
    accepted = _accept(body, owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert accepted.value.plan_id == 3
    request = outputs.StartObtainAction(8,_NOW) if start else outputs.ResolveSources(8,SourceSpec(plan_instance_id=1),_NOW)
    if not start:
        local_started = outputs.OutputsRepository(1).start_obtain_action(outputs.StartObtainAction(9,_NOW),new_operation_key(),owned)
        assert local_started.kind is DbOutcomeKind.COMPLETED, local_started.error
        started = outputs.OutputsRepository(1).start_obtain_action(outputs.StartObtainAction(8,_NOW),new_operation_key(),owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
    return owned, request


def _run_aggregate_role_command(owned, request, monkeypatch, *, start, finite):
    command = outputs._StartObtainCommand if start else outputs._ResolveSourcesCommand
    original = command.plan
    monkeypatch.setattr(command,"plan",lambda self,scope:_rewrite_plan(original(self,scope),"clear_progress",finite))
    repository = outputs.OutputsRepository(1)
    return repository.start_obtain_action(request,new_operation_key(),owned) if start else repository.resolve_sources(request,new_operation_key(),owned)


@pytest.mark.parametrize("start", [False,True])
@pytest.mark.parametrize("finite", [False,True])
@pytest.mark.parametrize("table", ["plans","action_dependencies","obtain_source_selections"])
def test_point_iteration_cannot_relabel_its_numeric_ids_as_actions(tmp_path,monkeypatch,start,finite,table):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    owned,request = _aggregate_role_world(tmp_path,start)
    original = streams._verify_context
    checked = []
    try:
        def inspect(db,event,context,reads,q):
            original(db,event,context,reads,q)
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
        monkeypatch.setattr(streams,"_verify_context",inspect)
        result = _run_aggregate_role_command(owned,request,monkeypatch,start=start,finite=finite)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start", [False,True])
@pytest.mark.parametrize("finite", [False,True])
@pytest.mark.parametrize("failure", ["optional","sql"])
def test_aggregate_role_is_revoked_when_page_assembly_fails(tmp_path,monkeypatch,start,finite,failure):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    import sqlite3
    owned,request = _aggregate_role_world(tmp_path,start)
    verify = streams._verify_context
    checked = []
    try:
        def inspect(db,event,context,reads,q):
            verify(db,event,context,reads,q)
            if checked:
                return
            checked.append(event.event_id)
            target = 6 if start else 5
            optional = reads.optional
            fault = sqlite3.DatabaseError("page SQL failed") if failure=="sql" else RuntimeError("page assembly failed")
            def failed(table,identity,columns=None):
                if (table,identity)==("actions",target):
                    if failure == "sql":
                        db.set_authorizer(lambda action, table_name, *rest: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_READ and table_name == "actions" else sqlite3.SQLITE_OK)
                        try:
                            return optional(table,identity,columns)
                        finally:
                            db.set_authorizer(None)
                    raise fault
                return optional(table,identity,columns)
            reads.optional = failed
            iterator = reads.iter_members("actions","plan_id",3)
            try:
                with pytest.raises(type(fault)):
                    while True:
                        next(iterator)
            finally:
                reads.optional = optional
            with pytest.raises(ConsistencyError):
                reads.optional("actions",target)
            iterator.close()
            assert reads.required("actions",8)["plan_id"] == 3
        monkeypatch.setattr(streams,"_verify_context",inspect)
        result = _run_aggregate_role_command(owned,request,monkeypatch,start=start,finite=finite)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("start", [False,True])
@pytest.mark.parametrize("finite", [False,True])
@pytest.mark.parametrize("exit_kind", ["exhaust","close","throw","consumer_error"])
def test_real_plan_aggregate_page_role_ends_with_its_iterator(tmp_path,monkeypatch,start,finite,exit_kind):
    from contextlib import closing
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories import source_transactions as streams
    owned,request = _aggregate_role_world(tmp_path,start)
    verify = streams._verify_context
    checked = []
    try:
        def inspect(db,event,context,reads,q):
            verify(db,event,context,reads,q)
            if checked:
                return
            checked.append(event.event_id)
            target = 6 if start else 5
            with pytest.raises(ConsistencyError):
                reads.optional("actions",target)
            for table,column,identity in (("plans","id",3),("action_dependencies","id",1),("actions","plan_id",1)):
                with pytest.raises(ConsistencyError):
                    streams.bind_aggregate_page(reads,table,column,identity,after=0,limit=1)
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
        monkeypatch.setattr(streams,"_verify_context",inspect)
        result = _run_aggregate_role_command(owned,request,monkeypatch,start=start,finite=finite)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
    finally:
        owned.connection.close()


@pytest.mark.parametrize("finite", [False,True])
def test_plan_aggregate_keeps_real_accepted_prefix_fields(tmp_path,monkeypatch,finite):
    from camctl.persistence.repositories import source_transactions as streams
    owned,request = _aggregate_role_world(tmp_path,True)
    verify = streams._verify_context
    checked = []
    try:
        def inspect(db,event,context,reads,q):
            verify(db,event,context,reads,q)
            if event.event_id != context.transaction.last_event_id:
                return
            # START owner 已经是实际已接受前序；finite 尚未写 SQL，Bounded 已写。
            assert any(row.table == "plans" and row.row_id == 3 for row in event.rows)
            rows = [facts for page in reads.iter_members("actions","plan_id",3) for _,facts in page]
            owner = next(row for row in rows if row["id"] == 8)
            assert (owner["status"],owner["execution_started"]) == (2,1)
            checked.append(event.event_id)
        monkeypatch.setattr(streams,"_verify_context",inspect)
        result = _run_aggregate_role_command(owned,request,monkeypatch,start=True,finite=finite)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert checked
    finally:
        owned.connection.close()
