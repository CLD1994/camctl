"""分段处理、持久化与报告共用的有限值和登记编码。"""

from dataclasses import dataclass
from enum import StrEnum

from camctl.contracts.enums import enum_for
from camctl.contracts.values import MAX_OBJECT_ID


def text_enum_for(name, column, *, module):
    """只从权威整数登记派生文本成员。"""
    return StrEnum(name, {member.name: member.name.lower() for member in enum_for(column)}, module=module)


def decode_text_enum(kind, value, *, column):
    if type(value) is not int:
        raise ValueError('保存的分段枚举必须是整数')
    return kind[enum_for(column)(value).name]


DefaultScopeState = text_enum_for("DefaultScopeState", "segment_sets.assessment_json.scope_state", module=__name__)

SegmentGapKind = text_enum_for("SegmentGapKind", "segment_gaps.kind", module=__name__)

SegmentGapNeighborState = text_enum_for("SegmentGapNeighborState", "segment_gaps.neighbors_json.state", module=__name__)


@dataclass(frozen=True, order=True, slots=True)
class SegmentKey:
    """CAM 目录编号和目录内 index 的数值顺序，不表达文件归属。"""

    directory_number: int
    index: int

    def __post_init__(self) -> None:
        if (type(self.directory_number) is not int
                or not 1 <= self.directory_number <= MAX_OBJECT_ID):
            raise ValueError("CAM 目录编号必须是正安全整数")
        if type(self.index) is not int or not 1 <= self.index <= 999:
            raise ValueError("分段 index 必须是 1～999 的整数")

    def successor(self) -> "SegmentKey":
        """返回设备命名顺序中的下一个位置。"""
        if self.index < 999:
            return SegmentKey(self.directory_number, self.index + 1)
        return SegmentKey(self.directory_number + 1, 1)

    def predecessor(self) -> "SegmentKey | None":
        """返回设备命名顺序中的前一个位置；首个位置没有前项。"""
        if self.index > 1:
            return SegmentKey(self.directory_number, self.index - 1)
        if self.directory_number > 1:
            return SegmentKey(self.directory_number - 1, 999)
        return None


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
