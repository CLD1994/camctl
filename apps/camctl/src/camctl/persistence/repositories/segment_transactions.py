"""分段完整收场：有限引用、原连接逐事件提交及原键逐页核实。"""
from contextlib import closing
from dataclasses import dataclass, replace
from types import SimpleNamespace
from itertools import zip_longest

from camctl.capture.segment_models import SegmentAssessmentStatus, DefaultScopeState
from camctl.capture.segment_finish import FinishSegmentRecording, SegmentFinishResult, validate_finish_header, request_document
from camctl.capture.segment_finish import (
    SavedSegmentCompletionProof,
    result_ref_from_document,
)
from camctl.capture.segment_storage import (
    document,
    assessment_from_document,
    facts_from_document,
    SegmentResultRef,
)
from camctl.capture.segment_errors import segment_error_header
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.history.events import load_event_registry, foreign_key_targets
from camctl.history.reads import PlanReadScope, ReadCoverage
from camctl.history.validators import EventContext
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.fact_reads import opened_operation_fact_scope
from camctl.persistence.transaction import (
    BoundedCommandPlan, CommandPlan, TransactionError, commit_operation, row_facts,
    row_change, update_change, event_envelope, next_row_id, read_transaction_range,
    read_saved_transaction_page,
)
from camctl.outputs.catalog import OutputKind
from . import segments as storage



def _one(db,sql,params=()):
    with closing(db.execute(sql,params)) as cursor:
        return cursor.fetchone()


def _verify_action6_inspections_finished(db, action, setrow, assessment):
    """来源成功前核对已建立的检查责任与其实际读取结果。"""
    from camctl.capture.segment_models import CoverageStatus
    from camctl.devices.tasks import CaptureResultPolicy
    from camctl.persistence.repositories.segment_inspections import (
        _result_requires_read, inspection_input_read_succeeded,
    )

    if action["execution_spec_json"].get("result_policy") != CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value:
        return
    cursor_id = 0
    while True:
        with closing(db.execute(
            "SELECT id FROM segment_inspections WHERE action_id=? AND set_id=? AND id>?"
            " ORDER BY id LIMIT 128", (action["id"], setrow["id"], cursor_id),
        )) as cursor:
            rows = cursor.fetchall()
        if not rows:
            return
        if assessment.coverage_status is CoverageStatus.NOT_PERFORMED:
            raise ConsistencyError("已建立分段检查责任不能按未检查发布")
        for (inspection_id,) in rows:
            inspection = row_facts(db, "segment_inspections", inspection_id)
            if inspection is None or inspection["state"] == int(enum_for("segment_inspections.state").PENDING):
                raise ConsistencyError("来源成功前分段检查必须结束")
            result = inspection["result_json"]
            if result is None:
                raise ConsistencyError("已结束分段检查缺少结果")
            if (_result_requires_read(inspection["state"], result)
                    and not inspection_input_read_succeeded(db, inspection)):
                raise ConsistencyError("来源成功前检查副本的原读取必须可靠成功")
        cursor_id = rows[-1][0]


def _publishes_candidates(action, setrow):
    if setrow["publication_kind"] != int(enum_for("segment_sets.publication_kind").CANCELED):
        return True
    from camctl.devices.tasks import CaptureResultPolicy
    return (action["execution_spec_json"].get("result_policy")
            == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value
            and action["type"] == int(enum_for("actions.type").CAMERA_TIMELAPSE))


def _candidate_facts(request,reader,batch=128):
    setrow=storage._set(reader.connection,request.ref)
    canceled=setrow["publication_kind"]==int(enum_for("segment_sets.publication_kind").CANCELED)
    action=row_facts(reader.connection,"actions",request.ref.action_id) if canceled else None
    if not _publishes_candidates(action,setrow):
        return
    cursor=None
    while True:
        page=storage.read_segment_finish_candidates(request.ref,cursor,batch,reader)
        yield from page.items
        if page.next_cursor is None:return
        cursor=page.next_cursor


def _candidates(request,reader,batch=128):
    yield from (saved.candidate for saved in _candidate_facts(request,reader,batch))


def _scopes(request,reader,batch=128):
    if isinstance(request.ref, SegmentResultRef):
        return
    cursor=None
    while True:
        page=storage.read_segment_scope(request.ref,cursor,batch,reader,published=False)
        yield page.items
        if page.next_cursor is None:return
        cursor=page.next_cursor


def _verify_scope_members(request,assessment,reader):
    """将正式关联与冻结范围的实际有序原片逐项对应，不能只比计数。"""
    selected=assessment.default_scope
    def expected():
        if selected.state is not DefaultScopeState.NONEMPTY:return
        cursor=None
        while True:
            page=storage.read_segment_members(request.ref,cursor,128,reader)
            for stored in page.items:
                member=stored.member
                if member.cursor is None or not selected.first_cursor<=member.cursor<=selected.last_cursor:continue
                candidate=storage.segment_output_candidate(request.ref,stored.member_id,reader)
                if candidate is not None and candidate.kind is OutputKind.ORIGINAL:yield candidate.member_id
            if page.next_cursor is None:return
            cursor=page.next_cursor
    actual=(row for page in _scopes(request,reader) for row in page)
    count=0
    for member_id,row in zip_longest(expected(),actual):
        count+=1
        if row is None or member_id is None or row['member_id']!=member_id or row['ordinal']!=count:
            raise ConsistencyError('默认范围没有完整覆盖冻结选段的实际有序原片')
    if count!=(selected.member_count or 0):raise ConsistencyError('冻结选段计数与实际成员不同')


@dataclass(frozen=True)
class _FailureSources:
    canceled: bool = False
    conclusion: dict | None = None
    anchor_owned: bool = False
    error_rows: tuple = ()


def _read_failure_sources(request, assessment, setrow, db, conclusion):
    """取得本次终态判断直接依赖的有限记录，不执行旧决定算法。"""
    from camctl.capture.segment_models import SegmentAssessmentReason, SegmentDecision, SegmentDecisionStatus
    if isinstance(request.ref, SegmentResultRef):
        return _FailureSources(conclusion=conclusion)
    if isinstance(assessment, SegmentDecision):
        return _FailureSources(canceled=(assessment.status is SegmentDecisionStatus.CANCELED
            and _canceled_result(db,request.ref)))
    if assessment.reason is not SegmentAssessmentReason.EXPLICIT_ERROR or request.failure_event_id is None:
        return _FailureSources()
    from camctl.contracts.enums import load_registry
    from camctl.persistence.fact_reads import read_event
    owner=load_registry()['history_objects']['action']['id']
    anchor=request.failure_event_id
    owned=_one(db,'SELECT 1 FROM entity_event_links WHERE entity_type=? AND entity_id=? AND event_id=?',
        (owner,request.ref.action_id,anchor)) is not None
    if not owned:
        return _FailureSources()
    event=read_event(db,anchor).event
    registry=load_event_registry()['events']
    rows=[]
    for changed in event.rows:
        source_attempt=None
        if changed.table=='operation_runs' and event.event_type==registry['OPERATION_CONFIGURED']['id'] and event.reason==3:
            source_run=row_facts(db,'operation_runs',changed.row_id)
        elif changed.table=='operation_attempts' and event.event_type==registry['ATTEMPT_RESULT']['id']:
            source_attempt=row_facts(db,'operation_attempts',changed.row_id)
            if source_attempt is None or source_attempt['result_event_id']!=anchor:
                continue
            source_run=row_facts(db,'operation_runs',source_attempt['run_id'])
        else:
            continue
        rows.append((changed,source_run,source_attempt))
    return _FailureSources(anchor_owned=True,error_rows=tuple(rows))


