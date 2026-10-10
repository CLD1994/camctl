"""取回和清理逐项使用设备文件原观察者的绑定。"""

from dataclasses import replace
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.bootstrap.flows import capture_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime
from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.capture.handlers import ObservedFile
from camctl.capture.result_inputs import RESULT_FILES_CONTRACT, RESULT_PAGE_CONTRACT
from camctl.capture.results import FileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.devices.read_session import ReadSession
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..capture.test_capture_contract import ResultsDouble
from .test_binding_changes import _Catalog as CaptureCatalog, _NOW
from .test_obtain_flow import _MemoryStream

pytestmark = pytest.mark.asyncio
_CONTENT = b"binding-photo-bytes"
_EVIDENCE = EvidenceRegistry((
    EvidenceContract(type="operation_returned", version=1, operation="control", fields=frozenset()),
    EvidenceContract(type="photo_taken", version=1, operation="control",
                     fields=frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract(type="results_returned", version=1, operation="result", fields=frozenset()),
    RESULT_FILES_CONTRACT,
    RESULT_PAGE_CONTRACT,
    EvidenceContract(type="read_returned", version=1, operation="read", fields=frozenset()),
    EvidenceContract(type="delete_returned", version=1, operation="delete", fields=frozenset()),
    EvidenceContract(type="file_absent", version=1, operation="delete",
                     fields=frozenset({"cleanup_item_id"}), identity_field="cleanup_item_id"),
    EvidenceContract(type="file_presence", version=1, operation="query",
                     fields=frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"),
))


class Catalog(CaptureCatalog):
    def action_types(self):
        return super().action_types() | {"obtain_action_outputs", "delete_action_outputs"}


class Driver:
    """控制、读取、删除和查询契约替身；记录实际调用的原绑定。"""

    def __init__(self, owned):
        self.owned = owned
        self.reads, self.deletes, self.queries = [], [], []
        self.fail_a = False
        self.unknown_delete = False
        self.unknown_query = False
        self.query_present = False
        self.delete_requests, self.query_requests = [], []

    async def control(self, request):
        identity = self.owned.connection.execute(
            "SELECT d.id FROM device_activities d JOIN actions a ON a.id=d.action_id"
            " WHERE a.device_id=?", (request.binding.device_id,)).fetchone()[0]
        return DeviceCallResult((DeviceObservation(
            type="photo_taken", version=1, data={"activity_id": str(identity)}),), None)

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        binding = tuple(json.loads(source.file_id)[:2])
        self.reads.append(binding)
        stream = (_BrokenStream(_CONTENT[offset:]) if self.fail_a and binding[0] == "cam-a"
                  else _MemoryStream(_CONTENT[offset:]))
        return ReadSession(source, offset, stream, idle_timeout_s)

    async def delete(self, request):
        self.delete_requests.append(request)
        self.deletes.append(request.binding)
        if self.unknown_delete:
            return DeviceCallResult((), None)
        return DeviceCallResult((DeviceObservation(
            type="file_absent", version=1,
            data={"cleanup_item_id": request.params["cleanup_item_id"]}),), None)

    async def query_state(self, request):
        self.query_requests.append(request)
        self.queries.append(request.binding)
        if self.unknown_query:
            return DeviceCallResult((), {"code": "source_query_failed"})
        return DeviceCallResult((DeviceObservation(
            type="file_presence", version=1,
            data={"cleanup_item_id": request.params["cleanup_item_id"],
                  "present": self.query_present}),), None)


class _BrokenStream(_MemoryStream):
    """完成一个四字节段后可靠结束读取，留下可续传的实际进度。"""

    def read(self, limit):
        if self._position >= 4:
            raise OSError("源端读取失败")
        return super().read(min(limit, 4))


def _registry(driver):
    declaration = DriverDeclaration(
        control_supported=True, stop_supported=False, query_supported=True,
        result_supported=False, read_supported=True, digest_supported=False,
        delete_supported=True)
    return DriverRegistry(tuple(DriverEntry(
        driver_id=driver_id, driver=driver, declaration=declaration, evidence=_EVIDENCE,
        status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
        for driver_id in ("camctl-adb", "alternate-camera")))


@pytest.fixture
def environment(tmp_path):
    cfg = load_config({
        "paths": {"state_db": str(tmp_path / "state.db"), "log_file": str(tmp_path / "log"),
                  **{name: str(tmp_path / name) for name in ("staging", "ready", "processing")}},
        "devices": {device: {"kind": "camera", "driver": "camctl-adb"}
                    for device in ("cam-a", "cam-b")}}, ConfigDefaults())
    assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    owned = open_existing(deps.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    context = SimpleNamespace(
        open_connection=lambda: open_existing(deps.state_db, DbOpenMode.EXISTING_RW, DbConfig()),
        clock=SimpleNamespace(utc_micros=lambda: _NOW))
    try:
        yield cfg, owned, context, Driver(owned)
    finally:
        owned.connection.close()
        close_runtime(deps)


def _accept(owned, request_id, actions):
    outcome = AcceptanceRepository().process_input(ProcessInput(
        ParsedInput("plan.json", {"request_id": request_id, "created_at": "2026-01-15 08:00:00",
                                  "name": "双设备", "actions": actions}),
        Catalog(), CommandMode.RUN, _NOW), new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


async def _save_photos(cfg, owned, context, driver):
    _accept(owned, "1", [{"name": device, "type": "camera_take_photo", "device_id": device,
                           "scheduled_at": "2026-01-15 09:00:00", "group": "files",
                           "params": {"type": "single_shot"}, "policy": {"max_delay_ms": 1000}}
                          for device in ("cam-a", "cam-b")])
    identities = [row[0] for row in owned.connection.execute("SELECT id FROM actions ORDER BY id")]
    results = ResultsDouble({identity: (ObservedFile(
        identity=f"photo-{identity}", locator={"path": f"/DCIM/{identity}"},
        evidence={"listing": str(identity)}, complete=True, size_bytes=len(_CONTENT),
        kind=FileKind.PHOTO, original_name=f"photo-{identity}.jpg", media_type="image/jpeg"),)
        for identity in identities})
    factory = session_capture_assembly(
        devices=cfg.devices, drivers=_registry(driver), results=results,
        staging=Path(cfg.paths.staging), wall_us=lambda: _NOW, monotonic_ns=lambda: 0,
        wait_config=lambda _action: CaptureWaitConfig(target_duration_ms=1000, driver_margin_ms=0))
    capture = capture_flow(factory)
    await capture(context)
    await capture.settle()
    assert owned.connection.execute("SELECT status FROM actions ORDER BY id").fetchall() == [(3,), (3,)]
    assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)


def _changed(cfg, state):
    devices = dict(cfg.devices)
    if state == "missing":
        devices.pop("cam-a")
    elif state == "mismatch":
        devices["cam-a"] = {"kind": "camera", "driver": "alternate-camera"}
    return replace(cfg, devices=devices)


@pytest.mark.parametrize("state", ["missing", "mismatch"])
async def test_selected_obtain_item_binding_failure_keeps_other_device(environment, state):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
                          "scheduled_at": "2026-01-15 09:00:00",
                          "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])
    current = _changed(cfg, state)
    factory = session_obtain_assembly(
        devices=current.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=1024,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    for _ in range(3):
        await obtain_flow(factory)(context)

    status = enum_for("obtain_items.status")
    rows = owned.connection.execute(
        "SELECT a.device_id,i.status,i.delivery_id FROM obtain_items i JOIN outputs o ON o.id=i.output_id"
        " JOIN device_files f ON f.id=o.device_file_id JOIN actions a ON a.id=f.observer_action_id"
        " ORDER BY a.device_id").fetchall()
    assert rows[0] == ("cam-a", int(status.FAILED), None)
    assert rows[1][0:2] == ("cam-b", int(status.DELIVERY_CREATED))
    assert driver.reads == [("cam-b", "camctl-adb")]
    assert owned.connection.execute("SELECT status FROM deliveries").fetchall() == [(5,)]
    assert owned.connection.execute("SELECT status FROM actions WHERE type=4").fetchone() == (4,)
    assert len(list(Path(cfg.paths.ready).glob("*.jpg"))) == 1


@pytest.mark.parametrize("state", ["missing", "mismatch", "matched"])
async def test_cleanup_uses_each_members_original_binding(environment, state):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    output_ids = [str(row[0]) for row in owned.connection.execute("SELECT id FROM outputs ORDER BY id")]
    _accept(owned, "2", [{"name": "清理", "type": "delete_action_outputs",
                          "scheduled_at": "2026-01-15 09:00:00", "params": {"output_ids": output_ids}}])
    current = _changed(cfg, state)
    factory = session_cleanup_assembly(
        devices=current.devices, drivers=_registry(driver), max_delete_attempts=3,
        max_query_attempts=3, staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0)
    for _ in range(3):
        await cleanup_flow(factory)(context)

    assert sorted(binding.device_id for binding in driver.deletes) == (
        ["cam-a", "cam-b"] if state == "matched" else ["cam-b"])
    assert all(binding.driver_id == "camctl-adb" for binding in driver.deletes)
    assert owned.connection.execute("SELECT status FROM cleanup_items ORDER BY id").fetchall() == (
        [(4,), (4,)] if state == "matched" else [(5,), (4,)])
    assert owned.connection.execute("SELECT status FROM actions WHERE type=5").fetchone() == (
        (3,) if state == "matched" else (4,))


async def test_matched_single_device_cleanup_saves_reliable_absence(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    output_id = str(owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0])
    _accept(owned, "2", [{"name": "清理", "type": "delete_action_outputs",
                          "scheduled_at": "2026-01-15 09:00:00",
                          "params": {"output_ids": [output_id]}}])
    factory = session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver), max_delete_attempts=3,
        max_query_attempts=3, staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0)
    await cleanup_flow(factory)(context)

    assert [(binding.device_id, binding.driver_id) for binding in driver.deletes] == [
        ("cam-a", "camctl-adb")]
    assert owned.connection.execute("SELECT status FROM cleanup_items").fetchone() == (4,)
    assert owned.connection.execute("SELECT status FROM actions WHERE type=5").fetchone() == (3,)
    assert owned.connection.execute(
        "SELECT presence_state FROM device_files WHERE id=("
        "SELECT device_file_id FROM outputs WHERE id=?)", (output_id,)).fetchone() == (3,)


@pytest.mark.parametrize("state", ["missing", "mismatch"])
async def test_verified_copy_publishes_without_source_device(environment, state):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
                          "scheduled_at": "2026-01-15 09:00:00",
                          "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])

    def factory(current):
        return session_obtain_assembly(
            devices=current.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
            ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=1024,
            occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)

    await obtain_flow(factory(cfg))(context)
    assert owned.connection.execute(
        "SELECT verification_state FROM file_copies ORDER BY id").fetchall() == [(5,), (5,)]
    read_count = len(driver.reads)
    current = _changed(cfg, state)
    await obtain_flow(factory(current))(context)

    assert len(driver.reads) == read_count
    assert owned.connection.execute("SELECT status FROM deliveries ORDER BY id").fetchall() == [(5,), (5,)]
    assert owned.connection.execute("SELECT status FROM actions WHERE type=4").fetchone() == (3,)
    assert sorted(path.read_bytes() for path in Path(cfg.paths.ready).glob("*.jpg")) == [_CONTENT, _CONTENT]


@pytest.mark.parametrize("state", ["missing", "mismatch"])
async def test_unfinished_read_binding_failure_closes_responsibility_and_keeps_progress(environment, state):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
                          "scheduled_at": "2026-01-15 09:00:00",
                          "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])

    def factory(current):
        return session_obtain_assembly(
            devices=current.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
            ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
            occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)

    driver.fail_a = True
    await obtain_flow(factory(cfg))(context)
    copy_id, delivery_id, run_id = owned.connection.execute(
        "SELECT c.id,d.id,r.id FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id"
        " JOIN device_files f ON f.id=c.source_device_file_id"
        " JOIN actions a ON a.id=f.observer_action_id"
        " JOIN operation_runs r ON r.copy_id=c.id WHERE a.device_id='cam-a'").fetchone()
    assert owned.connection.execute(
        "SELECT committed_bytes,slot_device_id FROM file_copies WHERE id=?", (copy_id,)).fetchone() == (4, "cam-a")
    attempts = owned.connection.execute(
        "SELECT id,status,result_json,effect_state FROM operation_attempts WHERE run_id=?", (run_id,)).fetchall()
    assert len(attempts) == 1 and attempts[0][1] == 3
    read_count = len(driver.reads)
    driver.fail_a = False
    current = _changed(cfg, state)
    for _ in range(2):
        await obtain_flow(factory(current))(context)

    assert owned.connection.execute("SELECT status FROM deliveries WHERE id=?", (delivery_id,)).fetchone() == (6,)
    assert owned.connection.execute(
        "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?", (run_id,)).fetchone() == (4, 1, 0)
    assert owned.connection.execute(
        "SELECT committed_bytes,slot_device_id FROM file_copies WHERE id=?", (copy_id,)).fetchone() == (4, None)
    assert owned.connection.execute(
        "SELECT status,source_dependency FROM obtain_items WHERE delivery_id=?", (delivery_id,)).fetchone() == (3, 0)
    assert owned.connection.execute(
        "SELECT id,status,result_json,effect_state FROM operation_attempts WHERE run_id=?", (run_id,)).fetchall() == attempts
    assert len(driver.reads) == read_count
    assert owned.connection.execute("SELECT status FROM actions WHERE type=4").fetchone() == (4,)
    assert [path.read_bytes() for path in Path(cfg.paths.ready).glob("*.jpg")] == [_CONTENT]


async def _pending_cleanup(environment, *, confirmed_present=False):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    _accept(owned, "2", [{"name": "清理", "type": "delete_action_outputs",
                          "scheduled_at": "2026-01-15 09:00:00",
                          "params": {"output_ids": [str(output_id)]}}])
    driver.unknown_delete = True
    driver.unknown_query = not confirmed_present
    driver.query_present = confirmed_present
    factory = session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver), max_delete_attempts=3,
        max_query_attempts=3, staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0)
    await cleanup_flow(factory)(context)
    assert owned.connection.execute("SELECT status,restriction_state FROM cleanup_items").fetchone() == (
        3, int(enum_for("cleanup_items.restriction_state").IRREVERSIBLE))
    return output_id


@pytest.mark.parametrize("state", ["missing", "mismatch"])
async def test_unfinished_cleanup_binding_failure_preserves_unknown_delete(environment, state):
    cfg, owned, context, driver = environment
    output_id = await _pending_cleanup(environment)
    source_before = owned.connection.execute(
        "SELECT presence_state FROM device_files WHERE id=(SELECT device_file_id FROM outputs WHERE id=?)",
        (output_id,)).fetchone()
    attempts = owned.connection.execute(
        "SELECT id,status,effect_state,result_json FROM operation_attempts"
        " WHERE run_id IN (SELECT id FROM operation_runs WHERE cleanup_item_id IS NOT NULL) ORDER BY id").fetchall()
    assert len(attempts) == 2
    current = _changed(cfg, state)
    factory = session_cleanup_assembly(
        devices=current.devices, drivers=_registry(driver), max_delete_attempts=3,
        max_query_attempts=3, staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0)
    await cleanup_flow(factory)(context)

    assert owned.connection.execute("SELECT status,restriction_state,error_code FROM cleanup_items").fetchone() == (
        5, int(enum_for("cleanup_items.restriction_state").IRREVERSIBLE), 5)
    assert owned.connection.execute(
        "SELECT status,attempts_used,retry_wait_required FROM operation_runs"
        " WHERE cleanup_item_id IS NOT NULL ORDER BY id").fetchall() == [(4, 1, 0), (4, 1, 0)]
    assert owned.connection.execute(
        "SELECT id,status,effect_state,result_json FROM operation_attempts"
        " WHERE run_id IN (SELECT id FROM operation_runs WHERE cleanup_item_id IS NOT NULL) ORDER BY id").fetchall() == attempts
    assert owned.connection.execute(
        "SELECT presence_state FROM device_files WHERE id=(SELECT device_file_id FROM outputs WHERE id=?)",
        (output_id,)).fetchone() == source_before
    assert len(driver.deletes) == len(driver.queries) == 1
    assert owned.connection.execute("SELECT status FROM actions WHERE type=5").fetchone() == (4,)


async def test_cleanup_passes_committed_ticket_and_each_operation_timeout(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    output_id = str(owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0])
    _accept(owned, "2", [{"name": "清理", "type": "delete_action_outputs",
                          "scheduled_at": "2026-01-15 09:00:00",
                          "params": {"output_ids": [output_id]}}])
    devices = dict(cfg.devices)
    devices["cam-a"] = {**devices["cam-a"], "cleanup": {
        "delete_timeout_s": Decimal("1.25"), "query_timeout_s": Decimal("2.75")}}
    driver.unknown_delete = True
    factory = session_cleanup_assembly(
        devices=devices, drivers=_registry(driver), max_delete_attempts=3,
        max_query_attempts=3, staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0)
    await cleanup_flow(factory)(context)

    for request, operation, timeout in (
            (driver.delete_requests[0], "delete", Decimal("1.25")),
            (driver.query_requests[0], "query", Decimal("2.75"))):
        assert request.ticket is not None
        assert request.ticket.operation == operation
        assert request.ticket.target_id == request.params["cleanup_item_id"]
        assert request.timeout_s == timeout
        assert owned.connection.execute(
            "SELECT run_id,attempt_no FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (request.ticket.run_id, request.ticket.attempt_id)).fetchone() == (
            request.ticket.run_id, request.ticket.attempt_id)
