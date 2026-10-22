"""分段关系的有限保存请求与确定编码；不保存增长成员数组。"""
from __future__ import annotations
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from camctl.capture import segment_models as models
from camctl.capture.segment_models import (
    SegmentSetRef,SegmentMember,CompletionFacts,SegmentAssessment,SegmentSourceKind,PublicationKind,
    SegmentDecision, SegmentDecisionScope, SegmentDecisionStatus, CoverageMetric, CoverageStatus,
    ResultCheckEndReason,CurrentSegmentResult)
from camctl.contracts.enums import enum_for
from camctl.contracts.segment_values import decode_text_enum
from camctl.contracts.values import ObjectId, UtcMicros
from camctl.contracts.history_values import HistoryBoundary
from camctl.devices.segments import SegmentKey
from camctl.outputs.catalog import OutputKind
from camctl.capture.result_pages import ResultPageRef

if TYPE_CHECKING:
    from camctl.operations.attempts import AttemptConfig
    from camctl.capture.media import RecordingFailure


# 类型映射不维护成员清单；编号和值只从权威登记派生。
_ENUM_COLUMNS = {
    SegmentSourceKind:'segment_sets.source_kind',
    PublicationKind:'segment_sets.publication_kind',
    ResultCheckEndReason:'segment_check_conclusions.reason',
    CurrentSegmentResult:'segment_check_conclusions.current_result',
    models.EvidenceState: 'segment_sets.facts_json.evidence',
    models.SegmentMemberRole: 'segment_members.role',
    models.OwnershipState: 'segment_members.ownership',
    models.FileCompletionState: 'segment_members.completion',
    models.PreviewPairingState: 'segment_members.pairing_state',
    models.SegmentAssessmentStatus: 'segment_sets.assessment_json.status',
    models.SegmentAssessmentReason: 'segment_sets.assessment_json.reason',
    models.CompletionBasis: 'segment_sets.assessment_json.basis',
    models.OverrecordStatus: 'segment_sets.assessment_json.overrecord',
    models.DefaultScopeState: 'segment_sets.assessment_json.scope_state',
    models.DefaultScopeBasis: 'segment_sets.assessment_json.scope_basis',
    models.SegmentGapKind: 'segment_gaps.kind',
    models.SegmentGapNeighborState: 'segment_gaps.neighbors_json.state',
}

# segment_sets.state 的既有 ASSESS 转换要求 facts_json 从 NULL 推进一次。
# 公共视频覆盖决策没有 OSMO CompletionFacts，因此用固定哨兵表达“无旧式
# 完成事实”；事件输入仍解码为 None，不会把设备控制事实伪装成 OSMO 模型。
_NO_COMPLETION_FACTS = {"kind": "no_completion_facts", "version": 1}


def facts_document(value, *, public_decision=False):
    """编码旧式完成事实列；公共决策使用无 OSMO 事实哨兵推进现有转换。"""
    if public_decision:
        if value is not None:
            raise TypeError("公共分段决策不能携带 OSMO 完成事实")
        return dict(_NO_COMPLETION_FACTS)
    return document(value)


@dataclass(frozen=True)
class SegmentResultRef:
    action_id: int
    set_id: int
    version: int

    def __post_init__(self):
        for value in (self.action_id,self.set_id,self.version):
            ObjectId(value)


@dataclass(frozen=True)
class ResultMemberInputCursor:
    ref: SegmentResultRef
    after_file_id: int

    def __post_init__(self):
        if not isinstance(self.ref,SegmentResultRef):
            raise TypeError('来源成员游标必须绑定原集合身份')
        ObjectId(self.after_file_id)


