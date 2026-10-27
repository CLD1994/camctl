"""通用 Logger 在调用时冻结事实，并遵守同步及异步入口契约。"""

import asyncio
from datetime import datetime
import inspect
import json
import linecache
import logging
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime import get_logger, get_sync_logger
from camctl.logging_runtime import diagnostics, events
from camctl.logging_runtime.admission import LogSource
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel
from camctl.operations.error_diagnostics import DiagnosticError
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.tool_diagnostics import (
    ToolCallOutput, ToolDiagnostics, ToolStream, ToolStreamState,
)


def channel_stub():
    return create_autospec(LogChannel, instance=True, spec_set=True)


def document(channel):
    return json.loads(channel.slog.call_args.args[0].rendered_message())


@pytest.fixture
def diagnostic_sink(monkeypatch):
    """替身遵守单条接缝：prepare 冻结，admit 只交接原记录。"""
    records = []
    states = []

    def prepare(record, *, error=None, state=None, channel=None, fallback=None):
        prepared = SimpleNamespace(record=record, error=error, state=state,
                                   channel=channel, fallback=fallback)
        states.append(prepared)
        return prepared

    async def admit(state):
        records.append(state.record)
        return state

    monkeypatch.setattr(diagnostics, "prepare_log_diagnostic", prepare)
    monkeypatch.setattr(diagnostics, "admit_diagnostic", admit)
    return SimpleNamespace(records=records, states=states)


@pytest.mark.parametrize("method,level", [("debug", LogLevel.DEBUG), ("info", LogLevel.INFO),
                                          ("warning", LogLevel.WARNING)])
@pytest.mark.asyncio
async def test_low_levels_share_channel_and_finish_without_async_admission(method, level):
    channel = channel_stub()
    logger = get_logger("camctl.business")
    with events.event_logging(channel):
        pending = getattr(logger, method)("returned %s", "事实")
        channel.slog.assert_not_called()
        assert await pending is channel.slog.return_value
    record = channel.slog.call_args.args[0]
    payload = document(channel)
    assert record.level is level and record.source is LogSource.COROUTINE
    assert payload["message"] == "returned 事实"
    assert payload["logger"] == "camctl.business" and payload["stage"] == "runtime"
    assert payload["level"] == level.name
    assert datetime.fromisoformat(payload["logged_at_utc"]).utcoffset().total_seconds() == 0
    assert isinstance(payload["pid"], int)
    channel.alog.assert_not_called()


@pytest.mark.parametrize("message,args,expected", [
    ("value %(name)s", ({"name": "mapped"},), "value mapped"),
    ("%s", ({},), "{}"), ("%s", (), "%s"), ("100%%", (), "100%%"),
    ("%s:%d", ("中文", 7), "中文:7"),
])
def test_sync_logger_uses_standard_log_record_message_semantics(message, args, expected):
    channel = channel_stub()
    with events.event_logging(channel):
        get_sync_logger("test").info(message, *args)
    assert document(channel)["message"] == expected
    assert channel.slog.call_args.args[0].source is LogSource.SYNC


@pytest.mark.parametrize("level", [LogLevel.DEBUG, LogLevel.INFO, LogLevel.WARNING,
                                  logging.DEBUG, logging.INFO, logging.WARNING])
def test_sync_log_accepts_existing_enum_and_standard_low_levels(level):
    channel = channel_stub()
    with events.event_logging(channel):
        get_sync_logger("test").log(level, "fact")
    assert channel.slog.call_count == 1


@pytest.mark.parametrize("level", [0, 15, 45, 60, True, "INFO", None])
def test_unknown_levels_are_rejected_at_call_time(level):
    for logger in (get_logger("test"), get_sync_logger("test")):
        with pytest.raises(ValueError):
            logger.log(level, "fact")


@pytest.mark.parametrize("level", [LogLevel.ERROR, logging.ERROR, logging.CRITICAL])
def test_sync_logger_rejects_important_levels(level):
    channel = channel_stub()
    with events.event_logging(channel), pytest.raises(ValueError):
        get_sync_logger("test").log(level, "fact")
    channel.slog.assert_not_called()


@pytest.mark.parametrize("field", ["level", "logged_at_utc", "pid", "logger", "message",
                                   "pathname", "lineno", "function", "exc_info", "exception"])
def test_extra_cannot_override_generated_log_fields(field):
    for logger in (get_logger("test"), get_sync_logger("test")):
        with pytest.raises(KeyError):
            logger.info("fact", extra={field: "forged"})


