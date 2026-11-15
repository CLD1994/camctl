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


def is_checked_prefix_original(member):
    """顺序复核仍需看到归属未知的原片，不能将它当成不存在。"""
    from camctl.capture.segment_models import OwnershipState, SegmentMemberRole
    return (member.role is SegmentMemberRole.ORIGINAL
            and member.ownership is not OwnershipState.NOT_OWNED)


class Action6CheckedPrefix:
    """消费实际相邻成员及其已保存检查事实，保留固定大小前缀摘要。"""

    def __init__(self, decision, target, start_key):
        from camctl.contracts.video_coverage import CoverageAccumulator, CoverageMetric, CoverageTarget
        self.decision = decision
        self.target = CoverageTarget(CoverageMetric(target.metric.value), target.value)
        self.accumulated = CoverageAccumulator(self.target)
        self.expected = start_key
        self.interruption = 'start_unknown' if start_key is None else None
        self.checked = 0
        self.first_cursor = None
        self.last_cursor = None
        self.reached = False
        self.previous_key = None

    def accept_member(self, member, following):
        """返回是否需要读取该成员的检查事实。"""
        from camctl.capture.segment_models import FileCompletionState, OwnershipState
        if self.expected is None:
            self.interruption = self.interruption or 'start_unknown'
            return False
        if member.key is None:
            self.interruption = 'order_unknown'
            return False
        if member.key != self.expected:
            self.interruption = 'order_unknown' if member.key == self.previous_key else 'segment_gap'
            return False
        if following is not None and following.key == member.key:
            self.interruption = 'order_unknown'
            return False
        if (member.ownership is not OwnershipState.OWNED
                or member.completion is not FileCompletionState.COMPLETE
                or member.size_bytes is None):
            self.interruption = 'file_unconfirmed'
            return False
        if member.size_bytes == 0:
            self.interruption = 'empty_original'
            return False
        return True

    def accept_inspection(self, member, inspection, copy):
        """验证该成员的原检查与副本，并返回是否继续读取下一成员。"""
        from camctl.contracts.video_coverage import CoverageMetric, VideoMetric, advance_coverage
        inspections = enum_for('segment_inspections.state')
        verification = enum_for('file_copies.verification_state')
        trusted = {int(verification.MATCHED), int(verification.SOURCE_CHECKSUM_UNAVAILABLE)}
        status = self.decision.coverage_status
        if self.checked < self.decision.checked_count:
            if (inspection is None or inspection.state != int(inspections.COMPLETED)
                    or inspection.target_json != {'metric': self.target.metric.value, 'value': self.target.value}
                    or copy is None or copy.inspection_state != int(inspections.COMPLETED)
                    or not copy.prepared or copy.verification_state not in trusted or copy.committed_bytes != copy.source_size
                    or copy.target_sha256 is None):
                raise ConsistencyError('Action6 已确认前缀必须引用同目标、完整校验的检查副本')
            document = inspection.result_json
            metric_doc = None if document is None else document.get('metric')
            if not isinstance(metric_doc, dict):
                raise ConsistencyError('Action6 已确认检查缺少保存的主视频指标')
            metric = VideoMetric(CoverageMetric(metric_doc.get('metric')),
                                 metric_doc.get('value'), metric_doc.get('reason'))
            if metric.metric is not self.target.metric or metric.value is None:
                raise ConsistencyError('Action6 已确认前缀包含可靠值之外的检查记录')
            if self.accumulated.reached:
                raise ConsistencyError('Action6 检查前缀在目标达成后仍包含后续成员')
            self.accumulated = advance_coverage(self.accumulated, metric)
            self.checked += 1
            if self.first_cursor is None:
                self.first_cursor = member.cursor
            self.last_cursor = member.cursor
            self.previous_key = member.key
            self.expected = member.key.successor()
            if self.accumulated.reached:
                if self.checked != self.decision.checked_count or status is not CoverageStatus.REACHED:
                    raise ConsistencyError('Action6 目标达成位置与保存的检查计数不一致')
                self.reached = True
                return False
            return True
        if status is CoverageStatus.UNCONFIRMED:
            if self.interruption is not None:
                return False
            if inspection is None or inspection.state == int(inspections.PENDING):
                raise ConsistencyError('Action6 未确认前缀缺少已收场的下一成员检查')
            document = inspection.result_json
            metric_doc = None if document is None else document.get('metric')
            if not isinstance(metric_doc, dict):
                raise ConsistencyError('Action6 未确认检查缺少保存的主视频指标')
            metric = VideoMetric(CoverageMetric(metric_doc.get('metric')),
                                 metric_doc.get('value'), metric_doc.get('reason'))
            if metric.metric is not self.target.metric or metric.value is not None:
                raise ConsistencyError('Action6 未确认中断必须落在未知指标处')
            self.interruption = metric.reason or 'metric_unknown'
            return False
        if status is CoverageStatus.CONFIRMED_SHORT:
            raise ConsistencyError('Action6 确认不足遗漏了连续范围内的实际原片')
        if status is CoverageStatus.REACHED and self.accumulated.reached:
            self.reached = True
            return False
        raise ConsistencyError('Action6 决策在可靠检查前缀后停止但未说明中断')

    def require_decision(self, *, source_kind, publication_kind):
        """在成员耗尽或可靠中断后复核结果和默认前缀。"""
        decision = self.decision
        status = decision.coverage_status
        if self.checked != decision.checked_count or self.accumulated.confirmed != decision.confirmed:
            raise ConsistencyError('Action6 保存指标或检查计数与可靠逐段事实不一致')
        if status is CoverageStatus.REACHED:
            if not self.reached or not self.accumulated.reached:
                raise ConsistencyError('Action6 达标决策未由已保存前缀实际达成')
        elif status is CoverageStatus.CONFIRMED_SHORT:
            complete_scan = (source_kind == int(enum_for('segment_sets.source_kind').COMPLETE_SCAN)
                             and publication_kind == int(enum_for('segment_sets.publication_kind').RESULT))
            if self.accumulated.reached or not complete_scan or self.interruption is not None:
                raise ConsistencyError('Action6 确认不足决策缺少完整且连续的全量检查')
        elif status is CoverageStatus.UNCONFIRMED and self.accumulated.reached:
            raise ConsistencyError('Action6 未确认决策的可靠指标已达到目标')
        if decision.checked_count == 0:
            unknown_scope = self.interruption in ('start_unknown', 'order_unknown')
            expected_scope = DefaultScopeState.UNKNOWN if unknown_scope else DefaultScopeState.EMPTY
            if decision.default_scope.state is not expected_scope:
                raise ConsistencyError('Action6 零成员异常范围必须区分顺序未知与已知空范围')
        elif (decision.default_scope.state is not DefaultScopeState.NONEMPTY
                or decision.default_scope.member_count != self.checked
                or decision.default_scope.first_cursor != self.first_cursor
                or decision.default_scope.last_cursor != self.last_cursor):
            raise ConsistencyError('Action6 异常默认范围必须等于可靠检查前缀')
