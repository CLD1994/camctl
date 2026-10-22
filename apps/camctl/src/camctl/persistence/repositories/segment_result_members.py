"""按原H0有限保存并双向固定统一结果的全部成员。"""
from contextlib import closing
from itertools import zip_longest

from camctl.capture.segment_storage import SegmentMemberInput,SegmentResultRef,document,encode_enum
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal,parse_exact_json
from camctl.contracts.values import ConsistencyError,OperationKey,UtcMicros
from .segment_profiles import profile_for_action
from camctl.history.events import load_event_registry
from camctl.history.reads import SegmentReadScope
from camctl.persistence.transaction import (
    CommandPlan,event_envelope,next_row_id,row_change,row_facts,saved_transaction_events,update_change)
from .segment_result_sets import _one,_retained_input,_run_files,read_result_set

_EVENT=load_event_registry()['events']['SEGMENT_FACTS']
_STATE=enum_for('segment_sets.state')


def _expected_input(db,setrow,file_id):
    if setrow['run_id'] is None:
        raise ConsistencyError('无RESULTS集合没有来源成员')
    boundary=HistoryBoundary(setrow['source_txn_id'],setrow['source_event_id'])
    page=_one(db,'SELECT MAX(page_event_id) FROM result_page_files'
        ' WHERE run_id=? AND file_id=? AND created_event_id<=?',
        (setrow['run_id'],file_id,boundary.last_event_id))[0]
    if page is None:raise ConsistencyError('成员不属于原run在H0的实际文件全集')
    return _retained_input(db,file_id,page,boundary)


def _member_values(db,setrow,item):
    from .segments import _fixed_file
    if not isinstance(item,SegmentMemberInput) or item!=_expected_input(db,setrow,item.file_id):
        raise ConsistencyError('结果成员没有保持原H0的文件、页和严格分类')
    file=_fixed_file(db,item.file_id,item.file_event_id)
    identity=parse_exact_json(file['identity_key'])[2]
    parsed=profile_for_action(db,setrow['action_id']).classify_file(identity)
    return dict(set_id=setrow['id'],file_id=item.file_id,page_event_id=item.page_event_id,
        file_event_id=item.file_event_id,identity=identity,
        directory_number=None if parsed.key is None else parsed.key.directory_number,
        segment_index=None if parsed.key is None else parsed.key.index,
        role=encode_enum(item.role),ownership=encode_enum(item.ownership),completion=encode_enum(item.completion),
        pairing_state=encode_enum(item.pairing_state),pairing_identity=item.pairing_identity,
        pairing_fact_id=item.pairing_fact_id)


def _expected_inputs(db,setrow):
    if setrow['run_id'] is None:return
    boundary=HistoryBoundary(setrow['source_txn_id'],setrow['source_event_id'])
    for file_id,page in _run_files(db,setrow['run_id'],boundary):
        yield _retained_input(db,file_id,page,boundary)


def _fixed_count(db,setrow):
    from .segments import _member_input_from_row
    def actual():
        previous=0
        while True:
            with closing(db.execute('SELECT id FROM segment_members WHERE set_id=? AND file_id>?'
                    ' ORDER BY file_id LIMIT 128',(setrow['id'],previous))) as query:
                selected=query.fetchmany(128)
            if not selected:return
            for (identity,) in selected:
                row=row_facts(db,'segment_members',identity)
                previous=row['file_id']
                item=_member_input_from_row(row)
                expected=_member_values(db,setrow,item)
                if not json_equal({name:row[name] for name in expected},expected):
                    raise ConsistencyError('实际成员行与原固定来源不同')
                yield item
    count=0
    for expected,stored in zip_longest(_expected_inputs(db,setrow),actual()):
        if expected is None or stored is None or expected!=stored:
            raise ConsistencyError('固定成员没有双向覆盖原H0的全部文件')
        count+=1
    return count


def verify_result_member_event(db,ref,event,context):
    from .segments import _member_input_from_row
    setrow=read_result_set(ref,context.current_reads)
    if event.reason == _EVENT["branches"]["GAPS"]["reason"]:
        from .segments import _gap

        payload = tuple(_gap(db, dict(row.after.values)) for row in event.rows)
        expected = _current_gap_rows(db, setrow, payload, event.event_id)
        if event.rows != expected or not json_equal(
            event.evidence,
            {
                "segment_request": {
                    "ref": document(ref),
                    "input": [document(gap) for gap in payload],
                }
            },
        ):
            raise ConsistencyError("当前扫描缺口与原实际有序成员不同")
        return
    if setrow['state']!=_STATE.COLLECTING:
        raise ConsistencyError('统一结果成员只能在收集阶段追加或固定')
    if event.reason==_EVENT['branches']['MEMBERS']['reason']:
        if not 1<=len(event.rows)<=128:
            raise ConsistencyError('结果成员事件必须保持有限非空批次')
        inputs=[]
        start=next_row_id(db,'segment_members')
        for offset,row in enumerate(event.rows):
            values=dict(row.after.values)
            item=_member_input_from_row(values)
            expected={**_member_values(db,setrow,item),'created_event_id':event.event_id}
            if (row.table!='segment_members' or row.before.exists or row.row_id!=start+offset
                    or not json_equal(values,expected)):
                raise ConsistencyError('结果成员事件未保持实际来源行及分配身份')
            inputs.append(item)
        payload=[document(item) for item in inputs]
    elif event.reason==_EVENT['branches']['FIX_RESULT_MEMBERS']['reason']:
        count=_fixed_count(db,setrow)
        expected=update_change('segment_sets',ref.set_id,
            {'state':int(_STATE.COLLECTING),'member_count':None},
            {'state':int(_STATE.MEMBERS_FIXED),'member_count':count})
        if event.rows!=(expected,):
            raise ConsistencyError('固定事件没有保存原完整成员计数和状态')
        payload=None
    else:
        raise ConsistencyError('统一集合成员入口没有该事件分支')
    if not json_equal(event.evidence,{'segment_request':{'ref':document(ref),'input':payload}}):
        raise ConsistencyError('结果成员事件缺少原完整有限申请')