@pytest.mark.asyncio
async def test_call_freezes_message_nested_fields_metadata_and_original_context(monkeypatch):
    from camctl.logging_runtime import formatting
    channel, later = channel_stub(), channel_stub()
    class Message:
        value = "before"
        def __str__(self):
            return self.value
    message = Message()
    extra = {"details": {"values": [1]}, "action_id": 9}
    with events.event_logging(channel), events.log_context(action_id=3, request_key="original"):
        with events.measure_stage("prepare", level=None):
            pending = get_logger("test").info("value %s", message, extra=extra)
    message.value = "after"
    extra["details"]["values"].append(2)
    monkeypatch.setattr(formatting, "utc_now", lambda: "after-await")
    with events.event_logging(later), events.log_context(request_key="later"):
        await pending
    payload = document(channel)
    assert payload["message"] == "value before"
    assert payload["details"] == {"values": [1]}
    assert payload["action_id"] == 9 and payload["request_key"] == "original"
    assert payload["stage"] == "prepare" and payload["logged_at_utc"] != "after-await"
    later.slog.assert_not_called()


@pytest.mark.asyncio
async def test_module_logger_is_reusable_across_task_contexts():
    logger, channel = get_logger("test"), channel_stub()
    ready = asyncio.Event()
    async def work(action_id):
        with events.log_context(action_id=action_id):
            await ready.wait()
            await logger.info("task")
    with events.event_logging(channel):
        tasks = [asyncio.create_task(work(identity)) for identity in (1, 2)]
        ready.set()
        await asyncio.gather(*tasks)
    assert [json.loads(call.args[0].message)["action_id"]
            for call in channel.slog.call_args_list] == [1, 2]
    assert logger.name == "test" and not hasattr(logger, "__dict__")


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.asyncio
async def test_source_and_stacklevel_point_to_business_call_without_reading_source(monkeypatch, synchronous):
    def unexpected_read(*args, **kwargs):
        raise AssertionError("日志栈捕获不得读取源码")
    monkeypatch.setattr(linecache, "checkcache", unexpected_read)
    monkeypatch.setattr(linecache, "getlines", unexpected_read)
    logger = get_sync_logger("test") if synchronous else get_logger("test")
    channel = channel_stub()
    def wrapper():
        return logger.info("fact", stack_info=True, stacklevel=2)
    with events.event_logging(channel):
        expected_line = inspect.currentframe().f_lineno + 1
        result = wrapper()
        if not synchronous:
            await result
    payload = document(channel)
    assert payload["pathname"] == __file__ and payload["lineno"] == expected_line
    assert payload["function"] == inspect.currentframe().f_code.co_name
    assert payload["stack_info"][-1] == {
        "filename": __file__, "lineno": expected_line, "function": payload["function"]}
    assert not any(frame["function"] in ("_prepare", "_caller", "log", "info")
                   and frame["filename"].endswith("logging_runtime/logger.py")
                   for frame in payload["stack_info"])


@pytest.mark.parametrize("method,level", [("error", "ERROR"), ("critical", "CRITICAL"),
                                         ("exception", "ERROR")])
@pytest.mark.asyncio
async def test_important_methods_prepare_before_await_and_preserve_output_level(diagnostic_sink, method, level):
    channel, fallback = channel_stub(), []
    with events.event_logging(channel, fallback=fallback.append), events.log_context(action_id=7):
        expected_line = inspect.currentframe().f_lineno + 1
        pending = getattr(get_logger("test"), method)("important %s", "fact")
    assert len(diagnostic_sink.states) == 1 and diagnostic_sink.records == []
    prepared = diagnostic_sink.states[0]
    assert prepared.channel is channel and prepared.fallback == fallback.append
    assert await pending is prepared
    record = diagnostic_sink.records[0]
    payload = json.loads(record.message)
    assert record.level is LogLevel.ERROR and payload["level"] == level
    assert payload["message"] == "important fact" and payload["action_id"] == 7
    assert payload["lineno"] == expected_line and payload["pathname"] == __file__
    assert "exception" not in payload


@pytest.mark.asyncio
async def test_exception_captures_original_graph_before_await(diagnostic_sink):
    error = ExceptionGroup("original", [ValueError("child")])
    error.add_note("before")
    error.__cause__ = OSError("original cause")
    try:
        raise error
    except ExceptionGroup:
        pending = get_logger("test").exception("fact")
    error.add_note("after")
    error.__cause__ = ValueError("later cause")
    await pending
    payload = json.loads(diagnostic_sink.records[0].message)
    graph = payload["exception"]
    assert graph["notes"] == ["before"] and graph["cause"]["message"] == "original cause"
    assert graph["exceptions"][0]["message"] == "child" and graph["traceback"]
    assert diagnostic_sink.states[0].error is error


