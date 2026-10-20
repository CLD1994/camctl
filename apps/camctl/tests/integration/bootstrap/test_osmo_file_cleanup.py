"""真实 OSMO 合同、清理装配和共享主机文件任务的组合验证。"""

from dataclasses import replace
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.contracts.workflow_errors import item_error_id
from camctl.devices import definitions_runtime
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers import runtime as drivers_runtime
from camctl.devices.drivers.adb_cameras.registration import register_builtin_camera_drivers
from camctl.devices.drivers.adb_cameras.file_commands import FILE_COMMAND_EVIDENCE
from camctl.devices.drivers.registry import DriverRegistry
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.models import AttemptStatus, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.recovery import RecoveryBoundary
from camctl.outputs.cleanup_flow import (
    CleanupQueryConsumer, _begin_cleanup_intent, advance_cleanup, delete_source_file,
)
from camctl.outputs.cleanup_results import saved_cleanup_delete, saved_cleanup_attempt
from camctl.outputs.work_files import WorkFileLimits
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.persistence.repositories.scheduling import register_window_guard
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..devices.test_adb_camera_transport import LocalAdbChannel
from ..outputs.test_qualification import (
    _seed_environment, _seed_plan, _seed_action, _seed_device_file, _seed_output,
)
from ..outputs.test_cleanup_delete_save_recovery import _FaultOutputs
from ..outputs.test_cleanup_query_save_recovery import _start_cleanup
from .test_output_binding_changes import _NOW, _accept

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


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    """仅预置已登记源；清理受理、意图、结果及交接全部执行生产事务。"""
    binding = DeviceBinding("cam-1", getattr(request, "param", BINDING.driver_id))
    monkeypatch.setattr(definitions_runtime, "_definitions", {})
    monkeypatch.setattr(drivers_runtime, "_entries", {})
    register_capture_guards()
    register_cancellation_guards()
    register_window_guard()
    cfg = load_config({
        "paths": {"state_db": str(tmp_path / "state.db"), "log_file": str(tmp_path / "log"),
                  **{name: str(tmp_path / name) for name in ("staging", "ready", "processing")}},
        "devices": {
            "cam-1": {"kind": "camera", "driver": binding.driver_id, "adb": {"serial": "serial-1"},
                      "cleanup": {"delete_retry_interval_s": "0", "query_retry_interval_s": "0"}},
            "action6": {"kind": "camera", "driver": "dji-action6", "adb": {"serial": "never-called"}},
            "hb-device": {"kind": "camera", "driver": "hasselblad_cfv100c",
                "hasselblad": {"executable": "/usr/bin/hb_cli", "serial": "never-called",
                    "output_storage": "device"}},
        }}, ConfigDefaults())
    _, owned = _seed_environment(tmp_path)
    connection = owned.connection
    _seed_plan(connection, 1)
    for action_id, device, driver_id in ((11, "cam-1", binding.driver_id),
                                       (12, "hb", "hasselblad_cfv100c"), (13, "hb-device", "hasselblad_cfv100c")):
        _seed_action(connection, action_id, 1, action_type=2, device_id=device, status=3)
        connection.execute("UPDATE actions SET driver_id=? WHERE id=?", (driver_id, action_id))
    paths = {}
    for output_id, name, action_id, file_binding in (
        (701, "clip 'one'\n.OSV", 11, binding), (702, "clip 'one'\n.LRF", 11, binding),
        (706, "hb-original.3FR", 13, DeviceBinding("hb-device", "hasselblad_cfv100c")),
    ):
        name = name.replace(".OSV", ".MP4") if file_binding.driver_id == "dji-action6" else name
        path = tmp_path / "device" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"source")
        paths[output_id] = path
        _seed_device_file(connection, output_id, action_id, size=6)
        connection.execute("UPDATE device_files SET identity_key=?,locator_json=?,original_name=? WHERE id=?",
            (json.dumps([file_binding.device_id, file_binding.driver_id, str(path)]), json.dumps({"path": str(path)}), name, output_id))
        _seed_output(connection, output_id, action_id, output_id, size=6)
    connection.execute("UPDATE device_files SET role=3,original_device_file_id=701,pairing_evidence_json='{}' WHERE id=702")
    connection.execute("UPDATE outputs SET kind=3 WHERE id=702")
    for output_id, purpose, relative, source, kind in (
        (703, 5, "capture-originals/703.3fr", 12, 1),
        (704, 6, "capture-previews/704.jpg", 12, 3),
        (705, 4, "derived/705.mp4", 11, 2),
    ):
        path = Path(cfg.paths.staging) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")
        paths[output_id] = path
        connection.execute("INSERT INTO intermediate_files (id,owner_action_id,purpose,relative_path,"
            " retention_state,cleanup_state,size_bytes,created_event_id,last_event_id,change_count)"
            " VALUES (?,?,?,?,3,1,6,1,1,1)", (output_id, source, purpose, relative))
        connection.execute("INSERT INTO outputs (id,source_action_id,kind,intermediate_file_id,availability,"
            " cleanup_status,media_json,created_event_id,last_event_id,change_count) VALUES (?,?,?,?,1,1,'{}',1,1,1)",
            (output_id, source, kind, output_id))
    connection.executemany("INSERT INTO output_origins (output_id,original_output_id) VALUES (?,?)",
                           ((702, 701), (704, 703), (705, 701)))
    _seed_action(connection, 14, 1, action_type=4, status=3)
    connection.execute("INSERT INTO deliveries (id,action_id,output_id,file_name,display_name,status,"
        " publication_intent_event_id,published_event_id,withdrawal_state,created_event_id,last_event_id,change_count)"
        " VALUES (801,14,701,'801.OSV','retained copy',5,1,1,1,1,1,1)")
    connection.commit()
    copy = Path(cfg.paths.ready) / "801.OSV"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"delivered-copy")
    register_builtin_camera_drivers(cfg)
    from camctl.devices.drivers.hasselblad.registration import register_builtin_hasselblad_driver
    register_builtin_hasselblad_driver(cfg)
    registry = drivers_runtime.current_registry()
    entry = registry.entry(binding.driver_id)
    channel = FileChannel()
    entry.driver.transport = channel
    work = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(batch_size=100, limit_per_run=100))
    context = SimpleNamespace(open_connection=lambda: open_existing(
        Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()))
    value = SimpleNamespace(cfg=cfg, owned=owned, registry=registry, entry=entry, channel=channel,
                            binding=binding, paths=paths, copy=copy, work=work, context=context)
    try:
        yield value
        assert not work.executor.unfinished_files()
    finally:
        owned.connection.close()


