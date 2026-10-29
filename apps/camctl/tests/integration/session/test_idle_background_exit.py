"""会话在数据库空闲时交付已有后台结果，再核对关闭竞争。"""

import asyncio
from dataclasses import replace

import pytest

from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork, FlowWakeup
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.session import SessionRepository
from camctl.session.service import run_session
from camctl.session.work import WorkFacts, classify_work
from camctl.session.locks import probe_admission
from camctl.scheduling.notifications import WorkNotifier
from ..support.database import create_test_database
from .support import session_context, assert_database_closed
from .test_session import FixedClock, _MIN

pytestmark = pytest.mark.asyncio


class ObservedWork(CombinedLocalWork):
    def __init__(self, owner):
        super().__init__((owner,))
        self.settling = asyncio.Event()

    async def settle(self):
        self.settling.set()
        await super().settle()


def idle_world(tmp_path, *, report_failed=False, completed=None):
    path = tmp_path / "state.db"
    create_test_database(path)
    facts = WorkFacts(0, 0, False, report_failed, False, False, False, False)
    state = {"facts": facts, "rounds": 0, "closes": []}
    entered, release = asyncio.Event(), asyncio.Event()

    async def body(_context):
        state["rounds"] += 1
        entered.set()
        await release.wait()
        if completed is not None:
            completed(state)
        return FlowWakeup.IDLE

    owner = BackgroundFlow(body)
    local = ObservedWork(owner)
    class ObservedRepository(SessionRepository):
        def close_admission(self, command, key, owned):
            def observed(connection):
                facts = command.facts_query(connection)
                state["closes"].append(classify_work(facts).needs_driver)
                return facts
            return super().close_admission(replace(command, facts_query=observed), key, owned)

    context = session_context(
        path, clock=FixedClock(_MIN), session_repository=ObservedRepository(),
        flows={"obtain": owner}, local_work=local,
        facts_query=lambda connection: state["facts"], poll_interval_s=0.001,
    )
    return context, state, local, entered, release



@pytest.mark.parametrize("report_failed", [False, True])
async def test_idle_database_enters_settlement_without_restarting_empty_rounds(tmp_path, report_failed):
    context, state, local, entered, release = idle_world(tmp_path,
        report_failed=report_failed)
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(local.settling.wait(), 2)
        assert not task.done()
        assert state["rounds"] == 1
        assert not probe_admission(context.paths.admission_lock).is_free
        assert await context.database.read(lambda owned: owned.connection.execute("SELECT 1").fetchone()) == (1,)
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.succeeded is (not report_failed)
        assert result.reason == ("report_error" if report_failed else None)
        assert state["rounds"] == 1
        assert state["closes"] == [False]
        assert probe_admission(context.paths.admission_lock).is_free
        assert local.required_settlements() == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("completion_phase", ["settlement", "facts_read"])
async def test_late_background_failure_is_delivered_before_normal_close(tmp_path, completion_phase):
    failure = OSError(5, "后台实际保存失败", "/state")

    def fail(_state):
        raise failure

    context, state, local, entered, release = idle_world(tmp_path, completed=fail)
    if completion_phase == "facts_read":
        original_open = context.open_database

        async def open_database():
            database = await original_open()
            original_read = database.read

            async def read(callback, **kwargs):
                result = await original_read(callback, **kwargs)
                if kwargs.get("description") == "会话工作事实":
                    release.set()
                    await context.flows["obtain"].unconsumed_task
                return result

            database.read = read
            return database

        context.open_database = open_database
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(local.settling.wait(), 2)
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.reason == "state_db_error"
        assert "后台实际保存失败" in str(result.details)
        assert "/state" in str(result.details)
        assert state["rounds"] == 1
        assert state["closes"] == []
        await assert_database_closed(context.database)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("report_failed", [False, True])
async def test_new_work_during_settlement_is_driven_before_admission_closes(tmp_path, report_failed):
    def new_work(state):
        if state["rounds"] == 1:
            state["facts"] = replace(state["facts"], unfinished_actions=1)

    context, state, local, entered, release = idle_world(tmp_path,
        report_failed=report_failed, completed=new_work)
    original = context.flows["obtain"]

    async def flow(current):
        if state["facts"].unfinished_actions:
            assert not probe_admission(context.paths.admission_lock).is_free
            state["facts"] = replace(state["facts"], unfinished_actions=0)
        await original(current)

    context.flows = {"obtain": flow}
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(local.settling.wait(), 2)
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.succeeded is (not report_failed)
        assert result.reason == ("report_error" if report_failed else None)
        assert state["closes"] == [True, False]
        assert state["rounds"] == 2
        assert probe_admission(context.paths.admission_lock).is_free
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_persistent_work_keeps_driving_before_idle_settlement(tmp_path):
    context, state, local, entered, release = idle_world(tmp_path)
    state["facts"] = replace(state["facts"], unfinished_actions=1)
    waiting, resume = asyncio.Event(), asyncio.Event()

    class PausedNotifier(WorkNotifier):
        async def wait_changed(self, observed, deadline):
            waiting.set()
            await resume.wait()
            return await super().wait_changed(observed, deadline)

    context.wake = PausedNotifier()
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert entered.is_set()
        assert not local.settling.is_set()
        assert not task.done()
        assert not probe_admission(context.paths.admission_lock).is_free
        state["facts"] = replace(state["facts"], unfinished_actions=0)
        release.set()
        resume.set()
        result = await asyncio.wait_for(task, 2)
        assert result.succeeded
        assert probe_admission(context.paths.admission_lock).is_free
    finally:
        release.set()
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
