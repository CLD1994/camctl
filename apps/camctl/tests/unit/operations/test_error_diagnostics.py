"""异常图保留原消息、完整栈和全部原因，不按格式化默认值裁剪。"""

import json
import linecache

import pytest

from camctl.operations.error_diagnostics import DiagnosticError, exception_diagnostic


@pytest.fixture(autouse=True)
def source_lines(monkeypatch):
    monkeypatch.setattr(linecache, 'getline', lambda *args: '')
    monkeypatch.setattr(linecache, 'checkcache', lambda *args: None)


def test_absent_exception_remains_absent():
    assert exception_diagnostic(None) is None


def _original_error():
    try:
        raise ValueError('first\nline\u0000' + 'x' * 20000)
    except ValueError as error:
        error.add_note('original note')
        return error


def test_original_traceback_message_notes_and_both_chains_are_retained():
    original = _original_error()
    try:
        raise original
    except ValueError:
        try:
            raise RuntimeError('outer') from OSError(5, 'explicit cause', 'source')
        except RuntimeError as error:
            diagnostic = exception_diagnostic(error)
    assert diagnostic['type'] == 'builtins.RuntimeError'
    assert diagnostic['message'] == 'outer'
    assert any(frame['function'] == 'test_original_traceback_message_notes_and_both_chains_are_retained'
               for frame in diagnostic['traceback'])
    assert diagnostic['suppress_context'] is True
    assert diagnostic['cause']['type'] == 'builtins.OSError'
    assert diagnostic['context']['message'] == str(original)
    assert any(frame['function'] == '_original_error' for frame in diagnostic['context']['traceback'])
    assert diagnostic['context']['notes'] == ['original note']
    assert json.loads(json.dumps(diagnostic)) == diagnostic


def test_group_keeps_every_member_and_all_nested_groups():
    leaves = [ValueError(f'leaf-{index}') for index in range(30)]
    nested = RuntimeError('deepest original')
    for depth in range(25):
        nested = ExceptionGroup(f'depth-{depth}', [nested])
    group = BaseExceptionGroup('all failures', [*leaves, nested, KeyboardInterrupt('actual control')])
    diagnostic = exception_diagnostic(group)
    assert len(diagnostic['exceptions']) == 32
    assert [item['message'] for item in diagnostic['exceptions'][:30]] == [f'leaf-{index}' for index in range(30)]
    current = diagnostic['exceptions'][30]
    for _ in range(25):
        assert len(current['exceptions']) == 1
        current = current['exceptions'][0]
    assert current['message'] == 'deepest original'
    assert diagnostic['exceptions'][31]['type'] == 'builtins.KeyboardInterrupt'
    json.dumps(diagnostic)


def test_cycles_and_repeated_members_reference_the_original_path():
    first, second = ValueError('first'), RuntimeError('second')
    first.__cause__ = second
    second.__context__ = first
    group = ExceptionGroup('shared', [first, first])
    diagnostic = exception_diagnostic(group)
    assert diagnostic['exceptions'][1] == {'$ref': '#/exceptions/0'}
    assert diagnostic['exceptions'][0]['cause']['context'] == {'$ref': '#/exceptions/0'}
    json.dumps(diagnostic)


@pytest.mark.parametrize('filename,filename2', [('source', 'target'), (b'\xffsource', b'\x00target')])
def test_os_error_keeps_errno_and_both_filenames(filename, filename2):
    diagnostic = exception_diagnostic(OSError(18, 'cannot rename', filename, None, filename2))
    assert diagnostic['errno'] == 18
    for name, actual in [('filename', filename), ('filename2', filename2)]:
        assert diagnostic[name] == ({'bytes_hex': actual.hex()} if isinstance(actual, bytes) else actual)
    json.dumps(diagnostic)


def test_known_diagnostic_mapping_is_complete_and_arbitrary_attributes_are_not_copied():
    details = {'stderr_hex': bytes(range(256)).hex() * 100, 'offset': 17,
               'nested': {'completed': False, 'values': [None, 'unchanged']}}
    error = DiagnosticError('failure', diagnostic_details=details)
    error.arbitrary = object()
    diagnostic = exception_diagnostic(error)
    assert error.diagnostic_details == details
    assert diagnostic['diagnostic_details'] == details
    assert 'arbitrary' not in diagnostic
    assert json.loads(json.dumps(diagnostic)) == diagnostic


def test_self_traceback_keeps_recursive_frames_without_default_compression(monkeypatch):
    import sys
    monkeypatch.setattr(sys, 'tracebacklimit', 1, raising=False)
    def descend(depth):
        if depth:
            return descend(depth - 1)
        raise LookupError('at original deepest frame')
    try:
        descend(30)
    except LookupError as error:
        diagnostic = exception_diagnostic(error)
    assert sum(frame['function'] == 'descend' for frame in diagnostic['traceback']) == 31
    assert all(isinstance(frame['filename'], str) and isinstance(frame['lineno'], int)
               for frame in diagnostic['traceback'])


def test_diagnosis_never_looks_up_source_files(monkeypatch):
    def unexpected_lookup(*args, **kwargs):
        raise AssertionError('diagnostic must not read source text')
    monkeypatch.setattr(linecache, 'getline', unexpected_lookup)
    monkeypatch.setattr(linecache, 'getlines', unexpected_lookup)
    monkeypatch.setattr(linecache, 'checkcache', unexpected_lookup)
    diagnostic = exception_diagnostic(_original_error())
    assert diagnostic['traceback']
