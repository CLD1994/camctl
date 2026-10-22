"""拍摄分段规则使用的稳定事实、有限摘要与结果分类。"""

from dataclasses import dataclass
from enum import StrEnum

from camctl.contracts.values import MAX_OBJECT_ID, ObjectId
from camctl.contracts.enums import enum_for
from camctl.devices.segments import SegmentKey


def _text_enum(name, column):
    """从唯一整数登记派生 P1 文本枚举，保留原 StrEnum 契约。"""
    return StrEnum(
        name, {member.name: member.name.lower() for member in enum_for(column)},
        module=__name__,
    )


EvidenceState = _text_enum("EvidenceState", "segment_sets.facts_json.evidence")

SegmentSourceKind = _text_enum('SegmentSourceKind','segment_sets.source_kind')

PublicationKind = _text_enum('PublicationKind','segment_sets.publication_kind')

ResultCheckEndReason = _text_enum('ResultCheckEndReason','segment_check_conclusions.reason')

CurrentSegmentResult = _text_enum('CurrentSegmentResult','segment_check_conclusions.current_result')


SegmentMemberRole = _text_enum("SegmentMemberRole", "segment_members.role")


OwnershipState = _text_enum("OwnershipState", "segment_members.ownership")


FileCompletionState = _text_enum("FileCompletionState", "segment_members.completion")


PreviewPairingState = _text_enum("PreviewPairingState", "segment_members.pairing_state")


SegmentAssessmentStatus = _text_enum("SegmentAssessmentStatus", "segment_sets.assessment_json.status")


SegmentAssessmentReason = _text_enum("SegmentAssessmentReason", "segment_sets.assessment_json.reason")


CompletionBasis = _text_enum("CompletionBasis", "segment_sets.assessment_json.basis")


OverrecordStatus = _text_enum("OverrecordStatus", "segment_sets.assessment_json.overrecord")


DefaultScopeState = _text_enum("DefaultScopeState", "segment_sets.assessment_json.scope_state")


DefaultScopeBasis = _text_enum("DefaultScopeBasis", "segment_sets.assessment_json.scope_basis")


SegmentGapKind = _text_enum("SegmentGapKind", "segment_gaps.kind")


SegmentGapNeighborState = _text_enum("SegmentGapNeighborState", "segment_gaps.neighbors_json.state")


@dataclass(frozen=True, slots=True)
class SegmentSetRef:
    """一个冻结结果集合的有限稳定引用。"""

    action_id: int
    scan_id: int
    final_page_event_id: int
    version: int

    def __post_init__(self) -> None:
        for name in ("action_id", "scan_id", "final_page_event_id", "version"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= MAX_OBJECT_ID:
                raise ValueError(f"{name} 必须是正安全整数")


@dataclass(frozen=True, order=True, slots=True)
class SegmentCursor:
    """集合内一个成员的完整设备顺序键和稳定身份。"""

    key: SegmentKey
    member_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, SegmentKey):
            raise ValueError("分段游标必须携带设备顺序键")
        if not isinstance(self.member_identity, str) or not self.member_identity:
            raise ValueError("分段游标必须携带稳定成员身份")
        self.member_identity.encode("utf-8")


@dataclass(frozen=True, slots=True)
class SegmentMember:
    """单个文件的有界事实；不携带整个结果集合。"""

    identity: str
    key: SegmentKey | None
    role: SegmentMemberRole
    ownership: OwnershipState
    completion: FileCompletionState
    size_bytes: int | None
    pairing_identity: str | None = None
    pairing_state: PreviewPairingState = PreviewPairingState.NOT_APPLICABLE

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity:
            raise ValueError("分段成员必须携带稳定文件身份")
        self.identity.encode("utf-8")
        if self.key is not None and not isinstance(self.key, SegmentKey):
            raise ValueError("分段成员顺序键无效")
        if not isinstance(self.role, SegmentMemberRole):
            raise ValueError("分段成员角色未登记")
        if not isinstance(self.ownership, OwnershipState):
            raise ValueError("分段成员归属状态未登记")
        if not isinstance(self.completion, FileCompletionState):
            raise ValueError("分段成员写入状态未登记")
        if not isinstance(self.pairing_state, PreviewPairingState):
            raise ValueError("预览配对状态未登记")
        if (self.role is SegmentMemberRole.PREVIEW
                and self.pairing_state is PreviewPairingState.NOT_APPLICABLE):
            raise ValueError("预览成员必须明确配对已确认、未配对或未知")
        if (self.role is not SegmentMemberRole.PREVIEW
                and self.pairing_state is not PreviewPairingState.NOT_APPLICABLE):
            raise ValueError("原片和其他文件不得携带预览配对状态")
        if self.size_bytes is not None and (
                type(self.size_bytes) is not int
                or not 0 <= self.size_bytes <= MAX_OBJECT_ID):
            raise ValueError("文件长度必须是安全非负整数或未知")
        if self.pairing_identity is not None:
            if not isinstance(self.pairing_identity, str) or not self.pairing_identity:
                raise ValueError("配对身份必须是非空文本或未知")
            self.pairing_identity.encode("utf-8")
        if ((self.pairing_state is PreviewPairingState.PAIRED)
                != (self.pairing_identity is not None)):
            raise ValueError("已确认配对必须提供身份，其他配对状态不得冒用身份")

    @property
    def cursor(self) -> SegmentCursor | None:
        return None if self.key is None else SegmentCursor(self.key, self.identity)


