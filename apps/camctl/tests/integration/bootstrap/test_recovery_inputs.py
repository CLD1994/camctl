"""本次会话统一固定恢复水位，生产工厂共享实际启动边界。"""

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.recovery import RecoveryBoundary
from camctl.persistence.initialization import initialize_state

from .test_composition import _config_for


def test_embedded_runtime_does_not_assume_previous_local_work_stopped(tmp_path):
    config = _config_for(tmp_path)
    initialize_state(config, tmp_path / "state.db")
    deps = build_runtime(CommandMode.RUN, config)
    try:
        assert deps.recovery_boundary is RecoveryBoundary.UNCONFIRMED
        assert deps.recovery_max_event_id is None
    finally:
        close_runtime(deps)


@pytest.mark.asyncio
async def test_production_factories_share_initial_boundary_before_new_session_events(tmp_path, monkeypatch):
    from camctl.bootstrap import capture_assembly

    config = _config_for(tmp_path)
    initialize_state(config, tmp_path / "state.db")
    factories = []
    original = capture_assembly.session_capture_assembly

    def observed(**kwargs):
        factories.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", observed)
    deps = build_runtime(CommandMode.RUN, config, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    try:
        outcome = await execute_command(deps, None)

        assert outcome.succeeded
        assert deps.recovery_max_event_id == 0
        assert len(factories) == 3
        assert all(factory["recovery_boundary"] is RecoveryBoundary.HOST_LOCAL_SETTLED for factory in factories)
        assert all(factory["recovery_max_event_id"]() == 0 for factory in factories)
    finally:
        close_runtime(deps)


@pytest.mark.asyncio
async def test_production_capture_factories_share_actual_pending_result_responsibility(tmp_path, monkeypatch):
    """首次真实 START 回执未知后，另两默认工厂只接手原结果。"""
    from dataclasses import replace
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from camctl.acceptance.service import AcceptanceContext
    from camctl.bootstrap import capture_assembly, lifecycle
    from camctl.capture.handlers import capture_handler
    from camctl.contracts.values import ConsistencyError, new_operation_key
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.ports import DriverDeclaration
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.scheduling import ObserveWindowRequest, SchedulingRepository, StartActionRequest
    from camctl.persistence.transaction import saved_transaction_events
    from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog
    from ..capture.test_capture_contract import _NOW
    from ..support.acceptance import accept_document
    from ..support.bootstrap_recovery import StorageGate, TransactionFault
    from ..support.bootstrap_result_recovery import ResultPorts
    from ..support.database import business_database
    from ..support.session import session_config

    config = session_config(tmp_path, devices={"cam-1": {"kind": "camera", "driver": "camctl-adb"}})
    path = tmp_path / "state.db"
    initialize_state(config, path)
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = await accept_document({"request_id": "1", "created_at": instant, "name": "原结果共享",
        "actions": [{"name": "录像", "type": "camera_record", "device_id": "cam-1",
            "scheduled_at": instant, "params": {"type": "video"}, "policy": {"max_delay_ms": 60000}}]},
        AcceptanceContext(CommandMode.RUN, ResultCatalog(), AcceptanceRepository(),
            SimpleNamespace(utc_micros=lambda: _NOW)), path)
    assert accepted.plan_id == 1
    scheduling = SchedulingRepository()
    async with business_database(path) as database:
        for method, request in ((scheduling.observe_window, ObserveWindowRequest(1, _NOW, _NOW)),
                                (scheduling.start_action, StartActionRequest(1, _NOW, _NOW))):
            key = new_operation_key()
            receipt = await database.write(lambda owned: method(request, key, owned), key=key)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error

    driver = ResultPorts(False)
    registry = DriverRegistry((DriverEntry("camctl-adb", driver,
        DriverDeclaration(True, True, False, True, False, False, False), RESULT_EVIDENCE,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    factories, arguments, inputs, receipts = [], [], [], []
    actual_factory = capture_assembly.session_capture_assembly
    actual_save = CaptureRepository.finish_start_result
    actual_session = lifecycle.run_session

    def factory(**options):
        actual = actual_factory(**options, wall_us=lambda: _NOW, monotonic_ns=lambda: 7_000_000_000)
        factories.append(actual)
        arguments.append(options)
        return actual

    def save(repository, finish, observation, key, owned, **options):
        inputs.append((finish, observation, key, options))
        if len(inputs) == 1:
            fault = TransactionFault(owned.connection, after_commit=True)
            receipt = actual_save(repository, finish, observation, key, replace(owned, connection=fault), **options)
            assert fault.commit_calls == 1
        elif len(inputs) == 2:
            gate = StorageGate(original_key=True, unknown=True)
            receipt = gate.submit(lambda _request, operation_key, current:
                actual_save(repository, finish, observation, operation_key, current, **options), None, key, owned)
            assert gate.hits == 1
        else:
            receipt = actual_save(repository, finish, observation, key, owned, **options)
        receipts.append(receipt)
        return receipt

    class BootstrapObserved(BaseException):
        pass

    async def session(context, source):
        actual_opened = context.on_session_open

        async def opened(database):
            await actual_opened(database)
            assert len(factories) == 3
            runtimes = [await factory(database, "cam-1") for factory in factories]
            with pytest.raises(ConsistencyError) as failure:
                await capture_handler("camera_record")(1, runtimes[0])
            assert receipts, str(failure.value)
            assert receipts[0].kind is DbOutcomeKind.UNKNOWN
            await capture_handler("camera_record")(1, runtimes[1])
            await capture_handler("camera_record")(1, runtimes[2])
            assert inputs == [inputs[0]] * 3
            assert all(value[0] is inputs[0][0] and value[1] is inputs[0][1] for value in inputs)
            assert all(value[3]["start_finish"] is inputs[0][3]["start_finish"] for value in inputs)
            assert [receipt.kind for receipt in receipts] == [DbOutcomeKind.UNKNOWN, DbOutcomeKind.UNKNOWN, DbOutcomeKind.COMPLETED]
            assert len(driver.controls) == 1 and driver.results == [] and driver.stops == []
            assert all(option["pending_call_results"] is arguments[0]["pending_call_results"] for option in arguments)
            events = await database.read(lambda owned: saved_transaction_events(owned.connection, inputs[0][2]))
            assert events and {event["occurred_at"] for event in events} == {_NOW}
            assert await database.read(lambda owned: owned.connection.execute(
                "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
                (str(inputs[0][2]),)).fetchone()) == (1,)
            raise BootstrapObserved()

        context.on_session_open = opened
        context.clock = SimpleNamespace(utc_micros=lambda: _NOW)
        return await actual_session(context, source)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", factory)
    monkeypatch.setattr(CaptureRepository, "finish_start_result", save)
    monkeypatch.setattr(lifecycle, "run_session", session)
    monkeypatch.setattr("camctl.devices.drivers.runtime.current_registry", lambda: registry)
    deps = build_runtime(CommandMode.RUN, config, catalog=ResultCatalog())
    try:
        try:
            result = await execute_command(deps, None)
        except BootstrapObserved:
            pass
        else:
            pytest.fail(f"未到三工厂原结果交付边界: {result!r}")
    finally:
        close_runtime(deps)
