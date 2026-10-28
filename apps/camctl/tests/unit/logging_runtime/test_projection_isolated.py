"""公共错误日志投影：源码与受约束替身，禁止实际资源访问。"""
import builtins
import importlib.abc
import importlib.resources
import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

SOURCE = Path(__file__).parents[3] / 'src/camctl/logging_runtime/diagnostics.py'
EXCEPTION_SOURCE = Path(__file__).parents[3] / 'src/camctl/operations/error_diagnostics.py'
LOGGER_SOURCE = Path(__file__).parents[3] / 'src/camctl/logging_runtime/logger.py'

@dataclass(frozen=True)
class ErrorValue:
    code: str
    stage: str
    details: dict

@dataclass(frozen=True)
class CallOutcome:
    error: object = None
    settlement: object = None
    diagnostics: object = None

@dataclass(frozen=True)
class LogRecord:
    level: object
    message: str
    source: object = None
    fallback: object = None
    structured: bool = False

class Receipt:
    def __init__(self):
        self.admission_result = None
        self.write_result = None
        self.callbacks = []
    def add_admission_callback(self, callback): self.callbacks.append(callback)
    def complete_admission(self, result):
        self.admission_result = result
        for callback in self.callbacks: callback(result)
    def complete_write(self, result): self.write_result = result

class BlockCamctl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'camctl' or fullname.startswith('camctl.'):
            raise AssertionError('禁止导入真实 camctl: ' + fullname)

def forbidden(*args, **kwargs):
    raise AssertionError('禁止访问真实资源')

@contextmanager
def isolated():
    modules = {}
    def stub(name, **values):
        module = ModuleType(name)
        module.__dict__.update(values)
        modules[name] = module
    class AdmissionKind(Enum): ACCEPTED = 'accepted'; FALLBACK_STDERR = 'fallback'
    class Level(Enum):
        DEBUG = 'debug'
        INFO = 'info'
        WARNING = 'warning'
        ERROR = 'error'
    class LogSource(Enum): COROUTINE = 'coroutine'; SYNC = 'sync'
    class ToolDiagnostics: pass
    class ToolStream: pass
    stub('camctl.logging_runtime.admission', AdmissionDecision=lambda kind, level: SimpleNamespace(kind=kind, level=level), AdmissionKind=AdmissionKind, LogSource=LogSource)
    stub('camctl.logging_runtime.events', current_logging_context=lambda: None)
    stub('camctl.logging_runtime.models', LogLevel=Level, LogWriteResult=object)
    stub('camctl.logging_runtime.receipt', LogReceipt=Receipt)
    stub('camctl.logging_runtime.service', AdmissionResult=lambda decision, *args, **kw: SimpleNamespace(decision=decision, **kw), LogChannel=object, LogRecord=LogRecord,
         admission_result_for=lambda error: None)
    stub('camctl.logging_runtime.formatting', log_metadata=lambda level: {})
    stub('camctl.operations.error_diagnostics')
    stub('camctl.operations.models', AttemptTicket=type('AttemptTicket', (), {}), CallOutcome=CallOutcome, ErrorValue=ErrorValue)
    stub('camctl.operations.process', RawToolOutcome=type('RawToolOutcome', (), {}))
    stub('camctl.operations.result_format', error_document=lambda e: {'code': e.code, 'stage': e.stage, 'details': dict(e.details)})
    stub('camctl.operations.tool_diagnostics', ToolDiagnostics=ToolDiagnostics, ToolStream=ToolStream,
         encode_diagnostics=lambda value: {})
    name = 'isolated_projection'
    module = ModuleType(name)
    with patch.dict(sys.modules, {name: module, **modules}), patch.object(sys, 'meta_path', [BlockCamctl(), *sys.meta_path]), \
         patch.object(importlib.resources, 'files', forbidden), patch.object(socket, 'socket', forbidden), \
         patch.object(sqlite3, 'connect', forbidden), patch.object(subprocess, 'Popen', forbidden), \
         patch.object(threading.Thread, 'start', forbidden):
        # 只允许读取被测源码；源码执行及调用全程禁止文件访问。
        source = SOURCE.read_text(encoding='utf-8')
        exception_source = EXCEPTION_SOURCE.read_text(encoding='utf-8')
        logger_source = LOGGER_SOURCE.read_text(encoding='utf-8')
        with patch.object(builtins, 'open', forbidden), patch.object(Path, 'open', forbidden):
            exec(compile(exception_source, str(EXCEPTION_SOURCE), 'exec'), modules['camctl.operations.error_diagnostics'].__dict__)
            exec(compile(source, str(SOURCE), 'exec'), module.__dict__)
            sys.modules['camctl.logging_runtime.diagnostics'] = module
            logger = ModuleType('isolated_logger')
            sys.modules[logger.__name__] = logger
            exec(compile(logger_source, str(LOGGER_SOURCE), 'exec'), logger.__dict__)
            module.test_logger = logger
            yield module

