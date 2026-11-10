"""原录像终态事务可靠未提交后，已生效取消结束其普通登记资格。"""

from dataclasses import replace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _NOW
from ..capture.test_record_media_result_settlement import _attempts


pytestmark = pytest.mark.asyncio


class _BeforeCandidates(Exception):
    """本次测试只验证前置责任核实，后续取消收场由拥有者推进。"""


class _CandidateBoundary:
    def __init__(self, connection, check):
        self.connection, self.check = connection, check

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.split())
        if normalized.startswith((
            "SELECT id,device_id,scheduled_at,plan_id,input_index FROM actions",
            "SELECT t.id, t.attempt_no, r.id, r.kind, r.activity_id",
            "SELECT id FROM actions WHERE type = 2 AND status = 2",
            "SELECT id, status, input_fields_json FROM actions WHERE type = 6",
        )):
            self.check()
            raise _BeforeCandidates
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("entrance", [
    "normal", "residual", "restricted", "normal-cancel", "restricted-cancel",
])
@pytest.mark.parametrize("terminal", [None, False, True], ids=["cancel-accepted", "cancel-effective", "target-canceled"])
async def test_default_entry_resolves_uncommitted_recording_decision_from_cancellation_facts(
        pipeline, capture_resources, monkeypatch, entrance, terminal):
    from ..support.bootstrap_consumers import recording_world, TransactionFault, apply_cancellation

    world = await recording_world(pipeline, capture_resources)
    original_attempts = _attempts(world.owned)
    original_save = CaptureRepository.finish_recording_results
    inputs, outcomes = [], []

    def save(repository, request, key, current):
        inputs.append((request, key))
        if len(inputs) == 1:
            fault = TransactionFault(current.connection, after_commit=False)
            result = original_save(repository, request, key, replace(current, connection=fault))
            assert fault.commit_calls == 1
        else:
            result = original_save(repository, request, key, current)
        outcomes.append(result)
        return result

    monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
    try:
        world.wall[0] = _NOW + 200_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, world.runtime)
        assert len(inputs) == 1 and outcomes[0].kind is DbOutcomeKind.UNKNOWN
        request, key = inputs[0]
        assert request.capture.occurred_at == world.wall[0]
        await world.reconnect()
        assert saved_transaction_events(world.owned.connection, key) is None
        await apply_cancellation(world, terminal)
        assert _attempts(world.owned) == original_attempts
        world.prime_clock()
        before = tuple(world.owned.connection.iterdump())
        calls = tuple(world.driver.calls)
        await world.replay(entrance, kind="completion")
        assert inputs == [(request, key), (request, key)]
        assert outcomes[1].kind is DbOutcomeKind.COMPLETED, outcomes[1].error
        assert outcomes[1].value.disposition.value == ("saved" if terminal is None else "retired")
        events = saved_transaction_events(world.owned.connection, key)
        if terminal is None:
            assert events is not None and {event["occurred_at"] for event in events} == {request.capture.occurred_at}
            assert world.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (3, 0)
            assert world.owned.connection.execute("SELECT status FROM operation_runs WHERE id=?", (request.run_id,)).fetchone() == (3,)
            assert world.owned.connection.execute("SELECT device_file_id FROM outputs").fetchall() == [(world.source_id,)]
            assert _attempts(world.owned) == original_attempts
        else:
            assert events is None and tuple(world.owned.connection.iterdump()) == before
        assert tuple(world.driver.calls) == calls and len(world.reader.requests) == 1
        assert world.results.calls == [1] and world.tools() == ["probe"]
    finally:
        await world.close()
