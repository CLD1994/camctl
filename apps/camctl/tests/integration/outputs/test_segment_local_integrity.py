"""检查副本本地取回的重拷沿用原 READ_FILE 责任。"""

from decimal import Decimal
import hashlib
import json
from unittest.mock import patch

import pytest

import camctl.outputs.copy as copy_module
from camctl.capture.handlers import capture_handler
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.segment_inspections import (
    SegmentInspectionPhase,
    SegmentInspectionRequest,
    inspect_segment,
)
from camctl.capture.video_coverage import CoverageMetric, CoverageTarget
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.devices.read_session import ReadChunk
from camctl.host_files.io import LocalSourceReader
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskExecutor
from camctl.operations.attempts import RetryWaitGate
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.outputs.qualification import OperationConfig
from camctl.session.supervision import Supervisor

from ..capture.result_consumer_fixtures import consumer_world, returned
from ..capture.test_record_file_completion_wait import (
    _completed_result,
    _result_port,
    _stop_context,
)
from ..capture.test_capture_contract import _NOW
from ..persistence.test_segment_file_pairing import _entry
from .test_segment_copy_reuse import (
    _CONTENT,
    _InspectionTools,
    _ReadDriver,
    _Digest,
    _stopped_segmented_world,
)
from .test_segment_default_scope import _obtain

from camctl.outputs.handoff import DeliveryDirectories
from camctl.outputs.obtain_flow import (
    DeviceReadAssembly,
    ObtainRuntime,
    advance_obtain,
)


register_capture_guards()
register_operation_guards()
pytestmark = pytest.mark.asyncio


