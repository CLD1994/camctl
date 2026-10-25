"""从原已保存来源独立核实并保存有限结果决定。"""
from contextlib import closing
from dataclasses import replace
from camctl.capture.segment_storage import (
    CurrentSegmentResult,PublicationKind,ResultCheckConclusionRef,ResultCheckConclusionSave,
    ResultCheckEndReason,SegmentResultRef,SegmentSourceKind,decode_enum,document,encode_enum)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal,parse_exact_json
from camctl.contracts.values import ConsistencyError,OperationKey
from camctl.history.events import load_event_registry
from camctl.history.reads import ResultCheckWriteScope,SegmentReadScope
from camctl.persistence.transaction import (
    CommandPlan,TransactionScope,event_envelope,next_row_id,row_change,row_facts,saved_transaction_events,update_change,
    _read_sql_value)
from .segment_result_sets import _boundary,_one,_read_transaction,read_result_set

_EVENT=load_event_registry()['events']['SEGMENT_FACTS']
_REASON=_EVENT['branches']['CONCLUDE_CHECK']['reason']
_RUN_EVENT=load_event_registry()['events']['OPERATION_CONFIGURED']
_CONFIGURE=_RUN_EVENT['branches']['CONFIGURE']['reason']
_FINISH=_RUN_EVENT['branches']['FINISH']['reason']
_CONFIG_COLUMNS=('max_attempts_used','timeout_s_json','retry_interval_s_json')
_CURRENT_REASONS = frozenset(
    (
        ResultCheckEndReason.CONTROL_SUFFICIENT,
        ResultCheckEndReason.SEGMENTS_SUFFICIENT,
        ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
        ResultCheckEndReason.EXPLICIT_RESULT_ERROR,
        ResultCheckEndReason.GAP_CONFIRMED,
        ResultCheckEndReason.ORIGINAL_ABSENT,
    )
)
_NATIVE_REASONS = frozenset(
    (
        ResultCheckEndReason.NATIVE_RESULT_DECIDED,
        ResultCheckEndReason.NATIVE_FINITE_CANCEL_END,
    )
)


def _values(db,request,setrow):
    if (request.reason is not ResultCheckEndReason.NO_RESULT_WORK
            or setrow['source_kind']!=encode_enum(SegmentSourceKind.NO_RESULTS)
            or setrow['publication_kind']!=encode_enum(PublicationKind.RESULT)
            or setrow['state']!=int(enum_for('segment_sets.state').MEMBERS_FIXED)
            or setrow['member_count']!=0 or request.anchor_event_id is None
            or any(value is not None for value in (request.prior_run_end_event_id,
                request.check_config,request.binding_failure,request.prior_conclusion_id))):
        raise ConsistencyError('该申请不是无RESULTS的原基准结束决定')
    action=row_facts(db,'actions',request.ref.action_id)
    activity=row_facts(db,'device_activities',setrow['activity_id'])
    if (
        action["status"] != int(enum_for("actions.status").RUNNING)
        or action["execution_started"] != 1
        or action["cancel_requested"]
        or activity is None
    ):
        raise ConsistencyError("无RESULTS结束决定缺少原运行中活动资格")
    kinds=enum_for('operation_runs.kind')
    if (
        _one(
            db,
            "SELECT id FROM operation_runs WHERE action_id=? AND kind=? LIMIT 1",
            (request.ref.action_id, int(kinds.CHECK_CAPTURE_RESULTS)),
        )
        is not None
    ):
        raise ConsistencyError("已有RESULTS责任，不能采用无RESULTS结束分区")
    preparation = _preparation_run_for_anchor(db, setrow, request.anchor_event_id)
    start = _one(
        db,
        "SELECT id FROM operation_runs WHERE action_id=? AND kind=?",
        (request.ref.action_id, int(kinds.START)),
    )
    if preparation is not None or start is not None:
        failure = no_result_failure(db, setrow, request.anchor_event_id)
        return dict(
            set_id=request.ref.set_id,
            reason=encode_enum(request.reason),
            anchor_event_id=request.anchor_event_id,
            prior_run_end_event_id=None,
            config_event_id=None,
            prior_conclusion_id=None,
            current_result=encode_enum(CurrentSegmentResult.NONE),
            current_failure_json={"code": failure.code, "details": failure.details},
        )
    if activity["dispatch_state"] != int(
        enum_for("device_activities.dispatch_state").NOT_DISPATCHED
    ):
        raise ConsistencyError("基准结束决定缺少原未派发资格")
    call=_one(db,'SELECT id FROM baseline_read_calls WHERE action_id=? AND activity_id=? AND result_event_id=?',
        (request.ref.action_id,setrow['activity_id'],request.anchor_event_id))
    if call is None:raise ConsistencyError('基准决定缺少同动作实际结果依据')
    from .baseline_reads import read_failure_anchor
    class Reader:connection=db
    boundary=HistoryBoundary(setrow['source_txn_id'],setrow['source_event_id'])
    actual=read_failure_anchor(request.ref.action_id,call[0],boundary,Reader)
    if actual is None or actual.result_event_id!=request.anchor_event_id:
        raise ConsistencyError('基准决定不是原H0内的可靠错误或返回未知')
    return dict(set_id=request.ref.set_id,reason=encode_enum(request.reason),
        anchor_event_id=request.anchor_event_id,prior_run_end_event_id=None,config_event_id=None,
        prior_conclusion_id=None,current_result=encode_enum(CurrentSegmentResult.NONE),current_failure_json=None)


def _preparation_run_for_anchor(db, setrow, anchor):
    return _one(db,
        "SELECT r.id FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE r.action_id=? AND r.kind=? AND t.result_event_id=?",
        (setrow["action_id"], int(enum_for("operation_runs.kind").PREPARE_CAPTURE), anchor))


def no_result_failure(db, setrow, anchor):
    """按指定实际结果辨认失败来源，旧 START 不覆盖本次设置失败。"""
    if _preparation_run_for_anchor(db, setrow, anchor) is not None:
        return no_result_preparation_failure(db, setrow, anchor)
    return no_result_start_failure(db, setrow, anchor)