@pytest.mark.asyncio
async def test_explicit_exception_tuple_uses_its_own_traceback_without_mutating_error(diagnostic_sink):
    try:
        raise ValueError("traceback source")
    except ValueError as source:
        traceback = source.__traceback__
    original = RuntimeError("original")
    await get_logger("test").error("fact", exc_info=(RuntimeError, original, traceback))
    graph = json.loads(diagnostic_sink.records[0].message)["exception"]
    assert graph["message"] == "original" and graph["traceback"][0]["function"].startswith("test_explicit")
    assert original.__traceback__ is None


@pytest.mark.asyncio
async def test_exception_can_explicitly_suppress_current_error(diagnostic_sink):
    try:
        raise ValueError("ignored")
    except ValueError:
        await get_logger("test").exception("fact", exc_info=False)
    assert "exception" not in json.loads(diagnostic_sink.records[0].message)
    assert diagnostic_sink.states[0].error is None


@pytest.mark.asyncio
async def test_explicit_diagnostic_state_is_handed_to_diagnostic_component(diagnostic_sink):
    state = object()
    await get_logger("test").error("fact", state=state)
    assert diagnostic_sink.states[0].state is state
    for logger in (get_logger("test"), get_sync_logger("test")):
        with pytest.raises(ValueError):
            logger.info("fact", state=state)


@pytest.mark.parametrize("message,args", [("invalid %d", ("原参数",)), ("%(missing)s", ({},))])
def test_message_format_failure_retains_original_template_and_arguments(message, args):
    channel = channel_stub()
    with events.event_logging(channel):
        get_sync_logger("test").warning(message, *args)
    payload = document(channel)
    assert payload["message"] == message
    assert payload["message_arguments"] == list(args)
    assert payload["message_format_error"]["type"].startswith("builtins.")


@pytest.mark.asyncio
async def test_exception_message_failure_retains_full_original_graph(diagnostic_sink):
    class UnprintableError(Exception):
        def __str__(self):
            raise RuntimeError("format failure")
    error = UnprintableError("original argument")
    error.add_note("original note")
    error.__cause__ = ValueError("original cause")
    await get_logger("test").error("fact", exc_info=error)
    payload = json.loads(diagnostic_sink.records[0].message)
    assert payload["exception"]["arguments"] == ["original argument"]
    assert payload["exception"]["notes"] == ["original note"]
    assert payload["exception"]["cause"]["arguments"] == ["original cause"]
    assert "exception_format_error" in payload


@pytest.mark.asyncio
async def test_exception_graph_formatter_failure_uses_shared_safe_original_fields(diagnostic_sink):
    class UnprintableError(Exception):
        def __str__(self):
            raise ValueError("message unavailable")
    error = UnprintableError({1: b"original argument"})
    await get_logger("test").error("fact", exc_info=error)
    payload = json.loads(diagnostic_sink.records[0].message)
    assert payload["exception"]["arguments"] == [{"1": {"bytes_hex": b"original argument".hex()}}]
    assert "exception_format_error" in payload


def test_unknown_extra_object_does_not_change_original_business_execution():
    channel = channel_stub()
    class Unprintable:
        def __repr__(self):
            raise ValueError("repr unavailable")
    with events.event_logging(channel):
        get_sync_logger("test").info("fact", extra={"value": Unprintable()})
        result = 17
    assert result == 17 and "Unprintable" in document(channel)["value"]


@pytest.mark.asyncio
async def test_error_details_use_shared_lossless_and_media_safe_snapshot(diagnostic_sink):
    media = b"secret-media-stdout"
    stderr = b"original stderr\xff"
    raw = RawToolOutcome(LocalExit(exit_code=1), media, "output_failed", None, stderr=stderr)
    error = DiagnosticError("original", diagnostic_details={"stdout": media, "stderr": stderr})
    await get_logger("test").error("fact", exc_info=error, extra={"raw": raw})
    message = diagnostic_sink.records[0].message
    payload = json.loads(message)
    assert "secret-media-stdout" not in message and media.hex() not in message
    assert payload["exception"]["diagnostic_details"]["stdout"] == {"byte_count": len(media)}
    assert payload["exception"]["diagnostic_details"]["stderr"]["hex"] == stderr.hex()
    assert payload["raw"]["output"] == {"byte_count": len(media)}
    assert payload["raw"]["stderr"]["text"] == stderr.decode("utf-8", "backslashreplace")


