"""恢复边界在本次执行会话首次可靠打开后固定，先于业务事务。"""

import pytest

from camctl.session.service import run_session

from .test_session import environment, FakeClock, _facts, _MIN


@pytest.mark.asyncio
async def test_run_initializes_recovery_once_before_clock_and_device_work(environment):
    context, _, _, _ = environment
    observed = []

    def initialize(owned):
        assert not owned.connection.in_transaction
        observed.append(owned.connection.execute("SELECT COALESCE(MAX(id), 0) FROM history_events").fetchone()[0])

    async def check(current):
        assert len(observed) == 1

    context.on_session_open = initialize
    context.facts_query = lambda connection: _facts()
    context.clock = FakeClock([_MIN + 60_000_000] * 3)
    context.flows = {"device": check}

    outcome = await run_session(context, None)

    assert outcome.succeeded
    assert observed == [0]


@pytest.mark.asyncio
async def test_recovery_seed_failure_stops_before_device_work(environment):
    context, _, _, _ = environment
    calls = []

    def initialize(owned):
        raise RuntimeError("initial history boundary unavailable")

    async def device(current):
        calls.append("device")

    context.on_session_open = initialize
    context.flows = {"device": device}

    outcome = await run_session(context, None)

    assert not outcome.succeeded
    assert outcome.reason == "state_db_error"
    assert calls == []