@dataclass(frozen=True)
class ResultSetCreate:
    action_id: int
    activity_id: int|None
    run_id: int|None
    source_kind: SegmentSourceKind
    source_boundary: HistoryBoundary
    current_scan: ResultPageRef|None
    publication_kind: PublicationKind
    predecessor_set_id: int|None
    cancel_event_id: int|None
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.action_id)
        for value in (self.activity_id,self.run_id,self.predecessor_set_id,self.cancel_event_id):
            if value is not None:ObjectId(value)
        if not isinstance(self.source_kind,SegmentSourceKind) or not isinstance(self.publication_kind,PublicationKind):
            raise TypeError('结果集合必须使用来源及发布分类')
        if not isinstance(self.source_boundary,HistoryBoundary):
            raise TypeError('结果集合必须使用原完整历史边界')
        if self.current_scan is not None and not isinstance(self.current_scan,ResultPageRef):
            raise TypeError('当前扫描必须引用真实原末页')
        UtcMicros(self.occurred_at)


@dataclass(frozen=True)
class ResultCheckConclusionSave:
    ref: SegmentResultRef
    reason: ResultCheckEndReason
    anchor_event_id: int|None
    prior_run_end_event_id: int|None
    check_config: AttemptConfig|None
    binding_failure: RecordingFailure|None
    prior_conclusion_id: int|None
    occurred_at: int

    def __post_init__(self):
        from camctl.operations.attempts import AttemptConfig
        from camctl.capture.media import RecordingFailure
        if not isinstance(self.ref,SegmentResultRef) or not isinstance(self.reason,ResultCheckEndReason):
            raise TypeError('有限决定必须使用实际集合和结束分类')
        for value in (self.anchor_event_id,self.prior_run_end_event_id,self.prior_conclusion_id):
            if value is not None:ObjectId(value)
        if self.check_config is not None and not isinstance(self.check_config,AttemptConfig):
            raise TypeError('有限决定必须保留完整采用配置')
        if self.binding_failure is not None and not isinstance(self.binding_failure,RecordingFailure):
            raise TypeError('绑定决定必须使用原有限失败输入')
        UtcMicros(self.occurred_at)


@dataclass(frozen=True)
class ResultCheckConclusionRef:
    ref: SegmentResultRef
    conclusion_id: int
    event_id: int

    def __post_init__(self):
        if not isinstance(self.ref,SegmentResultRef):raise TypeError('有限决定引用必须属于实际结果集合')
        ObjectId(self.conclusion_id);ObjectId(self.event_id)


def encode_enum(value):
    return int(enum_for(_ENUM_COLUMNS[type(value)])[value.name])


def decode_enum(kind, value):
    return decode_text_enum(kind, value, column=_ENUM_COLUMNS[kind])


def document(value):
    """只编码有限模型；调用者不得传增长集合。"""
    from camctl.capture.media import RecordingFailure

    if isinstance(value, RecordingFailure):
        from camctl.contracts.workflow_errors import validate_error_details

        if value.code != "device_binding_unavailable":
            raise ValueError("有限决定只接收已登记的绑定失败输入")
        validate_error_details(value.code, value.details)
        return {"code": value.code, "details": dict(value.details)}
    if isinstance(value, StrEnum):
        return encode_enum(value) if type(value) in _ENUM_COLUMNS else value.value
    if is_dataclass(value):
        return {field.name: document(getattr(value, field.name)) for field in fields(value)}
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value,Decimal) and value.is_finite():
        return value
    raise ValueError('分段保存只接受固定大小模型')


def key(value):
    return None if value is None else SegmentKey(**value)


def cursor(value):
    return None if value is None else models.SegmentCursor(key(value['key']), value['member_identity'])


def gap_from_document(value):
    if value is None:
        return None
    raw = dict(value)
    for name in ('start_key', 'end_key'):
        raw[name] = key(raw[name])
    raw['kind'] = decode_enum(models.SegmentGapKind, raw['kind'])
    for name in ('left_neighbor_state', 'right_neighbor_state'):
        raw[name] = decode_enum(models.SegmentGapNeighborState, raw[name])
    return models.SegmentGap(**raw)


