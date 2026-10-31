"""受管命令日志对应真实句柄与收场，不执行系统进程。"""

import asyncio
import json
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime.events import event_logging, log_context
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel
from camctl.operations.process import LocalExit, ToolCallCancelled, ToolStartError, execute_tool
from .test_process import FakeProcess, FakeStop, _ready, _spec, _within

pytestmark = pytest.mark.asyncio


def _channel():
    return create_autospec(LogChannel, instance=True, spec_set=True)


def _records(channel):
    records = [call.args[0] for call in channel.slog.call_args_list]
    assert all(record.level is LogLevel.INFO for record in records)
    return [json.loads(record.message) for record in records]


async def test_started_requires_handle_and_returned_requires_exit_output_and_watchers():
    entered, release, output_release, watcher_closed = (asyncio.Event() for _ in range(4))

    class Process(FakeProcess):
        async def wait_output(self):
            await output_release.wait()

    class Stop(FakeStop):
        async def requested(self):
            try:
                await super().requested()
            finally:
                await asyncio.sleep(0)
                watcher_closed.set()

    process = Process(b"private media")
    process.stderr = b"private diagnostic"
    channel = _channel()

    async def spawn(spec):
        entered.set()
        await release.wait()
        return process

    with event_logging(channel), log_context(action_id=31, activity_id="7"):
        task = asyncio.create_task(execute_tool(_spec(timeout_s=Decimal("9")), stop=Stop(), spawner=spawn))
        try:
            await entered.wait()
            assert _records(channel) == []
            release.set()
            await _within(lambda: len(_records(channel)) == 1)
            assert _records(channel)[0]["event"] == "started"
            process.exit(exit_code=255)
            await asyncio.sleep(0)
            assert len(_records(channel)) == 1
            output_release.set()
            outcome = await task
        finally:
            release.set()
            output_release.set()
            process.exit(exit_code=255)
            await asyncio.gather(task, return_exceptions=True)
    started, returned = _records(channel)
    assert watcher_closed.is_set()
    assert (started["stage"], returned["stage"]) == ("external_tool", "external_tool")
    assert started["tool_call_id"] == returned["tool_call_id"]
    assert returned["event"] == "returned" and returned["local_exit_code"] == 255
    assert outcome.exit == LocalExit(exit_code=255)
    assert returned["tool_error"] is None and returned["local_signal"] is None
    for record in (started, returned):
        assert record["tool_argv"] == ["tool", "--flag"]
        assert record["timeout_s"] == "9"
        assert record["action_id"] == 31 and record["activity_id"] == "7"
        assert "private" not in json.dumps(record)
        assert "status" not in record
    assert returned["elapsed_ms"] >= 0


@pytest.mark.parametrize("trigger", ["timeout", "cancelled", "output_failed"])
async def test_terminated_command_reports_actual_exit_and_original_call_error(trigger):
    class Process(FakeProcess):
        def request_terminate(self):
            super().request_terminate()
            self.exit(signal=15)
            raise PermissionError("signal denied after exit")

    process = Process(b"private")
    if trigger == "output_failed":
        process.output_failure = "pipe read failed"
    stop = FakeStop()
    if trigger == "cancelled":
        stop.request()
    channel = _channel()
    with event_logging(channel):
        outcome = await execute_tool(_spec(timeout_s=Decimal(0) if trigger == "timeout" else None),
                                     stop=stop, spawner=lambda _: _ready(process))
    started, returned = _records(channel)
    assert started["event"] == "started" and returned["event"] == "returned"
    assert returned["tool_error"] == outcome.error == trigger
    assert returned["local_signal"] == 15 and returned["local_exit_code"] is None
    assert returned["output_failure"] == process.output_failure
    assert returned["signal_failures"] == [{"stage": "terminate", "message": "PermissionError: signal denied after exit"}]


async def test_cancelled_waiter_records_actual_command_return_before_propagating_cancel():
    class Process(FakeProcess):
        def request_terminate(self):
            super().request_terminate()
            self.exit(signal=15)

    process = Process()
    channel = _channel()
    with event_logging(channel):
        task = asyncio.create_task(execute_tool(_spec(), stop=FakeStop(), spawner=lambda _: _ready(process)))
        await _within(lambda: len(_records(channel)) == 1)
        task.cancel()
        with pytest.raises(ToolCallCancelled) as caught:
            await task
    assert caught.value.outcome.exit == LocalExit(signal=15)
    assert [record["event"] for record in _records(channel)] == ["started", "returned"]
    assert _records(channel)[1]["tool_error"] == "cancelled"


async def test_spawn_failure_has_no_started_or_returned_event_and_preserves_cause():
    error = FileNotFoundError("tool missing")
    channel = _channel()

    async def spawn(spec):
        raise error

    with event_logging(channel):
        with pytest.raises(ToolStartError) as caught:
            await execute_tool(_spec(), stop=FakeStop(), spawner=spawn)
    assert caught.value.__cause__ is error
    record, = _records(channel)
    assert record["event"] == "start_failed"
    assert record["exception_type"] == "FileNotFoundError"
    assert "local_exit_code" not in record


async def test_unexpected_observer_error_preserves_original_error_and_records_raised():
    error = OSError("wait failed")

    class Process(FakeProcess):
        async def wait(self):
            raise error

    channel = _channel()
    with event_logging(channel):
        with pytest.raises(OSError) as caught:
            await execute_tool(_spec(), stop=FakeStop(), spawner=lambda _: _ready(Process()))
    assert caught.value is error
    assert [record["event"] for record in _records(channel)] == ["started", "raised"]
    assert _records(channel)[1]["exception_type"] == "OSError"
    assert "local_exit_code" not in _records(channel)[1]


async def test_concurrent_commands_keep_unique_call_ids_and_original_identities():
    channel = _channel()

    async def run(activity):
        process = FakeProcess()
        process.exit(exit_code=0)
        with log_context(activity_id=activity):
            return await execute_tool(_spec(), stop=FakeStop(), spawner=lambda _: _ready(process))

    with event_logging(channel):
        await asyncio.gather(run("a"), run("b"))
    records = _records(channel)
    calls = {}
    for record in records:
        calls.setdefault(record["tool_call_id"], []).append(record)
    assert len(calls) == 2
    assert {pair[0]["activity_id"] for pair in calls.values()} == {"a", "b"}
    assert all([record["event"] for record in pair] == ["started", "returned"]
               and pair[0]["activity_id"] == pair[1]["activity_id"] for pair in calls.values())


async def test_rejected_arguments_do_not_claim_an_executed_command():
    channel = _channel()
    with event_logging(channel):
        with pytest.raises(ValueError):
            await execute_tool(_spec(), stop=FakeStop(), spawner=lambda _: _ready(FakeProcess()),
                               stdout_sink=lambda data: None)
    assert _records(channel) == []