def no_result_preparation_failure(db, setrow, anchor):
    """核实原 H0 内最新设置失败及无启动效果，不建立 START 或 RESULTS。"""
    from camctl.capture.media import RecordingFailure
    from .capture_facts import load_start_facts
    from .segment_result_sets import _check_boundary, _row_at

    boundary = HistoryBoundary(setrow["source_txn_id"], setrow["source_event_id"])
    current = _check_boundary(db, boundary)
    action = _row_at(db, "actions", setrow["action_id"], setrow["action_id"], boundary, current)
    activity = _row_at(db, "device_activities", setrow["activity_id"], setrow["action_id"], boundary, current)
    found = _preparation_run_for_anchor(db, setrow, anchor)
    latest = _one(db, "SELECT r.id FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE r.activity_id=? AND r.kind=? AND t.attempt_no=1 AND t.intent_event_id<=?"
        " ORDER BY r.id DESC LIMIT 1",
        (setrow["activity_id"], int(enum_for("operation_runs.kind").PREPARE_CAPTURE), boundary.last_event_id))
    if found is None or found != latest:
        raise ConsistencyError("准备决定不是原活动最新设置责任的实际结果")
    run = _row_at(db, "operation_runs", found[0], setrow["action_id"], boundary, current)
    facts = load_start_facts(db, action)
    if (action["status"] != int(enum_for("actions.status").RUNNING)
            or action["execution_started"] != 1 or action["cancel_requested"]
            or activity["action_id"] != action["id"]
            or activity["occupancy_state"] != int(enum_for("device_activities.occupancy_state").HELD)
            or not facts.not_started or not facts.preparation_resolved
            or facts.activity is None or facts.activity["id"] != activity["id"]
            or run["action_id"] != action["id"] or run["activity_id"] != activity["id"]
            or run["status"] not in (int(enum_for("operation_runs.status").FAILED),
                int(enum_for("operation_runs.status").UNCONFIRMED))
            or run["attempts_used"] != 1 or run["error_json"] is None
            or setrow["run_id"] is not None or setrow["member_count"] != 0
            or setrow["source_kind"] != encode_enum(SegmentSourceKind.NO_RESULTS)):
        raise ConsistencyError("准备失败缺少原运行中占用、可靠收场或无启动效果依据")
    if (_one(db, "SELECT id FROM operation_runs WHERE action_id=? AND kind=? LIMIT 1",
            (action["id"], int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS))) is not None
            or _one(db, "SELECT id FROM outputs WHERE source_action_id=? LIMIT 1", (action["id"],)) is not None
            or _one(db, "SELECT id FROM baseline_read_calls WHERE activity_id=? AND state=1 LIMIT 1",
                (activity["id"],)) is not None):
        raise ConsistencyError("准备失败不能越过已有 RESULTS、产物或未收场的基准调用")
    if _ended_attempts(db, run, boundary) != anchor:
        raise ConsistencyError("准备失败没有原 H0 内的实际结果事件")
    attempt_id = _one(db, "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=1", (run["id"],))
    attempt = None if attempt_id is None else _row_at(db, "operation_attempts", attempt_id[0],
        action["id"], boundary, current)
    if (attempt is None or attempt["run_id"] != run["id"]
            or attempt["intent_event_id"] is None
            or not 1 <= attempt["intent_event_id"] < anchor <= boundary.last_event_id
            or attempt["status"] not in (int(enum_for("operation_attempts.status").FAILED),
                int(enum_for("operation_attempts.status").UNKNOWN))
            or attempt["result_event_id"] != anchor or attempt["error_json"] is None):
        raise ConsistencyError("准备决定没有同票真实失败或返回未知")
    from camctl.history.decoding import decode_event_row
    from camctl.persistence.transaction import read_transaction_range
    from .result_pages import _COLUMNS

    raw = _one(db, f"SELECT {_COLUMNS} FROM history_events WHERE id=?", (anchor,))
    event = None if raw is None else decode_event_row(raw)
    if (event is None or event.event_type != load_event_registry()["events"]["ATTEMPT_RESULT"]["id"]
            or not any(row.table == "operation_attempts" and row.row_id == attempt["id"]
                and all(json_equal(row.after.values.get(name), attempt[name]) for name in
                    ("status", "result_event_id", "error_json", "result_json")) for row in event.rows)):
        raise ConsistencyError("准备失败结果事件没有保存同票的真实错误及返回")
    transaction = read_transaction_range(db, event.transaction_id)
    if not transaction.first_event_id <= anchor <= transaction.last_event_id <= boundary.last_event_id:
        raise ConsistencyError("准备失败实际结果不属于原 H0 内的完整事务")
    return RecordingFailure("capture_failed", {"activity_id": str(activity["id"]), "reason": "device_failed"})


def no_result_start_failure(db, setrow, anchor):
    """从原已结束 START 的实际结果恢复既定失败，不补造结果核实。"""
    from camctl.capture.media import RecordingFailure

    found = _one(
        db,
        "SELECT id FROM operation_runs WHERE action_id=? AND kind=?",
        (setrow["action_id"], int(enum_for("operation_runs.kind").START)),
    )
    run = None if found is None else row_facts(db, "operation_runs", found[0])
    if (
        run is None
        or run["status"] not in (4, 6)
        or run["activity_id"] != setrow["activity_id"]
        or setrow["run_id"] is not None
        or setrow["member_count"] != 0
        or _ended_attempts(
            db, run, HistoryBoundary(setrow["source_txn_id"], setrow["source_event_id"])
        )
        != anchor
    ):
        raise ConsistencyError("无RESULTS启动失败缺少原全部尝试及实际关闭")
    raw = run["error_json"]
    if raw is None:
        raise ConsistencyError("原已关闭 START 没有实际失败")
    if raw["code"] in (
        "device_start_failed",
        "start_attempts_exhausted",
        "capture_result_unconfirmed",
        "capture_failed",
    ):
        failure = RecordingFailure(raw["code"], raw["details"])
    elif raw["code"] == "result_unconfirmed":
        failure = RecordingFailure(
            "capture_result_unconfirmed",
            {"activity_id": str(setrow["activity_id"]), "reason": "start_unknown"},
        )
    else:
        # 驱动错误保留在原 run/attempt；公开动作结果只采用业务失败码。
        # 使用原全部已结束尝试的效果，终态发布后仍可核实同一决定。
        uncertain = _one(
            db,
            "SELECT 1 FROM operation_attempts WHERE run_id=? AND effect_state!=? LIMIT 1",
            (run["id"], int(enum_for("operation_attempts.effect_state").NO_EFFECT)),
        )
        if (uncertain is None
                and run["status"] == int(enum_for("operation_runs.status").FAILED)):
            failure = RecordingFailure(
                "capture_failed",
                {"activity_id": str(setrow["activity_id"]), "reason": "device_failed"},
            )
        else:
            failure = RecordingFailure(
                "capture_result_unconfirmed",
                {"activity_id": str(setrow["activity_id"]), "reason": "start_unknown"},
            )
    from camctl.contracts.workflow_errors import validate_error_details

    validate_error_details(failure.code, failure.details)
    return failure


