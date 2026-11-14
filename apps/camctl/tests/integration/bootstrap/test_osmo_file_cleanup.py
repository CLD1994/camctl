"""真实 OSMO 合同、清理装配和共享主机文件任务的组合验证。"""

from dataclasses import replace
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest
import pytest_asyncio
from decimal import Decimal

from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.host_files.tasks import FileTaskExecutor
from camctl.session.supervision import Supervisor
from camctl.contracts.workflow_errors import item_error_id
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers import runtime as drivers_runtime
from camctl.devices.drivers.adb_cameras.registration import register_builtin_camera_drivers
from camctl.devices.drivers.adb_cameras.file_commands import FILE_COMMAND_EVIDENCE
from camctl.devices.drivers.registry import DriverRegistry
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.recovery import RecoveryBoundary, recovery_registry
from camctl.outputs.cleanup_flow import (
    advance_cleanup, delete_source_file,
)
from camctl.outputs.cleanup_results import saved_cleanup_delete, saved_cleanup_attempt
from support.repository_ports import repository_port
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..devices.test_adb_camera_transport import LocalAdbChannel
from ..outputs.test_cleanup_delete_save_recovery import _FaultOutputs
from ..outputs.test_cleanup_query_save_recovery import _start_cleanup
from .test_output_binding_changes import _NOW, _accept
from ..support.capture import capture_database
from ..support.bootstrap_cleanup_sources import CONTENT, prepare_delivery, prepare_photo, prepare_recording
from ..support.outputs import output_test_session
from ..support.bootstrap_cleanup_cases import cleanup_case_factory, save_cleanup_intent

pytestmark = pytest.mark.asyncio
BINDING = DeviceBinding("cam-1", "dji-osmo360-ii")


class FileChannel(LocalAdbChannel):
    """只控制 ADB 传输交付；完整生产脚本仍由真实受管进程执行。"""

    def __init__(self):
        super().__init__()
        self.behaviors = []
        self.operations = []
        self.raw = []

    async def run(self, spec, stop):
        script = shlex.split(spec.argv[5])[2]
        operation = "delete" if 'rm -- "$target"' in script else "query"
        self.operations.append(operation)
        behavior = self.behaviors.pop(0) if self.behaviors else "normal"
        if behavior == "unreceived":
            # 未取得远端响应，既不伪造文件观察，也不执行额外命令。
            self.calls.append(spec)
            raw = RawToolOutcome(LocalExit(exit_code=1), b"", None, None, stderr=b"connection lost")
        else:
            raw = await super().run(spec, stop)
            if behavior == "lost_after":
                raw = replace(raw, output=b"")
            elif behavior == "error_after":
                raw = replace(raw, exit=LocalExit(exit_code=7), stderr=b"late channel error")
        self.raw.append(raw)
        return raw


