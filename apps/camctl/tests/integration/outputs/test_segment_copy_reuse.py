"""检查副本复用的取回等待集合固定在来源发布事务 H。"""

from decimal import Decimal
import hashlib
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import _stop_call
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.segment_inspections import (
    SegmentInspectionPhase,
    SegmentInspectionRequest,
    inspect_segment,
)
from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.devices.read_session import ReadChunk
from camctl.devices.read_session import ReadSession
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.io import LocalSourceReader
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskId, FileTaskResult, FileTaskExecutor
from camctl.outputs.segment_copy_reuse import bound_waiters_at_publication
from camctl.outputs.obtain_flow import (
    DeviceReadAssembly,
    ObtainRuntime,
    _action_facts,
    _delivery_candidate,
    advance_obtain,
)
import camctl.outputs.obtain_flow as obtain_flow
from camctl.outputs.handoff import DeliveryDirectories
from camctl.operations.attempts import RetryWaitGate
from camctl.outputs.work_files import (
    WorkFileContext,
    WorkFileLimits,
    WorkFileSingleOutcome,
    clean_one_work_file,
)
from camctl.outputs.qualification import OperationConfig
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.segment_selections import (
    FixSegmentSelection,
    fix_segment_selection,
)
from camctl.session.supervision import Supervisor

from ..capture.result_consumer_fixtures import consumer_world, returned
from ..capture.test_record_file_completion_wait import (
    _completed_result,
    _result_port,
    _stop_context,
)
from ..capture.test_capture_contract import _NOW
from ..capture.test_action6_segmented_normal import Action6RecordCatalog
from ..persistence.test_segment_file_pairing import _entry
from .test_segment_default_scope import _obtain


register_capture_guards()
register_operation_guards()

pytestmark = pytest.mark.asyncio

_CONTENT = b"0123456789"


class _ReadStream:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.position = 0

    def read(self, limit: int) -> bytes:
        data = self.content[self.position:self.position + limit]
        self.position += len(data)
        return data

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ReadDriver:
    def __init__(self, content: bytes, *, unavailable: bool = False) -> None:
        self.content = content
        self.unavailable = unavailable
        self.offsets = []

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        self.offsets.append(offset)
        if self.unavailable:
            raise OSError("injected device source unavailable")
        return ReadSession(
            source, offset, _ReadStream(self.content[offset:]), idle_timeout_s)


class _Digest:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    async def read_digest(self):
        from camctl.outputs.copy import SourceDigest

        return SourceDigest(digest=self.digest, error=None)


class _InspectionTools:
    def __init__(self) -> None:
        self.probe = MediaProbe(
            duration_s=Decimal("1.5"),
            error=None,
            streams=(MediaStream(
                index=0, codec_type="video", codec_name="h264",
                width=1920, height=1080, duration_s=Decimal("1.5"),
                attached_pic=False, frame_count=45, fps=Decimal("30")),),
        )

    async def probe_segment(self, input, *, count_frames: bool) -> FileTaskResult:
        return FileTaskResult(
            FileTaskId("segment-probe"), ran=True, value=self.probe)

    async def probe(self, input) -> FileTaskResult:
        raise AssertionError("逐段检查必须调用显式主流探测入口")

    async def repair(self, input, output, *, trim_s):
        raise AssertionError("逐段检查不调用修复工具")


