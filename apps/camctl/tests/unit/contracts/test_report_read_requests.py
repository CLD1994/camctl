"""增长请求从实际公开节点和唯一登记派生，不访问包资源。"""
from unittest.mock import create_autospec

import pytest


def test_no_public_coverage_has_no_growth_request(monkeypatch):
    from camctl.contracts import report_reads
    assert report_reads.report_expansion_request('video_coverage', None, source_action_id=9) is None


@pytest.mark.parametrize('reference,document,matched', [
    ('#/$defs/segment_error_header', '../../../protocol/errors/workflow-codes.json', True),
    ('workflow-codes.json#/$defs/segment_error_header', '../../../protocol/errors/workflow-codes.json', True),
    ('./workflow-codes.json#/$defs/segment_error_header', '../../../protocol/errors/workflow-codes.json', True),
    ('https://schemas.example.test/workflow-codes.json#/$defs/segment_error_header',
     'https://schemas.example.test/workflow-codes.json', True),
    ('workflow-codes.json#/$defs/segment_error_header',
     'https://schemas.example.test/workflow-codes.json', True),
    ('other.json#/$defs/segment_error_header', '../../../protocol/errors/workflow-codes.json', False),
    ('other/workflow-codes.json#/$defs/segment_error_header', '../../../protocol/errors/workflow-codes.json', False),
    ({'$id': 'other.json', '$ref': '#/$defs/segment_error_header'}, '../../../protocol/errors/workflow-codes.json', False),
    ('https://other.example.test/workflow-codes.json#/$defs/segment_error_header',
     'https://schemas.example.test/workflow-codes.json', False),
])
def test_registered_error_schema_binds_source_by_document_and_fragment(monkeypatch, reference, document, matched):
    from camctl.contracts import report_reads
    registry = {'error_registry': document, 'read_expansions': {'segment_error': {
        'trigger': {'kind': 'registered_error', 'details_schema': '#/$defs/segment_error_header'}}}}
    monkeypatch.setattr(report_reads, '_dependencies', create_autospec(
        report_reads._dependencies, return_value=registry))
    monkeypatch.setattr(report_reads, 'registered_error', create_autospec(
        report_reads.registered_error, return_value={
            'details_schema': {'allOf': [{'$ref': reference} if isinstance(reference, str) else reference]}}))
    from camctl.contracts.segment_errors import SegmentErrorHeader
    from camctl.contracts.segment_values import DefaultScopeState
    header = SegmentErrorHeader(21, 0, DefaultScopeState.UNKNOWN, None, 0)
    monkeypatch.setattr(report_reads, 'validate_segment_error_header', create_autospec(
        report_reads.validate_segment_error_header, return_value=header))
    request = report_reads.report_expansion_request('error', {'code': 'registered_source_failure', 'details': {}})
    if not matched:
        assert request is None
        report_reads.validate_segment_error_header.assert_not_called()
        return
    assert request.phase == 'segment_error'
    assert request.source_action_id == 21
    assert request.header == header
