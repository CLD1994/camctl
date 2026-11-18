"""原清理保存申请在数据库故障后保持身份，实际删除不重放。"""

from dataclasses import replace
from sqlite3 import OperationalError
import sqlite3

import pytest

from support.repository_ports import repository_port
from camctl.session.service import LocalWorkPort

from camctl.contracts.values import ConsistencyError
from camctl.outputs.cleanup_flow import delete_source_file
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.operations import OperationRepository

from ..bootstrap.test_output_binding_changes import _save_photos, environment, _registry
from ..support.outputs import cleanup_factory, sqlite_operation_fault
from ..support.database import database_path, business_database
from ..support.capture import capture_database
from .test_cleanup_query_save_recovery import _MatrixDriver, _new_runtime, _start_cleanup
from .test_cleanup_query_commit_recovery import _CommitFaultConnection

pytestmark = pytest.mark.asyncio


class _FaultOutputs(OutputsRepository):
    def __init__(self, method, kind, after_commit=False):
        self.method, self.kind, self.after_commit = method, kind, after_commit
        self.calls = []
        self.enabled = True

    def _save(self, method, request, key, owned):
        if method != self.method:
            return getattr(super(), method)(request, key, owned)
        self.calls.append((request, key))
        if not self.enabled:
            return getattr(super(), method)(request, key, owned)
        if self.kind is DbOutcomeKind.ROLLED_BACK:
            return DbOutcome(self.kind, error=OperationalError("保存确认回滚"))
        fault = _CommitFaultConnection(owned.connection, after_commit=self.after_commit)
        return getattr(super(), method)(request, key, replace(owned, connection=fault))

    def begin_cleanup_attempt(self, request, key, owned):
        return self._save("begin_cleanup_attempt", request, key, owned)

    def finish_cleanup_attempt(self, request, key, owned):
        return self._save("finish_cleanup_attempt", request, key, owned)

    def finish_cleanup_item(self, request, key, owned):
        return self._save("finish_cleanup_item", request, key, owned)

    def fail_cleanup_item(self, request, key, owned):
        return self._save("fail_cleanup_item", request, key, owned)

    def cancel_cleanup_item(self, request, key, owned):
        return self._save("cancel_cleanup_item", request, key, owned)

    def fail_cleanup_binding(self, request, key, owned):
        return self._save("fail_cleanup_binding", request, key, owned)


class _FaultCompanion(OperationRepository):
    def __init__(self, fault="rollback"):
        self.calls = []
        self.enabled = True
        self.fault = fault

    def finish_stale_runs(self, request, key, owned):
        self.calls.append((request, key))
        if self.enabled:
            if self.fault == "rollback":
                return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OperationalError("伴随回滚"))
            fault = _CommitFaultConnection(owned.connection, after_commit=self.fault == "commit_after")
            return super().finish_stale_runs(request, key, replace(owned, connection=fault))
        return super().finish_stale_runs(request, key, owned)


async def _configured(environment, owned, driver, *, database=None, devices=None,
                      registry=None, evidence=None, delete_attempts=3,
                      query_attempts=3, wall=None, **options):
    from pathlib import Path
    from camctl.devices.drivers.registry import DriverRegistry
    if registry is None:
        registry = _registry(driver)
        if evidence is not None:
            registry = DriverRegistry((replace(registry.entry("camctl-adb"), evidence=evidence),))
    factory = cleanup_factory(
        devices=environment[0].devices if devices is None else devices,
        drivers=registry, max_delete_attempts=delete_attempts,
        max_query_attempts=query_attempts, staging=Path(environment[0].paths.staging),
        occurred_at=wall or (lambda: 1_800_000_000_000_000),
        monotonic_ns=lambda: 0, **options)
    return await factory(database or await capture_database(owned))


async def _prepared(environment, *, query_attempts=3, missing_binding=False,
                    before_query=False, evidence=None, delete_attempts=3,
                    devices=("cam-a",)):
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver, devices=devices)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'"
    ).fetchone()[0]
    request_id = "2"
    if before_query:
        from .test_cleanup_query_cancel_before_delete import _c1_unknown_delete_terminal
        prior_driver = _MatrixDriver(owned)
        await _c1_unknown_delete_terminal(cfg, owned, prior_driver, output_id)
        assert len(prior_driver.deletes) == len(prior_driver.queries) == 1
        request_id = "3"
    action_id, item_id = _start_cleanup(owned, output_id, request_id)
    driver = _MatrixDriver(owned)
    driver.unknown_delete = False
    runtime = await _configured(environment, owned, driver,
        devices={} if missing_binding else cfg.devices, evidence=evidence,
        delete_attempts=delete_attempts, query_attempts=query_attempts)
    return owned, context, runtime, driver, item_id


async def _resumed(environment, runtime, owned, resources, driver, **options):
    database = await resources.restart_database(database_path(owned))
    resumed = await _configured(environment, owned, driver, database=database, **options)
    return resumed


