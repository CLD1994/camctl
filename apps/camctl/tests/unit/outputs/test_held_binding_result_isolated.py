"""读取结束与绑定失败竞争，全部协作者使用进程内替身。"""

import asyncio
import sys
from dataclasses import dataclass
from enum import Enum
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from camctl.contracts import enums


def module(name, **members):
    value = ModuleType(name)
    value.__dict__.update(members)
    return value


class RunOutcome(Enum):
    FAILED = "failed"
    CANCELED = "canceled"


class RunStatus(Enum):
    FAILED = "failed"
    CANCELED = "canceled"


class AttemptStatus(Enum):
    SUCCEEDED = "succeeded"


class EffectState(Enum):
    UNKNOWN = "unknown"


class SettlementBasis(Enum):
    OBSERVED = "observed"
    NOT_DISPATCHED = "not_dispatched"


class Support(Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class DbOutcomeKind(Enum):
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class FinishDisposition(Enum):
    SAVED = "saved"
    ALREADY_ENDED = "already_ended"


class SharedSave:
    async def run(self, callback):
        return await callback()

    def retry_failed(self):
        pass


def value(*args, **kwargs):
    return SimpleNamespace(args=args, **kwargs)


@dataclass(frozen=True)
class attempt_finish:
    ticket: object
    outcome: object
    occurred_at: int
    run_finish: object = None


def run_finish(status, error=None):
    return SimpleNamespace(status=status, error=error)


def outcome(**kwargs):
    return SimpleNamespace(error=None, **kwargs)


def settlement(basis, evidence):
    return SimpleNamespace(basis=basis, evidence=evidence)


stub_modules = [
    module("camctl.capture.recovery", RecoveryBoundary=object, RecoveryBlockedReason=object, RecoveryDiagnostic=object),
    module("camctl.operations.attempts", AttemptFinish=attempt_finish, BeginDisposition=object,
           FinishDisposition=FinishDisposition, ReadResumeDisposition=object, ReadResumeRequest=object,
           RunFinish=run_finish, RunOutcome=RunOutcome, RunStatus=RunStatus),
    module("camctl.operations.models", AttemptStatus=AttemptStatus, AttemptTicket=object, CallOutcome=outcome,
           EffectState=EffectState, ErrorValue=value, EvidenceValue=value, Settlement=settlement,
           SettlementBasis=SettlementBasis),
    module("camctl.operations.validation", validate_outcome=lambda ticket, result, evidence: SimpleNamespace(outcome=result)),
    module("camctl.operations.error_diagnostics", exception_diagnostic=lambda error: None),
    module("camctl.persistence.models", DbOutcomeKind=DbOutcomeKind,
           db_failure=lambda cls, message, result: cls(message), db_outcome_for=object),
    module("camctl.persistence.business", SharedSave=SharedSave, settle_required=object),
    module("camctl.outputs.database_reads", read_one=object),
    module("camctl.devices.evidence", EvidenceError=type("EvidenceError", (Exception,), {})),
    module("camctl.logging_runtime.diagnostics", DiagnosticState=object, diagnostic_state_for=lambda error: None,
           record_call_outcome=lambda *args, **kwargs: None, record_error=lambda *args, **kwargs: None),
    module("camctl.outputs.copy", SourceChecksumSupport=Support),
    module("camctl.persistence.repositories.outputs", OutputsRepository=object),
    module("camctl.devices.bindings", binding_failure_details=lambda result: {"reason": "physical_identity_mismatch"}),
]

with (patch.object(enums, "resource_bytes", side_effect=AssertionError("禁止读取真实包资源")),
      patch.dict(sys.modules, {item.__name__: item for item in stub_modules})):
    from camctl.outputs import read_attempts


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("禁止读取真实包资源"))
    for item in stub_modules:
        monkeypatch.setitem(sys.modules, item.__name__, item)
    monkeypatch.setattr(read_attempts, "_record_read_business_error", lambda pending: None)


def setup(monkeypatch, status, canceled, *, digest_missing=True):
    ticket = SimpleNamespace(target_id="2", run_id=5, attempt_id=1)
    held = read_attempts.HeldReadEnd(ticket, SimpleNamespace(stopped=True, error=None), 20, evidence=object())
    facts = SimpleNamespace(committed_bytes=12, source_size=12, source_support=Support.SUPPORTED,
                            source_sha256=None if digest_missing else "digest")
    class Repository:
        def load_copy_state(self, copy_id, owned):
            return facts
    monkeypatch.setattr(sys.modules["camctl.persistence.repositories.outputs"], "OutputsRepository", Repository)
    monkeypatch.setattr(read_attempts, "running_read_ticket", lambda owned, copy_id: (ticket, 10))
    async def read_one(database, sql, params):
        return status, canceled
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
def test_clean_end_selects_terminal_then_cancel_then_binding(monkeypatch, status, canceled, expected):
    runtime, ticket, _ = setup(monkeypatch, status, canceled)
    business = SimpleNamespace(diagnostic=object())
    pending = asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), business))
    assert pending.finish.ticket is ticket
    assert pending.finish.outcome.outcome.status is AttemptStatus.SUCCEEDED
    assert pending.finish.occurred_at == (20 if status in (1, 2) else 91)
    assert (None if pending.finish.run_finish is None else pending.finish.run_finish.status) is expected
    assert pending.business is (business if expected is RunOutcome.FAILED else None)
    assert pending.preserved_run_terminal is (status not in (1, 2))


