"""Action6 覆盖报告在后续取回与文件维护后保持固定 H。"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.contracts.enums import enum_for
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.history.snapshots import prepare_snapshot
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.outputs.cleanup_flow import CleanupRuntime, advance_cleanup
from camctl.outputs.handoff import DeliveryDirectories
from camctl.outputs.obtain_flow import ObtainRuntime, advance_obtain
from camctl.outputs.work_files import (
    WorkFileContext,
    WorkFileLimits,
    WorkFileSingleOutcome,
    clean_one_work_file,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.executor import DbExecutor
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    ProcessInput,
)
from camctl.persistence.repositories.history import (
    HistoryRepository,
    SqliteSnapshotStore,
)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.session.supervision import Supervisor
from camctl.host_files.tasks import FileTaskExecutor

from ..capture.test_action6_segmented_recovery import (
    _CONTENT,
    _entries,
    _overrun_recording,
    _publish_ordinary_result,
)
from ..capture.test_record_file_completion_wait import _NOW
from ..outputs.test_segment_default_scope import ObtainCatalog, _obtain
from ..outputs.test_cleanup_recovery import _EVIDENCE as CLEANUP_EVIDENCE
from ..reporting.test_generation import _freeze, _spec

pytestmark = pytest.mark.asyncio

_BINDING = DeviceBinding(device_id="cam-1", driver_id="camctl-adb")


class _DeleteCatalog(ObtainCatalog):
    def action_types(self):
        return super().action_types() | {"delete_action_outputs"}


class _AbsentDeviceFile:
    async def delete(self, request):
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="file_absent",
                version=1,
                data={"cleanup_item_id": request.params["cleanup_item_id"]},
            ),),
            error=None,
        )

    async def query_file(self, request):
        raise AssertionError("可靠 DELETE 缺席事实不需要查询")


class _StrategyHistory(HistoryRepository):
    def __init__(self, path, *, strategy, batch_size):
        super().__init__(path)
        self._forced_strategy = strategy
        self._forced_batch_size = batch_size

    def open_segment_history(
        self, action_id, boundary, *, strategy="auto", batch_size=128
    ):
        return super().open_segment_history(
            action_id,
            boundary,
            strategy=self._forced_strategy,
            batch_size=self._forced_batch_size,
        )


async def _save_history_snapshot(history, action_id):
    boundary = history.current_boundary()
    with history.open_segment_history(
        action_id, boundary, strategy="current", batch_size=1
    ) as handle:
        prepared = prepare_snapshot(handle)
    executor = DbExecutor(
        lambda: open_existing(history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4,
        enqueue_timeout_seconds=9,
    )
    try:
        saved = await SqliteSnapshotStore(history.path, executor).save_snapshots(
            (prepared,)
        )
        assert len(saved) == 1
        return boundary
    finally:
        prepared.close()
        await executor.close()


def _delete_request(owned, output_id, now):
    instant = datetime.fromtimestamp(now // 1_000_000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    saved = AcceptanceRepository().process_input(
        ProcessInput(
            ParsedInput(
                "delete.json",
                {
                    "request_id": "9901",
                    "created_at": instant,
                    "name": "删除来源文件",
                    "actions": [
                        {
                            "name": "删除",
                            "type": "delete_action_outputs",
                            "scheduled_at": instant,
                            "params": {"output_ids": [str(output_id)]},
                        }
                    ],
                },
            ),
            _DeleteCatalog(),
            CommandMode.RUN,
            now,
        ),
        new_operation_key(),
        owned,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error


def _coverage_action(document, action_id):
    return next(
        action
        for plan in document["plans"]
        for action in plan["actions"]
        if action["action_instance_id"] == str(action_id)
    )


def _generate_at_h(tmp_path, report, *, strategy, batch_size, name):
    spec = _spec(
        tmp_path,
        report,
        f"staging/{name}.json",
        entity_batch_size=1 if batch_size == 1 else 3,
        event_batch_size=batch_size,
    )
    return generate_report_file(
        spec,
        repository=_StrategyHistory(
            tmp_path / "state.db",
            strategy=strategy,
            batch_size=batch_size,
        ),
        buffer_size=7 if batch_size == 1 else 29,
    )


async def test_action6_fixed_h_survives_obtain_delete_and_inspection_cleanup(
    tmp_path, monkeypatch
):
    owned, capture_runtime, action_id, handler, _tools = await _overrun_recording(
        tmp_path, (Decimal("18"),)
    )
    try:
        obtain_action_id, selection_id = _obtain(
            owned, capture_runtime, action_id, "default", 9900
        )

        history = HistoryRepository(tmp_path / "state.db")
        snapshot_boundary = await _save_history_snapshot(history, action_id)

        await _publish_ordinary_result(
            capture_runtime, owned, action_id, handler, _entries(1)
        )
        from camctl.persistence.repositories.segment_selections import (
            FixSegmentSelection,
            fix_segment_selection,
        )

        fixed = fix_segment_selection(
            FixSegmentSelection(selection_id, capture_runtime.wall_us()),
            new_operation_key(),
            owned,
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error

        staging = tmp_path / "staging"
        directories = DeliveryDirectories(
            staging=staging,
            ready=tmp_path / "ready",
            processing=tmp_path / "processing",
        )
        directories.ready.mkdir(exist_ok=True)
        directories.processing.mkdir(exist_ok=True)
        executor = FileTaskExecutor(Supervisor())
        obtain_runtime = ObtainRuntime(
            owned=owned,
            devices={},
            directories=directories,
            segment_size=4,
            retry_gate=RetryWaitGate(),
            monotonic_ns=lambda: 22_000_000_000,
            occurred_at=lambda: _NOW + 600_000_001,
            outputs=OutputsRepository(),
            operations=OperationRepository(),
            file_executor=executor,
        )

        # 真实 Obtain 编排创建已授权交付，然后在读取前冻结 H。
        # H 后恢复该 delivery/copy，完成实际读取、摘要和发布。
        import camctl.outputs.obtain_flow as obtain_flow

        async def pause_after_grant(_runtime, _now):
            return None

        original_local_reads = obtain_flow._advance_local_reads
        monkeypatch.setattr(obtain_flow, "_advance_local_reads", pause_after_grant)
        await advance_obtain(obtain_runtime)
        monkeypatch.setattr(obtain_flow, "_advance_local_reads", original_local_reads)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (obtain_action_id,)
        ).fetchone() == (2,)
        delivery_row = owned.connection.execute(
            "SELECT d.id,c.id,c.target_file_id,c.source_sha256,c.target_sha256"
            " FROM obtain_items i JOIN deliveries d ON d.id=i.delivery_id"
            " JOIN file_copies c ON c.delivery_id=d.id"
            " WHERE i.selection_id=?",
            (selection_id,),
        ).fetchone()
        assert delivery_row is not None
        (
            delivery_id,
            delivery_copy_id,
            delivery_target_id,
            source_sha_at_h,
            target_sha_at_h,
        ) = delivery_row
        assert source_sha_at_h == hashlib.sha256(_CONTENT).hexdigest()
        assert target_sha_at_h is None
        obtain_snapshot_boundary = await _save_history_snapshot(
            history, obtain_action_id
        )

        report = _freeze(owned)
        first = _generate_at_h(
            tmp_path,
            report,
            strategy="initial",
            batch_size=1,
            name="at-h-initial",
        )
        original_bytes = first.path.read_bytes()
        original_document = json.loads(original_bytes)
        validate_document("protocol/status-report.schema.json", original_document)
        source_action = _coverage_action(original_document, action_id)
        assert source_action["status"] == "failed"
        assert source_action["result"]["coverage"]["status"] == "confirmed_short"
        assert source_action["result"]["coverage"]["metric"] == "duration_s"
        assert source_action["result"]["coverage"]["confirmed"] == Decimal("18")
        assert source_action["result"]["coverage"]["checked_output_ids"] == [
            source_action["outputs"][0]["output_id"]
        ]
        source_output_at_h = source_action["outputs"][0]
        assert source_output_at_h["availability"] == "available"
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (obtain_action_id,)
        ).fetchone() == (2,)
        obtain_action = _coverage_action(original_document, obtain_action_id)
        assert obtain_action["status"] == "running"
        h_delivery = next(
            item for item in obtain_action["deliveries"]
            if item["delivery_id"] == str(delivery_id)
        )
        assert h_delivery["status"] == "pending"
        assert "sha256" not in h_delivery

        (source_output_id, source_file_id) = owned.connection.execute(
            "SELECT o.id,o.device_file_id FROM outputs o"
            " WHERE o.source_action_id=? AND o.kind=1 ORDER BY o.id LIMIT 1",
            (action_id,),
        ).fetchone()
        member_id, = owned.connection.execute(
            "SELECT id FROM segment_members WHERE file_id=? AND role=1",
            (source_file_id,),
        ).fetchone()
        from camctl.persistence.repositories.segment_inspections import (
            read_inspection_for_member,
        )

        inspection = read_inspection_for_member(action_id, member_id, owned)
        assert inspection.inspection is not None
        assert inspection.copy is not None
        inspection_id = inspection.inspection.inspection_id
        inspection_copy_id = inspection.copy.copy_id
        inspection_file_id = inspection.copy.target_file_id
        inspection_path = inspection.copy.input_relative_path
        assert (tmp_path / "staging" / inspection_path).read_bytes() == _CONTENT

        h_ref = report.boundary
        with history.open_segment_history(
            action_id, h_ref, strategy="current", batch_size=1
        ) as handle:
            inspection_rows = []
            cursor = None
            while True:
                page = handle.read_page("segment_inspections", cursor=cursor, limit=1)
                inspection_rows.extend(page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
            matching = [
                row.values for row in inspection_rows if row.row_id == inspection_id
            ]
            assert len(matching) == 1 and matching[0]["state"] == 2
            inspection_copy_at_h = handle.row("file_copies", inspection_copy_id)
            assert inspection_copy_at_h is not None
            assert inspection_copy_at_h["inspection_id"] == inspection_id
            assert inspection_copy_at_h["target_file_id"] == inspection_file_id
        delivery_rows_at_h = history.restore_entity(
            "delivery", delivery_id, h_ref, event_batch_size=1
        )
        delivery_at_h = delivery_rows_at_h[("deliveries", delivery_id)]
        delivery_copy_at_h = delivery_rows_at_h[("file_copies", delivery_copy_id)]
        assert delivery_at_h["status"] == 1
        assert delivery_copy_at_h["source_intermediate_file_id"] == inspection_file_id
        assert delivery_copy_at_h["source_device_file_id"] is None
        assert delivery_copy_at_h["target_file_id"] == delivery_target_id
        assert delivery_copy_at_h["target_sha256"] is None
        source_file_at_h = history.restore_primary_at_h(
            "device_file", source_file_id, h_ref
        )
        inspection_file_at_h = history.restore_primary_at_h(
            "intermediate_file", inspection_file_id, h_ref
        )
        assert source_file_at_h is not None
        assert inspection_file_at_h is not None
        assert source_file_at_h["presence_state"] != 3
        assert source_file_at_h["checksum_support"] == 2
        assert source_file_at_h["sha256"] is None
        assert inspection_file_at_h["cleanup_state"] != 4

        obtain_runtime.occurred_at = lambda: _NOW + 650_000_000
        obtain_runtime.monotonic_ns = lambda: 23_000_000_000
        for _ in range(4):
            await advance_obtain(obtain_runtime)
            if owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (obtain_action_id,)
            ).fetchone() != (2,):
                break
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (obtain_action_id,)
        ).fetchone() == (3,)
        delivery_name, = owned.connection.execute(
            "SELECT file_name FROM deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        assert (directories.ready / delivery_name).read_bytes() == _CONTENT

        copy_facts = owned.connection.execute(
            "SELECT source_sha256,target_sha256,target_file_id,verification_state"
            " FROM file_copies WHERE id=?",
            (delivery_copy_id,),
        ).fetchone()
        digest = hashlib.sha256(_CONTENT).hexdigest()
        assert copy_facts[:3] == (digest, digest, delivery_target_id)
        assert copy_facts[3] == int(enum_for("file_copies.verification_state").MATCHED)
        target_sha256, = owned.connection.execute(
            "SELECT sha256 FROM intermediate_files WHERE id=?",
            (delivery_target_id,),
        ).fetchone()
        assert target_sha256 == digest

        delete_now = _NOW + 700_000_000
        _delete_request(owned, source_output_id, delete_now)
        delete_action_id, = owned.connection.execute(
            "SELECT MAX(id) FROM actions WHERE type=5"
        ).fetchone()
        cleanup_runtime = CleanupRuntime(
            owned=owned,
            outputs=OutputsRepository(),
            operations=OperationRepository(),
            driver=_AbsentDeviceFile(),
            evidence=CLEANUP_EVIDENCE,
            binding_of=lambda _item_id: _BINDING,
            occurred_at=lambda: delete_now + 1,
            delete_config=AttemptConfig(1, Decimal("10"), Decimal(0)),
            query_config=AttemptConfig(1, Decimal("10"), Decimal(0)),
            retry_gate=RetryWaitGate(),
            delete_supported=True,
            query_supported=True,
        )
        await advance_cleanup(cleanup_runtime)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (delete_action_id,)
        ).fetchone() == (3,)
        assert owned.connection.execute(
            "SELECT presence_state FROM device_files WHERE id=?", (source_file_id,)
        ).fetchone() == (int(enum_for("device_files.presence_state").ABSENT),)

        work_state = OutputsRepository().load_work_file_state(inspection_file_id, owned)
        assert work_state.owner_finished and work_state.operations_stopped
        cleaned = await clean_one_work_file(
            inspection_file_id,
            WorkFileContext(
                repository=OutputsRepository(),
                owned=owned,
                staging=staging,
                occurred_at=delete_now + 3,
                limits=WorkFileLimits(batch_size=16, limit_per_run=16),
                executor=executor,
            ),
        )
        assert cleaned.outcome is WorkFileSingleOutcome.DELETED
        assert not (staging / inspection_path).exists()
        assert owned.connection.execute(
            "SELECT retention_state,cleanup_state FROM intermediate_files WHERE id=?",
            (inspection_file_id,),
        ).fetchone() == (2, 4)

        current_source = owned.connection.execute(
            "SELECT checksum_support,sha256,presence_state FROM device_files"
            " WHERE id=?",
            (source_file_id,),
        ).fetchone()
        assert current_source == (
            2,
            None,
            int(enum_for("device_files.presence_state").ABSENT),
        )
        current_inspection_file = owned.connection.execute(
            "SELECT cleanup_state FROM intermediate_files WHERE id=?",
            (inspection_file_id,),
        ).fetchone()
        assert current_inspection_file == (4,)

        after_report = _freeze(owned)
        after = _generate_at_h(
            tmp_path,
            after_report,
            strategy="current",
            batch_size=2,
            name="after-obtain-delete-cleanup",
        )
        after_document = json.loads(after.path.read_bytes())
        validate_document("protocol/status-report.schema.json", after_document)
        after_obtain = _coverage_action(after_document, obtain_action_id)
        published_delivery = next(
            item for item in after_obtain["deliveries"]
            if item["delivery_id"] == str(delivery_id)
        )
        assert published_delivery["status"] == "published"
        assert published_delivery["sha256"] == digest
        assert "sha256" not in h_delivery
        after_source_action = _coverage_action(after_document, action_id)
        assert (
            source_action["result"]["coverage"]
            == after_source_action["result"]["coverage"]
        )
        after_source_output = next(
            item for item in after_source_action["outputs"]
            if item["output_id"] == source_output_at_h["output_id"]
        )
        assert after_source_output["availability"] == "cleaned"

        for strategy, batch_size, label in (
            ("current", 1, "after-current"),
            ("snapshot", 2, "after-snapshot"),
            ("initial", 3, "after-initial"),
        ):
            rebuilt = _generate_at_h(
                tmp_path,
                report,
                strategy=strategy,
                batch_size=batch_size,
                name=label,
            )
            assert rebuilt.path.read_bytes() == original_bytes
            rebuilt_document = json.loads(rebuilt.path.read_bytes())
            validate_document("protocol/status-report.schema.json", rebuilt_document)

        assert snapshot_boundary.last_event_id < report.boundary.last_event_id
        assert obtain_snapshot_boundary.last_event_id <= report.boundary.last_event_id
        with HistoryRepository(tmp_path / "state.db").open_segment_history(
            action_id,
            report.boundary,
            strategy="snapshot",
            batch_size=1,
        ) as handle:
            assert handle.stats["events"] > 0
        source_at_h = history.restore_primary_at_h(
            "device_file", source_file_id, report.boundary
        )
        inspection_at_h = history.restore_primary_at_h(
            "intermediate_file", inspection_file_id, report.boundary
        )
        assert source_at_h["presence_state"] != 3
        assert inspection_at_h["cleanup_state"] != 4
    finally:
        owned.connection.close()
