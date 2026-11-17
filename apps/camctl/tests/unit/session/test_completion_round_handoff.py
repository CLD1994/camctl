"""多个后台发现轮次错开完成时，会话先核实退出，再启动新轮次。"""

import asyncio

import pytest

from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork, ReportBackgroundFlow
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.session import CloseDecision
from camctl.session.service import run_session
from camctl.session.work import WorkFacts, classify_work
from camctl.scheduling.notifications import WorkNotifier

from .test_business_database_session import world

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("new_work", [False, True])
async def test_last_completed_owner_checks_work_before_restarting_an_idle_owner(new_work):
    context, _database, owned, _repository, _session, _admission = world()
    owned.connection.execute.return_value.fetchone.return_value = None
    context.poll_interval_s = 0
    completed, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, pending = [], ["new input"] if new_work else []

    async def obtain(current):
        calls.append("obtain")
        await completed.wait()

    async def cleanup(current):
        calls.append("cleanup")
        entered.set()
        await release.wait()
        pending.clear()

    obtain_owner, cleanup_owner = BackgroundFlow(obtain), BackgroundFlow(cleanup)
    context.flows = {"obtain": obtain_owner, "cleanup": cleanup_owner}
    context.local_work = CombinedLocalWork((obtain_owner, cleanup_owner))
    context.facts_query = lambda _: WorkFacts(
        len(pending), 0, False, False, False, False, False, False)
    await obtain_owner(context)
    completed.set()
    await asyncio.sleep(0)
    assert context.local_work.required_settlements() == 1

    session = asyncio.create_task(run_session(context, None))
    restarted = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait((session, restarted), return_when=asyncio.FIRST_COMPLETED)
    finally:
        release.set()
        restarted.cancel()
        await asyncio.gather(restarted, return_exceptions=True)
    result = await session

    assert result.succeeded
    assert calls == (["obtain", "cleanup"] if new_work else ["obtain"])
    assert context.local_work.required_settlements() == 0


async def test_running_owner_remains_required_and_new_work_can_advance():
    context, _database, owned, _repository, _session, _admission = world()
    owned.connection.execute.return_value.fetchone.return_value = None
    context.poll_interval_s = 0
    release = asyncio.Event()
    calls = []

    async def obtain(current):
        await release.wait()

    async def cleanup(current):
        calls.append("cleanup")
        release.set()

    obtain_owner, cleanup_owner = BackgroundFlow(obtain), BackgroundFlow(cleanup)
    context.flows = {"obtain": obtain_owner, "cleanup": cleanup_owner}
    context.local_work = CombinedLocalWork((obtain_owner, cleanup_owner))
    await obtain_owner(context)

    result = await run_session(context, None)

    assert result.succeeded
    assert calls == ["cleanup"] and release.is_set()
    assert context.local_work.required_settlements() == 0


async def test_initial_empty_world_still_runs_report_control_and_discovery():
    context, _database, _owned, _repository, _session, _admission = world()
    calls = []

    async def report(current):
        calls.append("report")

    async def control(current):
        calls.append("control")

    async def discover(current):
        calls.append("discovery")

    owner = BackgroundFlow(discover)
    context.local_work = CombinedLocalWork((owner,))
    context.flows = {"report": report, "cancel": control, "obtain": owner}

    result = await run_session(context, None)

    assert result.succeeded
    assert calls == ["report", "control", "discovery"]


async def test_new_report_change_after_completion_keeps_report_and_control_advancing():
    context, _database, _owned, _repository, _session, _admission = world()
    release = asyncio.Event()
    calls, changes = [], ["new report change"]

    async def discover(current):
        await release.wait()

    async def report(current):
        calls.append("report")
        changes.clear()

    async def control(current):
        calls.append("control")

    owner = BackgroundFlow(discover)
    context.local_work = CombinedLocalWork((owner,))
    context.flows = {"report": report, "cancel": control, "obtain": owner}
    context.facts_query = lambda _: WorkFacts(
        0, 0, bool(changes), False, False, False, False, False)
    await owner(context)
    release.set()
    await asyncio.sleep(0)

    result = await run_session(context, None)

    assert result.succeeded
    assert calls == ["report", "control"] and not changes