def facts_from_document(value):
    if value is None:
        return None
    if value == _NO_COMPLETION_FACTS:
        return None
    raw = dict(value)
    for name in ('stop','file_wait','scan','start','control_completion','control_overrun'):
        raw[name] = decode_enum(models.EvidenceState, raw[name])
    raw['start_key'] = key(raw['start_key'])
    if raw.get("start_observation") is not None:
        raw["start_observation"] = models.SegmentStartObservation(
            **raw["start_observation"]
        )
    return CompletionFacts(**raw)


def assessment_document(value):
    raw = document(value)
    if isinstance(value, SegmentDecision):
        return {"decision_type": "video_coverage_v1", **raw}
    # 下界可能超过 SQLite 整数，规范文本保持精确，None 保持未知。
    raw['lower_bound_ms'] = None if value.lower_bound_ms is None else str(value.lower_bound_ms)
    return raw


def assessment_from_document(value):
    raw = dict(value)
    if raw.get("decision_type") == "video_coverage_v1":
        return decision_from_document(raw)
    if "decision_type" in raw:
        raise ValueError("分段决策判别类型未登记")
    for name, kind in [('status',models.SegmentAssessmentStatus),('reason',models.SegmentAssessmentReason),('basis',models.CompletionBasis),('overrecord',models.OverrecordStatus)]:
        raw[name] = decode_enum(kind,raw[name])
    lower = raw['lower_bound_ms']
    if lower is not None:
        if not isinstance(lower,str) or not lower.isascii() or not lower.isdecimal() or str(int(lower)) != lower:
            raise ValueError('分段下界必须是规范非负十进制文本')
        raw['lower_bound_ms'] = int(lower)
    raw['set_ref'] = SegmentSetRef(**raw['set_ref'])
    for name in ('last_nonzero_cursor','last_owned_cursor'):
        raw[name] = cursor(raw[name])
    raw['confirmed_start_key'] = key(raw['confirmed_start_key'])
    raw['first_gap'] = gap_from_document(raw['first_gap'])
    scope = dict(raw['default_scope'])
    scope['state'] = decode_enum(models.DefaultScopeState,scope['state'])
    scope['basis'] = decode_enum(models.DefaultScopeBasis,scope['basis'])
    scope['set_ref'] = SegmentSetRef(**scope['set_ref'])
    for name in ('first_cursor','last_cursor'):
        scope[name] = cursor(scope[name])
    raw['default_scope'] = models.DefaultSegmentScope(**scope)
    return SegmentAssessment(**raw)


def decision_from_document(value):
    """只解码 Action6 的公共有限头，不尝试解释为 OSMO 判断。"""
    raw = dict(value)
    if raw.pop("decision_type", None) != "video_coverage_v1":
        raise ValueError("公共分段决策类型未登记")
    raw["set_ref"] = SegmentSetRef(**raw["set_ref"])
    raw["status"] = SegmentDecisionStatus(raw["status"])
    raw["metric"] = CoverageMetric(raw["metric"])
    raw["coverage_status"] = CoverageStatus(raw["coverage_status"])
    if raw["metric"] is CoverageMetric.DURATION_S:
        raw["target"] = Decimal(raw["target"])
        if raw["confirmed"] is not None:
            raw["confirmed"] = Decimal(raw["confirmed"])
    scope = dict(raw["default_scope"])
    scope["state"] = decode_enum(models.DefaultScopeState, scope["state"])
    scope["set_ref"] = SegmentSetRef(**scope["set_ref"])
    for name in ("first_cursor", "last_cursor"):
        scope[name] = cursor(scope[name])
    raw["default_scope"] = SegmentDecisionScope(**scope)
    return SegmentDecision(**raw)


@dataclass(frozen=True)
class SegmentSetCreate:
    ref: SegmentSetRef
    activity_id: int
    run_id: int
    target_duration_ms: int
    occurred_at: int

    def __post_init__(self):
        if not isinstance(self.ref, SegmentSetRef):
            raise TypeError('必须使用完整集合引用')
        for name in ('activity_id','run_id','target_duration_ms'):
            ObjectId(getattr(self,name))
        UtcMicros(self.occurred_at)


