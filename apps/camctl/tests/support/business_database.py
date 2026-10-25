"""业务单元测试内联执行完整callback，复用真实执行结果分类。"""
import asyncio

from camctl.persistence.business import BusinessDatabase
from camctl.persistence.executor import DbExecutor
from camctl.persistence.runtime import OwnedConnection


def inline_business_database(owned: OwnedConnection) -> BusinessDatabase:
    """owned由用例提供受真实接口约束的连接替身；本函数不启动线程。"""
    executor = DbExecutor(lambda: owned)
    def submit(job):
        future = asyncio.get_running_loop().create_future()
        executor._results[job.key] = future
        executor._execute(job, owned)
        return future
    executor.submit_read = submit
    executor.submit_write = submit
    return BusinessDatabase(executor, owned.metadata)