def test_determined_result_is_not_replaced_after_cancel(monkeypatch):
    runtime, ticket, _ = setup(monkeypatch, 2, 1)
    pending = read_attempts.PendingReadResult(SimpleNamespace(ticket=ticket), "original-key")
    runtime.pending_read_results[(5, 1)] = pending
    runtime.database.read = Mock(side_effect=AssertionError("已有结果不能重新判断"))
    assert asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), object())) is pending
    assert pending.key == "original-key"


def test_result_determined_during_read_takes_precedence(monkeypatch):
    runtime, ticket, _ = setup(monkeypatch, 2, 1)
    pending = read_attempts.PendingReadResult(SimpleNamespace(ticket=ticket), "original-key")
    async def read(callback):
        runtime.pending_read_results[(5, 1)] = pending
        return callback(object())
    runtime.database.read = read
    assert asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), object())) is pending


def test_cancel_observed_during_lookup_keeps_original_binding_candidate(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    async def canceled_during_lookup(database, sql, params):
        return 2, 1
    monkeypatch.setattr(read_attempts, "read_one", canceled_during_lookup)
    pending = asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), SimpleNamespace(diagnostic=object())))
    assert pending.finish.run_finish.status is RunOutcome.FAILED
    assert pending.business is not None


def test_completed_digest_leaves_local_work_untouched(monkeypatch):
    runtime, _, held = setup(monkeypatch, 2, 0, digest_missing=False)
    assert asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), object())) is None
    assert runtime.pending_read_ends[2] is held


def test_unconfirmed_clean_end_is_not_turned_into_success(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 1)
    runtime.pending_read_ends[2] = read_attempts.HeldReadEnd(
        runtime.pending_read_ends[2].ticket, SimpleNamespace(stopped=False, error=None), 20, evidence=object())
    with pytest.raises(ValueError):
        asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), object()))
    assert runtime.pending_read_results == {}


def test_late_cancel_does_not_replace_previously_determined_binding_finish(monkeypatch):
    runtime, ticket, _ = setup(monkeypatch, 2, 0)
    business = SimpleNamespace(diagnostic=object())
    pending = asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), business))
    again = asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), object()))
    assert again is pending
    assert pending.finish.run_finish.status is RunOutcome.FAILED
    assert runtime.pending_read_results[(ticket.run_id, ticket.attempt_id)] is pending


def test_unknown_save_keeps_original_finish_and_key_after_cancel(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    pending = asyncio.run(read_attempts.held_binding_read_result(
        runtime, 2, object(), SimpleNamespace(diagnostic=object())))
    saved = []
    class Operations:
        def finish_binding_read(self, finish, key, owned):
            saved.append((finish, key))
            return SimpleNamespace(kind=DbOutcomeKind.UNKNOWN if len(saved) == 1 else DbOutcomeKind.COMPLETED,
                                   error="unknown", value=SimpleNamespace(run_was_terminal=False,
                                                                          disposition=FinishDisposition.SAVED,
                                                                          run_status=RunStatus.FAILED))
    async def write(callback, *, key):
        return callback(object())
    runtime.operations = Operations()
    runtime.database.write = write
    with pytest.raises(ValueError):
        asyncio.run(read_attempts.save_read_result(runtime, pending))
    assert runtime.pending_read_results[(5, 1)] is pending
    assert asyncio.run(read_attempts.save_read_result(runtime, pending, retry=True)) is pending
    assert saved == [(pending.finish, pending.key), (pending.finish, pending.key)]


@pytest.mark.parametrize("terminal,disposition,preserved", [
    (True, FinishDisposition.SAVED, True),
    (False, FinishDisposition.ALREADY_ENDED, True),
    (False, FinishDisposition.SAVED, False),
])
def test_save_receipt_controls_terminal_handling(monkeypatch, terminal, disposition, preserved):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    pending = asyncio.run(read_attempts.held_binding_read_result(
        runtime, 2, object(), SimpleNamespace(diagnostic=object())))
    class Operations:
        def finish_binding_read(self, finish, key, owned):
            return SimpleNamespace(kind=DbOutcomeKind.COMPLETED,
                                   value=SimpleNamespace(run_was_terminal=terminal, disposition=disposition,
                                                         run_status=RunStatus.FAILED))
    async def write(callback, *, key):
        return callback(object())
    runtime.operations = Operations()
    runtime.database.write = write
    result = asyncio.run(read_attempts.save_read_result(runtime, pending))
    assert result.preserved_run_terminal is preserved
    assert result.finish is pending.finish and result.key == pending.key
    assert runtime.pending_read_results[(5, 1)] is result


def test_cancel_transaction_receipt_retires_binding_business(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    business = SimpleNamespace(diagnostic=object())
    pending = asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), business))
    class Operations:
        def finish_binding_read(self, finish, key, owned):
            return SimpleNamespace(kind=DbOutcomeKind.COMPLETED, value=SimpleNamespace(
                run_was_terminal=False, disposition=FinishDisposition.SAVED, run_status=RunStatus.CANCELED))
    async def write(callback, *, key):
        return callback(object())
    runtime.operations, runtime.database.write = Operations(), write
    result = asyncio.run(read_attempts.save_read_result(runtime, pending))
    assert result.finish.run_finish.status is RunOutcome.CANCELED
    assert result.finish.outcome is pending.finish.outcome
    assert result.key == pending.key and result.business is None
    assert runtime.pending_read_results[(5, 1)] is result