def _factory(world, **kwargs):
    return session_cleanup_assembly(devices=world.cfg.devices, drivers=world.registry,
        max_delete_attempts=kwargs.pop("max_delete_attempts", 3),
        max_query_attempts=kwargs.pop("max_query_attempts", 2),
        staging=Path(world.cfg.paths.staging), file_executor=world.work.executor,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0, **kwargs)


@pytest.mark.parametrize("operation", ["query", "delete"])
@pytest.mark.parametrize("world", ["dji-osmo360-ii", "dji-action6"], indirect=True)
async def test_registered_osmo_recovery_is_consumed_by_formal_assembly(world, operation):
    runtime = _factory(world)(world.owned)
    evidence = runtime.recovery_evidence_for(world.binding, operation)
    assert evidence is not None
    template = evidence.contract("adb_foreground_recovery", 1)
    assert template.operation == operation and template.fields == frozenset()
    assert template.identity_field is None
    assert evidence.contract(operation + "_returned", 1) is world.entry.evidence.contract(operation + "_returned", 1)
    assert evidence.contract(operation + "_foreground_recovery", 1).fields == frozenset({"terminate_grace_s"})
    assert runtime.recovery_evidence_for(world.binding, "digest") is None
    assert world.entry.declaration.file_query_supported and world.entry.declaration.delete_supported
    assert not world.entry.declaration.query_supported