def _request(raw):
    from camctl.operations.attempts import AttemptConfig
    from camctl.capture.media import RecordingFailure

    config=None if raw['check_config'] is None else AttemptConfig(**raw['check_config'])
    request = ResultCheckConclusionSave(
        SegmentResultRef(**raw["ref"]),
        decode_enum(ResultCheckEndReason, raw["reason"]),
        raw["anchor_event_id"],
        raw["prior_run_end_event_id"],
        config,
        None
        if raw["binding_failure"] is None
        else RecordingFailure(**raw["binding_failure"]),
        raw["prior_conclusion_id"],
        raw["occurred_at"],
    )
    if not json_equal(document(request),raw):raise ConsistencyError('有限决定证据不是完整规范申请')
    return request


def requires_check_group(db,event):
    if event.event_type==_EVENT['id'] and event.reason==_REASON:return True
    if event.event_type!=_RUN_EVENT['id'] or event.reason not in (_CONFIGURE,_FINISH):return False
    created=_EVENT['branches']['CREATE_RESULT']['reason']
    for row in event.rows:
        if row.table=='operation_runs' and _one(db,
                'SELECT s.id FROM operation_runs r JOIN segment_sets s ON s.action_id=r.action_id AND s.run_id=r.id'
                ' JOIN history_events e ON e.id=s.created_event_id'
                " WHERE r.id=? AND e.event_type=? AND json_extract(e.body_json,'$.reason')=? LIMIT 1",
                (row.row_id,_EVENT['id'],created)) is not None:
            return True
    return False


def _adopted_config_event(db,run):
    from camctl.contracts.enums import load_registry
    owner=load_registry()['history_objects']['action']['id']
    found=_one(db,'SELECT e.id FROM entity_event_links l JOIN history_events e ON e.id=l.event_id'
        ' JOIN json_each(e.body_json,\'$.rows\') r'
        " WHERE l.entity_type=? AND l.entity_id=? AND json_extract(r.value,'$.table')='operation_runs'"
        " AND json_extract(r.value,'$.id')=?"
        " AND (json_type(r.value,'$.after.values.max_attempts_used') IS NOT NULL"
        " OR json_type(r.value,'$.after.values.timeout_s_json') IS NOT NULL"
        " OR json_type(r.value,'$.after.values.retry_interval_s_json') IS NOT NULL) ORDER BY l.event_id DESC LIMIT 1",
        (owner,run['action_id'],run['id']))
    if found is None:raise ConsistencyError('原RESULTS缺少实际采用配置事件')
    return found[0]


def _ended_attempts(db,run,boundary):
    """按原票序逐项核实际结束，不形成全部尝试字典。"""
    from camctl.history.decoding import decode_event_row
    from .result_pages import _COLUMNS
    expected=1;last=None
    with closing(db.execute('SELECT id,attempt_no,status,result_event_id,result_json FROM operation_attempts'
            ' WHERE run_id=? ORDER BY attempt_no',(run['id'],))) as cursor:
        for identity,number,status,result_event,result in cursor:
            if (number!=expected or status==int(enum_for('operation_attempts.status').RUNNING)
                    or result_event is None or result_event>boundary.last_event_id or result is None):
                raise ConsistencyError('有限决定必须具有原run全部连续且实际结束的尝试')
            raw=_one(db,f'SELECT {_COLUMNS} FROM history_events WHERE id=?',(result_event,))
            actual=None if raw is None else decode_event_row(raw)
            if (actual is None or actual.event_type!=load_event_registry()['events']['ATTEMPT_RESULT']['id']
                    or not any(row.table=='operation_attempts' and row.row_id==identity
                        and row.after.values.get('result_event_id')==result_event
                        and row.after.values.get('status')==status
                        and json_equal(row.after.values.get('result_json'),parse_exact_json(result)) for row in actual.rows)):
                raise ConsistencyError('有限决定的尝试结果没有同票实际结束事件')
            expected+=1;last=result_event
    if last is None or expected-1!=run['attempts_used']:
        raise ConsistencyError('原RESULTS累计次数与全部实际票数不同')
    return last


def _prepare(db,request):
    class Reader:connection=db
    setrow=read_result_set(request.ref,Reader)
    if request.reason is ResultCheckEndReason.NO_RESULT_WORK:
        return setrow,None,(),_values(db,request,setrow)
    if request.reason is ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION:
        return _prepare_canceled(db, request, setrow)
    if request.reason in (
        ResultCheckEndReason.STOP_UNCONFIRMED_CLOSED,
        ResultCheckEndReason.BINDING_UNAVAILABLE,
    ):
        return _prepare_external(db, request, setrow)
    if request.reason in _NATIVE_REASONS:
        return _prepare_native(db, request, setrow)
    action = row_facts(db, 'actions', request.ref.action_id)
    from camctl.devices.tasks import CaptureResultPolicy
    action6_assessed = (
        action['execution_spec_json'].get('result_policy')
        == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
        and setrow['source_kind'] == encode_enum(SegmentSourceKind.COMPLETE_SCAN)
        and setrow['state'] == int(enum_for('segment_sets.state').ASSESSED)
    )
    if (
        request.reason not in _CURRENT_REASONS
        or setrow["source_kind"]
        not in (
            encode_enum(SegmentSourceKind.RETAINED_SOURCES),
            encode_enum(SegmentSourceKind.COMPLETE_SCAN),
        )
        or setrow["publication_kind"] != encode_enum(PublicationKind.RESULT)
        or setrow["state"] != int(enum_for("segment_sets.state").MEMBERS_FIXED)
        and not action6_assessed
        or request.prior_run_end_event_id is not None
        or request.binding_failure is not None
        or request.prior_conclusion_id is not None
        or request.check_config is None
    ):
        raise ConsistencyError('该有限决定不属于已实现的原RESULTS无可靠当前扫描耗尽分区')
    action=row_facts(db,'actions',request.ref.action_id)
    run=row_facts(db,'operation_runs',setrow['run_id'])
    if (action['status']!=int(enum_for('actions.status').RUNNING) or action['execution_started']!=1
            or action['cancel_requested'] or run is None or run['action_id']!=action['id']
            or run['activity_id']!=setrow['activity_id']
            or run['kind']!=int(enum_for('operation_runs.kind').CHECK_CAPTURE_RESULTS)
            or run['status'] not in (int(enum_for('operation_runs.status').ACTIVE),int(enum_for('operation_runs.status').PENDING))):
        raise ConsistencyError('原RESULTS不具备运行中且可有限收场的责任')
    last=_ended_attempts(db,run,HistoryBoundary(setrow['source_txn_id'],setrow['source_event_id']))
    from .segment_current import current_decision

    reason, current_result, current_failure = current_decision(db, setrow)
    if request.reason is not reason:
        raise ConsistencyError("结束原因与最后实际扫描的独立结论不同")
    config=request.check_config
    if (
        request.anchor_event_id != last
        or config.timeout_s is None
        or config.retry_interval_s is None
        or reason is ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED
        and run["attempts_used"] < config.max_attempts
    ):
        raise ConsistencyError('实际最后结果、完整采用配置或耗尽资格不成立')
    desired=dict(zip(_CONFIG_COLUMNS,(config.max_attempts,config.timeout_s,config.retry_interval_s)))
    changed={name:value for name,value in desired.items() if not json_equal(run[name],value)}
    prefix=[]
    if changed:prefix.append((_CONFIGURE,update_change('operation_runs',run['id'],
        {name:run[name] for name in changed},changed)))
    from .capture import result_run_close_row

    status, error = _close_values(run, current_failure, reason)
    prefix.append((_FINISH, result_run_close_row(run, status, error)))
    values = dict(
        set_id=request.ref.set_id,
        reason=encode_enum(request.reason),
        anchor_event_id=last,
        prior_run_end_event_id=None,
        config_event_id=None if changed else _adopted_config_event(db, run),
        prior_conclusion_id=None,
        current_result=encode_enum(current_result),
        current_failure_json=None
        if current_failure is None
        else {"code": current_failure.code, "details": current_failure.details},
    )
    return setrow,run,tuple(prefix),values


