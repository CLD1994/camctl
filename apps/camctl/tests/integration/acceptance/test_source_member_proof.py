"""受理成员只消费本组实际新建的 FIXED owner 和完整原输入。"""
from dataclasses import replace

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.repositories.transactions import CommandPlan, commit_operation, event_envelope, row_change, row_facts
from camctl.history.reads import AdmissionSourceReadScope

from .test_acceptance import Catalog, _NOW, _plan_body
from ..support.acceptance import accept_source
from ..support.database import database_path
from ..persistence.test_runtime import _create_valid_database



def _body():
    shots = [{"name":f"shot-{i}","group":"chosen" if i<2 else "other",
              "type":"camera_take_photo","device_id":"cam-1","scheduled_at":"2026-01-15 09:00:00",
              "params":{"type":"single_shot"},"policy":{"max_delay_ms":1000}} for i in range(3)]
    fetch = {"name":"fetch","type":"obtain_action_outputs","scheduled_at":"2026-01-15 09:00:00",
             "params":{"source":{"group":"chosen"},"purpose":"manual"}}
    return _plan_body(actions=[*shots,fetch])


def _proposal(body, owned):
    """由真实受理生成合法历史，再以公开事务命令验证独立存储守卫。"""
    import asyncio
    from camctl.contracts.history_values import ReadOrder, ReadScope
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.history import HistoryRepository

    source = database_path(owned).with_name("canonical-" + str(new_operation_key()) + ".db")
    _create_valid_database(source)
    context = AcceptanceContext(CommandMode.RUN, Catalog(), AcceptanceRepository(),
        type("Clock", (), {"utc_micros": staticmethod(lambda: _NOW)})())
    result = asyncio.run(accept_source(ParsedInput("input.json", body), context, source))
    history = HistoryRepository(source)
    boundary = history.current_boundary()
    events = history.read_events(ReadScope(ReadOrder.ASCENDING, None, 1,
        boundary.last_event_id, 128, lambda value: value), boundary).items
    events = tuple(replace(event, change_seq=None) for event in events)
    owners, state = {}, {}
    for event in events:
        for row in event.rows:
            state.setdefault(row.table, {})
            if row.table == "plans":
                owner = ("plan", row.row_id)
            elif row.table == "actions":
                owner = ("action", row.row_id)
            else:
                owner = ("action", row.after.values["action_id"])
            owners[(row.table, row.row_id)] = owner
    return CommandPlan(events, owners, state, result,
        source_read_scope=AdmissionSourceReadScope(result.plan_id, 128))


def _commit(body, owned, *, edit=None, bounded=False, edit_context=None):
    proposal = _proposal(body, owned)

    class Command:
        def plan(self, scope):
            plan = proposal if edit is None else edit(proposal, scope)
            allocation = scope.allocate(len(plan.events))
            plan = replace(plan, events=tuple(replace(event,
                event_id=allocation.first_event_id + index,
                transaction_id=allocation.txn_id) for index, event in enumerate(plan.events)))
            if not bounded:
                return plan
            plan = _bounded_admission(plan, scope.connection)
            if edit_context is not None:
                context = plan.context
                plan = replace(plan, context=lambda event, transaction:
                    edit_context(context(event, transaction), event, transaction, scope))
            return plan

    return commit_operation(Command(), new_operation_key(), owned)


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("mutation", ["missing_member","wrong_group","future_owner","missing_port"])
def test_actual_admission_rejects_partial_or_wrong_original_members(tmp_path,mutation,bounded):
    path = tmp_path/"state.db"
    _create_valid_database(path)
    owned = open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    def plan(result, scope):
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
        return result
    try:
        before=tuple(owned.connection.iterdump())
        outcome=_commit(_body(),owned,edit=plan,bounded=bounded)
        assert outcome.kind == "rolled_back", outcome
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


def test_existing_fixed_owner_cannot_append_by_admission_branch_and_fake_dictionary(tmp_path):
    path=tmp_path/"state.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    try:
        accepted=_commit(_body(),owned)
        assert accepted.kind == "completed", accepted.error
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
    from camctl.persistence.repositories.transactions import BoundedCommandPlan
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
def test_admission_final_seal_covers_every_actual_new_fixed_owner(tmp_path, bounded, mutation, owner_count):
    path = tmp_path / "complete.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    body = _body()
    # 两个真实来源 owner，分别固定同组；检查漏掉其中一个的整条事件。
    if owner_count == 2:
        body["actions"].append({**body["actions"][-1], "name": "fetch-again"})
    def edited(result, scope):
        templates = list(result.events)
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
        return replace(result, events=tuple(templates))
    try:
        before = tuple(owned.connection.iterdump())
        result = _commit(body, owned, edit=edited, bounded=bounded)
        if mutation in ("control", "late_unrelated"):
            assert result.kind == "completed", result.error
            assert tuple(owned.connection.execute("SELECT action_id,depends_on_action_id FROM action_dependencies ORDER BY id")) == (((4,1),(4,2)) if owner_count == 1 else ((4,1),(4,2),(5,1),(5,2)))
        else:
            assert result.kind == "rolled_back", (mutation, result)
            assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("source", [{"group":"chosen"}, {"action_name":"shot-0"}, {"current_plan":True}])
