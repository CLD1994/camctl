"""已保存缺席与原失败终态分别交接，使用真实仓储及完整历史。"""

from dataclasses import replace
from pathlib import Path
import json

import pytest

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.registry import DriverRegistry
from camctl.outputs.cleanup_absence import HandoffCleanupAbsence
from camctl.outputs.cleanup_flow import (
    advance_cleanup,
    delete_source_file,
    FailCleanupItem,
    FinishCleanupAction,
)
from camctl.outputs.cleanup_results import saved_cleanup_absence
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_cleanup_delete_save_recovery import _FaultOutputs, _prepared
from .test_cleanup_query_save_recovery import _start_cleanup, _original_member_rows
from ..bootstrap.test_output_binding_changes import _NOW, environment

pytestmark = pytest.mark.asyncio


async def _terminal(environment, *, query=False, with_error=False):
    owned, context, runtime, driver, item_id = await _prepared(environment)
    driver.unknown_delete = query
    if query:
        driver.matrix_enabled, driver.present = True, False
    if with_error:
        from camctl.devices.ports import DeviceCallResult
        from camctl.operations.models import (
            CallOutcome,
            AttemptStatus,
            EffectState,
            ErrorValue,
            Settlement,
            SettlementBasis,
            EvidenceValue,
        )
        from camctl.devices.evidence import DeviceObservation

        async def failed_absence(request):
            driver.deletes.append(request)
            return DeviceCallResult.from_outcome(
                CallOutcome(
                    status=AttemptStatus.FAILED,
                    effect=EffectState.CONFIRMED,
                    error=ErrorValue(
                        "device_error", "delete", {"original": "retained"}
                    ),
                    settlement=Settlement(
                        SettlementBasis.OBSERVED,
                        EvidenceValue("delete_returned", 1, {}),
                    ),
                    observations=(
                        DeviceObservation(
                            "file_absent", 1, {"cleanup_item_id": str(item_id)}
                        ),
                    ),
                )
            )

        driver.delete = failed_absence
    runtime.outputs = _FaultOutputs("finish_cleanup_item", DbOutcomeKind.ROLLED_BACK)
    assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
    return _finish_original(owned, item_id), (owned, context, runtime, driver, item_id)


