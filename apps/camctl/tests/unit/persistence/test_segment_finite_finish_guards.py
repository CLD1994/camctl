"""有限完成提案通过真实读取端口、当前事实守卫及逐事件 seal。"""
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.segment_models import (
    SegmentSetRef,SegmentAssessment,SegmentAssessmentStatus,SegmentAssessmentReason,
    CompletionBasis,CompletionFacts,EvidenceState,DefaultSegmentScope,DefaultScopeState,
    DefaultScopeBasis,OverrecordStatus,
)
from camctl.capture.segment_finish import FinishSegmentRecording,request_document
from camctl.capture.media import RecordingFailure
from camctl.capture.segment_storage import SegmentResultRef,assessment_document,facts_document
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.devices.tasks import CaptureResultPolicy
from camctl.history.reads import PlanReadScope
from camctl.history.validators import EventContext,ValidatedEvent
from camctl.persistence import fact_reads
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.repositories import segment_transactions as finish
from camctl.persistence.transaction import CommandPlan,encode_json_value,event_envelope,row_change
from camctl.history.events import business_columns


@pytest.mark.parametrize("ref_kind",["scan","retained","no_results"])
@pytest.mark.parametrize("defect",[None,"current_cancel","missing_marker","forged_failure","missing_terminal","foreign_terminal"])
def test_finite_proposal_real_guard_and_seal_chain(monkeypatch,defect,ref_kind):
    ref=SegmentSetRef(1,8,9,1)
    facts=CompletionFacts(*(EvidenceState.UNKNOWN,)*4,None,EvidenceState.UNKNOWN,
                          EvidenceState.UNKNOWN,True,False)
    selected=DefaultSegmentScope(DefaultScopeState.EMPTY,next(value for value in DefaultScopeBasis
        if value is not DefaultScopeBasis.UNKNOWN),ref,None,None,0)
    assessment=SegmentAssessment(SegmentAssessmentStatus.FAILED,SegmentAssessmentReason.NO_NONZERO_ORIGINAL,
        CompletionBasis.NONE,0,0,None,None,None,OverrecordStatus.UNKNOWN,selected,ref,60000,0,None,0,False)
    action={"id":1,"type":int(enum_for("actions.type").CAMERA_RECORD),"status":2,
            "cancel_requested":int(defect=="current_cancel"),"plan_id":6,"error_code":None,
            "error_details_json":None,"execution_spec_json":{"result_policy":CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value}}
    activity={"id":3,"action_id":1}
    run={"id":4,"action_id":1,"activity_id":3,"status":3,"retry_wait_required":0,"error_json":None}
    setrow={"id":2,"action_id":1,"activity_id":3,"run_id":4,"current_attempt_id":8,
            "current_final_page_event_id":9,"version":1,"member_count":0,"state":3,
            "created_event_id":7,"published_event_id":None,
            "publication_kind":int(enum_for("segment_sets.publication_kind").RESULT),
            "source_kind":int(enum_for("segment_sets.source_kind").COMPLETE_SCAN),
            "facts_json":facts_document(facts),"assessment_json":assessment_document(assessment)}
    conclusion=None
    failure_details={"activity_id":"3","reason":"no_outputs"}
    failure_code="capture_failed"
    if ref_kind!="scan":
        ref=SegmentResultRef(1,2,1)
        setrow.update(current_attempt_id=None,current_final_page_event_id=None,facts_json=None,assessment_json=None,
            source_kind=int(enum_for("segment_sets.source_kind")["NO_RESULTS" if ref_kind=="no_results" else "RETAINED_SOURCES"]))
        failure_code="capture_failed" if ref_kind=="no_results" else "capture_result_unconfirmed"
        failure_details={"activity_id":"3","reason":"baseline_read_failed" if ref_kind=="no_results" else "outputs_unknown"}
        values={name:None for name in business_columns("segment_check_conclusions")}
        values.update(set_id=2,reason=int(enum_for("segment_check_conclusions.reason")["NO_RESULT_WORK" if ref_kind=="no_results" else "RESULT_BUDGET_EXHAUSTED"]),
            current_result=int(enum_for("segment_check_conclusions.current_result")["NONE" if ref_kind=="no_results" else "UNCONFIRMED"]))
        conclusion={"id":12,"created_event_id":7,**values}
        if ref_kind=="no_results":
            activity=run=None
            action["execution_started"]=0
            setrow.update(activity_id=None,run_id=None)
            action["cancel_requested"]=0 if defect=="current_cancel" else 1
            setrow["publication_kind"]=int(enum_for("segment_sets.publication_kind").CANCELED)
            conclusion["reason"]=values["reason"]=int(enum_for("segment_check_conclusions.reason").CANCELED_BEFORE_PUBLICATION)
    rows={"actions":action,"device_activities":activity,"operation_runs":run,
          "operation_attempts":{"id":8,"run_id":4,"result_last_page_event_id":9},
          "segment_sets":setrow,"plans":{"id":6,"status":2},"segment_check_conclusions":conclusion}
    rows={table:row for table,row in rows.items() if row is not None}
    db=create_autospec(sqlite3.Connection,instance=True,spec_set=True)
    db.in_transaction=True
    def execute(sql,parameters=()):
        cursor=create_autospec(sqlite3.Cursor,instance=True,spec_set=True)
        cursor.fetchone.return_value=None
        cursor.fetchmany.return_value=()
        cursor.description=()
        if sql.startswith("SELECT * FROM ") and "WHERE id = ?" in sql:
            table=sql.split()[3]
            row=rows.get(table)
            if row is not None and row["id"]==parameters[0]:
                cursor.description=tuple((name,) for name in row)
                cursor.fetchone.return_value=tuple(encode_json_value(value) if isinstance(value,(dict,list)) else value for value in row.values())
        elif sql.startswith("SELECT id FROM segment_check_conclusions WHERE set_id="):
            cursor.fetchone.return_value=None if conclusion is None else (12,)
        elif sql.startswith("SELECT COUNT(*)"):
            cursor.fetchone.return_value=(0,)
        elif sql.startswith("SELECT id FROM segment_sets WHERE action_id=? AND version=?"):
            cursor.fetchone.return_value=(2,)
        elif sql.startswith("SELECT 1 FROM actions WHERE plan_id="):
            cursor.fetchone.return_value=(1,) # another pending action keeps the parent plan open
        elif sql.startswith("SELECT 1 FROM segment_sets WHERE action_id=?") and "published_event_id" not in sql:
            cursor.fetchone.return_value=(1,)
        elif sql.startswith("SELECT") and any(table in sql for table in ("segment_members","segment_scope_members","segment_check_conclusions","outputs","operation_attempts","operation_runs","device_activities","segment_sets")):
            pass
        else:
            raise AssertionError(sql)
        return cursor
    db.execute.side_effect=execute
    created=event_envelope(7,2,finish.storage._EVENT["id"],finish.storage._EVENT["branches"]["CREATE"]["reason"],(),4)
    if conclusion is not None:
        created=event_envelope(7,2,finish.storage._EVENT["id"],finish.storage._EVENT["branches"]["CONCLUDE_CHECK"]["reason"],
            (row_change("segment_check_conclusions",12,values),),4)
    monkeypatch.setattr(fact_reads,"read_event",create_autospec(fact_reads.read_event,
        return_value=fact_reads.EventRecord((),created,TransactionRange(2,7,7))))
    request=FinishSegmentRecording(ref,setrow["activity_id"],setrow["run_id"],5,None if ref_kind=="no_results" else RecordingFailure(failure_code,failure_details))
    tx=TransactionRange(7,10,11)
    qualification={"action_status":2,"finish_run":False,"complete_plan":False,"plan_result_status":2,
       "plan_status":2,"preparation_events":(),"run_status":3,"run_retry_wait_required":0,"run_error_json":None}
    outcome=(6,None,None) if ref_kind=="no_results" else (4,finish.registered_error(failure_code)["action_error_id"],request.failure.details)
    header={"request":request_document(request),"action_status":outcome[0],"first_output_id":1,"first_origin_id":1,
       "output_count":0,"original_count":0,"preview_count":0,"scope_count":0,"batch_size":128,
       "finish_run":False,"complete_plan":False,"plan_status":2}
    events=tuple(finish._canonical_events(request,tx,header,finish._FinishFacts(action,run,setrow,assessment,outcome),
        qualification,lambda:iter(()),lambda:iter(()),lambda *_:None))
    if defect=="forged_failure":
        from dataclasses import replace
        from camctl.persistence.transaction import update_change
        events=(replace(events[0],rows=(update_change("actions",1,{"status":2,"error_code":None,"error_details_json":None},
            {"status":4,"error_code":outcome[1],"error_details_json":{"activity_id":"3","reason":"invalid_outputs"}}),)),events[1])
    plan=CommandPlan(events,{}, {table:{row["id"]:row} for table,row in rows.items()})
    accepted=[]
    state={}
    final={"segment_sets":{2:{**setrow,"published_event_id":None if defect=="missing_marker" else 11}}}
    first_reads=SqlEventReads(db,PlanReadScope(6,128),accepted,plan.events)
    first_reads.bind_segment_finish_state(state,tx)
    first_reads._kernel_transaction=tx
    first_reads._kernel_accepted=accepted
    context=EventContext(tx,plan.owners,plan.state_rows,final,current_reads=first_reads)
    if defect in ("current_cancel","missing_marker","forged_failure"):
        with pytest.raises(ConsistencyError):
            first_reads.verify_segment_finish_event(events[0],context,1)
        assert "accepted" not in state
        return
    first_reads.verify_segment_finish_event(events[0],context,1)
    first=ValidatedEvent(events[0],"ACTION_FINISHED","CANCEL" if outcome[0]==6 else "FAIL",(),{})
    first_reads.seal_segment_finish(first)
    accepted.append(first)
    final_action={**action,"status":outcome[0],"error_code":outcome[1],"error_details_json":outcome[2]}
    marker_reads=SqlEventReads(db,PlanReadScope(6,128),accepted,plan.events)
    marker_reads.bind_segment_finish_state(state,tx)
    marker_reads._kernel_transaction=tx
    marker_reads._kernel_accepted=accepted
    marker_context=EventContext(tx,plan.owners,{**plan.state_rows,"actions":{1:final_action}},final,current_reads=marker_reads)
    if defect=="missing_terminal":
        accepted.clear()
    elif defect=="foreign_terminal":
        from dataclasses import replace
        accepted[0]=replace(first,envelope=replace(first.envelope,transaction_id=8))
    if defect in ("missing_terminal","foreign_terminal"):
        with pytest.raises(ConsistencyError):
            marker_reads.verify_segment_event(events[1],marker_context)
        return
    marker_reads.verify_segment_event(events[1],marker_context)
    marker_reads.seal_segment_finish(ValidatedEvent(events[1],"SEGMENT_FACTS","ACTIVATE",(),{}))
    assert "progress" not in state


