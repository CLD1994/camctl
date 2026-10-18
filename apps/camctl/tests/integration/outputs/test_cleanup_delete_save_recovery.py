"""原清理保存申请在数据库故障后保持身份，实际删除不重放。"""

from dataclasses import replace
from sqlite3 import OperationalError

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.outputs.cleanup_flow import delete_source_file
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.operations import OperationRepository

from ..bootstrap.test_output_binding_changes import _save_photos, environment
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


async def _prepared(environment):
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute("SELECT id FROM outputs ORDER BY id LIMIT 1").fetchone()[0]
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    driver = _MatrixDriver(owned)
    driver.unknown_delete = False
    runtime = _new_runtime(cfg, owned, driver).for_item(item_id)
    runtime.for_item = None
    return owned, context, runtime, driver, item_id


@pytest.mark.parametrize("method", ["begin_cleanup_attempt", "finish_cleanup_attempt", "finish_cleanup_item"])
@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_delete_original_request_survives_save_failure(environment, method, fault):
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs(method, DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    runtime.outputs = port
    try:
        await delete_source_file(runtime, item_id)
    except ConsistencyError:
        pass
    assert len(port.calls) == 1
    original = port.calls[0]
    assert len(driver.deletes) == (0 if method == "begin_cleanup_attempt" else 1)
    binding = runtime.binding_of(item_id)
    owned.connection.close()
    fresh = context.open_connection()
    try:
        port.enabled = False
        resumed = replace(runtime, owned=fresh, binding_of=lambda _: binding, occurred_at=lambda: 1_800_000_000_000_000, for_item=None)
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
async def test_delete_companion_failure_keeps_original_request(environment, fault):
    owned, context, runtime, driver, item_id = await _prepared(environment)
    companion = _FaultCompanion(fault)
    runtime.operations = companion
    assert (await delete_source_file(runtime, item_id)).phase == "companion_rejected"
    original = companion.calls[0]
    companion.enabled = False
    owned.connection.close()
    fresh = context.open_connection()
    try:
        resumed = replace(runtime, owned=fresh, occurred_at=lambda: 1_800_000_000_000_000)
        assert (await delete_source_file(resumed, item_id)).phase in ("succeeded", "already_terminal")
        assert companion.calls[1] == original
        assert len(driver.deletes) == 1
    finally:
        fresh.connection.close()



@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_cancel_before_recovered_delete_never_dispatches(environment, fault):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    runtime.outputs = port
    result = await delete_source_file(runtime, item_id)
    assert result.phase == "delete_budget_rejected"
    original = port.calls[0]
    action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
    # 未决连接先关闭，取消以新连接提交，再接回原会话保存集合。
    binding = runtime.binding_of(item_id)
    owned.connection.close()
    fresh = context.open_connection()
    try:
        _apply_cleanup_cancel(fresh, action_id)
        port.enabled = False
        resumed = replace(runtime, owned=fresh, binding_of=lambda _: binding, for_item=None)
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
    from camctl.devices.evidence import EvidenceRegistry
    from camctl.operations.validation import OutcomeValidationError
    owned, context, runtime, driver, item_id = await _prepared(environment)
    runtime.evidence = EvidenceRegistry(())
    with pytest.raises(OutcomeValidationError) as first:
        await delete_source_file(runtime, item_id)
    assert len(driver.deletes) == 1
    original = runtime.pending_deletes[item_id].actual
    changed = _new_runtime(environment[0], owned, driver).for_item(item_id)
    resumed = replace(runtime, evidence=changed.evidence)
    with pytest.raises(OutcomeValidationError) as second:
        await delete_source_file(resumed, item_id)
    assert second.value is not first.value
    assert type(second.value) is type(first.value)
    assert str(second.value) == str(first.value)
    pending = runtime.pending_deletes[item_id]
    assert vars(pending.validation_diagnostic) == {
        "error_type": "OutcomeValidationError", "message": str(first.value)}
    assert not any(isinstance(value, BaseException) for value in vars(pending).values())
    assert runtime.pending_deletes[item_id].actual is original
    assert len(driver.deletes) == 1


@pytest.mark.parametrize("method", ["finish_cleanup_attempt", "finish_cleanup_item"])
async def test_new_factory_without_current_driver_completes_original_delete(environment, method):
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.outputs.cleanup_flow import advance_cleanup
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs(method, DbOutcomeKind.ROLLED_BACK)
    runtime.outputs = port
    await delete_source_file(runtime, item_id)
    original = port.calls[0]
    factory = session_cleanup_assembly(devices=environment[0].devices, drivers=DriverRegistry(),
        max_delete_attempts=3, max_query_attempts=3,
        pending_intents=runtime.pending_intents, pending_deletes=runtime.pending_deletes,
        pending_saves=runtime.pending_saves, pending_queries=runtime.pending_queries,
        pending_companions=runtime.pending_companions)
    owned.connection.close()
    fresh = context.open_connection()
    try:
        resumed = factory(fresh)
        resumed.outputs = port
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
    runtime.evidence = _EVIDENCE.with_contract(
        EvidenceContract("file_delete_presence", 1, "delete", frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"))
    runtime.delete_config = AttemptConfig(3 if cancel_after_observation == "second_ticket" else 1, Decimal(10), Decimal(0))
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
            runtime.outputs = OnceCancelPort(owned, item_id, action_id, "cancel")
        else:
            _apply_cleanup_cancel(owned, action_id)
    result = await delete_source_file(runtime, item_id)
    assert (result.phase, result.detail) == (("canceled", "file_delete_failed") if cancel_after_observation
                                            else ("failed", "delete_attempts_exhausted"))
    assert not driver.queries


async def test_query_ticket_granted_before_cancel_is_saved_not_dispatched(environment):
    from .test_cleanup_query_cancel_before_delete import _EntryPort
    from camctl.outputs.cleanup_flow import _verify_before_delete
    owned, context, runtime, driver, item_id = await _prepared(environment)
    action_id, output_id = owned.connection.execute(
        "SELECT action_id,output_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    runtime.outputs = _EntryPort(owned, item_id, action_id, "cancel")
    result = await _verify_before_delete(runtime, item_id, action_id, output_id)
    assert result.phase == "canceled"
    assert not driver.queries and not driver.deletes
    import json
    raw = owned.connection.execute(
        "SELECT a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()[0]
    assert json.loads(raw)["settlement"]["basis"] == "not_dispatched"


@pytest.mark.parametrize("method", ["fail_cleanup_item", "cancel_cleanup_item", "fail_cleanup_binding"])
@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_all_terminal_member_saves_keep_original_application(environment, method, fault):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment)
    if method == "fail_cleanup_item":
        driver.unknown_delete = True
        runtime.query_config = replace(runtime.query_config, max_attempts=1)
        assert (await delete_source_file(runtime, item_id)).phase == "query_unknown"
    elif method == "cancel_cleanup_item":
        action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
        _apply_cleanup_cancel(owned, action_id)
    else:
        runtime.binding_check = lambda saved: check_binding(saved, SimpleNamespace(devices={}))
    port = _FaultOutputs(method, DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    runtime.outputs = port
    try:
        await delete_source_file(runtime, item_id)
    except ConsistencyError:
        pass
    assert len(port.calls) == 1
    original = port.calls[0]
    original_delete_count, original_query_count = len(driver.deletes), len(driver.queries)
    owned.connection.close()
    fresh = context.open_connection()
    try:
        port.enabled = False
        resumed = replace(runtime, owned=fresh, occurred_at=lambda: 1_800_000_000_000_000, for_item=None)
        result = await delete_source_file(resumed, item_id)
        assert result.phase in ("failed", "canceled", "already_terminal")
        assert port.calls[1] == original
        assert (len(driver.deletes), len(driver.queries)) == (original_delete_count, original_query_count)
        assert not runtime.pending_saves
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_query_begin_uses_original_request_and_ticket(environment, fault):
    from camctl.outputs.cleanup_flow import _verify_before_delete
    owned, context, runtime, driver, item_id = await _prepared(environment)
    driver.matrix_enabled, driver.present = True, False
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    runtime.outputs = port
    action_id, output_id = owned.connection.execute("SELECT action_id,output_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    assert (await _verify_before_delete(runtime, item_id, action_id, output_id)).phase == "query_rejected"
    assert not driver.queries and not driver.deletes
    original = port.calls[0]
    binding = runtime.binding_of(item_id)
    owned.connection.close()
    fresh = context.open_connection()
    try:
        port.enabled = False
        resumed = replace(runtime, owned=fresh, binding_of=lambda _: binding, occurred_at=lambda: 1_800_000_000_000_000, for_item=None)
        assert (await delete_source_file(resumed, item_id)).phase == "succeeded"
        assert len(driver.queries) == 1 and not driver.deletes
        assert port.calls[1] == original
    finally:
        fresh.connection.close()


class _UnreadableKeyConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        if "FROM history_transactions WHERE operation_key" in sql:
            raise OperationalError("原键暂时不可核实")
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.mark.parametrize("method", ["begin_cleanup_attempt", "finish_cleanup_attempt", "finish_cleanup_item"])
async def test_temporarily_unreadable_original_key_does_not_replace_responsibility(environment, method):
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs(method, DbOutcomeKind.UNKNOWN, after_commit=True)
    runtime.outputs = port
    await delete_source_file(runtime, item_id)
    original = port.calls[0]
    original_count = len(driver.deletes)
    binding = runtime.binding_of(item_id)
    owned.connection.close()
    fresh = context.open_connection()
    try:
        port.enabled = False
        broken = replace(runtime, owned=replace(fresh, connection=_UnreadableKeyConnection(fresh.connection)), binding_of=lambda _: binding, for_item=None)
        try:
            blocked = await delete_source_file(broken, item_id)
            assert blocked.phase in ("delete_budget_rejected", "finish_rejected", "result_rejected")
        except (OperationalError, ConsistencyError):
            pass
        assert len(driver.deletes) == original_count
        resumed = replace(runtime, owned=fresh, binding_of=lambda _: binding, for_item=None)
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
        await waiter
    assert len(driver.deletes) == 1
    assert owned.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 4
    assert not runtime.pending_deletes and not runtime.pending_intents


async def test_cleanup_begin_same_key_rejects_changed_original_input(environment):
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.cleanup_flow import _begin_query_attempt
    owned, context, runtime, driver, item_id = await _prepared(environment)
    action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
    granted = _begin_query_attempt(runtime, item_id, action_id)
    assert granted.kind is DbOutcomeKind.COMPLETED
    original = runtime.pending_intents[item_id]
    saved_count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
    same = OutputsRepository().begin_cleanup_attempt(original.request, original.key, owned)
    assert same.kind is DbOutcomeKind.COMPLETED and same.value.ticket == granted.value.ticket
    changed = (
        replace(original.request, occurred_at=original.request.occurred_at + 1),
        replace(original.request, config=replace(original.request.config, max_attempts=4)),
        replace(original.request, action_id=action_id + 1),
    )
    for request in changed:
        rejected = OutputsRepository().begin_cleanup_attempt(request, original.key, owned)
        assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    other_key = owned.connection.execute("SELECT operation_key FROM history_transactions WHERE operation_key<>? LIMIT 1", (original.key,)).fetchone()[0]
    rejected = OutputsRepository().begin_cleanup_attempt(original.request, other_key, owned)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0] == saved_count


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
    runtime.evidence = runtime.evidence.with_contract(EvidenceContract(
        "file_delete_presence", 1, "delete", frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"))
    driver.unknown_delete = not saved_present
    port = _AlreadyEndedOutputs(runtime.evidence, saved_present)
    runtime.outputs = port
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
    from camctl.outputs.cleanup_flow import _verify_before_delete
    owned, context, runtime, driver, item_id = await _prepared(environment)
    driver.matrix_enabled, driver.present = True, not saved_present
    runtime.operations = _AlreadyEndedQuery(runtime.evidence, saved_present)
    action_id, output_id = owned.connection.execute("SELECT action_id,output_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    result = await _verify_before_delete(runtime, item_id, action_id, output_id)
    assert result.phase == ("still_present" if saved_present else "succeeded")
    assert result.diagnostic is not None
    assert len(driver.queries) == 1 and not driver.deletes


async def test_already_ended_unreadable_history_keeps_late_raw_until_handoff(environment):
    from camctl.devices.evidence import EvidenceContract
    class UnreadableResultHistory(_UnreadableKeyConnection):
        def execute(self, sql, parameters=()):
            if "SELECT t.operation_key FROM history_events e" in sql:
                raise OperationalError("原结果完整事务暂时不可核实")
            return self._connection.execute(sql, parameters)
    owned, context, runtime, driver, item_id = await _prepared(environment)
    runtime.evidence = runtime.evidence.with_contract(EvidenceContract(
        "file_delete_presence", 1, "delete", frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"))
    runtime.outputs = _AlreadyEndedOutputs(runtime.evidence, False)
    runtime.owned = replace(owned, connection=UnreadableResultHistory(owned.connection))
    with pytest.raises(OperationalError):
        await delete_source_file(runtime, item_id)
    pending = runtime.pending_deletes[item_id]
    original_raw = pending.actual
    original_finish, original_key = pending.finish, pending.key
    assert len(driver.deletes) == 1
    assert pending.saved_actual is None
    runtime.owned = owned
    result = await delete_source_file(runtime, item_id)
    assert result.phase == "succeeded" and result.diagnostic is not None
    assert pending.actual is original_raw and pending.finish is original_finish and pending.key == original_key
    assert len(driver.deletes) == 1 and not runtime.pending_deletes


@pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
async def test_binding_failure_original_application_can_finish_after_later_cancel(environment, fault):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment)
    runtime.binding_check = lambda saved: check_binding(saved, SimpleNamespace(devices={}))
    port = _FaultOutputs("fail_cleanup_binding", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    runtime.outputs = port
    with pytest.raises(ConsistencyError):
        await delete_source_file(runtime, item_id)
    original = port.calls[0]
    action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
    owned.connection.close()
    fresh = context.open_connection()
    try:
        _apply_cleanup_cancel(fresh, action_id)
        port.enabled = False
        runtime.owned = fresh
        result = await delete_source_file(runtime, item_id)
        assert result.phase != "result_rejected", result
        assert original[0].canceled is False
        assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == (5 if fault == "commit_after" else 6)
        if fault == "commit_after":
            assert port.calls[1] == original
        else:
            assert len(port.calls) == 1
            assert fresh.connection.execute("SELECT 1 FROM history_transactions WHERE operation_key=?", (original[1],)).fetchone() is None
        assert not runtime.pending_saves
        assert not driver.deletes and not driver.queries
    finally:
        fresh.connection.close()



@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_recovered_intent_dispatch_uses_its_original_adopted_timeout(environment, operation):
    from decimal import Decimal
    from camctl.outputs.cleanup_flow import _verify_before_delete
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK)
    runtime.outputs = port
    if operation == "delete":
        assert (await delete_source_file(runtime, item_id)).phase == "delete_budget_rejected"
        original_config = runtime.delete_config
    else:
        action_id, output_id = owned.connection.execute("SELECT action_id,output_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        assert (await _verify_before_delete(runtime, item_id, action_id, output_id)).phase == "query_rejected"
        original_config = runtime.query_config
        driver.matrix_enabled, driver.present = True, False
    port.enabled = False
    resumed = replace(runtime, delete_config=replace(runtime.delete_config, timeout_s=Decimal(1)),
                      query_config=replace(runtime.query_config, timeout_s=Decimal(2)))
    assert (await delete_source_file(resumed, item_id)).phase == "succeeded"
    actual_request = (driver.delete_requests if operation == "delete" else driver.query_requests)[0]
    assert actual_request.timeout_s == original_config.timeout_s


@pytest.mark.parametrize("fault", ["rollback", "commit_after"])
@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_unissued_original_call_is_not_dispatched_after_binding_disappears(environment, fault, operation):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    runtime.outputs = port
    if operation == "delete":
        assert (await delete_source_file(runtime, item_id)).phase == "delete_budget_rejected"
    else:
        from camctl.outputs.cleanup_flow import _verify_before_delete
        action_id, output_id = owned.connection.execute("SELECT action_id,output_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        assert (await _verify_before_delete(runtime, item_id, action_id, output_id)).phase == "query_rejected"
    binding = runtime.binding_of(item_id)
    owned.connection.close()
    fresh = context.open_connection()
    try:
        port.enabled = False
        resumed = replace(runtime, owned=fresh, binding_of=lambda _: binding,
            binding_check=lambda saved: check_binding(saved, SimpleNamespace(devices={})), for_item=None)
        result = await delete_source_file(resumed, item_id)
        assert not driver.deletes and not driver.queries, result
        assert result.phase == "failed"
        attempts = fresh.connection.execute("SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()[0]
        assert attempts == int(fault == "commit_after")
    finally:
        fresh.connection.close()


async def test_unreadable_original_binding_key_does_not_create_cancel_application(environment):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment)
    runtime.binding_check = lambda saved: check_binding(saved, SimpleNamespace(devices={}))
    port = _FaultOutputs("fail_cleanup_binding", DbOutcomeKind.ROLLED_BACK)
    runtime.outputs = port
    with pytest.raises(ConsistencyError):
        await delete_source_file(runtime, item_id)
    original = runtime.pending_saves[("fail_cleanup_binding", item_id)]
    action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
    _apply_cleanup_cancel(owned, action_id)
    port.enabled = False
    runtime.owned = replace(owned, connection=_UnreadableKeyConnection(owned.connection))
    with pytest.raises(OperationalError):
        await delete_source_file(runtime, item_id)
    assert runtime.pending_saves[("fail_cleanup_binding", item_id)] is original
    assert len(port.calls) == 1
    assert owned.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 2
    runtime.owned = owned
    assert (await delete_source_file(runtime, item_id)).phase == "canceled"
    assert not driver.deletes and not driver.queries


async def test_plain_save_exception_does_not_dispose_binding_application_as_business_rejection(environment):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    class PlainErrorPort(_FaultOutputs):
        def fail_cleanup_binding(self, request, key, owned):
            if self.enabled:
                self.calls.append((request, key))
                raise OSError("普通保存接口错误，没有类型化回执")
            return super().fail_cleanup_binding(request, key, owned)
    owned, context, runtime, driver, item_id = await _prepared(environment)
    runtime.binding_check = lambda saved: check_binding(saved, SimpleNamespace(devices={}))
    port = PlainErrorPort("fail_cleanup_binding", DbOutcomeKind.ROLLED_BACK)
    runtime.outputs = port
    with pytest.raises(OSError):
        await delete_source_file(runtime, item_id)
    original = runtime.pending_saves[("fail_cleanup_binding", item_id)]
    action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
    _apply_cleanup_cancel(owned, action_id)
    port.enabled = False
    # 第一次重入必须取得原请求的类型化回滚，不从普通异常推断未生效。
    assert (await delete_source_file(runtime, item_id)).phase == "result_rejected"
    assert runtime.pending_saves[("fail_cleanup_binding", item_id)] is original
    assert len(port.calls) == 2 and port.calls[1] == port.calls[0]
    # 随后沿原 key 可靠核实未生效，才结束旧分支并形成取消申请。
    assert (await delete_source_file(runtime, item_id)).phase == "canceled"
    assert not runtime.pending_saves
    assert not driver.deletes and not driver.queries


async def test_cancel_after_binding_prevents_ticket_dispatch_releases_own_restriction(environment):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment)
    class BindingChangesAfterGrant(_FaultOutputs):
        def begin_cleanup_attempt(self, request, key, owned):
            result = super().begin_cleanup_attempt(request, key, owned)
            runtime.binding_check = lambda saved: check_binding(saved, SimpleNamespace(devices={}))
            return result
    port = BindingChangesAfterGrant("finish_cleanup_attempt", DbOutcomeKind.ROLLED_BACK)
    runtime.outputs = port
    assert (await delete_source_file(runtime, item_id)).phase == "finish_rejected"
    assert not driver.deletes
    action_id = owned.connection.execute("SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
    _apply_cleanup_cancel(owned, action_id)
    port.enabled = False
    assert (await delete_source_file(runtime, item_id)).phase == "canceled"
    assert owned.connection.execute("SELECT status,restriction_state,error_code FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (6, 3, None)
    assert not driver.deletes and not driver.queries


@pytest.mark.parametrize("present", [True, None])
async def test_new_factory_completes_original_query_before_parsing_removed_driver(environment, present):
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.outputs.cleanup_flow import advance_cleanup
    from .test_cleanup_query_save_recovery import _QueryFinishPort
    owned, context, runtime, driver, item_id = await _prepared(environment)
    driver.unknown_delete = True
    driver.matrix_enabled, driver.present = True, present
    runtime.operations = _QueryFinishPort(DbOutcomeKind.ROLLED_BACK, item_id, driver, runtime.retry_gate)
    with pytest.raises(ConsistencyError):
        await delete_source_file(runtime, item_id)
    original_pending = next(iter(runtime.pending_queries.values()))
    original_finish, original_key = original_pending.finish, original_pending.key
    owned.connection.close()
    fresh = context.open_connection()
    try:
        factory = session_cleanup_assembly(devices=environment[0].devices, drivers=DriverRegistry(),
            max_delete_attempts=3, max_query_attempts=3,
            pending_intents=runtime.pending_intents, pending_deletes=runtime.pending_deletes,
            pending_saves=runtime.pending_saves, pending_queries=runtime.pending_queries,
            pending_companions=runtime.pending_companions)
        resumed = factory(fresh)
        await advance_cleanup(resumed)
        assert original_pending.finish is original_finish and original_pending.key == original_key
        assert len(driver.deletes) == 1 and len(driver.queries) == 1
        assert not runtime.pending_queries and not runtime.pending_intents
        assert fresh.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == 3
    finally:
        fresh.connection.close()


async def test_unknown_intent_cannot_be_disposed_while_original_transaction_can_still_commit(environment):
    from types import SimpleNamespace
    from camctl.devices.bindings import check_binding
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.UNKNOWN, after_commit=False)
    runtime.outputs = port
    assert (await delete_source_file(runtime, item_id)).phase == "delete_budget_rejected"
    pending = runtime.pending_intents[item_id]
    assert owned.connection.in_transaction
    binding = runtime.binding_of(item_id)
    fresh = context.open_connection()
    try:
        fresh.connection.execute("PRAGMA busy_timeout=0")
        resumed = replace(runtime, owned=fresh, binding_of=lambda _: binding,
            binding_check=lambda saved: check_binding(saved, SimpleNamespace(devices={})), for_item=None)
        with pytest.raises((OperationalError, ConsistencyError)):
            await delete_source_file(resumed, item_id)
        assert runtime.pending_intents[item_id] is pending and pending.ticket is None
        assert len(port.calls) == 1 and not driver.deletes
        # 原事务确实还能迟到提交；新连接的旧快照缺键不能证明未生效。
        owned.connection.execute("COMMIT")
        port.enabled = False
        assert (await delete_source_file(resumed, item_id)).phase == "failed"
        assert port.calls[1] == port.calls[0]
        assert fresh.connection.execute("SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()[0] == 1
        assert not driver.deletes and not driver.queries
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("fault", ["rollback", "commit_after"])
async def test_recovered_ticket_does_not_dispatch_after_delete_capability_closes(environment, fault):
    owned, context, runtime, driver, item_id = await _prepared(environment)
    port = _FaultOutputs("begin_cleanup_attempt", DbOutcomeKind.ROLLED_BACK if fault == "rollback" else DbOutcomeKind.UNKNOWN,
                         after_commit=fault == "commit_after")
    runtime.outputs = port
    assert (await delete_source_file(runtime, item_id)).phase == "delete_budget_rejected"
    binding = runtime.binding_of(item_id)
    owned.connection.close()
    fresh = context.open_connection()
    try:
        port.enabled = False
        resumed = replace(runtime, owned=fresh, binding_of=lambda _: binding, delete_supported=False, for_item=None)
        assert (await delete_source_file(resumed, item_id)).phase == "failed"
        assert not driver.deletes and not driver.queries
        assert fresh.connection.execute("SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?", (item_id,)).fetchone()[0] == int(fault == "commit_after")
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("broken", ["type", "version", "data", "status", "effect"])
async def test_delete_facts_rejects_incomplete_saved_not_dispatched(environment, broken):
    import json
    from camctl.contracts.values import ConsistencyError
    from camctl.outputs.cleanup_flow import _output_delete_facts
    from .test_cleanup_query_cancel_before_delete import _EntryPort
    owned, context, runtime, driver, item_id = await _prepared(environment)
    action_id, output_id = owned.connection.execute(
        "SELECT action_id,output_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    runtime.outputs = _EntryPort(owned, item_id, action_id, "cancel")
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
    with pytest.raises(ConsistencyError):
        _output_delete_facts(owned.connection, output_id)
    assert not driver.deletes and not driver.queries


@pytest.mark.parametrize("member_status", [4, 5, 6])
async def test_existing_companion_resumes_actual_terminal_member_without_new_call(environment, member_status):
    from .test_cleanup_query_save_recovery import _apply_cleanup_cancel
    owned, context, runtime, driver, item_id = await _prepared(environment)
    companion = _FaultCompanion()
    runtime.operations = companion
    if member_status == 5:
        driver.unknown_delete = True
        driver.matrix_enabled, driver.present = True, True
        runtime.delete_config = replace(runtime.delete_config, max_attempts=1)
        assert (await delete_source_file(runtime, item_id)).phase == "still_present"
    elif member_status == 6:
        action_id = owned.connection.execute(
            "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0]
        _apply_cleanup_cancel(owned, action_id)
    assert (await delete_source_file(runtime, item_id)).phase == "companion_rejected"
    pending = runtime.pending_companions[item_id]
    original_request, original_key = pending.request, pending.key
    raw_owner = runtime.pending_deletes.get(item_id)
    raw = None if raw_owner is None else raw_owner.actual
    member_before = owned.connection.execute("SELECT * FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == member_status
    attempts_before = owned.connection.execute(
        "SELECT a.* FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,)).fetchall()
    calls_before = len(driver.deletes), len(driver.queries)
    companion.enabled = False
    assert (await delete_source_file(runtime, item_id)).phase == "already_terminal"
    assert companion.calls == [(original_request, original_key), (original_request, original_key)]
    assert pending.request is original_request and pending.key == original_key
    if raw_owner is not None:
        assert raw_owner.actual is raw
    assert owned.connection.execute("SELECT * FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == member_before
    assert owned.connection.execute(
        "SELECT a.* FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,)).fetchall() == attempts_before
    assert (len(driver.deletes), len(driver.queries)) == calls_before
    assert not runtime.pending_companions


@pytest.mark.parametrize("member_status", [None, 1, 2, 3])
async def test_pending_companion_nonterminal_preserves_database_and_calls(environment, monkeypatch, member_status):
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import StaleRunFinish, RunOutcome
    from camctl.outputs.cleanup_flow import PendingCleanupCompanion, ProgressCleanupItem
    if member_status == 1:
        # 保留公开固定目标事务产生的未限制成员，不执行下一步 restrict。
        with monkeypatch.context() as patch:
            patch.setattr(OutputsRepository, "restrict_cleanup_item",
                          lambda *args: DbOutcome(DbOutcomeKind.COMPLETED))
            owned, context, runtime, driver, item_id = await _prepared(environment)
    else:
        owned, context, runtime, driver, item_id = await _prepared(environment)
    if member_status == 3:
        saved = runtime.outputs.progress_cleanup_item(
            ProgressCleanupItem(item_id, runtime.occurred_at()), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    if member_status is None:
        item_id += 999
    else:
        assert owned.connection.execute("SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0] == member_status
    pending = PendingCleanupCompanion(owned.metadata.instance_id,
        StaleRunFinish((f"exists/{item_id}",), RunOutcome.CANCELED, runtime.occurred_at()),
        new_operation_key())
    runtime.pending_companions[item_id] = pending
    database_before = tuple(owned.connection.iterdump())
    with pytest.raises(ConsistencyError, match="待存伴随流程没有可靠成员终态"):
        await delete_source_file(runtime, item_id)
    assert tuple(owned.connection.iterdump()) == database_before
    assert runtime.pending_companions[item_id] is pending
    assert not driver.deletes and not driver.queries
