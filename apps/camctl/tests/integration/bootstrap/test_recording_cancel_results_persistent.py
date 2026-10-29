"""全冷正式会话从旧持久取消历史发现未结束的录像用途。"""

import pytest

from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptFinish, AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _NOW
from ..capture.test_record_media_result_settlement import _attempts
from ..support.bootstrap_consumers import recording_world, consumer_world, apply_cancellation, save_legacy_capture_cancel
from .test_read_default_consumers import _snapshot
from .test_recording_cancel_results_settlement import _original_results

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entrance", [
    "normal", "residual", "restricted", "normal-cancel", "restricted-cancel",
])
@pytest.mark.parametrize("late_result", [False, True], ids=["cancel-is-latest", "later-owned-result"])
async def test_default_discovers_canceled_recording_results_without_session_holder(
        pipeline, capture_resources, entrance, late_result):
    world = await recording_world(pipeline, capture_resources)
    owned = world.owned
    operations = OperationRepository()
    try:
        run_id, before_run = _original_results(owned)
        assert (before_run["status"], before_run["retry_wait_required"]) == (2, 1)
        if late_result:
            key = new_operation_key()
            started = await world.database.write(lambda current: operations.begin_attempt(AttemptIntent(
                operation="result", action_id=1, kind=OperationKind.CHECK_CAPTURE_RESULTS,
                target=AttemptTarget(activity_id=1), query_purpose=None,
                config=world.runtime.check_config, occurred_at=_NOW + 50), key, current), key=key)
            assert started.kind is DbOutcomeKind.COMPLETED, started.error
            assert started.value.disposition.value == "granted"
        await apply_cancellation(world, terminal=False)
        save_legacy_capture_cancel(world, occurred_at=_NOW + 100)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (6,)
        if late_result:
            ticket = started.value.ticket
            actual = validate_outcome(ticket, CallOutcome(
                status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
                settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
                observations=(DeviceObservation("result_files_listed", 1,
                    {"activity_id": "1", "entries": []}),)), world.entry.evidence)
            key = new_operation_key()
            saved = await world.database.write(lambda current: operations.finish_attempt(
                AttemptFinish(ticket, actual, _NOW + 200), key, current), key=key)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            assert owned.connection.execute(
                "SELECT e.event_type FROM actions a JOIN history_events e ON e.id=a.last_event_id WHERE a.id=1").fetchone() == (12,)
            _same, before_run = _original_results(owned)
        attempts = _attempts(owned)
        history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        action = _snapshot(owned, "actions", 1)
        rows = {table: owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("recording_processing", "device_files", "file_copies", "intermediate_files")}
        calls = tuple(world.driver.calls)
        await world.reconnect()
        # 全冷工厂拥有全新空会话集合，只从规范持久事实发现责任。
        cold = await consumer_world((world.owned, world.roots, world.source_id), capture_resources)
        try:
            cold.prime_clock()
            await cold.replay(entrance, kind="completion")
            assert _original_results(cold.owned) == (run_id, {**before_run, "status": 5, "retry_wait_required": 0})
            assert _snapshot(cold.owned, "actions", 1) == action and _attempts(cold.owned) == attempts
            assert cold.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
            for table, saved_rows in rows.items():
                assert cold.owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == saved_rows
            assert tuple(world.driver.calls) == calls and world.results.calls == [1]
            assert len(world.reader.requests) == len(world.ends) == 1 and world.tools() == []
            assert cold.driver.calls == [] and cold.reader.requests == [] and cold.results.calls == []
        finally:
            await cold.close()
    finally:
        await world.close()