@dataclass(frozen=True, slots=True)
class SegmentStartObservation:
    """可信起点引用原固定基准及实际观察，不复制文件属性。"""

    baseline_fixed_event_id: int
    page_event_id: int
    file_id: int

    def __post_init__(self) -> None:
        for value in (self.baseline_fixed_event_id, self.page_event_id, self.file_id):
            ObjectId(value)
        if self.baseline_fixed_event_id >= self.page_event_id:
            raise ValueError("起点观察必须发生在原基准可靠固定之后")


@dataclass(frozen=True, slots=True)
class SegmentStart:
    """实际目录事实推导出的起点；未知时不携带猜测位置。"""

    key: SegmentKey | None = None
    observation: SegmentStartObservation | None = None

    def __post_init__(self) -> None:
        if (self.key is None) != (self.observation is None):
            raise ValueError("起点位置和原始观察引用必须共同存在或共同未知")
        if self.key is not None and not isinstance(self.key, SegmentKey):
            raise TypeError("起点位置必须使用设备分段顺序键")
        if self.observation is not None and not isinstance(
            self.observation, SegmentStartObservation
        ):
            raise TypeError("起点必须引用实际目录观察")


@dataclass(frozen=True, slots=True)
class CompletionFacts:
    """完成判定所需的控制、扫描、起点和有限核实事实。"""

    stop: EvidenceState
    file_wait: EvidenceState
    scan: EvidenceState
    start: EvidenceState
    start_key: SegmentKey | None
    control_completion: EvidenceState
    control_overrun: EvidenceState
    finite_check_complete: bool
    explicit_error: bool
    start_observation: SegmentStartObservation | None = None

    def __post_init__(self) -> None:
        for name in ("stop", "file_wait", "scan", "start",
                     "control_completion", "control_overrun"):
            if not isinstance(getattr(self, name), EvidenceState):
                raise ValueError(f"{name} 状态未登记")
        if self.start_key is not None and not isinstance(self.start_key, SegmentKey):
            raise ValueError("可靠起点必须使用分段顺序键")
        if (self.start is EvidenceState.CONFIRMED) != (self.start_key is not None):
            raise ValueError("可靠起点状态与起点顺序键必须共同表达")
        if self.start_observation is not None and (
            not isinstance(self.start_observation, SegmentStartObservation)
            or self.start is not EvidenceState.CONFIRMED
        ):
            raise ValueError("起点观察只用于已确认起点")
        if type(self.finite_check_complete) is not bool or type(self.explicit_error) is not bool:
            raise ValueError("有限核实与明确错误必须是布尔值")


@dataclass(frozen=True, slots=True)
class DefaultSegmentScope:
    """冻结结果集合中的默认原片范围，不展开成员列表。"""

    state: DefaultScopeState
    basis: DefaultScopeBasis
    set_ref: SegmentSetRef
    first_cursor: SegmentCursor | None
    last_cursor: SegmentCursor | None
    member_count: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, DefaultScopeState) or not isinstance(self.basis, DefaultScopeBasis):
            raise ValueError("默认范围状态或依据未登记")
        if not isinstance(self.set_ref, SegmentSetRef):
            raise ValueError("默认范围必须引用冻结结果集合")
        if self.state is DefaultScopeState.UNKNOWN:
            if (self.basis is not DefaultScopeBasis.UNKNOWN or self.first_cursor is not None
                    or self.last_cursor is not None or self.member_count is not None):
                raise ValueError("未知默认范围不得携带猜测边界")
        elif self.state is DefaultScopeState.EMPTY:
            if (self.basis is DefaultScopeBasis.UNKNOWN or self.first_cursor is not None
                    or self.last_cursor is not None or self.member_count != 0):
                raise ValueError("已确定空范围必须明确为零成员且不带游标")
        else:
            if (self.basis is DefaultScopeBasis.UNKNOWN or self.first_cursor is None
                    or self.last_cursor is None or type(self.member_count) is not int
                    or self.member_count <= 0 or self.last_cursor < self.first_cursor):
                raise ValueError("非空默认范围必须提供有效首尾游标和成员数")


