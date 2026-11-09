"""真实拍摄发布、来源解析与有界默认选择共同使用冻结范围。"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import item_error_id
from camctl.capture.handlers import capture_handler
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    ProcessInput,
)
from camctl.persistence.repositories.outputs import (
    OutputsRepository,
    StartObtainAction,
    ResolveSources,
)
from camctl.persistence.repositories.segment_selections import (
    FixSegmentSelection,
    fix_segment_selection,
)
from camctl.outputs.sources import read_source_spec
from camctl.persistence.transaction import row_facts

from ..capture.test_record_segment_scope_recovery import (
    _stopped_world,
    SegmentWaitCatalog,
)
from ..capture.test_record_file_completion_wait import (
    _completed_result,
    _stop_context,
)
from ..persistence.test_segment_file_pairing import _entry
from .test_sources import _seed_output
from ..support.outputs import prepare_obtain_selection, segmented_output_source

pytestmark = pytest.mark.asyncio


class ObtainCatalog(SegmentWaitCatalog):
    def action_types(self):
        return super().action_types() | {"obtain_action_outputs"}


async def _source(
    tmp_path,
    indexes=(7, 8, 9, 10, 11, 12),
    *,
    known=True,
    sizes=None,
    finalized=True,
    pending_capture_group=None,
):
    entries = []
    for index in indexes:
        name = f"CAM_001/CAM_20261022100000_{index:04d}_D."
        entries.extend((
            _entry(name + "OSV", size=10 if sizes is None else sizes[index]),
            _entry(name + "LRF"),
        ))
    return await segmented_output_source(
        tmp_path, catalog=SegmentWaitCatalog(),
        entries=sorted(entries, key=lambda item: item["identity"]),
        known=known, finalized=finalized, pending_capture_group=pending_capture_group,
    )


def _obtain(owned, runtime, source_id, mode, request_id, *, exclude_output_ids=None):
    return prepare_obtain_selection(
        owned, catalog=ObtainCatalog(), now=runtime.wall_us(), source_id=source_id,
        mode=mode, request_id=request_id, exclude_output_ids=exclude_output_ids,
    )


async def test_default_scope_and_explicit_ids_select_independently(tmp_path):
    owned, runtime, aid = await _source(tmp_path)
    try:
        originals = [
            row[0]
            for row in owned.connection.execute(
                "SELECT id FROM outputs WHERE kind=1 ORDER BY id"
            )
        ]
        previews = [
            row[0]
            for row in owned.connection.execute(
                "SELECT id FROM outputs WHERE kind=3 ORDER BY id"
            )
        ]
        scenarios = (
            ("default", "default", 2, originals[:3]),
            ("preview", "preview", 3, previews[:3]),
            ("explicit", originals[3:5], 4, originals[3:5]),
        )
        action_ids = []
        selection_items = {}
        operation_keys = set()
        for label, mode, request_id, expected in scenarios:
            action, selection = _obtain(owned, runtime, aid, mode, request_id)
            action_ids.append(action)
            request = FixSegmentSelection(selection, runtime.wall_us())
            key = new_operation_key()
            assert key not in operation_keys, label
            operation_keys.add(key)
            receipt = fix_segment_selection(request, key, owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            actual = [
                row[0]
                for row in owned.connection.execute(
                    "SELECT output_id FROM obtain_items WHERE selection_id=? ORDER BY id",
                    (selection,),
                )
            ]
            assert actual == expected, label
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM outputs"
            ).fetchone() == (12,)
            before = owned.connection.execute(
                "SELECT COUNT(*) FROM history_events"
            ).fetchone()
            again = fix_segment_selection(request, key, owned)
            assert again.kind is DbOutcomeKind.COMPLETED, again.error
            assert (
                owned.connection.execute(
                    "SELECT COUNT(*) FROM history_events"
                ).fetchone()
                == before
            )
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM deliveries"
            ).fetchone() == (0,)
            selection_items[selection] = expected
            for fixed_selection, fixed_items in selection_items.items():
                actual_fixed_items = [
                    row[0]
                    for row in owned.connection.execute(
                        "SELECT output_id FROM obtain_items WHERE selection_id=?"
                        " ORDER BY id",
                        (fixed_selection,),
                    )
                ]
                assert actual_fixed_items == fixed_items
        assert len(set(action_ids)) == len(scenarios)
        assert len(selection_items) == len(scenarios)
        assert len(operation_keys) == len(scenarios)
    finally:
        owned.connection.close()


async def test_cleanup_and_cold_recovery_preserve_scope_for_new_selections(tmp_path, capture_resources):
    """核对选择、清理和取消仓储的协作及重开后的原键重送。"""
    from camctl.cancellation.models import (
        ApplyCancelTarget, CancelApplyMode, CancellationEffect, CancelOutcomeChoice,
        FinishCancelAction, FixCancelTargets, FixedCancelSet, FixedTarget,
        RecordCancelResult, SelectionBasis, StartCancelAction,
    )
    from camctl.capture.files import FilePresenceSave
    from camctl.contracts.enums import enum_for
    from camctl.outputs.cleanup_flow import (
        CleanupItemDisposition, CleanupTargetsDisposition, FixCleanupTargets,
        RestrictCleanupItem, StartCleanupAction,
    )
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from camctl.persistence.repositories.outputs import SettleCanceledObtain
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..bootstrap.test_output_binding_changes import Catalog
    from ..support.outputs import submit_output_document

    owned, runtime, aid = await _source(tmp_path)
    try:
        now = runtime.wall_us()
        instant = datetime.fromtimestamp(now // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        originals = [row[0] for row in owned.connection.execute(
            "SELECT id FROM outputs WHERE kind=1 ORDER BY id")]
        previews = [row[0] for row in owned.connection.execute(
            "SELECT id FROM outputs WHERE kind=3 ORDER BY id")]
        assert len(originals) == len(previews) == 6
        scope = owned.connection.execute(
            "SELECT * FROM segment_scope_members ORDER BY ordinal").fetchall()
        original_action, original_selection = _obtain(owned, runtime, aid, "default", 100)
        original_request = FixSegmentSelection(original_selection, now)
        original_key = new_operation_key()
        first = fix_segment_selection(original_request, original_key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        identity_query = (
            "SELECT id,selection_id,requested_output_id,output_id,basis,original_output_id,"
            "preview_output_id,preview_size,repaired_size FROM obtain_items"
            " WHERE selection_id=? ORDER BY id"
        )
        original_identities = owned.connection.execute(identity_query, (original_selection,)).fetchall()
        assert [row[3] for row in original_identities] == originals[:3]
        item_ids = tuple(row[0] for row in original_identities)
        assert owned.connection.execute(
            "SELECT status,source_dependency,delivery_id FROM obtain_items"
            " WHERE selection_id=? ORDER BY id", (original_selection,)).fetchall() == [
                (int(enum_for("obtain_items.status").SELECTED), 0, None)] * 3

        # 本例是存储契约协作：取消已固定但尚未开始读取的原取回。
        accepted = await submit_output_document(
            runtime.database, catalog=Catalog(), occurred_at=now, body={
                "request_id": "901", "created_at": instant, "name": "取消取回",
                "actions": [{"name": "取消取回", "type": "cancel_task",
                             "scheduled_at": instant,
                             "params": {"target": {"action_instance_id": str(original_action)}}}],
            })
        assert accepted.plan_id is not None, accepted
        cancel_id, = owned.connection.execute(
            "SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
        cancellations = CancellationRepository()
        started = cancellations.start_cancel_action(StartCancelAction(cancel_id, now), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        fixed_cancel = cancellations.fix_cancel_targets(FixCancelTargets(
            cancel_id, FixedCancelSet((FixedTarget(original_action, SelectionBasis.DIRECT,
                                                   CancellationEffect.NOT_APPLIED),)), now),
            new_operation_key(), owned)
        assert fixed_cancel.kind is DbOutcomeKind.COMPLETED, fixed_cancel.error
        cancel_item, = fixed_cancel.value.item_ids
        applied = cancellations.apply_cancel_target(ApplyCancelTarget(
            cancel_item, CancelApplyMode.WITH_STOP, now), new_operation_key(), owned)
        assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
        repository = OutputsRepository()
        settled = repository.settle_canceled_obtain(
            SettleCanceledObtain(original_action, now, item_ids), new_operation_key(), owned)
        assert settled.kind is DbOutcomeKind.COMPLETED, settled.error
        assert settled.value.complete and not settled.value.file_ids
        recorded = cancellations.record_cancel_result(RecordCancelResult(
            cancel_item, now, outcome=CancelOutcomeChoice.CANCELED), new_operation_key(), owned)
        assert recorded.kind is DbOutcomeKind.COMPLETED, recorded.error
        finished = cancellations.finish_cancel_action(FinishCancelAction(cancel_id, now), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (original_action,)).fetchone() == (
            int(enum_for("actions.status").CANCELED),)
        assert owned.connection.execute(identity_query, (original_selection,)).fetchall() == original_identities
        assert owned.connection.execute(
            "SELECT status,source_dependency,delivery_id FROM obtain_items"
            " WHERE selection_id=? ORDER BY id", (original_selection,)).fetchall() == [
                (int(enum_for("obtain_items.status").CANCELED), 0, None)] * 3

        accepted = await submit_output_document(
            runtime.database, catalog=Catalog(), occurred_at=now, body={
                "request_id": "902", "created_at": instant, "name": "清理产物",
                "actions": [{"name": "清理产物", "type": "delete_action_outputs",
                             "scheduled_at": instant,
                             "params": {"output_ids": [str(originals[1])]}}],
            })
        assert accepted.plan_id is not None, accepted
        cleanup_id, = owned.connection.execute(
            "SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
        started = repository.start_cleanup_action(StartCleanupAction(cleanup_id, now), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        fixed = repository.fix_cleanup_targets(FixCleanupTargets(cleanup_id, now), new_operation_key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        assert fixed.value.disposition is CleanupTargetsDisposition.SAVED
        cleanup_item, = fixed.value.item_ids
        restricted = repository.restrict_cleanup_item(
            RestrictCleanupItem(cleanup_item, now), new_operation_key(), owned)
        assert restricted.kind is DbOutcomeKind.COMPLETED, restricted.error
        assert restricted.value.disposition is CleanupItemDisposition.SAVED
        assert owned.connection.execute(
            "SELECT requested_output_id,output_id,restriction_state FROM cleanup_items WHERE id=?",
            (cleanup_item,)).fetchone() == (
                originals[1], originals[1], int(enum_for("cleanup_items.restriction_state").ACTIVE))
        file_id = owned.connection.execute(
            "SELECT device_file_id FROM outputs WHERE id=?", (originals[1],)).fetchone()[0]
        absent = runtime.capture.save_file_presence(FilePresenceSave(
            file_id, int(enum_for("device_files.presence_state").ABSENT), now), new_operation_key(), owned)
        assert absent.kind is DbOutcomeKind.COMPLETED, absent.error
        owned.connection.close()
        database = await capture_resources.restart_database(tmp_path / "state.db")
        owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        runtime = await capture_resources.runtime(
            database, driver=runtime.driver, evidence=runtime.evidence,
            wall_us=runtime.wall_us, monotonic_ns=runtime.monotonic_ns,
        )
        assert owned.connection.execute(
            "SELECT presence_state FROM device_files WHERE id=?", (file_id,)).fetchone() == (
                int(enum_for("device_files.presence_state").ABSENT),)
        original_items = owned.connection.execute(
            "SELECT * FROM obtain_items WHERE selection_id=? ORDER BY id", (original_selection,)).fetchall()
        history_before = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        repeated = fix_segment_selection(original_request, original_key, owned)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert repeated.value == first.value
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == history_before
        assert owned.connection.execute(
            "SELECT * FROM obtain_items WHERE selection_id=? ORDER BY id",
            (original_selection,)).fetchall() == original_items

        for request_id, mode, expected, statuses in (
            (101, "default", originals[:3], [2, 4, 2]),
            (102, "preview", previews[:3], [2, 2, 2]),
            (103, originals[3:5], originals[3:5], [2, 2]),
        ):
            _, selection = _obtain(owned, runtime, aid, mode, request_id)
            saved = fix_segment_selection(FixSegmentSelection(selection, now), new_operation_key(), owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            rows = owned.connection.execute(
                "SELECT output_id,status,error_code FROM obtain_items WHERE selection_id=? ORDER BY id",
                (selection,)).fetchall()
            assert [(row[0], row[1]) for row in rows] == list(zip(expected, statuses))
            errors = [None] * len(expected)
            if mode == "default":
                errors[1] = item_error_id("obtain_items", "output_cleanup_started")
            assert [row[2] for row in rows] == errors
        assert owned.connection.execute(
            "SELECT * FROM segment_scope_members ORDER BY ordinal").fetchall() == scope

        accepted = await submit_output_document(
            runtime.database, catalog=Catalog(), occurred_at=now, body={
                "request_id": "903", "created_at": instant, "name": "清理来源",
                "actions": [{"name": "清理来源", "type": "delete_action_outputs",
                             "scheduled_at": instant,
                             "params": {"source": {"action_instance_id": str(aid)}}}],
            })
        assert accepted.plan_id is not None, accepted
        cleanup_id, = owned.connection.execute(
            "SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
        started = repository.start_cleanup_action(StartCleanupAction(cleanup_id, now), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        fields, = owned.connection.execute(
            "SELECT input_fields_json FROM actions WHERE id=?", (cleanup_id,)).fetchone()
        resolved = repository.resolve_sources(ResolveSources(cleanup_id,
            read_source_spec(fields), now),
            new_operation_key(), owned)
        assert resolved.kind is DbOutcomeKind.COMPLETED, resolved.error
        fixed = repository.fix_cleanup_targets(FixCleanupTargets(cleanup_id, now), new_operation_key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        assert fixed.value.disposition is CleanupTargetsDisposition.SAVED
        assert {row[0] for row in owned.connection.execute(
            "SELECT requested_output_id FROM cleanup_items WHERE action_id=?",
            (cleanup_id,))} == set(originals + previews)
    finally:
        owned.connection.close()


async def test_group_waits_for_pending_source_and_keeps_successful_member_scope(tmp_path):
    from camctl.devices.directory import DirectoryRead
    from camctl.operations.attempts import RetryWaitGate
    from camctl.operations.models import ErrorValue
    from camctl.outputs.handoff import DeliveryDirectories
    from camctl.outputs.obtain_summary import ObtainPhase, decide_obtain_finish
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.persistence.repositories.outputs import load_obtain_facts
    from camctl.persistence.repositories.scheduling import (
        ObserveWindowRequest,
        SchedulingRepository,
        StartActionRequest,
    )
    from ..capture.test_baseline_prepare import Directory

    owned, runtime, aid = await _source(tmp_path, pending_capture_group="同组分段")
    try:
        plan_id = (await runtime.action(aid))["plan_id"]
        scenarios = []
        for request_id, scope in ((2, "group"), (3, "plan")):
            source = {"plan_instance_id": str(plan_id)}
            if scope == "group":
                source["group"] = "同组分段"
            action, selection = _obtain(
                owned, runtime, source, "default", request_id
            )
            scenarios.append((scope, action, selection))
        for _scope, _action, selection in scenarios:
            fixed = fix_segment_selection(
                FixSegmentSelection(selection, runtime.wall_us()), new_operation_key(), owned)
            assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        for scope, action, _ in scenarios:
            facts = load_obtain_facts(owned.connection, action)
            assert facts.waiting_sources == 1 and facts.total == 3, scope
            assert decide_obtain_finish(facts).phase is ObtainPhase.WAIT_SOURCES, scope
            from camctl.persistence.repositories.outputs import FinishObtain
            finish = OutputsRepository().finish_obtain(
                FinishObtain(action, runtime.wall_us()), new_operation_key(), owned)
            assert finish.kind is DbOutcomeKind.ROLLED_BACK
            assert row_facts(owned.connection, "actions", action)["status"] == 2
        pending = aid + 1
        now = runtime.wall_us()
        scheduling = SchedulingRepository()
        for save, request in (
            (scheduling.observe_window, ObserveWindowRequest(pending, now, now)),
            (scheduling.start_action, StartActionRequest(pending, now, now)),
        ):
            receipt = save(request, new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert (await runtime.action(pending))["status"] == 2
        runtime.baseline_directory = Directory(
            [
                lambda request: DirectoryRead(
                    None,
                    ErrorValue(
                        "adb_directory_failed",
                        "device_file_access",
                        {"reason": "actual_error"},
                    ),
                    None,
                ),
            ]
        )
        await capture_handler("camera_record")(pending, runtime)
        pending_selections = [row[0] for row in owned.connection.execute(
            "SELECT s.id FROM obtain_source_selections s JOIN action_dependencies d"
            " ON d.id=s.dependency_id WHERE d.depends_on_action_id=? AND s.status=1", (pending,))]
        assert len(pending_selections) == 2
        for selection in pending_selections:
            fixed = fix_segment_selection(FixSegmentSelection(selection, now), new_operation_key(), owned)
            assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        for scope, action, selection in scenarios:
            facts = load_obtain_facts(owned.connection, action)
            assert (
                facts.waiting_sources == 0
                and facts.total == 3
                and facts.failed_sources == 1
            ), scope
            assert (
                decide_obtain_finish(facts).phase is ObtainPhase.WAIT_PREPARATION
            ), scope
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM obtain_items WHERE selection_id=?",
                (selection,),
            ).fetchone() == (3,)
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM obtain_items i"
                " JOIN obtain_source_selections s ON s.id=i.selection_id"
                " JOIN action_dependencies dep ON dep.id=s.dependency_id"
                " WHERE dep.action_id=?",
                (action,),
            ).fetchone() == (3,)
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM deliveries d JOIN obtain_items i"
                " ON i.delivery_id=d.id JOIN obtain_source_selections s"
                " ON s.id=i.selection_id JOIN action_dependencies dep"
                " ON dep.id=s.dependency_id WHERE dep.action_id=?",
                (action,),
            ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM obtain_items"
        ).fetchone() == (6,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM deliveries"
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize(
    ("indexes", "known", "code", "items"),
    [
        ((7, 8, 10), True, None, 2),
        ((7,), False, None, 1),
    ],
)
async def test_failed_source_uses_confirmed_prefix_or_source_failure(
    tmp_path, indexes, known, code, items
):
    owned, runtime, aid = await _source(tmp_path, indexes, known=known)
    try:
        action, selection = _obtain(owned, runtime, aid, "default", 2)
        receipt = fix_segment_selection(
            FixSegmentSelection(selection, runtime.wall_us()),
            new_operation_key(),
            owned,
        )
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        row = row_facts(owned.connection, "obtain_source_selections", selection)
        assert row["status"] == 2 and row["error_code"] == code
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM obtain_items WHERE selection_id=?", (selection,)
        ).fetchone() == (items if code is None else 0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mode", ["default", "preview"])
async def test_failed_segment_source_with_complete_empty_catalog_saves_source_error(
    tmp_path, mode
):
    owned, runtime, aid = await _source(tmp_path, indexes=(), known=False)
    try:
        assert row_facts(owned.connection, "actions", aid)["status"] == 4
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (aid,)
        ).fetchone() == (0,)
        _, selection = _obtain(owned, runtime, aid, mode, 2)
        request = FixSegmentSelection(selection, runtime.wall_us())
        key = new_operation_key()
        receipt = fix_segment_selection(request, key, owned)

        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        row = row_facts(owned.connection, "obtain_source_selections", selection)
        assert row["status"] == 2
        assert row["error_code"] == item_error_id(
            "obtain_source_selections", "source_action_failed"
        )
        assert row["error_details_json"] == {"source_action_instance_id": str(aid)}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM obtain_items WHERE selection_id=?", (selection,)
        ).fetchone() == (0,)

        db_path = owned.connection.execute("PRAGMA database_list").fetchone()[2]
        owned.connection.close()
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

        owned = open_existing(Path(db_path), DbOpenMode.EXISTING_RW, DbConfig())
        before = owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone()
        repeated = fix_segment_selection(request, key, owned)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert repeated.value == receipt.value
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone() == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mode", ["default", "preview"])
async def test_failed_segment_source_with_only_formal_photo_keeps_scope_error(
    tmp_path, mode,
):
    owned, runtime, aid = await _source(tmp_path, indexes=(), known=False)
    try:
        _seed_output(owned.connection, 9901, aid, 1, file_id=9902)
        owned.connection.execute(
            "UPDATE outputs SET original_name='photo.jpg',media_type='image/jpeg' WHERE id=9901"
        )
        owned.connection.execute(
            "UPDATE device_files SET original_name='photo.jpg',media_type='image/jpeg' WHERE id=9902"
        )
        owned.connection.commit()
        assert row_facts(owned.connection, "actions", aid)["status"] == 4
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (aid,)
        ).fetchone() == (1,)
        _, selection = _obtain(owned, runtime, aid, mode, 2)
        receipt = fix_segment_selection(
            FixSegmentSelection(selection, runtime.wall_us()),
            new_operation_key(),
            owned,
        )

        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        row = row_facts(owned.connection, "obtain_source_selections", selection)
        assert row["error_code"] == item_error_id(
            "obtain_source_selections", "default_segment_scope_unconfirmed"
        )
        assert row["error_code"] != item_error_id(
            "obtain_source_selections", "source_action_failed"
        )
    finally:
        owned.connection.close()


async def test_failed_segment_explicit_id_keeps_item_not_found_error(tmp_path):
    owned, runtime, aid = await _source(tmp_path, indexes=(), known=False)
    try:
        _, selection = _obtain(owned, runtime, aid, [9909], 2)
        receipt = fix_segment_selection(
            FixSegmentSelection(selection, runtime.wall_us()),
            new_operation_key(),
            owned,
        )

        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        saved_selection = row_facts(
            owned.connection, "obtain_source_selections", selection
        )
        assert saved_selection["error_code"] is None
        rows = owned.connection.execute(
            "SELECT requested_output_id,error_code,error_details_json FROM obtain_items"
            " WHERE selection_id=? ORDER BY id",
            (selection,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][:2] == (
            9909,
            item_error_id("obtain_items", "output_not_found"),
        )
        assert json.loads(rows[0][2]) == {"requested_output_id": "9909"}
    finally:
        owned.connection.close()


@pytest.mark.parametrize("scope", ["empty", "unknown"])
@pytest.mark.parametrize("exclude", [False, True])
async def test_default_scope_failure_does_not_create_delivery_targets(tmp_path, scope, exclude):
    from camctl.contracts.workflow_errors import item_error_id
    from camctl.operations.attempts import RetryWaitGate
    from camctl.outputs.handoff import DeliveryDirectories
    from camctl.persistence.repositories.operations import OperationRepository

    owned, runtime, aid = await _source(
        tmp_path,
        (7, 8, 9),
        sizes={7: 0, 8: 10, 9: 10} if scope == "empty" else None,
        finalized=scope != "unknown",
    )
    try:
        identities = [row[0] for row in owned.connection.execute("SELECT id FROM outputs")]
        action, selection = _obtain(owned, runtime, aid, "default", 2,
                                    exclude_output_ids=identities if exclude else None)
        from types import SimpleNamespace
        from camctl.devices.drivers.registry import DriverRegistry
        from camctl.bootstrap.obtain_assembly import obtain_flow
        from ..support.outputs import obtain_factory
        factory = obtain_factory(
            devices={}, drivers=DriverRegistry(), staging=tmp_path / "staging",
            ready=tmp_path / "ready", processing=tmp_path / "processing",
            occurred_at=runtime.wall_us, monotonic_ns=runtime.monotonic_ns,
        )
        await obtain_flow(factory)(SimpleNamespace(database=runtime.database))
        row = row_facts(owned.connection, "obtain_source_selections", selection)
        code = (
            "default_segment_scope_empty"
            if scope == "empty"
            else "default_segment_scope_unconfirmed"
        )
        assert row["status"] == 2 and row["error_code"] == item_error_id(
            "obtain_source_selections", code
        )
        assert row["error_details_json"]["retained_original_count"] > 0
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM obtain_items"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM deliveries"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies"
        ).fetchone() == (0,)
        finished = row_facts(owned.connection, "actions", action)
        assert finished["status"] == 4
        assert finished["error_details_json"] == {}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM obtain_items"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM deliveries"
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("all_excluded", [False, True])
async def test_default_segment_exclusions_keep_scope_and_replay(tmp_path, all_excluded, capture_resources):
    from camctl.persistence.repositories.outputs import FinishObtain
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    owned, runtime, aid = await _source(tmp_path)
    try:
        originals = [row[0] for row in owned.connection.execute(
            "SELECT id FROM outputs WHERE kind=1 ORDER BY id")]
        excluded = originals if all_excluded else [originals[0], originals[-1], 9909]
        expected = [] if all_excluded else originals[1:3]
        action, selection = _obtain(owned, runtime, aid, "default", 2,
                                    exclude_output_ids=excluded)
        request, key = FixSegmentSelection(selection, runtime.wall_us()), new_operation_key()
        for iteration in range(2):
            receipt = fix_segment_selection(request, key, owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            actual = [row[0] for row in owned.connection.execute(
                "SELECT output_id FROM obtain_items WHERE selection_id=? ORDER BY id", (selection,))]
            assert actual == expected
            if iteration == 0:
                owned.connection.close()
                owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
                database = await capture_resources.restart_database(tmp_path / "state.db")
                runtime = await capture_resources.runtime(
                    database, driver=runtime.driver, evidence=runtime.evidence,
                    wall_us=runtime.wall_us, monotonic_ns=runtime.monotonic_ns,
                )
        if all_excluded:
            for operation_key in (new_operation_key(), new_operation_key()):
                done = OutputsRepository().finish_obtain(
                    FinishObtain(action, runtime.wall_us()), operation_key, owned)
                assert done.kind is DbOutcomeKind.COMPLETED, done.error
                assert (done.value.action_status, done.value.prepared, done.value.failed) == (3, 0, 0)
        assert owned.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (0,)
    finally:
        owned.connection.close()
