"""文件任务只在实际执行时记录 INFO，并保留提交者的可靠身份。"""

import asyncio
import json
from contextvars import Context

import pytest

from camctl.host_files.tasks import AsyncFileTask, FileTask, FileTaskExecutor, FileTaskId
from camctl.logging_runtime.events import event_logging, log_context, record_event
from camctl.logging_runtime.models import LogLevel
from camctl.session.supervision import Supervisor


class Channel:
    def __init__(self):
        self.records = []

    def slog(self, record):
        self.records.append(json.loads(record.message))


class Owner:
    async def take_over(self, task):
        pass


@pytest.mark.asyncio
async def test_sync_file_task_retains_identity_in_an_independent_execution_context():
    channel = Channel()

    async def runner(body):
        return Context().run(body)

    executor = FileTaskExecutor(Supervisor(), thread_runner=runner)
    with event_logging(channel), log_context(action_id=11, database_id="database"):
        result = await executor.run_file_task(FileTask(FileTaskId("copy-1"), 7,
            "copy", "复制", lambda stop: record_event("inside_file") or 23), Owner())
    assert result.value == 23
    stages = [entry for entry in channel.records if entry["stage"] == "file_task"]
    assert [entry["event"] for entry in stages] == ["started", "returned"]
    for entry in channel.records:
        assert entry["action_id"] == 11
        assert entry["database_id"] == "database"
        assert entry["file_task_id"] == "copy-1"
        assert entry["file_ids"] == [7]
        assert entry["level"] == "INFO"


@pytest.mark.asyncio
async def test_async_file_task_reports_the_actual_error_without_changing_result():
    channel = Channel()

    async def body(control):
        raise ValueError("original")

    executor = FileTaskExecutor(Supervisor())
    with event_logging(channel), log_context(action_id=12):
        result = await executor.run_async_file_task(AsyncFileTask(FileTaskId("publish-1"),
            (8, 9), "publication", "发布", body), Owner())
    assert result.ran and result.error == "ValueError: original"
    assert [entry["event"] for entry in channel.records] == ["started", "raised"]
    assert all(entry["action_id"] == 12 for entry in channel.records)


@pytest.mark.asyncio
async def test_withdrawn_queued_task_has_no_execution_event():
    channel = Channel()
    entered, release = asyncio.Event(), asyncio.Event()

    async def runner(body):
        entered.set()
        await release.wait()
        return body()

    executor = FileTaskExecutor(Supervisor(), thread_runner=runner)
    with event_logging(channel):
        pending = asyncio.create_task(executor.run_file_task(FileTask(FileTaskId("queued"),
            3, "copy", "复制", lambda stop: pytest.fail("撤回任务不能执行")), Owner()))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert channel.records == []


@pytest.mark.asyncio
async def test_owned_task_reports_original_exception_before_result_wrapper():
    channel, executor = Channel(), FileTaskExecutor(Supervisor())
    original = ValueError("original")

    async def body(control):
        raise original

    with event_logging(channel), pytest.raises(ValueError) as caught:
        await executor.run_owned_async_file_task(AsyncFileTask(
            FileTaskId("owned"), (4,), "publish", "发布", body))
    assert caught.value is original
    assert [entry["event"] for entry in channel.records] == ["started", "raised"]
    assert channel.records[-1]["exception_type"] == "ValueError"


@pytest.mark.asyncio
@pytest.mark.parametrize("level,expected", [(LogLevel.DEBUG, ["DEBUG", "INFO", "DEBUG"]),
                                          (None, ["INFO"])])
async def test_producer_level_controls_task_events_while_preserving_inner_context(level, expected):
    channel, executor = Channel(), FileTaskExecutor(Supervisor())

    async def body(control):
        record_event("actual_command")
        return 7

    with event_logging(channel), log_context(action_id=13):
        result = await executor.run_owned_async_file_task(AsyncFileTask(
            FileTaskId("segment"), (5,), "copy_segment", "分段复制", body, log_level=level))
    assert result == 7
    assert [entry["level"] for entry in channel.records] == expected
    assert all(entry["action_id"] == 13 and entry["file_task_id"] == "segment"
               for entry in channel.records)