def _finish_original(owned, item_id):
    from camctl.operations.attempts import StaleRunFinish, RunOutcome
    from camctl.operations.models import ErrorValue
    from camctl.persistence.repositories.operations import OperationRepository

    output_id, action_id = owned.connection.execute(
        "SELECT output_id,action_id FROM cleanup_items WHERE id=?", (item_id,)
    ).fetchone()
    repository = OutputsRepository()
    result = repository.fail_cleanup_item(
        FailCleanupItem(
            item_id, "file_delete_failed", {"output_id": str(output_id)}, _NOW + 1
        ),
        new_operation_key(),
        owned,
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    keys = tuple(
        row[0]
        for row in owned.connection.execute(
            "SELECT responsibility_key FROM operation_runs WHERE cleanup_item_id=? AND status IN (1,2)",
            (item_id,),
        )
    )
    result = OperationRepository().finish_stale_runs(
        StaleRunFinish(
            keys,
            RunOutcome.FAILED,
            _NOW + 2,
            ErrorValue("file_delete_failed", "delete", {"output_id": str(output_id)}),
        ),
        new_operation_key(),
        owned,
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    result = repository.finish_cleanup_action(
        FinishCleanupAction(action_id, _NOW + 3), new_operation_key(), owned
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return output_id, action_id


def _request(owned, item_id):
    saved = saved_cleanup_absence(owned.connection, item_id)
    output = owned.connection.execute(
        "SELECT o.id,o.device_file_id,o.intermediate_file_id FROM cleanup_items c"
        " JOIN outputs o ON o.id=c.output_id WHERE c.id=?",
        (item_id,),
    ).fetchone()
    return HandoffCleanupAbsence(
        owned.metadata.instance_id,
        item_id,
        saved.ticket,
        saved.result_event_id,
        saved.key,
        *output,
        _NOW + 4,
    )


def _facts(owned, item_id, action_id):
    return _original_member_rows(owned, action_id, item_id)


@pytest.mark.parametrize(
    "query,with_error", [(False, False), (False, True), (True, False)]
)
async def test_terminal_absence_is_discovered_with_no_current_ports(
    environment, query, with_error
):
    (output_id, action_id), (owned, _, _, driver, item_id) = await _terminal(
        environment, query=query, with_error=with_error
    )
    from camctl.outputs.cleanup_discovery import (
        cleanup_item_batches,
        extra_cleanup_settlements,
    )

    original = _facts(owned, item_id, action_id)
    sibling = owned.connection.execute(
        "SELECT * FROM outputs WHERE id<>?", (output_id,)
    ).fetchall()
    assert extra_cleanup_settlements(owned.connection) == 1
    assert tuple(cleanup_item_batches(owned.connection, batch_size=1)) == ((item_id,),)
    runtime = session_cleanup_assembly(
        devices={},
        drivers=DriverRegistry(),
        max_delete_attempts=1,
        max_query_attempts=1,
        occurred_at=lambda: _NOW + 4,
    )(owned)
    await advance_cleanup(runtime)
    assert _facts(owned, item_id, action_id) == original
    assert owned.connection.execute(
        "SELECT availability,cleanup_status,cleanup_error_json FROM outputs WHERE id=?",
        (output_id,),
    ).fetchone() == (3, 4, None)
    assert (
        owned.connection.execute(
            "SELECT * FROM outputs WHERE id<>?", (output_id,)
        ).fetchall()
        == sibling
    )
    assert extra_cleanup_settlements(owned.connection) == 0
    assert tuple(cleanup_item_batches(owned.connection, batch_size=1)) == ()
    history = tuple(owned.connection.execute("SELECT * FROM history_events"))
    await advance_cleanup(runtime)
    assert tuple(owned.connection.execute("SELECT * FROM history_events")) == history
    assert len(driver.deletes) == 1 and len(driver.queries) == int(query)


@pytest.mark.parametrize("partial", ["file", "both", "contradiction"])
async def test_partial_file_facts_only_write_actual_changes(environment, partial):
    (output_id, _), (owned, _, _, _, item_id) = await _terminal(environment)
    request = _request(owned, item_id)
    from camctl.capture.files import FilePresenceSave
    from camctl.persistence.repositories.capture import CaptureRepository

    if partial in ("file", "both"):
        result = CaptureRepository().save_file_presence(
            FilePresenceSave(request.device_file_id, 3, _NOW + 4),
            new_operation_key(),
            owned,
        )
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
    repository = OutputsRepository()
    if partial in ("both", "contradiction"):
        result = repository.handoff_cleanup_absence(request, new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        if partial == "contradiction":
            result = CaptureRepository().save_file_presence(
                FilePresenceSave(request.device_file_id, 2, _NOW + 5),
                new_operation_key(),
                owned,
            )
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
    before = tuple(owned.connection.execute("SELECT * FROM history_events"))
    result = repository.handoff_cleanup_absence(request, new_operation_key(), owned)
    if partial == "contradiction":
        assert result.kind is DbOutcomeKind.ROLLED_BACK
    else:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
    after = tuple(owned.connection.execute("SELECT * FROM history_events"))
    assert len(after) - len(before) == int(partial == "file")
    if partial == "file":
        assert after[-1][2] == 20


@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_handoff_original_application_survives_save_failure_and_factory_rebuild(
    environment, fault
):
    (_, action_id), (owned, context, runtime, _, item_id) = await _terminal(environment)
    from .test_cleanup_query_commit_recovery import _CommitFaultConnection
    from camctl.persistence.models import DbOutcome
    from sqlite3 import OperationalError

    class Fault(OutputsRepository):
        def __init__(self):
            self.enabled = True
            self.calls = []
            self.cancel_once = False

        def handoff_cleanup_absence(self, request, key, owned):
            self.calls.append((request, key))
            if self.cancel_once:
                self.cancel_once = False
                import asyncio

                raise asyncio.CancelledError
            if self.enabled:
                if fault == "rollback":
                    return DbOutcome(
                        DbOutcomeKind.ROLLED_BACK,
                        error=OperationalError("原交接确认回滚"),
                    )
                owned = replace(
                    owned,
                    connection=_CommitFaultConnection(
                        owned.connection, after_commit=fault == "commit_after"
                    ),
                )
            return super().handoff_cleanup_absence(request, key, owned)

    port = Fault()
    runtime.outputs = port
    runtime.pending_saves.clear()
    original = _facts(owned, item_id, action_id)
    assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
    request, key = port.calls[0]
    # 保存边界的控制取消不会换掉原文件事实申请。
    owned.connection.close()
    fresh = context.open_connection()
    try:
        resumed = session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            max_delete_attempts=1,
            max_query_attempts=1,
            pending_saves=runtime.pending_saves,
            occurred_at=lambda: _NOW + 999,
        )(fresh)
        port.enabled = False
        port.cancel_once = True
        resumed.outputs = port
        import asyncio

        with pytest.raises(asyncio.CancelledError):
            await advance_cleanup(resumed)
        assert runtime.pending_saves
        await advance_cleanup(resumed)
        assert port.calls[1] == port.calls[2] == (request, key)
        assert not runtime.pending_saves
        assert fresh.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),),
        ).fetchone() == (1,)
        assert (
            fresh.connection.execute(
                "SELECT * FROM cleanup_items WHERE id=?", (item_id,)
            ).fetchone()
            == original[1]
        )
    finally:
        fresh.connection.close()


