"""逐真实分段成员建立输入副本、探测主流并恢复已保存检查事实。"""

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.segment_inspections import (
    SegmentInspectionPhase,
    SegmentInspectionRequest,
    inspect_segment,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.outputs.qualification import OperationConfig
from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository
from camctl.persistence.repositories.segment_inspections import (
    InspectionCreate,
    read_inspection_for_member,
)

from .test_input_copy import _CONTENT, _NOW, _seed_device_file, pipeline

pytestmark = pytest.mark.asyncio


class _Digest:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0

    async def read_digest(self):
        from camctl.outputs.copy import SourceDigest

        self.calls += 1
        return SourceDigest(digest=self.digest, error=None)


class _FailedDigest:
    def __init__(self, error: str = "source SHA query failed") -> None:
        self.error = error
        self.calls = 0

    async def read_digest(self):
        from camctl.outputs.copy import SourceDigest

        self.calls += 1
        return SourceDigest(digest=None, error=self.error)


class _Stream:
    def __init__(self, content: bytes, *, fail_after: int | None = None) -> None:
        self.content = content
        self.position = 0
        self.fail_after = fail_after
        self.read_calls = 0

    def read(self, limit: int) -> bytes:
        self.read_calls += 1
        if self.fail_after is not None and self.position >= self.fail_after:
            raise OSError("injected source read interruption")
        data = self.content[self.position:self.position + limit]
        self.position += len(data)
        return data

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ReadDriver:
    def __init__(self, content: bytes, *, fail_after: int | None = None) -> None:
        self.content = content
        self.fail_after = fail_after
        self.offsets: list[int] = []
        self.streams: list[_Stream] = []
        self.chunk_calls: list[int] = []

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        from camctl.devices.read_session import ReadSession

        self.offsets.append(offset)
        stream = _Stream(self.content[offset:], fail_after=self.fail_after)
        self.streams.append(stream)
        session = ReadSession(source, offset, stream, idle_timeout_s)
        original_read_chunk = session.read_chunk

        def read_chunk(limit: int):
            self.chunk_calls.append(limit)
            return original_read_chunk(limit)

        session.read_chunk = read_chunk
        return session


class _SegmentTools:
    def __init__(self) -> None:
        self.requests: list[bool] = []
        self.probe = MediaProbe(
            duration_s=Decimal("1.5"),
            error=None,
            streams=(MediaStream(
                index=0,
                codec_type="video",
                codec_name="h264",
                width=1920,
                height=1080,
                duration_s=Decimal("1.5"),
                attached_pic=False,
                frame_count=45,
                fps=Decimal("30"),
            ),),
        )

    async def probe_segment(self, input, *, count_frames: bool) -> FileTaskResult:
        self.requests.append(count_frames)
        return FileTaskResult(FileTaskId("segment-probe"), ran=True, value=self.probe)

    async def probe(self, input) -> FileTaskResult:
        raise AssertionError("逐段检查必须调用显式主流探测入口")

    async def repair(self, input, output, *, trim_s):
        raise AssertionError("逐段检查不调用修复工具")


def _fixed_member(owned) -> None:
    connection = owned.connection
    connection.execute(
        "UPDATE actions SET execution_spec_json=? WHERE id=1",
        ('{"result_policy":"action6_video_segments_v1"}',),
    )
    connection.execute(
        "INSERT INTO segment_sets (id,created_event_id,action_id,activity_id,run_id,"
        "current_attempt_id,current_final_page_event_id,source_kind,source_txn_id,"
        "source_event_id,publication_kind,predecessor_set_id,cancel_event_id,version,"
        "target_duration_ms,state,member_count,facts_json,published_event_id,assessment_json)"
        " VALUES (1,1,1,NULL,NULL,NULL,NULL,3,1,1,1,NULL,NULL,1,1000,2,1,NULL,NULL,NULL)"
    )
    connection.execute(
        "INSERT INTO segment_members (id,created_event_id,set_id,file_id,page_event_id,"
        "file_event_id,identity,directory_number,segment_index,role,ownership,completion,"
        "pairing_state,pairing_identity,pairing_fact_id)"
        " VALUES (1,1,1,11,1,1,'file-0011',1,1,1,1,1,1,NULL,NULL)"
    )
    connection.commit()


def _inspection_runtime(owned, roots, digest, *, driver=None, **overrides):
    tools = _SegmentTools()
    read_driver = driver or _ReadDriver(_CONTENT)
    values = dict(
        owned=owned,
        roots=roots,
        sessions=DriverReadSessions(owned, read_driver, ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=lambda: _NOW + 10,
        digest_supported=True,
        digest=digest,
        segment_size=4,
        max_read_attempts=3,
        retry_interval_s=Decimal("0"),
        read_idle_timeout_s=Decimal("10"),
    )
    values.update(overrides)
    flow = MediaFlow(**values)
    request = SegmentInspectionRequest(
        1, 1, CoverageTarget(CoverageMetric.DURATION_S, 60),
        OperationConfig(3, Decimal("10"), Decimal("0")), _NOW + 10,
    )
    return flow, request, read_driver, tools


async def _leave_source_digest_failure_with_held_end(pipeline, monkeypatch):
    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    digest = _FailedDigest()
    flow, request, driver, tools = _inspection_runtime(owned, roots, digest)
    monkeypatch.setattr(
        "camctl.capture.segment_inspections._finish_internal_read",
        lambda *_args, **_kwargs: None,
    )
    first = await inspect_segment(flow, request)
    monkeypatch.undo()
    assert first.phase is SegmentInspectionPhase.WAITING
    assert first.reason == "verification_failed"
    assert flow.pending_read_ends
    copy_id = read_inspection_for_member(1, 1, owned).copy.copy_id
    run = owned.connection.execute(
        "SELECT status FROM operation_runs WHERE copy_id=?", (copy_id,),
    ).fetchone()
    assert run[0] == int(enum_for("operation_runs.status").ACTIVE)
    return owned, roots, flow, request, driver, digest, tools, copy_id


def _cold_inspection_flow(flow, owned, roots, driver, *, recovery_boundary,
                          recovery_max_event_id=None):
    return replace(
        flow,
        owned=owned,
        sessions=DriverReadSessions(owned, driver, ticket=None),
        pending_read_results={},
        pending_read_business={},
        pending_read_ends={},
        pending_segment_inspection_results={},
        recovery_boundary=recovery_boundary,
        recovery_max_event_id=recovery_max_event_id,
    )


@pytest.mark.parametrize(
    ("metric", "expected", "count_frames"),
    [
        (CoverageMetric.DURATION_S, Decimal("1.5"), False),
        (CoverageMetric.FRAMES, 45, True),
    ],
)
async def test_inspection_copies_full_member_then_reuses_saved_metric(
    pipeline, metric, expected, count_frames,
) -> None:
    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    tools = _SegmentTools()
    flow = MediaFlow(
        owned=owned,
        roots=roots,
        sessions=DriverReadSessions(owned, _ReadDriver(_CONTENT), ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=lambda: _NOW + 10,
        digest_supported=True,
        digest=_Digest(hashlib.sha256(_CONTENT).hexdigest()),
    )
    request = SegmentInspectionRequest(
        action_id=1,
        member_id=1,
        target=CoverageTarget(metric, 60),
        budget=OperationConfig(3, Decimal("10"), Decimal("0")),
        occurred_at=_NOW + 10,
    )

    first = await inspect_segment(flow, request)
    assert first.phase is SegmentInspectionPhase.COMPLETED, first
    assert first.metric is not None and first.metric.value == expected
    assert tools.requests == [count_frames]

    facts = read_inspection_for_member(1, 1, owned)
    assert facts.inspection is not None and facts.inspection.state == 2
    assert facts.copy is not None
    assert facts.copy.committed_bytes == len(_CONTENT)
    assert facts.copy.target_sha256 == hashlib.sha256(_CONTENT).hexdigest()
    assert (roots.staging / facts.copy.input_relative_path).read_bytes() == _CONTENT

    resumed = await inspect_segment(flow, request)
    assert resumed.phase is SegmentInspectionPhase.COMPLETED
    assert resumed.metric == first.metric
    assert tools.requests == [count_frames]

    with pytest.raises(ConsistencyError, match="不能替换来源、目标或原预算"):
        await inspect_segment(flow, replace(
            request, target=CoverageTarget(metric, 61)
        ))
    with pytest.raises(ConsistencyError, match="不能替换来源、目标或原预算"):
        await inspect_segment(flow, replace(
            request, budget=OperationConfig(4, Decimal("10"), Decimal("0"))
        ))
    assert tools.requests == [count_frames]


async def test_two_actual_members_keep_independent_inspection_and_copy_owners(pipeline):
    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    from .test_input_copy import _seed_device_file

    _seed_device_file(owned.connection, 12)
    owned.connection.execute(
        "INSERT INTO segment_members (id,created_event_id,set_id,file_id,page_event_id,"
        "file_event_id,identity,directory_number,segment_index,role,ownership,completion,"
        "pairing_state,pairing_identity,pairing_fact_id)"
        " VALUES (2,1,1,12,1,1,'file-0012',1,2,1,1,1,1,NULL,NULL)"
    )
    owned.connection.commit()
    tools = _SegmentTools()
    flow = MediaFlow(
        owned=owned,
        roots=roots,
        sessions=DriverReadSessions(owned, _ReadDriver(_CONTENT), ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=lambda: _NOW + 10,
        digest_supported=True,
        digest=_Digest(hashlib.sha256(_CONTENT).hexdigest()),
    )
    budget = OperationConfig(3, Decimal("10"), Decimal("0"))

    first = await inspect_segment(flow, SegmentInspectionRequest(
        1, 1, CoverageTarget(CoverageMetric.DURATION_S, 60), budget, _NOW + 10,
    ))
    second = await inspect_segment(flow, SegmentInspectionRequest(
        1, 2, CoverageTarget(CoverageMetric.DURATION_S, 60), budget, _NOW + 10,
    ))

    first_facts = read_inspection_for_member(1, 1, owned)
    second_facts = read_inspection_for_member(1, 2, owned)
    assert first.phase is second.phase is SegmentInspectionPhase.COMPLETED
    assert first.inspection_id != second.inspection_id
    assert first.copy_id != second.copy_id
    assert first_facts.inspection.source_device_file_id == 11
    assert second_facts.inspection.source_device_file_id == 12
    assert first_facts.copy.inspection_id == first.inspection_id
    assert second_facts.copy.inspection_id == second.inspection_id
    assert first_facts.copy.source_size == second_facts.copy.source_size == len(_CONTENT)
    assert tools.requests == [False, False]


async def test_partial_copy_is_not_probed_until_complete(pipeline):
    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    tools = _SegmentTools()
    flow = MediaFlow(
        owned=owned,
        roots=roots,
        sessions=DriverReadSessions(
            owned, _ReadDriver(_CONTENT, fail_after=4), ticket=None,
        ),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=lambda: _NOW + 10,
        digest_supported=True,
        digest=_Digest(hashlib.sha256(_CONTENT).hexdigest()),
        segment_size=4,
    )
    step = await inspect_segment(flow, SegmentInspectionRequest(
        1, 1,
        CoverageTarget(CoverageMetric.DURATION_S, 60),
        OperationConfig(3, Decimal("10"), Decimal("0")),
        _NOW + 10,
    ))

    assert step.phase is SegmentInspectionPhase.WAITING
    assert tools.requests == []
    copy = read_inspection_for_member(1, 1, owned).copy
    assert copy is not None
    assert 0 < copy.committed_bytes < copy.source_size
    assert copy.verification_state not in (3, 5)


async def test_source_digest_failure_finishes_owned_inspection_after_clean_read(pipeline):
    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    digest = _FailedDigest()
    flow, request, driver, tools = _inspection_runtime(owned, roots, digest)

    step = await inspect_segment(flow, request)

    assert step.phase is SegmentInspectionPhase.FAILED
    assert step.reason == "source_file_unconfirmed"
    facts = read_inspection_for_member(1, 1, owned)
    assert facts.inspection is not None
    assert facts.inspection.state == int(enum_for("segment_inspections.state").FAILED)
    result = facts.inspection.result_json
    assert result["metric"] == {
        "metric": "duration_s", "value": None, "reason": "source_file_unconfirmed",
    }
    assert result["probe"] is None
    assert result["tool"] == {"ran": False, "error": None}
    assert result["failure"] == {
        "code": "read_failed",
        "reason": "source_file_unconfirmed",
        "read_error": {
            "code": "source_file_unconfirmed", "stage": "source_read", "details": {},
        },
        "verification_error": {"reason": "source SHA query failed"},
    }

    run = owned.connection.execute(
        "SELECT status,error_json,attempts_used,retry_wait_required"
        " FROM operation_runs WHERE copy_id=?", (facts.copy.copy_id,),
    ).fetchone()
    assert run[0] == int(enum_for("operation_runs.status").FAILED)
    assert json.loads(run[1]) == result["failure"]["read_error"]
    assert run[2:] == (1, 0)
    attempts = owned.connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id=("
        " SELECT id FROM operation_runs WHERE copy_id=?)", (facts.copy.copy_id,),
    ).fetchall()
    assert attempts == [(int(enum_for("operation_attempts.status").SUCCEEDED),)]
    copy = owned.connection.execute(
        "SELECT slot_device_id,committed_bytes,source_size,round,recopies_used,"
        "verification_state,verification_error_json FROM file_copies WHERE id=?",
        (facts.copy.copy_id,),
    ).fetchone()
    assert copy[0] is None
    assert copy[1:6] == (
        len(_CONTENT), len(_CONTENT), 1, 0,
        int(enum_for("file_copies.verification_state").FAILED),
    )
    assert json.loads(copy[6]) == {"reason": "source SHA query failed"}
    assert driver.offsets == [0]
    assert digest.calls == 1
    assert tools.requests == []


async def test_held_clean_end_recovers_inspection_locally_without_read_or_sha_retry(
    pipeline, monkeypatch,
):
    owned, roots, flow, request, driver, digest, tools, copy_id = (
        await _leave_source_digest_failure_with_held_end(pipeline, monkeypatch)
    )

    resumed = await inspect_segment(flow, request)

    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert resumed.reason == "source_file_unconfirmed"
    assert driver.offsets == [0]
    assert digest.calls == 1
    assert tools.requests == []
    run = owned.connection.execute(
        "SELECT status,attempts_used FROM operation_runs WHERE copy_id=?", (copy_id,),
    ).fetchone()
    assert run == (int(enum_for("operation_runs.status").FAILED), 1)
    attempt = owned.connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id=("
        "SELECT id FROM operation_runs WHERE copy_id=?)", (copy_id,),
    ).fetchone()
    assert attempt[0] == int(enum_for("operation_attempts.status").SUCCEEDED)
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,),
    ).fetchone()[0] is None


async def test_unconfirmed_cross_process_boundary_keeps_running_read_and_slot(
    pipeline, monkeypatch,
):
    owned, roots, flow, request, _driver, digest, _tools, copy_id = (
        await _leave_source_digest_failure_with_held_end(pipeline, monkeypatch)
    )
    flow.pending_read_ends.clear()
    database_path = Path(owned.connection.execute(
        "PRAGMA database_list",
    ).fetchone()[2])
    owned.connection.close()
    reopened = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
    driver = _ReadDriver(_CONTENT)
    cold = _cold_inspection_flow(
        flow, reopened, roots, driver,
        recovery_boundary=RecoveryBoundary.UNCONFIRMED,
    )

    resumed = await inspect_segment(cold, request)

    assert resumed.phase is SegmentInspectionPhase.WAITING
    assert resumed.reason == "recovery_blocked"
    assert driver.offsets == []
    assert digest.calls == 1
    inspection = read_inspection_for_member(1, 1, reopened)
    assert inspection.inspection.state == int(enum_for("segment_inspections.state").PENDING)
    assert reopened.connection.execute(
        "SELECT status FROM operation_runs WHERE copy_id=?", (copy_id,),
    ).fetchone()[0] == int(enum_for("operation_runs.status").ACTIVE)
    assert reopened.connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id=("
        "SELECT id FROM operation_runs WHERE copy_id=?)", (copy_id,),
    ).fetchone()[0] == int(enum_for("operation_attempts.status").RUNNING)
    assert reopened.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,),
    ).fetchone()[0] is not None
    reopened.connection.close()


