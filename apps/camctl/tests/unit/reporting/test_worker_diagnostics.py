"""报告诊断消息的纯内存编码和字段分类，不导入报告运行资源。"""

import json

import pytest

from camctl.reporting.messages import (
    ErrorKind, MessageProtocolError, ResultFailureMessage,
    StartupFailedMessage, StartupPhase, decode_message, encode_message,
)


def _failure(**fields):
    return ResultFailureMessage(
        job_id="diagnostic-1", instance_id="a" * 32,
        error_kind=ErrorKind.REPORT, error_code="RuntimeError",
        error_message="生成失败", **fields,
    )

@pytest.mark.parametrize("factory", [
    _failure,
    lambda **fields: StartupFailedMessage(
        phase=StartupPhase.WORK_LOCK, reason="工作锁失败", **fields),
])
def test_failure_wire_keeps_complete_diagnostic_and_legacy_defaults(factory):
    graph = {"type": "builtins.OSError", "message": "完整原因\x00末尾",
             "cause": {"$ref": "#"}, "notes": ["补充事实"]}
    message = factory(diagnostic=graph)

    assert decode_message(encode_message(message)) == message
    assert json.loads(encode_message(message))["diagnostic"] == graph
    legacy = decode_message(encode_message(factory()))
    assert legacy.diagnostic is None
    assert legacy.diagnostic_fallback is None
    assert legacy.diagnostic_size_bytes is None
    assert "diagnostic" not in json.loads(encode_message(factory()))

@pytest.mark.parametrize("factory", [
    _failure,
    lambda **fields: StartupFailedMessage(
        phase=StartupPhase.RUNTIME_CHECK, reason="运行库失败", **fields),
])
def test_fallback_wire_preserves_route_and_diagnostic_byte_count(factory):
    message = factory(diagnostic_fallback="stderr", diagnostic_size_bytes=70001)

    assert decode_message(encode_message(message)) == message

@pytest.mark.parametrize("fields", [
    {"diagnostic": []},
    {"diagnostic_fallback": "file", "diagnostic_size_bytes": 1},
    {"diagnostic_fallback": "stderr"},
    {"diagnostic_size_bytes": 1},
    {"diagnostic_fallback": "stderr", "diagnostic_size_bytes": True},
    {"diagnostic_fallback": "stderr", "diagnostic_size_bytes": 0},
    {"diagnostic_fallback": "stderr", "diagnostic_size_bytes": -1},
    {"diagnostic": {}, "diagnostic_fallback": "stderr", "diagnostic_size_bytes": 1},
])
def test_invalid_diagnostic_route_is_rejected(fields):
    with pytest.raises(MessageProtocolError):
        _failure(**fields)
