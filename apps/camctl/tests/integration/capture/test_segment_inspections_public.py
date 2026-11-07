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

from ..support.segment_inspection_cases import inspection_source, assembled_inspections, prepare_other_inspection
from ..support.capture import capture_database
from ..support.segment_inspection_cases import CONTENT as _CONTENT, Digest as _Digest, FailedDigest as _FailedDigest, ReadDriver as _ReadDriver, SegmentTools as _SegmentTools

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def inspection_world(tmp_path, request):
    count = getattr(request, "param", 2)
    owned, runtime, ref, members = await inspection_source(tmp_path, count=count)
    supervisor = Supervisor()
    try:
        database = await capture_database(owned)
        roots = BoundDirectories(staging=tmp_path / 'staging')
        roots.staging.mkdir(exist_ok=True)
        def build(*, driver=None, digest=None, segment_size=4 * 1024 * 1024, database_override=None):
            selected_database = database if database_override is None else database_override
            driver = driver or _ReadDriver(_CONTENT)
            digest = digest or _Digest(hashlib.sha256(_CONTENT).hexdigest())
            tools = _SegmentTools()
            flow = MediaFlow(database=selected_database, roots=roots,
                sessions=DriverReadSessions(selected_database, driver, ticket=None), tools=tools,
                policy=MediaPolicy(repair_margin_s=Decimal('2')), occurred_at=runtime.wall_us,
                digest_supported=True, digest=digest, segment_size=segment_size,
                file_executor=FileTaskExecutor(supervisor), retry_interval_s=Decimal('0'))
            def request(member=0, metric=CoverageMetric.DURATION_S, attempts=3):
                return SegmentInspectionRequest(ref.action_id, members[member].member_id,
                    CoverageTarget(metric, 60), OperationConfig(attempts, Decimal('10'), Decimal('0')),
                    runtime.wall_us())
            return flow, request, driver, digest, tools
        yield owned, roots, members, build, supervisor, runtime
    finally:
        result = await supervisor.drain_required()
        assert not result.failed
        owned.connection.close()


@pytest.mark.parametrize(('metric', 'expected', 'frames'), [
    (CoverageMetric.DURATION_S, Decimal('1.5'), False),
    (CoverageMetric.FRAMES, 45, True),
])
async def test_complete_copy_and_saved_metric_reused(inspection_world, metric, expected, frames):
    owned, roots, members, build, _, _ = inspection_world
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
    owned, roots, members, build, _, _ = inspection_world
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
    owned, _, _, build, _, _ = inspection_world
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
    owned, _, _, build, _, _ = inspection_world
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
    owned, _, _, build, _, _ = inspection_world
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
    owned, _, _, build, _, _ = inspection_world
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
    owned, _, _, build, _, _ = inspection_world
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


async def test_saved_probe_result_is_reused_after_database_worker_reopen(inspection_world, monkeypatch):
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository
    from ..support.capture import restart_capture_database
    owned, _, _, build, supervisor, _ = inspection_world
    flow, request, driver, digest, tools = build()
    original = request()
    save = SegmentInspectionRepository.save_result
    calls = []

    def commit_then_unknown(repository, command, key, connection):
        calls.append((command, key))
        result = save(repository, command, key, connection)
        assert result.kind is DbOutcomeKind.COMPLETED
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError('commit reply lost'))

    monkeypatch.setattr(SegmentInspectionRepository, 'save_result', commit_then_unknown)
    first = await inspect_segment(flow, original)
    assert first.phase is SegmentInspectionPhase.WAITING
    assert first.reason == 'result_save_unknown'
    assert tools.requests == [False]
    before = read_inspection_for_member(original.action_id, original.member_id, owned)
    result_event, = owned.connection.execute('SELECT result_event_id FROM segment_inspections WHERE id=?', (before.inspection.inspection_id,)).fetchone()
    assert result_event is not None
    drained = await supervisor.drain_required()
    assert not drained.failed
    database = await restart_capture_database(owned)
    monkeypatch.setattr(SegmentInspectionRepository, 'save_result', save)
    new_flow, _, new_driver, new_digest, new_tools = build(database_override=database)
    resumed = await inspect_segment(new_flow, original)
    assert resumed.phase is SegmentInspectionPhase.COMPLETED
    assert resumed.metric.value == Decimal('1.5')
    assert new_driver.offsets == [] and new_digest.calls == 0 and new_tools.requests == []
    assert driver.offsets == [0] and digest.calls == 1 and tools.requests == [False]
    after = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert after.inspection == before.inspection
    assert owned.connection.execute('SELECT result_event_id FROM segment_inspections WHERE id=?', (after.inspection.inspection_id,)).fetchone() == (result_event,)
    assert len(calls) == 1


