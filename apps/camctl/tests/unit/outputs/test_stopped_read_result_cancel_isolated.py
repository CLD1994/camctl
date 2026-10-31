"""实际读取停止后，原观察及可靠保存身份的取消收场。"""

import asyncio
import importlib.util
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


class AttemptStatus(Enum):
    SUCCEEDED = 'succeeded'
    UNKNOWN = 'unknown'
    FAILED = 'failed'


class EffectState(Enum):
    UNKNOWN = 'unknown'
    NO_EFFECT = 'no_effect'


class SettlementBasis(Enum):
    OBSERVED = 'observed'
    NOT_DISPATCHED = 'not_dispatched'


class RunOutcome(Enum):
    CANCELED = 'canceled'


class DbOutcomeKind(Enum):
    COMPLETED = 'completed'
    UNKNOWN = 'unknown'


class FinishDisposition(Enum):
    SAVED = 'saved'
    ALREADY_ENDED = 'already_ended'


class InputPhase(Enum):
    OWNER_SKIPPED = 'owner_skipped'
    SEGMENT_FAILED = 'segment_failed'
    INPUT_READY = 'input_ready'


@dataclass(frozen=True)
class InputStep:
    phase: InputPhase
    error: BaseException | None = None
    source_failed: bool = False
    read_end: object = None
    stop_requested: bool = False
    content_complete: bool = False


@dataclass(frozen=True)
class AttemptFinish:
    ticket: object
    outcome: object
    occurred_at: int
    run_finish: object = None
    retry_wait: bool = False


class SharedSave:
    """保存失败后只有明确 retry 才可重送，成功结果供后续等待者复用。"""
    def __init__(self): self.result = self.error = None; self.done = False
    async def run(self, callback):
        if not self.done:
            try: self.result = await callback()
            except BaseException as error: self.error = error
            self.done = True
        if self.error is not None: raise self.error
        return self.result
    def retry_failed(self):
        if self.error is not None: self.done = False; self.error = None


@pytest.fixture
def subject(monkeypatch):
    class ConsistencyError(ValueError): pass
    class CopySegmentError(RuntimeError): pass
    class SegmentFailure(Enum): STOPPED = 'stopped'
    def error_value(code, domain): return NS(code=code, domain=domain)
    def call_outcome(**kwargs): return NS(**kwargs)
    def finish(status): return NS(status=status, error=None)
    modules = [
        module('camctl.capture.recovery', RecoveryBoundary=object, RecoveryBlockedReason=object, RecoveryDiagnostic=object),
        module('camctl.capture.input_copy', InputPhase=InputPhase, InputStep=InputStep),
        module('camctl.contracts.values', ConsistencyError=ConsistencyError, OperationKey=str, new_operation_key=lambda: 'original-key'),
        module('camctl.operations.attempts', AttemptFinish=AttemptFinish, BeginDisposition=object,
               FinishDisposition=FinishDisposition, ReadResumeDisposition=object, ReadResumeRequest=object,
               RunFinish=finish, RunOutcome=RunOutcome, RunStatus=object),
        module('camctl.operations.models', AttemptStatus=AttemptStatus, AttemptTicket=object,
               CallOutcome=call_outcome, EffectState=EffectState, ErrorValue=error_value,
               EvidenceValue=lambda *args: NS(args=args), Settlement=lambda basis, evidence: NS(basis=basis, evidence=evidence),
               SettlementBasis=SettlementBasis),
        module('camctl.operations.validation', validate_outcome=lambda ticket, outcome, evidence: NS(outcome=outcome)),
        module('camctl.operations.error_diagnostics', exception_diagnostic=lambda error: None),
        module('camctl.persistence.models', DbOutcomeKind=DbOutcomeKind,
               db_failure=lambda cls, message, outcome: cls(message), db_outcome_for=lambda error: None),
        module('camctl.persistence.business', SharedSave=SharedSave, settle_required=object),
        module('camctl.outputs.database_reads', read_one=object),
        module('camctl.devices.evidence', EvidenceError=type('EvidenceError', (Exception,), {}), EvidenceContract=object),
        module('camctl.logging_runtime.diagnostics', DiagnosticState=type('DiagnosticState', (), {}),
               diagnostic_state_for=lambda error: None, record_call_outcome=lambda *args, **kwargs: None,
               record_error=lambda *args, **kwargs: None),
        module('camctl.outputs.copy', CopySegmentError=CopySegmentError),
        module('camctl.host_files.segments', SegmentFailure=SegmentFailure),
    ]
    for item in modules: monkeypatch.setitem(sys.modules, item.__name__, item)
    name = '_isolated_cancel_read_attempts'
    path = Path(__file__).parents[3] / 'src/camctl/outputs/read_attempts.py'
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


class Database:
    def __init__(self, outcomes): self.outcomes, self.saved = list(outcomes), []
    async def write(self, callback, *, key):
        finish, actual_key = callback(object())
        self.saved.append((finish, key, actual_key))
        kind = self.outcomes.pop(0)
        result = NS(run_was_terminal=False, disposition=FinishDisposition.SAVED)
        return NS(kind=kind, value=result if kind is DbOutcomeKind.COMPLETED else None, error=None)


