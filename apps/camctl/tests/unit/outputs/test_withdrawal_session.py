"""撤回会话的公开返回分区与状态库绑定。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.withdrawal import WithdrawalProgress, WithdrawalSession

pytestmark = pytest.mark.asyncio


class Database:
    def __init__(self, instance='one', ready='/ready'):
        self.metadata = SimpleNamespace(instance_id=instance, ready_path=ready,
                                        processing_path='/processing')

    async def read(self, callback, **kwargs):
        class Cursor:
            def fetchall(self): return [(7, 'photo.jpg', 5, 1, None, 23, 19)]
            def close(self): pass
        return callback(SimpleNamespace(connection=SimpleNamespace(execute=lambda *args: Cursor())))


class BusyExecutor:
    def unfinished_files(self): return {23}


async def test_busy_file_is_explicit_and_reliable_connection_can_change():
    session = WithdrawalSession(ready=Path('/ready'), processing=Path('/processing'), executor=BusyExecutor())
    assert await session.withdraw_delivery(19, database=Database(), occurred_at=lambda: 1) is WithdrawalProgress.WAITING_FOR_FILE
    assert await session.resume_pending_saves(7, database=Database()) is WithdrawalProgress.STEP_FINISHED
    with pytest.raises(ConsistencyError):
        await session.resume_pending_saves(7, database=Database(instance='another'))
    with pytest.raises(ConsistencyError):
        await session.resume_pending_saves(7, database=Database(ready='/other'))