async def _bound_local_delivery(tmp_path):
    owned, capture_runtime, source_action_id, handler = \
        await _stopped_segmented_world(tmp_path)
    capture_runtime.check_config = type(capture_runtime.check_config)(
        1, Decimal("1"), Decimal(0))
    action_id, selection_id = _obtain(
        owned, capture_runtime, source_action_id, "default", 40)
    context = _stop_context(owned, source_action_id)

    async def read(request, _batch):
        stem = "DJI_001/DJI_20261022100000_0001_D."
        entries = [_entry(stem + "MP4"), _entry(stem + "LRF")]
        return _completed_result(
            request.ticket, context,
            entries=sorted(entries, key=lambda item: item["identity"]),
            finalized=True)

    _result_port(capture_runtime, read)

    class _PublicationBoundary(Exception):
        pass

    finish_segment_recording = capture_runtime.capture.finish_segment_recording

    def hold_before_publication(*_args):
        raise _PublicationBoundary

    capture_runtime.capture.finish_segment_recording = hold_before_publication
    with pytest.raises(_PublicationBoundary):
        await capture_handler(handler)(source_action_id, capture_runtime)
    capture_runtime.capture.finish_segment_recording = finish_segment_recording
    set_id, = owned.connection.execute(
        "SELECT id FROM segment_sets WHERE action_id=? ORDER BY version DESC LIMIT 1",
        (source_action_id,),
    ).fetchone()
    member_id, = owned.connection.execute(
        "SELECT id FROM segment_members WHERE set_id=? AND role=1 ORDER BY id LIMIT 1",
        (set_id,),
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
    assert inspected.copy is not None
    input_file_id = inspected.copy.target_file_id
    input_path = staging / inspected.copy.input_relative_path
    assert input_path.read_bytes() == _CONTENT

    await capture_handler(handler)(source_action_id, capture_runtime)
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE id=?", (source_action_id,),
    ).fetchone()[0] != 2
    fixed = owned.connection.execute(
        "SELECT status FROM obtain_source_selections WHERE id=?", (selection_id,),
    ).fetchone()[0]
    if fixed == 1:
        from camctl.persistence.repositories.segment_selections import (
            FixSegmentSelection,
            fix_segment_selection,
        )

        result = fix_segment_selection(
            FixSegmentSelection(selection_id, _NOW + 21_000_000),
            new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error

    directories = DeliveryDirectories(
        staging=staging,
        ready=tmp_path / "ready",
        processing=tmp_path / "processing",
    )
    directories.ready.mkdir(exist_ok=True)
    directories.processing.mkdir(exist_ok=True)
    executor = FileTaskExecutor(Supervisor())
    device_id, driver_id = owned.connection.execute(
        "SELECT device_id,driver_id FROM actions WHERE id=?", (source_action_id,),
    ).fetchone()
    read_evidence = EvidenceRegistry(contracts=(EvidenceContract(
        type="read_returned", version=1, operation="read",
        fields=frozenset()),))
    runtime = ObtainRuntime(
        owned=owned,
        devices={device_id: DeviceReadAssembly(
            driver=_ReadDriver(_CONTENT),
            binding=DeviceBinding(device_id, driver_id),
            evidence=read_evidence,
            digest_supported=True,
            digest_for=lambda _file_id: _Digest(hashlib.sha256(_CONTENT).hexdigest()),
            retry_interval_s=Decimal("5"),
            max_read_attempts=3,
            read_idle_timeout_s=Decimal("10"),
        )},
        directories=directories,
        segment_size=4,
        retry_gate=RetryWaitGate(),
        monotonic_ns=lambda: 20_000_000_000,
        occurred_at=lambda: _NOW + 23_000_000,
        outputs=OutputsRepository(),
        operations=OperationRepository(),
        file_executor=executor,
    )

    async def defer_local_reads(_runtime, _now):
        return None

    with patch("camctl.outputs.obtain_flow._advance_local_reads", defer_local_reads):
        await advance_obtain(runtime)
    copy_id, = owned.connection.execute(
        "SELECT id FROM file_copies WHERE delivery_id IN "
        "(SELECT id FROM deliveries WHERE action_id=?)", (action_id,),
    ).fetchone()
    copy_row = owned.connection.execute(
        "SELECT source_intermediate_file_id,source_device_file_id,target_file_id"
        " FROM file_copies WHERE id=?", (copy_id,),
    ).fetchone()
    assert copy_row[0] == input_file_id and copy_row[1] is None
    assert copy_row[2] is not None
    return owned, runtime, action_id, copy_id, input_path, directories


async def _inject_target_mismatch(monkeypatch, mismatch_count):
    original = copy_module.hash_target
    calls = 0

    def injected(ref, roots):
        nonlocal calls
        result = original(ref, roots)
        if result.error is None:
            calls += 1
            if calls <= mismatch_count:
                return type(result)(digest="0" * 64,
                                    size_bytes=result.size_bytes, error=None)
        return result

    monkeypatch.setattr(copy_module, "hash_target", injected)
    return lambda: calls


def _read_attempt_state(owned, copy_id):
    run = owned.connection.execute(
        "SELECT id,status,attempts_used,max_attempts_used,retry_wait_required,error_json"
        " FROM operation_runs WHERE copy_id=? AND kind=3", (copy_id,),
    ).fetchone()
    attempts = owned.connection.execute(
        "SELECT attempt_no,status,error_json FROM operation_attempts"
        " WHERE run_id=? ORDER BY attempt_no", (run[0],),
    ).fetchall()
    return run, attempts


async def _advance_until_terminal(runtime, owned, action_id, *, ticks=5):
    for index in range(ticks):
        runtime.monotonic_ns = lambda index=index: (20 + index * 10) * 1_000_000_000
        await advance_obtain(runtime)
        status, = owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (action_id,),
        ).fetchone()
        if status != 2:
            break