async def test_same_session_replays_original_read_end_after_save_failure(inspection_world, monkeypatch):
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.operations import OperationRepository
    owned, _, _, build, _, _ = inspection_world
    digest = _FailedDigest()
    flow, request, driver, _, tools = build(digest=digest, segment_size=4)
    original = request()
    finish = OperationRepository.finish_attempt
    calls = []

    def fail_original_read_finish(repository, command, key, connection):
        if command.ticket.operation == "read":
            calls.append((command, key))
            if len(calls) == 1:
                return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError('read finish reply unavailable'))
        return finish(repository, command, key, connection)

    monkeypatch.setattr(OperationRepository, 'finish_attempt', fail_original_read_finish)
    with pytest.raises(ConsistencyError, match='读取结果未可靠保存'):
        await inspect_segment(flow, original)
    assert len(calls) == 1
    first = calls[0]
    assert owned.connection.execute('SELECT COUNT(*) FROM history_transactions WHERE operation_key=?', (str(first[1]),)).fetchone() == (0,)
    from camctl.contracts.enums import enum_for
    assert owned.connection.execute('SELECT status FROM operation_attempts WHERE run_id=? AND attempt_no=?', (first[0].ticket.run_id, first[0].ticket.attempt_id)).fetchone() == (int(enum_for('operation_attempts.status').RUNNING),)
    resumed = await inspect_segment(flow, original)
    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert resumed.reason == 'source_file_unconfirmed'
    assert len(calls) == 2 and calls[1] == first
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert facts.inspection.result_json['failure']['verification_error'] == {'reason': digest.error}
    assert owned.connection.execute('SELECT COUNT(*) FROM history_transactions WHERE operation_key=?', (str(first[1]),)).fetchone() == (1,)
    assert driver.offsets == [0] and digest.calls == 1 and tools.requests == []


async def test_failed_read_is_saved_after_database_worker_reopen(inspection_world, monkeypatch):
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository
    from ..support.capture import restart_capture_database
    owned, _, _, build, supervisor, _ = inspection_world
    digest = _FailedDigest()
    flow, request, driver, _, tools = build(digest=digest, segment_size=4)
    original = request()
    save = SegmentInspectionRepository.save_result
    calls = []

    def unavailable(repository, command, key, connection):
        calls.append((command, key))
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError('result reply unavailable'))

    monkeypatch.setattr(SegmentInspectionRepository, 'save_result', unavailable)
    first = await inspect_segment(flow, original)
    assert first.phase is SegmentInspectionPhase.WAITING
    assert first.reason == 'result_save_unknown'
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    row = owned.connection.execute('SELECT status, error_json FROM operation_runs WHERE copy_id=?', (facts.copy.copy_id,)).fetchone()
    from camctl.contracts.enums import enum_for
    from camctl.contracts.json_values import parse_exact_json
    assert row[0] == int(enum_for('operation_runs.status').FAILED)
    assert parse_exact_json(row[1])['code'] == 'source_file_unconfirmed'
    assert facts.inspection.state == int(enum_for('segment_inspections.state').PENDING)
    assert len(calls) == 1
    drained = await supervisor.drain_required()
    assert not drained.failed
    database = await restart_capture_database(owned)
    monkeypatch.setattr(SegmentInspectionRepository, 'save_result', save)
    new_flow, _, new_driver, new_digest, new_tools = build(database_override=database)
    resumed = await inspect_segment(new_flow, original)
    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert resumed.reason == 'source_file_unconfirmed'
    saved = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert saved.inspection.result_json['failure']['verification_error'] == {'reason': digest.error}
    assert new_driver.offsets == [] and new_digest.calls == 0 and new_tools.requests == []
    assert driver.offsets == [0] and digest.calls == 1 and tools.requests == []
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is None


