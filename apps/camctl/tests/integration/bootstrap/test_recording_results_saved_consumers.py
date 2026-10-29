"""公开会话入口在业务终态筛选前核实原录像共同事务。"""

from dataclasses import replace
import sqlite3

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.transaction import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _attempts
from ..support.bootstrap_consumers import (
    StorageGate, TransactionFault, consumer_world, FirstProbeUnavailableExecutor,
)

pytestmark = pytest.mark.asyncio


class _UnavailableOriginalKey:
    """原键查询真实失败；ROLLBACK 失败时仓储仍返回 UNKNOWN。"""

    def __init__(self, connection, key, unknown):
        self.connection, self.key, self.unknown = connection, key, unknown
        self.key_reads = 0

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT id FROM history_transactions WHERE operation_key"):
            assert parameters == (str(self.key),)
            self.key_reads += 1
            raise sqlite3.OperationalError("original transaction unavailable")
        if normalized == "ROLLBACK" and self.unknown:
            raise sqlite3.OperationalError("rollback unavailable")
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("entrance", [
    "normal", "residual", "restricted", "normal-cancel", "restricted-cancel",
])
@pytest.mark.parametrize("confirmation", ["saved", "rolled-back", "unknown"])
async def test_default_entry_confirms_original_recording_transaction_after_terminal(
        pipeline, capture_resources, monkeypatch, entrance, confirmation):
    results = ResultsDouble({1: (replace(_entry("original-recording", size=len(_CONTENT)),
        locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
    world = await consumer_world(pipeline, capture_resources, duration="61", results=results,
        executor_factory=FirstProbeUnavailableExecutor)
    try:
        await capture_handler("camera_record")(1, world.runtime)
        assert len(world.executor.checkpoints) == 1 and world.tools() == []
        assert len(world.reader.requests) == 1 and results.calls == [1]
        assert world.owned.connection.execute("SELECT check_state FROM recording_processing WHERE id=1").fetchone() == (2,)
        assert world.owned.connection.execute(
            "SELECT status,retry_wait_required FROM operation_runs WHERE responsibility_key='results/1'").fetchone() == (2, 1)
        original_attempts = _attempts(world.owned)
        original_save = CaptureRepository.finish_recording_results
        inputs, outcomes = [], []
        gate = StorageGate(original_key=True, unknown=confirmation == "unknown")

        def save(repository, request, key, current):
            inputs.append((request, key))
            if len(inputs) == 1:
                fault = TransactionFault(current.connection, after_commit=True)
                result = original_save(repository, request, key, replace(current, connection=fault))
                assert fault.commit_calls == 1
            elif confirmation != "saved":
                result = gate.submit(lambda *args: original_save(repository, *args), request, key, current)
            else:
                result = original_save(repository, request, key, current)
            outcomes.append(result)
            return result

        monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
        world.wall[0] = _NOW + 200_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, world.runtime)
        assert len(inputs) == 1 and outcomes[0].kind is DbOutcomeKind.UNKNOWN
        request, key = inputs[0]
        assert request.capture.occurred_at == world.wall[0]
        assert world.tools() == ["probe"]
        await world.reconnect()
        assert saved_transaction_events(world.owned.connection, key) is not None
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert _attempts(world.owned) == original_attempts
        world.wall[0] = _NOW + 300_000_000
        world.prime_clock()
        before = tuple(world.owned.connection.iterdump())
        before_calls = tuple(world.driver.calls)
        await world.replay(entrance, kind="completion", failure=confirmation != "saved")
        assert inputs == [(request, key), (request, key)]
        expected = {"saved": DbOutcomeKind.COMPLETED,
            "rolled-back": DbOutcomeKind.ROLLED_BACK, "unknown": DbOutcomeKind.UNKNOWN}[confirmation]
        assert outcomes[1].kind is expected, outcomes[1].error
        if confirmation != "saved":
            assert gate.hits == gate.rollbacks == 1
        assert tuple(world.owned.connection.iterdump()) == before
        assert len(world.reader.requests) == 1 and tuple(world.driver.calls) == before_calls
        assert results.calls == [1] and world.tools() == ["probe"]
        if confirmation != "saved":
            # 原责任在失败后仍可按原输入恢复，不依赖查看集合内容。
            monkeypatch.setattr(CaptureRepository, "finish_recording_results",
                lambda repository, command, operation_key, owned: original_save(repository, command, operation_key, owned))
            await world.replay(entrance, kind="completion")
            assert tuple(world.owned.connection.iterdump()) == before
    finally:
        await world.close()
        world.owned.connection.close()
