"""录像耗尽先核原流程申请；新会话继续消费耐久 UNCONFIRMED 事实。"""

import json
from types import SimpleNamespace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.models import ResultRunClose
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..support.bootstrap_recovery import DefaultRecoveryWorld
from ..support.bootstrap_result_recovery import (
    ENTRANCES, StorageGate, held_result_close, original_input_and_external_facts, reliably_closed,
)

pytestmark = pytest.mark.asyncio


def _closed(world):
    events = reliably_closed(world)
    assert isinstance(world.request, ResultRunClose)
    assert world.receipts[1].value is None
    assert len(events) == 1
    values = events[0]["body"]["rows"][0]["after"]["values"]
    assert values["status"] == 6 and values["retry_wait_required"] == 0
    assert json.loads(world.owned.connection.execute("SELECT error_json FROM operation_runs WHERE id=?",
        (world.run_id,)).fetchone()[0]) == {"code": "capture_result_unconfirmed", "stage": "execution",
            "details": {"activity_id": str(world.activity_id), "reason": "outputs_unknown"}}
    assert world.owned.connection.execute(
        "SELECT capture_json,last_error_json,result_check_json FROM device_activities WHERE id=?",
        (world.activity_id,)).fetchone() == (None, None, None)


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("after", [False, True], ids=["commit-before", "commit-after"])
async def test_default_prefix_confirms_original_record_run_close_before_candidates(
        tmp_path, monkeypatch, capture_resources, entrance, after):
    async with held_result_close(tmp_path, capture_resources, monkeypatch, "record", after) as world:
        def check():
            _closed(world)
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?",
                (world.action_id,)).fetchone() == (2,)
            if after:
                assert tuple(world.owned.connection.iterdump()) == world.before

        await world.replay_close(entrance, check)
        check()


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("confirmation", ["unknown", "rolled-back"])
async def test_default_prefix_blocks_candidates_while_original_record_run_close_unreliable(
        tmp_path, monkeypatch, capture_resources, entrance, confirmation):
    async with held_result_close(tmp_path, capture_resources, monkeypatch, "record", True) as world:
        world.gate = StorageGate(original_key=True, unknown=confirmation == "unknown")
        await world.replay_close(entrance, lambda: pytest.fail("原申请未可靠核实不得取得业务候选"), failure=True)
        original_input_and_external_facts(world)
        assert world.gate.hits == 1
        expected = DbOutcomeKind.UNKNOWN if confirmation == "unknown" else DbOutcomeKind.ROLLED_BACK
        assert world.receipts[1].kind is expected
        assert tuple(world.owned.connection.iterdump()) == world.before


@pytest.mark.parametrize("after", [False, True], ids=["commit-before", "commit-after"])
async def test_record_handler_resends_original_run_close_before_business_finish(
        tmp_path, monkeypatch, capture_resources, after):
    async with held_result_close(tmp_path, capture_resources, monkeypatch, "record", after) as world:
        actual_finish = CaptureRepository.finish_capture
        finishes = []

        def finish(repository, request, key, current):
            assert world.inputs == [(world.request, world.key)] * 2
            assert world.receipts[1].kind is DbOutcomeKind.COMPLETED
            assert current.connection.execute("SELECT status FROM actions WHERE id=?",
                (world.action_id,)).fetchone() == (2,)
            assert saved_transaction_events(current.connection, world.key) is not None
            finishes.append((request, key))
            return actual_finish(repository, request, key, current)

        monkeypatch.setattr(CaptureRepository, "finish_capture", finish)
        runtime = await world.result_producer(world.driver)
        await capture_handler(world.handler)(world.action_id, runtime)
        assert len(finishes) == 1
        _closed(world)
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?",
            (world.action_id,)).fetchone() == (4,)


@pytest.mark.parametrize("with_file", [False, True], ids=["empty", "complete-other"])
async def test_reopened_record_without_session_request_consumes_durable_unconfirmed_run(
        tmp_path, monkeypatch, capture_resources, with_file):
    async with held_result_close(tmp_path, capture_resources, monkeypatch, "record", True,
                                 with_file=with_file) as world:
        source = SimpleNamespace(path=world.path, metadata=world.metadata,
            action_id=world.action_id, formed_at=world.formed_at + 2_000_000)
        cold = DefaultRecoveryWorld(source, capture_resources, monkeypatch)
        try:
            runtime = await cold.producer()
            await capture_handler(world.handler)(world.action_id, runtime)
            assert len(world.inputs) == 1, "新会话只消费耐久收场事实，不重造原键申请"
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?",
                (world.action_id,)).fetchone() == (4,)
            assert world.owned.connection.execute("SELECT status,attempts_used,retry_wait_required"
                " FROM operation_runs WHERE id=?", (world.run_id,)).fetchone() == (6, 1, 0)
            assert saved_transaction_events(world.owned.connection, world.key)[0]["occurred_at"] == world.formed_at
            assert len(world.driver.controls) == len(world.driver.stops) == len(world.driver.results) == 1
            for method in cold.methods:
                method.assert_not_called()
        finally:
            cold.close()