async def test_process_interruption_keeps_unconfirmed_read_and_slot(inspection_world, capture_resources, tmp_path):
    import asyncio
    import sys
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.contracts.enums import enum_for
    from ..support.capture import capture_database
    from ..support.database import database_path
    owned, roots, _, build, supervisor, _ = inspection_world
    _, request, _, _, _ = build(segment_size=4)
    original = request()
    path = database_path(owned)
    drained = await supervisor.drain_required()
    assert not drained.failed
    await capture_resources.close()
    receipt = tmp_path / 'interrupted-read.json'
    process = await asyncio.create_subprocess_exec(sys.executable, '-m',
        'integration.support.segment_interrupted_read', str(path), str(roots.staging),
        str(original.action_id), str(original.member_id), str(original.occurred_at), str(receipt),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        for _ in range(200):
            if receipt.exists() or process.returncode is not None:
                break
            await asyncio.sleep(0.05)
        assert receipt.exists(), '子进程没有进入实际读取'
        assert process.returncode is None
    finally:
        if process.returncode is None:
            process.kill()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == -9, (stdout, stderr)
    assert receipt.read_text() == 'read entered'
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert facts.inspection.state == int(enum_for('segment_inspections.state').PENDING)
    copy_id = facts.copy.copy_id
    before = owned.connection.execute('SELECT status, attempts_used FROM operation_runs WHERE copy_id=?', (copy_id,)).fetchone()
    assert before == (int(enum_for('operation_runs.status').ACTIVE), 1)
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (copy_id,)).fetchone()[0] is not None
    database = await capture_database(owned)
    flow, _, driver, digest, tools = build(database_override=database)
    assert flow.recovery_boundary is RecoveryBoundary.UNCONFIRMED
    resumed = await inspect_segment(flow, original)
    assert resumed.phase is SegmentInspectionPhase.WAITING
    assert resumed.reason == 'recovery_blocked'
    assert driver.offsets == [] and digest.calls == 0 and tools.requests == []
    assert owned.connection.execute('SELECT status, attempts_used FROM operation_runs WHERE copy_id=?', (copy_id,)).fetchone() == before
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (copy_id,)).fetchone()[0] is not None