@pytest_asyncio.fixture
async def world(tmp_path, request, capture_resources, isolated_driver_registries):
    """仅预置已登记源；清理受理、意图、结果及交接全部执行生产事务。"""
    binding = DeviceBinding("cam-1", getattr(request, "param", BINDING.driver_id))
    cfg = load_config({
        "paths": {"state_db": str(tmp_path / "state.db"), "log_file": str(tmp_path / "log"),
                  **{name: str(tmp_path / name) for name in ("staging", "ready", "processing")}},
        "devices": {
            "cam-1": {"kind": "camera", "driver": binding.driver_id, "adb": {"serial": "serial-1"},
                      "cleanup": {"delete_retry_interval_s": "0", "query_retry_interval_s": "0"}},
            "action6": {"kind": "camera", "driver": "dji-action6", "adb": {"serial": "never-called"}},
            "hb": {"kind": "camera", "driver": "hasselblad_cfv100c",
                "hasselblad": {"executable": "/usr/bin/hb_cli", "serial": "SERIAL-1",
                    "product_id": 10, "output_storage": "host"}},
            "hb-device": {"kind": "camera", "driver": "hasselblad_cfv100c",
                "hasselblad": {"executable": "/usr/bin/hb_cli", "serial": "SERIAL-2",
                    "product_id": 10, "output_storage": "device"}},
        }}, ConfigDefaults())
    from camctl.persistence.initialization import InitOutcome, initialize_state
    assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    tools = None
    supervisor = None
    try:
        action, original_path, preview_path, _record_driver, tools = await prepare_recording(
            cfg, owned, capture_resources, binding.driver_id, tmp_path)
        rows = owned.connection.execute(
            "SELECT id,kind FROM outputs WHERE source_action_id=? ORDER BY kind", (action,)).fetchall()
        ids = {"original": next(identity for identity, kind in rows if kind == 1),
               "preview": next(identity for identity, kind in rows if kind == 3),
               "repaired": next(identity for identity, kind in rows if kind == 2)}
        for role in ("preview", "repaired"):
            assert owned.connection.execute(
                "SELECT original_output_id FROM output_origins WHERE output_id=?", (ids[role],)).fetchone() == (ids["original"],)
        _, ids["host_original"], ids["host_preview"], _ = await prepare_photo(
            cfg, owned, capture_resources, device_id="hb", request_id=10)
        _, ids["hb_original"], ids["hb_preview"], hb_driver = await prepare_photo(
            cfg, owned, capture_resources, device_id="hb-device", request_id=11)
        delivery_id, delivery_file_id, copy = await prepare_delivery(
            cfg, owned, action, ids["original"], binding.driver_id)
        paths = {ids["original"]: original_path, ids["preview"]: preview_path}
        for role in ("host_original", "host_preview", "repaired"):
            relative, = owned.connection.execute(
                "SELECT f.relative_path FROM outputs o JOIN intermediate_files f"
                " ON f.id=o.intermediate_file_id WHERE o.id=?", (ids[role],)).fetchone()
            paths[ids[role]] = Path(cfg.paths.staging) / relative
        contents = {identity: path.read_bytes() for identity, path in paths.items()}
        register_builtin_camera_drivers(cfg)
        from camctl.devices.drivers.hasselblad.registration import register_builtin_hasselblad_driver
        register_builtin_hasselblad_driver(cfg)
        registry = drivers_runtime.current_registry()
        entry = registry.entry(binding.driver_id)
        channel = FileChannel()
        entry.driver.transport = channel
        supervisor = Supervisor()
        executor = FileTaskExecutor(supervisor)
        context = SimpleNamespace(database=await capture_database(owned), open_connection=lambda: open_existing(
            Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()))
        value = SimpleNamespace(cfg=cfg, owned=owned, registry=registry, entry=entry, channel=channel,
                                binding=binding, paths=paths, contents=contents, ids=ids, copy=copy,
                                delivery_id=delivery_id, delivery_file_id=delivery_file_id,
                                hb_driver=hb_driver, executor=executor, context=context, resources=capture_resources)
        with output_test_session():
            yield value
            assert not executor.unfinished_files()
    finally:
        try:
            if supervisor is not None:
                settled = await supervisor.drain_required()
                assert not settled.failed
                assert not executor.unfinished_files()
        finally:
            try:
                if tools is not None:
                    await tools.close()
            finally:
                try:
                    await capture_resources.close()
                finally:
                    owned.connection.close()


def _factory(world, **kwargs):
    return cleanup_case_factory(devices=world.cfg.devices, drivers=world.registry,
        max_delete_attempts=kwargs.pop("max_delete_attempts", 3),
        max_query_attempts=kwargs.pop("max_query_attempts", 2),
        staging=Path(world.cfg.paths.staging), file_executor=world.executor,
        occurred_at=lambda: _NOW, **kwargs)


@pytest.fixture
def driver_entry(request, isolated_driver_registries):
    """通过正式登记取得驱动声明，不建立清理业务资源。"""
    register_builtin_camera_drivers(load_config(None, ConfigDefaults()))
    driver_id = getattr(request, "param", BINDING.driver_id)
    return drivers_runtime.current_registry().entry(driver_id)