@dataclass(frozen=True, slots=True)
class SegmentGap:
    """一个缺失区间或一个非尾部零字节原片的结构化诊断。"""

    start_key: SegmentKey
    end_key: SegmentKey
    kind: SegmentGapKind
    left_member_identity: str | None
    right_member_identity: str | None
    left_neighbor_state: SegmentGapNeighborState
    right_neighbor_state: SegmentGapNeighborState
    empty_member_identity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.start_key, SegmentKey) or not isinstance(self.end_key, SegmentKey):
            raise ValueError("缺口必须提供可靠起止顺序键")
        if self.end_key < self.start_key:
            raise ValueError("缺口结束位置不能早于起始位置")
        if not isinstance(self.kind, SegmentGapKind):
            raise ValueError("缺口类型未登记")
        if (self.kind is SegmentGapKind.ZERO_BYTE_NON_TAIL
                and self.start_key != self.end_key):
            raise ValueError("零字节缺口必须只指向一个OSV位置")
        for name in ("left_neighbor_state", "right_neighbor_state"):
            if not isinstance(getattr(self, name), SegmentGapNeighborState):
                raise ValueError(f"{name} 未登记")
        for name in ("left_member_identity", "right_member_identity", "empty_member_identity"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise ValueError(f"{name} 必须是非空文件身份或未知")
                value.encode("utf-8")
        for side in ("left", "right"):
            state = getattr(self, f"{side}_neighbor_state")
            identity = getattr(self, f"{side}_member_identity")
            if (state is SegmentGapNeighborState.KNOWN) != (identity is not None):
                raise ValueError("已知邻接必须提供身份，未知或不存在邻接不得冒用身份")
        if ((self.kind is SegmentGapKind.ZERO_BYTE_NON_TAIL)
                != (self.empty_member_identity is not None)):
            raise ValueError("零字节缺口必须引用真实OSV身份，缺失位置不得携带文件身份")


@dataclass(frozen=True, slots=True)
class SegmentAccumulator:
    """逐成员推进的 O(1) 状态；完整缺口由冻结集合第二遍生成。"""

    set_ref: SegmentSetRef
    target_duration_ms: int
    confirmed_start_key: SegmentKey | None
    last_original: SegmentMember | None = None
    first_original: SegmentMember | None = None
    last_position_anchor: SegmentMember | None = None
    first_position_anchor: SegmentMember | None = None
    last_nonzero_cursor: SegmentCursor | None = None
    last_owned_cursor: SegmentCursor | None = None
    nonzero_original_count: int = 0
    unknown_original: bool = False
    optional_preview_unknown: bool = False
    order_unknown: bool = False
    conflicting_key: bool = False
    gap_count: int = 0
    first_gap: SegmentGap | None = None
    pending_gap_count: int = 0
    pending_first_gap: SegmentGap | None = None
    prefix_open: bool = True
    prefix_unknown: bool = False
    prefix_unknown_cursor: SegmentCursor | None = None
    prefix_first_cursor: SegmentCursor | None = None
    prefix_last_cursor: SegmentCursor | None = None
    prefix_limit_cursor: SegmentCursor | None = None
    prefix_count: int = 0
    trailing_zero_count: int = 0
    trailing_zero_first: SegmentMember | None = None
    first_n_cursor: SegmentCursor | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.set_ref, SegmentSetRef):
            raise ValueError("分段累积器必须绑定冻结结果集合")
        if type(self.target_duration_ms) is not int or not 1 <= self.target_duration_ms <= MAX_OBJECT_ID:
            raise ValueError("目标时长必须是正安全整数毫秒")
        if self.confirmed_start_key is not None and not isinstance(self.confirmed_start_key, SegmentKey):
            raise ValueError("已确认起点必须使用分段顺序键或未知")
        for name in ("nonzero_original_count", "gap_count", "pending_gap_count",
                     "prefix_count", "trailing_zero_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        for name in ("last_original", "first_original", "last_position_anchor",
                     "first_position_anchor", "trailing_zero_first"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, SegmentMember):
                raise ValueError(f"{name} 必须是 SegmentMember 或未知")
        for name in ("last_original", "first_original"):
            member = getattr(self, name)
            if member is not None and (
                    member.role is not SegmentMemberRole.ORIGINAL
                    or member.ownership is not OwnershipState.OWNED
                    or member.key is None):
                raise ValueError(f"{name} 必须是具有可靠键的已归属原片")
        for name in ("last_position_anchor", "first_position_anchor"):
            member = getattr(self, name)
            if member is not None and (
                    member.role is not SegmentMemberRole.ORIGINAL
                    or member.ownership not in (OwnershipState.OWNED, OwnershipState.UNKNOWN)
                    or member.key is None):
                raise ValueError(f"{name} 必须是具有可靠键的可能原片位置锚点")
        if (self.first_position_anchor is not None
                and self.confirmed_start_key is not None
                and self.first_position_anchor.key < self.confirmed_start_key):
            raise ValueError("起点之前的成员不得作为本次录像的首位置锚点")
        for name in ("last_nonzero_cursor", "last_owned_cursor", "prefix_unknown_cursor",
                     "prefix_first_cursor", "prefix_last_cursor", "prefix_limit_cursor",
                     "first_n_cursor"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, SegmentCursor):
                raise ValueError(f"{name} 必须是 SegmentCursor 或未知")
        if ((self.last_original is None) != (self.last_owned_cursor is None)
                or (self.last_original is not None
                    and self.last_original.cursor != self.last_owned_cursor)):
            raise ValueError("最后归属原片与最后归属游标必须共同引用同一成员")
        if (self.last_nonzero_cursor is not None and self.last_owned_cursor is not None
                and self.last_nonzero_cursor.key > self.last_owned_cursor.key):
            raise ValueError("最后非零原片不能晚于最后归属原片")
        if self.first_gap is not None and not isinstance(self.first_gap, SegmentGap):
            raise ValueError("首个缺口必须是 SegmentGap 或未知")
        if self.pending_first_gap is not None and not isinstance(self.pending_first_gap, SegmentGap):
            raise ValueError("暂存首缺口必须是 SegmentGap 或未知")
        if (self.gap_count == 0) != (self.first_gap is None):
            raise ValueError("缺口数量与首缺口摘要必须同时存在或缺省")
        if (self.pending_gap_count == 0) != (self.pending_first_gap is None):
            raise ValueError("暂存缺口数量与首缺口锚点必须同时存在或缺省")
        if (self.trailing_zero_count == 0) != (self.trailing_zero_first is None):
            raise ValueError("尾部零字节数量与首个成员锚点必须同时存在或缺省")
        if self.trailing_zero_first is not None and (
                self.trailing_zero_first.key is None
                or self.trailing_zero_first.size_bytes != 0
                or self.trailing_zero_first.completion is not FileCompletionState.COMPLETE
                or self.trailing_zero_first.ownership is not OwnershipState.OWNED
                or self.trailing_zero_first.role is not SegmentMemberRole.ORIGINAL):
            raise ValueError("尾部零字节锚点必须是完整、归属确认的零字节 OSV")
        if (self.prefix_count == 0) != (self.prefix_first_cursor is None):
            raise ValueError("连续前缀数量与首游标必须同时存在或缺省")
        if (self.prefix_count == 0) != (self.prefix_last_cursor is None):
            raise ValueError("连续前缀数量与末游标必须同时存在或缺省")
        if self.prefix_first_cursor is not None and self.prefix_last_cursor < self.prefix_first_cursor:
            raise ValueError("连续前缀末游标不能早于首游标")
        if self.prefix_count > self.nonzero_original_count:
            raise ValueError("连续前缀成员数不能超过已知非零原片数")
        if (type(self.unknown_original) is not bool
                or type(self.optional_preview_unknown) is not bool
                or type(self.order_unknown) is not bool
                or type(self.prefix_unknown) is not bool):
            raise ValueError("原片、预览、顺序或范围未知必须是布尔值")
        if not self.prefix_unknown and self.prefix_unknown_cursor is not None:
            raise ValueError("可靠前缀不得保留未知成员游标")
        if type(self.conflicting_key) is not bool or type(self.prefix_open) is not bool:
            raise ValueError("排序键冲突与连续前缀状态必须是布尔值")