async def test_host_local_settled_resumes_original_ticket_at_eof_without_sha_retry(
    pipeline, monkeypatch,
):
    owned, roots, flow, request, _driver, digest, tools, copy_id = (
        await _leave_source_digest_failure_with_held_end(pipeline, monkeypatch)
    )
    flow.pending_read_ends.clear()
    database_path = Path(owned.connection.execute(
        "PRAGMA database_list",
    ).fetchone()[2])
    max_event_id = owned.connection.execute(
        "SELECT MAX(id) FROM history_events",
    ).fetchone()[0]
    owned.connection.close()
    reopened = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
    driver = _ReadDriver(_CONTENT)
    cold = _cold_inspection_flow(
        flow, reopened, roots, driver,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=max_event_id,
    )

    resumed = await inspect_segment(cold, request)

    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert resumed.reason == "source_file_unconfirmed"
    assert driver.offsets == [len(_CONTENT)]
    assert len(driver.streams) == 1
    assert driver.chunk_calls == [1]
    assert driver.streams[0].read_calls == 0
    assert driver.streams[0].position == 0
    assert digest.calls == 1
    assert tools.requests == []
    run = reopened.connection.execute(
        "SELECT attempts_used,status,error_json FROM operation_runs WHERE copy_id=?",
        (copy_id,),
    ).fetchone()
    assert run[:2] == (1, int(enum_for("operation_runs.status").FAILED))
    assert json.loads(run[2]) == {
        "code": "source_file_unconfirmed", "stage": "source_read", "details": {},
    }
    assert reopened.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id=("
        "SELECT id FROM operation_runs WHERE copy_id=?)", (copy_id,),
    ).fetchone()[0] == 1
    assert reopened.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,),
    ).fetchone()[0] is None
    reopened.connection.close()