def document(module, fn):
    with module.error_diagnostics_context(None, lambda message: None):
        state = fn()
        return state, json.loads(module.diagnostic_record_for(state).message)

def resolve(doc, reference):
    node = doc
    for part in reference['$ref'][2:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node

def test_same_error_roles_share_one_resolvable_node_and_distinct_errors_remain():
    with isolated() as m:
        shared = ErrorValue('one', 'dispatch', {'stderr_hex': 'ff'})
        state, doc = document(m, lambda: m.record_call_outcome(CallOutcome(error=shared), run_error=shared))
        assert doc['error']['code'] == 'one'
        assert resolve(doc, doc['outcome']['error']) == doc['error']
        assert resolve(doc, doc['run_error']) == doc['error']
        assert doc['error']['details']['stderr_text'] == r'\xff'
        other = ErrorValue('one', 'dispatch', {'stderr_hex': 'ff'})
        _, doc = document(m, lambda: m.record_call_outcome(CallOutcome(error=shared), run_error=other))
        assert doc['run_error']['code'] == 'one'
        assert '$ref' not in doc['run_error']

def test_attached_calls_keep_failure_once_and_summarize_success_without_mutation():
    with isolated() as m:
        success = {'command': ['adb', 'setting'], 'status': 'succeeded', 'effect': 'confirmed',
                   'error': None, 'result_document': {}, 'stdout_hex': 'aa' * 13000, 'stderr_hex': ''}
        failure = {'command': ['adb', 'setting2'], 'status': 'failed', 'effect': 'unknown',
                   'error': {'code': 'failed'}, 'result_document': {}, 'stdout_hex': 'ff00', 'stderr_hex': 'ff'}
        outcome = CallOutcome(settlement={'evidence': {'data': {'calls': [success, failure]}}})
        _, doc = document(m, lambda: m.record_error({'setting_calls': [failure]}, stage='device_settings', details={'outcome': outcome}))
        calls = doc['details']['outcome']['settlement']['evidence']['data']['calls']
        assert calls[0]['command'] == success['command'] and calls[0]['status'] == 'succeeded'
        assert calls[0]['stdout_byte_count'] == 13000 and 'stdout_hex' not in calls[0]
        assert resolve(doc, calls[1]) == doc['error']['setting_calls'][0]
        assert doc['error']['setting_calls'][0]['stdout_hex'] == 'ff00'
        assert doc['error']['setting_calls'][0]['stderr_text'] == r'\xff'
        assert success['stdout_hex'] == 'aa' * 13000
        assert len(json.dumps(doc)) < 2000

def test_unknown_success_output_is_not_invented():
    with isolated() as m:
        call = {'command': ['adb'], 'status': 'succeeded', 'effect': 'confirmed', 'error': None,
                'result_document': {}, 'stdout_hex': None, 'stderr_hex': None}
        _, doc = document(m, lambda: m.record_error({'setting_calls': [call]}, stage='device_settings'))
        assert doc['error']['setting_calls'][0]['stdout_byte_count'] is None

@pytest.mark.parametrize('status', ['failed', 'unknown'])
def test_non_success_setting_calls_retain_distinct_full_outputs(status):
    with isolated() as m:
        calls = [dict(command=['adb', str(index)], status=status, effect='unknown', error={'code': 'E'},
                      result_document={'index': index}, stdout_hex='0080', stderr_hex='ff') for index in range(2)]
        _, doc = document(m, lambda: m.record_error({'setting_calls': calls}, stage='device_settings'))
        assert [call['command'] for call in doc['error']['setting_calls']] == [['adb', '0'], ['adb', '1']]
        for call in doc['error']['setting_calls']:
            assert call['stdout_hex'] == '0080'
            assert call['stderr_hex'] == 'ff'
            assert call['stderr_text'] == r'\xff'
            assert '$ref' not in call

def test_json_pointer_escapes_property_names():
    with isolated() as m:
        shared = ErrorValue('E', 'call', {})
        _, doc = document(m, lambda: m.record_error({'a/~': shared, 'again': shared}, stage='test'))
        assert doc['error']['again'] == {'$ref': '#/error/a~1~0'}
        assert resolve(doc, doc['error']['again']) == doc['error']['a/~']


def test_exception_graph_references_resolve_from_document_root():
    with isolated() as m:
        cause = ValueError('root')
        wrapper = RuntimeError('wrapper')
        wrapper.__cause__ = cause
        wrapper.__context__ = cause
        _, doc = document(m, lambda: m.record_error(wrapper, stage='test'))
        assert doc['error']['cause']['message'] == 'root'
        assert resolve(doc, doc['error']['context']) == doc['error']['cause']

def test_prepared_diagnostic_reference_names_original_record_without_claiming_delivery():
    with isolated() as m:
        error = ValueError('original')
        empty = m.DiagnosticState()
        assert m.diagnostic_reference_for(empty) is None
        assert m.diagnostic_reference_for(error) is None
        unprepared = m.record_error(error, stage='outside')
        assert m.diagnostic_reference_for(unprepared) is None
        with m.error_diagnostics_context(None, lambda message: None):
            state = m.record_error(error, stage='original')
            original_record = m.diagnostic_record_for(state)
            ref = m.diagnostic_reference_for(error)
            assert ref == {'diagnostic_ref': json.loads(original_record.message)['diagnostic_id']}
            assert m.diagnostic_reference_for(state) == ref
            assert m.record_error(error, stage='repeat') is state
            assert m.diagnostic_record_for(state) is original_record

def test_new_wrapper_retains_facts_and_references_prepared_cause():
    with isolated() as m:
        cause = ValueError('first failure')
        wrapper = RuntimeError('new phase')
        wrapper.__cause__ = cause
        wrapper.__context__ = cause
        with m.error_diagnostics_context(None, lambda message: None):
            state = m.record_error(cause, stage='first')
            ref = m.diagnostic_reference_for(state)
            later = m.record_error(wrapper, stage='second')
            doc = json.loads(m.diagnostic_record_for(later).message)
        assert doc['error']['message'] == 'new phase'
        assert doc['error']['cause'] == ref
        assert doc['error']['context'] == ref
        assert doc['diagnostic_id'] != ref['diagnostic_ref']

def test_unprepared_cause_remains_complete():
    with isolated() as m:
        cause = ValueError('not yet logged')
        wrapper = RuntimeError('new phase')
        wrapper.__cause__ = cause
        _, doc = document(m, lambda: m.record_error(wrapper, stage='test'))
        assert doc['error']['cause']['message'] == 'not yet logged'
        assert 'diagnostic_ref' not in doc['error']['cause']


def test_generic_log_record_and_wrapper_share_stable_original_reference():
    with isolated() as m:
        cause = ValueError('original')
        wrapper = RuntimeError('later phase')
        wrapper.__cause__ = cause
        context = SimpleNamespace(fields={}, fallback=lambda message: None)
        with m.error_diagnostics_context(None, context.fallback):
            first, _ = m.test_logger._prepare('test', 40, 'first', (), context=context, extra=None,
                                               exc_info=cause, stack_info=False, stacklevel=1, source=None)
            state = m.prepare_log_diagnostic(first, error=cause)
            original = m.diagnostic_record_for(state)
            ref = m.diagnostic_reference_for(state)
            second, _ = m.test_logger._prepare('test', 40, 'second', (), context=context, extra=None,
                                                exc_info=wrapper, stack_info=False, stacklevel=1, source=None)
            doc = json.loads(second.message)
            assert json.loads(original.message)['diagnostic_id'] == ref['diagnostic_ref']
            assert doc['exception']['message'] == 'later phase'
            assert doc['exception']['cause'] == ref

def test_format_failure_keeps_exception_graph_and_stable_identity():
    class BrokenMessage(Exception):
        def __str__(self):
            raise ValueError('cannot format')
    with isolated() as m:
        error = BrokenMessage('raw argument')
        error.__cause__ = ValueError('cause')
        state, doc = document(m, lambda: m.record_error(error, stage='test'))
        assert doc['diagnostic']['error']['arguments'] == ['raw argument']
        assert doc['diagnostic']['error']['cause']['arguments'] == ['cause']
        assert doc['diagnostic_id'] == m.diagnostic_reference_for(state)['diagnostic_ref']
        assert state._fallback_only

def test_format_failure_references_resolve_in_fallback_envelope():
    class BrokenMessage(Exception):
        def __str__(self): raise ValueError('broken')
    with isolated() as m:
        original = BrokenMessage('original')
        _, doc = document(m, lambda: m.record_call_outcome(CallOutcome(), run_error=original))
        assert resolve(doc, doc['diagnostic']['run_error']) == doc['diagnostic']['error']

def test_typed_tool_media_stdout_stays_excluded():
    with isolated() as m:
        tool = m.ToolDiagnostics()
        m.encode_diagnostics = lambda value: {'calls': [{'stdout': {'state': 'complete', 'hex': '010203'},
                                                          'stderr': {'state': 'complete', 'hex': 'ff'}}]}
        result = m.diagnostic_snapshot(tool)
        assert result['calls'][0]['stdout'] == {'state': 'complete', 'byte_count': 3}
        assert result['calls'][0]['stderr'] == {'state': 'complete', 'hex': 'ff', 'text': r'\xff'}

def test_logger_preserves_explicit_traceback_when_exception_is_a_reference():
    with isolated() as m:
        error = ValueError('same')
        context = SimpleNamespace(fields={}, fallback=lambda message: None)
        record, _ = m.test_logger._prepare('test', 40, 'message', (), context=context,
                                           extra={'other_error': error}, exc_info=error,
                                           stack_info=False, stacklevel=1, source=None)
        doc = json.loads(record.message)
        assert resolve(doc, doc['exception']) == doc['other_error']
        assert doc['exception_traceback'] == []


def test_fallback_reuses_frozen_record_and_shared_receipt():
    with isolated() as m:
        messages = []
        error = ValueError('failure')
        with m.error_diagnostics_context(None, messages.append):
            state = m.record_error(error, stage='test')
            record = m.diagnostic_record_for(state)
            ref = m.diagnostic_reference_for(state)
            receipt = m.submit_diagnostic(state)
            assert m.submit_diagnostic(state) is receipt
            assert m.diagnostic_record_for(state) is record
            assert m.diagnostic_reference_for(state) == ref
        assert len(messages) == 1
        assert json.loads(messages[0])['diagnostic_id'] == ref['diagnostic_ref']
        assert state.status is m.DiagnosticStatus.FALLBACK
        assert receipt.write_result.fallback_written is True

def test_shared_exception_inside_group_and_separate_role_uses_record_root():
    with isolated() as m:
        cause = ValueError('same')
        wrapper = RuntimeError('wrapper')
        wrapper.__cause__ = cause
        group = ExceptionGroup('group', [wrapper, cause])
        snapshot = m.diagnostic_snapshot({'group': group, 'also': cause})
        group_children = snapshot['group']['exceptions']
        # 子节点的编码顺序独立于列表顺序；两个角色都定位到同一实际节点。
        first = group_children[0]['cause']
        second = group_children[1]
        if '$ref' in first:
            assert resolve(snapshot, first) == second
            assert resolve(snapshot, snapshot['also']) == second
        else:
            assert resolve(snapshot, second) == first
            assert resolve(snapshot, snapshot['also']) == first

def test_failure_stderr_roles_reference_same_actual_bytes_and_text():
    with isolated() as m:
        call = {'command': ['adb'], 'status': 'failed', 'effect': 'unknown',
                'error': {'code': 'failed', 'details': {'stderr_hex': 'ff00'}},
                'result_document': {}, 'stdout_hex': '01', 'stderr_hex': 'ff00'}
        _, doc = document(m, lambda: m.record_error({'setting_calls': [call]}, stage='device_settings'))
        output = doc['error']['setting_calls'][0]
        assert resolve(doc, output['stderr_hex']) == output['error']['details']['stderr_hex'] == 'ff00'
        assert resolve(doc, output['stderr_text']) == output['error']['details']['stderr_text'] == r'\xff\x00'.replace(r'\x00', '\x00')

def test_snapshot_mapping_references_prepared_exception_but_keeps_unprepared_and_wrapper():
    with isolated() as m:
        existing = ValueError('existing')
        new = ValueError('new')
        wrapper = RuntimeError('wrapper')
        wrapper.__cause__ = existing
        with m.error_diagnostics_context(None, lambda message: None):
            state = m.record_error(existing, stage='original')
            ref = m.diagnostic_reference_for(state)
            snapshot = m.diagnostic_snapshot({'existing': existing, 'new': new, 'wrapper': wrapper})
        assert snapshot['existing'] == ref
        assert snapshot['new']['message'] == 'new'
        assert snapshot['wrapper']['message'] == 'wrapper'
        assert snapshot['wrapper']['cause'] == ref

def test_generic_unstructured_record_keeps_original_message_and_source():
    with isolated() as m:
        error = ValueError('problem')
        original = LogRecord(m.LogLevel.ERROR, 'literal non-json', source='origin', fallback=lambda _: None)
        with m.error_diagnostics_context(None, lambda message: None):
            state = m.prepare_log_diagnostic(original, error=error)
            frozen = m.diagnostic_record_for(state)
        content = json.loads(frozen.message)
        assert content['message'] == 'literal non-json'
        assert frozen.source == 'origin'
        assert content['diagnostic_id'] == m.diagnostic_reference_for(state)['diagnostic_ref']

def test_attached_calls_size_comparison_preserves_source():
    with isolated() as m:
        calls = [dict(command=['adb', str(index)], status='succeeded', effect='confirmed', error=None,
                      result_document={}, stdout_hex='ab' * 13000, stderr_hex='') for index in range(16)]
        calls.append(dict(command=['adb', 'failed'], status='failed', effect='unknown',
                          error={'code': 'E'}, result_document={}, stdout_hex='00ff', stderr_hex='ff'))
        payload = {'stage': 'device_settings', 'error': {'setting_calls': calls[-1:]},
                   'details': {'outcome': CallOutcome(settlement={'evidence': {'data': {'calls': calls}}})}}
        old = json.dumps(payload, default=lambda obj: obj.__dict__, separators=(',', ':')).encode()
        _, doc = document(m, lambda: m.record_error(payload['error'], stage=payload['stage'], details=payload['details']))
        new = json.dumps(doc, separators=(',', ':')).encode()
        assert len(new) < len(old) // 20
        assert all(call['stdout_hex'] == 'ab' * 13000 for call in calls[:-1])
        print(f'体积对照: 原始展开 {len(old)} 字节，投影 {len(new)} 字节；16 次成功、1 次失败')

def test_success_setting_nested_encoded_diagnostics_summarizes_stdout():
    with isolated() as m:
        call = {'command': ['adb'], 'status': 'succeeded', 'effect': 'confirmed', 'error': None,
                'result_document': {'diagnostics': {'version': 1, 'call_limit': 1,
                    'calls': [{'step_index': 1, 'step_name': 'setting',
                               'stdout': {'state': 'complete', 'hex': 'aabb'},
                               'stderr': {'state': 'complete', 'hex': 'ff'}}]}},
                'stdout_hex': 'aabb', 'stderr_hex': 'ff'}
        _, doc = document(m, lambda: m.record_error({'setting_calls': [call]}, stage='device_settings'))
        stream = doc['error']['setting_calls'][0]['result_document']['diagnostics']['calls'][0]['stdout']
        assert stream == {'state': 'complete', 'byte_count': 2}
        assert call['result_document']['diagnostics']['calls'][0]['stdout']['hex'] == 'aabb'

def test_success_status_with_actual_error_keeps_stdout():
    with isolated() as m:
        call = {'command': ['adb'], 'status': 'succeeded', 'effect': 'unknown', 'error': {'code': 'E'},
                'result_document': {}, 'stdout_hex': 'aabb', 'stderr_hex': 'ff'}
        _, doc = document(m, lambda: m.record_error({'setting_calls': [call]}, stage='device_settings'))
        assert doc['error']['setting_calls'][0]['stdout_hex'] == 'aabb'

def test_failed_setting_nested_tool_bytes_remain_readable_and_lossless():
    with isolated() as m:
        call = {'command': ['adb'], 'status': 'failed', 'effect': 'unknown', 'error': {'code': 'E'},
                'result_document': {'diagnostics': {'version': 1, 'call_limit': 1,
                    'calls': [{'step_index': 1, 'step_name': 'setting',
                               'stdout': {'state': 'unconfirmed', 'hex': 'aabb'},
                               'stderr': {'state': 'unconfirmed', 'hex': 'ff'}}]}},
                'stdout_hex': None, 'stderr_hex': None}
        _, doc = document(m, lambda: m.record_error({'setting_calls': [call]}, stage='device_settings'))
        tool = doc['error']['setting_calls'][0]['result_document']['diagnostics']['calls'][0]
        assert tool['stdout']['hex'] == 'aabb'
        assert tool['stderr']['hex'] == 'ff'
        assert tool['stderr']['text'] == r'\xff'

def test_admission_keeps_same_record_and_receipt_for_redelivery():
    with isolated() as m:
        records = []
        class Channel:
            def submit(self, record, *, receipt):
                records.append(record)
                receipt.complete_admission(SimpleNamespace(decision=SimpleNamespace(kind=m.AdmissionKind.ACCEPTED)))
                return receipt
        with m.error_diagnostics_context(Channel(), lambda message: None):
            original = ValueError('original')
            state = m.record_error(original, stage='first')
            record = m.diagnostic_record_for(state)
            receipt = m.submit_diagnostic(state)
            assert m.record_error(original, stage='again') is state
            assert m.submit_diagnostic(state) is receipt
            assert m.diagnostic_record_for(state) is record
            assert m.diagnostic_reference_for(state)['diagnostic_ref'] == json.loads(record.message)['diagnostic_id']
        assert records == [record]
        assert state.status is m.DiagnosticStatus.ADMITTED
