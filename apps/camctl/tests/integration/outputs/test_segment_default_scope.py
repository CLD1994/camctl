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
from camctl.persistence.transaction import row_facts, encode_json_value

from ..capture.test_record_segment_scope_recovery import (
    _stopped_world,
    SegmentWaitCatalog,
)
from ..capture.test_record_file_completion_wait import (
    _result_port,
    _completed_result,
    _stop_context,
)
from ..persistence.test_segment_file_pairing import _entry
from .test_sources import _seed_output

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
    owned, runtime, aid, handler, _ = await _stopped_world(
        tmp_path,
        control_known=known,
        pending_capture_group=pending_capture_group,
    )
    runtime.check_config = type(runtime.check_config)(1, Decimal("1"), Decimal(0))
    ctx = _stop_context(owned, aid)

    async def read(request, batch):
        entries = []
        for index in indexes:
            name = f"CAM_001/CAM_20261022100000_{index:04d}_D."
            entries.extend(
                (
                    _entry(name + "OSV", size=10 if sizes is None else sizes[index]),
                    _entry(name + "LRF"),
                )
            )
        return _completed_result(
            request.ticket,
            ctx,
            entries=sorted(entries, key=lambda item: item["identity"]),
            finalized=finalized,
        )

    _result_port(runtime, read)
    await capture_handler(handler)(aid, runtime)
    return owned, runtime, aid


