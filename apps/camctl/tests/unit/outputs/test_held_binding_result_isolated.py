"""读取结束与绑定失败竞争，隔离存储和设备协作者。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from camctl.capture.media import RecordingFailure
from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding, binding_failure_details
from camctl.devices.read_session import ReadEnd
from camctl.operations.attempts import FinishAttemptResult, FinishDisposition, RunOutcome, RunStatus
from camctl.operations.models import AttemptStatus, AttemptTicket
from camctl.outputs import read_attempts
from camctl.outputs.copy import SourceChecksumSupport as Support
from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.capture import FinishBindingFailure
from camctl.persistence.repositories.outputs import FailReadBinding

pytestmark = pytest.mark.asyncio
MISSING_BINDING = BindingResult(BindingStatus.DEVICE_MISSING, DeviceBinding("cam-1", "driver"), None)


def completed(*, status=RunStatus.FAILED, disposition=FinishDisposition.SAVED, terminal=False):
    return DbOutcome(DbOutcomeKind.COMPLETED,
        FinishAttemptResult(disposition, AttemptStatus.SUCCEEDED, status, terminal))


class ImmediateSave:
    """在本用例中同步交付原控制异常；其他用例使用真实共享保存。"""
    async def run(self, callback):
        return await callback()

    def retry_failed(self):
        pass


@pytest.fixture(autouse=True)
def isolate_diagnostics(monkeypatch):
    monkeypatch.setattr(read_attempts, "_record_read_business_error", lambda pending: None)
    monkeypatch.setattr(read_attempts, "record_error", lambda *args, **kwargs: None)


def setup(monkeypatch, status, canceled, *, digest_missing=True):
    ticket = AttemptTicket(1, "read", "2", "read/2", 5)
    held = read_attempts.HeldReadEnd(ticket, ReadEnd(True, 12, None), 20, evidence=_LOCAL_READ_EVIDENCE)
    facts = SimpleNamespace(committed_bytes=12, source_size=12, source_support=Support.SUPPORTED,
                            source_sha256=None if digest_missing else "a" * 64,
                            target_sha256="a" * 64, verification_state=1)
    class Repository:
        def load_copy_state(self, copy_id, owned):
            return facts
    monkeypatch.setattr(outputs, "OutputsRepository", Repository)
    monkeypatch.setattr(read_attempts, "running_read_ticket", lambda owned, copy_id: (ticket, 10))
    async def read_one(database, sql, params):
        return status, canceled, None, 3, None
    monkeypatch.setattr(read_attempts, "read_one", read_one)
    class Database:
        async def read(self, callback):
            return callback(object())
    runtime = SimpleNamespace(database=Database(), pending_read_ends={2: held},
                              pending_read_results={}, occurred_at=lambda: 91)
    return runtime, ticket, held


@pytest.mark.parametrize("status,canceled,expected", [
    (1, 0, RunOutcome.FAILED), (2, 1, RunOutcome.FAILED),
    (3, 1, None), (4, 0, None), (5, 1, None), (6, 0, None), (7, 1, None),
])
async def test_clean_end_selects_terminal_then_cancel_then_binding(monkeypatch, status, canceled, expected):
    runtime, ticket, _ = setup(monkeypatch, status, canceled)
    business = SimpleNamespace(diagnostic=object())
    pending = await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, business)
    assert pending.finish.ticket is ticket
    assert pending.finish.outcome.outcome.status is AttemptStatus.SUCCEEDED
    assert pending.finish.occurred_at == (20 if status in (1, 2) else 91)
    assert (None if pending.finish.run_finish is None else pending.finish.run_finish.status) is expected
    assert pending.business is (business if expected is RunOutcome.FAILED else None)
    assert pending.preserved_run_terminal is (status not in (1, 2))


async def test_inspection_binding_result_keeps_original_read_without_capture_business(monkeypatch):
    runtime, ticket, _ = setup(monkeypatch, 2, 0)
    async def inspection_owner(database, sql, params): return 2, 0, 8, None, None
    monkeypatch.setattr(read_attempts, "read_one", inspection_owner)
    recorded = []
    monkeypatch.setattr(read_attempts, "record_error", lambda error, **kwargs: recorded.append(error))
    pending = await read_attempts.held_binding_read_result(
        runtime, 2, MISSING_BINDING, SimpleNamespace(diagnostic=object()))
    assert pending.finish.ticket is ticket
    assert pending.finish.run_finish.status is RunOutcome.FAILED
    assert pending.binding_read is True
    assert pending.business is None
    assert len(recorded) == 1


async def test_determined_result_is_not_replaced_after_cancel(monkeypatch):
    runtime, ticket, _ = setup(monkeypatch, 2, 1)
    pending = read_attempts.PendingReadResult(SimpleNamespace(ticket=ticket), "original-key")
    runtime.pending_read_results[(5, 1)] = pending
    runtime.database.read = Mock(side_effect=AssertionError("已有结果不能重新判断"))
    assert await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, object()) is pending
    assert pending.key == "original-key"


@pytest.mark.parametrize("canceled", [0, 1])
async def test_missing_configuration_with_saved_mismatch_keeps_binding_cancel_arbitration(monkeypatch, canceled):
    runtime, ticket, _ = setup(monkeypatch, 2, canceled, digest_missing=False)
    facts = SimpleNamespace(committed_bytes=12, source_size=12, source_support=Support.SUPPORTED,
        source_sha256="a" * 64, target_sha256="b" * 64, verification_state=4)
    class Repository:
        def load_copy_state(self, copy_id, owned): return facts
    monkeypatch.setattr(outputs, "OutputsRepository", Repository)
    binding = MISSING_BINDING
    pending = await read_attempts.held_binding_read_result(runtime, 2, binding, SimpleNamespace(diagnostic=object()))
    assert pending.finish.ticket is ticket
    assert pending.finish.outcome.outcome.status is AttemptStatus.SUCCEEDED
    assert pending.finish.run_finish.status is RunOutcome.FAILED
    assert pending.finish.run_finish.error.details["reason"] == "missing"
    assert pending.binding_read is True


async def test_result_determined_during_read_takes_precedence(monkeypatch):
    runtime, ticket, _ = setup(monkeypatch, 2, 1)
    pending = read_attempts.PendingReadResult(SimpleNamespace(ticket=ticket), "original-key")
    async def read(callback):
        runtime.pending_read_results[(5, 1)] = pending
        return callback(object())
    runtime.database.read = read
    assert await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, object()) is pending


async def test_cancel_observed_during_lookup_keeps_original_binding_candidate(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    async def canceled_during_lookup(database, sql, params):
        return 2, 1, None, 3, None
    monkeypatch.setattr(read_attempts, "read_one", canceled_during_lookup)
    pending = await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, SimpleNamespace(diagnostic=object()))
    assert pending.finish.run_finish.status is RunOutcome.FAILED
    assert pending.business is not None


async def test_completed_digest_leaves_local_work_untouched(monkeypatch):
    runtime, _, held = setup(monkeypatch, 2, 0, digest_missing=False)
    assert await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, object()) is None
    assert runtime.pending_read_ends[2] is held


async def test_unconfirmed_clean_end_is_not_turned_into_success(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 1)
    runtime.pending_read_ends[2] = read_attempts.HeldReadEnd(
        runtime.pending_read_ends[2].ticket, ReadEnd(False, 12, None), 20, evidence=_LOCAL_READ_EVIDENCE)
    with pytest.raises(ValueError):
        await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, object())
    assert runtime.pending_read_results == {}


async def test_late_cancel_does_not_replace_previously_determined_binding_finish(monkeypatch):
    runtime, ticket, _ = setup(monkeypatch, 2, 0)
    business = SimpleNamespace(diagnostic=object())
    pending = await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, business)
    again = await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, object())
    assert again is pending
    assert pending.finish.run_finish.status is RunOutcome.FAILED
    assert runtime.pending_read_results[(ticket.run_id, ticket.attempt_id)] is pending


async def test_unknown_save_keeps_original_finish_and_key_after_cancel(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    pending = await read_attempts.held_binding_read_result(
        runtime, 2, MISSING_BINDING, SimpleNamespace(diagnostic=object()))
    saved = []
    class Operations:
        def finish_binding_read(self, finish, key, owned, *, current_identity=None):
            saved.append((finish, key))
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("unknown")) if len(saved) == 1 else completed()
    async def write(callback, *, key):
        return callback(object())
    runtime.operations = Operations()
    runtime.database.write = write
    with pytest.raises(ValueError):
        await read_attempts.save_read_result(runtime, pending)
    assert runtime.pending_read_results[(5, 1)] is pending
    assert await read_attempts.save_read_result(runtime, pending, retry=True) is pending
    assert saved == [(pending.finish, pending.key), (pending.finish, pending.key)]


@pytest.mark.parametrize("terminal,disposition,preserved", [
    (True, FinishDisposition.SAVED, True),
    (False, FinishDisposition.ALREADY_ENDED, True),
    (False, FinishDisposition.SAVED, False),
])
async def test_save_receipt_controls_terminal_handling(monkeypatch, terminal, disposition, preserved):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    pending = await read_attempts.held_binding_read_result(
        runtime, 2, MISSING_BINDING, SimpleNamespace(diagnostic=object()))
    class Operations:
        def finish_binding_read(self, finish, key, owned, *, current_identity=None):
            return completed(terminal=terminal, disposition=disposition)
    async def write(callback, *, key):
        return callback(object())
    runtime.operations = Operations()
    runtime.database.write = write
    result = await read_attempts.save_read_result(runtime, pending)
    assert result.preserved_run_terminal is preserved
    assert result.finish is pending.finish and result.key == pending.key
    assert runtime.pending_read_results[(5, 1)] is result


async def test_cancel_transaction_receipt_retires_binding_business(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    business = SimpleNamespace(diagnostic=object())
    pending = await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, business)
    class Operations:
        def finish_binding_read(self, finish, key, owned, *, current_identity=None):
            return completed(status=RunStatus.CANCELED)
    async def write(callback, *, key):
        return callback(object())
    runtime.operations, runtime.database.write = Operations(), write
    result = await read_attempts.save_read_result(runtime, pending)
    assert result.finish.run_finish.status is RunOutcome.CANCELED
    assert result.finish.outcome is pending.finish.outcome
    assert result.key == pending.key and result.business is None
    assert runtime.pending_read_results[(5, 1)] is result


@pytest.mark.parametrize("capture", [False, True])
async def test_binding_business_retains_original_read_result_key(monkeypatch, capture):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    if capture:
        request = FinishBindingFailure(7, 20, RecordingFailure("device_binding_unavailable",
            binding_failure_details(MISSING_BINDING)))
    else:
        request = FailReadBinding(2, 9, MISSING_BINDING, 20)
    business = read_attempts.PendingReadBusiness(lambda *args: None, request, "b" * 32)
    pending = await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, business)
    assert pending.business.key == "b" * 32
    assert pending.business.request.read_result_key == pending.key
    if capture:
        assert pending.business.request.read_copy_id == 2


async def test_unknown_then_canceled_receipt_reuses_original_candidate_and_key(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    pending = await read_attempts.held_binding_read_result(
        runtime, 2, MISSING_BINDING, SimpleNamespace(diagnostic=object()))
    calls = []
    class Operations:
        def finish_binding_read(self, finish, key, owned, *, current_identity=None):
            calls.append((finish, key))
            if len(calls) == 1:
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("unknown"))
            return completed(status=RunStatus.CANCELED)
    async def write(callback, *, key):
        return callback(object())
    runtime.operations, runtime.database.write = Operations(), write
    with pytest.raises(ValueError):
        await read_attempts.save_read_result(runtime, pending)
    result = await read_attempts.save_read_result(runtime, pending, retry=True)
    assert calls == [(pending.finish, pending.key), (pending.finish, pending.key)]
    assert result.business is None and result.finish.run_finish.status is RunOutcome.CANCELED


@pytest.mark.parametrize("status,recorded", [(RunStatus.FAILED, True), (RunStatus.CANCELED, False)])
async def test_completed_receipt_survives_control_error_and_records_only_selected_failure(monkeypatch, status, recorded):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    business = SimpleNamespace(diagnostic=object())
    pending = await read_attempts.held_binding_read_result(runtime, 2, MISSING_BINDING, business)
    receipt = completed(status=status)
    object.__setattr__(pending, "result_save", ImmediateSave())
    control = KeyboardInterrupt("after commit")
    async def write(callback, *, key):
        raise control
    runtime.database.write = write
    monkeypatch.setattr(read_attempts, "db_outcome_for", lambda error: receipt)
    errors = []
    monkeypatch.setattr(read_attempts, "_record_read_business_error", errors.append)
    with pytest.raises(KeyboardInterrupt):
        await read_attempts.save_read_result(runtime, pending)
    held = runtime.pending_read_results[(5, 1)]
    assert held.key == pending.key
    assert held.business is business and held.finish is pending.finish
    assert errors == ([business] if recorded else [])


async def test_canceled_control_error_retries_original_candidate(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    pending = await read_attempts.held_binding_read_result(
        runtime, 2, MISSING_BINDING, SimpleNamespace(diagnostic=object()))
    receipt = completed(status=RunStatus.CANCELED)
    object.__setattr__(pending, "result_save", ImmediateSave())
    calls = []
    class Operations:
        def finish_binding_read(self, finish, key, owned, *, current_identity=None):
            calls.append((finish, key))
            return receipt
    async def write(callback, *, key):
        result = callback(object())
        if len(calls) == 1:
            raise KeyboardInterrupt("after commit")
        return result
    runtime.operations, runtime.database.write = Operations(), write
    monkeypatch.setattr(read_attempts, "db_outcome_for", lambda error: receipt)
    with pytest.raises(KeyboardInterrupt):
        await read_attempts.save_read_result(runtime, pending)
    held = runtime.pending_read_results[(5, 1)]
    result = await read_attempts.save_read_result(runtime, held, retry=True)
    assert calls == [(pending.finish, pending.key), (pending.finish, pending.key)]
    assert result.business is None and result.finish.run_finish.status is RunOutcome.CANCELED


async def test_business_held_before_diagnostic_control_error(monkeypatch):
    from camctl.contracts.values import new_operation_key

    runtime = SimpleNamespace(pending_read_business={})
    business = read_attempts.PendingReadBusiness(lambda *args: None, object(), new_operation_key())
    def fail_diagnostic(pending):
        raise KeyboardInterrupt("diagnostic interruption")
    monkeypatch.setattr(read_attempts, "_record_read_business_error", fail_diagnostic)
    with pytest.raises(KeyboardInterrupt):
        await read_attempts.save_prepared_read_business(runtime, 2, "read_binding_failure", business)
    assert runtime.pending_read_business[(2, "read_binding_failure")] is business
