"""相机 READ 准备错误保留每步原诊断与全部原因，不执行设备操作。"""

import json

import pytest

from camctl.devices.drivers.hasselblad.driver import CameraReadError
from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
from camctl.operations.error_diagnostics import DiagnosticError, exception_diagnostic


@pytest.mark.parametrize('settlement', list(ReadPreparationSettlement))
def test_camera_read_error_preserves_steps_and_each_original_cause(settlement):
    steps = [{'name': 'download', 'command': ['hb_cli', 'download', '原图.fff'],
              'stdout': {'encoding': 'base64', 'data': 'AP8='},
              'stderr': {'encoding': 'base64', 'data': '/wAK'},
              'local_exit_code': 7, 'remote_exit_code': None}]
    try:
        raise OSError(5, '读取失败', b'original-\xff.fff')
    except OSError as source:
        source_error = source
    close = DiagnosticError('关闭失败', diagnostic_details={'exit_code': 9})
    close.__cause__ = ValueError('子原因')
    grouped = ExceptionGroup('下载收场失败', [source_error, close])
    error = CameraReadError('preparation_and_cleanup_failed', steps,
        causes=(source_error, grouped), settlement=settlement)
    document = exception_diagnostic(error)
    diagnostic = document['diagnostic_details']
    causes = document['diagnostic_facts']['causes']
    assert isinstance(error, ReadPreparationError)
    assert error.settlement is settlement
    assert diagnostic['reason'] == 'preparation_and_cleanup_failed'
    assert diagnostic['settlement'] == settlement.value
    assert diagnostic['steps'] == steps
    assert causes[0]['errno'] == 5
    assert causes[0]['filename'] == {'bytes_hex': b'original-\xff.fff'.hex()}
    assert causes[0]['traceback'][-1]['function'] == 'test_camera_read_error_preserves_steps_and_each_original_cause'
    children = causes[1]['exceptions']
    assert children[0] == {'$ref': '#/diagnostic_facts/causes/0'}
    assert children[1]['diagnostic_details'] == {'exit_code': 9}
    assert children[1]['cause']['message'] == '子原因'
    assert json.loads(json.dumps(error.diagnostic_details, ensure_ascii=False)) == diagnostic
    assert json.loads(json.dumps(document, ensure_ascii=False)) == document