def _failure(request,assessment,original_count,setrow,sources,*,before_event_id):
    if isinstance(request.ref, SegmentResultRef):
        from .segment_dispositions import saved_result_failure

        return saved_result_failure(request, setrow, sources.conclusion)
    from camctl.capture.segment_models import SegmentAssessmentReason, SegmentDecision, SegmentDecisionStatus
    if isinstance(assessment, SegmentDecision):
        if assessment.status is SegmentDecisionStatus.SUCCEEDED:
            if request.failure is not None:
                raise ConsistencyError("Action6 成功判断不能携带失败申请")
            return 3, None, None
        if assessment.status is SegmentDecisionStatus.CANCELED:
            if request.failure is not None or not sources.canceled:
                raise ConsistencyError("Action6 取消判断必须引用实际取消结果且不带失败")
            return 6, None, None
        if request.failure is None:
            raise ConsistencyError("Action6 失败或未确认判断必须保存原实际失败")
        if assessment.failure_code != request.failure.code:
            raise ConsistencyError("Action6 完成失败改变了冻结决策错误代码")
        if assessment.status is SegmentDecisionStatus.FAILED:
            expected = {
                "activity_id": str(request.activity_id),
                "reason": assessment.reason
                if assessment.reason in ("no_outputs", "invalid_outputs")
                else "invalid_outputs",
            }
            if request.failure.code != "capture_failed" or not json_equal(request.failure.details, expected):
                raise ConsistencyError("Action6 明确产物失败与冻结决策不同")
        elif assessment.status is SegmentDecisionStatus.UNCONFIRMED:
            expected = {
                "activity_id": str(request.activity_id),
                "reason": "completion_unknown"
                if assessment.reason == "completion_unknown"
                else "outputs_unknown",
            }
            if request.failure.code != "capture_result_unconfirmed" or not json_equal(request.failure.details, expected):
                raise ConsistencyError("Action6 未确认失败与冻结决策不同")
        return 4, registered_error(request.failure.code)["action_error_id"], request.failure.details
    facts=facts_from_document(setrow['facts_json'])
    failure=request.failure
    if assessment.status is SegmentAssessmentStatus.SUCCEEDED:
        if failure is not None:raise ConsistencyError('成功判断不能携带失败申请')
        return 3,None,None
    if failure is None:raise ConsistencyError('非成功完成必须保存原实际失败')
    reason=assessment.reason
    if reason is SegmentAssessmentReason.EXPLICIT_ERROR:
        anchor=request.failure_event_id
        if anchor is None or anchor>=before_event_id:
            raise ConsistencyError('明确错误必须引用完成组之前已保存的实际错误事件')
        if not sources.anchor_owned:
            raise ConsistencyError('原错误事件不属于来源动作')
        # 只接受操作责任/实际尝试的明确错误列，不搜索任意嵌套正文。
        matched=False
        for changed,source_run,source_attempt in sources.error_rows:
            error=changed.after.values.get('error_json')
            if (source_run is not None and source_run['action_id']==request.ref.action_id
                    and source_run['activity_id']==request.activity_id and isinstance(error,dict)
                    and ('activity_id' not in failure.details or failure.details['activity_id']==str(request.activity_id))
                    and ('operation_run_id' not in failure.details or failure.details['operation_run_id']==str(source_run['id']))
                    and error.get('code')==failure.code
                    and error.get('stage')==registered_error(failure.code)['stage']
                    and json_equal(error.get('details'),failure.details)):
                matched=True
            # 实际采集调用明确失败的既有公共包装：原实际错误仍只在
            # attempt.error_json，完成申请保存capture_failed/device_failed。
            if (source_attempt is not None and source_run is not None
                    and source_run['action_id']==request.ref.action_id and source_run['activity_id']==request.activity_id
                    and source_run['kind'] in tuple(int(enum_for('operation_runs.kind')[kind]) for kind in ('START','STOP','CHECK_CAPTURE_RESULTS'))
                    and source_attempt['status']==int(enum_for('operation_attempts.status').FAILED)
                    and isinstance(error,dict) and isinstance(error.get('code'),str)
                    and failure.code=='capture_failed'
                    and json_equal(failure.details,{'activity_id':str(request.activity_id),'reason':'device_failed'})):
                matched=True
        if not matched:raise ConsistencyError('明确错误与原run/attempt明确错误列的实际登记错误不同')
    elif reason is SegmentAssessmentReason.GAP:
        expected=segment_error_header(assessment,original_count).details('capture_segment_gap')
        if failure.code!='capture_segment_gap' or not json_equal(failure.details,expected):raise ConsistencyError('缺口错误与完整冻结判断不同')
    elif reason is SegmentAssessmentReason.NO_NONZERO_ORIGINAL:
        expected={'activity_id':str(request.activity_id),'reason':'no_outputs' if setrow['member_count']==0 else 'invalid_outputs'}
        if failure.code!='capture_failed' or not json_equal(failure.details,expected):raise ConsistencyError('必要原片缺失错误与实际冻结集合不同')
    else:
        if not facts.finite_check_complete:raise ConsistencyError('原有限核实责任尚未结束')
        completion_unknown=reason in (SegmentAssessmentReason.STOP_UNCONFIRMED,
            SegmentAssessmentReason.COMPLETION_UNCONFIRMED,SegmentAssessmentReason.LOWER_BOUND_INSUFFICIENT)
        expected={'activity_id':str(request.activity_id),'reason':'completion_unknown' if completion_unknown else 'outputs_unknown'}
        if failure.code!='capture_result_unconfirmed' or not json_equal(failure.details,expected):raise ConsistencyError('未确认结果与原有限核实分区不同')
    if reason is not SegmentAssessmentReason.EXPLICIT_ERROR and request.failure_event_id is not None:
        raise ConsistencyError('派生判断不接受无关原错误事件')
    return 4,registered_error(failure.code)['action_error_id'],failure.details


def _identity(request,db):
    """取得固定集合的直接身份和已保存决定，不重演事实形成。"""
    identity = storage.read_segment_identity(db, request.ref, fixed=True)
    action, activity, run, setrow = identity.action, identity.activity, identity.run, identity.setrow
    if ((None if activity is None else activity["id"]) != request.activity_id or setrow["activity_id"] != request.activity_id
            or setrow["run_id"] != request.run_id or (None if run is None else run["id"]) != request.run_id):
        raise ConsistencyError("完成申请改变原活动或RESULTS身份")
    if isinstance(request.ref, SegmentResultRef):
        from .segment_dispositions import saved_result_disposition
        from .segment_check_conclusions import read_fixed_result_check_conclusion
        conclusion = read_fixed_result_check_conclusion(db, setrow)
        assessment = saved_result_disposition(setrow, conclusion)
        _verify_saved_gap_count(db, setrow, assessment)
        return action, run, setrow, assessment, conclusion
    storage._check_write_kind(db, setrow, "ACTIVATE")
    if setrow["assessment_json"] is None:
        raise ConsistencyError("完成申请缺少已保存分段判断")
    from camctl.persistence.fact_reads import read_event
    created = read_event(db, setrow["created_event_id"]).event
    conclusion = None
    if created.reason == storage._EVENT["branches"]["CREATE_RESULT"]["reason"]:
        from .segment_check_conclusions import read_fixed_result_check_conclusion
        conclusion = read_fixed_result_check_conclusion(db, setrow)
    assessment = assessment_from_document(setrow["assessment_json"])
    _verify_saved_gap_count(db, setrow, assessment)
    return action, run, setrow, assessment, conclusion


def _verify_saved_gap_count(db, setrow, assessment):
    """固定决定消费时核实际缺口数量，不重算已保存的缺口分类。"""
    from camctl.capture.segment_models import SegmentDecision
    count = _one(db, "SELECT COUNT(*) FROM segment_gaps WHERE set_id=?", (setrow["id"],))[0]
    expected = 0 if isinstance(assessment, SegmentDecision) else (assessment.gap_count or 0)
    if count != expected:
        raise ConsistencyError("固定集合缺口数量与已保存决定不同")


def verify_assessment_sources(setrow, facts, reader):
    """形成判断时核源；无当前扫描时核已保存结论，不读取待保存判断。"""
    ref = storage._ref(setrow)
    if isinstance(ref, SegmentResultRef):
        from .segment_check_conclusions import read_verified_conclusion
        read_verified_conclusion(reader.connection, setrow)
        return
    verify_completion_proof(FinishSegmentRecording(
        ref, setrow["activity_id"], setrow["run_id"], 0), reader, facts=facts)


