"""有限 RESULTS 收尾经公开生产者、原仓储申请和默认入口恢复。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.capture import handlers
from camctl.capture.handlers import capture_handler
from camctl.capture.models import ResultSetPhase
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.operations.models import CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.devices.evidence import DeviceObservation
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from .bootstrap_recovery import DefaultRecoveryWorld, StorageGate, TransactionFault
from .capture import capture_database
from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, consumer_world, returned
from ..capture.test_capture_contract import _NOW

ENTRANCES = ("normal", "residual", "restricted", "cancel", "restricted_cancel")


def rows(owned, table):
    return owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


class ResultPorts:
    """实际请求身份决定控制观察；结果以正式 v1 观察表达未最终化集合。"""

    def __init__(self, with_file):
        self.with_file = with_file
        self.controls, self.stops, self.results = [], [], []

    async def control(self, request):
        self.controls.append(request)
        observation = {"take_photo": "photo_taken", "start_recording": "start_confirmed",
            "start_timelapse": "timelapse_sent"}[request.operation]
        return DeviceCallResult.from_outcome(returned("operation", observation, request.ticket.target_id))

    async def stop(self, request):
        self.stops.append(request)
        return DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", request.ticket.target_id))

    async def list_results(self, request, batch):
        self.results.append((request, batch))
        entries = [{"identity": "auxiliary", "kind": "other", "complete": True,
            "size_bytes": 41, "locator": {"path": "/DCIM/auxiliary"},
            "original_name": "auxiliary.bin", "media_type": "application/octet-stream"}] if self.with_file else []
        return DeviceCallResult.from_outcome(CallOutcome(effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": request.params["activity_id"], "entries": entries}),)))


class ResultCloseWorld(DefaultRecoveryWorld):
    async def result_producer(self, driver):
        self.database = await capture_database(self.path)
        registry = DriverRegistry((DriverEntry("camctl-adb", driver,
            DriverDeclaration(True, True, False, True, False, False, False),
            RESULT_EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        devices = {"cam-1": {"driver": "camctl-adb",
            "recording": {"max_start_attempts": 3, "max_stop_attempts": 1,
                "start_retry_interval_s": "0", "stop_retry_interval_s": "0"},
            "result_check": {"max_attempts": 1, "call_timeout_s": "1.25", "retry_interval_s": "0"}}}
        factory = session_capture_assembly(devices=devices, drivers=registry,
            staging=Path(self.config.paths.staging), wait_config=execution_wait_config,
            wall_us=lambda: self.wall[0], monotonic_ns=lambda: self.mono[0],
            pending_call_results=self.deps.capture_call_results,
            pending_capture_completions=self.deps.capture_completions,
            pending_result_closes=self.deps.capture_result_closes,
            pending_file_observations=self.deps.capture_file_observations,
            retry_wait_gate=self.deps.capture_retry_gate,
            recording_anchors=self.deps.capture_recording_anchors)
        self.runtime = await factory(self.database, "cam-1")
        assert self.runtime is not None
        return self.runtime

    async def replay_close(self, entrance, check, *, failure=False):
        """先等待公开恢复回调完整交付，再截停新的业务读取。"""
        actual = handlers.resume_result_check_closes
        completed = False

        async def resume(*args, **kwargs):
            nonlocal completed
            result = await actual(*args, **kwargs)
            completed = True
            return result

        entrance = {"cancel": "normal-cancel", "restricted_cancel": "restricted-cancel"}.get(entrance, entrance)
        with self.monkeypatch.context() as patch:
            patch.setattr(handlers, "resume_result_check_closes", resume)
            await self.replay(entrance, check, ready=lambda: completed, failure=failure)


@asynccontextmanager
async def held_result_close(tmp_path, resources, monkeypatch, consumer, after, *, with_file=True):
    world = None
    driver = ResultPorts(with_file)

    async def runtime_factory(owned, **_options):
        nonlocal world
        identity, = owned.connection.execute("SELECT id FROM actions WHERE type IN (1,2,3)").fetchone()
        source = SimpleNamespace(path=tmp_path / "state.db", metadata=owned.metadata,
            action_id=identity, formed_at=_NOW)
        world = ResultCloseWorld(source, resources, monkeypatch)
        return await world.result_producer(driver)

    owned, runtime, identity, handler = await consumer_world(tmp_path, consumer,
        start=False, independent_activity=True, runtime_factory=runtime_factory)
    world.owned, world.handler, world.driver = owned, handler, driver
    world.activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (identity,)).fetchone()
    assert identity != world.activity_id

    class Prepared(Exception):
        pass

    async def prepare(method):
        actual = getattr(CaptureRepository, method)

        def saved(repository, *args, **options):
            receipt = actual(repository, *args, **options)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=Prepared())

        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, method, saved)
            with pytest.raises(Prepared):
                await capture_handler(handler)(identity, runtime)

    try:
        await prepare("finish_start_result")
        if consumer in ("record", "timelapse"):
            elapsed = 60_000_000 if consumer == "record" else 700_000_000
            world.wall[0] += elapsed
            world.mono[0] += elapsed * 1000
        if consumer == "record":
            await prepare("conclude_activity")
        await capture_handler(handler)(identity, runtime)
        world.responsibility = f"results/{world.activity_id}"
        world.run_id, status, used, retry = owned.connection.execute(
            "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key=?", (world.responsibility,)).fetchone()
        assert (status, used, retry) == (2, 1, 1)
        raw = owned.connection.execute("SELECT result_json FROM operation_attempts WHERE run_id=?",
            (world.run_id,)).fetchone()[0]
        observation, = json.loads(raw)["observations"]
        assert (observation["type"], observation["version"]) == ("result_files_listed", 1)
        assert observation["data"]["activity_id"] == str(world.activity_id)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (identity,)).fetchone() == (2,)
        world.attempts, world.files = rows(owned, "operation_attempts"), rows(owned, "device_files")
        assert len(world.files) == int(with_file)
        assert len(driver.controls) == len(driver.results) == 1
        assert len(driver.stops) == int(consumer == "record")
        world.wall[0] += 1_000_000
        world.formed_at = world.wall[0]
        world.method = "close_unconfirmed_result_run" if consumer == "record" else "close_result_check_unconfirmed"
        actual_close = getattr(CaptureRepository, world.method)
        world.inputs, world.receipts, world.faults = [], [], []
        world.gate = None

        def save(repository, request, key, current):
            world.inputs.append((request, key))
            if len(world.inputs) == 1:
                fault = TransactionFault(current.connection, after_commit=after)
                world.faults.append(fault)
                receipt = actual_close(repository, request, key, replace(current, connection=fault))
            elif world.gate is not None:
                receipt = world.gate.submit(lambda *args: actual_close(repository, *args), request, key, current)
            else:
                receipt = actual_close(repository, request, key, current)
            world.receipts.append(receipt)
            return receipt

        monkeypatch.setattr(CaptureRepository, world.method, save)
        from camctl.contracts.values import ConsistencyError
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(identity, runtime)
        assert len(world.inputs) == len(world.receipts) == len(world.faults) == 1
        assert world.receipts[0].kind is DbOutcomeKind.UNKNOWN and world.faults[0].commit_calls == 1
        world.request, world.key = world.inputs[0]
        assert world.request.action_id == identity and world.request.occurred_at == world.formed_at
        if consumer != "record":
            assert world.request.phase is ResultSetPhase.UNCONFIRMED
            assert world.request.contract == "task_scope_files"
            assert world.request.observation == {"reason": "attempts_exhausted"}
            expected_error = {"code": "capture_result_unconfirmed", "stage": "execution",
                "details": {"activity_id": str(world.activity_id), "reason": "outputs_unknown"}}
            assert world.request.capture == {"status": "unconfirmed", "error": expected_error}
            assert world.request.error == expected_error
        owned.connection.close()
        await world.reconnect()
        with world.observer() as current:
            world.owned = current
            world.first_events = saved_transaction_events(current.connection, world.key)
            assert (world.first_events is not None) is after
            assert current.connection.execute("SELECT status,attempts_used,retry_wait_required"
                " FROM operation_runs WHERE id=?", (world.run_id,)).fetchone() == ((6, 1, 0) if after else (2, 1, 1))
            assert current.connection.execute("SELECT status FROM actions WHERE id=?", (identity,)).fetchone() == (2,)
            world.before = tuple(current.connection.iterdump())
            world.history = rows(current, "history_events")
            yield world
    finally:
        owned.connection.close()
        if world is not None:
            world.close()


def original_input_and_external_facts(world):
    assert world.inputs == [(world.request, world.key)] * 2
    assert world.inputs[1][0] is world.request and world.request.occurred_at == world.formed_at
    assert len(world.driver.controls) == len(world.driver.results) == 1
    assert len(world.driver.stops) == int(world.handler == "camera_record")
    assert rows(world.owned, "operation_attempts") == world.attempts
    assert rows(world.owned, "device_files") == world.files


def reliably_closed(world):
    original_input_and_external_facts(world)
    assert world.receipts[1].kind is DbOutcomeKind.COMPLETED, world.receipts[1].error
    events = saved_transaction_events(world.owned.connection, world.key)
    assert events and {event["occurred_at"] for event in events} == {world.formed_at}
    if world.first_events is not None:
        assert events == world.first_events
    assert world.owned.connection.execute("SELECT status,attempts_used,retry_wait_required"
        " FROM operation_runs WHERE id=?", (world.run_id,)).fetchone() == (6, 1, 0)
    assert world.owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(world.key),)).fetchone() == (1,)
    assert rows(world.owned, "history_events")[:len(world.history)] == world.history
    return events