def _events(db,request,prepared,transaction):
    _,_,prefix,values=prepared
    if transaction.last_event_id-transaction.first_event_id!=len(prefix):
        raise ConsistencyError('有限决定的完整事务范围与适用配置和关闭不同')
    events=[event_envelope(transaction.first_event_id+offset,transaction.txn_id,_RUN_EVENT['id'],reason,
        (row,),request.occurred_at) for offset,(reason,row) in enumerate(prefix)]
    if prefix and prefix[0][0]==_CONFIGURE:values={**values,'config_event_id':transaction.first_event_id}
    events.append(event_envelope(transaction.last_event_id,transaction.txn_id,_EVENT['id'],_REASON,
        (row_change('segment_check_conclusions',next_row_id(db,'segment_check_conclusions'),
            {**values,'created_event_id':transaction.last_event_id}),),request.occurred_at,
        {'result_check_decision':document(request)}))
    return tuple(events)


class ResultCheckGroup:
    """原内核以同连接推导并封存至多三条规范事件。"""
    def __init__(self,scope,transaction,connection):
        if not isinstance(scope,ResultCheckWriteScope):raise ConsistencyError('有限决定缺少完整原申请范围')
        self.request,self.transaction,self.connection=scope.request,transaction,connection
        self.events=_events(connection,self.request,_prepare(connection,self.request),transaction)
        self.position=0;self.pending=None

    def bind(self,context,event):
        if (context.transaction!=self.transaction or not isinstance(context.current_reads.scope,SegmentReadScope)
                or context.current_reads.scope.ref!=self.request.ref
                or self.pending is not None or self.position>=len(self.events)
                or replace(event,change_seq=None)!=self.events[self.position]):
            raise ConsistencyError('有限决定组与原连接实际配置、关闭或决定顺序不同')
        self.pending=event

    def verify_decision(self,ref,event):
        if (ref!=self.request.ref or event.event_id!=self.transaction.last_event_id
                or self.pending is None or replace(event,change_seq=None)!=self.events[-1]):
            raise ConsistencyError('有限决定不是原完整组的实际末事件')

    def seal(self,validated):
        if self.pending is None or replace(validated.envelope,change_seq=None)!=self.events[self.position]:
            raise ConsistencyError('有限决定组缺少全部守卫通过的规范事件')
        self.pending=None;self.position+=1

    def finish(self):
        if self.pending is not None or self.position!=len(self.events):
            raise ConsistencyError('有限决定组缺少适用配置、关闭或唯一末决定')


def verify_conclusion_event(db,ref,event,context):
    request=_request(event.evidence['result_check_decision'])
    group=getattr(context.current_reads,'_result_check_group',None)
    if not isinstance(group,ResultCheckGroup) or request!=group.request:
        raise ConsistencyError('有限决定缺少原内核可靠完整组')
    group.verify_decision(ref,event)


def verify_result_check_conclusion(request,key,owned,*,execution_finished=True):
    command=ConcludeResultCheckCommand(request,key)
    if not execution_finished:raise ConsistencyError('原有限决定保存尚未收场，不能核实缺键')
    with _read_transaction(owned) as db:
        if saved_transaction_events(db,OperationKey(key)) is None:return None
        boundary=_boundary(db)
        scope=TransactionScope(db,boundary.txn_id,boundary.last_event_id)
        plan=command.plan(scope)
        if not plan.read_only or scope.allocation is not None:
            raise ConsistencyError('原有限决定核实不能产生新写入')
        return plan.result


def read_fixed_result_check_conclusion(db, setrow):
    """读取集合已保存的不可变结论及其真实创建版本，不重演结论申请。"""
    with closing(db.execute("SELECT * FROM segment_check_conclusions WHERE set_id=?", (setrow["id"],))) as cursor:
        found = cursor.fetchone()
        if found is None:
            raise ConsistencyError("结果发布缺少已保存的有限核实结论")
        conclusion = {column[0]: _read_sql_value("segment_check_conclusions", column[0], value)
                      for column, value in zip(cursor.description, found)}
    if conclusion["set_id"] != setrow["id"]:
        raise ConsistencyError("已保存结论不属于原固定集合")
    from camctl.persistence.fact_reads import read_event
    from camctl.history.events import business_columns

    event = read_event(db, conclusion["created_event_id"]).event
    expected = row_change("segment_check_conclusions", conclusion["id"], {
        name: conclusion[name] for name in business_columns("segment_check_conclusions")
    })
    if (event.event_type != _EVENT["id"] or event.reason != _REASON
            or event.rows != (expected,)):
        raise ConsistencyError("已保存结论与实际不可变创建版本不同")
    return conclusion


def read_verified_conclusion(db, setrow):
    """按决定的原完整申请核实配置、真实轮次及责任关闭。"""
    return _load_verified_conclusion(db, setrow)


def _load_verified_conclusion(db, setrow):
    from types import SimpleNamespace
    from camctl.history.decoding import decode_event_row
    from .result_pages import _COLUMNS

    found = _one(
        db,
        "SELECT id,created_event_id FROM segment_check_conclusions WHERE set_id=?",
        (setrow["id"],),
    )
    if found is None:
        raise ConsistencyError("结果发布缺少原有限核实结束决定")
    raw = _one(db, f"SELECT {_COLUMNS} FROM history_events WHERE id=?", (found[1],))
    event = decode_event_row(raw)
    (key,) = _one(
        db,
        "SELECT operation_key FROM history_transactions WHERE id=?",
        (event.transaction_id,),
    )
    verify_result_check_conclusion(
        _request(event.evidence["result_check_decision"]),
        key,
        SimpleNamespace(connection=db),
    )
    return row_facts(db, "segment_check_conclusions", found[0])