@dataclass(frozen=True, slots=True)
class SegmentAssessment:
    """完成依据、结果分类与稳定默认范围的有限摘要。"""

    status: SegmentAssessmentStatus
    reason: SegmentAssessmentReason
    basis: CompletionBasis
    complete_segment_count: int | None
    lower_bound_ms: int | None
    last_nonzero_cursor: SegmentCursor | None
    last_owned_cursor: SegmentCursor | None
    confirmed_start_key: SegmentKey | None
    overrecord: OverrecordStatus
    default_scope: DefaultSegmentScope
    set_ref: SegmentSetRef
    target_duration_ms: int
    gap_count: int | None
    first_gap: SegmentGap | None
    empty_tail_count: int | None
    optional_preview_unknown: bool


    def __post_init__(self) -> None:
        if not isinstance(self.status, SegmentAssessmentStatus):
            raise ValueError("分段结果状态未登记")
        if not isinstance(self.reason, SegmentAssessmentReason):
            raise ValueError("分段结果原因未登记")
        if not isinstance(self.basis, CompletionBasis):
            raise ValueError("分段完成依据未登记")
        if not isinstance(self.overrecord, OverrecordStatus):
            raise ValueError("异常多录状态未登记")
        if self.complete_segment_count is not None and (
                type(self.complete_segment_count) is not int
                or self.complete_segment_count < 0):
            raise ValueError("完整非尾段数必须是非负整数或未知")
        if self.lower_bound_ms is not None and (
                type(self.lower_bound_ms) is not int or self.lower_bound_ms < 0):
            raise ValueError("分段时长下界必须是非负整数毫秒或未知")
        if (self.complete_segment_count is None) != (self.lower_bound_ms is None):
            raise ValueError("完整段数与时长下界必须共同确定或共同未知")
        if self.last_nonzero_cursor is not None and not isinstance(self.last_nonzero_cursor, SegmentCursor):
            raise ValueError("最后非零原片必须通过稳定游标引用")
        if self.last_owned_cursor is not None and not isinstance(self.last_owned_cursor, SegmentCursor):
            raise ValueError("最后归属原片必须通过稳定游标引用")
        if (self.last_nonzero_cursor is not None and self.last_owned_cursor is not None
                and self.last_nonzero_cursor.key > self.last_owned_cursor.key):
            raise ValueError("最后非零原片不能晚于最后归属原片")
        if self.confirmed_start_key is not None and not isinstance(self.confirmed_start_key, SegmentKey):
            raise ValueError("已确认起点必须使用分段顺序键或未知")
        if not isinstance(self.default_scope, DefaultSegmentScope):
            raise ValueError("默认范围必须是 DefaultSegmentScope")
        if not isinstance(self.set_ref, SegmentSetRef) or self.default_scope.set_ref != self.set_ref:
            raise ValueError("结果与默认范围必须引用同一冻结集合")
        if (type(self.target_duration_ms) is not int
                or not 1 <= self.target_duration_ms <= MAX_OBJECT_ID):
            raise ValueError("目标时长必须是正安全整数毫秒")
        if self.gap_count is not None and (type(self.gap_count) is not int or self.gap_count < 0):
            raise ValueError("缺口数量必须是非负整数或未知")
        if self.first_gap is not None and not isinstance(self.first_gap, SegmentGap):
            raise ValueError("首个缺口必须是 SegmentGap 或未知")
        if (self.gap_count in (None, 0)) != (self.first_gap is None):
            raise ValueError("缺口数量与首缺口摘要必须同时存在或缺省")
        if self.empty_tail_count is not None and (
                type(self.empty_tail_count) is not int or self.empty_tail_count < 0):
            raise ValueError("空尾原片数必须是非负整数或未知")
        if type(self.optional_preview_unknown) is not bool:
            raise ValueError("可选预览核实状态必须是布尔值")
        if self.gap_count is None and (
                self.default_scope.state is not DefaultScopeState.UNKNOWN
                or self.complete_segment_count is not None or self.lower_bound_ms is not None
                or self.empty_tail_count is not None
                or self.status is SegmentAssessmentStatus.SUCCEEDED):
            raise ValueError("缺口分类未知不能声明可靠时长、空尾、范围或成功")
        if self.status is SegmentAssessmentStatus.SUCCEEDED:
            if self.reason is not SegmentAssessmentReason.NONE or self.basis is CompletionBasis.NONE:
                raise ValueError("成功结果必须带已登记完成依据且不带失败原因")
            if self.optional_preview_unknown:
                raise ValueError("仍有可选预览事实未知时不得确认成功")
        elif self.basis is not CompletionBasis.NONE:
            raise ValueError("非成功结果不能声明完成依据")
        if (self.reason is SegmentAssessmentReason.PREVIEW_UNKNOWN
                and (self.status is not SegmentAssessmentStatus.UNCONFIRMED
                     or not self.optional_preview_unknown)):
            raise ValueError("预览未知原因必须对应未确认且带可选预览未知标记的结果")
