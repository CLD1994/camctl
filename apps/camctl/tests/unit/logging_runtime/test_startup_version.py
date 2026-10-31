"""版本查询和启动事实通过内存日志目标隔离外部依赖。"""

import json
import importlib
import importlib.resources
import sys
from importlib.metadata import PackageNotFoundError
from io import StringIO
from types import ModuleType
from unittest.mock import create_autospec

import pytest

from camctl import version
from camctl.logging_runtime.events import event_logging, log_context
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel
from camctl.logging_runtime import startup
from camctl.logging_runtime.startup import record_startup


@pytest.fixture
def main(monkeypatch):
    # --version 不使用 Schema；替身仅提供 CLI 导入所需的公共接口。
    schema = ModuleType("camctl.contracts.schemas")
    schema.SchemaRuleError = type("SchemaRuleError", (ValueError,), {})
    schema.SchemaValidationError = type("SchemaValidationError", (ValueError,), {})
    def validate_document(schema_name, document):
        raise AssertionError("版本查询不得读取 Schema")
    schema.validate_document = validate_document
    def reject_resources(*args, **kwargs):
        raise AssertionError("版本查询不得读取包资源")
    monkeypatch.setattr(importlib.resources, "files", reject_resources)
    monkeypatch.setitem(sys.modules, "camctl.contracts.schemas", schema)
    monkeypatch.delitem(sys.modules, "camctl.cli", raising=False)
    cli = importlib.import_module("camctl.cli")
    yield cli.main
    sys.modules.pop("camctl.cli", None)


def test_version_option_uses_installed_metadata_only(monkeypatch, main):
    calls = []
    monkeypatch.setattr(version.metadata, "version", lambda name: calls.append(name) or "7.8.9")
    out, err = StringIO(), StringIO()
    assert main(["--version"], stdout=out, stderr=err) == 0
    assert out.getvalue() == "7.8.9\n" and err.getvalue() == ""
    assert calls == ["camctl"]


def test_missing_metadata_version_option_reports_error(monkeypatch, main):
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(version.metadata, "version", missing)
    out, err = StringIO(), StringIO()
    assert main(["--version"], stdout=out, stderr=err) == 1
    assert out.getvalue() == ""
    assert "PackageNotFoundError" in err.getvalue()


def test_startup_submits_installed_version_and_context(monkeypatch):
    monkeypatch.setattr(version.metadata, "version", lambda name: "7.8.9")
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel), log_context(command_name="run", session_key="s1"):
        record_startup()
    channel.submit.assert_called_once()
    record = channel.submit.call_args.args[0]
    data = json.loads(record.rendered_message())
    assert record.level is LogLevel.INFO and record.startup
    assert (data["event"], data["command_name"], data["session_key"], data["version"]) == ("started", "run", "s1", "7.8.9")


def test_metadata_failure_reports_error_and_preserves_startup(monkeypatch):
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(version.metadata, "version", missing)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel), log_context(command_name="init"):
        record_startup()
    diagnostic = channel.slog.call_args.args[0]
    assert diagnostic.level is LogLevel.ERROR
    assert "PackageNotFoundError" in json.loads(diagnostic.message)["exception"]["type"]
    data = json.loads(channel.submit.call_args.args[0].message)
    assert data["version"] is None and data["command_name"] == "init"


def test_without_channel_uses_context_fallback(monkeypatch):
    monkeypatch.setattr(version.metadata, "version", lambda name: "7.8.9")
    lines = []
    with event_logging(None, fallback=lines.append), log_context(command_name="describe"):
        record_startup()
    assert len(lines) == 1
    assert json.loads(lines[0])["version"] == "7.8.9"


def test_metadata_failure_without_channel_keeps_both_facts(monkeypatch):
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(version.metadata, "version", missing)
    lines = []
    with event_logging(None, fallback=lines.append), log_context(command_name="submit"):
        record_startup()
    error, started = map(json.loads, lines)
    assert "PackageNotFoundError" in error["exception"]["type"]
    assert started["version"] is None and started["command_name"] == "submit"


def test_failed_submission_uses_fallback_without_stopping_caller(monkeypatch):
    monkeypatch.setattr(version.metadata, "version", lambda name: "7.8.9")
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    channel.submit.side_effect = RuntimeError("unavailable")
    lines = []
    with event_logging(channel, fallback=lines.append), log_context(command_name="run"):
        record_startup()
    assert len(lines) == 2
    error, started = map(json.loads, lines)
    assert error["level"] == "ERROR"
    assert error["exception"]["type"] == "builtins.RuntimeError"
    assert error["exception"]["message"] == "unavailable"
    assert error["exception"]["traceback"]
    assert started["version"] == "7.8.9"


def test_startup_encoding_failure_reports_original_exception(monkeypatch):
    monkeypatch.setattr(version.metadata, "version", lambda name: "7.8.9")
    def failed_encoding(level, payload):
        raise ValueError("encoding unavailable")
    monkeypatch.setattr(startup, "encode_record", failed_encoding)
    lines = []
    with event_logging(None, fallback=lines.append), log_context(command_name="run"):
        record_startup()
    assert len(lines) == 1
    error = json.loads(lines[0])
    assert error["level"] == "ERROR"
    assert error["exception"]["type"] == "builtins.ValueError"
    assert error["exception"]["message"] == "encoding unavailable"
    assert error["exception"]["traceback"]


def test_metadata_diagnostic_submission_failure_keeps_exception_context(monkeypatch):
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(version.metadata, "version", missing)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    channel.slog.side_effect = OSError("diagnostic unavailable")
    lines = []
    with event_logging(channel, fallback=lines.append), log_context(command_name="run"):
        record_startup()
    assert len(lines) == 1
    error = json.loads(lines[0])
    assert error["level"] == "ERROR"
    assert error["exception"]["type"] == "builtins.OSError"
    assert error["exception"]["message"] == "diagnostic unavailable"
    assert "PackageNotFoundError" in error["exception"]["context"]["type"]
    assert json.loads(channel.submit.call_args.args[0].message)["version"] is None