async def test_failed_inspection_result_unknown_replays_original_key_once(pipeline, monkeypatch):
    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    digest = _FailedDigest()
    flow, request, _driver, _tools = _inspection_runtime(owned, roots, digest)
    original_save = SegmentInspectionRepository.save_result
    keys = []

    def commit_then_report_unknown(repository, save, key, connection):
        keys.append(key)
        outcome = original_save(repository, save, key, connection)
        if len(keys) == 1:
            assert outcome.kind is DbOutcomeKind.COMPLETED
            return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError("commit reply lost"))
        return outcome

    monkeypatch.setattr(
        SegmentInspectionRepository, "save_result", commit_then_report_unknown,
    )
    first = await inspect_segment(flow, request)
    assert first.phase is SegmentInspectionPhase.WAITING
    assert first.reason == "result_save_unknown"
    first_row = owned.connection.execute(
        "SELECT state,result_event_id FROM segment_inspections WHERE id=1",
    ).fetchone()
    assert first_row[0] == int(enum_for("segment_inspections.state").FAILED)
    assert first_row[1] is not None
    copy_id = read_inspection_for_member(1, 1, owned).copy.copy_id
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,),
    ).fetchone()[0] is not None

    resumed = await inspect_segment(flow, request)

    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert resumed.reason == "source_file_unconfirmed"
    assert len(keys) == 2 and keys[0] == keys[1]
    assert owned.connection.execute(
        "SELECT state,result_event_id FROM segment_inspections WHERE id=1",
    ).fetchone() == first_row
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,),
    ).fetchone()[0] is None
    assert digest.calls == 1


