"""可靠 CLOSED 输入在新会话中只用本地事实登记产物与业务终态。"""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import capture_handler
from camctl.capture.models import ResultSetPhase
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import (
    ControlDriver, DeleteDriver, DeviceCallResult, DigestDriver, DriverDeclaration,
    ReadDriver, ResultDriver, StateQueryDriver, StopDriver,
)
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.timelapse import register_timelapse_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .result_consumer_fixtures import RESULT_EVIDENCE, consumer_world, returned, _prepare_through_handler
from ..support.capture import capture_factory, capture_test_session
from ..support.result_cases import ResultCase
from .test_result_consumer_saves import _actual


pytestmark = pytest.mark.asyncio
register_capture_guards()
register_operation_guards()
register_outputs_guards()
register_timelapse_guards()

_CONSUMERS = ("photo", "timelapse", "canceled_timelapse")
_BINDINGS = ("matched", "missing", "mismatch")
_HISTORIES = ("latest_complete", "prior_complete_latest_failed")


class _ClosedBeforeBusiness(Exception):
    """真实仓储已经提交 G，业务终态尚未执行。"""


def _rows(owned, table):
    return owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _capture_facts(owned, action_id):
    return owned.connection.execute(
        "SELECT activity_state,occupancy_state,result_set_state,result_check_json,"
        " capture_json,completion_basis,last_error_json,wait_completed_event_id"
        " FROM device_activities WHERE action_id=?", (action_id,)).fetchone()


def _unconfirmed_error(activity_id):
    return {"code": "capture_result_unconfirmed", "stage": "execution",
            "details": {"activity_id": str(activity_id), "reason": "outputs_unknown"}}


async def closed_consumer_world(tmp_path, monkeypatch, *, consumer, history):
    """原会话资源实际关闭后交付完整CLOSED历史给下一会话。"""
    async with capture_test_session() as session:
        case = ResultCase(session)
        try:
            world = await _prepare_closed_world(tmp_path, monkeypatch,
                consumer=consumer, history=history, case=case)
        except BaseException as original:
            try:
                await case.close()
            except BaseException as closing:
                raise BaseExceptionGroup('CLOSED准备与文件收场均失败', [original, closing])
            raise
        else:
            await case.close()
            return world