@pytest.mark.parametrize('inspection_world', [7], indirect=True)
async def test_canceled_capture_processes_seven_inspections_in_batches(inspection_world, capture_resources, monkeypatch):
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.segment_inspections import InspectionCreate, SegmentInspectionRepository
    from ..persistence.test_segment_result_versions import _cancel
    owned, roots, members, build, _, source_runtime = inspection_world
    _, request, _, _, _ = build()
    runtime, driver = await assembled_inspections(capture_resources, owned, roots, source_runtime)
    repo = SegmentInspectionRepository()
    for n in range(7):
        item = request(n)
        command = InspectionCreate(item.action_id, item.member_id,
            {'metric': 'duration_s', 'value': Decimal('60')},
            {'max_attempts': 3, 'timeout_s': Decimal('10'), 'retry_interval_s': Decimal('0')}, item.occurred_at)
        result = repo.create(command, new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
    other_action, other_member = await prepare_other_inspection(capture_resources, owned, roots, source_runtime)
    other_before = read_inspection_for_member(other_action, other_member, owned)
    ids = [row[0] for row in owned.connection.execute(
        'SELECT id FROM segment_inspections WHERE action_id=? ORDER BY id', (request().action_id,))]
    _cancel(owned, runtime, request().action_id)
    original_read = runtime.database.read
    observed_pages = []

    async def observe_read(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        if isinstance(result, list) and result and all(
                isinstance(row, tuple) and len(row) == 1 and row[0] in ids for row in result):
            observed_pages.append(tuple(row[0] for row in result))
        return result

    monkeypatch.setattr(runtime.database, 'read', observe_read)
    await capture_handler('camera_record')(request().action_id, runtime)
    assert all(tuple(ids[offset:offset + 2]) in observed_pages for offset in range(0, 7, 2))
    assert read_inspection_for_member(other_action, other_member, owned) == other_before
    rows = owned.connection.execute('SELECT member_id,state,result_json FROM segment_inspections WHERE action_id=? ORDER BY member_id', (request().action_id,)).fetchall()
    from camctl.contracts.enums import enum_for
    assert len(rows) == 7
    assert all(row[1] == int(enum_for('segment_inspections.state').UNCONFIRMED) for row in rows)
    from camctl.contracts.json_values import parse_exact_json
    assert all(parse_exact_json(row[2])['failure']['reason'] == 'source_canceled' for row in rows)
    assert driver.reads.offsets == [] and driver.digest_calls == 0


async def test_saved_read_end_recovers_after_process_exit_without_new_attempt(inspection_world, capture_resources, tmp_path):
    import asyncio
    import json
    import sys
    from camctl.capture.recovery import RecoveryBoundary
    from ..support.capture import capture_database
    from ..support.database import database_path
    owned, roots, _, build, supervisor, _ = inspection_world
    _, request, _, _, _ = build(segment_size=4)
    original = request()
    path = database_path(owned)
    assert not (await supervisor.drain_required()).failed
    await capture_resources.close()
    receipt = tmp_path / 'finished-read.json'
    process = await asyncio.create_subprocess_exec(sys.executable, '-m',
        'integration.support.segment_interrupted_read', str(path), str(roots.staging),
        str(original.action_id), str(original.member_id), str(original.occurred_at), str(receipt), 'finished',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert process.returncode == 0, (stdout, stderr)
    evidence = json.loads(receipt.read_text())
    assert 'read finish unavailable' in evidence['error']
    assert evidence['offsets'] == [0] and evidence['digest_calls'] == 1
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    copy_id = facts.copy.copy_id
    max_event, = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
    database = await capture_database(owned)
    flow, _, driver, digest, tools = build(database_override=database)
    flow.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
    flow.recovery_max_event_id = max_event
    resumed = await inspect_segment(flow, original)
    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert resumed.reason == 'source_file_unconfirmed'
    assert driver.offsets == [] and digest.calls == 0 and tools.requests == []
    assert owned.connection.execute('SELECT attempts_used FROM operation_runs WHERE copy_id=?', (copy_id,)).fetchone() == (1,)
    assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts WHERE run_id=(SELECT id FROM operation_runs WHERE copy_id=?)', (copy_id,)).fetchone() == (1,)
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (copy_id,)).fetchone()[0] is None


async def test_canceled_capture_releases_existing_terminal_inspection(inspection_world, capture_resources, monkeypatch):
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository
    from ..persistence.test_segment_result_versions import _cancel
    from ..support.capture import restart_capture_database
    owned, roots, _, build, supervisor, source_runtime = inspection_world
    _, request, _, _, _ = build()
    original = request()
    runtime, driver = await assembled_inspections(capture_resources, owned, roots, source_runtime)
    release = OutputsRepository.release_read_slot
    calls = []

    def unavailable(repository, command, key, connection):
        calls.append((command, key))
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError('release unavailable'))

    monkeypatch.setattr(OutputsRepository, 'release_read_slot', unavailable)
    with pytest.raises(ConsistencyError, match='原读取业务结果未可靠保存'):
        await inspect_segment(runtime.media, original)
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert facts.inspection.result_json['failure']['verification_error'] == {'reason': str({'code': 'device_error'})}
    before = owned.connection.execute('SELECT state,result_json,result_event_id FROM segment_inspections WHERE id=?', (facts.inspection.inspection_id,)).fetchone()
    assert before[2] is not None
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is not None
    assert len(calls) == 1
    assert not (await supervisor.drain_required()).failed
    await restart_capture_database(owned)
    monkeypatch.setattr(OutputsRepository, 'release_read_slot', release)
    new_runtime, new_driver = await assembled_inspections(capture_resources, owned, roots, source_runtime)
    _cancel(owned, new_runtime, original.action_id)
    await capture_handler('camera_record')(original.action_id, new_runtime)
    assert owned.connection.execute('SELECT state,result_json,result_event_id FROM segment_inspections WHERE id=?', (facts.inspection.inspection_id,)).fetchone() == before
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is None
    assert driver.reads.offsets == [0] and driver.digest_calls == 1
    assert new_driver.reads.offsets == [] and new_driver.digest_calls == 0


async def test_cancel_capture_takes_over_the_actual_completed_read(inspection_world, capture_resources, monkeypatch):
    import asyncio
    from threading import Event
    from camctl.capture.handlers import capture_handler
    from camctl.contracts.enums import enum_for
    from camctl.persistence.repositories.operations import OperationRepository
    from ..persistence.test_segment_result_versions import _cancel
    owned, roots, _, build, _, source_runtime = inspection_world
    _, request, _, _, _ = build()
    runtime, driver = await assembled_inspections(capture_resources, owned, roots, source_runtime)
    original = request()
    finish = OperationRepository.finish_attempt
    reached, release = Event(), Event()
    observed = []

    def wait_at_read_finish(repository, command, key, connection):
        if command.ticket.operation == 'read':
            observed.append((command, key))
            reached.set()
            if not release.wait(10):
                raise TimeoutError('测试没有释放原读取结果保存')
        return finish(repository, command, key, connection)

    monkeypatch.setattr(OperationRepository, 'finish_attempt', wait_at_read_finish)
    task = asyncio.create_task(inspect_segment(runtime.media, original))
    try:
        assert await asyncio.to_thread(reached.wait, 10), '实际读取未到达结束保存'
        assert driver.reads.offsets == [0] and driver.digest_calls == 1
        _cancel(owned, runtime, original.action_id)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=10)
    await capture_handler('camera_record')(original.action_id, runtime)
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert facts.inspection.state == int(enum_for('segment_inspections.state').FAILED)
    assert facts.inspection.result_json['failure']['verification_error'] == {'reason': str({'code': 'device_error'})}
    assert facts.copy.committed_bytes == len(_CONTENT)
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is None
    run = owned.connection.execute('SELECT id,status,attempts_used FROM operation_runs WHERE copy_id=?', (facts.copy.copy_id,)).fetchone()
    assert run[1:] == (int(enum_for('operation_runs.status').FAILED), 1)
    attempt = owned.connection.execute('SELECT status FROM operation_attempts WHERE run_id=?', (run[0],)).fetchone()
    assert attempt == (int(enum_for('operation_attempts.status').SUCCEEDED),)
    assert len(observed) == 1 and observed[0][0].ticket.run_id == run[0]
    assert driver.reads.offsets == [0] and driver.digest_calls == 1


