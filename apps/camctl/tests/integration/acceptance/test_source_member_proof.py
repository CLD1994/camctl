"""受理成员只消费本组实际新建的 FIXED owner 和完整原输入。"""
from dataclasses import replace

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode, ProcessInput
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInputCommand, register_acceptance_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import CommandPlan, commit_operation, event_envelope, row_change, row_facts
from camctl.history.reads import AdmissionSourceReadScope

from .test_acceptance import Catalog, _NOW, _plan_body
from ..persistence.test_runtime import _create_valid_database

register_acceptance_guards()
register_capture_guards()
register_outputs_guards()


def _body():
    shots = [{"name":f"shot-{i}","group":"chosen" if i<2 else "other",
              "type":"camera_take_photo","device_id":"cam-1","scheduled_at":"2026-01-15 09:00:00",
              "params":{"type":"single_shot"},"policy":{"max_delay_ms":1000}} for i in range(3)]
    fetch = {"name":"fetch","type":"obtain_action_outputs","scheduled_at":"2026-01-15 09:00:00",
             "params":{"source":{"group":"chosen"},"purpose":"manual"}}
    return _plan_body(actions=[*shots,fetch])


def _accept(body,owned):
    return AcceptanceRepository().process_input(ProcessInput(ParsedInput("input.json",body),Catalog(),CommandMode.RUN,_NOW),new_operation_key(),owned)


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("mutation", ["missing_member","wrong_group","future_owner","missing_port"])
def test_actual_admission_rejects_partial_or_wrong_original_members(tmp_path,monkeypatch,mutation,bounded):
    path = tmp_path/"state.db"
    _create_valid_database(path)
    owned = open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    original = ProcessInputCommand.plan
    def plan(self,scope):
        result = original(self,scope)
        events = list(result.events)
        index = next(i for i,e in enumerate(events) if e.event_type==3 and e.reason==3)
        members = events[index]
        if mutation == "missing_member":
            events[index] = replace(members,rows=members.rows[:1])
        elif mutation == "wrong_group":
            row=members.rows[-1]
            events[index] = replace(members,rows=(*members.rows[:-1],replace(row,after=replace(row.after,values={**row.after.values,"depends_on_action_id":3}))))
        elif mutation == "future_owner":
            owner_index=next(i for i,e in enumerate(events) if any(r.table=="actions" and r.row_id==4 for r in e.rows))
            events[owner_index],events[index]=events[index],events[owner_index]
            events=[replace(e,event_id=result.events[0].event_id+i) for i,e in enumerate(events)]
        result = replace(result,events=tuple(events),source_read_scope=None if mutation=="missing_port" else result.source_read_scope)
        return _bounded_admission(result, scope.connection) if bounded else result
    monkeypatch.setattr(ProcessInputCommand,"plan",plan)
    try:
        before=tuple(owned.connection.iterdump())
        outcome=_accept(_body(),owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


def test_existing_fixed_owner_cannot_append_by_admission_branch_and_fake_dictionary(tmp_path):
    path=tmp_path/"state.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    try:
        accepted=_accept(_body(),owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        before=tuple(owned.connection.iterdump())
        class Append:
            def plan(self,scope):
                allocation=scope.allocate(1)
                row=row_change("action_dependencies",3,{"action_id":4,"depends_on_action_id":3})
                state={"actions":{i:row_facts(scope.connection,"actions",i) for i in (3,4)},"action_dependencies":{}}
                event=event_envelope(allocation.first_event_id,allocation.txn_id,3,3,(row,),_NOW)
                return CommandPlan((event,),{("action_dependencies",3):("action",4)},state,source_read_scope=AdmissionSourceReadScope(1,128))
        outcome=commit_operation(Append(),new_operation_key(),owned)
        assert outcome.kind == "rolled_back", outcome
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


def _bounded_admission(plan, connection):
    """真实逐事件写入；当前事实只来自原连接，不把未来提案放入 context。"""
    from camctl.history.validators import EventContext
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.transaction import BoundedCommandPlan
    def context(event, transaction):
        state = {table: {} for table in plan.state_rows}
        for proposed in plan.events:
            for row in proposed.rows:
                facts = row_facts(connection, row.table, row.row_id)
                if facts is not None:
                    state.setdefault(row.table, {})[row.row_id] = facts
        return EventContext(transaction, plan.owners, state, state,
            current_reads=SqlEventReads(connection, plan.source_read_scope)
            if plan.source_read_scope is not None else None)
    return BoundedCommandPlan(iter(plan.events), context, plan.result, lambda db: None)


@pytest.mark.parametrize("owner_count", [1, 2])
@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("mutation", ["control", "omit_all", "omit_one", "late_match", "late_unrelated", "split_members", "other_event_last"])
def test_admission_final_seal_covers_every_actual_new_fixed_owner(tmp_path, monkeypatch, bounded, mutation, owner_count):
    path = tmp_path / "complete.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    body = _body()
    # 两个真实来源 owner，分别固定同组；检查漏掉其中一个的整条事件。
    if owner_count == 2:
        body["actions"].append({**body["actions"][-1], "name": "fetch-again"})
    finalize = ProcessInputCommand._finalize
    original_plan = ProcessInputCommand.plan
    def edited(self, scope, templates, result):
        templates = list(templates)
        indices = [i for i,e in enumerate(templates) if e.event_type == 3 and e.reason == 3]
        if mutation == "omit_all":
            templates = [e for e in templates if not (e.event_type == 3 and e.reason == 3)]
        elif mutation == "omit_one":
            templates.pop(indices[-1])
        elif mutation in ("late_match", "late_unrelated"):
            identity = 2 if mutation == "late_match" else 3
            index = next(i for i,e in enumerate(templates) if any(r.table == "actions" and r.row_id == identity for r in e.rows))
            late = templates.pop(index)
            if mutation == "late_match":
                templates = [replace(e, rows=tuple(r for r in e.rows if r.after.values.get("depends_on_action_id") != identity))
                             if e.event_type == 3 and e.reason == 3 else e for e in templates]
            templates.append(late)
        elif mutation == "split_members":
            index = indices[0]
            event = templates[index]
            templates[index:index+1] = [replace(event, rows=event.rows[:1]), replace(event, rows=event.rows[1:])]
        elif mutation == "other_event_last":
            # 成员事件仍存在，但不完整；后接合法计划事件不能逃过最后封口。
            templates.pop(indices[-1])
        return finalize(self, scope, templates, result)
    def planned(self, scope):
        result = original_plan(self, scope)
        return _bounded_admission(result, scope.connection) if bounded else result
    monkeypatch.setattr(ProcessInputCommand, "_finalize", edited)
    monkeypatch.setattr(ProcessInputCommand, "plan", planned)
    try:
        before = tuple(owned.connection.iterdump())
        result = _accept(body, owned)
        if mutation in ("control", "late_unrelated"):
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            assert tuple(owned.connection.execute("SELECT action_id,depends_on_action_id FROM action_dependencies ORDER BY id")) == (((4,1),(4,2)) if owner_count == 1 else ((4,1),(4,2),(5,1),(5,2)))
        else:
            assert result.kind is DbOutcomeKind.ROLLED_BACK, (mutation, result)
            assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("source", [{"group":"chosen"}, {"action_name":"shot-0"}, {"current_plan":True}])
@pytest.mark.parametrize("delete", [False, True])
def test_admission_real_local_forms_share_final_seal(tmp_path, monkeypatch, bounded, source, delete):
    path = tmp_path / "forms.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    body = _body()
    body["actions"][-1]["params"]["source"] = source
    if delete:
        body["actions"][-1]["type"] = "delete_action_outputs"
        del body["actions"][-1]["params"]["purpose"]
    original = ProcessInputCommand.plan
    if bounded:
        monkeypatch.setattr(ProcessInputCommand,"plan",lambda self,scope:_bounded_admission(original(self,scope),scope.connection))
    try:
        result = _accept(body,owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        expected = () if delete and "group" in source else (1,) if "action_name" in source else (1,2) if "group" in source else (1,2,3)
        if delete and "group" in source:
            assert owned.connection.execute("SELECT status FROM actions WHERE id=4").fetchone() == (4,)
        assert tuple(row[0] for row in owned.connection.execute("SELECT depends_on_action_id FROM action_dependencies ORDER BY id")) == expected
        assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections").fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("bounded", [False, True])
def test_admission_reliable_zero_needs_no_empty_members_event(tmp_path, monkeypatch, bounded):
    path = tmp_path / "zero.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    body = _body()
    body["actions"] = [body["actions"][-1]]
    body["actions"][0]["params"]["source"] = {"current_plan":True}
    original = ProcessInputCommand.plan
    if bounded:
        monkeypatch.setattr(ProcessInputCommand,"plan",lambda self,scope:_bounded_admission(original(self,scope),scope.connection))
    try:
        result = _accept(body,owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert owned.connection.execute("SELECT source_resolution_state FROM actions").fetchone() == (2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM action_dependencies").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=3").fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("mutation", ["missing", "wrong_plan", "wrong_scope"])
def test_admission_f_binding_cannot_be_omitted_or_replaced(tmp_path,monkeypatch,bounded,mutation):
    from camctl.history.reads import PlanReadScope
    path=tmp_path/"scope.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    original=ProcessInputCommand.plan
    def planned(self,scope):
        result=original(self,scope)
        changed = None if mutation=="missing" else AdmissionSourceReadScope(99,128) if mutation=="wrong_plan" else PlanReadScope(result.result.plan_id,128)
        result=replace(result,source_read_scope=changed)
        return _bounded_admission(result,scope.connection) if bounded else result
    monkeypatch.setattr(ProcessInputCommand,"plan",planned)
    try:
        before=tuple(owned.connection.iterdump())
        result=_accept(_body(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mutation", ["late_scope", "swap_scope", "fake_reads", "other_connection", "clear_progress"])
def test_bounded_admission_keeps_kernel_f_binding_through_actual_stream(tmp_path,monkeypatch,mutation):
    from camctl.persistence.event_reads import SqlEventReads
    path=tmp_path/"stream.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    other=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    original=ProcessInputCommand.plan
    if mutation == "clear_progress":
        finalize = ProcessInputCommand._finalize
        def omitted(self,scope,templates,result):
            return finalize(self,scope,[event for event in templates if not(event.event_type==3 and event.reason==3)],result)
        monkeypatch.setattr(ProcessInputCommand,"_finalize",omitted)
    class FakeReads(SqlEventReads):
        pass
    def planned(self,scope):
        result=_bounded_admission(original(self,scope),scope.connection)
        context=result.context
        def changed(event,transaction):
            actual=context(event,transaction)
            if mutation=="late_scope" and event.event_id==transaction.first_event_id:
                return replace(actual,current_reads=None)
            if mutation=="swap_scope" and event.event_id!=transaction.first_event_id:
                return replace(actual,current_reads=SqlEventReads(scope.connection,AdmissionSourceReadScope(99,128)))
            if mutation=="fake_reads":
                return replace(actual,current_reads=FakeReads(scope.connection,actual.current_reads.scope))
            if mutation=="other_connection":
                return replace(actual,current_reads=SqlEventReads(other.connection,actual.current_reads.scope))
            # 没有 handler 的可变进度可以替代内核；再省整条成员事件，最终仍须拒绝。
            actual.current_reads._admission_binding=None
            return actual
        return replace(result,context=changed)
    monkeypatch.setattr(ProcessInputCommand,"plan",planned)
    try:
        before=tuple(owned.connection.iterdump())
        result=_accept(_body(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result
        if mutation == "clear_progress":
            from camctl.contracts.values import ConsistencyError
            assert isinstance(result.error, ConsistencyError)
        assert tuple(owned.connection.iterdump())==before
    finally:
        other.connection.close()
        owned.connection.close()


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("source", [{"group":"missing"}, {"action_name":"missing"}, {"action_name":"fetch"}, {"group":"only-obtain"}])
def test_final_admission_zero_requires_a_reliably_valid_source(tmp_path,monkeypatch,bounded,source):
    from copy import deepcopy
    path=tmp_path/"invalid-zero.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    body=_body()
    finalize=ProcessInputCommand._finalize
    original=ProcessInputCommand.plan
    def changed(self,scope,templates,result):
        actual=[]
        for event in templates:
            if event.event_type==3 and event.reason==3:
                continue
            rows=[]
            for row in event.rows:
                if row.table=="actions" and row.row_id==4:
                    values=deepcopy(row.after.values)
                    values["input_fields_json"]["params"]["source"]=source
                    if source.get("group") == "only-obtain":
                        values["group_name"] = "only-obtain"
                    row=replace(row,after=replace(row.after,values=values))
                rows.append(row)
            actual.append(replace(event,rows=tuple(rows)))
        return finalize(self,scope,actual,result)
    def planned(self,scope):
        result=original(self,scope)
        return _bounded_admission(result,scope.connection) if bounded else result
    monkeypatch.setattr(ProcessInputCommand,"_finalize",changed)
    monkeypatch.setattr(ProcessInputCommand,"plan",planned)
    try:
        before=tuple(owned.connection.iterdump())
        result=_accept(body,owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()
