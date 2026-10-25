"""增长请求从实际公开节点和唯一登记派生，不访问包资源。"""
from unittest.mock import create_autospec

import pytest


def test_no_public_coverage_has_no_growth_request(monkeypatch):
    from camctl.contracts import report_reads
    assert report_reads.report_expansion_request('video_coverage', None, source_action_id=9) is None


def test_registered_error_schema_selects_source_even_when_source_action_has_no_error(monkeypatch):
    from camctl.contracts import report_reads
    registry = {'read_expansions': {'segment_error': {
        'trigger': {'kind': 'registered_error', 'details_schema': '#/$defs/segment_error_header'}}}}
    monkeypatch.setattr(report_reads, '_dependencies', create_autospec(
        report_reads._dependencies, return_value=registry))
    monkeypatch.setattr(report_reads, 'registered_error', create_autospec(
        report_reads.registered_error, return_value={
            'details_schema': {'allOf': [{'$ref': '#/$defs/segment_error_header'}]}}))
    from camctl.contracts.segment_errors import SegmentErrorHeader
    from camctl.contracts.segment_values import DefaultScopeState
    header = SegmentErrorHeader(21, 0, DefaultScopeState.UNKNOWN, None, 0)
    monkeypatch.setattr(report_reads, 'validate_segment_error_header', create_autospec(
        report_reads.validate_segment_error_header, return_value=header))
    request = report_reads.report_expansion_request('error', {'code': 'registered_source_failure', 'details': {}})
    assert request.phase == 'segment_error'
    assert request.source_action_id == 21
    assert request.header == header