class ConcludeResultCheckCommand:
    def __init__(self,request,key):
        if not isinstance(request,ResultCheckConclusionSave):raise TypeError('有限决定需要完整原申请')
        self.request,self.key=request,OperationKey(key)

    def plan(self,scope):
        db,request=scope.connection,self.request
        evidence={'result_check_decision':document(request)}
        saved=saved_transaction_events(db,self.key)
        if saved is not None:
            event=saved[-1]
            if (not 1<=len(saved)<=3 or event['type']!=_EVENT['id'] or event['reason']!=_REASON
                    or event['occurred_at']!=request.occurred_at or not json_equal(event['body']['evidence'],evidence)
                    or len(event['body']['rows'])!=1):
                raise ConsistencyError('原有限决定的完整申请或事件不同')
            change=event['body']['rows'][0]
            row=row_facts(db,'segment_check_conclusions',change['id'])
            from camctl.history.events import business_columns
            if (change['table']!='segment_check_conclusions' or change['before']['exists']
                    or row is None or row['set_id']!=request.ref.set_id
                    or row['created_event_id']!=event['event_id']
                    or not json_equal({name:row[name] for name in business_columns('segment_check_conclusions')},change['after']['values'])):
                raise ConsistencyError('原有限决定与实际不可变保存行不同')
            _verify_saved_prefix(db,request,saved,row)
            return CommandPlan((),{},{},ResultCheckConclusionRef(request.ref,row['id'],event['event_id']),True)
        prepared=_prepare(db,request)
        setrow,run,prefix,_=prepared
        allocation=scope.allocate(len(prefix)+1)
        events=_events(db,request,prepared,allocation)
        identity=events[-1].rows[0].row_id
        owners={('segment_check_conclusions',identity):('action',request.ref.action_id)}
        state={'actions':{request.ref.action_id:row_facts(db,'actions',request.ref.action_id)},
            'segment_sets':{request.ref.set_id:setrow},'segment_check_conclusions':{}}
        if run is not None:
            owners[('operation_runs',run['id'])]=('action',request.ref.action_id)
            state['operation_runs']={run['id']:run}
            state['device_activities']={run['activity_id']:row_facts(db,'device_activities',run['activity_id'])}
        return CommandPlan(events,owners,state,
            ResultCheckConclusionRef(request.ref,identity,allocation.last_event_id),
            segment_read_scope=SegmentReadScope(request.ref),result_check_write_scope=ResultCheckWriteScope(request))