async def test_read_slot_release_unknown_replays_same_key(pipeline, monkeypatch):
    from camctl.persistence.repositories.outputs import OutputsRepository

    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    flow, request, _driver, _tools = _inspection_runtime(owned, roots, _FailedDigest())
    original_release = OutputsRepository.release_read_slot
    keys = []

    def commit_then_report_unknown(repository, save, key, connection):
        keys.append(key)
        outcome = original_release(repository, save, key, connection)
        if len(keys) == 1:
            assert outcome.kind is DbOutcomeKind.COMPLETED
            return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError("commit reply lost"))
        return outcome

    monkeypatch.setattr(
        OutputsRepository, "release_read_slot", commit_then_report_unknown,
    )
    with pytest.raises(ConsistencyError, match="原读取业务结果未可靠保存"):
        await inspect_segment(flow, request)
    facts = read_inspection_for_member(1, 1, owned)
    assert facts.inspection.state == int(enum_for("segment_inspections.state").FAILED)
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (facts.copy.copy_id,),
    ).fetchone()[0] is None

    resumed = await inspect_segment(flow, request)

    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert len(keys) == 2 and keys[0] == keys[1]
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (facts.copy.copy_id,),
    ).fetchone()[0] is None


async def test_failed_read_without_process_local_finish_resumes_source_failure_after_reopen(
    pipeline, monkeypatch,
):
    from camctl.persistence.repositories.outputs import OutputsRepository

    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    digest = _FailedDigest()
    flow, request, driver, tools = _inspection_runtime(owned, roots, digest)
    monkeypatch.setattr(
        "camctl.capture.segment_inspections.settle_inspection_read_failure",
        lambda *_args, **_kwargs: False,
    )
    first = await inspect_segment(flow, request)
    monkeypatch.undo()
    assert first.phase is SegmentInspectionPhase.WAITING
    run = owned.connection.execute(
        "SELECT status,error_json FROM operation_runs WHERE copy_id=1",
    ).fetchone()
    assert run[0] == int(enum_for("operation_runs.status").FAILED)
    assert json.loads(run[1])["code"] == "source_file_unconfirmed"
    assert owned.connection.execute(
        "SELECT state FROM segment_inspections WHERE id=1",
    ).fetchone()[0] == int(enum_for("segment_inspections.state").PENDING)
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=1",
    ).fetchone()[0] is not None

    database_path = Path(owned.connection.execute(
        "PRAGMA database_list",
    ).fetchone()[2])
    flow.pending_read_results.clear()
    flow.pending_read_ends.clear()
    flow.pending_segment_inspection_results.clear()
    flow.pending_read_business.clear()
    owned.connection.close()
    reopened = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
    cold = _cold_inspection_flow(
        flow, reopened, roots, _ReadDriver(_CONTENT),
        recovery_boundary=RecoveryBoundary.UNCONFIRMED,
    )

    resumed = await inspect_segment(cold, request)

    assert resumed.phase is SegmentInspectionPhase.FAILED
    assert resumed.reason == "source_file_unconfirmed"
    facts = read_inspection_for_member(1, 1, reopened)
    assert facts.inspection.state == int(enum_for("segment_inspections.state").FAILED)
    assert facts.inspection.result_json["failure"] == {
        "code": "read_failed",
        "reason": "source_file_unconfirmed",
        "read_error": json.loads(run[1]),
        "verification_error": {"reason": "source SHA query failed"},
    }
    assert json.loads(reopened.connection.execute(
        "SELECT verification_error_json FROM file_copies WHERE id=1",
    ).fetchone()[0]) == {"reason": "source SHA query failed"}
    assert reopened.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id=("
        "SELECT id FROM operation_runs WHERE copy_id=1)",
    ).fetchone()[0] == 1
    assert reopened.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=1",
    ).fetchone()[0] is None
    assert digest.calls == 1
    assert driver.offsets == [0]
    assert tools.requests == []
    reopened.connection.close()


