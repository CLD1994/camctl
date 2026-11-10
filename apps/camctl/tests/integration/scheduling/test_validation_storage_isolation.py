"""不同业务存储对象的规则在同库写入及连接重开后保持独立。"""

from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventValidationError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.scheduling import ObserveWindowRequest, SchedulingRepository
from camctl.persistence.repositories.validation import BusinessStorage
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_window_expiration import _accept_plan, _photo_body, _SCHEDULED, owned


@pytest.mark.asyncio
async def test_storage_rules_stay_independent_across_interleaved_writes_and_reopen(owned, tmp_path: Path):
    await _accept_plan(owned, tmp_path, _photo_body(count=3))
    rejection = EventValidationError("test-specific window rejection")

    def reject_window(event, context):
        raise rejection

    normal = SchedulingRepository(BusinessStorage())
    blocked = SchedulingRepository(BusinessStorage({"window": reject_window}))

    def observe(repository, connection, action_id):
        return repository.observe_window(
            ObserveWindowRequest(action_id, _SCHEDULED, _SCHEDULED), new_operation_key(), connection)

    failed = observe(blocked, owned, 1)
    assert failed.kind is DbOutcomeKind.ROLLED_BACK and failed.error is rejection
    assert observe(normal, owned, 1).kind is DbOutcomeKind.COMPLETED
    failed = observe(blocked, owned, 2)
    assert failed.kind is DbOutcomeKind.ROLLED_BACK and failed.error is rejection
    assert owned.connection.execute(
        "SELECT id,first_window_observed_at FROM actions ORDER BY id").fetchall() == [
            (1, _SCHEDULED), (2, None), (3, None)]
    owned.connection.close()

    reopened = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert observe(normal, reopened, 2).kind is DbOutcomeKind.COMPLETED
        failed = observe(blocked, reopened, 3)
        assert failed.kind is DbOutcomeKind.ROLLED_BACK and failed.error is rejection
        assert observe(normal, reopened, 3).kind is DbOutcomeKind.COMPLETED
        assert reopened.connection.execute(
            "SELECT id,first_window_observed_at FROM actions ORDER BY id").fetchall() == [
                (1, _SCHEDULED), (2, _SCHEDULED), (3, _SCHEDULED)]
    finally:
        reopened.connection.close()


@pytest.mark.asyncio
async def test_missing_rule_rejects_write_without_affecting_complete_storage(owned, tmp_path: Path):
    await _accept_plan(owned, tmp_path, _photo_body())
    incomplete = SchedulingRepository(BusinessStorage({"window": None}))
    normal = SchedulingRepository(BusinessStorage())
    request = ObserveWindowRequest(1, _SCHEDULED, _SCHEDULED)
    key = new_operation_key()
    rejected = incomplete.observe_window(request, key, owned)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, EventValidationError)
    assert "window" in str(rejected.error)
    assert owned.connection.execute(
        "SELECT first_window_observed_at FROM actions WHERE id=1").fetchone() == (None,)
    accepted = normal.observe_window(request, key, owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert owned.connection.execute(
        "SELECT first_window_observed_at FROM actions WHERE id=1").fetchone() == (_SCHEDULED,)