@pytest.mark.parametrize("method", ["begin_cleanup_attempt", "finish_cleanup_attempt", "finish_cleanup_item"])
@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_delete_original_request_survives_save_failure(environment, method, fault, capture_resources):
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs(method, DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    with repository_port(OutputsRepository, port, (method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        try:
            await delete_source_file(runtime, item_id)
        except ConsistencyError:
            pass
        assert len(port.calls) == 1
        original = port.calls[0]
        assert len(driver.deletes) == (0 if method == "begin_cleanup_attempt" else 1)
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            port.enabled = False
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver)
            result = await delete_source_file(resumed, item_id)
            assert result.phase in ("succeeded", "already_terminal")
            assert len(driver.deletes) == 1
            assert port.calls[1] == original
            assert fresh.connection.execute(
                "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()[0] == 1
            assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 4
        finally:
            fresh.connection.close()


@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_delete_companion_failure_keeps_original_request(environment, fault, capture_resources):
    owned, context, runtime, driver, item_id = await _prepared(environment)
    companion = _FaultCompanion(fault)
    with repository_port(OperationRepository, companion, ("finish_stale_runs",),
                         matches=lambda _name, args, _kwargs: f"delete/{item_id}" in args[0].responsibility_keys):
        assert (await delete_source_file(runtime, item_id)).phase == "companion_rejected"
        original = companion.calls[0]
        companion.enabled = False
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver)
            assert (await delete_source_file(resumed, item_id)).phase in ("succeeded", "already_terminal")
            assert companion.calls[1] == original
            assert len(driver.deletes) == 1
        finally:
            fresh.connection.close()



@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_cancel_before_recovered_delete_never_dispatches(environment, fault, capture_resources):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    with repository_port(OutputsRepository, port, (port.method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        result = await delete_source_file(runtime, item_id)
        assert result.phase == "delete_budget_rejected"
        original = port.calls[0]
        action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
        # 未决连接先关闭，取消以新连接提交，再接回原会话保存集合。
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            _apply_cleanup_cancel(fresh, action_id)
            port.enabled = False
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver)
            step = await delete_source_file(resumed, item_id)
            assert step.phase == "canceled"
            assert not driver.deletes
            assert fresh.connection.execute("SELECT status,restriction_state,error_code FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (6, 3, None)
            if fault == "commit_after":
                assert port.calls[1] == original
                assert fresh.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0] > 0
        finally:
            fresh.connection.close()


async def test_delete_return_validation_failure_keeps_original_actual(environment):
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    from camctl.operations.validation import OutcomeValidationError
    owned, _, runtime, driver, item_id = await _prepared(environment, evidence=EvidenceRegistry((
        EvidenceContract("file_absent", 1, "delete", frozenset({"cleanup_item_id"}),
                         identity_field="cleanup_item_id"),
        EvidenceContract("file_presence", 1, "query", frozenset({"cleanup_item_id", "present"}),
                         identity_field="cleanup_item_id"),)))
    returned = []
    original_delete = driver.delete
    async def record_actual(request):
        result = await original_delete(request)
        returned.append(result)
        return result
    driver.delete = record_actual
    with pytest.raises(OutcomeValidationError) as first:
        await delete_source_file(runtime, item_id)
    resumed = await _configured(environment, owned, driver)
    with pytest.raises(OutcomeValidationError) as second:
        await delete_source_file(resumed, item_id)
    assert second.value is not first.value
    assert type(second.value) is type(first.value)
    assert str(second.value) == str(first.value)
    assert len(returned) == len(driver.deletes) == 1
    assert returned[0].observations[0].data == {"cleanup_item_id": str(item_id)}
    assert owned.connection.execute(
        "SELECT a.status,a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=?", (item_id,)).fetchone() == (1, None)


@pytest.mark.parametrize("method", ["finish_cleanup_attempt", "finish_cleanup_item"])
async def test_new_factory_without_current_driver_completes_original_delete(environment, method, capture_resources):
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.outputs.cleanup_flow import advance_cleanup
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs(method, DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, (method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        await delete_source_file(runtime, item_id)
        original = port.calls[0]
        factory = cleanup_factory(devices=environment[0].devices, drivers=DriverRegistry(),
            max_delete_attempts=3, max_query_attempts=3)
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            resumed = await factory(await capture_resources.restart_database(fresh))
            port.enabled = False
            await advance_cleanup(resumed)
            assert port.calls[1] == original
            assert len(driver.deletes) == 1
            assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 4
        finally:
            fresh.connection.close()



@pytest.mark.parametrize("cancel_after_observation", [False, True, "second_ticket"])
async def test_delete_post_observation_present_avoids_independent_query(environment, cancel_after_observation):
    from decimal import Decimal
    from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
    from camctl.devices.ports import DeviceCallResult
    from camctl.operations.attempts import AttemptConfig
    from ..bootstrap.test_output_binding_changes import _EVIDENCE
    from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, ErrorValue, Settlement, SettlementBasis, EvidenceValue
    owned, context, runtime, driver, item_id = await _prepared(environment)
    original_delete = driver.delete

    async def present_delete(request):
        await original_delete(request)
        return DeviceCallResult.from_outcome(CallOutcome(status=AttemptStatus.FAILED,
            error=ErrorValue("device_error", "delete", {"diagnostic": "post observation retained"}),
            effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})),
            observations=(DeviceObservation("file_delete_presence", 1,
                {"cleanup_item_id": str(item_id), "present": True}),)))

    driver.delete = present_delete
    devices = {name: {**declaration, "cleanup": {"delete_retry_interval_s": Decimal(0)}}
               for name, declaration in environment[0].devices.items()}
    runtime = await _configured(environment, owned, driver, devices=devices,
        evidence=_EVIDENCE.with_contract(EvidenceContract("file_delete_presence", 1, "delete",
            frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id")),
        delete_attempts=3 if cancel_after_observation == "second_ticket" else 1)
    assert (await delete_source_file(runtime, item_id)).phase == "still_present"
    assert not driver.queries
    if cancel_after_observation:
        from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
        action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
        if cancel_after_observation == "second_ticket":
            from .test_cleanup_query_cancel_before_delete import _EntryPort
            class OnceCancelPort(_EntryPort):
                def begin_cleanup_attempt(self, intent, key, owned):
                    if not self.entries:
                        return super().begin_cleanup_attempt(intent, key, owned)
                    return OutputsRepository.begin_cleanup_attempt(self, intent, key, owned)
            port = OnceCancelPort(owned, item_id, action_id, "cancel")
        else:
            _apply_cleanup_cancel(owned, action_id)
    if cancel_after_observation == "second_ticket":
        with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                             matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
            result = await delete_source_file(runtime, item_id)
    else:
        result = await delete_source_file(runtime, item_id)
    assert (result.phase, result.detail) == (("canceled", "file_delete_failed") if cancel_after_observation
                                            else ("failed", "delete_attempts_exhausted"))
    assert not driver.queries


async def test_query_ticket_granted_before_cancel_is_saved_not_dispatched(environment):
    from .test_cleanup_query_cancel_before_delete import _EntryPort
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=True)
    action_id, = owned.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    port = _EntryPort(owned, item_id, action_id, "cancel")
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
        assert (await delete_source_file(runtime, item_id)).phase == "canceled"
    assert not driver.queries and not driver.deletes
    import json
    raw = owned.connection.execute(
        "SELECT a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=?", (item_id,)).fetchone()[0]
    assert json.loads(raw)["settlement"]["basis"] == "not_dispatched"


@pytest.mark.parametrize("method", ["fail_cleanup_item", "cancel_cleanup_item", "fail_cleanup_binding"])
@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_all_terminal_member_saves_keep_original_application(environment, method, fault, capture_resources):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(
        environment, query_attempts=1 if method == "fail_cleanup_item" else 3,
        missing_binding=method == "fail_cleanup_binding")
    if method == "fail_cleanup_item":
        driver.unknown_delete = True
        assert (await delete_source_file(runtime, item_id)).phase == "query_unknown"
    elif method == "cancel_cleanup_item":
        action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
        _apply_cleanup_cancel(owned, action_id)
    port = _FaultOutputs(method, DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    with repository_port(OutputsRepository, port, (method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        try:
            await delete_source_file(runtime, item_id)
        except ConsistencyError:
            pass
        assert len(port.calls) == 1
        original = port.calls[0]
        original_delete_count, original_query_count = len(driver.deletes), len(driver.queries)
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            port.enabled = False
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver)
            result = await delete_source_file(resumed, item_id)
            assert result.phase in ("failed", "canceled", "already_terminal")
            assert port.calls[1] == original
            assert (len(driver.deletes), len(driver.queries)) == (original_delete_count, original_query_count)
            assert fresh.connection.execute(
                "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
                (str(original[1]),)).fetchone() == (1,)
        finally:
            fresh.connection.close()


@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_query_begin_uses_original_request_and_ticket(environment, fault, capture_resources):
    owned, context, runtime, driver, item_id = await _prepared(environment, before_query=True)
    driver.matrix_enabled, driver.present = True, False
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    with repository_port(OutputsRepository, port, (port.method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        assert (await delete_source_file(runtime, item_id)).phase == "query_rejected"
        assert not driver.queries and not driver.deletes
        original = port.calls[0]
        assert original[0].operation == "query"
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            port.enabled = False
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver)
            assert (await delete_source_file(resumed, item_id)).phase == "succeeded"
            assert len(driver.queries) == 1 and not driver.deletes
            assert port.calls[1] == original
            assert driver.query_requests[0].ticket.run_id == fresh.connection.execute(
                "SELECT id FROM operation_runs WHERE cleanup_item_id=?", (item_id,)).fetchone()[0]
        finally:
            fresh.connection.close()


@pytest.mark.parametrize("method", ["begin_cleanup_attempt", "finish_cleanup_attempt", "finish_cleanup_item"])
async def test_temporarily_unreadable_original_key_does_not_replace_responsibility(environment, method, capture_resources):
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs(method, DbOutcomeKind.UNKNOWN, after_commit=True)
    with repository_port(OutputsRepository, port, (method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        await delete_source_file(runtime, item_id)
        original = port.calls[0]
        original_count = len(driver.deletes)
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            port.enabled = False
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver)
            async with sqlite_operation_fault(
                    await capture_database(fresh), sqlite3.SQLITE_READ, "history_transactions") as fault:
                try:
                    blocked = await delete_source_file(resumed, item_id)
                    assert blocked.phase in ("delete_budget_rejected", "finish_rejected", "result_rejected")
                except (sqlite3.DatabaseError, ConsistencyError):
                    pass
            assert fault["triggered"]
            assert len(driver.deletes) == original_count
            assert (await delete_source_file(resumed, item_id)).phase in ("succeeded", "already_terminal")
            assert all(call == original for call in port.calls)
            assert len(driver.deletes) == 1
        finally:
            fresh.connection.close()


async def test_waiter_cancel_keeps_delete_owner_until_actual_return_is_saved(environment):
    import asyncio
    owned, context, runtime, driver, item_id = await _prepared(environment)
    started, interrupted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_delete = driver.delete

    async def controlled_delete(request):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            interrupted.set()
            await release.wait()
        return await original_delete(request)

    driver.delete = controlled_delete
    waiter = asyncio.create_task(delete_source_file(runtime, item_id))
    try:
        await asyncio.wait_for(started.wait(), 2)
        waiter.cancel()
        await asyncio.wait_for(interrupted.wait(), 2)
        waiter.cancel()
        assert not waiter.done()
        assert owned.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 3
        with pytest.raises(ConsistencyError):
            await delete_source_file(runtime, item_id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(waiter), 5)
    finally:
        import sys
        primary_error = sys.exception()
        release.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(waiter, return_exceptions=True)), 5)
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(f"等待删除任务收场失败: {cleanup_error!r}")
    assert len(driver.deletes) == 1
    assert owned.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 4
    from camctl.bootstrap.cleanup_assembly import cleanup_flow
    from camctl.devices.drivers.registry import DriverRegistry
    owner: LocalWorkPort = cleanup_flow(cleanup_factory(devices={}, drivers=DriverRegistry(),
        max_delete_attempts=3, max_query_attempts=3))
    assert owner.required_settlements() == 0


async def test_cleanup_begin_same_key_rejects_changed_original_input(environment):
    from decimal import Decimal
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind
    from camctl.outputs.batches import BatchOutcome, BatchRequest
    from camctl.persistence.repositories.file_batches import FileBatchRepository
    from ..bootstrap.test_output_binding_changes import _NOW
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=True)
    action_id, = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    device_id, = owned.connection.execute(
        "SELECT a.device_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id"
        " JOIN device_files f ON f.id=o.device_file_id JOIN actions a ON a.id=f.observer_action_id"
        " WHERE c.id=?", (item_id,),
    ).fetchone()
    batch = FileBatchRepository().acquire(
        BatchRequest(action_id, device_id, _NOW), new_operation_key(), owned)
    assert batch.kind is DbOutcomeKind.COMPLETED and batch.value is BatchOutcome.GRANTED, batch
    original = AttemptIntent("query", action_id, OperationKind.CHECK_FILE_EXISTS,
        AttemptTarget(cleanup_item_id=item_id), None, AttemptConfig(3, Decimal(10), Decimal(0)), _NOW)
    key = new_operation_key()
    repository = OutputsRepository()
    granted = repository.begin_cleanup_attempt(original, key, owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    assert granted.value.disposition is BeginDisposition.GRANTED and granted.value.ticket is not None, granted
    saved_count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
    same = repository.begin_cleanup_attempt(original, key, owned)
    assert same.kind is DbOutcomeKind.COMPLETED and same.value.ticket == granted.value.ticket
    changed = (replace(original, occurred_at=original.occurred_at + 1),
        replace(original, config=replace(original.config, max_attempts=4)),
        replace(original, action_id=action_id + 1))
    for request in changed:
        assert repository.begin_cleanup_attempt(request, key, owned).kind is DbOutcomeKind.ROLLED_BACK
    other_key, = owned.connection.execute(
        "SELECT operation_key FROM history_transactions WHERE operation_key<>? LIMIT 1", (str(key),)).fetchone()
    assert repository.begin_cleanup_attempt(original, other_key, owned).kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0] == saved_count
    assert not driver.deletes and not driver.queries


class _AlreadyEndedOutputs(OutputsRepository):
    def __init__(self, evidence, present):
        self.evidence, self.present = evidence, present
        self.existing_finish = self.existing_key = None
        self.late_finish = None

    def finish_cleanup_attempt(self, finish, key, owned):
        from camctl.contracts.values import new_operation_key
        from camctl.devices.evidence import DeviceObservation
        from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, Settlement, SettlementBasis, EvidenceValue
        from camctl.operations.validation import validate_outcome
        if self.existing_finish is None:
            observation = DeviceObservation("file_delete_presence", 1,
                {"cleanup_item_id": finish.ticket.target_id, "present": self.present})
            actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
                settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("delete_returned", 1, {})),
                observations=(observation,))
            self.existing_key = new_operation_key()
            self.existing_finish = replace(finish, outcome=validate_outcome(finish.ticket, actual, self.evidence),
                occurred_at=finish.occurred_at - 1, retry_wait=self.present)
            first = super().finish_cleanup_attempt(self.existing_finish, self.existing_key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED, first.error
        self.late_finish = finish
        return super().finish_cleanup_attempt(finish, key, owned)


@pytest.mark.parametrize("saved_present", [True, False])
async def test_delete_already_ended_consumes_exact_saved_result_instead_of_late_return(environment, saved_present):
    import json
    from camctl.devices.evidence import EvidenceContract
    from camctl.operations.attempts import FinishDisposition
    owned, context, runtime, driver, item_id = await _prepared(environment)
    evidence = _registry(driver).entry("camctl-adb").evidence.with_contract(EvidenceContract(
        "file_delete_presence", 1, "delete", frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"))
    runtime = await _configured(environment, owned, driver, evidence=evidence)
    driver.unknown_delete = not saved_present
    port = _AlreadyEndedOutputs(evidence, saved_present)
    with repository_port(OutputsRepository, port, ("finish_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].ticket.target_id == str(item_id)):
        result = await delete_source_file(runtime, item_id)
        assert result.phase == ("still_present" if saved_present else "succeeded")
        assert len(driver.deletes) == 1 and not driver.queries
        row = owned.connection.execute("SELECT a.result_json,e.occurred_at,t.operation_key FROM operation_attempts a JOIN history_events e ON e.id=a.result_event_id JOIN history_transactions t ON t.id=e.transaction_id JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()
        assert json.loads(row[0])["observations"][0]["data"]["present"] is saved_present
        assert row[1:] == (port.existing_finish.occurred_at, port.existing_key)
        assert port.late_finish is not port.existing_finish


class _AlreadyEndedQuery(OperationRepository):
    def __init__(self, evidence, present):
        self.evidence, self.present = evidence, present
        self.existing_key = self.existing_finish = None

    def finish_attempt(self, finish, key, owned):
        from camctl.contracts.values import new_operation_key
        from camctl.devices.evidence import DeviceObservation
        from camctl.operations.models import CallOutcome, AttemptStatus, EffectState, Settlement, SettlementBasis, EvidenceValue
        from camctl.operations.validation import validate_outcome
        if self.existing_finish is None:
            actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
                settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("file_presence", 1, {})),
                observations=(DeviceObservation("file_presence", 1,
                    {"cleanup_item_id": finish.ticket.target_id, "present": self.present}),))
            self.existing_key = new_operation_key()
            self.existing_finish = replace(finish, outcome=validate_outcome(finish.ticket, actual, self.evidence),
                occurred_at=finish.occurred_at - 1, retry_wait=self.present)
            first = super().finish_attempt(self.existing_finish, self.existing_key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED, first.error
        return super().finish_attempt(finish, key, owned)


@pytest.mark.parametrize("saved_present", [True, False])
async def test_query_already_ended_consumes_exact_saved_result_instead_of_late_return(environment, saved_present):
    import json
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=True)
    driver.matrix_enabled, driver.present = True, not saved_present
    port = _AlreadyEndedQuery(_registry(driver).entry("camctl-adb").evidence, saved_present)
    with repository_port(OperationRepository, port, ("finish_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].ticket.target_id == str(item_id)):
        result = await delete_source_file(runtime, item_id)
        assert result.phase == ("still_present" if saved_present else "succeeded")
        assert result.diagnostic is not None
        assert len(driver.queries) == 1 and not driver.deletes
        row = owned.connection.execute(
            "SELECT a.result_json,e.occurred_at,t.operation_key FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id JOIN history_events e ON e.id=a.result_event_id"
            " JOIN history_transactions t ON t.id=e.transaction_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()
        assert json.loads(row[0])["observations"][0]["data"]["present"] is saved_present
        assert row[1:] == (port.existing_finish.occurred_at, port.existing_key)


async def test_already_ended_unreadable_history_keeps_late_raw_until_handoff(environment):
    from camctl.devices.evidence import EvidenceContract
    from camctl.operations.attempts import FinishDisposition
    owned, _, runtime, driver, item_id = await _prepared(environment)
    evidence = _registry(driver).entry("camctl-adb").evidence.with_contract(EvidenceContract(
        "file_delete_presence", 1, "delete", frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"))
    runtime = await _configured(environment, owned, driver, evidence=evidence)
    class UnreadableSavedHistory(_AlreadyEndedOutputs):
        enabled = True
        triggered = False
        late_calls = []
        def finish_cleanup_attempt(self, finish, key, worker):
            result = super().finish_cleanup_attempt(finish, key, worker)
            self.late_calls.append((finish, key))
            if self.enabled and result.value.disposition is FinishDisposition.ALREADY_ENDED:
                def authorize(operation, table, column, database, trigger):
                    if operation == sqlite3.SQLITE_READ and table == "history_transactions":
                        self.triggered = True
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                worker.connection.set_authorizer(authorize)
            return result
    port = UnreadableSavedHistory(evidence, False)
    with repository_port(OutputsRepository, port, ("finish_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].ticket.target_id == str(item_id)):
        try:
            with pytest.raises(sqlite3.DatabaseError):
                await delete_source_file(runtime, item_id)
        finally:
            await (await capture_database(owned)).read(lambda worker: worker.connection.set_authorizer(None))
            port.enabled = False
        assert port.triggered and len(driver.deletes) == 1
        original = port.late_calls[0]
        result = await delete_source_file(runtime, item_id)
        assert result.phase == "succeeded" and result.diagnostic is not None
        assert all(call == original for call in port.late_calls)
        assert len(driver.deletes) == 1
        row = owned.connection.execute(
            "SELECT e.occurred_at,t.operation_key FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id JOIN history_events e ON e.id=a.result_event_id"
            " JOIN history_transactions t ON t.id=e.transaction_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()
        assert row == (port.existing_finish.occurred_at, port.existing_key)


@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_binding_failure_original_application_can_finish_after_later_cancel(environment, fault, capture_resources):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment, missing_binding=True)
    port = _FaultOutputs("fail_cleanup_binding", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    with repository_port(OutputsRepository, port, (port.method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        with pytest.raises(ConsistencyError):
            await delete_source_file(runtime, item_id)
        original = port.calls[0]
        action_id, = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            _apply_cleanup_cancel(fresh, action_id)
            port.enabled = False
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver, devices={})
            result = await delete_source_file(resumed, item_id)
            assert result.phase != "result_rejected", result
            assert original[0].canceled is False
            assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == (5 if fault == "commit_after" else 6)
            if fault == "commit_after":
                assert port.calls[1] == original
            else:
                assert len(port.calls) == 1
                assert fresh.connection.execute("SELECT 1 FROM history_transactions WHERE operation_key=?", (str(original[1]),)).fetchone() is None
            assert not driver.deletes and not driver.queries
        finally:
            fresh.connection.close()



@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_recovered_intent_dispatch_uses_its_original_adopted_timeout(environment, operation):
    from decimal import Decimal
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=operation == "query")
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, (port.method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        assert (await delete_source_file(runtime, item_id)).phase == ("delete_budget_rejected" if operation == "delete" else "query_rejected")
        original_config = port.calls[0][0].config
        if operation == "query":
            driver.matrix_enabled, driver.present = True, False
        devices = {name: {**declaration, "cleanup": {"delete_timeout_s": Decimal(1), "query_timeout_s": Decimal(2)}}
                   for name, declaration in environment[0].devices.items()}
        resumed = await _configured(environment, owned, driver, devices=devices)
        port.enabled = False
        assert (await delete_source_file(resumed, item_id)).phase == "succeeded"
        actual_request = (driver.delete_requests if operation == "delete" else driver.query_requests)[0]
        assert actual_request.timeout_s == original_config.timeout_s
        assert port.calls[1] == port.calls[0]


@pytest.mark.parametrize("fault", ["rollback", "commit_after"])
@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_unissued_original_call_is_not_dispatched_after_binding_disappears(environment, fault, operation, capture_resources):
    owned, context, runtime, driver, item_id = await _prepared(environment, before_query=operation == "query")
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    with repository_port(OutputsRepository, port, (port.method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        assert (await delete_source_file(runtime, item_id)).phase == ("delete_budget_rejected" if operation == "delete" else "query_rejected")
        original = port.calls[0]
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            port.enabled = False
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver, devices={})
            assert (await delete_source_file(resumed, item_id)).phase == "failed"
            assert not driver.deletes and not driver.queries
            attempts, = fresh.connection.execute(
                "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()
            assert attempts == int(fault == "commit_after")
            assert all(call == original for call in port.calls)
        finally:
            fresh.connection.close()


async def test_unreadable_original_binding_key_does_not_create_cancel_application(environment):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, _, runtime, driver, item_id = await _prepared(environment, missing_binding=True)
    port = _FaultOutputs("fail_cleanup_binding", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, ("fail_cleanup_binding",),
                         matches=lambda _name, args, _kwargs: args[0].item_id == item_id):
        with pytest.raises(ConsistencyError):
            await delete_source_file(runtime, item_id)
        original = port.calls[0]
        action_id, = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        _apply_cleanup_cancel(owned, action_id)
        port.enabled = False
        async with sqlite_operation_fault(await capture_database(owned), sqlite3.SQLITE_READ, "history_transactions") as fault:
            with pytest.raises(sqlite3.DatabaseError):
                await delete_source_file(runtime, item_id)
        assert fault["triggered"] and port.calls == [original]
        assert owned.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 2
        assert (await delete_source_file(runtime, item_id)).phase == "canceled"
        assert not driver.deletes and not driver.queries


async def test_plain_save_exception_does_not_dispose_binding_application_as_business_rejection(environment):
    from camctl.persistence.models import db_outcome_for
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    class PlainErrorPort(_FaultOutputs):
        error = OSError("普通保存接口错误，没有类型化回执")
        def fail_cleanup_binding(self, request, key, owned):
            if self.enabled:
                self.calls.append((request, key))
                raise self.error
            return super().fail_cleanup_binding(request, key, owned)
    owned, _, runtime, driver, item_id = await _prepared(environment, missing_binding=True)
    port = PlainErrorPort("fail_cleanup_binding", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, ("fail_cleanup_binding",),
                         matches=lambda _name, args, _kwargs: args[0].item_id == item_id):
        with pytest.raises(ConsistencyError) as caught:
            await delete_source_file(runtime, item_id)
        assert db_outcome_for(caught.value).kind is DbOutcomeKind.UNKNOWN
        assert db_outcome_for(caught.value).error is port.error
        original = port.calls[0]
        action_id, = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        _apply_cleanup_cancel(owned, action_id)
        port.enabled = False
        # 真实执行器分类 UNKNOWN；须可靠核实原键，不能仅凭普通异常转取消。
        async with sqlite_operation_fault(await capture_database(owned), sqlite3.SQLITE_READ, "history_transactions") as fault:
            with pytest.raises(sqlite3.DatabaseError):
                await delete_source_file(runtime, item_id)
        assert fault["triggered"] and port.calls == [original]
        assert owned.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (2,)
        assert (await delete_source_file(runtime, item_id)).phase == "canceled"
        assert owned.connection.execute("SELECT id FROM history_transactions WHERE operation_key=?", (str(original[1]),)).fetchone() is None
        assert not driver.deletes and not driver.queries


async def test_cancel_after_binding_prevents_ticket_dispatch_releases_own_restriction(environment):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, _, _, driver, item_id = await _prepared(environment)
    devices = dict(environment[0].devices)
    runtime = await _configured(environment, owned, driver, devices=devices)
    class BindingChangesAfterGrant(_FaultOutputs):
        def begin_cleanup_attempt(self, request, key, worker):
            result = super().begin_cleanup_attempt(request, key, worker)
            devices.clear()
            return result
    port = BindingChangesAfterGrant("finish_cleanup_attempt", DbOutcomeKind.ROLLED_BACK)
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt", "finish_cleanup_attempt"),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id) in (item_id, str(item_id))):
        assert (await delete_source_file(runtime, item_id)).phase == "finish_rejected"
        assert not driver.deletes
        action_id, = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        _apply_cleanup_cancel(owned, action_id)
        port.enabled = False
        assert (await delete_source_file(runtime, item_id)).phase == "canceled"
        assert owned.connection.execute("SELECT status,restriction_state,error_code FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (6, 3, None)
        assert not driver.deletes and not driver.queries


@pytest.mark.parametrize("present", [True, None])
async def test_new_factory_completes_original_query_before_parsing_removed_driver(environment, present, capture_resources):
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.outputs.cleanup_flow import advance_cleanup
    from .test_cleanup_query_save_recovery import _QueryFinishPort
    owned, context, runtime, driver, item_id = await _prepared(environment)
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, present
    port = _QueryFinishPort(DbOutcomeKind.ROLLED_BACK, item_id, driver)
    with repository_port(OperationRepository, port, ("finish_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].ticket.target_id == str(item_id) and args[0].ticket.operation == "query"):
        with pytest.raises(ConsistencyError):
            await delete_source_file(runtime, item_id)
        original = port.finishes[0]
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            port.kind = DbOutcomeKind.COMPLETED
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver, registry=DriverRegistry())
            await advance_cleanup(resumed)
            assert port.finishes == [original, original]
            assert len(driver.deletes) == len(driver.queries) == 1
            assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 3
            from camctl.devices.bindings import DeviceConfigurationError
            with pytest.raises(DeviceConfigurationError):
                await advance_cleanup(resumed)
            assert len(driver.deletes) == len(driver.queries) == 1
        finally:
            fresh.connection.close()


async def test_unknown_intent_cannot_be_disposed_while_original_transaction_can_still_commit(environment):
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    owned, _, _, driver, item_id = await _prepared(environment)
    path = database_path(owned)
    retained = {}
    class CloseFault:
        def __init__(self, connection):
            self.connection = connection
            self.enabled = True
        def close(self):
            if self.enabled:
                raise OSError("未知提交后原连接关闭未确认")
            self.connection.close()
        def __getattr__(self, name):
            return getattr(self.connection, name)
    def open_worker_connection():
        original = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        if not retained:
            fault = CloseFault(original.connection)
            retained["connection"] = fault
            return replace(original, connection=fault)
        return original
    database = await BusinessDatabase.open(open_worker_connection)
    try:
        runtime = await _configured(environment, owned, driver, database=database)
        port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.UNKNOWN, after_commit=False)
        with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                             matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
            assert (await delete_source_file(runtime, item_id)).phase == "delete_budget_rejected"
            original = port.calls[0]
            assert await database.read(lambda worker: retained["connection"].in_transaction)
            async with business_database(path, config=DbConfig(busy_timeout_ms=1)) as other:
                resumed = await _configured(environment, owned, driver, database=other, devices={})
                with pytest.raises((OperationalError, ConsistencyError)):
                    await delete_source_file(resumed, item_id)
                assert port.calls == [original] and not driver.deletes
                # 在原 worker 线程实际提交仍存活的旧事务，不能用别处缺键推断未生效。
                def late_commit(worker):
                    retained["connection"].execute("COMMIT")
                await database.read(late_commit)
                port.enabled = False
                assert (await delete_source_file(resumed, item_id)).phase == "failed"
                assert port.calls == [original, original]
                assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()[0] == 1
                assert not driver.deletes and not driver.queries
    finally:
        def close_original(worker):
            retained["connection"].enabled = False
            retained["connection"].close()
        await database.read(close_original)
        await database.close()


@pytest.mark.parametrize("fault", ["rollback", "commit_after"])
async def test_recovered_ticket_does_not_dispatch_after_delete_capability_closes(environment, fault, capture_resources):
    from camctl.devices.drivers.registry import DriverRegistry
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    with repository_port(OutputsRepository, port, (port.method,),
                         matches=lambda _name, args, _kwargs: (args[0].target.cleanup_item_id if _name == "begin_cleanup_attempt" else args[0].ticket.target_id if _name == "finish_cleanup_attempt" else args[0].item_id) in (item_id, str(item_id))):
        assert (await delete_source_file(runtime, item_id)).phase == "delete_budget_rejected"
        original = port.calls[0]
        await (await capture_database(owned)).close()
        owned.connection.close()
        fresh = context.open_connection()
        try:
            port.enabled = False
            old = _registry(driver).entry("camctl-adb")
            registry = DriverRegistry((replace(old, declaration=replace(old.declaration, delete_supported=False)),))
            resumed = await _resumed(environment, runtime, fresh, capture_resources, driver, registry=registry)
            assert (await delete_source_file(resumed, item_id)).phase == "failed"
            assert not driver.deletes and not driver.queries
            assert fresh.connection.execute("SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()[0] == int(fault == "commit_after")
            assert all(call == original for call in port.calls)
        finally:
            fresh.connection.close()


@pytest.mark.parametrize("broken", ["type", "version", "data", "status", "effect"])
async def test_delete_facts_rejects_incomplete_saved_not_dispatched(environment, broken):
    import json
    from .test_cleanup_query_cancel_before_delete import _EntryPort
    owned, _, runtime, driver, item_id = await _prepared(environment)
    action_id, output_id = owned.connection.execute(
        "SELECT action_id,output_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    port = _EntryPort(owned, item_id, action_id, "cancel")
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].target.cleanup_item_id == item_id):
        assert (await delete_source_file(runtime, item_id)).phase == "canceled"
    assert not driver.deletes
    attempt_id, raw = owned.connection.execute(
        "SELECT id,result_json FROM operation_attempts ORDER BY id DESC LIMIT 1").fetchone()
    if broken in ("status", "effect"):
        column = "status" if broken == "status" else "effect_state"
        owned.connection.execute(f"UPDATE operation_attempts SET {column}=? WHERE id=?",
                                 (4 if broken == "status" else 1, attempt_id))
    else:
        document = json.loads(raw)
        evidence = document["settlement"]["evidence"]
        evidence[broken] = {"type": "other", "version": 2, "data": {"unexpected": True}}[broken]
        owned.connection.execute("UPDATE operation_attempts SET result_json=? WHERE id=?",
                                 (json.dumps(document), attempt_id))
    owned.connection.commit()
    _, next_item = _start_cleanup(owned, output_id, "5")
    with pytest.raises(ConsistencyError):
        await delete_source_file(runtime, next_item)
    assert not driver.deletes and not driver.queries


@pytest.mark.parametrize("member_status", [4, 5, 6])
async def test_existing_companion_resumes_actual_terminal_member_without_new_call(environment, member_status):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, _, runtime, driver, item_id = await _prepared(environment, delete_attempts=1 if member_status == 5 else 3)
    companion = _FaultCompanion()
    with repository_port(OperationRepository, companion, ("finish_stale_runs",),
                         matches=lambda _name, args, _kwargs: f"delete/{item_id}" in args[0].responsibility_keys):
        if member_status == 5:
            driver.unknown_delete = True
            driver.matrix_enabled, driver.present = True, True
            assert (await delete_source_file(runtime, item_id)).phase == "still_present"
        elif member_status == 6:
            action_id, = owned.connection.execute(
                "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
            _apply_cleanup_cancel(owned, action_id)
        assert (await delete_source_file(runtime, item_id)).phase == "companion_rejected"
        original = companion.calls[0]
        member_before = owned.connection.execute("SELECT * FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        assert owned.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == member_status
        attempts_before = tuple(owned.connection.execute(
            "SELECT a.* FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,)))
        calls_before = len(driver.deletes), len(driver.queries)
        companion.enabled = False
        assert (await delete_source_file(runtime, item_id)).phase == "already_terminal"
        assert companion.calls == [original, original]
        assert owned.connection.execute("SELECT * FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == member_before
        assert tuple(owned.connection.execute(
            "SELECT a.* FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,))) == attempts_before
        assert (len(driver.deletes), len(driver.queries)) == calls_before


@pytest.mark.parametrize("member_status", [None, 1, 2, 3])
async def test_pending_companion_nonterminal_preserves_database_and_calls(environment, monkeypatch, member_status):
    owned, _, runtime, driver, item_id = await _prepared(environment)
    companion = _FaultCompanion()
    with repository_port(OperationRepository, companion, ("finish_stale_runs",),
                         matches=lambda _name, args, _kwargs: f"delete/{item_id}" in args[0].responsibility_keys):
        assert (await delete_source_file(runtime, item_id)).phase == "companion_rejected"
        original = companion.calls[0]
        calls_before = len(driver.deletes), len(driver.queries)
        # 先取得真实成员终态和伴随保存责任，再损坏其持久投影验证一致性检查。
        if member_status is None:
            owned.connection.execute("PRAGMA foreign_keys=OFF")
            owned.connection.execute("DELETE FROM cleanup_items WHERE id=?", (item_id,))
        else:
            owned.connection.execute(
                "UPDATE cleanup_items SET status=?,restriction_state=?,outcome=NULL,final_event_id=NULL WHERE id=?",
                (member_status, min(member_status, 2), item_id))
        owned.connection.commit()
        before = tuple(owned.connection.iterdump())
        companion.enabled = False
        with pytest.raises(ConsistencyError, match="待存伴随流程没有可靠成员终态"):
            await delete_source_file(runtime, item_id)
        assert tuple(owned.connection.iterdump()) == before
        assert companion.calls == [original]
        assert (len(driver.deletes), len(driver.queries)) == calls_before
