"""阶段事件保留真实返回、异常以及各并发任务自己的身份。"""

import asyncio
from datetime import datetime
import json
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime import events
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel, LogRecord


def channel_stub():
    return create_autospec(LogChannel, instance=True, spec_set=True)


def payloads(channel):
    result = []
    for call in channel.slog.call_args_list:
        record = call.args[0]
        payload = json.loads(record.rendered_message())
        assert payload["level"] == record.level.name
        assert datetime.fromisoformat(payload["logged_at_utc"]).utcoffset().total_seconds() == 0
        assert isinstance(payload["pid"], int)
        result.append(payload)
    channel.alog.assert_not_called()
    return result


@pytest.mark.parametrize("level", [LogLevel.INFO, LogLevel.DEBUG])
def test_stage_records_actual_body_and_elapsed_time(monkeypatch, level):
    ticks = iter((1_000_000, 3_500_000))
    monkeypatch.setattr(events, "monotonic_ns", lambda: next(ticks))
    channel = channel_stub()
    with events.event_logging(channel), events.log_context(action_id=7):
        with events.measure_stage("prepare", activity_id="12", level=level):
            events.record_event("inside")
    records = payloads(channel)
    assert [(p["stage"], p["event"]) for p in records] == [
        ("prepare", "started"), ("inside", "returned"), ("prepare", "returned")]
    assert all(p["action_id"] == 7 and p["activity_id"] == "12" for p in records)
    assert records[0]["level"] == records[-1]["level"] == level.name
    assert "elapsed_ms" not in records[0]
    assert records[-1]["elapsed_ms"] == 2.5


@pytest.mark.parametrize("error,event", [(ValueError("original"), "raised"),
                                        (asyncio.CancelledError(), "cancelled")])
def test_original_exception_is_propagated(error, event):
    channel = channel_stub()
    with events.event_logging(channel), pytest.raises(type(error)) as caught:
        with events.measure_stage("prepare"):
            raise error
    assert caught.value is error
    last = payloads(channel)[-1]
    assert last["event"] == event
    assert last["exception_type"] == type(error).__name__


def test_disabled_stage_preserves_context_for_actual_inner_events(monkeypatch):
    channel = channel_stub()
    def unexpected_clock():
        raise AssertionError("disabled stage must not measure time")
    monkeypatch.setattr(events, "monotonic_ns", unexpected_clock)
    with events.event_logging(channel):
        with events.measure_stage("suppressed", level=None, file_id=9):
            events.record_event("actual_inner_work")
        events.record_event("after")
    records = payloads(channel)
    assert [p["stage"] for p in records] == ["actual_inner_work", "after"]
    assert records[0]["file_id"] == 9
    assert "file_id" not in records[1]


@pytest.mark.asyncio
async def test_parallel_tasks_and_nested_channels_keep_their_own_identity():
    outer, inner = channel_stub(), channel_stub()
    ready = asyncio.Event()
    async def work(action_id):
        with events.log_context(action_id=action_id):
            await ready.wait()
            events.record_event("work")
    with events.event_logging(outer), events.log_context(session_key="s"):
        tasks = [asyncio.create_task(work(i)) for i in (1, 2)]
        with events.event_logging(inner), events.log_context(action_id=3):
            events.record_event("nested")
        ready.set()
        await asyncio.gather(*tasks)
        events.record_event("after")
    events.record_event("outside")
    assert [p.get("action_id") for p in payloads(outer)] == [1, 2, None]
    assert all(p["session_key"] == "s" for p in payloads(outer))
    assert payloads(inner)[0]["action_id"] == 3


def test_delivery_or_formatting_failure_does_not_change_the_body():
    channel = channel_stub()
    channel.slog.side_effect = RuntimeError("unavailable")
    with events.event_logging(channel), events.measure_stage("work"):
        events.record_event("unserializable", value=object())
        value = 17
    assert value == 17


def test_plain_record_freezes_metadata_and_preserves_original_message(monkeypatch):
    from camctl.logging_runtime import service
    monkeypatch.setattr(service, "utc_now", lambda: "later")
    record = LogRecord(LogLevel.WARNING, "original\n中文")
    first = json.loads(record.rendered_message())
    assert first["message"] == "original\n中文"
    assert first["level"] == "WARNING" and first["stage"] == "runtime"
    assert record.rendered_message() == record.rendered_message()
    assert first["logged_at_utc"] != "later"


def test_already_structured_diagnostic_is_not_wrapped_again():
    message = '{"level":"ERROR","stage":"device","error":{"code":"E"}}'
    assert LogRecord(LogLevel.ERROR, message, structured=True).rendered_message() == message


def test_context_snapshot_copies_fields_and_restores_channel_and_fallback():
    outer, inner, fallback = channel_stub(), channel_stub(), []
    with events.event_logging(outer, fallback=fallback.append), events.log_context(action_id=7):
        snapshot = events.current_logging_context()
        snapshot.fields["action_id"] = 99
        assert events.current_logging_context().fields == {"action_id": 7}
        assert snapshot.channel is outer and snapshot.fallback == fallback.append
        with events.event_logging(inner), events.measure_stage("work", level=None):
            nested = events.current_logging_context()
            assert nested.channel is inner and nested.fallback is None
            assert nested.fields == {"action_id": 7, "stage": "work"}
        restored = events.current_logging_context()
        assert restored.channel is outer and restored.fallback == fallback.append
        assert restored.fields == {"action_id": 7}
    assert events.current_logging_context().channel is None
    assert events.current_logging_context().fallback is None
    assert events.current_logging_context().fields == {}


@pytest.mark.asyncio
async def test_slow_stage_reports_in_progress_and_actual_completion():
    channel = channel_stub()
    with events.event_logging(channel), events.log_context(session_key="session"):
        with events.monitor_slow_stage("session.flow", threshold_s=0.01, flow="scheduling"):
            await asyncio.sleep(0.03)
            assert payloads(channel)[0]["event"] == "started"
    records = payloads(channel)
    assert [p["event"] for p in records] == ["started", "returned"]
    assert all(p["level"] == "INFO" and p["flow"] == "scheduling" for p in records)
    assert records[0]["started_at_utc"] == records[1]["started_at_utc"]
    assert records[1]["elapsed_ms"] >= 10


@pytest.mark.asyncio
async def test_fast_stage_is_silent_and_slow_error_preserves_exception():
    channel = channel_stub()
    error = ValueError("original")
    with events.event_logging(channel):
        with events.monitor_slow_stage("fast", threshold_s=1):
            await asyncio.sleep(0)
        with pytest.raises(ValueError) as raised:
            with events.monitor_slow_stage("slow", threshold_s=0.01):
                await asyncio.sleep(0.03)
                raise error
    assert raised.value is error
    records = payloads(channel)
    assert [(p["stage"], p["event"]) for p in records] == [
        ("slow", "started"), ("slow", "raised")]
    assert records[-1]["exception_type"] == "ValueError"


@pytest.mark.asyncio
async def test_slow_stage_logs_if_event_loop_could_not_run_the_timer(monkeypatch):
    channel = channel_stub()
    ticks = iter((0, 2_000_000_000, 2_000_000_000))
    monkeypatch.setattr(events, "monotonic_ns", lambda: next(ticks))
    with events.event_logging(channel):
        with events.monitor_slow_stage("session.flow"):
            pass
    assert [p["event"] for p in payloads(channel)] == ["started", "returned"]
    assert payloads(channel)[-1]["elapsed_ms"] == 2000
