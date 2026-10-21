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


@pytest.mark.parametrize("mutation", ["missing_member","wrong_group","future_owner","missing_port"])
def test_actual_admission_rejects_partial_or_wrong_original_members(tmp_path,monkeypatch,mutation):
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
        return replace(result,events=tuple(events),source_read_scope=None if mutation=="missing_port" else result.source_read_scope)
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
