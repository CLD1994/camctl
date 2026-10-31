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
    ASSUMED = 'assumed'


class RunOutcome(Enum):
    CANCELED = 'canceled'


class DbOutcomeKind(Enum):
    COMPLETED = 'completed'
    UNKNOWN = 'unknown'


class FinishDisposition(Enum):
    SAVED = 'saved'
    ALREADY_ENDED = 'already_ended'


class RecoveryBoundary(Enum):
    CONFIRMED = 'confirmed'
    UNCONFIRMED = 'unconfirmed'


class RecoveryBlockedReason(Enum):
    ACTIVE_LOCAL_READ = 'active_local_read'
    UNCONFIRMED_BOUNDARY = 'unconfirmed_boundary'
    MISSING_HORIZON = 'missing_horizon'
    INTENT_OUTSIDE_HORIZON = 'intent_outside_horizon'
    EVIDENCE_UNAVAILABLE = 'evidence_unavailable'


@dataclass(frozen=True)
class RecoveryDiagnostic:
    reason: RecoveryBlockedReason
    run_id: int
    attempt_id: int


@dataclass(frozen=True)
class AttemptTicket:
    attempt_id: int
    operation: str
    target_id: str
    responsibility_key: str
    run_id: int


@dataclass(frozen=True)
class EvidenceValue:
    type: str
    version: int
    data: dict

    @classmethod
    def of(cls, contract): return cls(contract.type, contract.version, {})


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
    def call_outcome(**kwargs): return NS(**{'error': None, **kwargs})
    def finish(status): return NS(status=status, error=None)
    modules = [
        module('camctl.capture.recovery', RecoveryBoundary=RecoveryBoundary, RecoveryBlockedReason=RecoveryBlockedReason, RecoveryDiagnostic=RecoveryDiagnostic),
        module('camctl.capture.input_copy', InputPhase=InputPhase, InputStep=InputStep),
        module('camctl.contracts.values', ConsistencyError=ConsistencyError, OperationKey=str, new_operation_key=lambda: 'original-key'),
        module('camctl.operations.attempts', AttemptFinish=AttemptFinish, BeginDisposition=object,
               FinishDisposition=FinishDisposition, ReadResumeDisposition=object, ReadResumeRequest=object,
               RunFinish=finish, RunOutcome=RunOutcome, RunStatus=object),
        module('camctl.operations.models', AttemptStatus=AttemptStatus, AttemptTicket=AttemptTicket,
               CallOutcome=call_outcome, EffectState=EffectState, ErrorValue=error_value,
               EvidenceValue=EvidenceValue, Settlement=lambda basis, evidence: NS(basis=basis, evidence=evidence),
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


class EvidenceRegistry:
    """只声明驱动实际支持的冷恢复依据。"""
    def __init__(self, supported=('local_read_recovery',)):
        self.supported = supported

    def contract(self, name, version):
        if name not in self.supported or version != 1:
            raise sys.modules['camctl.devices.evidence'].EvidenceError(name)
        return NS(type=name, version=version, operation='read', fields=frozenset())


class Cursor:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def close(self): pass


class RecoveryDatabase:
    """保持原读取尝试与结束依据，完成后才移除 RUNNING 尝试。"""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.running = True
        self.intent_event = 10
        self.saved = []
        self.connection = self

    def execute(self, sql, args):
        assert 'FROM operation_runs r' in sql and args == (9,)
        return Cursor([(4, 'read/9', 1, 1, 1, None, self.intent_event)] if self.running else [])

    async def read(self, callback): return callback(self)

    async def write(self, callback, *, key):
        finish, actual_key = callback(self)
        self.saved.append((finish, key, actual_key))
        kind = self.outcomes.pop(0)
        if kind is DbOutcomeKind.COMPLETED:
            self.running = False
        result = NS(run_was_terminal=False, disposition=FinishDisposition.SAVED)
        return NS(kind=kind, value=result if kind is DbOutcomeKind.COMPLETED else None, error=None)


def recovery_setup(subject, monkeypatch, *, saved_end=None, download_active=False,
                   outcomes=(DbOutcomeKind.COMPLETED,)):
    database = RecoveryDatabase(outcomes)
    ticket = AttemptTicket(1, 'read', '9', 'read/9', 4)
    diagnostics = []
    runtime = NS(database=database, pending_read_results={}, continuing_read_tickets={},
        pending_read_ends={}, pending_read_business={}, file_executor=None,
        operations=NS(finish_attempt=lambda finish, key, owned: (finish, key)),
        occurred_at=lambda: 999, recovery_boundary=RecoveryBoundary.CONFIRMED,
        recovery_max_event_id=10, last_recovery_diagnostic=None,
        on_recovery_diagnostic=diagnostics.append)
    monkeypatch.setitem(sys.modules, 'camctl.persistence.repositories.read_end',
        module('camctl.persistence.repositories.read_end', load_read_end=lambda copy_id, owned: saved_end))
    monkeypatch.setitem(sys.modules, 'camctl.host_files.downloads',
        module('camctl.host_files.downloads', download_owned=lambda original, staging: download_active))
    async def read_one(database, sql, params):
        if 'target_file_id,c.source_intermediate_file_id' in sql:
            assert params == (9,)
            return (701, 702)
        assert 'a.cancel_requested,r.status' in sql and params == (4, 9)
        return (1, 2)
    monkeypatch.setattr(subject, 'read_one', read_one)
    calls = []
    async def save_and_settle(runtime, pending, *, retry=False):
        calls.append((pending, retry))
        prepared = await subject.save_read_result(runtime, pending, retry=retry)
        if prepared is not None:
            subject.forget_read_result(runtime, prepared)
    return runtime, ticket, EvidenceRegistry(), save_and_settle, calls, diagnostics


def test_cold_recovery_preserves_old_ticket_and_assumed_unknown(subject, monkeypatch):
    runtime, ticket, evidence, save, calls, diagnostics = recovery_setup(subject, monkeypatch)
    assert asyncio.run(subject.settle_canceled_read(runtime, 9, evidence, save)) is True
    [(finish, key, actual_key)] = runtime.database.saved
    outcome = finish.outcome.outcome
    assert finish.ticket == ticket and finish.occurred_at == 999
    assert (outcome.status, outcome.error.code, outcome.effect) == (
        AttemptStatus.UNKNOWN, 'result_not_saved', EffectState.UNKNOWN)
    assert outcome.settlement.basis is SettlementBasis.ASSUMED
    assert outcome.settlement.evidence.type == 'local_read_recovery'
    assert finish.run_finish.status is RunOutcome.CANCELED
    assert key == actual_key == 'original-key' and not diagnostics
    assert len(calls) == 1 and calls[0][1] is False


@pytest.mark.parametrize('case,reason', [
    ('download', RecoveryBlockedReason.ACTIVE_LOCAL_READ),
    ('unconfirmed', RecoveryBlockedReason.UNCONFIRMED_BOUNDARY),
    ('no_horizon', RecoveryBlockedReason.MISSING_HORIZON),
    ('outside_horizon', RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON),
    ('no_evidence', RecoveryBlockedReason.EVIDENCE_UNAVAILABLE),
])
def test_cold_recovery_rejects_unreliable_boundary(subject, monkeypatch, case, reason):
    runtime, ticket, evidence, save, calls, diagnostics = recovery_setup(
        subject, monkeypatch, download_active=case == 'download')
    if case == 'unconfirmed': runtime.recovery_boundary = RecoveryBoundary.UNCONFIRMED
    if case == 'no_horizon': runtime.recovery_max_event_id = None
    if case == 'outside_horizon': runtime.recovery_max_event_id = 9
    if case == 'no_evidence': evidence = EvidenceRegistry(())
    assert asyncio.run(subject.settle_canceled_read(runtime, 9, evidence, save)) is False
    assert not runtime.database.saved and not calls and not runtime.pending_read_results
    assert diagnostics == [RecoveryDiagnostic(reason, ticket.run_id, ticket.attempt_id)]


@pytest.mark.parametrize('active_file', [701, 702])
def test_active_file_owner_blocks_recovery(subject, monkeypatch, active_file):
    runtime, _, evidence, save, calls, _ = recovery_setup(subject, monkeypatch)
    runtime.file_executor = NS(unfinished_files=lambda: (active_file,))
    assert asyncio.run(subject.settle_canceled_read(runtime, 9, evidence, save)) is False
    assert not calls and not runtime.database.saved and not runtime.pending_read_results


def test_saved_read_end_takes_precedence_over_assumed_recovery(subject, monkeypatch):
    ticket = AttemptTicket(1, 'read', '9', 'read/9', 4)
    end = NS(stopped=True, error=None, bytes_read=10)
    runtime, _, evidence, save, calls, _ = recovery_setup(
        subject, monkeypatch, saved_end=(ticket, end, 42, None))
    assert asyncio.run(subject.settle_canceled_read(runtime, 9, evidence, save)) is True
    [(finish, _, _)] = runtime.database.saved
    outcome = finish.outcome.outcome
    assert finish.ticket == ticket and outcome.status is AttemptStatus.SUCCEEDED
    assert outcome.settlement.basis is SettlementBasis.OBSERVED
    assert outcome.settlement.evidence.type == 'read_returned'
    assert finish.run_finish.status is RunOutcome.CANCELED
    assert len(calls) == 1


@pytest.mark.parametrize('held_kind', ['business', 'end'])
def test_existing_business_or_held_end_blocks_cold_recovery(subject, monkeypatch, held_kind):
    runtime, ticket, evidence, save, calls, _ = recovery_setup(subject, monkeypatch)
    if held_kind == 'business':
        existing = object()
        runtime.pending_read_business[(9, 'read_end')] = existing
    else:
        existing = subject.HeldReadEnd(ticket, NS(stopped=True, error=None), 42, evidence=evidence)
        runtime.pending_read_ends[9] = existing
    assert asyncio.run(subject.settle_canceled_read(runtime, 9, evidence, save)) is False
    assert not calls and not runtime.database.saved and not runtime.pending_read_results
    assert (runtime.pending_read_business[(9, 'read_end')] if held_kind == 'business'
            else runtime.pending_read_ends[9]) is existing


def test_unknown_cold_save_retries_original_pending_result(subject, monkeypatch):
    runtime, ticket, evidence, save, calls, _ = recovery_setup(subject, monkeypatch,
        outcomes=(DbOutcomeKind.UNKNOWN, DbOutcomeKind.COMPLETED))
    with pytest.raises(ValueError):
        asyncio.run(subject.settle_canceled_read(runtime, 9, evidence, save))
    original = runtime.pending_read_results[(4, 1)]
    runtime.occurred_at = lambda: 1001
    runtime.recovery_boundary = RecoveryBoundary.UNCONFIRMED
    assert asyncio.run(subject.settle_canceled_read(runtime, 9, evidence, save)) is True
    assert calls == [(original, False), (original, True)]
    first, second = runtime.database.saved
    assert first == second and first[1:] == ('original-key', 'original-key')
    assert first[0].ticket == ticket and first[0].occurred_at == 999
    assert first[0].outcome.outcome.error.code == 'result_not_saved'
