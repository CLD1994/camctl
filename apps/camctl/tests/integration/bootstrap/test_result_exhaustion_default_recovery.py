"""默认入口先核实有限 RESULTS 原收场申请，再取得业务资格。"""

import json

import pytest

from camctl.persistence.models import DbOutcomeKind

from ..support.bootstrap_result_recovery import (
    ENTRANCES, StorageGate, held_result_close, original_input_and_external_facts, reliably_closed,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("consumer", ["photo", "timelapse"])
@pytest.mark.parametrize("after", [False, True], ids=["commit-before", "commit-after"])
async def test_default_prefix_confirms_original_exhaustion_before_candidates(
        tmp_path, monkeypatch, capture_resources, entrance, consumer, after):
    async with held_result_close(tmp_path, capture_resources, monkeypatch, consumer, after) as world:
        def check():
            reliably_closed(world)
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?",
                (world.action_id,)).fetchone() == (2,)
            capture, error = world.owned.connection.execute(
                "SELECT capture_json,last_error_json FROM device_activities WHERE id=?",
                (world.activity_id,)).fetchone()
            assert json.loads(capture) == world.request.capture
            assert json.loads(error) == world.request.error
            if after:
                assert tuple(world.owned.connection.iterdump()) == world.before

        await world.replay_close(entrance, check)
        check()


@pytest.mark.parametrize("entrance", ENTRANCES)
@pytest.mark.parametrize("confirmation", ["unknown", "rolled-back"])
async def test_default_prefix_retains_original_exhaustion_when_confirmation_fails(
        tmp_path, monkeypatch, capture_resources, entrance, confirmation):
    async with held_result_close(tmp_path, capture_resources, monkeypatch, "photo", True) as world:
        world.gate = StorageGate(original_key=True, unknown=confirmation == "unknown")
        await world.replay_close(entrance, lambda: pytest.fail("原申请未可靠核实不得取得业务候选"), failure=True)
        original_input_and_external_facts(world)
        assert world.gate.hits == 1
        expected = DbOutcomeKind.UNKNOWN if confirmation == "unknown" else DbOutcomeKind.ROLLED_BACK
        assert world.receipts[1].kind is expected
        assert tuple(world.owned.connection.iterdump()) == world.before
