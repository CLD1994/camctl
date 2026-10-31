"""输入读取取消时区分源末尾与可靠段进度；协作者均为内存端口。"""

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


class SaveDisposition(Enum):
    SAVED = 'saved'
    REJECTED = 'rejected'
    UNKNOWN = 'unknown'


class QualificationOutcome(Enum):
    GRANTED = 'granted'
    REJECTED_FINAL = 'rejected_final'


class ResumeOutcome(Enum):
    CONTINUE = 'continue'
    VERIFY = 'verify'


class SegmentOutcome(Enum):
    COPY = 'copy'
    ALL_COMMITTED = 'all_committed'


class SegmentSaveDisposition(Enum):
    SAVED = 'saved'
    SKIPPED = 'skipped'


class CompletionPhase(Enum):
    PREPARED = 'prepared'


class Settlement(Enum):
    SETTLED = 'settled'
    WAITING = 'waiting'


@dataclass(frozen=True)
class ReadEnd:
    stopped: bool
    bytes_read: int
    error: str | None


class CopySegmentError(RuntimeError):
    def __init__(self, message, *, source_failed=False):
        super().__init__(message)
        self.source_failed = source_failed


@pytest.fixture
def subject(monkeypatch):
    class ConsistencyError(ValueError):
        pass
    class ReadPreparationError(Exception):
        pass
    class CopyPreparationError(Exception):
        pass
    class CopyCompletionError(Exception):
        pass
    class RecopyExhausted(Exception):
        pass
    copy = module('camctl.outputs.copy', CompletionPhase=CompletionPhase,
        CompletionStep=object, CopyCompletionError=CopyCompletionError,
        CopyPreparationError=CopyPreparationError, CopySegmentError=CopySegmentError,
        CopyStateFacts=object, CopyStep=object, ResumeOutcome=ResumeOutcome,
        RecopyExhausted=RecopyExhausted, SegmentOutcome=SegmentOutcome,
        SegmentSaveDisposition=SegmentSaveDisposition, SegmentStep=object,
        SourceDigestReader=object)
    modules = [
        module('camctl.capture.media', SaveDisposition=SaveDisposition, SaveReceipt=object),
        module('camctl.contracts.enums', enum_for=lambda name: NS(MATCHED=1, SOURCE_CHECKSUM_UNAVAILABLE=2)),
        module('camctl.contracts.values', ConsistencyError=ConsistencyError, ObjectId=int),
        module('camctl.devices.read_session', ReadChunk=object, ReadEnd=ReadEnd,
               ReadPreparationError=ReadPreparationError, ReadPreparationSettlement=Settlement),
        module('camctl.host_files.models', FileRef=lambda **kwargs: NS(**kwargs)), copy,
        module('camctl.outputs.qualification', FileCandidate=lambda **kwargs: NS(**kwargs),
               OperationConfig=object, QualificationOutcome=QualificationOutcome),
        module('camctl.outputs.read_attempts', ReadDispatchPrevented=type('ReadDispatchPrevented', (), {})),
    ]
    for item in modules:
        monkeypatch.setitem(sys.modules, item.__name__, item)
    name = '_isolated_cancel_input_copy'
    path = Path(__file__).parents[3] / 'src/camctl/capture/input_copy.py'
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


class Session:
    def __init__(self, position, error):
        self.offset = 0
        self.final_position = position
        self.end = ReadEnd(True, position, error)
        self.stop_requested = False

    def position(self): return self.offset
    def request_stop(self): self.stop_requested = True
    async def wait_stopped(self): return self.end


class Sessions:
    def __init__(self, session): self.session = session
    async def open_session(self, source_device_file_id, offset, *, on_start=None):
        assert (source_device_file_id, offset) == (11, 0)
        return self.session


class Copies:
    def __init__(self, outcome):
        self.outcome = outcome
        self.completed = False
        self.facts = NS(source_size=10, committed_bytes=0, verification_state=0,
                        target_sha256=None, target=NS(file_id=701, purpose='recording_input', relative_path='701.mp4'))

    async def qualify(self, candidate):
        return NS(disposition=SaveDisposition.SAVED, value=NS(outcome=QualificationOutcome.GRANTED, copy_id=9))
    async def copy_state(self, copy_id): return self.facts
    async def prepare(self, copy_id): return NS(decision=NS(outcome=ResumeOutcome.CONTINUE, offset=0))
    async def transfer(self, copy_id, session, segment_size):
        session.offset = session.final_position
        if isinstance(self.outcome, Exception): raise self.outcome
        if self.outcome == 'all':
            if self.facts.committed_bytes == 10:
                return NS(plan=NS(outcome=SegmentOutcome.ALL_COMMITTED), saved=None)
            self.facts.committed_bytes = 10
            return NS(plan=NS(outcome=SegmentOutcome.COPY, start=0, end=10, final=True),
                      saved=NS(disposition=SegmentSaveDisposition.SAVED, committed_bytes=10, reason=None))
        return NS(plan=NS(outcome=SegmentOutcome.COPY, start=0, end=10, final=True),
                  saved=NS(disposition=SegmentSaveDisposition.SKIPPED, committed_bytes=0, reason='owner_cancel'))
    async def complete(self, copy_id, digest):
        self.completed = True
        return NS(phase=CompletionPhase.PREPARED)


def obtain(subject, outcome, position, end_error):
    session, copies, observations = Session(position, end_error), Copies(outcome), []
    context = subject.InputContext(action_id=1, processing_id=5, source_device_file_id=11,
        target_extension='mp4', config=object(), segment_size=10, staging=Path('staging'),
        copies=copies, sessions=Sessions(session), occurred_at=42,
        on_read_end=lambda end, complete: observations.append((end, complete)))
    result = asyncio.run(subject.obtain_recording_input(context))
    assert session.stop_requested
    return result, copies, observations


def test_owner_skip_at_source_end_is_not_reliable_completion(subject):
    result, copies, observations = obtain(subject, 'skip', 10, None)
    assert result.phase is subject.InputPhase.OWNER_SKIPPED
    assert result.content_complete is True
    assert copies.facts.committed_bytes == 0 and not copies.completed
    assert observations == [(result.read_end, False)]


def test_all_committed_is_reliable_completion(subject):
    result, copies, observations = obtain(subject, 'all', 10, None)
    assert result.phase is subject.InputPhase.INPUT_READY and copies.completed
    assert result.content_complete is True
    assert observations == [(result.read_end, True)]


@pytest.mark.parametrize('position', [4, 10])
def test_source_error_preserves_original_facts(subject, position):
    error = CopySegmentError('source failed', source_failed=True)
    result, copies, observations = obtain(subject, error, position, 'failed')
    assert result.phase is subject.InputPhase.SEGMENT_FAILED
    assert result.error is error and result.source_failed
    assert result.content_complete is (position == 10)
    assert not copies.completed and observations == [(result.read_end, False)]