@dataclass(frozen=True)
class SegmentMemberInput:
    """原申请只冻结引用和分类，不再次接受文件属性。"""
    file_id: int
    page_event_id: int
    file_event_id: int
    role: models.SegmentMemberRole
    ownership: models.OwnershipState
    completion: models.FileCompletionState
    pairing_identity: str | None = None
    pairing_state: models.PreviewPairingState = models.PreviewPairingState.NOT_APPLICABLE
    pairing_fact_id: int | None = None

    def __post_init__(self):
        for name in ('file_id','page_event_id','file_event_id'):
            ObjectId(getattr(self,name))
        if self.pairing_fact_id is not None:
            ObjectId(self.pairing_fact_id)
            if self.role is not models.SegmentMemberRole.PREVIEW:
                raise ValueError('只有预览成员可以引用配对事实')
        # P1 模型承担分类组合校验；此处没有独立的文件属性输入。
        models.SegmentMember('reference',None,self.role,self.ownership,self.completion,None,
            self.pairing_identity,self.pairing_state)


@dataclass(frozen=True)
class SegmentPageCursor:
    ref: SegmentSetRef | SegmentResultRef
    position: tuple

    def __post_init__(self):
        if (
            not isinstance(self.ref, (SegmentSetRef, SegmentResultRef))
            or type(self.position) is not tuple
        ):
            raise ValueError('分页位置必须绑定冻结集合并使用元组')


@dataclass(frozen=True)
class SegmentMemberCursor(SegmentPageCursor):
    """按完整设备顺序续读成员；零只是空键技术分区，不是设备键。"""

    def __post_init__(self):
        super().__post_init__()
        if len(self.position)!=4:
            raise ValueError('成员分页位置必须保留完整技术排序键')
        directory,index,identity,row_id=self.position
        ObjectId(row_id)
        if type(directory) is not int or type(index) is not int:
            raise ValueError('成员排序分区必须是整数')
        if (directory,index)!=(0,0):SegmentKey(directory,index)
        if not isinstance(identity,str) or not identity:
            raise ValueError('成员分页位置必须包含真实稳定身份')



@dataclass(frozen=True)
class SegmentGapCursor(SegmentPageCursor):
    """按缺口序号续读。"""

    def __post_init__(self):
        super().__post_init__()
        if len(self.position)!=1:raise ValueError('缺口分页位置必须是单一序号')
        ObjectId(self.position[0])



@dataclass(frozen=True)
class SegmentScopeCursor(SegmentPageCursor):
    """按默认成员序号续读。"""

    def __post_init__(self):
        super().__post_init__()
        if len(self.position)!=1:raise ValueError('默认范围分页位置必须是单一序号')
        ObjectId(self.position[0])



@dataclass(frozen=True)
class StoredSegmentMember:
    member_id: int
    file_id: int
    page_event_id: int
    file_event_id: int
    member: SegmentMember
    pairing_fact_id: int | None = None


@dataclass(frozen=True)
class SegmentOutputCandidate:
    """一个冻结成员应登记的正式产物及其固定原片关联，不携带文件属性。"""
    member_id: int
    file_id: int
    kind: OutputKind
    original_member_id: int | None = None
    original_file_id: int | None = None

    def __post_init__(self):
        ObjectId(self.member_id)
        ObjectId(self.file_id)
        if self.kind not in (OutputKind.ORIGINAL,OutputKind.PREVIEW):
            raise ValueError('分段候选只能为原片或预览')
        paired=self.kind is OutputKind.PREVIEW
        if (self.original_member_id is not None)!=paired or (self.original_file_id is not None)!=paired:
            raise ValueError('预览候选必须明确其合格原片，原片不得携带origin')
        if paired:
            ObjectId(self.original_member_id)
            ObjectId(self.original_file_id)
