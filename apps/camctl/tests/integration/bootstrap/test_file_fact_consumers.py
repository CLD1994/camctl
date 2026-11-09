"""公开流程在业务筛选和取消生效前接手原文件保存责任。"""

from copy import deepcopy
from dataclasses import replace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import capture_assembly, lifecycle
from camctl.session.outcome import SessionOutcome
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.registry import DriverRegistry
from camctl.history.events import business_columns
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.transaction import row_facts, saved_transaction_events

from ..capture.result_consumer_fixtures import ResultCatalog, consumer_world
from .test_media_assembly import _config
from ..capture.test_result_consumer_saves import _actual


pytestmark = pytest.mark.asyncio

_METHODS = {
    "discovery": "save_file_observation",
    "presence": "save_file_presence",
    "ownership": "save_file_ownership",
    "completion": "save_file_completion",
}


async def _default_context(tmp_path, monkeypatch):
    """只隔离会话循环，保留默认流程和三个真实工厂，驱动登记为空。"""
    from camctl.devices.drivers import runtime as driver_runtime

    monkeypatch.setattr(driver_runtime, "current_registry", lambda: DriverRegistry(()))
    contexts, factory_calls = [], []
    original = capture_assembly.session_capture_assembly

    def factory(**kwargs):
        real = original(**kwargs)

        def observed(owned, device_id):
            factory_calls.append(device_id)
            return real(owned, device_id)

        return observed

    async def session(context, _source):
        contexts.append(context)
        return SessionOutcome(succeeded=True)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", factory)
    monkeypatch.setattr(lifecycle, "run_session",
                        create_autospec(lifecycle.run_session, side_effect=session))
    deps = lifecycle.build_runtime(CommandMode.RUN, _config(tmp_path), catalog=ResultCatalog())
    try:
        assert deps.startup_error is None
        outcome = await lifecycle.execute_command(deps, None)
        assert outcome.succeeded
        assert len(contexts) == 1
        return deps, contexts[0], factory_calls
    except BaseException:
        lifecycle.close_runtime(deps)
        raise


async def _hold_original_file_stage(tmp_path, resources, monkeypatch, stage, commit_after):
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.capture.handlers import capture_handler
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.ports import DeviceCallResult
    from ..capture.result_consumer_fixtures import RESULT_EVIDENCE
    from ..support.bootstrap_consumers import consumer_world as media_world, TransactionFault, StorageGate

    owned, preparation, _action, _handler = await consumer_world(tmp_path, "record")
    actual = _actual(1, with_files=True, complete=True)

    class ResultDriver:
        def __init__(self):
            self.requests, self.returned_at = [], []

        async def list_results(self, request, batch):
            self.requests.append(request)
            self.returned_at.append(world.wall[0])
            return DeviceCallResult.from_outcome(deepcopy(actual))

    driver = ResultDriver()
    results = DriverResultListing(driver, DeviceBinding("cam-1", "camctl-adb"), RESULT_EVIDENCE)
    world = await media_world((owned, preparation.roots, None), resources,
        results=results, evidence=RESULT_EVIDENCE)
    method = _METHODS[stage]
    original_save = getattr(CaptureRepository, method)
    inputs, order, receipts = [], [], []
    preparing = [True]
    prepare_gate = StorageGate(original_key=True, unknown=True)

    def save(repository, command, key, current):
        inputs.append((command, key))
        if preparing[0]:
            if len(inputs) == 1:
                fault = TransactionFault(current.connection, after_commit=commit_after)
                result = original_save(repository, command, key, replace(current, connection=fault))
                assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            else:
                result = prepare_gate.submit(lambda *args: original_save(repository, *args), command, key, current)
        else:
            order.append("file_confirmation")
            result = original_save(repository, command, key, current)
        receipts.append(result)
        return result

    from camctl.persistence.repositories.operations import OperationRepository
    actual_finish = OperationRepository.finish_attempt
    world.result_saves = []

    def finish(repository, request, key, current, **options):
        if request.ticket.operation == "result":
            world.result_saves.append((request, key))
        return actual_finish(repository, request, key, current, **options)

    monkeypatch.setattr(OperationRepository, "finish_attempt", finish)
    monkeypatch.setattr(CaptureRepository, method, save)
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, world.runtime)
        assert len(inputs) == 2 and inputs[1] == inputs[0]
        assert len(driver.requests) == 1 and world.result_saves == []
        await world.reconnect()
        assert (saved_transaction_events(world.owned.connection, inputs[0][1]) is not None) is commit_after
        preparing[0] = False
    except BaseException:
        try:
            await world.close()
        finally:
            owned.connection.close()
        raise
    return world, driver, actual, inputs, order, receipts