@pytest.mark.parametrize(
    "changed", ["source", "ticket", "key", "time", "output", "file"]
)
async def test_original_handoff_key_rejects_changed_source_or_identity(
    environment, changed
):
    (_, _), (owned, _, _, _, item_id) = await _terminal(environment)
    request = _request(owned, item_id)
    key = new_operation_key()
    repository = OutputsRepository()
    assert (
        repository.handoff_cleanup_absence(request, key, owned).kind
        is DbOutcomeKind.COMPLETED
    )
    before = tuple(owned.connection.iterdump())
    changed_request = {
        "source": replace(request, result_event_id=request.result_event_id + 1),
        "ticket": replace(
            request,
            ticket=replace(request.ticket, attempt_id=request.ticket.attempt_id + 1),
        ),
        "key": replace(request, source_key=new_operation_key()),
        "time": replace(request, occurred_at=request.occurred_at + 1),
        "output": replace(request, output_id=request.output_id + 1),
        "file": replace(request, device_file_id=request.device_file_id + 1),
    }[changed]
    result = repository.handoff_cleanup_absence(changed_request, key, owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before
    assert (
        repository.handoff_cleanup_absence(request, key, owned).kind
        is DbOutcomeKind.COMPLETED
    )
    assert tuple(owned.connection.iterdump()) == before


async def test_later_member_failure_keeps_independent_cleanup_completed(environment):
    (output_id, _), (owned, _, _, _, item_id) = await _terminal(environment)
    assert (
        OutputsRepository()
        .handoff_cleanup_absence(_request(owned, item_id), new_operation_key(), owned)
        .kind
        is DbOutcomeKind.COMPLETED
    )
    _, second = _start_cleanup(owned, output_id, "4")
    result = OutputsRepository().fail_cleanup_item(
        FailCleanupItem(
            second, "file_delete_failed", {"output_id": str(output_id)}, _NOW + 6
        ),
        new_operation_key(),
        owned,
    )
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert owned.connection.execute(
        "SELECT availability,cleanup_status,cleanup_error_json FROM outputs WHERE id=?",
        (output_id,),
    ).fetchone() == (3, 4, None)


async def test_real_promoted_host_source_handoff_preserves_preview_and_original_failure(
    tmp_path, monkeypatch
):
    from ..capture.test_host_photo_flow import _world
    from camctl.capture.handlers import capture_handler
    from camctl.bootstrap.cleanup_assembly import HostArtifacts

    cfg, owned, driver, capture_runtime, _ = await _world(tmp_path)
    try:
        await capture_handler("camera_take_photo")(1, capture_runtime)
        output_id = owned.connection.execute(
            "SELECT id FROM outputs WHERE kind=1"
        ).fetchone()[0]
        action_id, item_id = _start_cleanup(owned, output_id, "2")
        runtime = session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            staging=Path(cfg.paths.staging),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW,
        )(owned)
        runtime.outputs = _FaultOutputs(
            "finish_cleanup_item", DbOutcomeKind.ROLLED_BACK
        )
        assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
        _finish_original(owned, item_id)
        original = _facts(owned, item_id, action_id)
        source = _request(owned, item_id)
        assert source.device_file_id is None
        preview = owned.connection.execute(
            "SELECT * FROM outputs WHERE kind=3"
        ).fetchone()
        preview_file = owned.connection.execute(
            "SELECT * FROM intermediate_files WHERE id=(SELECT intermediate_file_id FROM outputs WHERE kind=3)"
        ).fetchone()

        def forbidden(*args, **kwargs):
            raise AssertionError("可靠已保存缺席的主机交接不能执行unlink或stat")

        monkeypatch.setattr(HostArtifacts, "delete", forbidden)
        monkeypatch.setattr(HostArtifacts, "query_file", forbidden)
        resumed = session_cleanup_assembly(
            devices={},
            drivers=DriverRegistry(),
            staging=Path(cfg.paths.staging),
            max_delete_attempts=1,
            max_query_attempts=1,
            occurred_at=lambda: _NOW + 4,
        )(owned)
        await advance_cleanup(resumed)
        assert _facts(owned, item_id, action_id) == original
        assert owned.connection.execute(
            "SELECT cleanup_state FROM intermediate_files WHERE id=?",
            (source.intermediate_file_id,),
        ).fetchone() == (4,)
        assert (
            owned.connection.execute("SELECT * FROM outputs WHERE kind=3").fetchone()
            == preview
        )
        assert (
            owned.connection.execute(
                "SELECT * FROM intermediate_files WHERE id=(SELECT intermediate_file_id FROM outputs WHERE kind=3)"
            ).fetchone()
            == preview_file
        )
        key = owned.connection.execute(
            "SELECT transaction_id FROM history_events WHERE event_type=20 ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        bodies = [
            json.loads(row[0])
            for row in owned.connection.execute(
                "SELECT body_json FROM history_events WHERE transaction_id=? ORDER BY id",
                (key,),
            )
        ]
        assert len(bodies) == 2
        assert bodies[0]["evidence"] == {"cleanup_request": {"advance_cursor": False}}
        assert bodies[1]["evidence"] == {
            "cleanup_result_source": {
                "item_id": item_id,
                "result_event_id": source.result_event_id,
            }
        }
    finally:
        owned.connection.close()