async def test_host_local_recovers_original_ticket_at_eof_after_uncommitted_read_end(inspection_world, capture_resources, tmp_path):
    import asyncio
    import json
    import sys
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.contracts.enums import enum_for
    from ..support.capture import capture_database
    from ..support.database import database_path
    owned, roots, _, build, supervisor, _ = inspection_world
    _, request, _, _, _ = build(segment_size=4)
    original = request()
    path = database_path(owned)
    assert not (await supervisor.drain_required()).failed
    await capture_resources.close()
    receipt = tmp_path / 'unsaved-read-end.json'
    process = await asyncio.create_subprocess_exec(sys.executable, '-m',
        'integration.support.segment_interrupted_read', str(path), str(roots.staging),
        str(original.action_id), str(original.member_id), str(original.occurred_at), str(receipt), 'eof',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert process.returncode == 0, (stdout, stderr)
    evidence = json.loads(receipt.read_text())
    assert 'read end unavailable' in evidence['error']
    assert evidence['offsets'] == [0] and evidence['digest_calls'] == 0
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    copy_id = facts.copy.copy_id
    assert facts.copy.committed_bytes == facts.copy.source_size == len(_CONTENT)
    assert (roots.staging / facts.copy.input_relative_path).read_bytes() == _CONTENT
    assert owned.connection.execute('SELECT read_end_event_id FROM file_copies WHERE id=?', (copy_id,)).fetchone() == (None,)
    run = owned.connection.execute('SELECT id,status,attempts_used,max_attempts_used FROM operation_runs WHERE copy_id=?', (copy_id,)).fetchone()
    assert run[1:] == (int(enum_for('operation_runs.status').ACTIVE), 1, 3)
    original_attempt = owned.connection.execute('SELECT id,attempt_no,intent_event_id FROM operation_attempts WHERE run_id=?', (run[0],)).fetchone()
    max_event, = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
    database = await capture_database(owned)
    flow, _, driver, digest, tools = build(database_override=database)
    flow.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
    flow.recovery_max_event_id = max_event
    resumed = await inspect_segment(flow, original)
    assert resumed.phase is SegmentInspectionPhase.COMPLETED, resumed
    assert resumed.metric.value == Decimal('1.5')
    assert driver.offsets == [len(_CONTENT)]
    assert len(driver.streams) == 1 and driver.streams[0].read_calls == 0
    assert driver.chunk_calls == [1]
    assert digest.calls == 1 and tools.requests == [False]
    assert owned.connection.execute('SELECT id,attempt_no,intent_event_id FROM operation_attempts WHERE run_id=?', (run[0],)).fetchall() == [original_attempt]
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (copy_id,)).fetchone()[0] is None


