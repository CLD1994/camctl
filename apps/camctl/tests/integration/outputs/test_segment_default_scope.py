"""真实拍摄发布、来源解析与有界默认选择共同使用冻结范围。"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.contracts.values import new_operation_key
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
            name = f"CAM001/CAM_20261022100000_{index:04d}_D."
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


def _obtain(owned, runtime, source_id, mode, request_id):
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


@pytest.mark.parametrize("mode", ["default", "preview", "explicit"])
async def test_default_scope_and_explicit_ids_select_independently(tmp_path, mode):
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
        action, selection = _obtain(
            owned, runtime, aid, originals[3:5] if mode == "explicit" else mode, 2
        )
        request = FixSegmentSelection(selection, runtime.wall_us())
        key = new_operation_key()
        receipt = fix_segment_selection(request, key, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        expected = (
            originals[3:5]
            if mode == "explicit"
            else previews[:3]
            if mode == "preview"
            else originals[:3]
        )
        actual = [
            row[0]
            for row in owned.connection.execute(
                "SELECT output_id FROM obtain_items WHERE selection_id=? ORDER BY id",
                (selection,),
            )
        ]
        assert actual == expected
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (
            12,
        )
        before = owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone()
        again = fix_segment_selection(request, key, owned)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert (
            owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
            == before
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM deliveries"
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("scope", ["group", "plan"])
async def test_group_waits_for_pending_source_and_keeps_successful_member_scope(
    tmp_path, scope
):
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
        source = {"plan_instance_id": str(plan_id)}
        if scope == "group":
            source["group"] = "同组分段"
        action, _ = _obtain(owned, runtime, source, "default", 2)
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
        facts = load_obtain_facts(owned.connection, action)
        assert facts.waiting_sources == 1 and facts.total == 3
        assert decide_obtain_finish(facts).phase is ObtainPhase.WAIT_SOURCES
        await _publish_and_finish(obtain, action, runtime.wall_us(), publish_limit=1)
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
        facts = load_obtain_facts(owned.connection, action)
        assert (
            facts.waiting_sources == 0
            and facts.total == 3
            and facts.failed_sources == 1
        )
        assert decide_obtain_finish(facts).phase is ObtainPhase.WAIT_PREPARATION
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM deliveries"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM obtain_items"
        ).fetchone() == (3,)
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


@pytest.mark.parametrize("scope", ["empty", "unknown"])
async def test_default_scope_failure_does_not_create_delivery_targets(tmp_path, scope):
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
        action, selection = _obtain(owned, runtime, aid, "default", 2)
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
