"""必要接纳事务的取消回执不重新授权普通电机通知。

组合真实数据库线程、接纳事务、文件锁、生产 motor_flow 和本地管道；
回执同步点仅延迟交付，不取消或替换原数据库操作。
"""
import asyncio
import os
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.motor_assembly import motor_flow
from camctl.contracts.clock import ClockPort
from camctl.contracts.values import new_operation_key
from camctl.motor.notification import open_notification_writer
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, register_acceptance_guards
from camctl.persistence.repositories.motor import register_motor_guards
from camctl.persistence.repositories.scheduling import register_window_guard
from camctl.persistence.repositories.session import SessionRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.clock import ClockCheckInput, register_clock_guard
from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths, probe_admission
from camctl.session.service import SessionContext, SessionPaths, run_session
from camctl.session.work import WorkFacts

from ..persistence.test_motor_transactions import NOW, _admit
from ..persistence.test_runtime import _create_valid_database

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.name != "posix", reason="POSIX 通知管道")]


@pytest.mark.parametrize("phase,cancel", [("acquire", True), ("close", True), ("acquire", False)])
async def test_admission_result_preserves_cancel_and_real_motor_dispatch_boundary(tmp_path, phase, cancel):
    path = tmp_path / "state.db"
    _create_valid_database(path)
    register_acceptance_guards()
    register_motor_guards()
    register_window_guard()
    register_clock_guard()
    database = await BusinessDatabase.open(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()))
    if phase == "acquire":
        await database.write(_admit, key=new_operation_key(), description="准备待发电机动作")
    session_path, admission_path = lock_file_paths(path)
    clock = create_autospec(ClockPort, instance=True, spec_set=True)
    clock.utc_micros.return_value = NOW
    clock.monotonic_ns.return_value = 0
    entered, release = asyncio.Event(), asyncio.Event()
    original_write = database.write
    description = "取得接纳资格" if phase == "acquire" else "原子关闭接纳"

    async def delayed_write(callback, **kwargs):
        pause = kwargs.get("description") == description and not entered.is_set()
        if pause and phase == "close":
            # 本轮空闲查询结束后，新输入先于原子关闭提交。
            await original_write(_admit, key=new_operation_key(), description="关闭前接纳新电机动作")
        result = await original_write(callback, **kwargs)
        if pause:
            assert result.kind is DbOutcomeKind.COMPLETED
            if phase == "close":
                assert result.value.admission_closed is False
            entered.set()
            await release.wait()
        return result

    database.write = delayed_write

    async def open_database():
        return database

    def facts(connection):
        count = connection.execute("SELECT COUNT(*) FROM actions WHERE status IN (1,2)").fetchone()[0]
        return WorkFacts(count, 0, False, False, False, False, False, False)

    read_fd, write_fd = os.pipe()
    writer = open_notification_writer(write_fd)
    context = SessionContext(
        CommandMode.RUN, None, clock, open_database, AcceptanceRepository(), SessionRepository(),
        SessionPaths(session_path, admission_path), lambda _: ClockCheckInput(NOW, 0, 0, 1),
        lambda: acquire_session(session_path), lambda: acquire_admission(admission_path), facts,
        flows={"motor": motor_flow(writer, {})}, poll_interval_s=0.001,
    )
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not probe_admission(admission_path).is_free
        if cancel:
            task.cancel("first stop")
            await asyncio.sleep(0)
            task.cancel("second stop")
            await asyncio.sleep(0)
            assert not task.done()
            assert not probe_admission(admission_path).is_free
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError, match="first stop"):
                await asyncio.wait_for(task, 5)
        else:
            assert (await asyncio.wait_for(task, 5)).succeeded
        writer.close()
        notification = os.read(read_fd, 4096)
        assert bool(notification) is (not cancel)
        assert probe_admission(admission_path).is_free
        lease = acquire_session(session_path)
        lease.close()
        # 重开真实库观察持久结果；停止不替业务动作伪造终态或发送事实。
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert owned.connection.execute("SELECT status FROM actions").fetchone() == ((1,) if cancel else (3,))
            assert owned.connection.execute("SELECT COUNT(*) FROM motor_notifications").fetchone() == ((0,) if cancel else (1,))
        finally:
            owned.connection.close()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await database.close()
        writer.close()
        os.close(read_fd)
