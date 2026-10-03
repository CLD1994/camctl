"""媒体事实用例的实际执行环境；任务生命周期另由直接入口测试验证。"""

import asyncio

from camctl.host_files import media
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId
from camctl.session.supervision import Supervisor


class _Owner:
    async def take_over(self, task):
        raise AssertionError("事实用例的等待者没有取消，不应产生接手")


async def _run(operation, args, stop):
    executor = FileTaskExecutor(Supervisor())
    identity = FileTaskId("media-fact")
    task = asyncio.create_task(operation(*args, executor=executor, task_id=identity, owner=_Owner()))
    watcher = None
    if stop is not None:
        # 先让入口登记任务，之后的停止请求通过公开执行器发出。
        await asyncio.sleep(0)

        async def forward_stop():
            await stop.requested()
            executor.request_stop(identity)

        watcher = asyncio.create_task(forward_stop())
    try:
        result = await task
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    assert result.ran is True and result.error is None, result
    return result.value


async def probe_media(input, roots, request, *, stop=None):
    return await _run(media.probe_media, (input, roots, request), stop)


async def repair_media(input, output, roots, request, *, stop=None):
    return await _run(media.repair_media, (input, output, roots, request), stop)