def setup(subject, monkeypatch, canceled, outcomes=(DbOutcomeKind.COMPLETED,)):
    ticket = NS(target_id='9', run_id=4, attempt_id=1)
    database = Database(outcomes)
    runtime = NS(database=database, pending_read_results={}, continuing_read_tickets={},
        pending_read_ends={}, operations=NS(finish_attempt=lambda finish, key, owned: (finish, key)),
        occurred_at=lambda: 999)
    async def read_one(database, sql, params):
        assert runtime.pending_read_results[(4, 1)].ticket is ticket
        assert params == (4, 9)
        return canceled, 10
    monkeypatch.setattr(subject, 'read_one', read_one)
    return runtime, ticket


def step(*, complete, error, phase=InputPhase.OWNER_SKIPPED, source_failed=False, exception=None):
    return InputStep(phase, error=exception, source_failed=source_failed,
                     read_end=NS(stopped=True, error=error), stop_requested=True,
                     content_complete=complete)


@pytest.mark.parametrize('complete,end_error,status,code', [
    (True, None, AttemptStatus.SUCCEEDED, None),
    (False, 'stopped', AttemptStatus.UNKNOWN, 'read_stopped'),
    (False, None, AttemptStatus.UNKNOWN, 'read_stopped'),
])
def test_cancel_keeps_actual_read_outcome(subject, monkeypatch, complete, end_error, status, code):
    runtime, ticket = setup(subject, monkeypatch, 1)
    original = step(complete=complete, error=end_error)
    pending = subject.stopped_read_result(runtime, ticket, original, object(), 42)
    assert pending is runtime.pending_read_results[(4, 1)]
    prepared = asyncio.run(subject.save_read_result(runtime, pending))
    outcome = prepared.finish.outcome.outcome
    assert (outcome.status, None if outcome.error is None else outcome.error.code) == (status, code)
    assert prepared.finish.run_finish.status is RunOutcome.CANCELED
    assert prepared.finish.ticket is ticket and prepared.finish.occurred_at == 42
    assert runtime.database.saved == [(prepared.finish, 'original-key', 'original-key')]


@pytest.mark.parametrize('complete,end_error,expected', [
    (False, 'stopped', AttemptStatus.UNKNOWN),
    (True, None, AttemptStatus.SUCCEEDED),
])
def test_uncanceled_stop_preserves_continuation_for_later_cancel(subject, monkeypatch, complete, end_error, expected):
    runtime, ticket = setup(subject, monkeypatch, 0)
    original = step(complete=complete, error=end_error,
                    phase=InputPhase.OWNER_SKIPPED if complete else InputPhase.SEGMENT_FAILED)
    pending = subject.stopped_read_result(runtime, ticket, original, object(), 42)
    assert asyncio.run(subject.save_read_result(runtime, pending)) is None
    continuation = runtime.continuing_read_tickets[9]
    assert continuation.ticket is ticket and continuation.step is original
    assert continuation.occurred_at == 42 and continuation.result_key == 'original-key'
    assert runtime.database.saved == []
    prevented = subject.ReadDispatchPrevented('canceled_before_dispatch', canceled=True)
    recovered = subject.prevented_read_result(runtime, ticket, prevented, object())
    assert recovered.key == 'original-key' and recovered.finish.occurred_at == 42
    assert recovered.finish.outcome.outcome.status is expected
    assert recovered.finish.run_finish.status is RunOutcome.CANCELED


def test_unknown_save_retries_original_result_and_key(subject, monkeypatch):
    runtime, ticket = setup(subject, monkeypatch, 1, (DbOutcomeKind.UNKNOWN, DbOutcomeKind.COMPLETED))
    pending = subject.stopped_read_result(runtime, ticket, step(complete=True, error=None), object(), 42)
    assert pending is not None
    with pytest.raises(ValueError): asyncio.run(subject.save_read_result(runtime, pending))
    held = runtime.pending_read_results[(4, 1)]
    prepared = asyncio.run(subject.save_read_result(runtime, held, retry=True))
    assert prepared.finish.ticket is ticket and prepared.finish.occurred_at == 42
    assert prepared.finish.outcome.outcome.status is AttemptStatus.SUCCEEDED
    assert len(runtime.database.saved) == 2
    assert runtime.database.saved[0] == runtime.database.saved[1] == (prepared.finish, 'original-key', 'original-key')


def test_source_error_is_not_reclassified_as_controlled_stop(subject, monkeypatch):
    runtime, ticket = setup(subject, monkeypatch, 1)
    actual = RuntimeError('source failed')
    observed = step(complete=False, error='failed', phase=InputPhase.SEGMENT_FAILED,
                    source_failed=True, exception=actual)
    assert subject.stopped_read_result(runtime, ticket, observed, object(), 42) is None
    assert not runtime.pending_read_results and not runtime.database.saved
