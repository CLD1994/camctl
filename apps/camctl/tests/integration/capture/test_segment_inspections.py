"""逐真实分段成员建立输入副本、探测主流并恢复已保存检查事实。"""

from dataclasses import replace
from decimal import Decimal
import hashlib

import pytest

from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.segment_inspections import (
    SegmentInspectionPhase,
    SegmentInspectionRequest,
    inspect_segment,
)
from camctl.capture.video_coverage import CoverageMetric, CoverageTarget
from camctl.contracts.values import ConsistencyError
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.outputs.qualification import OperationConfig
from camctl.persistence.repositories.segment_inspections import SegmentInspectionRepository
from camctl.persistence.repositories.segment_inspections import (
    read_inspection_for_member,
)

from .test_input_copy import _CONTENT, _NOW, pipeline

pytestmark = pytest.mark.asyncio


class _Digest:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    async def read_digest(self):
        from camctl.outputs.copy import SourceDigest

        return SourceDigest(digest=self.digest, error=None)


class _Stream:
    def __init__(self, content: bytes, *, fail_after: int | None = None) -> None:
        self.content = content
        self.position = 0
        self.fail_after = fail_after

    def read(self, limit: int) -> bytes:
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

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        from camctl.devices.read_session import ReadSession

        return ReadSession(
            source, offset, _Stream(self.content[offset:], fail_after=self.fail_after),
            idle_timeout_s,
        )


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