async def test_action6_finalizer_releases_slot_for_existing_terminal_inspection(
    pipeline, monkeypatch,
):
    from camctl.capture.action6_recovery import finalize_pending_action6_inspections

    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    digest = _FailedDigest()
    flow, request, driver, tools = _inspection_runtime(owned, roots, digest)
    monkeypatch.setattr(
        "camctl.capture.segment_inspections.settle_terminal_inspection_read_slot",
        lambda *_args, **_kwargs: False,
    )

    first = await inspect_segment(flow, request)
    assert first.phase is SegmentInspectionPhase.FAILED
    facts = read_inspection_for_member(1, 1, owned)
    result_event_id = owned.connection.execute(
        "SELECT result_event_id FROM segment_inspections WHERE id=?",
        (facts.inspection.inspection_id,),
    ).fetchone()[0]
    assert result_event_id is not None
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (facts.copy.copy_id,),
    ).fetchone()[0] is not None
    monkeypatch.undo()

    database_path = Path(owned.connection.execute(
        "PRAGMA database_list",
    ).fetchone()[2])
    owned.connection.close()
    reopened = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
    recovery_driver = _ReadDriver(_CONTENT)
    cold = _cold_inspection_flow(
        flow, reopened, roots, recovery_driver,
        recovery_boundary=RecoveryBoundary.UNCONFIRMED,
    )
    runtime = SimpleNamespace(
        owned=reopened, media=cold, wall_us=lambda: _NOW + 20,
    )

    assert finalize_pending_action6_inspections(
        runtime, 1, reason="source_terminal",
    )

    after = read_inspection_for_member(1, 1, reopened)
    assert after.inspection.state == int(enum_for("segment_inspections.state").FAILED)
    assert after.inspection.result_json == facts.inspection.result_json
    assert reopened.connection.execute(
        "SELECT result_event_id FROM segment_inspections WHERE id=?",
        (after.inspection.inspection_id,),
    ).fetchone()[0] == result_event_id
    assert reopened.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (facts.copy.copy_id,),
    ).fetchone()[0] is None
    assert recovery_driver.offsets == []
    assert driver.offsets == [0]
    assert digest.calls == 1
    assert tools.requests == []
    reopened.connection.close()


