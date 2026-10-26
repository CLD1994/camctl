"""会话在数据库空闲时交付已有后台结果，再核对关闭竞争。"""

import asyncio
from dataclasses import replace

import pytest

from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork, FlowWakeup
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.session import CloseDecision
from camctl.session import service
from camctl.session.service import run_session
from camctl.session.work import WorkFacts, classify_work
from unit.session.test_business_database_session import world

pytestmark = pytest.mark.asyncio


class ObservedWork(CombinedLocalWork):
    def __init__(self, owner):
        super().__init__((owner,))
        self.settling = asyncio.Event()

    async def settle(self):
        self.settling.set()
        await super().settle()


def idle_world(*, report_failed=False, completed=None):
    context, database, owned, repository, session, admission = world()
    owned.connection.execute.return_value.fetchone.return_value = None
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
    context.flows = {"obtain": owner}
    context.local_work = local
    context.facts_query = lambda _connection: state["facts"]
    context.poll_interval_s = 0.001

    def close(command, key, current):
        assert current is owned
        decision = classify_work(command.facts_query(current.connection))
        state["closes"].append(decision.needs_driver)
        if not decision.needs_driver:
            command.release_admission()
        return DbOutcome(DbOutcomeKind.COMPLETED, CloseDecision(not decision.needs_driver, decision))

    repository.close_admission.side_effect = close
    return context, database, repository, session, admission, state, local, entered, release


@pytest.mark.parametrize("report_failed", [False, True])
async def test_idle_database_enters_settlement_without_restarting_empty_rounds(report_failed):
    context, database, repository, session, admission, state, local, entered, release = idle_world(
        report_failed=report_failed)
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(local.settling.wait(), 2)
        assert not task.done()
        assert state["rounds"] == 1
        admission.close.assert_not_called()
        database.executor.close.assert_not_called()
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.succeeded is (not report_failed)
        assert result.reason == ("report_error" if report_failed else None)
        assert state["rounds"] == 1
        assert state["closes"] == [False]
        admission.close.assert_called()
        assert local.required_settlements() == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("completion_phase", ["settlement", "facts_read"])
async def test_late_background_failure_is_delivered_before_normal_close(completion_phase):
    failure = OSError(5, "后台实际保存失败", "/state")

    def fail(_state):
        raise failure

    context, database, repository, session, admission, state, local, entered, release = idle_world(completed=fail)
    if completion_phase == "facts_read":
        original_read = database.read

        async def read(callback, **kwargs):
            result = await original_read(callback, **kwargs)
            if kwargs.get("description") == "会话工作事实":
                release.set()
                await context.flows["obtain"].unconsumed_task
            return result

        database.read = read
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
        repository.close_admission.assert_not_called()
        database.executor.close.assert_awaited_once()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("report_failed", [False, True])
async def test_new_work_during_settlement_is_driven_before_admission_closes(report_failed):
    def new_work(state):
        if state["rounds"] == 1:
            state["facts"] = replace(state["facts"], unfinished_actions=1)

    context, database, repository, session, admission, state, local, entered, release = idle_world(
        report_failed=report_failed, completed=new_work)
    original = context.flows["obtain"]

    async def flow(current):
        if state["facts"].unfinished_actions:
            assert admission.close.call_count == 0
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
        admission.close.assert_called()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_persistent_work_keeps_driving_before_idle_settlement(monkeypatch):
    context, database, repository, session, admission, state, local, entered, release = idle_world()
    state["facts"] = replace(state["facts"], unfinished_actions=1)
    waiting, resume = asyncio.Event(), asyncio.Event()

    async def wait_for_more(*_args, **_kwargs):
        waiting.set()
        await resume.wait()

    monkeypatch.setattr(service, "_wait_for_more_work", wait_for_more)
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert entered.is_set()
        assert not local.settling.is_set()
        assert not task.done()
        admission.close.assert_not_called()
        state["facts"] = replace(state["facts"], unfinished_actions=0)
        release.set()
        resume.set()
        result = await asyncio.wait_for(task, 2)
        assert result.succeeded
        admission.close.assert_called()
    finally:
        release.set()
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
