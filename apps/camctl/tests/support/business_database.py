"""业务单元测试内联执行完整callback，复用真实执行结果分类。"""
import asyncio
from unittest.mock import create_autospec

from camctl.persistence.business import BusinessDatabase
from camctl.persistence.executor import DbExecutor
from camctl.persistence.runtime import OwnedConnection


def inline_business_database(owned: OwnedConnection) -> BusinessDatabase:
    """同步夹具可构造；首次await时内联分类，不启动线程或创建连接。"""
    port = create_autospec(DbExecutor, instance=True, spec_set=True)
    runner = None
    def executor():
        nonlocal runner
        if runner is None:
            runner = DbExecutor(lambda: owned)
        return runner
    def submit(job):
        current = executor()
        future = asyncio.get_running_loop().create_future()
        current._results[job.key] = future
        current._execute(job, owned)
        return future
    async def close():
        if runner is not None:
            await runner.close()
    async def withdraw(key):
        return await executor().withdraw(key)
    port.submit_read.side_effect = submit
    port.submit_write.side_effect = submit
    port.withdraw.side_effect = withdraw
    port.close.side_effect = close
    return BusinessDatabase(port, owned.metadata)