async def test_action6_inspection_recovery_pages_and_retries_exact_pending_keys(
    pipeline, monkeypatch,
):
    from camctl.capture import segment_inspections
    from camctl.capture.action6_recovery import finalize_pending_action6_inspections
    from camctl.outputs.read_attempts import PendingReadBusiness
    from camctl.outputs.slots import SlotRequest
    from camctl.persistence.repositories.outputs import OutputsRepository

    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    connection = owned.connection
    for member_id in range(2, 8):
        file_id = 10 + member_id
        _seed_device_file(connection, file_id)
        connection.execute(
            "INSERT INTO segment_members (id,created_event_id,set_id,file_id,page_event_id,"
            "file_event_id,identity,directory_number,segment_index,role,ownership,completion,"
            "pairing_state,pairing_identity,pairing_fact_id)"
            " VALUES (?,1,1,?,1,1,?,1,?,1,1,1,1,NULL,NULL)",
            (member_id, file_id, f"file-{file_id:04d}", member_id),
        )
    connection.execute("UPDATE segment_sets SET member_count=7 WHERE id=1")
    connection.commit()

    result_repository = SegmentInspectionRepository()
    target = {"metric": "duration_s", "value": 60}
    budget = {"max_attempts": 3, "timeout_s": 10, "retry_interval_s": 0}
    for member_id in range(1, 8):
        created = result_repository.create(
            InspectionCreate(1, member_id, target, budget, _NOW + 10),
            new_operation_key(), owned,
        )
        assert created.kind is DbOutcomeKind.COMPLETED, created.error

    flow, request, driver, tools = _inspection_runtime(owned, roots, _FailedDigest())
    flow.read_discovery_batch_size = 2
    copy_ids = {}

    release_read_slot = OutputsRepository.release_read_slot
    release_keys_by_copy = {}
    unknown_release_seen = False

    def commit_then_report_release_unknown(repository, save, key, owned_connection):
        nonlocal unknown_release_seen
        release_keys_by_copy.setdefault(save.copy_id, []).append(key)
        outcome = release_read_slot(repository, save, key, owned_connection)
        if not unknown_release_seen:
            unknown_release_seen = True
            assert outcome.kind is DbOutcomeKind.COMPLETED
            return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError("commit reply lost"))
        return outcome

    monkeypatch.setattr(
        OutputsRepository, "release_read_slot", commit_then_report_release_unknown,
    )
    with pytest.raises(ConsistencyError, match="原读取业务结果未可靠保存"):
        await inspect_segment(flow, replace(request, member_id=1))
    copy_ids[1] = connection.execute(
        "SELECT id FROM file_copies WHERE inspection_id=("
        " SELECT id FROM segment_inspections WHERE action_id=1 AND member_id=1)",
    ).fetchone()[0]
    original_failures = {
        1: connection.execute(
            "SELECT r.error_json,c.verification_error_json,s.result_json,s.result_event_id"
            " FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
            " JOIN segment_inspections s ON s.id=c.inspection_id WHERE c.id=?",
            (copy_ids[1],),
        ).fetchone(),
    }

    drivers = [driver]
    digests = [flow.digest]
    tools_list = [tools]
    for member_id in (3, 7):
        other_flow, other_request, other_driver, other_tools = _inspection_runtime(
            owned, roots, _FailedDigest(),
        )
        other_flow.read_discovery_batch_size = 2
        result = await inspect_segment(
            other_flow, replace(other_request, member_id=member_id),
        )
        assert result.phase is SegmentInspectionPhase.FAILED
        assert result.reason == "source_file_unconfirmed"
        drivers.append(other_driver)
        digests.append(other_flow.digest)
        tools_list.append(other_tools)
        copy_ids[member_id] = connection.execute(
            "SELECT id FROM file_copies WHERE inspection_id=("
            " SELECT id FROM segment_inspections WHERE action_id=1 AND member_id=?)",
            (member_id,),
        ).fetchone()[0]
        original_failures[member_id] = connection.execute(
            "SELECT r.error_json,c.verification_error_json,s.result_json,s.result_event_id"
            " FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
            " JOIN segment_inspections s ON s.id=c.inspection_id WHERE c.id=?",
            (copy_ids[member_id],),
        ).fetchone()

    active_flow, active_request, active_driver, active_tools = _inspection_runtime(
        owned, roots, _FailedDigest(),
    )
    original_finish = segment_inspections._finish_internal_read
    monkeypatch.setattr(segment_inspections, "_finish_internal_read",
                        lambda *_args, **_kwargs: None)
    active = await inspect_segment(active_flow, replace(active_request, member_id=5))
    monkeypatch.setattr(segment_inspections, "_finish_internal_read", original_finish)
    assert active.phase is SegmentInspectionPhase.WAITING
    assert active.reason == "verification_failed"
    copy_ids[5] = connection.execute(
        "SELECT id FROM file_copies WHERE inspection_id=("
        " SELECT id FROM segment_inspections WHERE action_id=1 AND member_id=5)",
    ).fetchone()[0]
    drivers.append(active_driver)
    digests.append(active_flow.digest)
    tools_list.append(active_tools)

    release_unknown_key = release_keys_by_copy[copy_ids[1]][0]
    assert connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_ids[1],),
    ).fetchone()[0] is None
    assert (copy_ids[1], "release_read_slot") in flow.pending_read_business

    connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=1")
    connection.commit()

    save_result = SegmentInspectionRepository.save_result
    result_keys = []
    unknown_result_seen = False

    def commit_then_report_result_unknown(repository, save, key, owned_connection):
        nonlocal unknown_result_seen
        result_keys.append((save.inspection_id, key))
        outcome = save_result(repository, save, key, owned_connection)
        if save.inspection_id == 5 and not unknown_result_seen:
            unknown_result_seen = True
            assert outcome.kind is DbOutcomeKind.COMPLETED
            return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError("commit reply lost"))
        return outcome

    monkeypatch.setattr(
        SegmentInspectionRepository, "save_result", commit_then_report_result_unknown,
    )
    runtime = SimpleNamespace(
        owned=owned, media=flow, pending_read_business=flow.pending_read_business,
        wall_us=lambda: _NOW + 20,
    )
    statements = []
    connection.set_trace_callback(statements.append)

    assert not finalize_pending_action6_inspections(
        runtime, 1, reason="inspection_canceled",
    )
    result_event_id = connection.execute(
        "SELECT result_event_id FROM segment_inspections WHERE action_id=1 AND member_id=5",
    ).fetchone()[0]
    assert result_event_id is not None
    assert connection.execute(
        "SELECT state FROM segment_inspections WHERE action_id=1 AND member_id=6",
    ).fetchone()[0] == int(enum_for("segment_inspections.state").PENDING)

    event_count_before_retry = connection.execute(
        "SELECT COUNT(*) FROM history_events",
    ).fetchone()[0]

    def unrelated_save(*_args):
        raise AssertionError("Action6 inspection recovery touched an unrelated read release")

    unrelated = PendingReadBusiness(
        unrelated_save, SlotRequest(999, _NOW + 20), new_operation_key(),
    )
    flow.pending_read_business[(999, "release_read_slot")] = unrelated

    assert finalize_pending_action6_inspections(
        runtime, 1, reason="inspection_canceled",
    )
    connection.set_trace_callback(None)

    pending_pages = [
        statement for statement in statements
        if statement.lstrip().upper().startswith("SELECT ID FROM SEGMENT_INSPECTIONS")
        and "ACTION_ID" in statement.upper() and "STATE" in statement.upper()
    ]
    slot_pages = [
        statement for statement in statements
        if statement.lstrip().upper().startswith("SELECT S.ID,C.ID FROM SEGMENT_INSPECTIONS")
    ]
    assert len(pending_pages) >= 4
    assert all("LIMIT 2" in statement.upper() for statement in pending_pages)
    assert any("ID>4" in statement.upper() for statement in pending_pages)
    assert len(slot_pages) >= 2
    assert all("LIMIT 2" in statement.upper() for statement in slot_pages)

    id5_keys = [key for inspection_id, key in result_keys if inspection_id == 5]
    assert len(id5_keys) == 2 and id5_keys[0] == id5_keys[1]
    assert connection.execute(
        "SELECT COUNT(*) FROM history_events WHERE id=?", (result_event_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM history_events",
    ).fetchone()[0] == event_count_before_retry + 1
    assert connection.execute(
        "SELECT state FROM segment_inspections WHERE action_id=1 AND state=?",
        (int(enum_for("segment_inspections.state").PENDING),),
    ).fetchone() is None
    for member_id in (1, 3, 7):
        after = connection.execute(
            "SELECT r.error_json,c.verification_error_json,s.result_json,s.result_event_id"
            " FROM file_copies c"
            " JOIN operation_runs r ON r.copy_id=c.id"
            " JOIN segment_inspections s ON s.id=c.inspection_id WHERE c.id=?",
            (copy_ids[member_id],),
        ).fetchone()
        before = original_failures[member_id]
        assert after == before
        assert connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE id=?",
            (copy_ids[member_id],),
        ).fetchone()[0] is None
    assert release_keys_by_copy[copy_ids[1]][0] == release_unknown_key
    assert release_keys_by_copy[copy_ids[1]][-1] == release_unknown_key
    assert (999, "release_read_slot") in flow.pending_read_business
    assert flow.pending_read_business[(999, "release_read_slot")] is unrelated
    assert connection.execute(
        "SELECT status FROM operation_runs WHERE copy_id=?", (copy_ids[5],),
    ).fetchone()[0] == int(enum_for("operation_runs.status").ACTIVE)
    assert connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id=("
        " SELECT id FROM operation_runs WHERE copy_id=?)", (copy_ids[5],),
    ).fetchone()[0] == int(enum_for("operation_attempts.status").RUNNING)
    assert connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_ids[5],),
    ).fetchone()[0] is not None
    assert [offset for driver in drivers for offset in driver.offsets] == [0, 0, 0, 0]
    assert sum(digest.calls for digest in digests) == 4
    assert all(not tools.requests for tools in tools_list)