async def _prepare_closed_world(tmp_path, monkeypatch, *, consumer, history, case):
    """公开建立 CLOSED 前置并关闭 Owned；返回路径、原申请及可靠事实。

    consumer 为 photo/timelapse/canceled_timelapse，history 为本文件两种历史。
    返回值不含可复用的旧 runtime；设备替身仅用于核原调用次数。
    """
    assert consumer in _CONSUMERS and history in _HISTORIES
    used = 2 if history == "prior_complete_latest_failed" else 1
    owned, runtime, action_id, handler = await case.world(
        tmp_path, "photo" if consumer == "photo" else "timelapse",
        independent_activity=True, result_page_contract=False,
        assembly_options={'check_config': AttemptConfig(used, Decimal('1.25'), Decimal(0))})
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert action_id != activity_id
        kind, name, media_type = (("other", "auxiliary.bin", "application/octet-stream")
            if consumer == "photo" else ("video", "retained-video.mp4", "video/mp4"))
        entry = {"identity": "retained-original", "kind": kind, "complete": True,
                 "size_bytes": 41, "locator": {"path": f"/DCIM/{name}"},
                 "original_name": name, "media_type": media_type}
        actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": str(activity_id), "entries": [entry]}),))
        result_driver = create_autospec(ResultDriver, instance=True)
        result_driver.list_results.return_value = DeviceCallResult.from_outcome(actual)
        case.results.delegate = DriverResultListing(result_driver,
            DeviceBinding('cam-1', 'camctl-adb'), RESULT_EVIDENCE)
        advance = capture_handler(handler)
        await advance(action_id, runtime)
        run_id, status, attempts_used, retry = owned.connection.execute(
            "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()
        assert (status, attempts_used, retry) == (2, 1, 1)
        raw, = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id=? AND attempt_no=1",
            (run_id,)).fetchone()
        assert json.loads(raw) == {"format_version": 1,
            "settlement": {"basis": "observed", "evidence": {
                "type": "results_returned", "version": 1, "data": {}}},
            "observations": [{"type": "result_files_listed", "version": 1,
                "data": {"activity_id": str(activity_id), "entries": [entry]}}]}
        if used == 2:
            result_driver.list_results.return_value = DeviceCallResult.from_outcome(
                _actual(activity_id, with_files=False))
            await advance(action_id, runtime)
            latest_status, latest_input, latest_error = owned.connection.execute(
                "SELECT status,result_json,error_json FROM operation_attempts WHERE run_id=?"
                " ORDER BY attempt_no DESC LIMIT 1", (run_id,)).fetchone()
            assert latest_status == 3 and json.loads(latest_input)["observations"] == []
            assert json.loads(latest_error) == {"code": "transport_timeout", "stage": "transport",
                                              "details": {"received_bytes": 23}}
        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == (2, used, 1)
        (file_id, source, presence, completion, size, saved_name, saved_media), = owned.connection.execute(
            "SELECT id,source_action_id,presence_state,completion_state,size_bytes,original_name,media_type"
            " FROM device_files").fetchall()
        assert (source, presence, completion, size, saved_name, saved_media) == (
            action_id, 2, 3, 41, name, media_type)
        basis_before, = owned.connection.execute(
            "SELECT completion_basis FROM device_activities WHERE id=?", (activity_id,)).fetchone()
        assert basis_before == 1
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        original_close = CaptureRepository.close_result_check_unconfirmed
        inputs = []

        def close(repository, request, key, current):
            inputs.append((request, key))
            receipt = original_close(repository, request, key, current)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=_ClosedBeforeBusiness())

        formed_at = case.clock.wall
        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, "close_result_check_unconfirmed", close)
            with pytest.raises(_ClosedBeforeBusiness):
                await advance(action_id, runtime)
        (request, key), = inputs
        expected_error = _unconfirmed_error(activity_id)
        assert request.action_id == action_id and request.occurred_at == formed_at
        assert request.phase is ResultSetPhase.UNCONFIRMED
        assert request.contract == "task_scope_files"
        assert request.observation == {"reason": "attempts_exhausted"}
        assert request.capture == {"status": "unconfirmed", "error": expected_error}
        assert request.error == expected_error
        close_events = saved_transaction_events(owned.connection, key)
        assert close_events is not None
        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == (6, used, 0)
        stop_driver = None
        if consumer == "canceled_timelapse":
            supported, = owned.connection.execute(
                "SELECT stop_supported FROM device_activities WHERE id=?", (activity_id,)).fetchone()
            assert supported == 1
            from .test_natural_wait_time_eligibility import _request_cancel
            case.clock.wall = formed_at + 10
            await _request_cancel(runtime, action_id, [case.clock.wall])
            case.clock.wall = formed_at + 20
            stop_driver = case.stopper
            await _prepare_through_handler(runtime, action_id, handler, "release")
            assert owned.connection.execute(
                "SELECT activity_state,occupancy_state FROM device_activities WHERE id=?",
                (activity_id,)).fetchone() == (3, 2)
            stop_driver.stop.assert_awaited_once()
        assert owned.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (action_id,)).fetchone() == (
                2, int(consumer == "canceled_timelapse"))
        facts = _capture_facts(owned, action_id)
        assert facts[2] == 4 and facts[5] == basis_before
        assert json.loads(facts[3]) == {"contract": "task_scope_files", "outcome": 3,
                                      "observation": {"reason": "attempts_exhausted"}}
        assert json.loads(facts[4]) == {"status": "unconfirmed", "error": expected_error}
        assert json.loads(facts[6]) == expected_error
        if consumer != "photo":
            assert facts[7] is not None
        world = SimpleNamespace(
            path=Path(owned.connection.execute("PRAGMA database_list").fetchone()[2]),
            metadata=owned.metadata, consumer=consumer, handler=handler, action_id=action_id,
            activity_id=activity_id, run_id=run_id, used=used, file_id=file_id,
            entry=entry, error=expected_error, request=request, key=key,
            close_events=close_events, formed_at=formed_at, capture_facts=facts,
            attempts=_rows(owned, "operation_attempts"), runs=_rows(owned, "operation_runs"),
            files=_rows(owned, "device_files"), events=_rows(owned, "history_events"),
            transactions=_rows(owned, "history_transactions"), control_driver=case.driver,
            result_driver=result_driver, stop_driver=stop_driver,
            expected_output=(action_id, 1, file_id, name, media_type, 41))
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        return world
    finally:
        owned.connection.close()


