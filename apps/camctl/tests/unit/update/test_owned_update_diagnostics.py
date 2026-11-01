"""共同编码器失败时保留实际异常和编码故障。"""
import importlib.util
from pathlib import Path

source = Path(__file__).absolute().parents[5] / 'apps/camctl/src/camctl/update_engine/diagnostic_fallback.py'
spec = importlib.util.spec_from_file_location('owned_update_diagnostics_under_test', source)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


def test_encoder_failure_keeps_error_graph_and_actual_system_facts():
    try:
        try:
            raise ValueError('inner')
        except ValueError as cause:
            raise OSError(13, 'denied', '/config') from cause
    except OSError as error:
        def failed_encoder(item):
            raise RuntimeError('encoder failed')
        result = diagnostics.freeze(error, 'preflight', failed_encoder)
    assert result['stage'] == 'preflight'
    assert result['errno'] == 13 and result['filename'] == '/config'
    assert result['cause']['message'] == 'inner'
    assert result['context'] == {'$ref': '#/cause'}
    assert result['traceback']
    assert result['encoder_error']['message'] == 'encoder failed'