def verify_completion_proof(request,reader,*,facts=None):
    """只采用原scan意图之前的实际控制和原页等待，不升级未知标志。"""
    if isinstance(request.ref, SegmentResultRef):
        from .segment_dispositions import verify_retained_disposition_sources

        verify_retained_disposition_sources(request.ref, reader.connection)
        return SavedSegmentCompletionProof(
            storage._set(reader.connection, request.ref)["source_event_id"]
        )
    setrow = storage._set(reader.connection, request.ref, fixed=True)
    action = row_facts(reader.connection, "actions", request.ref.action_id)
    from camctl.devices.tasks import CaptureResultPolicy
    if (action["execution_spec_json"].get("result_policy")
            == CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value):
        from .result_sources import ResultSourceKind, load_result_source
        from camctl.devices.recording_completion import completed_file_wait_source
        from .result_pages import _at

        _, activity, run, attempt, saved = storage._source(
            reader.connection, request.ref
        )
        source = load_result_source(reader.connection, saved.ref.ticket)
        canceled = _canceled_result(reader.connection, request.ref)
        spec = action["execution_spec_json"]
        if action["type"] == int(enum_for("actions.type").CAMERA_RECORD):
            if source.kind is not ResultSourceKind.STOP:
                raise ConsistencyError("Action6 普通录像完成缺少同一活动的可靠STOP来源")
        elif canceled:
            if source.kind not in (ResultSourceKind.STOP, ResultSourceKind.NATURAL):
                raise ConsistencyError("Action6 原生延时取消缺少可靠结束来源")
        elif source.kind is not ResultSourceKind.NATURAL or source.context is None:
            raise ConsistencyError("Action6 原生延时正常完成缺少可靠自然等待来源")
        if source.kind is ResultSourceKind.STOP:
            required_wait_ms = spec.get("file_completion_wait_ms")
            if (not source.requires_file_wait or source.context is None
                    or type(required_wait_ms) is not int or required_wait_ms <= 0):
                raise ConsistencyError("Action6 STOP 来源没有冻结的正文件等待规则")
            final, _ = _at(reader.connection, setrow["current_final_page_event_id"])
            settlement = final.page.outcome.settlement
            data = (
                None
                if settlement is None
                else settlement.evidence.data
            )
            source_page = None
            source_page_id = None if data is None else data.get(
                "file_completion_source_page_event_id"
            )
            if source_page_id is not None:
                if type(source_page_id) is not int or source_page_id >= final.ref.event_id:
                    raise ConsistencyError("Action6 文件等待复用页不是更早的原结果页")
                previous, _ = _at(reader.connection, source_page_id)
                if previous.ref.ticket.run_id != saved.ref.ticket.run_id:
                    raise ConsistencyError("Action6 文件等待复用页不属于相同 RESULTS 责任")
                previous_settlement = previous.page.outcome.settlement
                source_page = {
                    "event_id": source_page_id,
                    "activity_id": str(activity["id"]),
                    "data": (
                        None
                        if previous_settlement is None
                        else previous_settlement.evidence.data
                    ),
                }
            try:
                completed_file_wait_source(
                    data,
                    page_event_id=final.ref.event_id,
                    activity_id=str(activity["id"]),
                    stop_result_event_id=source.context["stop_result_event_id"],
                    required_wait_ms=required_wait_ms,
                    source_page=source_page,
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ConsistencyError("Action6 STOP 来源缺少真实完成的文件等待") from error
        elif source.kind is ResultSourceKind.NATURAL:
            if (source.context is None
                    or source.context.get("wait_completed_event_id")
                    != activity["wait_completed_event_id"]
                    or source.context.get("wait_completed_event_id") is None):
                raise ConsistencyError("Action6 自然完成缺少同一活动的可靠有限等待")
        return SavedSegmentCompletionProof(
            attempt["intent_event_id"],
            None if source.context is None else source.context.get("stop_result_event_id"),
            None if source.context is None else source.context.get("wait_completed_event_id"),
        )
    from camctl.capture.segment_models import EvidenceState
    from camctl.devices.capture_timing import CaptureTiming
    from camctl.devices.recording_completion import completed_file_wait_source
    from .capture import _CaptureStopCommand
    from .result_sources import load_result_source,ResultSourceKind
    from camctl.history.decoding import decode_event_row
    db=reader.connection
    action,activity,run,scan,last=storage._source(db,request.ref)
    setrow=storage._set(db,request.ref,fixed=True)
    facts=facts_from_document(setrow['facts_json']) if facts is None else facts
    upper=scan['intent_event_id']
    stop_id=wait_id=control_id=elapsed=None
    stop_required=(facts.stop is EvidenceState.CONFIRMED
        or facts.control_completion is EvidenceState.CONFIRMED or facts.control_overrun is EvidenceState.CONFIRMED)
    if stop_required:
        found=_one(db,'SELECT t.id,t.result_event_id FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id '
            'WHERE r.responsibility_key=? AND r.action_id=? AND r.activity_id=? AND t.result_event_id<? '
            'AND t.status!=? AND t.effect_state=? ORDER BY t.result_event_id DESC LIMIT 1 /* segment_stop_proof */',
            (f"stop/{action['id']}",action['id'],activity['id'],upper,
             int(enum_for('operation_attempts.status').RUNNING),int(enum_for('operation_attempts.effect_state').CONFIRMED)))
        if found is None:raise ConsistencyError('冻结CONFIRMED停止缺少原scan之前的实际STOP证明')
        stopped,_=_CaptureStopCommand.source(db,found[1])
        stop_id=found[1]
        if stopped['id']!=request.activity_id:raise ConsistencyError('原STOP证明改变实际活动')
    wait_ms=action['execution_spec_json'].get('file_completion_wait_ms')
    if facts.file_wait is EvidenceState.CONFIRMED and wait_ms is not None:
        source=load_result_source(db,last.ref.ticket)
        if source.kind is not ResultSourceKind.STOP or source.context is None:
            raise ConsistencyError('冻结完成等待缺少原scan的可靠STOP来源')
        original_stop=source.context['stop_result_event_id']
        if stop_id is not None and original_stop!=stop_id:raise ConsistencyError('等待来源改变原STOP身份')
        settlement=last.page.outcome.settlement
        data=None if settlement is None else settlement.evidence.data
        prior=None
        source_id=None if data is None else data.get('file_completion_source_page_event_id')
        if source_id is not None:
            page,_=storage._at(db,source_id)
            if (page.ref.ticket.run_id!=last.ref.ticket.run_id
                    or page.ref.ticket.target_id!=last.ref.ticket.target_id
                    or page.ref.ticket.responsibility_key!=last.ref.ticket.responsibility_key
                    or page.ref.ticket.attempt_id>last.ref.ticket.attempt_id
                    or source_id>request.ref.final_page_event_id):
                raise ConsistencyError('实际等待来源页改变原RESULTS责任、活动或冻结上界')
            prior={'event_id':source_id,'activity_id':str(request.activity_id),
                'data':None if page.page.outcome.settlement is None else page.page.outcome.settlement.evidence.data}
        wait_id=completed_file_wait_source(data,page_event_id=request.ref.final_page_event_id,
            activity_id=str(request.activity_id),stop_result_event_id=original_stop,required_wait_ms=wait_ms,source_page=prior)
        if wait_id is None:raise ConsistencyError('冻结完成等待缺少原页可靠实际等待')
    if facts.control_completion is EvidenceState.CONFIRMED or facts.control_overrun is EvidenceState.CONFIRMED:
        elapsed=activity['control_elapsed_ns']
        if elapsed is None:raise ConsistencyError('冻结控制完成没有原活动可靠连续计时')
        from camctl.contracts.enums import load_registry
        owner=load_registry()['history_objects']['action']['id']
        event_type=load_event_registry()['events']['DEVICE_OBSERVED']['id']
        found=_one(db,'SELECT e.id,e.transaction_id,e.event_type,e.event_version,e.occurred_at,e.clock_status,e.change_seq,e.body_json '
            'FROM entity_event_links l JOIN history_events e ON e.id=l.event_id '
            'WHERE l.entity_type=? AND l.entity_id=? AND l.event_id<? AND e.event_type=? '
            "AND json_extract(e.body_json,'$.evidence.observation.stop_result_event_id')=? "
            "AND EXISTS(SELECT 1 FROM json_each(e.body_json,'$.rows') r WHERE json_extract(r.value,'$.table')='device_activities' "
            "AND json_extract(r.value,'$.id')=? AND json_extract(r.value,'$.after.values.control_elapsed_ns')=?) "
            'ORDER BY l.event_id DESC LIMIT 1',(owner,action['id'],upper,event_type,stop_id,activity['id'],elapsed))
        if found is None:raise ConsistencyError('原连续计时没有随原STOP在scan之前保存')
        control=decode_event_row(found);control_id=control.event_id
        timing=CaptureTiming(setrow['target_duration_ms'],action['execution_spec_json'].get('startup_margin_ms',0))
        lower=timing.recording_lower_bound_ns(elapsed)
        if facts.control_completion is EvidenceState.CONFIRMED and lower<timing.duration_ms*1_000_000:
            raise ConsistencyError('冻结控制完成超过原连续计时所能证明的下界')
        if facts.control_overrun is EvidenceState.CONFIRMED and lower<=timing.duration_ms*1_000_000:
            raise ConsistencyError('冻结控制多录没有原连续计时的严格超目标下界')
    return SavedSegmentCompletionProof(upper,stop_id,wait_id,control_id,elapsed)


def _header_request(header):
    validate_finish_header(header)
    from camctl.capture.media import RecordingFailure
    raw=header['request']
    return FinishSegmentRecording(
        result_ref_from_document(raw["ref"]),
        raw["activity_id"],
        raw["run_id"],
        raw["occurred_at"],
        None if raw["failure"] is None else RecordingFailure(**raw["failure"]),
        raw["failure_event_id"],
    )


def _has_freezable_scan(db,action_id):
    if _one(db,'SELECT 1 FROM segment_sets WHERE action_id=? LIMIT 1',(action_id,)) is not None:return True
    activity=_one(db,'SELECT id FROM device_activities WHERE action_id=?',(action_id,))
    if activity is None:return False
    after=None
    while True:
        predicate='' if after is None else ' AND t.attempt_no<?'
        parameters=(f'results/{activity[0]}',) if after is None else (f'results/{activity[0]}',after)
        latest=_one(db,'SELECT t.result_last_page_event_id,t.attempt_no FROM operation_runs r '
            'JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key=? '
            'AND t.result_last_page_event_id IS NOT NULL'+predicate+' ORDER BY t.attempt_no DESC LIMIT 1',parameters)
        if latest is None:return False
        saved,_=storage._at(db,latest[0])
        page=saved.page
        if page.next_cursor is None and page.scan_complete and page.set_finalized and page.outcome.error is None:return True
        after=latest[1]


def _completion_qualification(db,request,setrow,conclusion,*,before_event_id=None,transaction=None):
    """只恢复原组前的有限主行字段；兄弟以实际原status查存在性。"""
    action=row_facts(db,'actions',request.ref.action_id)
    run=row_facts(db,'operation_runs',request.run_id)
    plan=row_facts(db,'plans',action['plan_id'])
    if before_event_id is None:
        original_action=action;original_run=run;original_plan=plan
        pending=_one(db,'SELECT 1 FROM actions WHERE plan_id=? AND id!=? AND status NOT IN (3,4,5,6) LIMIT 1',
            (plan['id'],action['id'])) is not None
    else:
        from camctl.persistence.row_history import read_row_values_at_boundary
        from camctl.contracts.enums import load_registry
        if transaction is None or transaction.first_event_id!=before_event_id:
            raise ConsistencyError('原完成资格缺少实际已核完整事务')
        previous=HistoryBoundary(transaction.txn_id-1,before_event_id-1)
        head=_one(db,'SELECT id FROM history_transactions ORDER BY id DESC LIMIT 1')
        current=read_transaction_range(db,head[0],transaction if head[0]==transaction.txn_id else None)
        boundary=HistoryBoundary(current.txn_id,current.last_event_id)
        def restore(table,row,owner,columns):
            return read_row_values_at_boundary(db,owner=owner,table=table,row_id=row['id'],columns=frozenset(columns),
                current_values=row,boundary=previous,current_boundary=boundary,validated_transaction=transaction)
        original_action=restore('actions',action,('action',action['id']),('status','cancel_requested'))
        original_run = (
            None
            if run is None
            else restore(
                "operation_runs",
                run,
                ("action", action["id"]),
                ("status", "retry_wait_required", "error_json"),
            )
        )
        original_plan=restore('plans',plan,('plan',plan['id']),('status',))
        owner=load_registry()['history_objects']['action']['id']
        after=0;pending=False
        while True:
            # status可能在受理创建时已终态，也可能由取消等事件改变；不只查ACTION_FINISHED。
            candidate=_one(db,"SELECT a.id FROM actions a WHERE a.plan_id=? AND a.id!=? AND a.id>? AND a.created_event_id<? "
                "AND COALESCE((SELECT json_extract(r.value,'$.after.values.status') FROM entity_event_links l "
                "JOIN history_events e ON e.id=l.event_id JOIN json_each(e.body_json,'$.rows') r "
                "WHERE l.entity_type=? AND l.entity_id=a.id AND l.event_id<? AND json_extract(r.value,'$.table')='actions' "
                "AND json_extract(r.value,'$.id')=a.id AND json_type(r.value,'$.after.values.status')='integer' "
                "ORDER BY l.event_id DESC LIMIT 1),0) NOT IN (3,4,5,6) ORDER BY a.id LIMIT 1",
                (plan['id'],action['id'],after,before_event_id,owner,before_event_id))
            if candidate is None:break
            sibling=row_facts(db,'actions',candidate[0])
            facts=restore('actions',sibling,('action',sibling['id']),('status',))
            if facts['status'] not in (3,4,5,6):pending=True;break
            after=sibling['id']
    canceled = _canceled_result(db, request.ref)
    if (
        original_action["status"] != 2
        or bool(original_action["cancel_requested"]) != canceled
    ):
        raise ConsistencyError('原组前来源不具备未取消的普通执行资格')
    original_run = original_run or {
        "status": 0,
        "retry_wait_required": 0,
        "error_json": None,
    }
    complete=original_plan['status'] in (1,2) and not pending
    return {
        "action_status": original_action["status"],
        "cancel_requested": original_action["cancel_requested"],
        "run_status": original_run["status"],
        "run_retry_wait_required": original_run["retry_wait_required"],
        "run_error_json": original_run["error_json"],
        "plan_status": original_plan["status"],
        "finish_run": original_run["status"] in (1, 2),
        "complete_plan": complete,
        "plan_result_status": 3 if complete else original_plan["status"],
        "preparation_events": _preparation_events(db, request, setrow, conclusion),
    }


def _preparation_events(db, request, setrow, conclusion):
    """从冻结来源复用未启动依据，保留实际基准错误并释放原占用。"""
    if not isinstance(request.ref, SegmentResultRef):
        return ()
    reasons = enum_for("segment_check_conclusions.reason")
    if conclusion["reason"] not in (
        int(reasons.NO_RESULT_WORK),
        int(reasons.CANCELED_BEFORE_PUBLICATION),
    ):
        return ()
    from .capture_facts import load_start_facts, unstarted_events

    facts = load_start_facts(db, row_facts(db, "actions", request.ref.action_id))
    if not facts.not_started or facts.activity is None:
        return ()
    from .segment_result_sets import _row_at, _boundary

    activity = _row_at(
        db,
        "device_activities",
        facts.activity["id"],
        request.ref.action_id,
        HistoryBoundary(setrow["source_txn_id"], setrow["source_event_id"]),
        _boundary(db),
    )
    facts = replace(facts, activity=activity)
    events = []
    if conclusion["reason"] == int(reasons.NO_RESULT_WORK):
        call = _one(
            db,
            "SELECT error_json FROM baseline_read_calls WHERE action_id=? AND result_event_id=?",
            (request.ref.action_id, conclusion["anchor_event_id"]),
        )
        from camctl.contracts.json_values import parse_exact_json

        error = None if call is None or call[0] is None else parse_exact_json(call[0])
        if error is not None and not json_equal(error, activity["last_error_json"]):
            events.append(
                event_envelope(
                    0,
                    0,
                    load_event_registry()["events"]["DEVICE_OBSERVED"]["id"],
                    2,
                    (
                        update_change(
                            "device_activities",
                            activity["id"],
                            {"last_error_json": activity["last_error_json"]},
                            {"last_error_json": error},
                        ),
                    ),
                    request.occurred_at,
                )
            )
    events.extend(
        unstarted_events(
            facts, request.occurred_at, run_status=None, preparation_resolved=True
        )
    )
    return tuple(events)


def _verify_qualification_header(header,qualification):
    if (header['finish_run']!=qualification['finish_run'] or header['complete_plan']!=qualification['complete_plan']
            or header['plan_status']!=qualification['plan_result_status']):
        raise ConsistencyError('原RESULTS或父计划适用收场与可靠原F前事实不同')


def _canceled_result(db, ref):
    row = storage._set(db, ref)
    if row["publication_kind"] == int(
        enum_for("segment_sets.publication_kind").CANCELED
    ):
        return True
    return (
        _one(
            db,
            "SELECT 1 FROM segment_check_conclusions WHERE set_id=? AND reason=?",
            (
                row["id"],
                int(
                    enum_for(
                        "segment_check_conclusions.reason"
                    ).NATIVE_FINITE_CANCEL_END
                ),
            ),
        )
        is not None
    )


def verify_finish_request_event(db,event,context,action_id):
    """从原保存事实决定适用域，再核原有限申请及最终提案结构。"""
    from camctl.devices.tasks import CaptureResultPolicy
    action=row_facts(db,'actions',action_id)
    header=event.evidence.get('segment_finish_request')
    segmented = action["type"] in (2, 3) and action["execution_spec_json"].get(
        "result_policy"
    ) in (
        CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value,
        CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1.value,
        CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
    )
    changed=next(row for row in event.rows if row.table=='actions' and row.row_id==action_id)
    status=changed.after.values.get('status')
    required = (
        segmented
        and status in (3, 4, 6)
        and (
            status == 3
            or _has_freezable_scan(db, action_id)
            or _one(db, "SELECT 1 FROM segment_sets WHERE action_id=?", (action_id,))
            is not None
        )
    )
    if header is None:
        if required:raise ConsistencyError('已有分段完成依据的来源必须提交原完整分段申请和同组最终ACTIVATE')
        return
    if not segmented or not required:raise ConsistencyError('分段完成原申请不适用于该来源终态')
    request=_header_request(header)
    if (request.ref.action_id!=action_id or request.occurred_at!=event.occurred_at
            or event.event_id!=context.transaction.first_event_id or len(event.rows)!=1):
        raise ConsistencyError('原有限分段申请改变来源身份、实际时刻或首事件位置')
    action,run,setrow,assessment,conclusion=_identity(request,db)
    canceled = _canceled_result(db, request.ref)
    if (
        action["status"] != 2
        or bool(action["cancel_requested"]) != canceled
        or setrow["published_event_id"] is not None
    ):
        raise ConsistencyError('分段终态缺少原普通执行资格')
    if _one(db,'SELECT 1 FROM outputs WHERE source_action_id=? LIMIT 1',(action_id,)) is not None:
        raise ConsistencyError('分段来源终态之前已有正式产物')
    if _one(db,'SELECT 1 FROM operation_attempts WHERE run_id=? AND status=1 LIMIT 1',(request.run_id,)) is not None:
        raise ConsistencyError('原RESULTS实际尝试尚未完成')
    reader=SimpleNamespace(connection=db)
    _verify_scope_members(request,assessment,reader)
    originals=previews=0
    for candidate in _candidates(request,reader,header['batch_size']):
        if candidate.kind is OutputKind.ORIGINAL:originals+=1
        else:previews+=1
    scope_count=sum(len(page) for page in _scopes(request,reader,header['batch_size']))
    if (header['original_count'],header['preview_count'],header['scope_count'])!=(originals,previews,scope_count):
        raise ConsistencyError('原有限头没有完整覆盖冻结候选和范围')
    expected_status,error,details=_failure(request,assessment,originals,setrow,_read_failure_sources(request,assessment,setrow,db,conclusion),before_event_id=event.event_id)
    if expected_status == 3:
        _verify_action6_inspections_finished(db, action, setrow, assessment)
    before={'status':2};after={'status':expected_status}
    if expected_status==4:
        before.update(error_code=action['error_code'],error_details_json=action['error_details_json'])
        after.update(error_code=error,error_details_json=details)
    if changed!=update_change('actions',action_id,before,after) or header['action_status']!=expected_status:
        raise ConsistencyError('来源终态与原冻结判断或实际有限失败不同')
    qualification=_completion_qualification(db,request,setrow,conclusion)
    _verify_qualification_header(header,qualification)
    complete_plan=qualification['complete_plan']
    output_count=originals+previews
    if (header['first_output_id']!=(next_row_id(db,'outputs') if output_count else 1)
            or header['first_origin_id']!=(next_row_id(db,'output_origins') if previews else 1)):
        raise ConsistencyError('原完整组的产物或配对身份范围不同')
    expected_events = (
        2
        + len(qualification["preparation_events"])
        + output_count
        + (scope_count + header["batch_size"] - 1) // header["batch_size"]
        + int(header["finish_run"])
        + int(complete_plan)
    )
    if context.transaction.last_event_id-context.transaction.first_event_id+1!=expected_events:
        raise ConsistencyError('原完整分段组没有预分配完整事件范围和最终ACTIVATE')
    storage._verify_final_activation(request.ref,setrow,event,context)
    final=context.transaction_rows['segment_sets'][setrow['id']]
    if final['published_event_id']!=context.transaction.last_event_id:
        raise ConsistencyError('原正式ACTIVATE不是完整完成组的最后事件')
    facts=_FinishFacts(action,run,setrow,assessment,(expected_status,error,details))
    context.current_reads.prepare_segment_finish(event,context.transaction,header,qualification,facts)


def _created_output(db,transaction,action_id,file_id,kind):
    # LIMIT 2 保留跨批重复检测；实际查找使用既有device_file_id唯一索引。
    with closing(db.execute(
        'SELECT o.id,o.created_event_id,e.event_type,e.transaction_id FROM outputs o '
        'LEFT JOIN history_events e ON e.id=o.created_event_id '
        'WHERE o.source_action_id=? AND o.device_file_id=? AND o.kind=? ORDER BY o.id LIMIT 2',
        (action_id,file_id,kind))) as cursor:
        rows=cursor.fetchmany(2)
    if len(rows)!=1 or not transaction.first_event_id<=rows[0][1]<=transaction.last_event_id:
        raise ConsistencyError('成员没有同完成组的唯一实际产物')
    if rows[0][2:]!=(load_event_registry()['events']['OUTPUT_REGISTERED']['id'],transaction.txn_id):
        raise ConsistencyError('产物创建没有本完成组已验证登记事件')
    return rows[0][0]


def _run_finish_row(request,qualification):
    before={name:qualification['run_'+name] for name in ('status','retry_wait_required','error_json')}
    if before['status'] not in (1,2):raise ConsistencyError('原RESULTS不适用新的成功收场')
    return update_change('operation_runs',request.run_id,before,{'status':3,'retry_wait_required':0,'error_json':None})


def _verify_run_finish_event(event,request,responsibility_key,qualification):
    evidence={'stale_run_request':{'kind':'stale_run_finish','responsibility_keys':[responsibility_key],'status':3,'error':None}}
    if (event.event_type!=load_event_registry()['events']['OPERATION_CONFIGURED']['id'] or event.reason!=3
            or event.occurred_at!=request.occurred_at or event.clock_status!=2 or not json_equal(event.evidence,evidence)
            or event.rows!=(_run_finish_row(request,qualification),)):
        raise ConsistencyError('原RESULTS共同收场的完整事件或前后行不同')


def _verify_group_closure(db,request,header,transaction,marker_id,qualification,reads):
    """最后具名守卫核实际先前收场，独立于命令complete回调。"""
    if marker_id!=transaction.last_event_id:raise ConsistencyError('正式ACTIVATE必须是完整组最后事件')
    _verify_qualification_header(header,qualification)
    def saved(identity):
        return reads.segment_finish_event(identity,transaction)

    identity = transaction.first_event_id + 1
    for template in qualification["preparation_events"]:
        expected = replace(
            template, event_id=identity, transaction_id=transaction.txn_id
        )
        if replace(saved(identity), change_seq=None) != expected:
            raise ConsistencyError("原未启动收场遗漏实际基准错误或占用释放")
        identity += 1
    identity += (
        header["output_count"]
        + (header["scope_count"] + header["batch_size"] - 1) // header["batch_size"]
    )
    run=reads.segment_finish_values('operation_runs',row_facts(db,'operation_runs',request.run_id),transaction)
    if header['finish_run']:
        _verify_run_finish_event(saved(identity),request,run['responsibility_key'],qualification);identity+=1
        if (run['status'],run['retry_wait_required'],run['error_json'])!=(3,0,None):
            raise ConsistencyError('最后守卫没有原RESULTS实际共同收场')
    elif run is not None and run["status"] not in (3, 4, 5, 6, 7):
        raise ConsistencyError("最后守卫仍有未结束原RESULTS责任")
    action=reads.segment_finish_values('actions',row_facts(db,'actions',request.ref.action_id),transaction)
    plan=reads.segment_finish_values('plans',row_facts(db,'plans',action['plan_id']),transaction)
    if header['complete_plan']:
        event=saved(identity);identity+=1
        if (event.event_type!=load_event_registry()['events']['PLAN_STATUS_CHANGED']['id'] or event.reason!=2
                or event.occurred_at!=request.occurred_at or event.clock_status!=2 or event.evidence or len(event.rows)!=1):
            raise ConsistencyError('最后守卫缺少同组实际父计划完成')
        expected=update_change('plans',plan['id'],{'status':qualification['plan_status']},{'status':3})
        if event.rows!=(expected,):raise ConsistencyError('原父计划共同完成的完整前后行不同')
    if plan['status']!=header['plan_status'] or identity!=marker_id:
        raise ConsistencyError('最后守卫的父计划结果或共同收场范围不同')


def verify_publication(db,ref,event,context):
    """独立核对实际已验证前序及当前值；提案只用于查证唯一marker结构。"""
    setrow=storage._set(db,ref,fixed=True)
    storage._verify_final_activation(ref,setrow,event,context)
    if setrow['published_event_id'] is not None or _one(db,'SELECT 1 FROM segment_sets WHERE action_id=? AND published_event_id IS NOT NULL',(ref.action_id,)):
        raise ConsistencyError('动作已经存在正式分段集合')
    tx=context.transaction
    reads=context.current_reads
    action=reads.segment_finish_values('actions',row_facts(db,'actions',ref.action_id),tx)
    accepted=reads.segment_finish_event(tx.first_event_id,tx)
    if accepted.event_type!=load_event_registry()['events']['ACTION_FINISHED']['id'] or action['status'] not in (3,4,5,6):
        raise ConsistencyError('分段发布必须引用本完成组已验证来源终态')
    def created(file_id,kind):
        return reads.segment_created_output(tx,ref.action_id,file_id,kind)
    if not any(row.table=='actions' and row.row_id==ref.action_id and row.after.values.get('status')==action['status'] for row in accepted.rows):
        raise ConsistencyError('来源终态不属于原完成组')
    header = accepted.evidence.get("segment_finish_request")
    request = _header_request(header)
    _, _, _, assessment, conclusion = _identity(request, db)
    from camctl.capture.segment_models import SegmentDecision, SegmentDecisionStatus
    if isinstance(assessment, SegmentDecision):
        if action['status'] == 3 and assessment.status is not SegmentDecisionStatus.SUCCEEDED:
            raise ConsistencyError('Action6 来源成功与冻结判断不同')
        if action['status'] == 6 and assessment.status is not SegmentDecisionStatus.CANCELED:
            raise ConsistencyError('Action6 来源取消与冻结判断不同')
    else:
        if action['status']==3 and assessment.status is not SegmentAssessmentStatus.SUCCEEDED:
            raise ConsistencyError('来源成功与冻结判断不同')
        if assessment.gap_count is None and (action['status']==3 or assessment.default_scope.state is not DefaultScopeState.UNKNOWN):
            raise ConsistencyError('缺口未知不能提升成功或默认范围')
    for changed in event.rows:
        if changed.table=='segment_scope_members':
            scope=row_facts(db,'segment_scope_members',changed.row_id)
            member=row_facts(db,'segment_members',scope['member_id'])
            if (scope['set_id']!=setrow['id'] or scope['output_id'] is not None
                    or created(member['file_id'],int(enum_for('outputs.kind').ORIGINAL))!=changed.after.values['output_id']):
                raise ConsistencyError('范围关联没有同组已验证实际原片')
    if event.reason!=storage._EVENT['branches']['ACTIVATE']['reason']:return
    # 末组核对固定候选和实际产物的双向完整覆盖。
    reader=SimpleNamespace(connection=db)
    count=originals=0
    header=accepted.evidence.get('segment_finish_request')
    request=_header_request(header)
    if request.ref!=ref or request.activity_id!=setrow['activity_id'] or request.run_id!=setrow['run_id'] or request.occurred_at!=event.occurred_at:
        raise ConsistencyError('最后守卫改变原完整分段申请')
    _verify_scope_members(request,assessment,reader)
    for candidate in _candidates(request,reader):
        kind=int(enum_for('outputs.kind')[candidate.kind.name])
        output_id=created(candidate.file_id,kind)
        count+=1
        if candidate.kind is OutputKind.ORIGINAL:originals+=1
        else:
            original=created(candidate.original_file_id,int(enum_for('outputs.kind').ORIGINAL))
            links=reads.segment_finish_origin(tx,output_id)
            if links!=(1,original,original):raise ConsistencyError('预览未准确关联本组真实原片')
    # 双向覆盖：同数量的外来/重复/缺项产物不能通过。
    total=0
    for identity,file_id,kind in reads.segment_finish_outputs(tx,ref.action_id):
        total+=1
        found=_one(db,'SELECT id FROM segment_members WHERE set_id=? AND file_id=?',(setrow['id'],file_id))
        candidate=None if found is None else storage.segment_output_candidate(ref,found[0],reader)
        if candidate is None or kind!=int(enum_for('outputs.kind')[candidate.kind.name]) or created(file_id,kind)!=identity:
            raise ConsistencyError('登记产物不是原冻结合格候选')
    if total!=count:raise ConsistencyError('完成组存在额外或重复产物')
    expected=assessment.default_scope.member_count or 0
    actual=0
    for page in _scopes(request,reader):
        for scope in page:
            scope=reads.segment_finish_values('segment_scope_members',scope,tx)
            actual+=1
            member=row_facts(db,'segment_members',scope['member_id'])
            if scope['output_id']!=created(member['file_id'],int(enum_for('outputs.kind').ORIGINAL)):
                raise ConsistencyError('正式默认范围缺少完整实际关联')
    if actual!=expected:raise ConsistencyError('默认范围数量与冻结判断不同')
    status,error,details=_failure(request,assessment,originals,setrow,_read_failure_sources(request,assessment,setrow,db,conclusion),before_event_id=tx.first_event_id)
    if (action['status'],action['error_code'],action['error_details_json'])!=(status,error,details):
        raise ConsistencyError('实际来源终态与冻结判断或有限错误头不同')
    qualification=context.current_reads.validated_segment_finish(tx,header)
    _verify_group_closure(db,request,header,tx,event.event_id,qualification,reads)


def _event_state(db,event,ref,proposal):
    """沿当前事件的固定外键加载有限关联，不复制整个动作或计划。"""
    state={}
    tables=load_event_registry()['tables']
    pending=[(row.table,row.row_id) for row in event.rows]
    for row in event.rows:
        for column,value in row.after.values.items():
            target=foreign_key_targets().get((row.table,column))
            if value is not None and target in tables:pending.append((target,value))
    seen=set()
    while pending:
        table,identity=pending.pop()
        if (table,identity) in seen:continue
        seen.add((table,identity))
        facts=row_facts(db,table,identity)
        state.setdefault(table,{})
        if facts is None:continue
        state[table][identity]=facts
        for column,value in facts.items():
            target=foreign_key_targets().get((table,column))
            if value is not None and target in tables:pending.append((target,value))
    # 关系守卫读取的范围以数据库实际完整查询为准，合法反向集合至多两类。
    coverage={}
    for row in event.rows:
        if row.table!='outputs':continue
        identities=[row.row_id]
        identities += [change.after.values['original_output_id'] for change in event.rows if change.table=='output_origins']
        for identity in identities:
            for column in ('output_id','original_output_id'):
                with closing(db.execute(f'SELECT id FROM output_origins WHERE {column}=? ORDER BY id LIMIT 3',(identity,))) as cursor:
                    relations=cursor.fetchmany(3)
                if len(relations)>2:raise ConsistencyError('原片派生关系超过登记最大类别数')
                coverage.setdefault(('output_origins',column),set()).add(identity)
                for item in relations:
                    relation=row_facts(db,'output_origins',item[0]);state.setdefault('output_origins',{})[item[0]]=relation
                    prior=row_facts(db,'outputs',relation['output_id']);state.setdefault('outputs',{})[prior['id']]=prior
    state.setdefault('output_origins',{})
    owners={}
    for row in event.rows:
        if row.table=='outputs':owners[row.table,row.row_id]=('output',row.row_id)
        elif row.table=='output_origins':owners[row.table,row.row_id]=('output',row.after.values['output_id'])
        elif row.table=='plans':owners[row.table,row.row_id]=('plan',row.row_id)
        else:owners[row.table,row.row_id]=('action',ref.action_id)
    # 归属解析使用本事件after；报告路由仍使用当前有限行和本事件after。
    return state,owners,ReadCoverage(coverage)


@dataclass(frozen=True)
class _FinishFacts:
    action: dict
    run: dict | None
    setrow: dict
    assessment: object
    outcome: tuple


def _finish_event_inputs(request, reader, batch):
    """显式取得每批候选的固定文件及范围文件身份，不逐项重复反查。"""
    def candidates():
        for saved in _candidate_facts(request,reader,batch):
            yield saved.candidate,saved.file

    def scopes():
        if isinstance(request.ref, SegmentResultRef):
            return
        cursor=None
        while True:
            page=storage.read_segment_finish_scope(request.ref,cursor,batch,reader)
            yield page.items
            if page.next_cursor is None:return
            cursor=page.next_cursor

    return candidates, scopes


def _canonical_events(request,transaction,header,facts,qualification,candidates,scopes,created):
    """从显式固定事实生成规范事件；前序产物映射由调用方独立取得。"""
    action, run, setrow = facts.action, facts.run, facts.setrow
    _verify_qualification_header(header,qualification)
    status,error,details=facts.outcome
    if status!=header['action_status']:raise ConsistencyError('规范终态与原冻结判断不同')
    types=load_event_registry()['events']
    identity=transaction.first_event_id
    def envelope(name,reason,rows,evidence=None):
        return event_envelope(identity,transaction.txn_id,types[name]['id'],reason,tuple(rows),request.occurred_at,evidence)
    before={'status':qualification['action_status']};after={'status':status}
    if status==4:
        before.update(error_code=None,error_details_json=None)
        after.update(error_code=error,error_details_json=details)
    reason = types["ACTION_FINISHED"]["branches"][
        {3: "SUCCEED", 4: "FAIL", 6: "CANCEL"}[status]
    ]["reason"]
    yield envelope(
        "ACTION_FINISHED",
        reason,
        (update_change("actions", request.ref.action_id, before, after),),
        {"segment_finish_request": header},
    )
    identity+=1
    for template in qualification["preparation_events"]:
        yield replace(template, event_id=identity, transaction_id=transaction.txn_id)
        identity += 1

    output_id=header['first_output_id'];origin_id=header['first_origin_id']
    originals=previews=0
    for wanted in (OutputKind.ORIGINAL,OutputKind.PREVIEW):
        for candidate, file in candidates():
            if candidate.kind is not wanted:continue
            from .capture import _unchecked_media
            values=dict(source_action_id=request.ref.action_id,kind=int(enum_for('outputs.kind')[wanted.name]),device_file_id=candidate.file_id,intermediate_file_id=None,
                original_name=file['original_name'],media_type=file['media_type'],availability=1,cleanup_status=1,cleanup_error_json=None,media_json=_unchecked_media(),error_json=None)
            rows=(row_change('outputs',output_id,values),)
            if wanted is OutputKind.PREVIEW:
                original=created(candidate.original_file_id,int(enum_for('outputs.kind').ORIGINAL))
                rows+=(row_change('output_origins',origin_id,dict(output_id=output_id,original_output_id=original)),)
                origin_id+=1;previews+=1
            else:originals+=1
            yield envelope('OUTPUT_REGISTERED',values['kind'],rows)
            identity+=1;output_id+=1
    count=0
    for page in scopes():
        if not page:continue
        bindings=[];rows=[]
        for row, file_id in page:
            output=created(file_id,int(enum_for('outputs.kind').ORIGINAL))
            bindings.append((row['id'],output))
            rows.append(update_change('segment_scope_members',row['id'],{'output_id':None},{'output_id':output}))
            count+=1
        evidence={'segment_request':{'ref':document(request.ref),'input':storage._request_document(tuple(bindings))}}
        yield envelope('SEGMENT_FACTS',storage._EVENT['branches']['PUBLISH']['reason'],rows,evidence)
        identity+=1
    if (originals,previews,count)!=(header['original_count'],header['preview_count'],header['scope_count']):
        raise ConsistencyError('规范事件阶段没有耗尽原完整候选及范围计数')
    if qualification['finish_run']:
        evidence={'stale_run_request':{'kind':'stale_run_finish','responsibility_keys':[run['responsibility_key']],'status':3,'error':None}}
        yield envelope('OPERATION_CONFIGURED',3,(_run_finish_row(request,qualification),),evidence)
        identity+=1
    if qualification['complete_plan']:
        yield envelope('PLAN_STATUS_CHANGED',2,(update_change('plans',action['plan_id'],{'status':qualification['plan_status']},{'status':3}),))
        identity+=1
    if identity!=transaction.last_event_id:raise ConsistencyError('规范事件阶段与原完整L不同')
    yield envelope('SEGMENT_FACTS',storage._EVENT['branches']['ACTIVATE']['reason'],
        (update_change('segment_sets',setrow['id'],{'published_event_id':None},{'published_event_id':identity}),),
        {'segment_request':{'ref':document(request.ref),'input':None}})


class _FinishCommand:
    def __init__(self,request,key,batch):self.request,self.key,self.batch=request,key,batch

    def plan(self,scope):
        db=scope.connection; request=self.request
        saved=_one(db,'SELECT id FROM history_transactions WHERE operation_key=?',(str(self.key),))
        if saved is not None:
            result=verify_segment_finish(request,self.key,scope,execution_finished=True)
            return CommandPlan((),{}, {},result=result,read_only=True)
        action,run,setrow,assessment,conclusion=_identity(request,db)
        canceled = _canceled_result(db, request.ref)
        if (
            action["status"] != 2
            or bool(action["cancel_requested"]) != canceled
            or setrow["published_event_id"] is not None
        ):
            raise ConsistencyError('原分段完成申请已无普通执行资格')
        if _one(db,'SELECT 1 FROM outputs WHERE source_action_id=? LIMIT 1',(request.ref.action_id,)):
            raise ConsistencyError('未完成来源已经存在正式产物')
        if _one(db,'SELECT 1 FROM operation_attempts WHERE run_id=? AND status=1 LIMIT 1',(request.run_id,)):
            raise ConsistencyError('RESULTS实际尝试仍在执行')
        originals=previews=0
        for candidate in _candidates(request,scope,self.batch):
            if candidate.kind is OutputKind.ORIGINAL:originals+=1
            else:previews+=1
        scope_count=sum(len(page) for page in _scopes(request,scope,self.batch))
        if scope_count!=(assessment.default_scope.member_count or 0):
            raise ConsistencyError('默认范围尚未完整保存')
        _verify_scope_members(request,assessment,scope)
        output_count=originals+previews
        first_output=next_row_id(db,'outputs') if output_count else 1
        first_origin=next_row_id(db,'output_origins') if previews else 1
        storage._capacity(first_output,output_count);storage._capacity(first_origin,previews)
        plan=row_facts(db,'plans',action['plan_id'])
        qualification=_completion_qualification(db,request,setrow,conclusion)
        complete_plan=qualification['complete_plan'];finish_run=qualification['finish_run']
        event_count = (
            1
            + len(qualification["preparation_events"])
            + output_count
            + (scope_count + self.batch - 1) // self.batch
            + int(finish_run)
            + int(complete_plan)
            + 1
        )
        allocation=scope.allocate(event_count)
        outcome=_failure(request,assessment,originals,setrow,_read_failure_sources(request,assessment,setrow,db,conclusion),before_event_id=scope.max_event_id+1)
        status,error,details=outcome
        if status == 3:
            _verify_action6_inspections_finished(db, action, setrow, assessment)
        facts=_FinishFacts(action,run,setrow,assessment,outcome)
        result=SegmentFinishResult(request.ref,status,3 if complete_plan else plan['status'],HistoryBoundary(allocation.txn_id,allocation.last_event_id),output_count,originals,previews,scope_count)
        header={'request':request_document(request),'output_count':output_count,'original_count':originals,'preview_count':previews,'scope_count':scope_count,
            'first_output_id':first_output,'first_origin_id':first_origin,'action_status':status,'plan_status':result.plan_status,'batch_size':self.batch,
            'finish_run':finish_run,'complete_plan':complete_plan}
        marker=storage.segment_event(request.ref,'ACTIVATE',storage.segment_activation_rows(request.ref,allocation.last_event_id,scope),allocation.last_event_id,allocation.txn_id,request.occurred_at,request_input=None)
        def proposal(identity):
            if identity!=marker.event_id:raise ConsistencyError('原提案只有唯一最终marker可查证')
            return marker
        def context(event,transaction):
            state,owners,coverage=_event_state(db,event,request.ref,proposal)
            # 固定提案事实只含marker身份，不含未来业务资格。
            final={**state,'segment_sets':{setrow['id']:{**setrow,'published_event_id':marker.event_id}}}
            reads=SqlEventReads(db,PlanReadScope(plan['id'],self.batch),proposal_event=proposal,segment_ref=request.ref)
            return EventContext(transaction,owners,state,final,coverage,current_reads=reads)
        def generate():
            transaction=TransactionRange(allocation.txn_id,allocation.first_event_id,allocation.last_event_id)
            candidates,scopes=_finish_event_inputs(request,scope,self.batch)
            created=lambda file_id,kind: _created_output(db,transaction,request.ref.action_id,file_id,kind)
            yield from _canonical_events(request,transaction,header,facts,qualification,candidates,scopes,created)
        def complete(db):
            from camctl.history.decoding import decode_event_row
            raw=_one(db,'SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',(marker.event_id,))
            actual=None if raw is None else decode_event_row(raw)
            current=storage._set(db,request.ref,fixed=True)
            if actual is None or replace(actual,change_seq=None)!=marker or current['published_event_id']!=marker.event_id:
                raise ConsistencyError('完整完成组没有实际提交唯一最终ACTIVATE')
            if request.run_id is not None and row_facts(
                db, "operation_runs", request.run_id
            )["status"] not in (3, 4, 5, 6, 7):
                raise ConsistencyError('完成组没有共同结束原RESULTS责任')
            if row_facts(db,'plans',plan['id'])['status']!=result.plan_status:
                raise ConsistencyError('完成组父计划结果与原有限回执不同')
        return BoundedCommandPlan(generate(),context,result,complete)


def finish_segment_recording(request,key,owned,*,batch_size=128):
    if not isinstance(request,FinishSegmentRecording):raise TypeError('必须保存原有限分段申请')
    if type(batch_size) is not int or not 1<=batch_size<=128:raise ValueError('批次必须为1—128')
    from .capture import register_capture_guards
    from .operations import register_operation_guards
    register_capture_guards();register_operation_guards();storage.register_segment_guards()
    return commit_operation(_FinishCommand(request,key,batch_size),key,owned)


def verify_segment_finish(request,key,reader,*,execution_finished,batch_size=128):
    """可靠原工作结束后核实固定完整组；在途缺键不能成为可靠不存在。"""
    if execution_finished is not True:raise ConsistencyError('原保存仍可能在途，不能核实为可靠缺键')
    if type(batch_size) is not int or not 1<=batch_size<=128:raise ValueError('原组批次必须为1—128')
    db=reader.connection
    opened=not db.in_transaction
    if opened:db.execute('BEGIN')
    try:
        with opened_operation_fact_scope(db, opened):
            return _verify_segment_finish(request,key,reader,batch_size)
    finally:
        if opened:db.execute('ROLLBACK')


def _verify_segment_finish(request,key,reader,batch_size):
    db=reader.connection
    found=_one(db,'SELECT id FROM history_transactions WHERE operation_key=?',(str(key),))
    if found is None:return None
    tx=read_transaction_range(db,found[0])
    first_page=read_saved_transaction_page(db,key,tx,None,1)
    first=first_page.items[0]
    header=first.evidence.get('segment_finish_request')
    if not isinstance(header,dict) or not json_equal(header.get('request'),request_document(request)):
        raise ConsistencyError('原操作键的有限完整申请不同')
    if first.event_type!=load_event_registry()['events']['ACTION_FINISHED']['id']:
        raise ConsistencyError('原完成组没有来源终态申请头')
    # 原固定事实重生成逐项预期，只使用原头中的身份，不调用分配器。
    result=_verify_events(request,key,tx,header,reader,batch_size,first_page)
    return result


def _verify_events(request,key,tx,header,reader,batch_size,first_page):
    """按固定完整组和冻结输入双向比较，只有读到原L才返回原结果。"""
    validate_finish_header(header)
    db=reader.connection
    action,run,setrow,assessment,conclusion=_identity(request,db)
    qualification=_completion_qualification(db,request,setrow,conclusion,before_event_id=tx.first_event_id,transaction=tx)
    _verify_qualification_header(header,qualification)
    _verify_scope_members(request,assessment,reader)
    def stream():
        yield from first_page.items
        position=first_page.next_cursor
        if position is None:return
        while True:
            page=read_saved_transaction_page(db,key,tx,position,batch_size)
            yield from page.items
            if page.next_cursor is None:return
            position=page.next_cursor
    outcome=_failure(request,assessment,header['original_count'],setrow,_read_failure_sources(request,assessment,setrow,db,conclusion),before_event_id=tx.first_event_id)
    facts=_FinishFacts(action,run,setrow,assessment,outcome)
    candidates,scopes=_finish_event_inputs(request,reader,header['batch_size'])
    created=lambda file_id,kind: _created_output(db,tx,request.ref.action_id,file_id,kind)
    for actual,expected in zip_longest(stream(),_canonical_events(request,tx,header,facts,qualification,candidates,scopes,created)):
        if actual is None or expected is None or replace(actual,change_seq=None)!=expected:
            raise ConsistencyError('原完成组完整事件与冻结完整申请不同')
        if expected.event_type==load_event_registry()['events']['OUTPUT_REGISTERED']['id']:
            row=expected.rows[0];values=row.after.values
            if _created_output(db,tx,request.ref.action_id,values['device_file_id'],values['kind'])!=row.row_id:
                raise ConsistencyError('原完整组产物不变身份映射与原范围不同')
    return SegmentFinishResult(request.ref,header['action_status'],header['plan_status'],HistoryBoundary(tx.txn_id,tx.last_event_id),header['output_count'],header['original_count'],header['preview_count'],header['scope_count'])
