"""分段有限错误头及单个缺口的共享公开表示。"""

from dataclasses import dataclass
from camctl.contracts.segment_values import (
    DefaultScopeState, SegmentGap, SegmentGapKind, SegmentGapNeighborState, SegmentKey,
)
from camctl.contracts.values import ObjectId
from camctl.contracts.workflow_errors import validate_error_details


def public_gap(gap: SegmentGap):
    def neighbor(side):
        state = getattr(gap, side + '_neighbor_state')
        value = {'state': state.value}
        identity = getattr(gap, side + '_member_identity')
        if identity is not None:
            value['file_identity'] = identity
        return value
    result = {'kind':gap.kind.value,
        'start_key':{'directory_number':gap.start_key.directory_number,'index':gap.start_key.index},
        'end_key':{'directory_number':gap.end_key.directory_number,'index':gap.end_key.index},
        'left':neighbor('left'),'right':neighbor('right')}
    if gap.empty_member_identity is not None:
        result['empty_file_identity'] = gap.empty_member_identity
    return result


@dataclass(frozen=True)
class SegmentErrorHeader:
    source_action_id: int
    gap_count: int | None
    scope_state: DefaultScopeState
    scope_count: int | None
    retained_original_count: int
    first_gap: SegmentGap | None = None

    def __post_init__(self):
        ObjectId(self.source_action_id)
        for value in (self.retained_original_count,):
            if type(value) is not int or value < 0:
                raise ValueError('错误头计数必须是非负整数')
        if self.gap_count is not None and (type(self.gap_count) is not int or self.gap_count < 0):
            raise ValueError('缺口计数必须是非负整数或未知')
        if not isinstance(self.scope_state,DefaultScopeState):
            raise ValueError('错误头范围状态未登记')
        if ((self.scope_state is DefaultScopeState.UNKNOWN and self.scope_count is not None)
            or (self.scope_state is DefaultScopeState.EMPTY and (type(self.scope_count) is not int or self.scope_count != 0))
            or (self.scope_state is DefaultScopeState.NONEMPTY and (type(self.scope_count) is not int or self.scope_count <= 0))):
            raise ValueError('错误头范围状态与计数矛盾')
        if self.scope_count is not None and self.scope_count > self.retained_original_count:
            raise ValueError('默认数量不能超过保留原片数')
        if (self.gap_count in (None,0)) != (self.first_gap is None):
            raise ValueError('缺口数量和首个缺口矛盾')

    def details(self, code: str):
        scope_text = ('尚未确认' if self.scope_state is DefaultScopeState.UNKNOWN else
            '已确定为空' if self.scope_state is DefaultScopeState.EMPTY else f'包含 {self.scope_count} 份原片')
        gap_text = '缺口分类尚未确认' if self.gap_count is None else f'确认 {self.gap_count} 处缺口'
        description = f'来源动作 {self.source_action_id}：{gap_text}；默认范围{scope_text}。'
        if self.first_gap is not None:
            start,end=self.first_gap.start_key,self.first_gap.end_key
            description += f'首个缺口位于目录 {start.directory_number} 的索引 {start.index:04d} 至目录 {end.directory_number} 的索引 {end.index:04d}。'
        raw = {'source_action_instance_id':str(self.source_action_id),'description':description,
            'gap_count':self.gap_count,'default_scope':{'state':self.scope_state.value,'member_count':self.scope_count},
            'retained_original_count':self.retained_original_count}
        if self.first_gap is not None:
            raw['first_gap'] = public_gap(self.first_gap)
        validate_error_details(code,raw)
        return raw


def validate_segment_error_header(code: str, value) -> SegmentErrorHeader:
    """校验持久化有限头；完整公开数组必须使用 P6 的完整边界校验。"""
    validate_error_details(code,value)
    if 'gaps' in value or 'retained_output_ids' in value or 'output_ids' in value['default_scope']:
        raise ValueError('持久化错误头不接受增长详情数组')
    raw=value.get('first_gap')
    gap=None
    if raw is not None:
        if any('output_id' in raw[side] for side in ('left','right')):
            raise ValueError('持久化首缺口只保存文件身份，output 身份由同H公开展开核实')
        gap=SegmentGap(SegmentKey(**raw['start_key']),SegmentKey(**raw['end_key']),SegmentGapKind(raw['kind']),
            raw['left'].get('file_identity'),raw['right'].get('file_identity'),
            SegmentGapNeighborState(raw['left']['state']),SegmentGapNeighborState(raw['right']['state']),raw.get('empty_file_identity'))
    scope=value['default_scope']
    return SegmentErrorHeader(int(value['source_action_instance_id']),value['gap_count'],DefaultScopeState(scope['state']),
        scope['member_count'],value['retained_original_count'],gap)