def _assert_original_result(world, driver, actual):
    from ..capture.test_result_consumer_saves import _assert_actual_saved

    _assert_actual_saved(world.owned, 1, actual)
    assert len(world.result_saves) == 1
    finish, result_key = world.result_saves[0]
    assert finish.ticket == driver.requests[0].ticket
    assert finish.outcome.outcome == actual and finish.occurred_at == driver.returned_at[0]
    events = saved_transaction_events(world.owned.connection, result_key)
    assert events is not None and {event["occurred_at"] for event in events} == {driver.returned_at[0]}
    assert world.owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(result_key),)).fetchone() == (1,)


def _business_state(owned):
    return tuple(row_facts(owned.connection, "actions", 1)[name]
                 for name in business_columns("actions"))


def _result_state(owned):
    return owned.connection.execute(
        "SELECT r.status,r.attempts_used,r.retry_wait_required,t.attempt_no,t.status,"
        "t.result_json,t.error_json FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE r.responsibility_key='results/1' ORDER BY t.attempt_no").fetchall()


def _file_complete(owned):
    return owned.connection.execute(
        "SELECT source_action_id,presence_state,completion_state,size_bytes FROM device_files"
        " WHERE observer_action_id=1").fetchone() == (1, 2, 3, 41)


@pytest.mark.parametrize("entrance,commit_after", [
    ("normal", True), ("residual", True), ("restricted", True),
    ("cancel", True), ("cancel", False),
    ("restricted_cancel", True), ("restricted_cancel", False),
])
@pytest.mark.parametrize("stage", tuple(_METHODS))
async def test_default_file_consumers_confirm_original_stage_before_filters(
        tmp_path, capture_resources, monkeypatch, entrance, commit_after, stage):
    from ..support.bootstrap_consumers import accept_cancellation

    world, driver, actual, inputs, order, receipts = await _hold_original_file_stage(
        tmp_path, capture_resources, monkeypatch, stage, commit_after)
    owned = world.owned
    try:
        before_action, before_result = _business_state(owned), _result_state(owned)
        cancel_id = None
        if entrance in ("cancel", "restricted_cancel"):
            cancel_id = await accept_cancellation(world, occurred_at=inputs[0][0].occurred_at + 30,
                request_id="3" if commit_after else "2")
            original_apply = CancellationRepository.apply_cancel_target

            def apply(repository, command, key, current, **options):
                assert _file_complete(current), "取消生效前须可靠保存全部原文件事实"
                order.append("cancel_apply")
                return original_apply(repository, command, key, current, **options)

            monkeypatch.setattr(CancellationRepository, "apply_cancel_target", apply)
        world.wall[0] = inputs[0][0].occurred_at + 5_000_000
        world.prime_clock()
        selected = {"cancel": "normal-cancel", "restricted_cancel": "restricted-cancel"}.get(entrance, entrance)
        await world.replay(selected, kind="round" if cancel_id is not None else "files", drivers=DriverRegistry(()))
        assert len(inputs) == 3 and inputs[-1] == inputs[0]
        assert order[0] == "file_confirmation" and _file_complete(owned)
        assert _result_state(owned) == before_result and len(driver.requests) == 1
        assert world.reader.requests == [] and world.tools() == [] and world.driver.calls == []
        if commit_after and cancel_id is None:
            assert _business_state(owned) == before_action
        if cancel_id is not None:
            assert "cancel_apply" in order
            assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (cancel_id,)).fetchone()[0] in (2, 3)
            assert owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=1").fetchone() == (1,)
        from camctl.capture.handlers import capture_handler
        runtime = await world.factory(world.database, "cam-1")
        await capture_handler("camera_record")(1, runtime)
        ticket = driver.requests[0].ticket
        final = owned.connection.execute(
            "SELECT status,result_json,error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()
        assert final[0] == 3 and final[1] is not None and final[2] is not None, final
        _assert_original_result(world, driver, actual)
        assert len(inputs) == 3
        if stage == "discovery":
            first = saved_transaction_events(owned.connection, inputs[0][1])
            assert first is not None and first[0]["occurred_at"] == inputs[0][0].occurred_at
    finally:
        await world.close()


@pytest.mark.parametrize("stage", tuple(_METHODS))
@pytest.mark.parametrize("confirmation", ["unknown", "rejected"])
@pytest.mark.parametrize("entrance", ["cancel", "restricted_cancel"])
async def test_default_cancel_keeps_original_file_responsibility_when_confirmation_fails(
        tmp_path, capture_resources, monkeypatch, stage, confirmation, entrance):
    from ..support.bootstrap_consumers import StorageGate, accept_cancellation

    world, driver, actual, inputs, order, receipts = await _hold_original_file_stage(
        tmp_path, capture_resources, monkeypatch, stage, False)
    owned = world.owned
    try:
        cancel_id = await accept_cancellation(world, occurred_at=inputs[0][0].occurred_at + 30)
        before_action, before_result = _business_state(owned), _result_state(owned)
        original_apply = CancellationRepository.apply_cancel_target

        def apply(repository, command, key, current, **options):
            order.append("cancel_apply")
            return original_apply(repository, command, key, current, **options)

        monkeypatch.setattr(CancellationRepository, "apply_cancel_target", apply)
        gate = StorageGate(original_key=confirmation == "unknown", unknown=confirmation == "unknown",
            table="device_files", column={"discovery": None, "presence": "presence_state",
                "ownership": "source_action_id", "completion": "completion_state"}[stage],
            operation=sqlite3.SQLITE_INSERT if stage == "discovery" else sqlite3.SQLITE_UPDATE)
        gate.install(monkeypatch, inputs[0][1])
        world.wall[0] = inputs[0][0].occurred_at + 5_000_000
        world.prime_clock()
        selected = "normal-cancel" if entrance == "cancel" else "restricted-cancel"
        await world.replay(selected, kind="files", failure=True, drivers=DriverRegistry(()))
        assert len(inputs) == 4 and all(value == inputs[0] for value in inputs)
        assert order == ["file_confirmation", "file_confirmation"]
        assert gate.hits == gate.rollbacks == len(gate.receipts) == 2
        expected = DbOutcomeKind.UNKNOWN if confirmation == "unknown" else DbOutcomeKind.ROLLED_BACK
        assert all(receipt.kind is expected for receipt in gate.receipts)
        assert _business_state(owned) == before_action and _result_state(owned) == before_result
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (cancel_id,)).fetchone() == (1,)
        assert len(driver.requests) == 1 and world.reader.requests == [] and world.tools() == []
        assert world.driver.calls == []
        # 同一会话仍持有原责任；外部故障解除后不换键、不重列举即可保存。
        gate.enabled = False
        await world.replay(selected, kind="files", drivers=DriverRegistry(()))
        assert inputs[-1] == inputs[0] and len(inputs) == 5 and _file_complete(owned)
        assert len(driver.requests) == 1
        from camctl.capture.handlers import capture_handler
        runtime = await world.factory(world.database, "cam-1")
        await capture_handler("camera_record")(1, runtime)
        assert len(driver.requests) == 1 and world.reader.requests == [] and world.tools() == []
        _assert_original_result(world, driver, actual)
        assert len(inputs) == 5
    finally:
        await world.close()