def test_constructor_proposal_cannot_replace_kernel_actual_prefix():
    from camctl.persistence.transaction import update_change
    db=create_autospec(sqlite3.Connection,instance=True,spec_set=True)
    db.in_transaction=True
    tx=TransactionRange(7,10,13)
    invented=ValidatedEvent(event_envelope(10,7,finish.load_event_registry()['events']['ACTION_FINISHED']['id'],1,
        (update_change('actions',1,{'status':2},{'status':3}),),5),'ACTION_FINISHED','SUCCEED',(),{})
    reads=SqlEventReads(db,PlanReadScope(6,128),[invented],[invented.envelope])
    reads.bind_segment_finish_state({},tx)
    reads._kernel_transaction=tx
    reads._kernel_accepted=[]
    assert reads.segment_finish_values('actions',{'id':1,'status':2},tx)['status']==2
    with pytest.raises(ConsistencyError,match='前序'):
        reads.segment_finish_event(10,tx)
    with pytest.raises(ConsistencyError,match='前序'):
        reads.segment_created_output(tx,1,11,int(enum_for('outputs.kind').ORIGINAL))
    assert tuple(reads.segment_finish_outputs(tx,1))==()
    assert reads.segment_finish_origin(tx,11)==(0,None,None)
    db.execute.assert_not_called()
