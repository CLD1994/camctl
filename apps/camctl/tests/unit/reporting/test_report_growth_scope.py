"""报告公开分支才申请增长恢复，错误头在补齐前保留证据。"""
from contextlib import ExitStack
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.report_reads import ReportExpansionRequest
from camctl.contracts.segment_errors import SegmentErrorHeader, public_gap
from camctl.contracts.segment_values import (
    DefaultScopeState, SegmentGap, SegmentGapKind, SegmentGapNeighborState, SegmentKey,
)
from camctl.contracts.values import ConsistencyError
from camctl.history.report_action import ReportHistoryHandle
from camctl.history.snapshots import SnapshotRow
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting import bounded_generation as generation

H = HistoryBoundary(5, 50)


def handles():
    source = create_autospec(ReportHistoryHandle, instance=True)
    source.action_id, source.boundary = 21, H
    source.primary.return_value = {'type': 2, 'execution_started': True, 'status': 3,
                                   'error_code': None}
    growth = create_autospec(ReportHistoryHandle, instance=True)
    growth.__enter__.return_value = growth
    source.open_expansion.return_value = growth
    return source, growth


def error_request(monkeypatch, header):
    from camctl.contracts import report_reads
    request = ReportExpansionRequest('segment_error', header.source_action_id, header)
    monkeypatch.setattr(report_reads, 'registered_error', create_autospec(
        report_reads.registered_error, return_value={'details_schema': {'$ref': '#/$defs/segment_error_header'}}))
    monkeypatch.setattr(report_reads, '_dependencies', create_autospec(
        report_reads._dependencies, return_value={'read_expansions': {'segment_error': {
            'trigger': {'kind': 'registered_error', 'details_schema': '#/$defs/segment_error_header'}}}}))
    monkeypatch.setattr(report_reads, 'validate_segment_error_header', create_autospec(
        report_reads.validate_segment_error_header, return_value=header))
    return request


def test_obtain_failure_requests_its_source_growth_even_if_source_error_is_null(monkeypatch):
    source, growth = handles()
    header = SegmentErrorHeader(21, 0, DefaultScopeState.UNKNOWN, None, 0)
    request = error_request(monkeypatch, header)
    repo = create_autospec(HistoryRepository, instance=True)
    repo.open_report_action.return_value = source
    source.__enter__.return_value = source
    with ExitStack() as stack:
        expansion = generation._SegmentExpansion(repo, H, object(), {
            'code': 'default_segment_scope_unconfirmed', 'details': {}}, stack)
        assert expansion.handle is growth
    source.open_expansion.assert_called_once_with(request)
    repo.open_report_action.assert_called_once_with(21, H)
    growth.__exit__.assert_called_once()


def test_first_gap_must_match_original_finite_header_before_output_ids_are_added(monkeypatch):
    source, growth = handles()
    def gap(index):
        return SegmentGap(SegmentKey(1, index), SegmentKey(1, index),
            SegmentGapKind.MISSING_POSITION, None, None,
            SegmentGapNeighborState.ABSENT, SegmentGapNeighborState.UNKNOWN)
    header = SegmentErrorHeader(21, 1, DefaultScopeState.UNKNOWN, None, 0, gap(2))
    error_request(monkeypatch, header)
    original = public_gap(header.first_gap)
    error = {'code': 'capture_segment_gap', 'details': {'first_gap': original}}
    monkeypatch.setattr(generation, '_rows', create_autospec(generation._rows,
        side_effect=lambda *args: iter((SnapshotRow('segment_gaps', 1, {
            'gap_no': 1, 'start_directory': 1, 'start_index': 1,
            'end_directory': 1, 'end_index': 1, 'kind': 1,
            'left_member_id': None, 'right_member_id': None, 'empty_member_id': None,
            'neighbors_json': {'left': 2, 'right': 3}}),))))
    growth.row.return_value = source.row.return_value = None
    with ExitStack() as stack, pytest.raises(ConsistencyError, match='首缺口'):
        generation._SegmentExpansion(create_autospec(HistoryRepository, instance=True),
            H, object(), error, stack, source_handle=source)
    assert error['details']['first_gap'] == original


@pytest.mark.parametrize('started,status,allowed', [
    (False, 1, True), (True, 2, True), (True, 3, False), (True, 4, False), (True, 6, False),
])
def test_no_formal_set_preserves_execution_and_terminal_state_boundary(monkeypatch, started, status, allowed):
    source, growth = handles()
    source.primary.return_value.update(execution_started=started, status=status)
    growth.primary.return_value = source.primary.return_value
    source.active_result_ref.return_value = growth.active_result_ref.return_value = None
    error_request(monkeypatch, SegmentErrorHeader(21, 0, DefaultScopeState.UNKNOWN, None, 0))
    with ExitStack() as stack:
        def expand():
            return generation._SegmentExpansion(create_autospec(HistoryRepository, instance=True),
                H, object(), {'code': 'default_segment_scope_unconfirmed', 'details': {}},
                stack, source_handle=source)
        if allowed:
            assert expand().ref is None
        else:
            with pytest.raises(ConsistencyError):
                expand()