async def test_close_transaction_sees_new_input_and_returns_to_ordinary_discovery():
    context, _database, owned, repository, _session, admission = world()
    owned.connection.execute.return_value.fetchone.return_value = None
    context.poll_interval_s = 0
    release = asyncio.Event()
    pending, discoveries, close_checks = [], [], []

    async def previous(current):
        await release.wait()

    async def discover(current):
        discoveries.append(tuple(pending))
        pending.clear()

    previous_owner, idle_owner = BackgroundFlow(previous), BackgroundFlow(discover)
    context.local_work = CombinedLocalWork((previous_owner, idle_owner))
    context.flows = {"previous": previous_owner, "obtain": idle_owner}
    context.facts_query = lambda _: WorkFacts(
        len(pending), 0, False, False, False, False, False, False)

    def close(command, key, current):
        if not close_checks:
            pending.append("accepted before admission closes")
        decision = classify_work(command.facts_query(current.connection))
        close_checks.append(decision.needs_driver)
        if decision.needs_driver:
            admission.close.assert_not_called()
        if not decision.needs_driver:
            command.release_admission()
        return DbOutcome(DbOutcomeKind.COMPLETED, CloseDecision(not decision.needs_driver, decision))

    repository.close_admission.side_effect = close
    await previous_owner(context)
    release.set()
    await asyncio.sleep(0)

    result = await run_session(context, None)

    assert result.succeeded
    assert close_checks == [True, False]
    assert discoveries == [("accepted before admission closes",)]
    admission.close.assert_called()


async def test_completed_failure_is_delivered_before_other_owner_can_start():
    context, _database, _owned, _repository, _session, _admission = world()
    release = asyncio.Event()
    calls = []
    failure = ValueError("original background result failed")

    async def failed(current):
        await release.wait()
        raise failure

    async def discover(current):
        calls.append("discovery")

    failed_owner, idle_owner = BackgroundFlow(failed), BackgroundFlow(discover)
    context.local_work = CombinedLocalWork((failed_owner, idle_owner))
    context.flows = {"failed": failed_owner, "obtain": idle_owner}
    await failed_owner(context)
    release.set()
    await asyncio.sleep(0)

    result = await run_session(context, None)

    assert result.reason == "state_db_error"
    assert str(failure) in result.details["error"]
    assert calls == []


@pytest.mark.parametrize("new_work", [False, True])
async def test_report_completion_rechecks_facts_before_following_report_without_poll_delay(new_work):
    context, _database, owned, _repository, _session, _admission = world()
    owned.connection.execute.return_value.fetchone.return_value = None
    context.poll_interval_s = 30
    first_entered, release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    waiting = asyncio.Event()
    events = []
    pending = True

    class ObservedNotifier(WorkNotifier):
        async def wait_changed(self, observed, deadline=None):
            waiting.set()
            return await super().wait_changed(observed, deadline)

    async def maintain(_context):
        nonlocal pending
        events.append("report")
        if len([event for event in events if event == "report"]) == 1:
            first_entered.set()
            await release.wait()
            events.append("first done")
            pending = new_work
        else:
            second_entered.set()
            pending = False

    async def should_advance(_context):
        return pending

    maintain.should_advance = should_advance
    owner = ReportBackgroundFlow(maintain)
    context.wake = ObservedNotifier()
    context.flows = {"report": owner}
    context.local_work = CombinedLocalWork((owner,))

    def facts(_connection):
        events.append("facts")
        return WorkFacts(0, 0, pending, False, False, False, False, False)

    context.facts_query = facts
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(first_entered.wait(), 1)
        await asyncio.wait_for(waiting.wait(), 1)
        release.set()
        if new_work:
            await asyncio.wait_for(second_entered.wait(), 0.5)
        result = await asyncio.wait_for(task, 1)
        assert result.succeeded
        first_done = events.index("first done")
        if new_work:
            second = events.index("report", first_done)
            assert "facts" in events[first_done + 1:second]
        else:
            assert not second_entered.is_set()
        assert events.count("report") == (2 if new_work else 1)
        assert context.local_work.required_settlements() == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