@pytest.mark.parametrize("operation", ["query", "delete"])
@pytest.mark.parametrize("driver_entry", ["dji-osmo360-ii", "dji-action6"], indirect=True)
async def test_registered_osmo_recovery_registry_preserves_operation_contract(driver_entry, operation):
    evidence = recovery_registry(driver_entry, operation)
    assert evidence is not None
    template = evidence.contract("adb_foreground_recovery", 1)
    assert template.operation == operation and template.fields == frozenset()
    assert template.identity_field is None
    assert evidence.contract(operation + "_returned", 1) is driver_entry.evidence.contract(operation + "_returned", 1)
    assert evidence.contract(operation + "_foreground_recovery", 1).fields == frozenset({"terminate_grace_s"})
    assert recovery_registry(driver_entry, "digest") is None
    assert driver_entry.declaration.file_query_supported and driver_entry.declaration.delete_supported
    assert not driver_entry.declaration.query_supported


@pytest.mark.parametrize("operation", ["query", "delete"])
@pytest.mark.parametrize("missing", ["authorization", "template", "body", "identity"])
async def test_recovery_registry_rejects_missing_or_invalid_registration(driver_entry, operation, missing):
    entry = driver_entry
    if missing == "authorization":
        entry = replace(entry, declaration=replace(entry.declaration, adb_foreground_recovery_operations=frozenset()))
    elif missing == "template":
        entry = replace(entry, evidence=EvidenceRegistry(FILE_COMMAND_EVIDENCE))
    else:
        entry = replace(entry, evidence=entry.evidence.with_contract(EvidenceContract(
            "adb_foreground_recovery", 1, operation, frozenset({"terminate_grace_s"})
            if missing == "body" else frozenset({"cleanup_item_id"}),
            identity_field=None if missing == "body" else "cleanup_item_id")))
    if missing in ("body", "identity"):
        with pytest.raises(ValueError, match="空正文"):
            recovery_registry(entry, operation)
    else:
        assert recovery_registry(entry, operation) is None


@pytest.mark.parametrize("role", ["original", "preview", "host_original", "host_preview", "repaired"])
async def test_exact_cleanup_keeps_osv_lrf_host_family_and_published_copy(world, role):
    output_id = world.ids[role]
    before = world.owned.connection.execute("SELECT * FROM deliveries").fetchall()
    copies = world.owned.connection.execute("SELECT * FROM file_copies WHERE delivery_id=?", (world.delivery_id,)).fetchall()
    assert len(copies) == 1
    _start_cleanup(world.owned, output_id, "2")
    flow = cleanup_flow(_factory(world))
    await flow(world.context)
    await flow.settle()
    for identity, path in world.paths.items():
        assert path.exists() is (identity != output_id)
        if identity != output_id:
            assert path.read_bytes() == world.contents[identity]
    assert world.copy.read_bytes() == CONTENT
    assert world.owned.connection.execute("SELECT * FROM deliveries").fetchall() == before
    assert world.owned.connection.execute("SELECT * FROM file_copies WHERE delivery_id=?", (world.delivery_id,)).fetchall() == copies
    assert world.owned.connection.execute("SELECT target_file_id FROM file_copies WHERE delivery_id=?", (world.delivery_id,)).fetchone() == (world.delivery_file_id,)
    assert world.owned.connection.execute("SELECT status,outcome FROM cleanup_items").fetchone() == (4, 1)
    assert world.channel.operations == (["delete"] if role in ("original", "preview") else [])
    assert world.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id IS NOT NULL").fetchone() == (1,)
    if role in ("host_original", "host_preview", "repaired"):
        assert world.owned.connection.execute("SELECT a.timeout_s_json,a.retry_interval_s_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id IS NOT NULL").fetchone() == (None, None)