async def fresh_closed_runtime(owned, world, binding, *, factory_state=None, runtime_options=None, result_supported=True):
    """实际工厂按本次合法配置装配全新的空会话。"""
    ports = tuple(create_autospec(protocol, instance=True) for protocol in (
        ControlDriver, StopDriver, StateQueryDriver, ResultDriver,
        ReadDriver, DigestDriver, DeleteDriver))
    methods = (ports[0].control, ports[1].stop, ports[2].query_state, ports[3].list_results,
               ports[4].open_read, ports[5].digest, ports[6].delete)
    for method in methods:
        method.side_effect = AssertionError("CLOSED 本地收尾不得发出新的设备调用")
    declaration = DriverDeclaration(True, True, True, result_supported, True, True, False)
    driver = SimpleNamespace(declaration=declaration, control=methods[0], stop=methods[1],
        query_state=methods[2], list_results=methods[3], open_read=methods[4],
        digest=methods[5], delete=methods[6])
    registry = DriverRegistry(tuple(DriverEntry(driver_id, driver, declaration, RESULT_EVIDENCE,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED) for driver_id in ("camctl-adb", "alternate-camera")))
    devices = {} if binding == "missing" else {"cam-1": {
        "driver": "alternate-camera" if binding == "mismatch" else "camctl-adb"}}
    options = dict(driver=driver, evidence=RESULT_EVIDENCE,
        devices=devices, drivers=registry, staging=world.path.parent / "staging", media_enabled=False,
        wall_us=lambda: world.formed_at + 5_000_000, monotonic_ns=lambda: 99_000_000_000,
        wait_config=lambda action: CaptureWaitConfig(target_duration_ms=600_000, driver_margin_ms=0))
    options.update({} if runtime_options is None else runtime_options)
    factory = await capture_factory(owned, **options)
    if factory_state is not None:
        factory_state.update(factory=factory, options=options)
    runtime = await factory(owned)
    return runtime, methods


def _assert_preserved(owned, world, methods):
    assert saved_transaction_events(owned.connection, world.key) == world.close_events
    assert _rows(owned, "operation_attempts") == world.attempts
    assert _rows(owned, "operation_runs") == world.runs
    assert _rows(owned, "device_files") == world.files
    assert _capture_facts(owned, world.action_id) == world.capture_facts
    assert _rows(owned, "history_events")[:len(world.events)] == world.events
    assert _rows(owned, "history_transactions")[:len(world.transactions)] == world.transactions
    world.control_driver.control.assert_awaited_once()
    assert world.result_driver.list_results.await_count == world.used
    if world.stop_driver is not None:
        world.stop_driver.stop.assert_awaited_once()
    for method in methods:
        method.assert_not_called()


def _assert_local_finish(owned, world, methods):
    _assert_preserved(owned, world, methods)
    status, error_code, details = owned.connection.execute(
        "SELECT status,error_code,error_details_json FROM actions WHERE id=?", (world.action_id,)).fetchone()
    if world.consumer == "canceled_timelapse":
        assert status == 6
    else:
        assert status == 4
        assert error_code == registered_error("capture_result_unconfirmed")["action_error_id"]
        assert json.loads(details) == world.error["details"]
    (output_id, *output), = owned.connection.execute(
        "SELECT o.id,o.source_action_id,o.kind,o.device_file_id,o.original_name,o.media_type,f.size_bytes"
        " FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=?",
        (world.action_id,)).fetchall()
    assert output_id > 0 and tuple(output) == world.expected_output
    output_transaction, terminal_transaction = owned.connection.execute(
        "SELECT oe.transaction_id,ae.transaction_id FROM outputs o"
        " JOIN history_events oe ON oe.id=o.created_event_id"
        " JOIN actions a ON a.id=o.source_action_id"
        " JOIN history_events ae ON ae.id=a.last_event_id WHERE o.id=?", (output_id,)).fetchone()
    assert output_transaction == terminal_transaction


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("binding", _BINDINGS)
@pytest.mark.parametrize("history", _HISTORIES)
async def test_closed_unconfirmed_keeps_files_without_device_access(
        tmp_path, monkeypatch, consumer, binding, history):
    world = await closed_consumer_world(tmp_path, monkeypatch, consumer=consumer, history=history)
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert owned.metadata == world.metadata
        runtime, methods = await fresh_closed_runtime(owned, world, binding)
        _assert_preserved(owned, world, methods)
        await capture_handler(world.handler)(world.action_id, runtime)
        _assert_local_finish(owned, world, methods)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("binding", _BINDINGS)
@pytest.mark.parametrize("history", _HISTORIES)
async def test_closed_terminal_reentry_does_not_append_outputs(
        tmp_path, monkeypatch, consumer, binding, history):
    world = await closed_consumer_world(tmp_path, monkeypatch, consumer=consumer, history=history)
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        runtime, methods = await fresh_closed_runtime(owned, world, binding)
        advance = capture_handler(world.handler)
        await advance(world.action_id, runtime)
        _assert_local_finish(owned, world, methods)
        before = tuple(owned.connection.iterdump())
        await advance(world.action_id, runtime)
        assert tuple(owned.connection.iterdump()) == before
        _assert_local_finish(owned, world, methods)
    finally:
        owned.connection.close()
