"""从真实受理与仓储事务验证主机照片各完整 H 的历史恢复。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.enums import enum_for, load_registry as load_enum_registry
from camctl.contracts.values import new_operation_key
from camctl.devices.catalog import DriverDefinitions, build_catalog
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
from camctl.history.snapshots import SnapshotRef, prepare_snapshot
from camctl.outputs.catalog import FileReference, OutputDraft, OutputKind, OutputCatalogFacts
from camctl.outputs.cleanup_flow import delete_source_file
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    ProcessInput,
    register_acceptance_guards,
)
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishCapture,
    register_capture_guards,
)
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.repositories.host_capture import (
    HostCaptureRepository,
    HostPhotoComplete,
)
from camctl.persistence.repositories.outputs import (
    OutputsRepository,
    register_outputs_guards,
)
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest,
    SchedulingRepository,
    StartActionRequest,
    register_window_guard,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts
from camctl.persistence.executor import DbExecutor
from camctl.outputs.cleanup_flow import (
    FixCleanupTargets,
    RestrictCleanupItem,
    StartCleanupAction,
)


_NOW = int(datetime(2026, 1, 15, 9, tzinfo=timezone.utc).timestamp() * 1_000_000)
_ORIGINAL = b"history-recovery-original"
_PREVIEW = b"\xff\xd8history-recovery-preview\xff\xd9"

register_acceptance_guards()
register_window_guard()
register_operation_guards()
register_capture_guards()
register_outputs_guards()


def _config(home: Path):
    return load_config({
        "paths": {
            "state_db": str(home / "state.db"),
            "log_file": str(home / "camctl.log"),
            "staging": str(home / "staging"),
            "ready": str(home / "ready"),
            "processing": str(home / "processing"),
        },
        "devices": {"cam-1": {
            "kind": "camera",
            "driver": DRIVER_ID,
            "hasselblad": {"executable": "/usr/bin/hb_cli", "serial": "SERIAL-1"},
        }},
    }, ConfigDefaults())


def _input_time() -> str:
    return datetime.fromtimestamp(_NOW / 1_000_000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


def _accept(owned, catalog, request_id: str, actions: list[dict]):
    result = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("host-photo-history.json", {
            "request_id": request_id,
            "created_at": _input_time(),
            "name": "历史恢复照片事务",
            "actions": actions,
        }),
        catalog,
        CommandMode.RUN,
        _NOW,
    ), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return result.value


async def _save_file_snapshot(path: Path, entity_id: int, entity_type: int) -> None:
    executor = DbExecutor(
        lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4,
        enqueue_timeout_seconds=9,
    )
    try:
        store = SqliteSnapshotStore(path, executor)
        seed, = await store.load_entity_images((SnapshotRef(entity_type, entity_id),))
        await store.save_snapshots((prepare_snapshot(seed),))
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_real_accepted_host_photo_history_restores_complete_boundaries_and_forward_snapshot(
    tmp_path: Path, monkeypatch,
):
    config = _config(tmp_path)
    database = Path(config.paths.state_db)
    initialized = initialize_state(config, database)
    assert initialized.outcome is InitOutcome.CREATED, initialized.detail
    owned = open_existing(database, DbOpenMode.EXISTING_RW, DbConfig())
    catalog = build_catalog(config, DriverDefinitions({DRIVER_ID: driver_definition()}))
    history = HistoryRepository(database)

    try:
        _accept(owned, catalog, "1", [{
            "name": "单张 RAW",
            "type": "camera_take_photo",
            "device_id": "cam-1",
            "scheduled_at": _input_time(),
            "params": {"type": "hasselblad_photo"},
            "policy": {"max_delay_ms": 5000},
        }])
        scheduling = SchedulingRepository()
        for save, request in (
            (scheduling.observe_window, ObserveWindowRequest(1, _NOW, _NOW)),
            (scheduling.start_action, StartActionRequest(1, _NOW, _NOW)),
        ):
            outcome = save(request, new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error

        source = HostCaptureRepository()
        prepared = source.prepare_host_photo(1, _NOW, new_operation_key(), owned)
        assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
        files = prepared.value
        boundary_prepared = history.current_boundary()
        file_entity = load_enum_registry()["history_objects"]["intermediate_file"]["id"]
        await _save_file_snapshot(database, files.original.file_id, file_entity)
        prepared_state = {
            ("action", 1): history.restore_entity("action", 1, boundary_prepared),
            ("intermediate_file", files.original.file_id): history.restore_entity(
                "intermediate_file", files.original.file_id, boundary_prepared),
            ("intermediate_file", files.preview.file_id): history.restore_entity(
                "intermediate_file", files.preview.file_id, boundary_prepared),
        }

        complete_request = HostPhotoComplete(
            1,
            files.original.file_id,
            len(_ORIGINAL),
            hashlib.sha256(_ORIGINAL).hexdigest(),
            files.preview.file_id,
            len(_PREVIEW),
            hashlib.sha256(_PREVIEW).hexdigest(),
            _NOW,
        )
        completed = source.save_host_photo_complete(
            complete_request, new_operation_key(), owned)
        assert completed.kind is DbOutcomeKind.COMPLETED, completed.error
        boundary_complete = history.current_boundary()
        complete_action = history.restore_entity("action", 1, boundary_complete)
        complete_original = history.restore_entity(
            "intermediate_file", files.original.file_id, boundary_complete)
        complete_preview = history.restore_entity(
            "intermediate_file", files.preview.file_id, boundary_complete)

        original_path = Path(config.paths.staging) / files.original.relative_path
        preview_path = Path(config.paths.staging) / files.preview.relative_path
        original_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(_ORIGINAL)
        preview_path.write_bytes(_PREVIEW)
        drafts = (
            OutputDraft(OutputKind.PREVIEW,
                FileReference(intermediate_file_id=files.preview.file_id), True,
                hashlib.sha256(_PREVIEW).hexdigest(),
                original_batch_intermediate_file_id=files.original.file_id,
                original_name="capture.jpg", media_type="image/jpeg"),
            OutputDraft(OutputKind.ORIGINAL,
                FileReference(intermediate_file_id=files.original.file_id), True,
                hashlib.sha256(_ORIGINAL).hexdigest(),
                original_name="capture.3fr", media_type="image/x-hasselblad-3fr"),
        )
        finished = CaptureRepository().finish_capture(FinishCapture(
            action_id=1,
            drafts=drafts,
            catalog_facts=OutputCatalogFacts(action_id=1, ownership_confirmed=True),
            occurred_at=_NOW,
        ), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        boundary_output = history.current_boundary()
        output_ids = finished.value.output_ids
        original_output_id, = owned.connection.execute(
            "SELECT id FROM outputs WHERE intermediate_file_id=?",
            (files.original.file_id,),
        ).fetchone()
        preview_output_id, = owned.connection.execute(
            "SELECT id FROM outputs WHERE intermediate_file_id=?",
            (files.preview.file_id,),
        ).fetchone()
        assert {original_output_id, preview_output_id} == set(output_ids)

        expected_at_output = {
            ("action", 1): history.restore_entity("action", 1, boundary_output),
            ("intermediate_file", files.original.file_id): history.restore_entity(
                "intermediate_file", files.original.file_id, boundary_output),
            ("intermediate_file", files.preview.file_id): history.restore_entity(
                "intermediate_file", files.preview.file_id, boundary_output),
            **{
                ("output", output_id): history.restore_entity("output", output_id, boundary_output)
                for output_id in output_ids
            },
        }

        _accept(owned, catalog, "2", [{
            "name": "清理主机原图",
            "type": "delete_action_outputs",
            "scheduled_at": _input_time(),
            "params": {"output_ids": [str(original_output_id)]},
        }])
        cleanup_type = int(enum_for("actions.type").DELETE_ACTION_OUTPUTS)
        cleanup_action_id, = owned.connection.execute(
            "SELECT id FROM actions WHERE type=? ORDER BY id DESC LIMIT 1",
            (cleanup_type,),
        ).fetchone()
        outputs = OutputsRepository()
        for method, request in (
            (outputs.start_cleanup_action, StartCleanupAction(cleanup_action_id, _NOW)),
            (outputs.fix_cleanup_targets, FixCleanupTargets(cleanup_action_id, _NOW)),
        ):
            outcome = method(request, new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        cleanup_items = owned.connection.execute(
            "SELECT id FROM cleanup_items WHERE action_id=? ORDER BY id",
            (cleanup_action_id,),
        ).fetchall()
        assert len(cleanup_items) == 1
        item_id, = cleanup_items[0]
        restricted = outputs.restrict_cleanup_item(
            RestrictCleanupItem(item_id, _NOW), new_operation_key(), owned)
        assert restricted.kind is DbOutcomeKind.COMPLETED, restricted.error
        cleanup_runtime = session_cleanup_assembly(
            devices={}, drivers=None, staging=Path(config.paths.staging),
            max_delete_attempts=1, max_query_attempts=1,
            occurred_at=lambda: _NOW, monotonic_ns=lambda: 0,
        )(owned)
        result = await delete_source_file(cleanup_runtime.for_item(item_id), item_id)
        assert result.phase == "succeeded", result
        assert not original_path.exists()
        boundary_cleanup = history.current_boundary()
        expected_at_cleanup = {
            ("action", 1): history.restore_entity("action", 1, boundary_cleanup),
            ("intermediate_file", files.original.file_id): history.restore_entity(
                "intermediate_file", files.original.file_id, boundary_cleanup),
            ("intermediate_file", files.preview.file_id): history.restore_entity(
                "intermediate_file", files.preview.file_id, boundary_cleanup),
            ("output", original_output_id): history.restore_entity(
                "output", original_output_id, boundary_cleanup),
            ("output", preview_output_id): history.restore_entity(
                "output", preview_output_id, boundary_cleanup),
        }

        # 各 H 都对应已提交事务的完整结束位置；当前投影回放到输出 H，
        # 应逐对象恢复清理前的动作、来源及产物关系。
        assert boundary_prepared.last_event_id < boundary_complete.last_event_id
        assert boundary_complete.last_event_id < boundary_output.last_event_id
        assert boundary_output.last_event_id < boundary_cleanup.last_event_id
        for (entity, entity_id), expected in prepared_state.items():
            assert history.restore_entity(entity, entity_id, boundary_prepared) == expected
        assert history.restore_entity("action", 1, boundary_complete) == complete_action
        assert history.restore_entity(
            "intermediate_file", files.preview.file_id, boundary_complete) == complete_preview
        for (entity, entity_id), expected in expected_at_output.items():
            restored = history.restore_entity(entity, entity_id, boundary_output)
            assert restored == expected

        action_at_cleanup = history.restore_entity("action", 1, boundary_cleanup)
        original_at_cleanup = history.restore_entity(
            "intermediate_file", files.original.file_id, boundary_cleanup)
        preview_at_cleanup = history.restore_entity(
            "intermediate_file", files.preview.file_id, boundary_cleanup)
        assert action_at_cleanup[("actions", 1)]["status"] == 3
        assert original_at_cleanup[("intermediate_files", files.original.file_id)][
            "cleanup_state"] == int(enum_for("intermediate_files.cleanup_state").COMPLETED)
        assert preview_at_cleanup[("intermediate_files", files.preview.file_id)][
            "cleanup_state"] == int(enum_for("intermediate_files.cleanup_state").NOT_NEEDED)
        assert action_at_cleanup == expected_at_cleanup[("action", 1)]
        assert original_at_cleanup == expected_at_cleanup[("intermediate_file", files.original.file_id)]
        assert preview_at_cleanup == expected_at_cleanup[("intermediate_file", files.preview.file_id)]
        for output_id in output_ids:
            assert history.restore_entity("output", output_id, boundary_cleanup) == \
                expected_at_cleanup[("output", output_id)]

        # 快照位于准备 H；完整字节、提升及清理之后，恢复原图到完整字节 H
        # 应选快照正向回放，并还原清理前的精确字节事实。
        original_page = history._object_event_page
        forward_directions = []

        def record_forward(ref, lower, upper, previous, expected_count, **kwargs):
            if ref == (file_entity, files.original.file_id):
                forward_directions.append(kwargs["forward"])
            return original_page(ref, lower, upper, previous, expected_count, **kwargs)

        monkeypatch.setattr(history, "_object_event_page", record_forward)
        restored_forward = history.restore_entity(
            "intermediate_file", files.original.file_id, boundary_complete)
        assert forward_directions and all(forward_directions)
        assert restored_forward == complete_original
        assert complete_preview == history.restore_entity(
            "intermediate_file", files.preview.file_id, boundary_complete)

        # H 处的原图和预览分别保有实际原始角色身份，预览仍关联原图产物。
        assert expected_at_output[("output", preview_output_id)][
            ("output_origins", preview_output_id)]["original_output_id"] == original_output_id
        assert original_path.parent.name == "capture-originals"
        assert preview_path.parent.name == "capture-previews"
    finally:
        owned.connection.close()
