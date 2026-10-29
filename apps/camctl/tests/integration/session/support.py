"""会话测试共用正式装配，并通过公开数据库端口观察关闭。"""

import asyncio

from camctl.persistence.models import DbExecutorError
from camctl.scheduling.notifications import WorkNotifier
from ..support.session import open_session_database, session_context


async def assert_database_closed(database):
    """已关闭的公开端口拒绝读取；不跨线程探测 SQLite 连接。"""
    try:
        await database.read(lambda owned: owned.connection.execute("SELECT 1").fetchone())
    except DbExecutorError:
        return
    raise AssertionError("会话返回后业务数据库仍接收读取")



class ObservedWorkNotifier(WorkNotifier):
    """沿真实通知端口记录会话已进入等待，避免用启动耗时推测进度。"""

    def __init__(self):
        super().__init__()
        self.wait_started = asyncio.Event()

    async def wait_changed(self, observed, deadline):
        self.wait_started.set()
        return await super().wait_changed(observed, deadline)