@pytest.mark.parametrize("mismatch_count,expected", [(1, "ready"), (2, "exhausted")])
async def test_local_recopy_keeps_read_attempt_and_checksum_exhaustion_is_final(
        tmp_path, monkeypatch, mismatch_count, expected):
    owned, runtime, action_id, copy_id, input_path, directories = \
        await _bound_local_delivery(tmp_path)
    try:
        digest_calls = await _inject_target_mismatch(monkeypatch, mismatch_count)

        # 首次目标摘要不一致后登记第二轮；原 READ_FILE 必须仍为同一个 RUNNING attempt。
        await advance_obtain(runtime)
        first_copy = owned.connection.execute(
            "SELECT round,recopies_used,committed_bytes"
            " FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone()
        assert first_copy[:3] == (2, 1, 0)
        assert digest_calls() == 1
        target_relative_path, = owned.connection.execute(
            "SELECT f.relative_path FROM file_copies c"
            " JOIN intermediate_files f ON f.id=c.target_file_id WHERE c.id=?",
            (copy_id,),
        ).fetchone()
        assert (directories.staging / target_relative_path).read_bytes() == _CONTENT
        run, attempts = _read_attempt_state(owned, copy_id)
        assert run[1:5] == (int(enum_for("operation_runs.status").ACTIVE), 1, 3, 0)
        assert [(row[0], row[1]) for row in attempts] == [
            (1, int(enum_for("operation_attempts.status").RUNNING))]

        await _advance_until_terminal(runtime, owned, action_id)
        run, attempts = _read_attempt_state(owned, copy_id)
        assert runtime.devices[next(iter(runtime.devices))].driver.offsets == []
        if expected == "ready":
            assert owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()[0] == int(enum_for("actions.status").SUCCEEDED)
            assert len(attempts) == 1
            assert attempts[0][1] == int(enum_for("operation_attempts.status").SUCCEEDED)
            assert run[1:5] == (int(enum_for("operation_runs.status").SUCCEEDED), 1, 3, 0)
            assert owned.connection.execute(
                "SELECT round,recopies_used,verification_state FROM file_copies WHERE id=?",
                (copy_id,),
            ).fetchone() == (2, 1, 3)
            assert digest_calls() == 2
        else:
            assert owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()[0] == int(enum_for("actions.status").FAILED)
            assert len(attempts) == 1
            assert attempts[0][1] == int(enum_for("operation_attempts.status").SUCCEEDED)
            assert run[1:5] == (int(enum_for("operation_runs.status").FAILED), 1, 3, 0)
            assert json.loads(run[5])["code"] == "checksum_mismatch"
            delivery_error, = owned.connection.execute(
                "SELECT error_json FROM deliveries WHERE action_id=?", (action_id,),
            ).fetchone()
            assert json.loads(delivery_error)["code"] == "checksum_mismatch"
            assert owned.connection.execute(
                "SELECT round,recopies_used,verification_state FROM file_copies WHERE id=?",
                (copy_id,),
            ).fetchone() == (2, 1, 4)
            assert digest_calls() == 2
        assert input_path.read_bytes() == _CONTENT
    finally:
        owned.connection.close()


async def test_local_source_failure_after_recopy_falls_back_with_original_budget(
        tmp_path, monkeypatch):
    owned, runtime, action_id, copy_id, input_path, directories = \
        await _bound_local_delivery(tmp_path)
    try:
        target_digest_calls = await _inject_target_mismatch(monkeypatch, 1)
        await advance_obtain(runtime)
        assert target_digest_calls() == 1
        assert owned.connection.execute(
            "SELECT round,recopies_used,committed_bytes FROM file_copies WHERE id=?",
            (copy_id,),
        ).fetchone() == (2, 1, 0)
        read_identity, = owned.connection.execute(
            "SELECT a.id FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.copy_id=? AND r.kind=3 AND a.attempt_no=1", (copy_id,),
        ).fetchone()
        original_target, = owned.connection.execute(
            "SELECT target_file_id FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone()
        assert owned.connection.execute(
            "SELECT status,copy_round FROM operation_attempts WHERE id=?",
            (read_identity,),
        ).fetchone() == (
            int(enum_for("operation_attempts.status").RUNNING), 1)

        original_read_chunk = LocalSourceReader.read_chunk
        failing_reader = None

        def fail_after_one_local_chunk(reader, limit):
            nonlocal failing_reader
            if failing_reader is None:
                failing_reader = reader
                reader._test_chunks_read = 0
            if reader is failing_reader and reader._test_chunks_read >= 1:
                return ReadChunk(data=None, error="injected recopy source failure")
            result = original_read_chunk(reader, limit)
            if reader is failing_reader and result.data is not None:
                reader._test_chunks_read += 1
            return result

        monkeypatch.setattr(LocalSourceReader, "read_chunk", fail_after_one_local_chunk)
        await advance_obtain(runtime)

        source_device_id, = owned.connection.execute(
            "SELECT o.device_file_id FROM file_copies c"
            " JOIN deliveries d ON d.id=c.delivery_id"
            " JOIN outputs o ON o.id=d.output_id WHERE c.id=?", (copy_id,),
        ).fetchone()
        copy_row = owned.connection.execute(
            "SELECT source_intermediate_file_id,source_device_file_id,round,"
            " recopies_used,committed_bytes,target_file_id"
            " FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone()
        assert copy_row[:5] == (None, source_device_id, 2, 1, 4)
        assert copy_row[5] == original_target
        assert input_path.read_bytes() == _CONTENT
        target_path, = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE id=?",
            (copy_row[5],),
        ).fetchone()
        assert (directories.staging / target_path).read_bytes() == _CONTENT[:4]
        assert runtime.file_executor.unfinished_files() == ()

        run, attempts = _read_attempt_state(owned, copy_id)
        assert run[1:5] == (int(enum_for("operation_runs.status").ACTIVE), 1, 3, 1)
        assert run[0] == owned.connection.execute(
            "SELECT run_id FROM operation_attempts WHERE id=?", (read_identity,),
        ).fetchone()[0]
        assert len(attempts) == 1
        assert attempts[0][0:2] == (
            1, int(enum_for("operation_attempts.status").FAILED))
        assert owned.connection.execute(
            "SELECT copy_round FROM operation_attempts WHERE run_id=?"
            " ORDER BY attempt_no", (run[0],),
        ).fetchone()[0] == 1

        device_reader = runtime.devices[next(iter(runtime.devices))].driver
        assert device_reader.offsets == []
        runtime.monotonic_ns = lambda: 25_000_000_000
        for _ in range(5):
            await advance_obtain(runtime)
            status, = owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,),
            ).fetchone()
            if status != int(enum_for("actions.status").RUNNING):
                break

        assert device_reader.offsets == [4]
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (action_id,),
        ).fetchone()[0] == int(enum_for("actions.status").SUCCEEDED)
        run, attempts = _read_attempt_state(owned, copy_id)
        assert run[1:5] == (
            int(enum_for("operation_runs.status").SUCCEEDED), 2, 3, 0)
        assert [(row[0], row[1]) for row in attempts] == [
            (1, int(enum_for("operation_attempts.status").FAILED)),
            (2, int(enum_for("operation_attempts.status").SUCCEEDED)),
        ]
        assert owned.connection.execute(
            "SELECT id,source_intermediate_file_id,source_device_file_id,round,"
            "recopies_used,committed_bytes,target_file_id,verification_state"
            " FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone() == (
            copy_id, None, source_device_id, 2, 1, len(_CONTENT), original_target,
            int(enum_for("file_copies.verification_state").MATCHED),
        )
        delivery_id, delivery_name, delivery_status = owned.connection.execute(
            "SELECT id,file_name,status FROM deliveries WHERE action_id=?",
            (action_id,),
        ).fetchone()
        assert delivery_status == int(enum_for("deliveries.status").PUBLISHED)
        published_path = directories.ready / delivery_name
        assert published_path.read_bytes() == _CONTENT
        assert hashlib.sha256(published_path.read_bytes()).hexdigest() == owned.connection.execute(
            "SELECT target_sha256 FROM file_copies WHERE id=?", (copy_id,),
        ).fetchone()[0]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.copy_id=? AND a.id=?", (copy_id, read_identity),
        ).fetchone()[0] == 1
        assert delivery_id is not None
    finally:
        owned.connection.close()