def _verify_saved_prefix(db,request,saved,conclusion):
    prefix=saved[:-1]
    class Reader:connection=db
    setrow=read_result_set(request.ref,Reader)
    if request.reason is ResultCheckEndReason.NO_RESULT_WORK:
        if prefix:raise ConsistencyError('原无RESULTS基准决定不能混入其他事件')
        if (conclusion["current_failure_json"] is not None
                or _preparation_run_for_anchor(db, setrow, request.anchor_event_id) is not None):
            failure = no_result_failure(db, setrow, request.anchor_event_id)
            if not json_equal(
                conclusion["current_failure_json"],
                {"code": failure.code, "details": failure.details},
            ):
                raise ConsistencyError("原无RESULTS决定改变了实际启动失败")
        return
    if request.reason in (
        ResultCheckEndReason.STOP_UNCONFIRMED_CLOSED,
        ResultCheckEndReason.BINDING_UNAVAILABLE,
    ):
        failure = _external_failure(db, request, setrow)
        expected = {"code": failure.code, "details": failure.details}
        if not json_equal(conclusion["current_failure_json"], expected):
            raise ConsistencyError("原外部失败决定改变实际失败输入")
        if not prefix:
            if setrow["run_id"] is not None:
                raise ConsistencyError("已有 RESULTS 的外部失败决定缺少完整关闭")
            return
    if request.reason is ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION:
        _check_canceled_request(db, request, setrow)
        if not prefix:
            run = (
                None
                if setrow["run_id"] is None
                else row_facts(db, "operation_runs", setrow["run_id"])
            )
            if run is not None and (
                run["status"] in (1, 2) or request.prior_run_end_event_id is None
            ):
                raise ConsistencyError("取消决定缺少原已关闭 RESULTS 引用")
            return
    external = request.reason in (
        ResultCheckEndReason.STOP_UNCONFIRMED_CLOSED,
        ResultCheckEndReason.BINDING_UNAVAILABLE,
    )
    native = request.reason in _NATIVE_REASONS
    if (
        request.reason
        not in _CURRENT_REASONS
        | _NATIVE_REASONS
        | {
            ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION,
            ResultCheckEndReason.STOP_UNCONFIRMED_CLOSED,
            ResultCheckEndReason.BINDING_UNAVAILABLE,
        }
        or len(prefix) not in (1, 2)
        or request.check_config is None
    ):
        raise ConsistencyError('原有限决定不是已实现的完整组')
    run=row_facts(db,'operation_runs',setrow['run_id'])
    config=request.check_config
    desired=dict(zip(_CONFIG_COLUMNS,(config.max_attempts,config.timeout_s,config.retry_interval_s)))
    if run is None or any(not json_equal(run[name],value) for name,value in desired.items()):
        raise ConsistencyError('原有限决定的实际采用配置不同')
    for event in prefix:
        rows=event['body']['rows']
        if (event['type']!=_RUN_EVENT['id'] or event['occurred_at']!=request.occurred_at
                or event['body']['evidence'] or len(rows)!=1 or rows[0]['table']!='operation_runs'
                or rows[0]['id']!=run['id'] or not rows[0]['before']['exists'] or not rows[0]['after']['exists']):
            raise ConsistencyError('原有限决定的前序事件身份或完整行不同')
    if len(prefix)==2:
        event=prefix[0];row=event['body']['rows'][0]
        before,after=row['before']['values'],row['after']['values']
        if (event['reason']!=_CONFIGURE or not after or set(before)!=set(after)
                or not set(after)<=set(_CONFIG_COLUMNS)
                or any(not json_equal(value,desired[name]) or json_equal(value,before[name]) for name,value in after.items())
                or conclusion['config_event_id']!=event['event_id']):
            raise ConsistencyError('原有限决定的配置事件不等于原完整申请')
    elif (conclusion['config_event_id'] is None or conclusion['config_event_id']>=prefix[0]['event_id']
            or conclusion['config_event_id']!=_adopted_config_event(db,run)):
        raise ConsistencyError('复用配置缺少原先实际采用事件')
    if (
        not external
        and request.reason is not ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION
        and _ended_attempts(
            db, run, HistoryBoundary(setrow["source_txn_id"], setrow["source_event_id"])
        )
        != request.anchor_event_id
        or request.reason is ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED
        and run["attempts_used"] < config.max_attempts
        or conclusion["anchor_event_id"] != request.anchor_event_id
    ):
        raise ConsistencyError('原有限决定的实际结果或已采用耗尽资格不同')
    event=prefix[-1];row=event['body']['rows'][0]
    from .segment_current import current_decision
    from .segment_native import native_classification

    action = row_facts(db, "actions", setrow["action_id"])
    from camctl.devices.tasks import CaptureResultPolicy

    action6_native = (
        native
        and action["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
    )
    if action6_native:
        from camctl.capture.action6_completion import action6_decision_failure
        from camctl.capture.segment_models import SegmentDecision, SegmentDecisionStatus
        from camctl.capture.segment_storage import assessment_from_document

        assessment = (
            None
            if setrow["assessment_json"] is None
            else assessment_from_document(setrow["assessment_json"])
        )
        if not isinstance(assessment, SegmentDecision):
            raise ConsistencyError("Action6 原生有限结论缺少已保存的 SegmentDecision")
        current_result = (
            CurrentSegmentResult.SUFFICIENT
            if assessment.status is SegmentDecisionStatus.SUCCEEDED
            else CurrentSegmentResult.UNMET
            if assessment.status is SegmentDecisionStatus.FAILED
            else CurrentSegmentResult.UNCONFIRMED
        )
        current_failure = action6_decision_failure(setrow["activity_id"], assessment)
        classified = None
    else:
        classified = native_classification(db, setrow) if native else None
    reason, current_result, current_failure = (
        (request.reason, current_result, current_failure)
        if action6_native
        else (request.reason, classified.current_result, classified.failure)
        if native
        else (
            request.reason,
            CurrentSegmentResult.UNCONFIRMED,
            _external_failure(db, request, setrow),
        )
        if external
        else (
            ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION,
            CurrentSegmentResult.NONE,
            None,
        )
        if request.reason is ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION
        else current_decision(db, setrow)
    )
    if (
        reason is not request.reason
        or conclusion["current_result"] != encode_enum(current_result)
        or not json_equal(
            conclusion["current_failure_json"],
            None
            if current_failure is None
            else {"code": current_failure.code, "details": current_failure.details},
        )
    ):
        raise ConsistencyError("原当前扫描结论或明确错误已改变")
    status, error = _close_values(run, current_failure, reason, current_result)
    expected = {"status": status, "retry_wait_required": 0, "error_json": error}
    before, after = row["before"]["values"], row["after"]["values"]
    if (
        event["reason"] != _FINISH
        or set(before) != set(after)
        or not {"status"} <= set(after) <= set(expected)
        or row["before"]["values"]["status"] not in (1, 2)
        or any(
            not json_equal(value, expected[name]) or json_equal(value, before[name])
            for name, value in after.items()
        )
        or any(not json_equal(run[name], value) for name, value in expected.items())
    ):
        raise ConsistencyError('原有限决定缺少原RESULTS的可靠完整关闭')


def _close_values(run, failure, reason, current_result=None):
    if reason is ResultCheckEndReason.CANCELED_BEFORE_PUBLICATION or (
        reason is ResultCheckEndReason.NATIVE_FINITE_CANCEL_END
        and current_result is not CurrentSegmentResult.SUFFICIENT
    ):
        return int(enum_for("operation_runs.status").CANCELED), None
    if (
        reason
        in (
            ResultCheckEndReason.CONTROL_SUFFICIENT,
            ResultCheckEndReason.SEGMENTS_SUFFICIENT,
        )
        or reason in _NATIVE_REASONS
        and current_result is CurrentSegmentResult.SUFFICIENT
    ):
        return int(enum_for("operation_runs.status").SUCCEEDED), None
    if failure is None:
        from .capture import _unconfirmed_run_error

        return int(
            enum_for("operation_runs.status").UNCONFIRMED
        ), _unconfirmed_run_error(run["activity_id"])
    from camctl.contracts.workflow_errors import registered_error

    return int(enum_for("operation_runs.status").FAILED), {
        "code": failure.code,
        "stage": registered_error(failure.code)["stage"],
        "details": failure.details,
    }


def _check_canceled_request(db, request, setrow):
    from camctl.history.decoding import decode_event_row
    from .result_pages import _COLUMNS

    if (
        setrow["publication_kind"] != encode_enum(PublicationKind.CANCELED)
        or request.anchor_event_id != setrow["cancel_event_id"]
        or request.binding_failure is not None
    ):
        raise ConsistencyError("取消决定必须引用原取消版本及实际请求")
    event = decode_event_row(
        _one(
            db,
            f"SELECT {_COLUMNS} FROM history_events WHERE id=?",
            (request.anchor_event_id,),
        )
    )
    if not any(
        row.table == "actions"
        and row.row_id == setrow["action_id"]
        and row.before.values.get("cancel_requested") == 0
        and row.after.values.get("cancel_requested") == 1
        for row in event.rows
    ):
        raise ConsistencyError("取消决定缺少原动作实际取消事件")
    if request.prior_run_end_event_id is not None:
        prior = decode_event_row(
            _one(
                db,
                f"SELECT {_COLUMNS} FROM history_events WHERE id=?",
                (request.prior_run_end_event_id,),
            )
        )
        if not any(
            row.table == "operation_runs"
            and row.row_id == setrow["run_id"]
            and row.before.values.get("status") in (1, 2)
            and row.after.values.get("status") in (3, 4, 5, 6, 7)
            for row in prior.rows
        ):
            raise ConsistencyError("取消复用的 RESULTS 关闭不是原实际结束事件")
    if request.prior_conclusion_id is not None:
        previous = row_facts(
            db, "segment_check_conclusions", request.prior_conclusion_id
        )
        if previous is None or previous["set_id"] != setrow["predecessor_set_id"]:
            raise ConsistencyError("取消决定未引用实际前版决定")


def _prepare_canceled(db, request, setrow):
    _check_canceled_request(db, request, setrow)
    action = row_facts(db, "actions", setrow["action_id"])
    if (
        action["status"] != 2
        or not action["cancel_requested"]
        or setrow["state"] != 2
        or _one(
            db,
            "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id WHERE r.action_id=? AND t.status=1 LIMIT 1",
            (action["id"],),
        )
    ):
        raise ConsistencyError("取消决定须等待所有实际调用可靠结束并固定原成员")
    run = (
        None
        if setrow["run_id"] is None
        else row_facts(db, "operation_runs", setrow["run_id"])
    )
    prefix = []
    config_event = None
    if run is not None:
        if run["status"] in (1, 2):
            if (
                request.prior_run_end_event_id is not None
                or request.check_config is None
            ):
                raise ConsistencyError(
                    "运行中 RESULTS 取消须采用完整配置且不能声称已结束"
                )
            config = request.check_config
            desired = dict(
                zip(
                    _CONFIG_COLUMNS,
                    (config.max_attempts, config.timeout_s, config.retry_interval_s),
                )
            )
            if config.timeout_s is None or config.retry_interval_s is None:
                raise ConsistencyError("取消结果核实缺少完整采用配置")
            changed = {
                name: value
                for name, value in desired.items()
                if not json_equal(run[name], value)
            }
            if changed:
                prefix.append(
                    (
                        _CONFIGURE,
                        update_change(
                            "operation_runs",
                            run["id"],
                            {name: run[name] for name in changed},
                            changed,
                        ),
                    )
                )
            else:
                config_event = _adopted_config_event(db, run)
            from .capture import result_run_close_row

            prefix.append(
                (
                    _FINISH,
                    result_run_close_row(
                        run, int(enum_for("operation_runs.status").CANCELED), None
                    ),
                )
            )
        elif request.prior_run_end_event_id is None:
            raise ConsistencyError("原 RESULTS 已结束时须引用实际关闭事件")
    elif request.check_config is not None or request.prior_run_end_event_id is not None:
        raise ConsistencyError("无 RESULTS 的取消不能伪造配置或关闭")
    values = dict(
        set_id=request.ref.set_id,
        reason=encode_enum(request.reason),
        anchor_event_id=request.anchor_event_id,
        prior_run_end_event_id=request.prior_run_end_event_id,
        config_event_id=config_event,
        prior_conclusion_id=request.prior_conclusion_id,
        current_result=encode_enum(CurrentSegmentResult.NONE),
        current_failure_json=None,
    )
    return setrow, run, tuple(prefix), values


def _external_failure(db, request, setrow):
    """核对实际 STOP 关闭或原绑定失败输入；不改写原设备返回。"""
    from camctl.capture.media import RecordingFailure

    if (
        setrow["publication_kind"] != encode_enum(PublicationKind.RESULT)
        or request.prior_run_end_event_id is not None
        or request.prior_conclusion_id is not None
    ):
        raise ConsistencyError("外部收场决定不能伪造取消或前次 RESULTS 结束")
    action = row_facts(db, "actions", setrow["action_id"])
    if request.reason is ResultCheckEndReason.BINDING_UNAVAILABLE:
        failure = request.binding_failure
        if (
            failure is None
            or failure.code != "device_binding_unavailable"
            or failure.details.get("device_id") != action["device_id"]
            or failure.details.get("expected_driver_id") != action["driver_id"]
            or request.anchor_event_id is not None
        ):
            raise ConsistencyError("绑定失败输入必须保留原动作的设备及预期驱动身份")
        from camctl.contracts.workflow_errors import validate_error_details

        validate_error_details(failure.code, failure.details)
        return failure
    if request.binding_failure is not None or request.anchor_event_id is None:
        raise ConsistencyError("STOP 未确认须引用实际关闭事件")
    stop = _one(
        db,
        "SELECT id FROM operation_runs WHERE action_id=? AND kind=?",
        (action["id"], int(enum_for("operation_runs.kind").STOP)),
    )
    if stop is None:
        raise ConsistencyError("STOP 未确认缺少原责任")
    run = row_facts(db, "operation_runs", stop[0])
    from camctl.history.decoding import decode_event_row
    from .result_pages import _COLUMNS

    raw = _one(
        db,
        f"SELECT {_COLUMNS} FROM history_events WHERE id=?",
        (request.anchor_event_id,),
    )
    event = None if raw is None else decode_event_row(raw)
    error = run["error_json"]
    if (
        run["activity_id"] != setrow["activity_id"]
        or run["status"] != int(enum_for("operation_runs.status").UNCONFIRMED)
        or error is None
        or error["code"] != "recording_stop_failed"
        or event is None
        or not any(
            row.table == "operation_runs"
            and row.row_id == run["id"]
            and row.before.values.get("status") in (1, 2)
            and row.after.values.get("status") == run["status"]
            for row in event.rows
        )
    ):
        raise ConsistencyError("STOP 决定与原预算耗尽的实际未确认关闭不同")
    return RecordingFailure(error["code"], error["details"])


def _prepare_external(db, request, setrow):
    failure = _external_failure(db, request, setrow)
    action = row_facts(db, "actions", setrow["action_id"])
    if (
        action["status"] != 2
        or not action["execution_started"]
        or action["cancel_requested"]
        or setrow["state"] != 2
        or _one(
            db,
            "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.action_id=? AND t.status=1 LIMIT 1",
            (action["id"],),
        )
    ):
        raise ConsistencyError("外部收场必须等待原实际调用可靠结束并固定原观察")
    run = (
        None
        if setrow["run_id"] is None
        else row_facts(db, "operation_runs", setrow["run_id"])
    )
    prefix = []
    config_event = None
    if run is not None:
        if run["status"] not in (1, 2) or request.check_config is None:
            raise ConsistencyError("外部收场须接手原仍开放的 RESULTS 责任")
        config = request.check_config
        if config.timeout_s is None or config.retry_interval_s is None:
            raise ConsistencyError("外部结果收场缺少完整采用配置")
        desired = dict(
            zip(
                _CONFIG_COLUMNS,
                (config.max_attempts, config.timeout_s, config.retry_interval_s),
            )
        )
        changed = {
            name: value
            for name, value in desired.items()
            if not json_equal(run[name], value)
        }
        if changed:
            prefix.append(
                (
                    _CONFIGURE,
                    update_change(
                        "operation_runs",
                        run["id"],
                        {name: run[name] for name in changed},
                        changed,
                    ),
                )
            )
        else:
            config_event = _adopted_config_event(db, run)
        from .capture import result_run_close_row

        status, error = _close_values(run, failure, request.reason)
        prefix.append((_FINISH, result_run_close_row(run, status, error)))
    elif request.check_config is not None:
        raise ConsistencyError("没有 RESULTS 时不能补造配置")
    values = dict(
        set_id=setrow["id"],
        reason=encode_enum(request.reason),
        anchor_event_id=request.anchor_event_id,
        prior_run_end_event_id=None,
        config_event_id=config_event,
        prior_conclusion_id=None,
        current_result=encode_enum(CurrentSegmentResult.UNCONFIRMED),
        current_failure_json={"code": failure.code, "details": failure.details},
    )
    return setrow, run, tuple(prefix), values


def _prepare_native(db, request, setrow):
    from camctl.devices.tasks import CaptureResultPolicy
    from .segment_native import native_classification

    action = row_facts(db, "actions", setrow["action_id"])
    canceled = request.reason is ResultCheckEndReason.NATIVE_FINITE_CANCEL_END
    if (
        action["execution_spec_json"].get("result_policy")
        == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
    ):
        if (
            action["type"] != int(enum_for("actions.type").CAMERA_TIMELAPSE)
            or request.reason is not ResultCheckEndReason.NATIVE_RESULT_DECIDED
            or action["status"] != 2
            or action["cancel_requested"]
            or setrow["source_kind"]
            != encode_enum(SegmentSourceKind.COMPLETE_SCAN)
            or setrow["publication_kind"] != encode_enum(PublicationKind.RESULT)
            or setrow["state"] not in (2, int(enum_for("segment_sets.state").ASSESSED))
            or request.check_config is None
            or any(
                value is not None
                for value in (
                    request.prior_run_end_event_id,
                    request.prior_conclusion_id,
                    request.binding_failure,
                )
            )
        ):
            raise ConsistencyError("Action6 原生结果决定必须沿完整自然来源和活动资格保存")
        run = row_facts(db, "operation_runs", setrow["run_id"])
        last = _ended_attempts(
            db,
            run,
            HistoryBoundary(setrow["source_txn_id"], setrow["source_event_id"]),
        )
        activity = row_facts(db, "device_activities", setrow["activity_id"])
        config = request.check_config
        if (
            run["status"] not in (1, 2)
            or request.anchor_event_id != last
            or config.timeout_s is None
            or config.retry_interval_s is None
            or activity["wait_completed_event_id"] is None
        ):
            raise ConsistencyError("Action6 原生结果缺少实际 RESULTS 结束票或有限等待")
        from camctl.capture.action6_completion import (
            action6_decision_failure,
        )
        from camctl.capture.segment_storage import SegmentResultRef
        from .segment_result_sets import read_current_segment_ref
        from .segment_current import classify_action6_current_set
        from camctl.capture.segment_models import SegmentDecision, SegmentDecisionStatus
        from camctl.capture.segment_storage import assessment_from_document
        from types import SimpleNamespace

        result_ref = SegmentResultRef(
            setrow["action_id"], setrow["id"], setrow["version"]
        )
        reader = SimpleNamespace(connection=db)
        ref = read_current_segment_ref(result_ref, reader)
        if ref is None:
            raise ConsistencyError("Action6 原生完整扫描缺少当前冻结结果引用")
        stored = (
            None
            if setrow["assessment_json"] is None
            else assessment_from_document(setrow["assessment_json"])
        )
        decision = stored if isinstance(stored, SegmentDecision) else None
        if decision is None:
            decision = classify_action6_current_set(db, action, ref, setrow).decision
        status = decision.status
        current_result = (
            CurrentSegmentResult.SUFFICIENT
            if status is SegmentDecisionStatus.SUCCEEDED
            else CurrentSegmentResult.UNMET
            if status is SegmentDecisionStatus.FAILED
            else CurrentSegmentResult.UNCONFIRMED
        )
        failure = action6_decision_failure(setrow["activity_id"], decision)
        desired = dict(
            zip(
                _CONFIG_COLUMNS,
                (config.max_attempts, config.timeout_s, config.retry_interval_s),
            )
        )
        changed = {
            name: value
            for name, value in desired.items()
            if not json_equal(run[name], value)
        }
        prefix = []
        if changed:
            prefix.append(
                (
                    _CONFIGURE,
                    update_change(
                        "operation_runs",
                        run["id"],
                        {name: run[name] for name in changed},
                        changed,
                    ),
                )
            )
        else:
            config_event = _adopted_config_event(db, run)
        from .capture import result_run_close_row

        run_status, error = _close_values(
            run, failure, request.reason, current_result
        )
        prefix.append(
            (_FINISH, result_run_close_row(run, run_status, error))
        )
        values = dict(
            set_id=setrow["id"],
            reason=encode_enum(request.reason),
            anchor_event_id=last,
            prior_run_end_event_id=None,
            prior_conclusion_id=None,
            config_event_id=None if changed else config_event,
            current_result=encode_enum(current_result),
            current_failure_json=None
            if failure is None
            else {"code": failure.code, "details": failure.details},
        )
        return setrow, run, tuple(prefix), values
    if (
        action["type"] != 3
        or action["execution_spec_json"].get("result_policy")
        != CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1.value
        or action["status"] != 2
        or bool(action["cancel_requested"]) != canceled
        or setrow["publication_kind"] != encode_enum(PublicationKind.RESULT)
        or setrow["state"] != 2
        or request.check_config is None
        or any(
            value is not None
            for value in (
                request.prior_run_end_event_id,
                request.prior_conclusion_id,
                request.binding_failure,
            )
        )
    ):
        raise ConsistencyError("原生决定必须沿原策略、实际取消资格和完整 RESULTS 责任")
    run = row_facts(db, "operation_runs", setrow["run_id"])
    last = _ended_attempts(
        db, run, HistoryBoundary(setrow["source_txn_id"], setrow["source_event_id"])
    )
    classified = native_classification(db, setrow)
    config = request.check_config
    if (
        run["status"] not in (1, 2)
        or request.anchor_event_id != last
        or config.timeout_s is None
        or config.retry_interval_s is None
        or classified.current_result is CurrentSegmentResult.UNCONFIRMED
        and run["attempts_used"] < config.max_attempts
    ):
        raise ConsistencyError("原生核实须取得可靠分类或用尽原有限预算")
    desired = dict(
        zip(
            _CONFIG_COLUMNS,
            (config.max_attempts, config.timeout_s, config.retry_interval_s),
        )
    )
    changed = {
        name: value
        for name, value in desired.items()
        if not json_equal(run[name], value)
    }
    prefix = []
    if changed:
        prefix.append(
            (
                _CONFIGURE,
                update_change(
                    "operation_runs",
                    run["id"],
                    {name: run[name] for name in changed},
                    changed,
                ),
            )
        )
    from .capture import result_run_close_row

    status, error = _close_values(
        run, classified.failure, request.reason, classified.current_result
    )
    prefix.append((_FINISH, result_run_close_row(run, status, error)))
    values = dict(
        set_id=setrow["id"],
        reason=encode_enum(request.reason),
        anchor_event_id=last,
        prior_run_end_event_id=None,
        prior_conclusion_id=None,
        config_event_id=None if changed else _adopted_config_event(db, run),
        current_result=encode_enum(classified.current_result),
        current_failure_json=None
        if classified.failure is None
        else {"code": classified.failure.code, "details": classified.failure.details},
    )
    return setrow, run, tuple(prefix), values