@pytest.mark.parametrize("capture", [False, True])
def test_binding_business_retains_original_read_result_key(monkeypatch, capture):
    @dataclass(frozen=True)
    class DeliveryRequest:
        read_result_key: object = None
    @dataclass(frozen=True)
    class CaptureRequest:
        read_result_key: object = None
        read_copy_id: object = None
    output = sys.modules["camctl.persistence.repositories.outputs"]
    monkeypatch.setattr(output, "FailReadBinding", DeliveryRequest, raising=False)
    capture_module = module("camctl.persistence.repositories.capture", FinishBindingFailure=CaptureRequest)
    monkeypatch.setitem(sys.modules, capture_module.__name__, capture_module)
    runtime, _, _ = setup(monkeypatch, 2, 0)
    business = read_attempts.PendingReadBusiness(object(), CaptureRequest() if capture else DeliveryRequest(), "business-key")
    pending = asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), business))
    assert pending.business.key == "business-key"
    assert pending.business.request.read_result_key == pending.key
    if capture:
        assert pending.business.request.read_copy_id == 2


def test_unknown_then_canceled_receipt_reuses_original_candidate_and_key(monkeypatch):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    pending = asyncio.run(read_attempts.held_binding_read_result(
        runtime, 2, object(), SimpleNamespace(diagnostic=object())))
    calls = []
    class Operations:
        def finish_binding_read(self, finish, key, owned):
            calls.append((finish, key))
            if len(calls) == 1:
                return SimpleNamespace(kind=DbOutcomeKind.UNKNOWN, error="unknown")
            return SimpleNamespace(kind=DbOutcomeKind.COMPLETED, value=SimpleNamespace(
                run_was_terminal=False, disposition=FinishDisposition.SAVED, run_status=RunStatus.CANCELED))
    async def write(callback, *, key):
        return callback(object())
    runtime.operations, runtime.database.write = Operations(), write
    with pytest.raises(ValueError):
        asyncio.run(read_attempts.save_read_result(runtime, pending))
    result = asyncio.run(read_attempts.save_read_result(runtime, pending, retry=True))
    assert calls == [(pending.finish, pending.key), (pending.finish, pending.key)]
    assert result.business is None and result.finish.run_finish.status is RunOutcome.CANCELED


@pytest.mark.parametrize("status,recorded", [(RunStatus.FAILED, True), (RunStatus.CANCELED, False)])
def test_completed_receipt_survives_control_error_and_records_only_selected_failure(monkeypatch, status, recorded):
    runtime, _, _ = setup(monkeypatch, 2, 0)
    business = SimpleNamespace(diagnostic=object())
    pending = asyncio.run(read_attempts.held_binding_read_result(runtime, 2, object(), business))
    receipt = SimpleNamespace(kind=DbOutcomeKind.COMPLETED, value=SimpleNamespace(
        run_was_terminal=False, disposition=FinishDisposition.SAVED, run_status=status))
    control = KeyboardInterrupt("after commit")
    async def write(callback, *, key):
        raise control
    runtime.database.write = write
    monkeypatch.setattr(read_attempts, "db_outcome_for", lambda error: receipt)
    errors = []
    monkeypatch.setattr(read_attempts, "_record_read_business_error", errors.append)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(read_attempts.save_read_result(runtime, pending))
    held = runtime.pending_read_results[(5, 1)]
    assert held.key == pending.key
    assert (held.business is business) is recorded
    assert errors == ([business] if recorded else [])


def test_business_held_before_diagnostic_control_error(monkeypatch):
    runtime = SimpleNamespace(pending_read_business={})
    business = SimpleNamespace(diagnostic=object())
    def fail_diagnostic(pending):
        raise KeyboardInterrupt("diagnostic interruption")
    monkeypatch.setattr(read_attempts, "_record_read_business_error", fail_diagnostic)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(read_attempts.save_prepared_read_business(runtime, 2, "read_binding_failure", business))
    assert runtime.pending_read_business[(2, "read_binding_failure")] is business
