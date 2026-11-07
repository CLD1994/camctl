"""真实成员的分段媒体检查与读取失败。"""
from dataclasses import replace
from decimal import Decimal
import hashlib

import pytest
import pytest_asyncio

from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.segment_inspections import SegmentInspectionPhase, SegmentInspectionRequest, inspect_segment
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs.qualification import OperationConfig
from camctl.persistence.repositories.segment_inspections import read_inspection_for_member
from camctl.session.supervision import Supervisor

from ..support.segment_inspection_cases import inspection_source
from ..support.capture import capture_database
from ..support.segment_inspection_cases import CONTENT as _CONTENT, Digest as _Digest, FailedDigest as _FailedDigest, ReadDriver as _ReadDriver, SegmentTools as _SegmentTools

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def inspection_world(tmp_path):
    owned, runtime, ref, members = await inspection_source(tmp_path)
    supervisor = Supervisor()
    try:
        database = await capture_database(owned)
        roots = BoundDirectories(staging=tmp_path / 'staging')
        roots.staging.mkdir(exist_ok=True)
        def build(*, driver=None, digest=None, segment_size=4 * 1024 * 1024):
            driver = driver or _ReadDriver(_CONTENT)
            digest = digest or _Digest(hashlib.sha256(_CONTENT).hexdigest())
            tools = _SegmentTools()
            flow = MediaFlow(database=database, roots=roots,
                sessions=DriverReadSessions(database, driver, ticket=None), tools=tools,
                policy=MediaPolicy(repair_margin_s=Decimal('2')), occurred_at=runtime.wall_us,
                digest_supported=True, digest=digest, segment_size=segment_size,
                file_executor=FileTaskExecutor(supervisor), retry_interval_s=Decimal('0'))
            def request(member=0, metric=CoverageMetric.DURATION_S, attempts=3):
                return SegmentInspectionRequest(ref.action_id, members[member].member_id,
                    CoverageTarget(metric, 60), OperationConfig(attempts, Decimal('10'), Decimal('0')),
                    runtime.wall_us())
            return flow, request, driver, digest, tools
        yield owned, roots, members, build
    finally:
        result = await supervisor.drain_required()
        assert not result.failed
        owned.connection.close()


@pytest.mark.parametrize(('metric', 'expected', 'frames'), [
    (CoverageMetric.DURATION_S, Decimal('1.5'), False),
    (CoverageMetric.FRAMES, 45, True),
])
async def test_complete_copy_and_saved_metric_reused(inspection_world, metric, expected, frames):
    owned, roots, members, build = inspection_world
    flow, request, driver, digest, tools = build()
    original = request(metric=metric)
    first = await inspect_segment(flow, original)
    assert first.phase is SegmentInspectionPhase.COMPLETED, first
    assert first.metric.value == expected
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert facts.copy.committed_bytes == len(_CONTENT)
    assert facts.copy.target_sha256 == hashlib.sha256(_CONTENT).hexdigest()
    assert (roots.staging / facts.copy.input_relative_path).read_bytes() == _CONTENT
    assert (await inspect_segment(flow, original)).metric == first.metric
    for changed in (replace(original, target=CoverageTarget(metric, 61)),
                    replace(original, budget=OperationConfig(4, Decimal('10'), Decimal('0')))):
        with pytest.raises(ConsistencyError):
            await inspect_segment(flow, changed)
    assert driver.offsets == [0]
    assert digest.calls == 1
    assert tools.requests == [frames]


async def test_two_members_have_independent_copies(inspection_world):
    owned, roots, members, build = inspection_world
    flow, request, driver, digest, tools = build()
    first, second = [await inspect_segment(flow, request(member=n)) for n in (0, 1)]
    assert first.phase is second.phase is SegmentInspectionPhase.COMPLETED
    assert first.inspection_id != second.inspection_id
    assert first.copy_id != second.copy_id
    for n, step in enumerate((first, second)):
        facts = read_inspection_for_member(request(n).action_id, request(n).member_id, owned)
        assert facts.inspection.source_device_file_id == members[n].file_id
        assert facts.copy.inspection_id == step.inspection_id
        assert facts.copy.source_size == len(_CONTENT)
        assert (roots.staging / facts.copy.input_relative_path).read_bytes() == _CONTENT
    assert tools.requests == [False, False]
    assert driver.offsets == [0, 0]


async def test_partial_copy_is_not_probed(inspection_world):
    owned, _, _, build = inspection_world
    driver = _ReadDriver(_CONTENT, fail_after=4)
    flow, request, _, digest, tools = build(driver=driver, segment_size=4)
    original = request()
    step = await inspect_segment(flow, original)
    assert step.phase is SegmentInspectionPhase.WAITING
    copy = read_inspection_for_member(original.action_id, original.member_id, owned).copy
    assert 0 < copy.committed_bytes < copy.source_size
    assert copy.verification_state not in (3, 5)
    assert tools.requests == []
    assert digest.calls == 0


