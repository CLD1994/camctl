"""真实资源/投影/Schema 与文件写入的逐层 codec 组合，不调用生成编排。"""
from hashlib import sha256

import pytest

from camctl.contracts import public_projection
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import validate_document
from camctl.reporting.encoding import ReportDocument, ReportStream, chunk_bytes, encode_report

from .test_encoding import _EXPECTED, accepted_report
from unit.reporting.test_layered_encoding import entity_tokens


@pytest.mark.parametrize('capacity', [1, 2, 128])
def test_real_projection_layered_file_matches_independent_original_bytes(accepted_report, tmp_path, capacity):
    document, facts = accepted_report
    plan = public_projection.project_public(public_projection.ProjectionInput(
        'plan', 1, facts, {'action': {1: {}}}))
    stream = ReportStream(ReportDocument(document.report_id, document.from_wm, document.to_wm))
    def tokens():
        yield from stream.open()
        yield from stream.open_array('plans')
        yield from entity_tokens(stream, 'plan', plan)
        yield from stream.close_array()
        yield from stream.open_array('plan_file_diagnostics')
        yield from stream.close_array()
        yield from stream.finish()
    path = tmp_path / f'codec-{capacity}.json'
    digest = sha256()
    size = 0
    with path.open('wb') as output:
        for chunk in chunk_bytes(tokens(), buffer_size=capacity):
            assert 0 < len(chunk) <= capacity
            assert output.write(chunk) == len(chunk)
            digest.update(chunk)
            size += len(chunk)
    actual = path.read_bytes()
    assert actual == _EXPECTED == encode_report(document, facts)
    assert size == len(actual) and digest.hexdigest() == sha256(_EXPECTED).hexdigest()
    validate_document('protocol/status-report.schema.json', parse_exact_json(actual.decode('utf-8')))


def test_actual_registry_orders_entity_children_after_ordinary_fields():
    assert public_projection.projection_structure('action').entity_fields == (
        ('outputs', 'output'), ('deliveries', 'delivery'))
    value = {'plan_instance_id': '1', 'actions': [
        {'action_instance_id': '1', 'z': [], 'outputs': [{'output_id': '2'}],
         'deliveries': [{'delivery_id': '1'}]}]}
    # 此对象只核 codec 排版；父业务适用性留给实际 NodeGate/generation。
    stream = ReportStream(ReportDocument('1', 0, 0))
    encoded = b''.join([*stream.open(), *stream.open_array('plans'),
                        *entity_tokens(stream, 'plan', value), *stream.close_array(), *stream.finish()])
    expected = b'{"from_wm":0,"report_id":"1","to_wm":0,"plans":[{"plan_instance_id":"1","actions":[{"action_instance_id":"1","z":[],"outputs":[{"output_id":"2"}],"deliveries":[{"delivery_id":"1"}]}]}]}\n'
    assert encoded == expected
