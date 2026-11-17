"""内部读取恢复与 UNKNOWN 耗尽从媒体和分段检查入口推进。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

from camctl.capture import media_flow, segment_inspections
from camctl.capture.input_copy import InputPhase, InputStep
from camctl.capture.media import MediaPolicy, MediaTools, SaveDisposition, SaveReceipt
from camctl.contracts.enums import enum_for
from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget
from camctl.devices.read_session import ReadEnd
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.operations.attempts import BeginAttemptResult, BeginDisposition
from camctl.persistence.repositories.operations import OperationRepository
from camctl.outputs import read_attempts
from camctl.outputs.qualification import FileQualification, OperationConfig, QualificationOutcome
from camctl.outputs.slots import SlotDecision, SlotOutcome
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.segment_inspections import SegmentInspectionRef, SegmentInspectionRepository
from unit.capture.test_input_copy import _state
from unit.capture.test_media_execution import _status


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def close(self):
        pass

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _ReadFacts:
    """只返回本用例的已保存读取事实，不访问真实数据库。"""

    def __init__(self, attempt_status, attempts_used=1):
        self.attempt_status = attempt_status
        self.attempts_used = attempts_used

    def execute(self, sql, params=()):
        if 'SELECT status,cancel_requested FROM actions' in sql:
            return _Cursor([(int(enum_for('actions.status').RUNNING), 0)])
        if 'SELECT m.file_id,d.original_name' in sql:
            return _Cursor([(30, 'recording.mp4')])
        if 'SELECT checksum_support' in sql:
            return _Cursor([(2,)])
        if 'SELECT id,status,attempts_used,max_attempts_used,retry_wait_required' in sql:
            return _Cursor([(4, int(enum_for('operation_runs.status').ACTIVE), self.attempts_used, 1, 0)])
        if 'SELECT attempts_used,retry_wait_required' in sql:
            return _Cursor([(self.attempts_used, 0)])
        if 'SELECT r.status,r.attempts_used,a.id,a.status' in sql:
            return _Cursor([(int(enum_for('operation_runs.status').ACTIVE), self.attempts_used, 88, self.attempt_status)])
        if 'SELECT c.id FROM file_copies' in sql:
            return _Cursor([])
        raise AssertionError(f'意外的读取: {sql}')


@pytest.fixture
def setup(monkeypatch):
    facts = _ReadFacts(int(enum_for('operation_attempts.status').UNKNOWN))
    owned = SimpleNamespace(connection=facts)
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.side_effect = lambda callback, **kwargs: callback(owned)
    repository = create_autospec(SegmentInspectionRepository, instance=True)
    operations = create_autospec(OperationRepository, instance=True)
    operations.exhaust_blocked_read.return_value = BeginAttemptResult(BeginDisposition.REJECTED, reason='budget_exhausted')
    sessions = Mock()
    sessions.for_attempt.return_value = sessions
    flow = media_flow.MediaFlow(database, BoundDirectories(Path('staging')), sessions,
        create_autospec(MediaTools, instance=True), MediaPolicy(Decimal('1')),
        lambda: 100, False, max_read_attempts=1, read_idle_timeout_s=Decimal('5'),
        retry_interval_s=Decimal('0'), operations=operations)
    copies = Mock()
    copies.qualify = AsyncMock(return_value=SaveReceipt(SaveDisposition.SAVED,
        FileQualification(QualificationOutcome.GRANTED, 9, 4, None, 701, None)))
    copies.copy_state = AsyncMock(return_value=_state(round=2))
    copies.repository.grant_read_slot = Mock(side_effect=AssertionError('不得授予新的读取机会'))
    monkeypatch.setattr(media_flow.MediaFlow, 'copies', lambda self: copies)
    monkeypatch.setattr(media_flow, 'load_processing_status', lambda *args: _status(repair_state=2))
    monkeypatch.setattr(read_attempts, 'unfinished_read_end', lambda *args: None)
    monkeypatch.setattr(read_attempts, 'retry_read_business', AsyncMock())
    monkeypatch.setattr(read_attempts, 'retry_read_ends', AsyncMock())
    monkeypatch.setattr(segment_inspections, 'retry_read_business', AsyncMock())
    monkeypatch.setattr(segment_inspections, 'retry_read_ends', AsyncMock())
    monkeypatch.setattr(media_flow, 'resume_media_saves', AsyncMock(return_value=False))
    monkeypatch.setattr(media_flow, '_fail_internal_read_input', AsyncMock())
    budget = OperationConfig(1, Decimal('5'), Decimal('0'))
    request = segment_inspections.SegmentInspectionRequest(
        21, 8, CoverageTarget(CoverageMetric.FRAMES, 10), budget, 100)
    ref = SegmentInspectionRef(4, 21, 3, 8, 30, int(enum_for('segment_inspections.state').PENDING),
        {'metric': 'frames', 'value': 10},
        {'max_attempts': 1, 'timeout_s': Decimal('5'), 'retry_interval_s': Decimal('0')})
    repository.create.return_value = DbOutcome(kind=DbOutcomeKind.COMPLETED, value=ref)
    repository.read.return_value = ref
    database.write.side_effect = lambda callback, **kwargs: callback(owned)
    monkeypatch.setattr(segment_inspections, 'SegmentInspectionRepository', lambda: repository)
    monkeypatch.setattr(segment_inspections, 'read_inspection_for_member',
                        lambda *args, **kwargs: SimpleNamespace(copy=None))
    return SimpleNamespace(flow=flow, facts=facts, copies=copies, owned=owned,
                           request=request, operations=operations, repository=repository)


def _save_business(setup, monkeypatch):
    async def save(flow, copy_id, operation, request, save):
        assert copy_id == 9
        assert operation in ('unknown_read_exhaustion', 'grant_read_slot')
        return SimpleNamespace(value=save(request, 'stable-operation-key', setup.owned))
    monkeypatch.setattr(read_attempts, 'save_read_business', save)


@pytest.mark.asyncio
async def test_verified_segment_waits_when_original_result_is_unconfirmed(setup, monkeypatch):
    setup.copies.copy_state.return_value = _state(round=2, verification_state=3, target_sha256='a' * 64)
    ticket = object()
    end = ReadEnd(stopped=True, bytes_read=10, error=None)
    monkeypatch.setattr(read_attempts, 'unfinished_read_end', lambda *args: (ticket, (ticket, end, 99, None)))
    trace = []
    async def valid(*args):
        trace.append('recheck')
        return True
    async def obtain(context):
        assert context.read_end is end
        trace.append('local_copy')
        return InputStep(InputPhase.INPUT_READY, copy_id=9,
            input_file=FileRef(701, FilePurpose.RECORDING_INPUT, 'recording-inputs/701.mp4', Path('staging')))
    async def finish(flow, actual, step, **kwargs):
        assert actual is ticket
        trace.append('finish_original')
    async def owned(flow, copy_id, callback):
        trace.append('file_owner')
        return await callback(None)
    monkeypatch.setattr(read_attempts, 'current_local_read_end', valid)
    monkeypatch.setattr(segment_inspections, 'obtain_recording_input', obtain)
    monkeypatch.setattr(segment_inspections, '_finish_internal_read', finish)
    monkeypatch.setattr(segment_inspections, 'run_owned_read', owned)
    import camctl.persistence.repositories.segment_inspections as inspection_repository
    monkeypatch.setattr(inspection_repository, 'inspection_input_read_succeeded', lambda *args: False)
    result = await segment_inspections.inspect_segment(setup.flow, setup.request)
    assert result.phase is segment_inspections.SegmentInspectionPhase.WAITING
    assert result.reason == 'read_result_unconfirmed'
    assert trace == ['file_owner', 'recheck', 'local_copy', 'finish_original']
    setup.copies.repository.grant_read_slot.assert_not_called()


@pytest.mark.asyncio
async def test_verified_segment_delivers_original_input_after_read_result_saved(setup, monkeypatch):
    setup.copies.copy_state.return_value = _state(round=2, verification_state=3, target_sha256='a' * 64)
    ticket = object()
    end = ReadEnd(stopped=True, bytes_read=10, error=None)
    expected_file = FileRef(701, FilePurpose.RECORDING_INPUT, 'recording-inputs/701.mp4', Path('staging'))
    monkeypatch.setattr(read_attempts, 'unfinished_read_end', lambda *args: (ticket, (ticket, end, 99, None)))
    monkeypatch.setattr(read_attempts, 'current_local_read_end', AsyncMock(return_value=True))
    trace = []
    saved = []

    async def finish(flow, actual, step, **kwargs):
        assert actual is ticket
        assert step.phase is InputPhase.INPUT_READY
        assert step.input_file == expected_file
        assert step.read_end is end
        saved.append(step)
        trace.append('read_result_saved')

    async def owned(flow, copy_id, callback):
        assert copy_id == 9
        return await callback(None)

    monkeypatch.setattr(segment_inspections, '_finish_internal_read', finish)
    monkeypatch.setattr(segment_inspections, 'run_owned_read', owned)
    import camctl.persistence.repositories.segment_inspections as inspection_repository
    monkeypatch.setattr(inspection_repository, 'inspection_input_read_succeeded',
                        lambda connection, inspection: bool(saved))
    stream = MediaStream(0, 'video', 'h264', 1920, 1080, Decimal('1'), False, frame_count=25)
    probe = MediaProbe(Decimal('1'), None, (stream,))

    async def probe_segment(input_file, *, count_frames):
        assert saved and saved[0].input_file is input_file
        assert input_file == expected_file
        assert count_frames is True
        trace.append('probe')
        return FileTaskResult(FileTaskId('probe'), True, value=probe)

    setup.flow.tools.probe_segment.side_effect = probe_segment
    def save_result(request, key, owned):
        assert request.inspection_id == 4
        assert request.result_json['metric'] == {'metric': 'frames', 'value': 25, 'reason': None}
        trace.append('inspection_saved')
        ref = replace(setup.repository.read.return_value, state=request.state, result_json=request.result_json)
        setup.repository.read.return_value = ref
        return DbOutcome(DbOutcomeKind.COMPLETED, value=ref)

    setup.repository.save_result.side_effect = save_result
    result = await segment_inspections.inspect_segment(setup.flow, setup.request)
    assert result.phase is segment_inspections.SegmentInspectionPhase.COMPLETED
    assert result.copy_id == 9
    assert (result.metric.metric, result.metric.value) == (CoverageMetric.FRAMES, 25)
    assert trace == ['read_result_saved', 'probe', 'inspection_saved']
    setup.flow.tools.probe_segment.assert_awaited_once_with(expected_file, count_frames=True)
    setup.copies.repository.grant_read_slot.assert_not_called()
    setup.flow.sessions.open_session.assert_not_called()


@pytest.mark.asyncio
async def test_verified_segment_without_reliable_end_waits(setup, monkeypatch):
    setup.copies.copy_state.return_value = _state(round=2, verification_state=3, target_sha256='a' * 64)
    monkeypatch.setattr(read_attempts, 'unfinished_read_end', lambda *args: (object(), None))
    result = await segment_inspections.inspect_segment(setup.flow, setup.request)
    assert (result.phase, result.reason) == (segment_inspections.SegmentInspectionPhase.WAITING, 'read_end_unconfirmed')
    setup.copies.repository.grant_read_slot.assert_not_called()


@pytest.mark.asyncio
async def test_media_entry_finishes_exhausted_unknown_before_slot_grant(setup, monkeypatch):
    _save_business(setup, monkeypatch)
    result = await media_flow.run_recording_media(setup.flow, 21, 1, 30)
    assert (result.phase, result.reason, result.copy_id) == (InputPhase.WAITING, 'run_ended', 9)
    setup.copies.repository.grant_read_slot.assert_not_called()
    setup.operations.exhaust_blocked_read.assert_called_once()


@pytest.mark.asyncio
async def test_segment_entry_finishes_exhausted_unknown_before_slot_grant(setup, monkeypatch):
    _save_business(setup, monkeypatch)
    monkeypatch.setattr(segment_inspections, 'settle_inspection_read_failure', AsyncMock(return_value=False))
    result = await segment_inspections.inspect_segment(setup.flow, setup.request)
    assert (result.phase, result.reason, result.copy_id) == (segment_inspections.SegmentInspectionPhase.WAITING, 'result_save_unknown', 9)
    setup.copies.repository.grant_read_slot.assert_not_called()
    setup.operations.exhaust_blocked_read.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,used,limit,exhausted', [
    ('UNKNOWN', 1, 1, True), ('RUNNING', 1, 1, False), ('UNKNOWN', 1, 2, False),
])
async def test_only_unknown_attempt_at_current_limit_uses_original_attempt_identity(setup, monkeypatch, status, used, limit, exhausted):
    setup.facts.attempt_status = int(enum_for('operation_attempts.status')[status])
    setup.facts.attempts_used = used
    setup.flow.max_read_attempts = limit
    _save_business(setup, monkeypatch)
    if not exhausted:
        setup.copies.repository.grant_read_slot.side_effect = None
        setup.copies.repository.grant_read_slot.return_value = SlotDecision(SlotOutcome.WAIT, reason='device_busy')
    result = await media_flow.run_recording_media(setup.flow, 21, 1, 30)
    if exhausted:
        assert result.reason == 'run_ended'
        intent, attempt_id, key, owned = setup.operations.exhaust_blocked_read.call_args.args
        assert (attempt_id, key, owned) == (88, 'stable-operation-key', setup.owned)
        assert (intent.action_id, intent.target.copy_id, intent.copy_round, intent.config.max_attempts) == (21, 9, 2, 1)
        setup.copies.repository.grant_read_slot.assert_not_called()
    else:
        assert result.phase is InputPhase.WAITING
        setup.operations.exhaust_blocked_read.assert_not_called()
        setup.copies.repository.grant_read_slot.assert_called_once()