async def test_source_digest_failure_preserves_original_error(inspection_world):
    owned, _, _, build = inspection_world
    digest = _FailedDigest()
    flow, request, driver, _, tools = build(digest=digest, segment_size=4)
    original = request()
    step = await inspect_segment(flow, original)
    assert step.phase is SegmentInspectionPhase.FAILED
    assert step.reason == 'source_file_unconfirmed'
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    failure = facts.inspection.result_json['failure']
    assert failure == {'code': 'read_failed', 'reason': 'source_file_unconfirmed',
        'read_error': {'code': 'source_file_unconfirmed', 'stage': 'source_read', 'details': {}},
        'verification_error': {'reason': digest.error}}
    assert facts.copy.committed_bytes == facts.copy.source_size == len(_CONTENT)
    assert driver.offsets == [0]
    assert digest.calls == 1
    assert tools.requests == []
    run = owned.connection.execute('SELECT status,attempts_used,retry_wait_required,error_json FROM operation_runs WHERE copy_id=?', (facts.copy.copy_id,)).fetchone()
    from camctl.contracts.enums import enum_for
    from camctl.contracts.json_values import parse_exact_json
    assert run[:3] == (int(enum_for('operation_runs.status').FAILED), 1, 0)
    assert parse_exact_json(run[3]) == failure['read_error']
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is None


async def test_one_attempt_really_exhausted(inspection_world):
    owned, _, _, build = inspection_world
    driver = _ReadDriver(_CONTENT, fail_after=0)
    digest = _FailedDigest()
    flow, request, _, _, tools = build(driver=driver, digest=digest, segment_size=4)
    original = request(attempts=1)
    step = await inspect_segment(flow, original)
    assert step.phase is SegmentInspectionPhase.FAILED
    assert step.reason == 'read_attempts_exhausted'
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert facts.inspection.result_json['failure']['reason'] == 'read_attempts_exhausted'
    assert driver.offsets == [0]
    assert digest.calls == 0
    assert tools.requests == []
    from camctl.contracts.enums import enum_for
    from camctl.contracts.json_values import parse_exact_json
    run = owned.connection.execute('SELECT status,attempts_used,retry_wait_required,error_json FROM operation_runs WHERE copy_id=?', (facts.copy.copy_id,)).fetchone()
    assert run[:3] == (int(enum_for('operation_runs.status').FAILED), 1, 0)
    assert parse_exact_json(run[3])['code'] == 'read_attempts_exhausted'
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is None


async def test_failed_result_unknown_replays_original_request_and_key(inspection_world, monkeypatch):
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository
    owned, _, _, build = inspection_world
    digest = _FailedDigest()
    flow, request, driver, _, tools = build(digest=digest, segment_size=4)
    original = request()
    save = SegmentInspectionRepository.save_result
    calls = []

    def commit_then_unknown(repository, command, key, connection):
        calls.append((command, key))
        result = save(repository, command, key, connection)
        if len(calls) == 1:
            assert result.kind is DbOutcomeKind.COMPLETED
            return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError('commit reply lost'))
        return result

    monkeypatch.setattr(SegmentInspectionRepository, 'save_result', commit_then_unknown)
    first = await inspect_segment(flow, original)
    assert first.phase is SegmentInspectionPhase.WAITING
    assert first.reason == 'result_save_unknown'
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    row = owned.connection.execute('SELECT state,result_event_id FROM segment_inspections WHERE id=?', (facts.inspection.inspection_id,)).fetchone()
    assert row[1] is not None
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is not None
    resumed = await inspect_segment(flow, original)
    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert resumed.reason == 'source_file_unconfirmed'
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0].occurred_at == original.occurred_at
    assert owned.connection.execute('SELECT state,result_event_id FROM segment_inspections WHERE id=?', (facts.inspection.inspection_id,)).fetchone() == row
    assert owned.connection.execute('SELECT COUNT(*) FROM history_transactions WHERE operation_key=?', (str(calls[0][1]),)).fetchone() == (1,)
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is None
    assert driver.offsets == [0] and digest.calls == 1 and tools.requests == []


async def test_slot_release_unknown_replays_original_request_and_key(inspection_world, monkeypatch):
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository
    owned, _, _, build = inspection_world
    digest = _FailedDigest()
    flow, request, driver, _, tools = build(digest=digest, segment_size=4)
    original = request()
    release = OutputsRepository.release_read_slot
    calls = []

    def commit_then_unknown(repository, command, key, connection):
        calls.append((command, key))
        result = release(repository, command, key, connection)
        if len(calls) == 1:
            assert result.kind is DbOutcomeKind.COMPLETED
            return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError('commit reply lost'))
        return result

    monkeypatch.setattr(OutputsRepository, 'release_read_slot', commit_then_unknown)
    with pytest.raises(ConsistencyError, match='原读取业务结果未可靠保存'):
        await inspect_segment(flow, original)
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is None
    resumed = await inspect_segment(flow, original)
    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0].occurred_at == original.occurred_at
    assert owned.connection.execute('SELECT COUNT(*) FROM history_transactions WHERE operation_key=?', (str(calls[0][1]),)).fetchone() == (1,)
    assert driver.offsets == [0] and digest.calls == 1 and tools.requests == []