@pytest.mark.parametrize("entry", ["sync", "async", "error"])
@pytest.mark.asyncio
async def test_message_format_failure_keeps_typed_tool_facts_for_media_filtering(diagnostic_sink, entry):
    channel = channel_stub()
    media, stderr = b"MEDIA_SENTINEL", b"original stderr\xff"
    raw = RawToolOutcome(LocalExit(exit_code=1), media, "output_failed", None, stderr=stderr)
    with events.event_logging(channel):
        if entry == "sync":
            get_sync_logger("test").warning("value=%d", raw)
        elif entry == "async":
            await get_logger("test").warning("value=%d", raw)
        else:
            await get_logger("test").error("value=%d", raw)
    record = diagnostic_sink.records[0] if entry == "error" else channel.slog.call_args.args[0]
    assert "MEDIA_SENTINEL" not in record.message and media.hex() not in record.message
    payload = json.loads(record.message)
    assert payload["message"] == "value=%d"
    argument = payload["message_arguments"][0]
    assert argument["output"] == {"byte_count": len(media)}
    assert argument["stderr"] == {"hex": stderr.hex(), "text": stderr.decode("utf-8", "backslashreplace")}


@pytest.mark.parametrize("entry", ["sync", "async", "error"])
@pytest.mark.asyncio
async def test_non_string_message_and_format_error_arguments_keep_media_boundary(diagnostic_sink, entry):
    class UnprintableRaw(RawToolOutcome):
        def __str__(self):
            raise RuntimeError(self)
    channel = channel_stub()
    media, stderr = b"MEDIA_SENTINEL", b"original stderr\xff"
    raw = UnprintableRaw(LocalExit(exit_code=1), media, "output_failed", None, stderr=stderr)
    with events.event_logging(channel):
        if entry == "sync":
            get_sync_logger("test").warning(raw)
        elif entry == "async":
            await get_logger("test").warning(raw)
        else:
            await get_logger("test").error(raw)
    record = diagnostic_sink.records[0] if entry == "error" else channel.slog.call_args.args[0]
    assert "MEDIA_SENTINEL" not in record.message and media.hex() not in record.message
    payload = json.loads(record.message)
    assert payload["message_value"]["output"] == {"byte_count": len(media)}
    assert payload["message_format_error"]["arguments"][0]["output"] == {"byte_count": len(media)}
    assert payload["message_value"]["stderr"]["hex"] == stderr.hex()


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("entry", ["sync", "async", "error"])
@pytest.mark.asyncio
async def test_tool_diagnostics_and_encoder_fallback_preserve_safe_original_streams(diagnostic_sink, entry, valid):
    channel = channel_stub()
    media, stderr = b"MEDIA_SENTINEL", b"original stderr\xff"
    streams = ToolCallOutput(1, "tool", ToolStream(ToolStreamState.COMPLETE, media),
                            ToolStream(ToolStreamState.COMPLETE, stderr))
    tool = ToolDiagnostics(1 if valid else 0, (streams,))
    with events.event_logging(channel):
        if entry == "sync":
            get_sync_logger("test").warning("fact", extra={"tool": tool})
        elif entry == "async":
            await get_logger("test").warning("fact", extra={"tool": tool})
        else:
            await get_logger("test").error("fact", extra={"tool": tool})
    record = diagnostic_sink.records[0] if entry == "error" else channel.slog.call_args.args[0]
    assert "MEDIA_SENTINEL" not in record.message and media.hex() not in record.message
    payload = json.loads(record.message)
    stdout = payload["tool"]["calls"][0]["stdout"]
    stderr_document = payload["tool"]["calls"][0]["stderr"]
    assert stdout == {"state": "complete", "byte_count": len(media)}
    if valid:
        assert stderr_document["hex"] == stderr.hex()
        assert stderr_document["text"] == stderr.decode("utf-8", "backslashreplace")
    else:
        assert stderr_document["data"]["hex"] == stderr.hex()
        assert stderr_document["data"]["text"] == stderr.decode("utf-8", "backslashreplace")
        assert "diagnostic_format_error" in payload


@pytest.mark.asyncio
async def test_low_log_without_context_does_not_create_an_output_channel():
    assert await get_logger("test").info("fact") is None
    assert get_sync_logger("test").warning("fact") is None


@pytest.mark.parametrize("kwargs,error_type", [({"extra": []}, TypeError),
    ({"exc_info": "invalid"}, TypeError), ({"stacklevel": 0}, ValueError),
    ({"stacklevel": True}, ValueError), ({"unknown": True}, TypeError)])
def test_invalid_call_parameters_raise_before_returning_an_awaitable(kwargs, error_type):
    with pytest.raises(error_type):
        get_logger("test").info("fact", **kwargs)
