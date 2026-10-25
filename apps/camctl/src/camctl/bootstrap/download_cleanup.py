"""按原 READ 责任与启动收场边界，有限回收主机下载遗留。"""

from contextlib import closing
import asyncio

from camctl.capture.recovery import RecoveryBoundary
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.enums import enum_for
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.host_files.downloads import load_download_cursor, save_download_cursor, recover_download
from camctl.operations.models import AttemptTicket, SettlementBasis
from camctl.operations.result_format import read_result_document
from camctl.operations.validation import validate_outcome

_READ_FILE = int(enum_for("operation_runs.kind").READ_FILE)
_RUNNING = int(enum_for("operation_attempts.status").RUNNING)


class ReadDownloadCleanup:
    """一轮最多核对 limit 个原 READ；未知目录不枚举、不删除。"""

    def __init__(self, staging, *, limit, boundary):
        if type(limit) is not int or limit <= 0:
            raise ValueError("下载遗留维护额度必须是正整数")
        if not isinstance(boundary, RecoveryBoundary):
            raise ValueError("下载遗留维护必须携带正式恢复边界")
        self.staging, self.limit, self.boundary = staging, limit, boundary
        self.horizon = None
        self.ceiling = None
        self.finished = False
        self.remaining = limit
        self.after = None
        self._lock = asyncio.Lock()

    async def initialize(self, database, *, horizon):
        def read_ceiling(owned):
            with closing(owned.connection.execute(
                "SELECT COALESCE(MAX(a.intent_event_id),0) FROM operation_attempts a"
                " JOIN operation_runs r ON r.id=a.run_id JOIN file_copies c ON c.id=r.copy_id"
                " JOIN device_files d ON d.id=c.source_device_file_id"
                " JOIN actions s ON s.id=d.observer_action_id WHERE r.kind=? AND s.driver_id=?",
                (_READ_FILE, DRIVER_ID))) as cursor:
                return int(cursor.fetchone()[0])

        ceiling = await database.read(read_ceiling, description="READ下载遗留上界读取")
        self.horizon = horizon
        self.ceiling = ceiling

    async def run(self, database):
        async with self._lock:
            await self._run(database)

    async def _run(self, database):
        if self.finished:
            return
        if self.ceiling is None:
            # 未接入启动初始化的调用方只固定本次候选范围；缺少主机
            # 上界仍阻止 RUNNING/ASSUMED 清理，不推断旧执行已经停止。
            await self.initialize(database, horizon=self.horizon)
        if self.after is None:
            self.after = await load_download_cursor(self.staging)
            if self.after >= self.ceiling:
                self.after = 0
        after, ceiling, remaining = self.after, self.ceiling, self.remaining

        def read_candidates(owned):
            with closing(owned.connection.execute(
                    "SELECT r.id,a.attempt_no,r.copy_id,r.responsibility_key,r.kind,a.status,a.intent_event_id,"
                    "a.result_event_id,a.effect_state,a.result_json,a.error_json,s.driver_id"
                    " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
                    " JOIN file_copies c ON c.id=r.copy_id"
                    " JOIN device_files d ON d.id=c.source_device_file_id"
                    " JOIN actions s ON s.id=d.observer_action_id"
                    " WHERE r.kind=? AND s.driver_id=? AND a.intent_event_id>? AND a.intent_event_id<=?"
                    " ORDER BY a.intent_event_id LIMIT ?",
                    (_READ_FILE, DRIVER_ID, after, ceiling, remaining))) as cursor:
                return cursor.fetchall()

        candidates = await database.read(read_candidates, description="READ下载遗留候选读取")
        for run_id, attempt_no, *row in candidates:
            self.remaining -= 1
            self.after = row[4]
            try:
                await self._recover(run_id, attempt_no, row)
            except BaseException as error:
                try:
                    await save_download_cursor(self.staging, self.after)
                except BaseException as cursor_error:
                    raise BaseExceptionGroup("下载维护与游标保存均失败", [error, cursor_error]) from None
                raise
            await save_download_cursor(self.staging, self.after)
        self.finished = True

    async def _recover(self, run_id, attempt_no, row):
        copy_id, responsibility, _, status, intent, result_event, effect, result, error, _ = row
        ticket = AttemptTicket(attempt_no, "read", str(copy_id), responsibility, run_id)
        if status == _RUNNING and result is None:
            # 启动边界不授权清理本次新派发；当前实际拥有者另行核实。
            if (self.boundary is RecoveryBoundary.UNCONFIRMED or self.horizon is None
                    or intent is None or intent > self.horizon):
                return
        elif status != _RUNNING and result is not None and result_event is not None:
            outcome = read_result_document(status, effect, parse_exact_json(result),
                                           None if error is None else parse_exact_json(error))
            validate_outcome(ticket, outcome, EVIDENCE)
            if outcome.settlement.basis is SettlementBasis.ASSUMED:
                if (self.boundary is RecoveryBoundary.UNCONFIRMED or self.horizon is None
                        or intent is None or intent > self.horizon):
                    return
        else:
            raise ValueError("原 READ 下载责任的状态与结果不一致")
        await recover_download(self.staging, ticket)