async def test_cancelled_complete_read_uses_existing_action6_inspection_finalizer(
    pipeline, monkeypatch,
):
    from camctl.capture.action6_recovery import finalize_pending_action6_inspections

    owned, roots, flow, request, driver, digest, tools, copy_id = (
        await _leave_source_digest_failure_with_held_end(pipeline, monkeypatch)
    )
    owned.connection.execute(
        "UPDATE actions SET cancel_requested=1 WHERE id=1",
    )
    owned.connection.commit()

    waiting = await inspect_segment(flow, request)

    assert waiting.phase is SegmentInspectionPhase.WAITING
    assert waiting.reason == "inspection_canceled"
    run = owned.connection.execute(
        "SELECT status,error_json FROM operation_runs WHERE copy_id=?", (copy_id,),
    ).fetchone()
    assert run[0] == int(enum_for("operation_runs.status").CANCELED)
    assert run[1] is None
    attempt = owned.connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id=("
        "SELECT id FROM operation_runs WHERE copy_id=?)", (copy_id,),
    ).fetchone()
    assert attempt[0] == int(enum_for("operation_attempts.status").SUCCEEDED)
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,),
    ).fetchone()[0] is None
    assert read_inspection_for_member(1, 1, owned).inspection.state == int(
        enum_for("segment_inspections.state").PENDING)

    runtime = SimpleNamespace(
        owned=owned, media=flow, wall_us=lambda: _NOW + 20,
    )
    assert finalize_pending_action6_inspections(
        runtime, 1, reason="inspection_canceled",
    )
    final = await inspect_segment(flow, request)

    assert final.phase is SegmentInspectionPhase.UNCONFIRMED
    assert final.reason == "inspection_canceled"
    facts = read_inspection_for_member(1, 1, owned)
    assert facts.inspection.result_json["failure"] == {
        "code": "inspection_unconfirmed", "reason": "inspection_canceled",
    }
    assert driver.offsets == [0]
    assert digest.calls == 1
    assert tools.requests == []


