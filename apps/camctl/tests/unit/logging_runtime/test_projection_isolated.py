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
    fallback: object = None
    structured: bool = False

class Receipt:
    def add_admission_callback(self, callback): pass

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
    class AdmissionKind(Enum): ACCEPTED = 'accepted'
    class Level(Enum): ERROR = 'error'
    class ToolDiagnostics: pass
    class ToolStream: pass
    stub('camctl.logging_runtime.admission', AdmissionDecision=object, AdmissionKind=AdmissionKind)
    stub('camctl.logging_runtime.models', LogLevel=Level, LogWriteResult=object)
    stub('camctl.logging_runtime.receipt', LogReceipt=Receipt)
    stub('camctl.logging_runtime.service', AdmissionResult=object, LogChannel=object, LogRecord=LogRecord,
         admission_result_for=lambda error: None)
    stub('camctl.logging_runtime.formatting', log_metadata=lambda level: {})
    stub('camctl.operations.error_diagnostics', DiagnosticError=type('DiagnosticError', (Exception,), {}),
         exception_diagnostic=lambda e: {'type': type(e).__name__, 'message': str(e)})
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
        with patch.object(builtins, 'open', forbidden), patch.object(Path, 'open', forbidden):
            exec(compile(source, str(SOURCE), 'exec'), module.__dict__)
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