@pytest.mark.parametrize("behaviors,expected,kept", [
    (["lost_after", "normal"], ["delete", "query"], False),
    (["unreceived", "normal", "normal"], ["delete", "query", "delete"], False),
    (["unreceived", "unreceived", "unreceived"], ["delete", "query", "query"], True),
    (["unreceived", "normal", "unreceived", "unreceived"], ["delete", "query", "delete", "query"], True),
])
async def test_unknown_delete_queries_original_target_with_independent_budget(world, behaviors, expected, kept):
    _, item_id = _start_cleanup(world.owned, world.ids["original"], "2")
    world.channel.behaviors = list(behaviors)
    factory = _factory(world)
    # 查询结果保存后开始新会话；先消费原事实，再使用同会话消费身份继续。
    for turn in range(8):
        if turn == 2:
            factory = _factory(world)
        await advance_cleanup(await factory(world.owned))
    assert world.channel.operations == expected
    assert world.paths[world.ids["original"]].exists() is kept
    assert world.paths[world.ids["preview"]].read_bytes() == b"source"
    rows = world.owned.connection.execute("SELECT kind,attempts_used FROM operation_runs WHERE cleanup_item_id=? ORDER BY id", (item_id,)).fetchall()
    assert rows == [(4, expected.count("delete")), (5, expected.count("query"))]
    state = world.owned.connection.execute("SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    assert state == ((5, item_error_id("cleanup_items", "file_query_attempts_exhausted")) if kept else (4, None))


async def test_reliable_absence_and_actual_channel_error_save_along_original_key(world):
    _, item_id = _start_cleanup(world.owned, world.ids["original"], "2")
    world.channel.behaviors = ["error_after"]
    factory = _factory(world)
    runtime = await factory(world.owned)
    port = _FaultOutputs("finish_cleanup_attempt", DbOutcomeKind.UNKNOWN, after_commit=True)
    with repository_port(OutputsRepository, port, ("finish_cleanup_attempt",),
                         matches=lambda _name, args, _kwargs: args[0].ticket.target_id == str(item_id)):
        assert (await delete_source_file(runtime, item_id)).phase == "finish_rejected"
    original = port.calls[0]
    assert world.channel.operations == ["delete"] and not world.paths[world.ids["original"]].exists()
    port.enabled = False
    fresh = world.context.open_connection()
    try:
        await world.resources.restart_database(fresh)
        resumed = await factory(fresh)
        with repository_port(OutputsRepository, port, ("finish_cleanup_attempt",),
                             matches=lambda _name, args, _kwargs: args[0].ticket.target_id == str(item_id)):
            await delete_source_file(resumed, item_id)
        assert port.calls[1] == original
        saved = saved_cleanup_delete(fresh.connection, item_id)
        assert saved.ticket == original[0].ticket
        assert saved.key == original[1]
        assert saved.actual.outcome == original[0].outcome.outcome
        assert saved.actual.outcome.error.code == "adb_file_delete_failed"
        assert saved.actual.outcome.error.details["local_exit_code"] == 7
        assert saved.actual.outcome.call_info.local_exit_code == 7
        assert saved.actual.outcome.settlement.basis is SettlementBasis.OBSERVED
        assert saved.actual.outcome.observations[0].data == {"cleanup_item_id": str(item_id), "present": False}
        assert world.channel.operations == ["delete"]
    finally:
        fresh.connection.close()


def _lost_intent(world, operation, *, output_id=None, item_id=None):
    if output_id is None:
        output_id = world.ids["original"]
    if item_id is None:
        _, item_id = _start_cleanup(world.owned, output_id, "2")
    local = output_id in {world.ids[role] for role in ("host_original", "host_preview", "repaired")}
    declaration = world.cfg.devices.get("cam-1", {}).get("cleanup", {})
    config = AttemptConfig(1) if local else AttemptConfig(
        3 if operation == "delete" else 2,
        Decimal(str(declaration.get(operation + "_timeout_s", "10"))),
        Decimal(str(declaration.get(operation + "_retry_interval_s", "0"))))
    ticket, horizon = save_cleanup_intent(world.owned, item_id, operation, config, _NOW)
    return item_id, ticket, horizon


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize("world", ["dji-osmo360-ii", "dji-action6"], indirect=True)
async def test_old_running_uses_registered_recovery_without_replaying_original_ticket(world, operation):
    item_id, ticket, horizon = _lost_intent(world, operation)
    fresh = world.context.open_connection()
    try:
        await world.resources.restart_database(fresh)
        recovered = await _factory(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon)(fresh)
        await advance_cleanup(recovered)
        row = fresh.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone()
        result = json.loads(row[1])
        assert row[0] == 4
        assert result["settlement"]["basis"] == "assumed"
        assert result["settlement"]["evidence"] == {"type": "adb_foreground_recovery", "version": 1, "data": {}}
        assert world.channel.operations == []
        # 旧 DELETE 需要查询；旧 QUERY 没有可能删除，后续可授新 DELETE。
        for _ in range(5):
            await advance_cleanup(recovered)
        assert world.channel.operations == (["query", "delete"] if operation == "delete" else ["delete"])
        assert fresh.connection.execute("SELECT COUNT(*) FROM operation_attempts WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone() == (1,)
        assert not world.paths[world.ids["original"]].exists() and world.paths[world.ids["preview"]].exists()
    finally:
        fresh.connection.close()


async def test_mixed_cleanup_routes_saved_device_host_and_unsupported_source_independently(world, monkeypatch):
    delivery = world.owned.connection.execute("SELECT * FROM deliveries").fetchall()
    copies = world.owned.connection.execute("SELECT * FROM file_copies WHERE delivery_id=?", (world.delivery_id,)).fetchall()
    hb = world.registry.entry("hasselblad_cfv100c")
    hb_deletes = []
    async def forbidden_delete(request):
        hb_deletes.append(request)
        raise AssertionError("不支持删除的设备不能接收删除请求")
    monkeypatch.setattr(hb.driver, "delete", forbidden_delete)
    action6 = world.registry.entry("dji-action6")
    world.registry = DriverRegistry((world.entry, action6, replace(hb,
        declaration=replace(hb.declaration, delete_supported=False))))
    selected = [world.ids[role] for role in ("original", "host_original", "hb_original")]
    await _accept(world.owned, "2", [{"name": "混合精确清理", "type": "delete_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00", "params": {"output_ids": [str(value) for value in selected]}}])
    factory = _factory(world)
    for _ in range(3):
        await advance_cleanup(await factory(world.owned))
    assert world.channel.operations == ["delete"] and hb_deletes == []
    assert not world.paths[world.ids["original"]].exists() and not world.paths[world.ids["host_original"]].exists()
    assert all(world.paths[world.ids[role]].read_bytes() == world.contents[world.ids[role]]
               for role in ("preview", "host_preview", "repaired"))
    failure = item_error_id("cleanup_items", "file_delete_failed")
    assert world.owned.connection.execute(
        "SELECT output_id,status,error_code FROM cleanup_items ORDER BY output_id").fetchall() == sorted([
            (world.ids["original"], 4, None), (world.ids["host_original"], 4, None),
            (world.ids["hb_original"], 5, failure), (world.ids["hb_preview"], 5, failure)])
    assert world.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id IS NOT NULL").fetchone() == (2,)
    assert world.owned.connection.execute("SELECT * FROM deliveries").fetchall() == delivery
    assert world.owned.connection.execute("SELECT * FROM file_copies WHERE delivery_id=?", (world.delivery_id,)).fetchall() == copies
    assert world.copy.read_bytes() == CONTENT


@pytest.mark.parametrize("verification", ["absent", "unknown"])
@pytest.mark.parametrize("world", ["dji-osmo360-ii", "dji-action6"], indirect=True)
async def test_old_delete_recovery_uses_original_query_budget_for_absence_or_unknown(world, verification):
    item_id, ticket, horizon = _lost_intent(world, "delete")
    if verification == "absent":
        world.paths[world.ids["original"]].unlink()
    else:
        world.channel.behaviors = ["unreceived", "unreceived"]
    fresh = world.context.open_connection()
    try:
        await world.resources.restart_database(fresh)
        factory = _factory(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon)
        await advance_cleanup(await factory(fresh))
        assert world.channel.operations == []
        recovered = saved_cleanup_attempt(fresh, ticket,
            database_id=fresh.metadata.instance_id, item_id=item_id)
        assert recovered.ticket == ticket
        assert recovered.actual.outcome.status is AttemptStatus.UNKNOWN
        assert recovered.actual.outcome.observations == ()
        for _ in range(5):
            await advance_cleanup(await factory(fresh))
        assert world.channel.operations == (["query"] if verification == "absent" else ["query", "query"])
        state = fresh.connection.execute("SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        assert state == ((4, None) if verification == "absent" else
            (5, item_error_id("cleanup_items", "file_query_attempts_exhausted")))
        assert world.paths[world.ids["preview"]].read_bytes() == b"source"
        assert fresh.connection.execute("SELECT attempts_used FROM operation_runs WHERE kind=4").fetchone() == (1,)
    finally:
        fresh.connection.close()
