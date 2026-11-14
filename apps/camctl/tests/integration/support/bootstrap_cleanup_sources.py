"""通过正式拍摄入口为清理场景准备同库来源。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.bootstrap.capture_assembly import execution_wait_config
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation, EvidenceContract
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.devices.tasks import StartReturn
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.scheduling import ObserveWindowRequest, SchedulingRepository, StartActionRequest

from ..bootstrap.test_media_assembly import _SessionDriver
from ..capture.result_consumer_fixtures import ResultCatalog
from ..capture.test_capture_contract import ResultsDouble, _entry
from .capture import capture_database
from .capture_media_cases import Results, ScriptTools
from .recording_consumer_cases import EVIDENCE, NOW

CONTENT = b"source"


class _SentRecording(_SessionDriver):
    async def control(self, request):
        self.calls.append(("control", request.operation))
        return DeviceCallResult((DeviceObservation("recording_sent", 1,
            {"activity_id": str(request.ticket.target_id)}),), None)


class _RecordingCatalog(ResultCatalog):
    def __init__(self, driver_id):
        self.driver_id_value = driver_id

    def driver_id(self, device_id):
        return self.driver_id_value if self.device_exists(device_id) else None

    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if definition is None or action_type != "camera_record":
            return definition
        factory = definition.task_factory
        return replace(definition, task_factory=lambda params: replace(factory(params),
            startup_margin_ms=10_000, start_return_meaning=StartReturn.SENT))


async def prepare_recording(cfg, owned, resources, driver_id, directory):
    """实际 START、结果保存与媒体处理形成录像源和修复文件。"""
    driver = _SentRecording(CONTENT)
    evidence = EVIDENCE.with_contract(EvidenceContract("recording_sent", 1, "control",
        frozenset({"activity_id"}), identity_field="activity_id"))
    registry = DriverRegistry((DriverEntry(driver_id, driver, DriverDeclaration(
        control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=True, digest_supported=True,
        delete_supported=False), evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    seconds = Decimal("200")
    streams = tuple(MediaStream(index, "video", "h264", 64, 64, seconds, False)
                    for index in range(2 if driver_id == "dji-osmo360-ii" else 1))
    tools = ScriptTools(MediaProbe(seconds, None, streams,
        format_name="mov,mp4,m4a,3gp,3g2,mj2"))
    probe, repair = tools.configure(Path(directory) / "media-tools")
    results = Results(ResultsDouble({}))
    wall, mono = [NOW - 75_000_000], [5_000_000_000]
    options = dict(driver=driver, evidence=evidence, devices=cfg.devices, drivers=registry,
        results=results, staging=Path(cfg.paths.staging), wait_config=execution_wait_config,
        wall_us=lambda: wall[0], monotonic_ns=lambda: mono[0], probe_request=probe,
        repair_request=repair)
    database = await capture_database(owned)
    factory = await resources.factory(database, **options)
    runtime = await factory(database)
    instant = datetime.fromtimestamp(wall[0] // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = await accept_input(ParsedInput("source.json", {"request_id": "1",
        "created_at": instant, "name": "录像来源", "actions": [{"name": "拍摄",
        "type": "camera_record", "device_id": "cam-1", "scheduled_at": instant,
        "params": {"type": "video"}, "policy": {"max_delay_ms": 5000}}]}),
        AcceptanceContext(CommandMode.RUN, _RecordingCatalog(driver_id), AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: wall[0])), new_operation_key(), database)
    assert accepted.plan_id is not None, accepted
    action_id, = owned.connection.execute("SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
    scheduling = SchedulingRepository()
    for method, request in ((scheduling.observe_window, ObserveWindowRequest(action_id, wall[0], wall[0])),
                            (scheduling.start_action, StartActionRequest(action_id, wall[0], wall[0]))):
        saved = method(request, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    await capture_handler("camera_record")(action_id, runtime)
    original_name = "clip 'one'\n." + ("MP4" if driver_id == "dji-action6" else "OSV")
    preview_name = "clip 'one'\n.LRF"
    original_path, preview_path = (Path(directory) / "device" / name for name in (original_name, preview_name))
    original_path.parent.mkdir(exist_ok=True)
    original_path.write_bytes(CONTENT)
    preview_path.write_bytes(CONTENT)
    original = replace(_entry(str(original_path), size=len(CONTENT)), locator={"path": str(original_path)},
        original_name=original_name, format_id=original_path.suffix[1:].lower())
    preview = replace(_entry(str(preview_path), size=len(CONTENT)), locator={"path": str(preview_path)},
        original_name=preview_name, format_id="lrf", paired_identity=original.identity)
    results.delegate = ResultsDouble({action_id: (original, preview)})
    await resources.restart_database(owned)
    database = await capture_database(owned)
    wall[0] = NOW
    factory = await resources.factory(database, **options)
    runtime = await factory(database)
    for _ in range(5):
        await capture_handler("camera_record")(action_id, runtime)
        if owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,):
            break
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
    assert "probe" in tools.calls and "repair" in tools.calls, tools.calls
    assert len([call for call in driver.calls if call[0] == "read"]) == 1
    return action_id, original_path, preview_path, driver, tools


async def prepare_photo(cfg, owned, resources, *, device_id, request_id):
    """使用照片正式装配形成同一设备活动的原图与预览。"""
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
    from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
    from camctl.devices.drivers.hasselblad.identity import CameraIdentity
    from camctl.host_files.models import BoundDirectories
    from ..capture.test_host_photo_flow import _PhotoDriver, _entry as photo_entry

    selected = {name: declaration for name, declaration in cfg.devices.items()
                if declaration["driver"] == DRIVER_ID}
    catalog = build_catalog(replace(cfg, devices=selected),
        DriverDefinitions({DRIVER_ID: driver_definition()}))
    database = await capture_database(owned)
    instant = datetime.fromtimestamp(NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = await accept_input(ParsedInput("photo.json", {"request_id": str(request_id),
        "created_at": instant, "name": "照片来源", "actions": [{"name": "拍摄",
        "type": "camera_take_photo", "device_id": device_id, "scheduled_at": instant,
        "params": {"type": "hasselblad_photo", "capture_format": "raw",
            "exposure": {"mode": "P", "iso": 100}, "white_balance": {"mode": "auto"},
            "focus": {"mode": "MF"}}, "policy": {"max_delay_ms": 5000}}]}),
        AcceptanceContext(CommandMode.RUN, catalog, AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: NOW)), new_operation_key(), database)
    assert accepted.plan_id is not None, accepted
    action_id, = owned.connection.execute("SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
    serial = cfg.devices[device_id]["hasselblad"]["serial"]
    scheduling = SchedulingRepository()
    for method, request in ((scheduling.observe_window, ObserveWindowRequest(action_id, NOW, NOW)),
        (scheduling.start_action, StartActionRequest(action_id, NOW, NOW,
            expected_identity=CameraIdentity(serial, 10)))):
        saved = method(request, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    driver = _PhotoDriver(Path(cfg.paths.staging))
    if serial != "SERIAL-1":
        run = driver.transport.run
        async def run_with_serial(spec, stop):
            actual = await run(spec, stop)
            return replace(actual, output=actual.output.replace(b"SERIAL-1", serial.encode()))
        driver.transport.run = run_with_serial
    driver.device_driver = HasselbladDriver(cfg.devices, BoundDirectories(staging=Path(cfg.paths.staging)),
        monotonic_ns=lambda: 1, transport=driver.transport)
    entry = photo_entry(driver)
    factory = await resources.factory(database, driver=driver, evidence=entry.evidence,
        devices=cfg.devices, drivers=DriverRegistry((entry,)), device_id=device_id,
        staging=Path(cfg.paths.staging), wait_config=lambda _: None,
        wall_us=lambda: NOW, monotonic_ns=lambda: 9_000_000_000)
    await capture_handler("camera_take_photo")(action_id, await factory(database))
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
    rows = owned.connection.execute("SELECT id,kind FROM outputs WHERE source_action_id=? ORDER BY kind", (action_id,)).fetchall()
    assert [row[1] for row in rows] == [1, 3], rows
    assert owned.connection.execute("SELECT original_output_id FROM output_origins WHERE output_id=?", (rows[1][0],)).fetchone() == (rows[0][0],)
    return action_id, rows[0][0], rows[1][0], driver


async def prepare_delivery(cfg, owned, action_id, output_id, driver_id):
    """真实取回、校验并发布同库原录像的副本。"""
    import hashlib
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    from camctl.outputs.handoff import DeliveryContext, DeliveryDirectories, DeliveryPhase, publish_delivery
    from camctl.outputs.obtain_flow import advance_obtain
    from camctl.persistence.repositories.outputs import OutputsRepository
    from .outputs import obtain_factory, output_drivers, prepare_obtain_selection
    from .public_delivery_cases import _Catalog, _ReadDriver

    action, _ = prepare_obtain_selection(owned, catalog=_Catalog(), now=NOW,
        source_id=action_id, mode=[output_id], request_id=20)
    staging, ready, processing = (Path(getattr(cfg.paths, name)) for name in ("staging", "ready", "processing"))
    for directory in (staging, ready, processing):
        directory.mkdir(parents=True, exist_ok=True)
    driver = _ReadDriver(CONTENT)
    evidence = EvidenceRegistry((
        EvidenceContract("read_returned", 1, "read", frozenset()),
        EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
    ))
    database = await capture_database(owned)
    factory = obtain_factory(devices={"cam-1": cfg.devices["cam-1"]},
        drivers=output_drivers(driver, evidence, driver_id=driver_id, read=True, digest=True),
        staging=staging, ready=ready, processing=processing,
        occurred_at=lambda: NOW + 1, monotonic_ns=lambda: 0, segment_size=len(CONTENT))
    await advance_obtain(factory(database))
    delivery_id, file_id, file_name, status, size, committed, source_hash, target_hash, dependency = owned.connection.execute(
        "SELECT d.id,c.target_file_id,d.file_name,d.status,c.source_size,c.committed_bytes,"
        " c.source_sha256,c.target_sha256,i.source_dependency FROM deliveries d"
        " JOIN file_copies c ON c.delivery_id=d.id JOIN obtain_items i ON i.delivery_id=d.id"
        " WHERE d.action_id=? AND d.output_id=?",
        (action, output_id)).fetchone()
    digest = hashlib.sha256(CONTENT).hexdigest()
    assert (status, size, committed, source_hash, target_hash, dependency) == (
        3, len(CONTENT), len(CONTENT), digest, digest, 0)
    assert len(driver.reads) == len(driver.digests) == 1
    published = await publish_delivery(delivery_id, DeliveryContext(OutputsRepository(), database,
        DeliveryDirectories(staging, ready, processing), NOW + 2))
    assert published.phase is DeliveryPhase.PUBLISHED, published
    copy = ready / file_name
    assert copy.read_bytes() == CONTENT
    return delivery_id, file_id, copy
