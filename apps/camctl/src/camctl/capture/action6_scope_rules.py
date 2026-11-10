"""Action6 冻结目标与固定成员范围的有界业务复核。"""

from camctl.capture.segment_candidates import eligible_original
from camctl.capture.segment_models import CoverageStatus, DefaultScopeState, SegmentDecisionStatus
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError


def require_action6_coverage_header(decision, target):
    """核对已保存覆盖结论和冻结目标的直接关系。"""
    if (decision.metric, decision.target) != (target.metric, target.value):
        raise ConsistencyError('Action6 覆盖目标与受理时冻结参数不同')
    if decision.coverage_status is CoverageStatus.NOT_PERFORMED:
        if decision.confirmed is not None or decision.checked_count != 0:
            raise ConsistencyError('未执行 Action6 媒体检查不得携带实测值或检查计数')
    elif decision.coverage_status is CoverageStatus.REACHED:
        if decision.confirmed is None or decision.confirmed < decision.target:
            raise ConsistencyError('Action6 达标结论低于冻结目标')
    elif decision.coverage_status is CoverageStatus.CONFIRMED_SHORT:
        if decision.confirmed is None or decision.confirmed >= decision.target:
            raise ConsistencyError('Action6 不足结论未低于冻结目标')


class Action6ScopeAssessment:
    """逐项消费固定成员，只持有范围与顺序的有限摘要。"""

    def __init__(self, decision):
        self.decision = decision
        self.eligible_count = 0
        self.original_count = 0
        self.first_cursor = None
        self.last_cursor = None
        self.cursor_at_checked_count = None
        self.prefix_count = 0
        self.prefix_first_cursor = None
        self.prefix_last_cursor = None
        self.prefix_open = True
        self.ordered = True
        self.cursor_missing = False
        self.known_gap = False
        self.previous_key = None

    def observe(self, member):
        if not eligible_original(member):
            return
        self.eligible_count += 1
        if member.cursor is None:
            self.ordered = False
            self.cursor_missing = True
            return
        self.original_count += 1
        self.first_cursor = member.cursor if self.first_cursor is None else min(self.first_cursor, member.cursor)
        self.last_cursor = member.cursor if self.last_cursor is None else max(self.last_cursor, member.cursor)
        if self.previous_key is not None:
            if member.key <= self.previous_key:
                self.ordered = False
            else:
                try:
                    if self.previous_key.successor() != member.key:
                        self.known_gap = True
                        self.prefix_open = False
                except ValueError:
                    self.known_gap = True
                    self.prefix_open = False
        self.previous_key = member.key
        if self.prefix_open:
            self.prefix_count += 1
            if self.prefix_first_cursor is None:
                self.prefix_first_cursor = member.cursor
            self.prefix_last_cursor = member.cursor
        if self.original_count == self.decision.checked_count:
            self.cursor_at_checked_count = member.cursor

    def require_decision(self, *, action_type, source_kind, publication_kind):
        decision = self.decision
        selected = decision.default_scope
        native_cancel_all = (decision.status is SegmentDecisionStatus.CANCELED
                             and action_type == 'camera_timelapse')
        full_scope = (decision.coverage_status is CoverageStatus.NOT_PERFORMED
                      or decision.status is SegmentDecisionStatus.CANCELED)
        if selected.state is DefaultScopeState.EMPTY:
            if native_cancel_all:
                expected_empty = self.eligible_count == 0
            elif full_scope:
                expected_empty = self.prefix_count == 0
            else:
                expected_empty = decision.checked_count == 0
            if not expected_empty:
                raise ConsistencyError('Action6 空视频范围仍存在应纳入的合格原片')
        elif selected.state is DefaultScopeState.NONEMPTY:
            if full_scope and native_cancel_all:
                expected_count, expected_first, expected_last = self.original_count, self.first_cursor, self.last_cursor
            elif full_scope:
                expected_count = self.prefix_count if self.known_gap else self.original_count
                expected_first = self.prefix_first_cursor if self.known_gap else self.first_cursor
                expected_last = self.prefix_last_cursor if self.known_gap else self.last_cursor
            else:
                expected_count, expected_first, expected_last = (
                    decision.checked_count, self.first_cursor, self.cursor_at_checked_count)
            if native_cancel_all and self.cursor_missing:
                raise ConsistencyError('Action6 原生取消全量视频范围缺少可排序游标')
            if ((full_scope and not self.ordered and not native_cancel_all)
                    or expected_count != selected.member_count
                    or expected_first != selected.first_cursor
                    or expected_last != selected.last_cursor):
                raise ConsistencyError('Action6 默认范围必须是有序合格视频的完整范围或已检查前缀')
        elif selected.state is not DefaultScopeState.UNKNOWN:
            raise ConsistencyError('Action6 默认范围状态无效')
        if decision.status is SegmentDecisionStatus.SUCCEEDED:
            checked_scope_unconfirmed = decision.coverage_status is CoverageStatus.UNCONFIRMED
            if (source_kind != int(enum_for('segment_sets.source_kind').COMPLETE_SCAN)
                    or (selected.state is not DefaultScopeState.NONEMPTY and not checked_scope_unconfirmed)
                    or decision.coverage_status is CoverageStatus.NOT_PERFORMED and self.known_gap):
                raise ConsistencyError('Action6 正常成功缺少完整扫描或符合检查状态的默认视频范围')
        if decision.status is SegmentDecisionStatus.CANCELED:
            if publication_kind != int(enum_for('segment_sets.publication_kind').CANCELED):
                raise ConsistencyError('Action6 取消必须使用原取消发布类型')
        elif publication_kind == int(enum_for('segment_sets.publication_kind').CANCELED):
            raise ConsistencyError('Action6 取消发布必须保存取消决策')
