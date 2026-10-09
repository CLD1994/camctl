"""清理原键核实完整请求，不将改变的责任解释为原事务响应。"""

from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.work_files import (
    CleanupChecked, CleanupIntent, CleanupResultSave, RetentionRelease,
    WorkFileFailure, WorkFileOutcome,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionError
from camctl.history.validators import EventValidationError
from camctl.persistence.repositories import outputs

from .test_work_files import (
    _cancel_delivery, _proposal, _release_ok, _seed_action_candidate, _validate,
    local_read, read_targets, work_env,  # noqa: F401
)


@pytest.mark.parametrize("stage", ["release", "intent", "checked"])
def test_work_file_original_key_rejects_changed_file(work_env, stage):
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _seed_action_candidate(owned, roots, file_id=900)
    repository = OutputsRepository()
    if stage == "release":
        request = RetentionRelease(file_id, 1_736_935_210_000_000)
        save = repository.save_retention_release
    elif stage == "intent":
        _release_ok(owned, file_id)
        request = CleanupIntent(file_id, 1_736_935_210_000_000)
        save = repository.save_cleanup_intent
    else:
        request = CleanupChecked(file_id, 1_736_935_210_000_000)
        save = repository.save_cleanup_checked
    key = new_operation_key()
    original = save(request, key, owned)
    assert original.kind is DbOutcomeKind.COMPLETED, original.error
    before = tuple(owned.connection.iterdump())

    changed = save(replace(request, file_id=900), key, owned)

    assert changed.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(changed.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("advance_cursor", [False, True])
def test_same_cursor_original_key_preserves_explicit_cleanup_request(
        work_env, advance_cursor):
    owned, _roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, file_id)
    repository = OutputsRepository()
    checked = repository.save_cleanup_checked(
        CleanupChecked(file_id, 1_736_935_209_000_000), new_operation_key(), owned)
    assert checked.kind is DbOutcomeKind.COMPLETED, checked.error
    request = CleanupResultSave(file_id, WorkFileOutcome.COMPLETED, None,
                               1_736_935_210_000_000, advance_cursor)
    key = new_operation_key()
    original = repository.save_cleanup_result(request, key, owned)
    assert original.kind is DbOutcomeKind.COMPLETED, original.error
    stored = outputs.saved_transaction_events(owned.connection, key)
    assert stored[0]["body"]["evidence"] == {
        "cleanup_request": {"advance_cursor": advance_cursor}}
    assert len(stored) == 1, "位置已经相同，保存输入不产生重复游标事件"
    before = tuple(owned.connection.iterdump())

    assert repository.save_cleanup_result(request, key, owned).kind is DbOutcomeKind.COMPLETED
    changed = repository.save_cleanup_result(
        replace(request, advance_cursor=not advance_cursor), key, owned)

    assert changed.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(changed.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("evidence", [
    {}, {"cleanup_request": {}}, {"cleanup_request": {"advance_cursor": 0}},
    {"cleanup_request": {"advance_cursor": "false"}},
    {"cleanup_request": {"advance_cursor": False, "other": True}},
])
def test_cleanup_result_guard_requires_exact_boolean_request(work_env, evidence):
    owned, _roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, file_id)
    request = CleanupResultSave(file_id, WorkFileOutcome.COMPLETED, None,
                               1_736_935_210_000_000, False)
    plan = _proposal(owned, outputs._CleanupResultCommand(request, new_operation_key()))

    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(plan.events[0], evidence=evidence),))


@pytest.mark.parametrize("advance_cursor", [False, True])
@pytest.mark.parametrize("changed_field", ["file", "outcome", "error", "cursor"])
def test_work_file_result_original_key_rejects_changed_request(
        work_env, advance_cursor, changed_field):
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, file_id)
    _seed_action_candidate(owned, roots, file_id=900)
    repository = OutputsRepository()
    request = CleanupResultSave(
        file_id=file_id, outcome=WorkFileOutcome.FAILED,
        error=WorkFileFailure(
            "work_file_delete_failed", {"delivery_id": str(qualification.delivery_id)}),
        occurred_at=1_736_935_210_000_000, advance_cursor=advance_cursor)
    key = new_operation_key()
    original = repository.save_cleanup_result(request, key, owned)
    assert original.kind is DbOutcomeKind.COMPLETED, original.error
    before = tuple(owned.connection.iterdump())
    changes = {
        "file": {"file_id": 900},
        "outcome": {"outcome": WorkFileOutcome.COMPLETED, "error": None},
        "error": {"error": WorkFileFailure(
            "work_file_delete_failed", {"delivery_id": "900"})},
        "cursor": {"advance_cursor": not advance_cursor},
    }

    changed = repository.save_cleanup_result(
        replace(request, **changes[changed_field]), key, owned)

    assert changed.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(changed.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == before
