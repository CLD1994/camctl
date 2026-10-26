"""标准日志路由使用独立 Logger 图，不修改进程全局日志配置。"""

import json
import logging
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind, LogSource
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import AdmissionResult, LogChannel
from camctl.logging_runtime.standard_logging import install_standard_logging, uninstall_standard_logging


def logger_graph():
    root = logging.RootLogger(logging.CRITICAL)
    manager = logging.Manager(root)
    root.manager = manager
    return root, manager


def channel_stub():
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    channel.slog.return_value = AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, LogLevel.ERROR), 1)
    return channel


def test_route_removes_duplicate_handlers_and_restores_original_configuration():
    root, manager = logger_graph()
    parent, child = manager.getLogger("vendor"), manager.getLogger("vendor.child")
    original = [logging.NullHandler() for _ in range(3)]
    root.addHandler(original[0])
    parent.addHandler(original[1])
    child.addHandler(original[2])
    parent.propagate = child.propagate = False
    parent.setLevel(logging.CRITICAL)
    child.setLevel(logging.CRITICAL)
    channel, fallback = channel_stub(), []
    route = install_standard_logging(channel, fallback.append, manager=manager)
    child.error("one %s", "error")
    assert channel.slog.call_count == 1
    record = channel.slog.call_args.args[0]
    assert record.source is LogSource.SYNC
    assert "one error" in record.message
    assert "vendor.child" in record.message
    assert fallback == []
    uninstall_standard_logging(route)
    route.close()
    assert root.handlers == [original[0]]
    assert parent.handlers == [original[1]]
    assert child.handlers == [original[2]]
    assert parent.propagate is child.propagate is False
    assert root.level == parent.level == child.level == logging.CRITICAL


def test_camctl_namespace_is_distinct_from_vendor_prefix():
    root, manager = logger_graph()
    channel = channel_stub()
    route = install_standard_logging(channel, lambda message: None, manager=manager)
    try:
        manager.getLogger("camctl").error("own")
        manager.getLogger("camctl.child").error("own child")
        manager.getLogger("camctl_vendor").error("external")
        assert channel.slog.call_count == 1
        assert "external" in channel.slog.call_args.args[0].message
    finally:
        route.close()


@pytest.mark.parametrize("level", [logging.ERROR, 45, logging.CRITICAL, 60])
def test_preserves_original_level_and_full_exception_graph(level):
    root, manager = logger_graph()
    channel = channel_stub()
    route = install_standard_logging(channel, lambda message: None, manager=manager)
    error = ExceptionGroup("all causes", [ValueError(str(index)) for index in range(20)])
    error.add_note("original note")
    try:
        raise error
    except ExceptionGroup:
        original = manager.getLogger("vendor").makeRecord("vendor", level, "source.py", 42,
                                                        "message %s", ("value",), __import__('sys').exc_info())
    try:
        manager.getLogger("vendor").handle(original)
        prepared = channel.slog.call_args.args[0]
        assert f"{original.levelname}/{level}" in prepared.message
        assert "source.py:42" in prepared.message
        assert "message value" in prepared.message
        diagnostic = json.loads(prepared.message.split("异常诊断: ", 1)[1])
        assert len(diagnostic["exceptions"]) == 20
        assert diagnostic["notes"] == ["original note"]
        assert diagnostic["traceback"]
        assert original.msg == "message %s"
        assert original.args == ("value",)
        assert original.exc_info[1] is error
    finally:
        route.close()


def test_unexpected_admission_error_falls_back_without_recursive_logging():
    root, manager = logger_graph()
    channel = channel_stub()
    channel.slog.side_effect = OSError("queue unavailable")
    attempts = []
    def unavailable(message):
        attempts.append(message)
        manager.getLogger("vendor").error("recursive")
        raise OSError("stderr unavailable")
    route = install_standard_logging(channel, unavailable, manager=manager)
    try:
        manager.getLogger("vendor").error("original")
        manager.getLogger("vendor").error("later")
        assert len(attempts) == 1
        assert "original" in attempts[0]
        payload = json.loads(attempts[0])
        assert payload["level"] == "ERROR" and payload["stage"] == "runtime"
        assert "logged_at_utc" in payload and isinstance(payload["pid"], int)
        assert "queue unavailable" in payload["message"]
    finally:
        route.close()


def test_invalid_message_format_preserves_original_exception_and_stack():
    root, manager = logger_graph()
    channel, attempts = channel_stub(), []
    route = install_standard_logging(channel, attempts.append, manager=manager)
    error = ValueError("original exception")
    error.add_note("original note")
    try:
        try:
            raise error
        except ValueError:
            manager.getLogger("vendor").error("invalid %d", "text", exc_info=True, stack_info=True)
        assert len(attempts) == 1
        message = json.loads(attempts[0])["message"]
        assert "invalid %d" in message and "text" in message
        assert "original exception" in message and "original note" in message
        assert "Stack (most recent call last)" in message
        assert "traceback" in message
        channel.slog.assert_not_called()
    finally:
        route.close()


def test_exception_message_format_failure_does_not_escape_logging():
    class UnprintableError(Exception):
        def __str__(self):
            raise RuntimeError("exception formatting failed")
    root, manager = logger_graph()
    channel, attempts = channel_stub(), []
    route = install_standard_logging(channel, attempts.append, manager=manager)
    error = UnprintableError("original argument")
    error.add_note("original note")
    error.__cause__ = ValueError("original cause")
    try:
        try:
            raise error
        except UnprintableError:
            manager.getLogger("vendor").error("normal message", exc_info=True, stack_info=True)
        assert len(attempts) == 1
        message = json.loads(attempts[0])["message"]
        assert "original argument" in message and "original note" in message
        assert "original cause" in message and "UnprintableError" in message
        assert "traceback" in message and "Stack (most recent call last)" in message
        assert "exception formatting failed" in message
        channel.slog.assert_not_called()
    finally:
        route.close()


def test_route_owns_source_filters_and_restores_them():
    root, manager = logger_graph()
    vendor = manager.getLogger("vendor")
    class Reject(logging.Filter):
        def filter(self, record):
            return False
    root_filter, vendor_filter = Reject(), Reject()
    root.addFilter(root_filter)
    vendor.addFilter(vendor_filter)
    channel = channel_stub()
    route = install_standard_logging(channel, lambda message: None, manager=manager)
    try:
        root.error("root diagnostic")
        vendor.error("vendor diagnostic")
        assert channel.slog.call_count == 2
    finally:
        route.close()
    assert root.filters == [root_filter]
    assert vendor.filters == [vendor_filter]


def test_fallback_reports_callback_false_and_stops_attempts():
    root, manager = logger_graph()
    channel = channel_stub()
    attempts = []
    def declined(message):
        attempts.append(message)
        return False
    route = install_standard_logging(channel, declined, manager=manager)
    try:
        manager.getLogger("vendor").error("original")
        fallback = channel.slog.call_args.args[0].fallback
        assert fallback("first") is False
        assert fallback("later") is False
        assert attempts == ["first"]
    finally:
        route.close()