@pytest.mark.parametrize("delete", [False, True])
def test_admission_real_local_forms_share_final_seal(tmp_path, bounded, source, delete):
    path = tmp_path / "forms.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    body = _body()
    body["actions"][-1]["params"]["source"] = source
    if delete:
        body["actions"][-1]["type"] = "delete_action_outputs"
        del body["actions"][-1]["params"]["purpose"]
    try:
        result = _commit(body, owned, bounded=bounded)
        assert result.kind == "completed", result.error
        expected = () if delete and "group" in source else (1,) if "action_name" in source else (1,2) if "group" in source else (1,2,3)
        if delete and "group" in source:
            assert owned.connection.execute("SELECT status FROM actions WHERE id=4").fetchone() == (4,)
        assert tuple(row[0] for row in owned.connection.execute("SELECT depends_on_action_id FROM action_dependencies ORDER BY id")) == expected
        assert owned.connection.execute("SELECT COUNT(*) FROM obtain_source_selections").fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("bounded", [False, True])
def test_admission_reliable_zero_needs_no_empty_members_event(tmp_path, bounded):
    path = tmp_path / "zero.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    body = _body()
    body["actions"] = [body["actions"][-1]]
    body["actions"][0]["params"]["source"] = {"current_plan":True}
    try:
        result = _commit(body, owned, bounded=bounded)
        assert result.kind == "completed", result.error
        assert owned.connection.execute("SELECT source_resolution_state FROM actions").fetchone() == (2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM action_dependencies").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=3").fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("mutation", ["missing", "wrong_plan", "wrong_scope"])
def test_admission_f_binding_cannot_be_omitted_or_replaced(tmp_path,bounded,mutation):
    from camctl.history.reads import PlanReadScope
    path=tmp_path/"scope.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    def planned(result, scope):
        changed = None if mutation=="missing" else AdmissionSourceReadScope(99,128) if mutation=="wrong_plan" else PlanReadScope(result.result.plan_id,128)
        result=replace(result,source_read_scope=changed)
        return result
    try:
        before=tuple(owned.connection.iterdump())
        result=_commit(_body(),owned,edit=planned,bounded=bounded)
        assert result.kind == "rolled_back",result
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mutation", ["late_scope", "swap_scope", "fake_reads", "other_connection", "clear_progress"])
def test_bounded_admission_keeps_kernel_f_binding_through_actual_stream(tmp_path,mutation):
    from camctl.persistence.event_reads import SqlEventReads
    path=tmp_path/"stream.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    other=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    from camctl.history.validators import EventContext
    class FakeReads(SqlEventReads):
        pass

    def edit(result, scope):
        if mutation == "clear_progress":
            return replace(result, events=tuple(event for event in result.events
                if not (event.event_type == 3 and event.reason == 3)))
        return result

    def changed(actual, event, transaction, scope):
        if mutation == "late_scope" and event.event_id == transaction.first_event_id:
            return replace(actual, current_reads=None)
        if mutation == "swap_scope" and event.event_id != transaction.first_event_id:
            return replace(actual, current_reads=SqlEventReads(scope.connection, AdmissionSourceReadScope(99, 128)))
        if mutation == "fake_reads":
            return replace(actual, current_reads=FakeReads(scope.connection, actual.current_reads.scope))
        if mutation == "other_connection":
            return replace(actual, current_reads=SqlEventReads(other.connection, actual.current_reads.scope))
        return actual
    try:
        before=tuple(owned.connection.iterdump())
        result=_commit(_body(),owned,edit=edit,bounded=True,edit_context=changed)
        assert result.kind == "rolled_back",result
        if mutation == "clear_progress":
            from camctl.contracts.values import ConsistencyError
            assert isinstance(result.error, ConsistencyError)
        assert tuple(owned.connection.iterdump())==before
    finally:
        other.connection.close()
        owned.connection.close()


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("source", [{"group":"missing"}, {"action_name":"missing"}, {"action_name":"fetch"}, {"group":"only-obtain"}])
def test_final_admission_zero_requires_a_reliably_valid_source(tmp_path,bounded,source):
    from copy import deepcopy
    path=tmp_path/"invalid-zero.db"
    _create_valid_database(path)
    owned=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
    body=_body()
    def changed(result, scope):
        templates = result.events
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
        return replace(result, events=tuple(actual))
    try:
        before=tuple(owned.connection.iterdump())
        result=_commit(body,owned,edit=changed,bounded=bounded)
        assert result.kind == "rolled_back",result
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()