@pytest.mark.parametrize('retained', [0, 1])
def test_proved_absent_formal_set_never_opens_unread_growth(monkeypatch, retained):
    source, growth = handles()
    source.primary.return_value.update(execution_started=False, status=1)
    source.active_result_ref.return_value = None
    error_request(monkeypatch, SegmentErrorHeader(21, None, DefaultScopeState.UNKNOWN, None, retained))
    with ExitStack() as stack:
        def expand():
            return generation._SegmentExpansion(None, H, object(), {
                'code': 'default_segment_scope_unconfirmed', 'details': {}}, stack, source_handle=source)
        if retained:
            with pytest.raises(ConsistencyError):
                expand()
        else:
            expansion = expand()
            assert list(expansion.members(generation._SEGMENT_PATHS[1])) == []
    source.open_expansion.assert_not_called()


def coverage_decision(state):
    from camctl.contracts.segment_values import SegmentCursor, SegmentSetRef
    from camctl.contracts.video_coverage import (
        SegmentDecision, SegmentDecisionScope, SegmentDecisionStatus, CoverageMetric, CoverageStatus,
    )
    ref = SegmentSetRef(21, 2, 10, 1)
    nonempty = state is DefaultScopeState.NONEMPTY
    scope = SegmentDecisionScope(state, ref,
        SegmentCursor(SegmentKey(1, 1), 'file-A') if nonempty else None,
        SegmentCursor(SegmentKey(1, 2), 'file-B') if nonempty else None,
        2 if nonempty else 0 if state is DefaultScopeState.EMPTY else None)
    return SegmentDecision(ref, SegmentDecisionStatus.UNCONFIRMED, CoverageMetric.FRAMES,
        20, 10 if nonempty else None,
        CoverageStatus.UNCONFIRMED if nonempty else CoverageStatus.NOT_PERFORMED,
        1 if nonempty else 0, scope, failure_code='coverage_unconfirmed', reason='metric_unknown')


def open_coverage(monkeypatch, stack, state):
    from camctl.contracts import report_reads, video_coverage
    from camctl.capture.segment_storage import SegmentResultRef
    source, growth = handles()
    source.primary.return_value['status'] = 2  # RUNNING 已有的判断仍需公开。
    decision = coverage_decision(state)
    monkeypatch.setattr(video_coverage, 'decision_from_document', create_autospec(
        video_coverage.decision_from_document, return_value=decision))
    monkeypatch.setattr(report_reads, '_dependencies', create_autospec(
        report_reads._dependencies, return_value={'read_expansions': {'video_coverage': {
            'trigger': {'kind': 'projection', 'projection': 'video_coverage'}}}}))
    source.active_result_ref.return_value = SegmentResultRef(21, 4, 1)
    source.read_result.return_value = {'assessment_json': {}}
    public = {'metric': 'frames', 'target': 20,
        'confirmed': 10 if state is DefaultScopeState.NONEMPTY else None,
        'status': 'unconfirmed' if state is DefaultScopeState.NONEMPTY else 'not_performed',
        'reason': {'code': 'metric_unknown'},
        'default_scope': {'state': state.value, 'member_count': decision.default_scope.member_count}}
    expansion = generation._CoverageExpansion(create_autospec(HistoryRepository, instance=True),
        source, H, object(), public, stack=stack)
    return expansion, source, growth


def test_absent_public_coverage_does_not_request_any_growth():
    source, growth = handles()
    expansion = generation._CoverageExpansion(None, source, H, object(), None)
    assert list(expansion.members(generation._COVERAGE_PATHS[1])) == []
    source.open_expansion.assert_not_called()


@pytest.mark.parametrize('state', [DefaultScopeState.UNKNOWN, DefaultScopeState.EMPTY])
def test_unknown_or_empty_coverage_reads_and_proves_its_empty_scope(monkeypatch, state):
    with ExitStack() as stack:
        expansion, source, growth = open_coverage(monkeypatch, stack, state)
        monkeypatch.setattr(generation, '_rows', create_autospec(generation._rows,
            side_effect=lambda *args: iter(())))
        assert list(expansion.members(generation._COVERAGE_PATHS[1])) == []
        source.open_expansion.assert_called_once_with(ReportExpansionRequest('video_coverage', 21))
        assert expansion.handle is growth
    growth.__exit__.assert_called_once()


@pytest.mark.parametrize('member', [None, {'set_id': 5, 'file_id': 30}, {'set_id': 4, 'file_id': None}])
def test_coverage_rejects_missing_or_wrong_scope_member_before_output_lookup(monkeypatch, member):
    with ExitStack() as stack:
        expansion, source, growth = open_coverage(monkeypatch, stack, DefaultScopeState.NONEMPTY)
        monkeypatch.setattr(generation, '_rows', create_autospec(generation._rows,
            side_effect=lambda *args: iter((SnapshotRow('segment_scope_members', 1,
                {'ordinal': 1, 'member_id': 29, 'output_id': 50}),))))
        growth.row.return_value = member
        with pytest.raises(ConsistencyError):
            list(expansion.members(generation._COVERAGE_PATHS[1]))
        expansion.repo.restore_primary_at_h.assert_not_called()