@pytest.mark.parametrize("operation", ["query", "delete"])
@pytest.mark.parametrize("missing", ["authorization", "template", "body", "identity"])
async def test_formal_recovery_keeps_missing_or_invalid_registration_blocked(world, operation, missing):
    entry = world.entry
    if missing == "authorization":
        entry = replace(entry, declaration=replace(entry.declaration, adb_foreground_recovery_operations=frozenset()))
    elif missing == "template":
        entry = replace(entry, evidence=EvidenceRegistry(FILE_COMMAND_EVIDENCE))
    else:
        entry = replace(entry, evidence=entry.evidence.with_contract(EvidenceContract(
            "adb_foreground_recovery", 1, operation, frozenset({"terminate_grace_s"})
            if missing == "body" else frozenset({"cleanup_item_id"}),
            identity_field=None if missing == "body" else "cleanup_item_id")))
    world.registry = DriverRegistry((entry, world.registry.entry("dji-action6"),
                                     world.registry.entry("hasselblad_cfv100c")))
    runtime = _factory(world)(world.owned)
    if missing in ("body", "identity"):
        with pytest.raises(ValueError, match="空正文"):
            runtime.recovery_evidence_for(BINDING, operation)
    else:
        assert runtime.recovery_evidence_for(BINDING, operation) is None


@pytest.mark.parametrize("output_id", [701, 702, 703, 704, 705])
async def test_exact_cleanup_keeps_osv_lrf_host_family_and_published_copy(world, output_id):
    before = world.owned.connection.execute("SELECT * FROM deliveries").fetchall()
    _start_cleanup(world.owned, output_id, "2")
    flow = cleanup_flow(_factory(world))
    await flow(world.context)
    await flow.settle()
    for identity, path in world.paths.items():
        assert path.exists() is (identity != output_id)
        if identity != output_id:
            assert path.read_bytes() == b"source"
    assert world.copy.read_bytes() == b"delivered-copy"
    assert world.owned.connection.execute("SELECT * FROM deliveries").fetchall() == before
    assert world.owned.connection.execute("SELECT status,outcome FROM cleanup_items").fetchone() == (4, 1)
    assert world.channel.operations == (["delete"] if output_id in (701, 702) else [])
    assert world.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (1,)
    if output_id in (703, 704, 705):
        assert world.owned.connection.execute("SELECT timeout_s_json,retry_interval_s_json FROM operation_attempts").fetchone() == (None, None)