class ResultMembersCommand:
    def __init__(self,kind,ref,payload,occurred_at,key):
        if not isinstance(ref,SegmentResultRef):raise TypeError('统一成员需要实际集合引用')
        if kind not in ("MEMBERS", "FIX_RESULT_MEMBERS", "GAPS"):
            raise ValueError("未登记的统一成员操作")
        if kind in ("MEMBERS", "GAPS") and (
            not isinstance(payload, tuple) or not 1 <= len(payload) <= 128
        ):
            raise ValueError('结果成员批次必须为1—128项元组')
        UtcMicros(occurred_at)
        self.kind,self.ref,self.payload,self.at,self.key=kind,ref,payload,occurred_at,OperationKey(key)

    def plan(self,scope):
        db,ref=scope.connection,self.ref
        encoded=None if self.payload is None else [document(item) for item in self.payload]
        evidence={'segment_request':{'ref':document(ref),'input':encoded}}
        saved=saved_transaction_events(db,self.key)
        setrow=read_result_set(ref,scope)
        if saved is not None:
            event=saved[0]
            if (len(saved)!=1 or event['type']!=_EVENT['id']
                    or event['reason']!=_EVENT['branches'][self.kind]['reason']
                    or event['occurred_at']!=self.at or not json_equal(event['body']['evidence'],evidence)):
                raise ConsistencyError('原键结果成员的有限申请不同')
            for change in event['body']['rows']:
                current=row_facts(db,change['table'],change['id'])
                if current is None or any(not json_equal(current[name],value)
                        for name,value in change['after']['values'].items() if name!='state'):
                    raise ConsistencyError('原成员保存的实际行与原事务不同')
            return CommandPlan((),{},{},ref,True)
        if self.kind == "GAPS":
            allocation = scope.allocate(1)
            rows = _current_gap_rows(
                db, setrow, self.payload, allocation.first_event_id
            )
            event = event_envelope(
                allocation.first_event_id,
                allocation.txn_id,
                _EVENT["id"],
                _EVENT["branches"]["GAPS"]["reason"],
                rows,
                self.at,
                evidence,
            )
            return CommandPlan(
                (event,),
                {(row.table, row.row_id): ("action", ref.action_id) for row in rows},
                {
                    "actions": {ref.action_id: row_facts(db, "actions", ref.action_id)},
                    "segment_sets": {ref.set_id: setrow},
                },
                ref,
                segment_read_scope=SegmentReadScope(ref),
            )
        if setrow['state']!=_STATE.COLLECTING:
            raise ConsistencyError('统一结果成员已固定')
        allocation=scope.allocate(1)
        if self.kind=='MEMBERS':
            start=next_row_id(db,'segment_members')
            rows=tuple(row_change('segment_members',start+offset,{
                **_member_values(db,setrow,item),'created_event_id':allocation.first_event_id})
                for offset,item in enumerate(self.payload))
        else:
            rows=(update_change('segment_sets',ref.set_id,{'state':int(_STATE.COLLECTING),'member_count':None},
                {'state':int(_STATE.MEMBERS_FIXED),'member_count':_fixed_count(db,setrow)}),)
        event=event_envelope(allocation.first_event_id,allocation.txn_id,_EVENT['id'],
            _EVENT['branches'][self.kind]['reason'],rows,self.at,evidence)
        return CommandPlan((event,),{(row.table,row.row_id):('action',ref.action_id) for row in rows},
            {'actions':{ref.action_id:row_facts(db,'actions',ref.action_id)},'segment_sets':{ref.set_id:setrow},
                'segment_members':{}},ref,segment_read_scope=SegmentReadScope(ref))


def _current_gap_rows(db, setrow, gaps, event_id):
    from itertools import islice
    from .segment_current import current_gaps
    from .segments import _gap_row

    if (
        setrow["state"] != _STATE.MEMBERS_FIXED
        or setrow["source_kind"]
        != int(enum_for("segment_sets.source_kind").RETAINED_SOURCES)
        or not 1 <= len(gaps) <= 128
    ):
        raise ConsistencyError("当前扫描缺口只能追加到未处置的跨轮保留集合")
    number = _one(
        db, "SELECT COUNT(*) FROM segment_gaps WHERE set_id=?", (setrow["id"],)
    )[0]
    expected = tuple(islice(current_gaps(db, setrow), number, number + len(gaps)))
    if expected != gaps:
        raise ConsistencyError("当前缺口必须完整延续最后扫描的原有次序")
    first = next_row_id(db, "segment_gaps")
    return tuple(
        row_change(
            "segment_gaps",
            first + offset,
            {
                **_gap_row(db, setrow, number + offset + 1, gap),
                "created_event_id": event_id,
            },
        )
        for offset, gap in enumerate(gaps)
    )