def _obtain(owned, runtime, source_id, mode, request_id, *, exclude_output_ids=None):
    now = runtime.wall_us()
    instant = datetime.fromtimestamp(now // 1000000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    params = {
        "source": source_id
        if isinstance(source_id, dict)
        else {"action_instance_id": str(source_id)}
    }
    if mode == "preview":
        params["filter"] = "preview"
    elif isinstance(mode, list):
        params["output_ids"] = [str(identity) for identity in mode]
    if exclude_output_ids is not None:
        params["exclude_output_ids"] = [str(identity) for identity in exclude_output_ids]
    request = {
        "request_id": str(request_id),
        "created_at": instant,
        "name": "分段取回",
        "actions": [
            {
                "name": "取回",
                "type": "obtain_action_outputs",
                "scheduled_at": instant,
                "params": params,
            }
        ],
    }
    saved = AcceptanceRepository().process_input(
        ProcessInput(
            ParsedInput("obtain.json", request), ObtainCatalog(), CommandMode.RUN, now
        ),
        new_operation_key(),
        owned,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    action_id = owned.connection.execute("SELECT MAX(id) FROM actions").fetchone()[0]
    repo = OutputsRepository()
    receipt = repo.start_obtain_action(
        StartObtainAction(action_id, now), new_operation_key(), owned
    )
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    owner = row_facts(owned.connection, "actions", action_id)
    receipt = repo.resolve_sources(
        ResolveSources(
            action_id,
            read_source_spec(encode_json_value(owner["input_fields_json"])),
            now,
        ),
        new_operation_key(),
        owned,
    )
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    selection = owned.connection.execute(
        "SELECT s.id FROM obtain_source_selections s JOIN action_dependencies d ON d.id=s.dependency_id WHERE d.action_id=?",
        (action_id,),
    ).fetchone()[0]
    return action_id, selection


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


async def test_cleanup_and_cold_recovery_preserve_scope_for_new_selections(tmp_path):
    from camctl.capture.files import FilePresenceSave
    from camctl.contracts.enums import enum_for
    from camctl.outputs.cleanup_flow import StartCleanupAction, FixCleanupTargets
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..bootstrap.test_output_binding_changes import _accept
    from .test_cleanup_query_save_recovery import _start_cleanup

    owned, runtime, aid = await _source(tmp_path)
    try:
        originals = [row[0] for row in owned.connection.execute(
            "SELECT id FROM outputs WHERE kind=1 ORDER BY id")]
        previews = [row[0] for row in owned.connection.execute(
            "SELECT id FROM outputs WHERE kind=3 ORDER BY id")]
        scope = owned.connection.execute(
            "SELECT * FROM segment_scope_members ORDER BY ordinal").fetchall()
        _, original_selection = _obtain(owned, runtime, aid, "default", 100)
        original_request = FixSegmentSelection(original_selection, runtime.wall_us())
        original_key = new_operation_key()
        first = fix_segment_selection(original_request, original_key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        original_items = owned.connection.execute(
            "SELECT * FROM obtain_items WHERE selection_id=? ORDER BY id",
            (original_selection,)).fetchall()

        _, cleanup_item = _start_cleanup(owned, originals[1], "902")
        assert owned.connection.execute(
            "SELECT requested_output_id,output_id FROM cleanup_items WHERE id=?",
            (cleanup_item,)).fetchone() == (originals[1], originals[1])
        file_id = owned.connection.execute(
            "SELECT device_file_id FROM outputs WHERE id=?", (originals[1],)).fetchone()[0]
        absent = runtime.capture.save_file_presence(FilePresenceSave(
            file_id, int(enum_for("device_files.presence_state").ABSENT), runtime.wall_us()),
            new_operation_key(), owned)
        assert absent.kind is DbOutcomeKind.COMPLETED, absent.error
        owned.connection.close()
        owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        runtime.owned = owned
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
            saved = fix_segment_selection(FixSegmentSelection(selection, runtime.wall_us()),
                new_operation_key(), owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            assert owned.connection.execute(
                "SELECT output_id,status FROM obtain_items WHERE selection_id=? ORDER BY id",
                (selection,)).fetchall() == list(zip(expected, statuses))
        assert owned.connection.execute(
            "SELECT * FROM segment_scope_members ORDER BY ordinal").fetchall() == scope

        _accept(owned, "903", [{"name": "清理来源", "type": "delete_action_outputs",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"source": {"action_instance_id": str(aid)}}}])
        cleanup_id = owned.connection.execute("SELECT MAX(id) FROM actions").fetchone()[0]
        repository = OutputsRepository()
        started = repository.start_cleanup_action(StartCleanupAction(cleanup_id, runtime.wall_us()),
            new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        owner = row_facts(owned.connection, "actions", cleanup_id)
        resolved = repository.resolve_sources(ResolveSources(cleanup_id,
            read_source_spec(encode_json_value(owner["input_fields_json"])), runtime.wall_us()),
            new_operation_key(), owned)
        assert resolved.kind is DbOutcomeKind.COMPLETED, resolved.error
        fixed = repository.fix_cleanup_targets(FixCleanupTargets(cleanup_id, runtime.wall_us()),
            new_operation_key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
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
    from camctl.outputs.obtain_flow import (
        ObtainRuntime,
        _fix_pending_selections,
        _publish_and_finish,
    )
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
        plan_id = runtime.action(aid)["plan_id"]
        scenarios = []
        for request_id, scope in ((2, "group"), (3, "plan")):
            source = {"plan_instance_id": str(plan_id)}
            if scope == "group":
                source["group"] = "同组分段"
            action, selection = _obtain(
                owned, runtime, source, "default", request_id
            )
            scenarios.append((scope, action, selection))
        obtain = ObtainRuntime(
            owned,
            {},
            DeliveryDirectories(
                tmp_path / "staging", tmp_path / "ready", tmp_path / "processing"
            ),
            1024,
            RetryWaitGate(),
            runtime.monotonic_ns,
            runtime.wall_us,
            OutputsRepository(),
            OperationRepository(),
        )
        _fix_pending_selections(obtain, runtime.wall_us())
        for scope, action, _ in scenarios:
            facts = load_obtain_facts(owned.connection, action)
            assert facts.waiting_sources == 1 and facts.total == 3, scope
            assert decide_obtain_finish(facts).phase is ObtainPhase.WAIT_SOURCES, scope
            await _publish_and_finish(
                obtain, action, runtime.wall_us(), publish_limit=1
            )
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
        assert runtime.action(pending)["status"] == 2
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
        obtain.discovery.begin_round(owned.connection, now)
        _fix_pending_selections(obtain, now)
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
    tmp_path, mode, monkeypatch
):
    owned, runtime, aid = await _source(tmp_path, indexes=(), known=False)
    try:
        from camctl.persistence.repositories import outputs as outputs_repository

        monkeypatch.setattr(
            outputs_repository,
            "_catalog_members",
            lambda *_args: pytest.fail(
                "有界失败来源判定不能枚举完整产物目录"
            ),
        )
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
    from camctl.outputs.obtain_flow import (
        ObtainRuntime,
        _fix_pending_selections,
        _publish_and_finish,
    )
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
        obtain = ObtainRuntime(
            owned,
            {},
            DeliveryDirectories(
                tmp_path / "staging", tmp_path / "ready", tmp_path / "processing"
            ),
            1024,
            RetryWaitGate(),
            runtime.monotonic_ns,
            runtime.wall_us,
            OutputsRepository(),
            OperationRepository(),
        )
        _fix_pending_selections(obtain, runtime.wall_us())
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
        await _publish_and_finish(obtain, action, runtime.wall_us(), publish_limit=1)
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
async def test_default_segment_exclusions_keep_scope_and_replay(tmp_path, all_excluded):
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
                runtime.owned = owned
        if all_excluded:
            for operation_key in (new_operation_key(), new_operation_key()):
                done = OutputsRepository().finish_obtain(
                    FinishObtain(action, runtime.wall_us()), operation_key, owned)
                assert done.kind is DbOutcomeKind.COMPLETED, done.error
                assert (done.value.action_status, done.value.prepared, done.value.failed) == (3, 0, 0)
        assert owned.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (0,)
    finally:
        owned.connection.close()
