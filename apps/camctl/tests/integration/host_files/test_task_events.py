"""真实文件工作线程继承所属动作的日志身份，并保留并发隔离。"""

import asyncio
import json
import threading

import pytest

from camctl.host_files.tasks import FileTask, FileTaskExecutor, FileTaskId
from camctl.logging_runtime.events import event_logging, log_context, record_event
from camctl.session.supervision import Supervisor


@pytest.mark.asyncio
async def test_parallel_file_threads_keep_their_submitter_identity():
    records, barrier = [], threading.Barrier(2)

    class Channel:
        def slog(self, record):
            records.append(json.loads(record.message))

    class Owner:
        async def take_over(self, task):
            pytest.fail("正常任务无需接手")

    executor = FileTaskExecutor(Supervisor())

    async def execute(action_id):
        def body(stop):
            barrier.wait(timeout=5)
            record_event("file_body")
            return action_id

        with log_context(action_id=action_id):
            return await executor.run_file_task(FileTask(FileTaskId(f"file-{action_id}"),
                action_id, "copy", "复制", body), Owner())

    with event_logging(Channel()):
        results = await asyncio.gather(execute(7), execute(8))
    assert [result.value for result in results] == [7, 8]
    assert len(records) == 6
    for action_id in (7, 8):
        owned = [record for record in records if record["action_id"] == action_id]
        assert [(record["stage"], record["event"]) for record in owned] == [
            ("file_task", "started"), ("file_body", "returned"), ("file_task", "returned")]
        assert all(record["file_task_id"] == f"file-{action_id}"
                   and record["file_ids"] == [action_id] for record in owned)