async def test_handoff_history_replays_and_preserves_frozen_report_bytes(
    environment, tmp_path
):
    (output_id, action_id), (owned, _, _, _, item_id) = await _terminal(environment)
    from camctl.persistence.repositories.history import (
        HistoryRepository,
        SqliteSnapshotStore,
    )
    from camctl.persistence.runtime import open_existing, DbConfig, DbOpenMode
    from camctl.persistence.executor import DbExecutor
    from camctl.history.snapshots import SnapshotRef, prepare_snapshot
    from camctl.history.replay import EntityImage, apply_forward, apply_reverse
    from camctl.history.validators import ValidatedEvent
    from camctl.history.decoding import decode_event_row
    from camctl.contracts.enums import load_registry
    from camctl.reporting.policy import ReportingRepository
    from camctl.reporting.generation import GenerationSpec, generate_report_file

    database = Path(environment[0].paths.state_db)
    history = HistoryRepository(database)
    request = _request(owned, item_id)
    before_boundary = history.current_boundary()
    before_output = history.restore_entity("output", output_id, before_boundary)
    before_file = history.restore_entity(
        "device_file", request.device_file_id, before_boundary
    )
    before_action = history.restore_entity("action", action_id, before_boundary)
    frozen = ReportingRepository().freeze_report(
        new_operation_key(), owned, occurred_at=_NOW + 3
    )
    assert frozen.kind is DbOutcomeKind.COMPLETED, frozen.error
    report = frozen.value.report
    spec = GenerationSpec(
        db_path=database,
        report_id=report.report_id,
        from_wm=report.from_wm,
        to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        staging_path=tmp_path / "old-report.json",
    )
    payload = generate_report_file(spec).path.read_bytes()
    type_id = load_registry()["history_objects"]["output"]["id"]
    executor = DbExecutor(
        lambda: open_existing(database, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4,
        enqueue_timeout_seconds=9,
    )
    try:
        store = SqliteSnapshotStore(database, executor)
        seed = (await store.load_entity_images((SnapshotRef(type_id, output_id),)))[0]
        prepared = prepare_snapshot(seed)
        await store.save_snapshots((prepared,))
    finally:
        await executor.close()
    outcome = OutputsRepository().handoff_cleanup_absence(
        request, new_operation_key(), owned
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    after_boundary = history.current_boundary()
    after_output = history.restore_entity("output", output_id, after_boundary)
    assert history.restore_entity("output", output_id, before_boundary) == before_output
    assert (
        history.restore_entity("device_file", request.device_file_id, before_boundary)
        == before_file
    )
    assert history.restore_entity("action", action_id, before_boundary) == before_action
    assert history.restore_entity("action", action_id, after_boundary) == before_action
    for entity, table, identity in (
        ("output", "outputs", output_id),
        ("device_file", "device_files", request.device_file_id),
    ):
        cursor = owned.connection.execute(
            f"SELECT * FROM {table} WHERE id=?", (identity,)
        )
        expected = dict(
            zip((column[0] for column in cursor.description), cursor.fetchone())
        )
        from camctl.persistence.transaction import json_columns
        from camctl.contracts.json_values import parse_exact_json

        for column in json_columns().get(table, ()):
            if expected[column] is not None:
                expected[column] = parse_exact_json(expected[column])
        assert (
            history.restore_entity(entity, identity, after_boundary)[(table, identity)]
            == expected
        )
    assert (
        generate_report_file(
            replace(spec, staging_path=tmp_path / "old-report-again.json")
        ).path.read_bytes()
        == payload
    )
    # 对实际完整事务的产物事件正向、逆向应用，不重算任何运行判断。
    raw = owned.connection.execute(
        "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE event_type=20 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    event = decode_event_row(raw)
    validated = ValidatedEvent(
        event,
        "OUTPUT_REGISTERED",
        "OBSERVATION",
        ((type_id, output_id),),
        {("outputs", output_id): (type_id, output_id)},
    )
    image = EntityImage(
        type_id,
        output_id,
        True,
        before_output,
        seed.boundary.last_event_id,
        seed.change_count,
    )
    forward = apply_forward(image, validated)
    from camctl.history.events import business_columns

    columns = business_columns("outputs")
    business = lambda rows: {
        key: {name: value for name, value in values.items() if name in columns}
        for key, values in rows.items()
    }
    assert business(forward.rows) == business(after_output)
    assert business(apply_reverse(forward, validated).rows) == business(before_output)
    assert forward.change_count == seed.change_count + 1


@pytest.mark.parametrize(
    "fault", ["source_item", "source_result", "output_only", "extra_column"]
)
async def test_source_observation_guard_rejects_unmatched_complete_fact_proposal(
    environment, fault
):
    (_, _), (owned, _, _, _, item_id) = await _terminal(environment)
    from camctl.persistence.repositories.cleanup_absence import (
        HandoffCleanupAbsenceCommand,
    )
    from camctl.persistence.transaction import commit_operation

    request = _request(owned, item_id)
    key = new_operation_key()

    class InvalidProposal:
        def plan(self, scope):
            plan = HandoffCleanupAbsenceCommand(request, key).plan(scope)
            output = plan.events[-1]
            if fault == "source_item":
                output = replace(
                    output,
                    evidence={
                        "cleanup_result_source": {
                            "item_id": item_id + 1,
                            "result_event_id": request.result_event_id,
                        }
                    },
                )
            elif fault == "source_result":
                output = replace(
                    output,
                    evidence={
                        "cleanup_result_source": {
                            "item_id": item_id,
                            "result_event_id": request.result_event_id + 1,
                        }
                    },
                )
            elif fault == "extra_column":
                row = output.rows[0]
                before = dict(row.before.values, original_name="photo-1.jpg")
                after = dict(row.after.values, original_name="other.jpg")
                output = replace(
                    output,
                    rows=(
                        replace(
                            row,
                            before=replace(row.before, values=before),
                            after=replace(row.after, values=after),
                        ),
                    ),
                )
            else:
                # 合法分配一个事件，单独的产物完成仍缺少前序文件完成。
                scope.allocation = replace(
                    scope.allocation, last_event_id=scope.allocation.first_event_id
                )
                return replace(
                    plan,
                    events=(replace(output, event_id=scope.allocation.first_event_id),),
                )
            return replace(plan, events=(*plan.events[:-1], output))

    before = tuple(owned.connection.iterdump())
    result = commit_operation(InvalidProposal(), key, owned)
    assert result.kind == "rolled_back"
    assert "缺席来源" in str(result.error)
    assert tuple(owned.connection.iterdump()) == before


async def test_pending_companion_precondition_is_checked_before_terminal_file_save(
    environment,
):
    (_, _), (owned, _, runtime, _, item_id) = await _terminal(environment)
    from camctl.outputs.cleanup_flow import PendingCleanupCompanion
    from camctl.operations.attempts import StaleRunFinish, RunOutcome
    from camctl.contracts.values import ConsistencyError

    runtime.pending_companions[item_id] = PendingCleanupCompanion(
        owned.metadata.instance_id,
        StaleRunFinish((f"delete/{item_id}",), RunOutcome.SUCCEEDED, _NOW),
        new_operation_key(),
    )
    before = tuple(owned.connection.iterdump())
    with pytest.raises(ConsistencyError, match="伴随流程收场"):
        await delete_source_file(runtime, item_id)
    assert tuple(owned.connection.iterdump()) == before


async def test_saved_unknown_then_late_absence_does_not_grant_terminal_file_handoff(
    environment,
):
    owned, _, runtime, driver, item_id = await _prepared(environment)
    from camctl.operations.models import (
        CallOutcome,
        AttemptStatus,
        EffectState,
        ErrorValue,
        Settlement,
        SettlementBasis,
        EvidenceValue,
    )
    from camctl.operations.validation import validate_outcome

    class SavedUnknown(OutputsRepository):
        def finish_cleanup_attempt(self, finish, key, owned):
            actual = CallOutcome(
                status=AttemptStatus.UNKNOWN,
                effect=EffectState.UNKNOWN,
                error=ErrorValue("device_error", "delete", {"original": "unknown"}),
                settlement=Settlement(
                    SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})
                ),
            )
            self.original_key = new_operation_key()
            original = replace(
                finish,
                outcome=validate_outcome(finish.ticket, actual, runtime.evidence),
                occurred_at=finish.occurred_at - 1,
            )
            result = super().finish_cleanup_attempt(original, self.original_key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            output_id, _ = _finish_original(owned, item_id)
            self.before = tuple(
                owned.connection.execute("SELECT * FROM history_events")
            )
            self.output_id = output_id
            return super().finish_cleanup_attempt(finish, key, owned)

    port = SavedUnknown()
    runtime.outputs = port
    result = await delete_source_file(runtime, item_id)
    assert result.phase == "already_terminal"
    assert result.diagnostic is not None
    assert saved_cleanup_absence(owned.connection, item_id) is None
    assert (
        tuple(owned.connection.execute("SELECT * FROM history_events")) == port.before
    )
    assert owned.connection.execute(
        "SELECT availability,cleanup_status FROM outputs WHERE id=?", (port.output_id,)
    ).fetchone() == (2, 5)
    assert len(driver.deletes) == 1 and not driver.queries
