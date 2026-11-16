"""报告发布编排使用的业务数据库适配器。"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

from camctl.contracts.values import new_operation_key
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import transfer_db_outcome
from camctl.persistence.runtime import StateDatabaseError
from camctl.reporting import policy


class DbPublicationSession:
    """每项报告管理操作各自提交完整事务。"""
    def __init__(self, database: BusinessDatabase, *,
                 occurred_at: Callable[[], int] | None = None) -> None:
        self._database = database
        self._occurred_at = occurred_at if occurred_at is not None else lambda: time.time_ns() // 1000

    async def _write(self, method, *args, **kwargs):
        key = new_operation_key()
        occurred_at = self._occurred_at()
        try:
            return await self._database.write(
                lambda owned: method(key, owned, *args, occurred_at=occurred_at, **kwargs), key=key)
        except sqlite3.Error as error:
            raise transfer_db_outcome(error, StateDatabaseError(
                f"报告状态库写入失败: {error}")) from error

    async def prepare(self, report_id, contents):
        return await self._write(policy.record_report_bytes, report_id, contents)

    async def record_intent(self, report_id):
        return await self._write(policy.record_report_publish_intent, report_id)

    async def save_publication(self, report_id, file_result):
        return await self._write(policy.publish_report, report_id, file_result)

    async def record_failure(self, report_id, error):
        return await self._write(policy.record_report_failure, report_id, error)

    async def settle_local(self, action_id, report_id):
        return await self._write(policy.record_local_report, action_id=action_id,
            local_report_id=report_id)