async def _stopped_segmented_world(tmp_path):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=Action6RecordCatalog(), start=False)
    (activity_id,) = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    fixed = runtime.capture.fix_baseline(
        BaselineFixSave(activity_id, 0, 0, runtime.wall_us()),
        new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    await capture_handler(handler)(action_id, runtime)
    runtime.wall_us = lambda: _NOW + 12_000_000
    runtime.monotonic_ns = lambda: 17_000_000_000
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(
        returned("stop", "stop_confirmed", activity_id))
    runtime.stopper = stopper
    assert (await _stop_call(runtime, runtime.action(action_id))).phase == "confirmed"
    return owned, runtime, action_id, handler


async def test_only_obtains_waiting_at_the_source_publication_boundary_are_bound(tmp_path):
    owned, runtime, source_action_id, handler = await _stopped_segmented_world(tmp_path)
    try:
        runtime.check_config = type(runtime.check_config)(
            1, Decimal("1"), Decimal(0))
        early_action_id, early_selection_id = _obtain(
            owned, runtime, source_action_id, "default", 2)

        context = _stop_context(owned, source_action_id)

        async def read(request, _batch):
            entries = []
            for index in (1, 2):
                stem = f"DJI_001/DJI_20261022100000_{index:04d}_D."
                entries.extend((_entry(stem + "MP4"), _entry(stem + "LRF")))
            return _completed_result(
                request.ticket, context,
                entries=sorted(entries, key=lambda item: item["identity"]),
                finalized=True)

        _result_port(runtime, read)
        await capture_handler(handler)(source_action_id, runtime)
        published_event_id = owned.connection.execute(
            "SELECT published_event_id FROM segment_sets WHERE action_id=?"
            " AND published_event_id IS NOT NULL ORDER BY version DESC LIMIT 1",
            (source_action_id,),
        ).fetchone()[0]

        late_action_id, late_selection_id = _obtain(
            owned, runtime, source_action_id, "default", 3)
        waiters = bound_waiters_at_publication(
            source_action_id, published_event_id, owned)

        assert [(item.action_id, item.selection_id) for item in waiters] == [
            (early_action_id, early_selection_id)]
        assert (late_action_id, late_selection_id) not in {
            (item.action_id, item.selection_id) for item in waiters}
    finally:
        owned.connection.close()


@pytest.mark.parametrize("scenario", [
    "success",
    "fallback_unknown_before_commit",
    "fallback_unknown_after_commit",
    "device_unavailable",
])
async def test_bound_obtains_fall_back_to_same_device_copy_after_local_read_failure(
        tmp_path, monkeypatch, scenario):
    owned, capture_runtime, source_action_id, handler = \
        await _stopped_segmented_world(tmp_path)
    try:
        capture_runtime.check_config = type(capture_runtime.check_config)(
            1, Decimal("1"), Decimal(0))
        # These obtains are accepted while the source is still running. Their
        # selections remain pending until the source publishes its result set.
        early = [
            _obtain(owned, capture_runtime, source_action_id, "default", 20),
            _obtain(owned, capture_runtime, source_action_id, "default", 21),
        ]
        context = _stop_context(owned, source_action_id)

        async def read(request, _batch):
            stem = "DJI_001/DJI_20261022100000_0001_D."
            entries = [_entry(stem + "MP4"), _entry(stem + "LRF")]
            return _completed_result(
                request.ticket, context,
                entries=sorted(entries, key=lambda item: item["identity"]),
                finalized=True)

        _result_port(capture_runtime, read)
        # Pause at the action's one publication transaction. Segment members
        # and the inspection input exist, while the source action is still
        # RUNNING; the subsequent successful commit establishes immutable H.
        class _PublicationBoundary(Exception):
            pass

        finish_segment_recording = capture_runtime.capture.finish_segment_recording

        def hold_before_publication(*_args):
            raise _PublicationBoundary

        capture_runtime.capture.finish_segment_recording = hold_before_publication
        with pytest.raises(_PublicationBoundary):
            await capture_handler(handler)(source_action_id, capture_runtime)
        capture_runtime.capture.finish_segment_recording = finish_segment_recording

        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (source_action_id,),
        ).fetchone()[0] == 2
        set_id, = owned.connection.execute(
            "SELECT id FROM segment_sets WHERE action_id=?"
            " ORDER BY version DESC LIMIT 1", (source_action_id,),
        ).fetchone()
        member_id, source_file_id = owned.connection.execute(
            "SELECT id,file_id FROM segment_members WHERE set_id=? AND role=1"
            " ORDER BY id LIMIT 1", (set_id,),
        ).fetchone()

        staging = tmp_path / "staging"
        for name in ("recording-inputs", "derived", "deliveries"):
            (staging / name).mkdir(parents=True, exist_ok=True)
        roots = BoundDirectories(staging=staging)
        inspection_flow = MediaFlow(
            owned=owned,
            roots=roots,
            sessions=DriverReadSessions(
                owned, _ReadDriver(_CONTENT), ticket=None),
            tools=_InspectionTools(),
            policy=MediaPolicy(repair_margin_s=Decimal("2")),
            occurred_at=lambda: _NOW + 20_000_000,
            digest_supported=True,
            digest=_Digest(hashlib.sha256(_CONTENT).hexdigest()),
        )
        inspection = await inspect_segment(
            inspection_flow,
            SegmentInspectionRequest(
                action_id=source_action_id,
                member_id=member_id,
                target=CoverageTarget(CoverageMetric.DURATION_S, 60),
                budget=OperationConfig(
                    inspection_flow.max_read_attempts,
                    inspection_flow.read_idle_timeout_s,
                    inspection_flow.retry_interval_s,
                ),
                occurred_at=_NOW + 20_000_000,
            ),
        )
        assert inspection.phase is SegmentInspectionPhase.COMPLETED, inspection
        from camctl.persistence.repositories.segment_inspections import (
            read_inspection_for_member,
        )

        inspected = read_inspection_for_member(source_action_id, member_id, owned)
        assert inspected.inspection is not None and inspected.copy is not None
        input_file_id = inspected.copy.target_file_id
        assert (staging / inspected.copy.input_relative_path).read_bytes() == _CONTENT

        await capture_handler(handler)(source_action_id, capture_runtime)
        published_event_id, = owned.connection.execute(
            "SELECT published_event_id FROM segment_sets WHERE action_id=?"
            " AND published_event_id IS NOT NULL ORDER BY version DESC LIMIT 1",
            (source_action_id,),
        ).fetchone()
        assert owned.connection.execute(
            "SELECT published_event_id FROM segment_sets WHERE id=?", (set_id,),
        ).fetchone()[0] == published_event_id

        for _action_id, selection_id in early:
            fixed = fix_segment_selection(
                FixSegmentSelection(selection_id, _NOW + 21_000_000),
                new_operation_key(), owned)
            assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error

        directories = DeliveryDirectories(
            staging=staging,
            ready=tmp_path / "ready",
            processing=tmp_path / "processing",
        )
        directories.ready.mkdir(exist_ok=True)
        directories.processing.mkdir(exist_ok=True)
        executor = FileTaskExecutor(Supervisor())
        source_binding = owned.connection.execute(
            "SELECT device_id,driver_id FROM actions WHERE id=?",
            (source_action_id,),
        ).fetchone()
        device_reader = _ReadDriver(
            _CONTENT, unavailable=scenario == "device_unavailable")
        read_evidence = EvidenceRegistry(contracts=(EvidenceContract(
            type="read_returned", version=1, operation="read",
            fields=frozenset()),))
        outputs = OutputsRepository()
        fallback_keys = []
        save_fallback = outputs.fallback_read_source

        def fallback_with_unknown_receipt(request, key, connection):
            fallback_keys.append(key)
            if (scenario == "fallback_unknown_before_commit"
                    and len(fallback_keys) == 1):
                return DbOutcome(
                    DbOutcomeKind.UNKNOWN, RuntimeError("fallback commit unknown before save"))
            outcome = save_fallback(request, key, connection)
            if (scenario == "fallback_unknown_after_commit"
                    and len(fallback_keys) == 1):
                assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
                return DbOutcome(
                    DbOutcomeKind.UNKNOWN, RuntimeError("fallback commit receipt lost"))
            return outcome

        outputs.fallback_read_source = fallback_with_unknown_receipt
        obtain_runtime = ObtainRuntime(
            owned=owned,
            devices={source_binding[0]: DeviceReadAssembly(
                driver=device_reader,
                binding=DeviceBinding(*source_binding),
                evidence=read_evidence,
                digest_supported=True,
                digest_for=lambda _file_id: _Digest(
                    hashlib.sha256(_CONTENT).hexdigest()),
                    retry_interval_s=Decimal("5"),
                max_read_attempts=3,
                read_idle_timeout_s=Decimal("10"),
            )},
            directories=directories,
            segment_size=4,
            retry_gate=RetryWaitGate(),
            monotonic_ns=lambda: 20_000_000_000,
            occurred_at=lambda: _NOW + 23_000_000,
            outputs=outputs,
            operations=OperationRepository(),
            file_executor=executor,
        )

        assert not OutputsRepository().load_work_file_state(
            input_file_id, owned).owner_finished

        # Keep the physical local read paused until both independently
        # authorized deliveries have their own target and READ_FILE identity.
        async def defer_local_reads(_runtime, _now):
            return None

        with monkeypatch.context() as patch:
            patch.setattr(obtain_flow, "_advance_local_reads", defer_local_reads)
            await advance_obtain(obtain_runtime)
        granted = owned.connection.execute(
            "SELECT c.id,c.source_intermediate_file_id,c.source_device_file_id,"
            " c.target_file_id,d.id FROM file_copies c"
            " JOIN deliveries d ON d.id=c.delivery_id"
            " WHERE d.action_id IN (?,?) ORDER BY d.action_id",
            (early[0][0], early[1][0]),
        ).fetchall()
        assert len(granted) == 2
        assert all(row[1] == input_file_id and row[2] is None for row in granted)
        assert len({row[3] for row in granted}) == 2
        assert len({row[4] for row in granted}) == 2

        original_read_chunk = LocalSourceReader.read_chunk
        failed_reader = None

        def fail_after_one_local_chunk(reader, limit):
            nonlocal failed_reader
            if failed_reader is None:
                failed_reader = reader
                reader._test_chunks_read = 0
            if reader is failed_reader and reader._test_chunks_read >= 1:
                return ReadChunk(data=None, error="injected local source failure")
            result = original_read_chunk(reader, limit)
            if reader is failed_reader and result.data is not None:
                reader._test_chunks_read += 1
            return result

        monkeypatch.setattr(LocalSourceReader, "read_chunk", fail_after_one_local_chunk)
        if scenario.startswith("fallback_unknown"):
            with pytest.raises(ConsistencyError, match="读取源切换事务"):
                await advance_obtain(obtain_runtime)
            assert len(fallback_keys) == 1
            event_type = 22
            from camctl.history.events import reason_for_branch

            fallback_reason = reason_for_branch("COPY_CHANGED", "SOURCE_FALLBACK")
            fallback_events = owned.connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE event_type=?"
                " AND json_extract(body_json,'$.reason')=?",
                (event_type, fallback_reason),
            ).fetchone()[0]
            if scenario == "fallback_unknown_before_commit":
                assert fallback_events == 0
                assert owned.connection.execute(
                    "SELECT source_intermediate_file_id,source_device_file_id"
                    " FROM file_copies WHERE id=?", (granted[0][0],),
                ).fetchone() == (input_file_id, None)
            else:
                assert fallback_events == 1
                assert owned.connection.execute(
                    "SELECT source_intermediate_file_id,source_device_file_id"
                    " FROM file_copies WHERE id=?", (granted[0][0],),
                ).fetchone() == (None, source_file_id)
        for _ in range(4):
            await advance_obtain(obtain_runtime)
            statuses = [owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()[0] for action_id, _selection_id in early]
            if all(status != 2 for status in statuses):
                break
        assert device_reader.offsets == []
        switched_copy = owned.connection.execute(
            "SELECT source_intermediate_file_id,source_device_file_id,committed_bytes"
            " FROM file_copies WHERE id=?", (granted[0][0],),
        ).fetchone()
        assert switched_copy == (None, source_file_id, 4)
        retry_run = owned.connection.execute(
            "SELECT attempts_used,max_attempts_used,retry_wait_required"
            " FROM operation_runs WHERE copy_id=? AND kind=3", (granted[0][0],),
        ).fetchone()
        assert retry_run == (1, 3, 1)
        from camctl.history.events import reason_for_branch

        fallback_reason = reason_for_branch("COPY_CHANGED", "SOURCE_FALLBACK")
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE event_type=22"
            " AND json_extract(body_json,'$.reason')=?", (fallback_reason,),
        ).fetchone()[0] == 1
        if scenario == "fallback_unknown_before_commit":
            assert len(fallback_keys) == 2
            assert fallback_keys[0] == fallback_keys[1]
        else:
            assert len(fallback_keys) == 1

        # The same READ_FILE retry interval still applies after the source
        # switch. Once it elapses, the existing slot/attempt flow resumes.
        obtain_runtime.monotonic_ns = lambda: 25_000_000_000
        if scenario == "device_unavailable":
            for _ in range(4):
                await advance_obtain(obtain_runtime)
            assert device_reader.offsets == [4]
            waiting_run = owned.connection.execute(
                "SELECT status,attempts_used,max_attempts_used,retry_wait_required"
                " FROM operation_runs WHERE copy_id=? AND kind=3", (granted[0][0],),
            ).fetchone()
            assert waiting_run == (2, 2, 3, 1)
            obtain_runtime.monotonic_ns = lambda: 30_000_000_000
            for _ in range(4):
                await advance_obtain(obtain_runtime)
                statuses = [owned.connection.execute(
                    "SELECT status FROM actions WHERE id=?", (action_id,),
                ).fetchone()[0] for action_id, _selection_id in early]
                if all(status != 2 for status in statuses):
                    break
            action_status = enum_for("actions.status")
            assert statuses == [
                int(action_status.FAILED), int(action_status.SUCCEEDED)]
            run_status = enum_for("operation_runs.status")
            failed_run = owned.connection.execute(
                "SELECT status,attempts_used,max_attempts_used,retry_wait_required"
                " FROM operation_runs WHERE copy_id=? AND kind=3", (granted[0][0],),
            ).fetchone()
            assert failed_run == (int(run_status.FAILED), 3, 3, 0)
            assert device_reader.offsets == [4, 4]
            attempt_errors = owned.connection.execute(
                "SELECT a.status,json_extract(a.error_json,'$.code')"
                " FROM operation_attempts a"
                " JOIN operation_runs r ON r.id=a.run_id"
                " WHERE r.copy_id=? ORDER BY a.attempt_no", (granted[0][0],),
            ).fetchall()
            assert len(attempt_errors) == 3
            assert all(row[0] == 3 for row in attempt_errors)
            assert [row[1] for row in attempt_errors] == [
                "local_read_failed", "device_error", "device_error"]
            await advance_obtain(obtain_runtime)
            assert owned.connection.execute(
                "SELECT attempts_used FROM operation_runs WHERE copy_id=? AND kind=3",
                (granted[0][0],),
            ).fetchone()[0] == 3
            return

        for _ in range(4):
            await advance_obtain(obtain_runtime)
            statuses = [owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()[0] for action_id, _selection_id in early]
            if all(status == 3 for status in statuses):
                break
        assert all(status == 3 for status in statuses)

        deliveries = owned.connection.execute(
            "SELECT i.id,d.id,c.id,c.source_intermediate_file_id,"
            " c.source_device_file_id,c.target_file_id"
            " FROM obtain_items i JOIN deliveries d ON d.id=i.delivery_id"
            " JOIN file_copies c ON c.delivery_id=d.id"
            " WHERE i.selection_id IN (?,?) ORDER BY i.selection_id",
            (early[0][1], early[1][1]),
        ).fetchall()
        assert len(deliveries) == 2
        assert len({row[1] for row in deliveries}) == 2
        assert len({row[2] for row in deliveries}) == 2
        assert deliveries[0][3] is None and deliveries[0][4] == source_file_id
        assert deliveries[1][3] == input_file_id and deliveries[1][4] is None
        assert len({row[5] for row in deliveries}) == 2
        assert device_reader.offsets == [4]
        resumed_run = owned.connection.execute(
            "SELECT r.status,r.attempts_used,r.max_attempts_used,r.retry_wait_required"
            " FROM operation_runs r WHERE r.copy_id=? AND r.kind=3",
            (deliveries[0][2],),
        ).fetchone()
        assert resumed_run == (3, 2, 3, 0)
        device_delivery_name, = owned.connection.execute(
            "SELECT file_name FROM deliveries WHERE id=?",
            (deliveries[0][1],),
        ).fetchone()
        assert (directories.ready / device_delivery_name).read_bytes() == _CONTENT
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id JOIN file_copies c ON c.id=r.copy_id"
            " JOIN deliveries d ON d.id=c.delivery_id"
            " WHERE d.id=? AND a.status=3",
            (deliveries[0][1],),
        ).fetchone()[0] == 1

        late_action_id, late_selection_id = _obtain(
            owned, capture_runtime, source_action_id, "default", 22)
        fixed_late = fix_segment_selection(
            FixSegmentSelection(late_selection_id, _NOW + 24_000_000),
            new_operation_key(), owned)
        assert fixed_late.kind is DbOutcomeKind.COMPLETED, fixed_late.error
        late_item_id, late_output_id = owned.connection.execute(
            "SELECT i.id,i.output_id FROM obtain_items i JOIN outputs o ON o.id=i.output_id"
            " WHERE i.selection_id=? AND o.device_file_id=?",
            (late_selection_id, source_file_id),
        ).fetchone()
        late_action = _action_facts(owned.connection, late_action_id)
        device_id, = owned.connection.execute(
            "SELECT device_id FROM actions WHERE id=?", (source_action_id,),
        ).fetchone()
        obtain_shell = SimpleNamespace(
            owned=owned,
            file_executor=object(),
            directories=roots,
            binding_check=None,
            devices={device_id: SimpleNamespace(
                max_read_attempts=3,
                read_idle_timeout_s=Decimal("10"),
                retry_interval_s=Decimal("0"))},
        )
        late_candidate = await _delivery_candidate(
            obtain_shell, late_action, late_item_id, late_output_id,
            late_selection_id, _NOW + 24_000_000)
        assert late_candidate.reuse_inspection_id is None
        assert late_candidate.source_device_file_id == source_file_id

        facts = OutputsRepository().load_work_file_state(input_file_id, owned)
        assert facts.owner_finished
        assert facts.operations_stopped
        context = WorkFileContext(
            repository=OutputsRepository(),
            owned=owned,
            staging=staging,
            occurred_at=_NOW + 25_000_000,
            limits=WorkFileLimits(batch_size=16, limit_per_run=16),
            executor=executor,
        )
        cleaned = await clean_one_work_file(input_file_id, context)
        assert cleaned.outcome is WorkFileSingleOutcome.DELETED
        assert not (staging / inspected.copy.input_relative_path).exists()
        retention, cleanup = owned.connection.execute(
            "SELECT retention_state,cleanup_state FROM intermediate_files WHERE id=?",
            (input_file_id,),
        ).fetchone()
        assert (retention, cleanup) == (2, 4)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (late_action_id,),
        ).fetchone()[0] == 2
    finally:
        owned.connection.close()