@pytest.mark.parametrize("behaviors,expected,kept", [
    (["lost_after", "normal"], ["delete", "query"], False),
    (["unreceived", "normal", "normal"], ["delete", "query", "delete"], False),
    (["unreceived", "unreceived", "unreceived"], ["delete", "query", "query"], True),
    (["unreceived", "normal", "unreceived", "unreceived"], ["delete", "query", "delete", "query"], True),
])
async def test_unknown_delete_queries_original_target_with_independent_budget(world, behaviors, expected, kept):
    _, item_id = _start_cleanup(world.owned, 701, "2")
    world.channel.behaviors = list(behaviors)
    factory = _factory(world)
    # 查询结果保存后开始新会话；先消费原事实，再使用同会话消费身份继续。
    for turn in range(8):
        if turn == 2:
            factory = _factory(world)
        await advance_cleanup(factory(world.owned))
    assert world.channel.operations == expected
    assert world.paths[701].exists() is kept
    assert world.paths[702].read_bytes() == b"source"
    rows = world.owned.connection.execute("SELECT kind,attempts_used FROM operation_runs WHERE cleanup_item_id=? ORDER BY id", (item_id,)).fetchall()
    assert rows == [(4, expected.count("delete")), (5, expected.count("query"))]
    state = world.owned.connection.execute("SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    assert state == ((5, item_error_id("cleanup_items", "file_query_attempts_exhausted")) if kept else (4, None))


async def test_reliable_absence_and_actual_channel_error_save_along_original_key(world):
    _, item_id = _start_cleanup(world.owned, 701, "2")
    world.channel.behaviors = ["error_after"]
    runtime = _factory(world)(world.owned)
    port = _FaultOutputs("finish_cleanup_attempt", DbOutcomeKind.UNKNOWN, after_commit=True)
    runtime.outputs = port
    assert (await delete_source_file(runtime, item_id)).phase == "finish_rejected"
    original = port.calls[0]
    assert world.channel.operations == ["delete"] and not world.paths[701].exists()
    assert len(runtime.pending_deletes) == 1
    port.enabled = False
    fresh = world.context.open_connection()
    try:
        resumed = _factory(world, pending_deletes=runtime.pending_deletes,
            pending_intents=runtime.pending_intents, pending_queries=runtime.pending_queries,
            pending_saves=runtime.pending_saves, pending_companions=runtime.pending_companions)(fresh)
        resumed.outputs = port
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


def _lost_intent(world, operation):
    _, item_id = _start_cleanup(world.owned, 701, "2")
    runtime = _factory(world)(world.owned)
    intent = AttemptIntent(operation=operation, action_id=world.owned.connection.execute(
        "SELECT action_id FROM cleanup_items WHERE id=?", (item_id,)).fetchone()[0],
        kind=OperationKind.DELETE_FILE if operation == "delete" else OperationKind.CHECK_FILE_EXISTS,
        target=AttemptTarget(cleanup_item_id=item_id), query_purpose=None,
        config=runtime.delete_config if operation == "delete" else runtime.query_config, occurred_at=_NOW)
    # 使用对应设备配置，只有已保存的原票据可进入恢复。
    runtime = runtime.for_operation(item_id, operation)
    intent = replace(intent, config=runtime.delete_config if operation == "delete" else runtime.query_config)
    begun = _begin_cleanup_intent(runtime, intent,
        None if operation == "delete" else CleanupQueryConsumer.BEFORE_DELETE)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    ticket = begun.value.ticket
    horizon = world.owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    runtime.pending_intents.clear()
    return item_id, ticket, horizon


@pytest.mark.parametrize("operation", ["delete", "query"])
@pytest.mark.parametrize("world", ["dji-osmo360-ii", "dji-action6"], indirect=True)
async def test_old_running_uses_registered_recovery_without_replaying_original_ticket(world, operation):
    item_id, ticket, horizon = _lost_intent(world, operation)
    fresh = world.context.open_connection()
    try:
        recovered = _factory(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
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
        assert not world.paths[701].exists() and world.paths[702].exists()
    finally:
        fresh.connection.close()


async def test_mixed_cleanup_routes_saved_device_host_and_unsupported_source_independently(world):
    delivery = world.owned.connection.execute("SELECT * FROM deliveries").fetchall()
    _accept(world.owned, "2", [{"name": "混合精确清理", "type": "delete_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00", "params": {"output_ids": ["701", "703", "706"]}}])
    factory = _factory(world)
    for _ in range(3):
        await advance_cleanup(factory(world.owned))
    assert world.channel.operations == ["delete"]
    assert not world.paths[701].exists() and not world.paths[703].exists()
    assert world.paths[702].exists() and world.paths[704].exists() and world.paths[705].exists()
    assert world.paths[706].read_bytes() == b"source"
    assert world.owned.connection.execute(
        "SELECT output_id,status,error_code FROM cleanup_items ORDER BY output_id").fetchall() == [
            (701, 4, None), (703, 4, None), (706, 5, item_error_id("cleanup_items", "file_delete_failed"))]
    assert world.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (2,)
    assert world.owned.connection.execute("SELECT * FROM deliveries").fetchall() == delivery
    assert world.copy.read_bytes() == b"delivered-copy"


@pytest.mark.parametrize("verification", ["absent", "unknown"])
@pytest.mark.parametrize("world", ["dji-osmo360-ii", "dji-action6"], indirect=True)
async def test_old_delete_recovery_uses_original_query_budget_for_absence_or_unknown(world, verification):
    item_id, ticket, horizon = _lost_intent(world, "delete")
    if verification == "absent":
        world.paths[701].unlink()
    else:
        world.channel.behaviors = ["unreceived", "unreceived"]
    fresh = world.context.open_connection()
    try:
        factory = _factory(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=horizon)
        await advance_cleanup(factory(fresh))
        assert world.channel.operations == []
        recovered = saved_cleanup_attempt(fresh, ticket,
            database_id=fresh.metadata.instance_id, item_id=item_id)
        assert recovered.ticket == ticket
        assert recovered.actual.outcome.status is AttemptStatus.UNKNOWN
        assert recovered.actual.outcome.observations == ()
        for _ in range(5):
            await advance_cleanup(factory(fresh))
        assert world.channel.operations == (["query"] if verification == "absent" else ["query", "query"])
        state = fresh.connection.execute("SELECT status,error_code FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        assert state == ((4, None) if verification == "absent" else
            (5, item_error_id("cleanup_items", "file_query_attempts_exhausted")))
        assert world.paths[702].read_bytes() == b"source"
        assert fresh.connection.execute("SELECT attempts_used FROM operation_runs WHERE kind=4").fetchone() == (1,)
    finally:
        fresh.connection.close()