async def test_only_actual_exhausted_read_uses_budget_exhausted_reason(pipeline):
    owned, roots = pipeline[0], pipeline[1]
    _fixed_member(owned)
    digest = _FailedDigest()
    driver = _ReadDriver(_CONTENT, fail_after=0)
    flow, request, driver, tools = _inspection_runtime(
        owned, roots, digest, driver=driver,
    )
    request = replace(
        request,
        budget=OperationConfig(1, Decimal("10"), Decimal("0")),
    )

    step = await inspect_segment(flow, request)

    assert step.phase is SegmentInspectionPhase.FAILED
    assert step.reason == "read_attempts_exhausted"
    facts = read_inspection_for_member(1, 1, owned)
    assert facts.inspection.result_json["metric"] == {
        "metric": "duration_s", "value": None, "reason": "read_attempts_exhausted",
    }
    assert facts.inspection.result_json["failure"]["reason"] == "read_attempts_exhausted"
    assert digest.calls == 0
    assert tools.requests == []
    assert driver.offsets == [0]
    run = owned.connection.execute(
        "SELECT status,attempts_used,retry_wait_required,error_json FROM operation_runs"
        " WHERE copy_id=?", (facts.copy.copy_id,),
    ).fetchone()
    assert run[:3] == (int(enum_for("operation_runs.status").FAILED), 1, 0)
    assert json.loads(run[3])["code"] == "read_attempts_exhausted"
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (facts.copy.copy_id,),
    ).fetchone()[0] is None


async def test_saved_probe_result_unknown_is_recovered_after_database_reopen(
    pipeline, monkeypatch, tmp_path,
):
    owned, roots = pipeline[0], pipeline[1]
    database_path = tmp_path / "state.db"
    _fixed_member(owned)
    tools = _SegmentTools()
    flow = MediaFlow(
        owned=owned,
        roots=roots,
        sessions=DriverReadSessions(owned, _ReadDriver(_CONTENT), ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=lambda: _NOW + 10,
        digest_supported=True,
        digest=_Digest(hashlib.sha256(_CONTENT).hexdigest()),
    )
    request = SegmentInspectionRequest(
        1, 1,
        CoverageTarget(CoverageMetric.DURATION_S, 60),
        OperationConfig(3, Decimal("10"), Decimal("0")),
        _NOW + 10,
    )
    original_save = SegmentInspectionRepository.save_result

    def commit_then_report_unknown(repository, save, key, connection):
        outcome = original_save(repository, save, key, connection)
        assert outcome.kind is DbOutcomeKind.COMPLETED
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=OSError("commit reply lost"))

    monkeypatch.setattr(
        SegmentInspectionRepository, "save_result", commit_then_report_unknown,
    )
    first = await inspect_segment(flow, request)
    assert first.phase is SegmentInspectionPhase.WAITING
    assert first.reason == "result_save_unknown"
    assert tools.requests == [False]

    owned.connection.close()
    reopened = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
    cold_flow = replace(
        flow,
        owned=reopened,
        sessions=DriverReadSessions(reopened, _ReadDriver(_CONTENT), ticket=None),
        pending_segment_inspection_results={},
    )
    monkeypatch.setattr(SegmentInspectionRepository, "save_result", original_save)
    resumed = await inspect_segment(cold_flow, request)
    assert resumed.phase is SegmentInspectionPhase.COMPLETED
    assert resumed.metric == first.metric
    assert tools.requests == [False]
    assert read_inspection_for_member(1, 1, reopened).inspection.state == 2
    reopened.connection.close()
