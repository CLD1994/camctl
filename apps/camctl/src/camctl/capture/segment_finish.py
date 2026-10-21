"""分段完成的有限原申请、原结果及历史头结构。"""
from dataclasses import dataclass
from camctl.capture.segment_models import SegmentSetRef
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ObjectId, UtcMicros, MAX_OBJECT_ID
from camctl.capture.media import RecordingFailure
from camctl.contracts.workflow_errors import registered_error, validate_error_details
from camctl.capture.segment_storage import document
from camctl.capture.segment_errors import validate_segment_error_header

@dataclass(frozen=True)
class FinishSegmentRecording:
    """引用已固定集合、唯一判断及其完整默认范围的原申请。"""
    ref: SegmentSetRef
    activity_id: int
    run_id: int
    occurred_at: int
    failure: RecordingFailure | None = None
    failure_event_id: int | None = None

    def __post_init__(self):
        if not isinstance(self.ref,SegmentSetRef):
            raise TypeError('完成分段必须引用原冻结集合')
        ObjectId(self.activity_id); ObjectId(self.run_id); UtcMicros(self.occurred_at)
        if self.failure is not None:
            if not isinstance(self.failure,RecordingFailure):raise TypeError('失败必须使用实际RecordingFailure')
            if 'action_error_id' not in registered_error(self.failure.code):raise ValueError('失败必须是登记的动作错误')
            if self.failure.code=='capture_segment_gap':
                validate_segment_error_header(self.failure.code,self.failure.details)
            else:
                validate_error_details(self.failure.code,self.failure.details)
            if any(name in self.failure.details for name in ('gaps','retained_output_ids','output_ids')):
                raise ValueError('完成申请不保存增长错误数组')
        if self.failure_event_id is not None:
            ObjectId(self.failure_event_id)
            if self.failure is None:raise ValueError('原错误事件引用必须携带实际失败')


def request_document(request):
    return {'ref':document(request.ref),'activity_id':request.activity_id,'run_id':request.run_id,
        'occurred_at':request.occurred_at,'failure':None if request.failure is None else
        {'code':request.failure.code,'details':request.failure.details},'failure_event_id':request.failure_event_id}


@dataclass(frozen=True)
class SegmentFinishResult:
    ref: SegmentSetRef
    action_status: int
    plan_status: int
    boundary: HistoryBoundary
    output_count: int
    original_count: int
    preview_count: int
    scope_count: int


@dataclass(frozen=True)
class SavedSegmentCompletionProof:
    scan_intent_event_id: int
    stop_result_event_id: int | None = None
    wait_source_page_event_id: int | None = None
    control_event_id: int | None = None
    control_elapsed_ns: int | None = None


def validate_finish_header(value):
    """历史解码与写入共同使用的有限头结构；业务资格由事务守卫核实。"""
    fields={'request','output_count','original_count','preview_count','scope_count','first_output_id','first_origin_id','action_status','plan_status','batch_size','finish_run','complete_plan'}
    if not isinstance(value,dict) or set(value)!=fields:raise ValueError('分段完成头成员不同')
    raw=value['request']
    if not isinstance(raw,dict) or set(raw)!={'ref','activity_id','run_id','occurred_at','failure','failure_event_id'}:raise ValueError('原申请字段不同')
    failure=raw['failure']
    if failure is not None and (not isinstance(failure,dict) or set(failure)!={'code','details'}):raise ValueError('实际失败字段不同')
    FinishSegmentRecording(SegmentSetRef(**raw['ref']),raw['activity_id'],raw['run_id'],raw['occurred_at'],
        None if failure is None else RecordingFailure(**failure),raw['failure_event_id'])
    for name in ('output_count','original_count','preview_count','scope_count'):
        if type(value[name]) is not int or not 0<=value[name]<=MAX_OBJECT_ID:raise ValueError('完成头计数无效')
    for name in ('first_output_id','first_origin_id'):ObjectId(value[name])
    if value['output_count']!=value['original_count']+value['preview_count'] or value['scope_count']>value['original_count']:
        raise ValueError('完成头计数不一致')
    if type(value['batch_size']) is not int or not 1<=value['batch_size']<=128:raise ValueError('原保存批次非法')
    if (type(value['action_status']) is not int or type(value['plan_status']) is not int
            or value['action_status'] not in (3,4) or value['plan_status'] not in (1,2,3)):
        raise ValueError('完成结果状态非法')
    if type(value['finish_run']) is not bool or type(value['complete_plan']) is not bool:raise ValueError('原收场分支非法')
    if value['complete_plan'] and value['plan_status']!=3:raise ValueError('父计划共同完成与原结果不同')
    for start,count in (('first_output_id','output_count'),('first_origin_id','preview_count')):
        if value[count]:ObjectId(value[start]+value[count]-1)
        elif value[start]!=1:raise ValueError('空身份范围必须使用规范起点')