async def test_cancel_after_read_end_before_result_is_taken_over(inspection_world, capture_resources, monkeypatch):
    import asyncio
    from threading import Event
    from camctl.capture.handlers import capture_handler
    from camctl.contracts.enums import enum_for
    from camctl.persistence.repositories import read_end
    from ..persistence.test_segment_result_versions import _cancel
    owned, roots, _, build, _, source_runtime = inspection_world
    _, request, _, _, _ = build()
    runtime, driver = await assembled_inspections(capture_resources, owned, roots, source_runtime)
    original = request()
    save = read_end.save_read_end
    reached, release = Event(), Event()
    observed = []

    def wait_at_read_end(command, key, connection):
        observed.append((command, key))
        reached.set()
        if not release.wait(10):
            raise TimeoutError('测试没有释放原读取结束保存')
        return save(command, key, connection)

    monkeypatch.setattr(read_end, 'save_read_end', wait_at_read_end)
    task = asyncio.create_task(inspect_segment(runtime.media, original))
    try:
        assert await asyncio.to_thread(reached.wait, 10), '实际读取未到达结束依据保存'
        assert driver.reads.offsets == [0] and driver.digest_calls == 0
        _cancel(owned, runtime, original.action_id)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=10)
    await capture_handler('camera_record')(original.action_id, runtime)
    facts = read_inspection_for_member(original.action_id, original.member_id, owned)
    assert facts.inspection.state == int(enum_for('segment_inspections.state').UNCONFIRMED)
    assert facts.inspection.result_json['failure'] == {'code': 'inspection_unconfirmed', 'reason': 'source_canceled'}
    assert facts.copy.committed_bytes == len(_CONTENT)
    assert owned.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (facts.copy.copy_id,)).fetchone()[0] is None
    run = owned.connection.execute('SELECT id,status,attempts_used FROM operation_runs WHERE copy_id=?', (facts.copy.copy_id,)).fetchone()
    assert run[1:] == (int(enum_for('operation_runs.status').CANCELED), 1)
    assert owned.connection.execute('SELECT status FROM operation_attempts WHERE run_id=?', (run[0],)).fetchone() == (int(enum_for('operation_attempts.status').SUCCEEDED),)
    assert len(observed) == 1 and observed[0][0].ticket.run_id == run[0]
    assert driver.reads.offsets == [0] and driver.digest_calls == 1
