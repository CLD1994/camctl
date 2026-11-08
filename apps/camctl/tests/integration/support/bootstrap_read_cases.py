"""读取与文件收场专项集中维护正式装配和真实数据库重开。"""

from dataclasses import replace
from pathlib import Path
from decimal import Decimal
import hashlib
import os

from camctl.bootstrap.obtain_assembly import session_obtain_assembly
from camctl.devices.drivers.registry import DriverRegistry
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .bootstrap_consumers import ConsumerWorld


class ObservedReadStream:
    """源端口替身记录真实关闭，交由生产 ReadSession 执行收场。"""

    def __init__(self, stream):
        self.stream, self.closed = stream, False

    def read(self, limit):
        return self.stream.read(limit)

    def cancel(self):
        stop = getattr(self.stream, "cancel", None) or self.stream.stop
        return stop()

    def close(self):
        self.stream.close()
        self.closed = True


def assert_read_ends(streams, sessions):
    assert streams and len(streams) == len(sessions)
    assert all(stream.closed for stream in streams)
    ends = [session.poll_stopped() for session in sessions]
    assert all(end is not None and end.stopped for end in ends)
    return ends


class InspectionCase(ConsumerWorld):
    """检查副本由正式拍摄工厂创建，READ及工具结果经真实端口取得。"""

    def __init__(self, source, resources, *, digest=None, digest_supported=True):
        from ..bootstrap.test_media_assembly import _EVIDENCE, _tool
        from .segment_inspection_cases import CONTENT as _CONTENT
        from camctl.host_files.models import BoundDirectories
        from camctl.capture.segment_inspections import SegmentInspectionRequest
        from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult
        from camctl.host_files.media import ProbeRequest
        from camctl.outputs.qualification import OperationConfig

        owned, source_runtime, ref, members = source
        roots = BoundDirectories(Path(owned.connection.execute("PRAGMA database_list").fetchone()[2]).parent / "staging")
        self.ref, self.member = ref, members[0]
        super().__init__((owned, roots, self.member.file_id), resources, duration="1.5", evidence=_EVIDENCE)
        self.wall[0] = source_runtime.wall_us() + 10
        self.digests = []

        async def digest_call(request):
            self.digests.append(request)
            value = (await digest.read_digest()).digest if digest is not None else hashlib.sha256(_CONTENT).hexdigest()
            return DeviceCallResult((DeviceObservation("file_digest", 1,
                {"file_id": request.params["file_id"], "sha256": value}),), None)

        self.driver.digest = digest_call
        self.entry = replace(self.entry,
            declaration=replace(self.entry.declaration, digest_supported=digest_supported))
        body = "from pathlib import Path\nimport json,sys\n"
        body += "assert '-count_frames' not in sys.argv\n"
        body += f"assert Path(sys.argv[-1]).read_bytes() == {bytes(_CONTENT)!r}\n"
        body += f"with Path({str(self.tool_log)!r}).open('a') as log: log.write('probe\\n')\n"
        body += "print(json.dumps({'format':{'duration':'1.5'},'streams':[{'index':0,'codec_type':'video','codec_name':'h264','width':1920,'height':1080,'duration':'1.5','nb_frames':'45','avg_frame_rate':'30/1','disposition':{'attached_pic':0}}]}))\n"
        self.probe_request = ProbeRequest(ffprobe=_tool(self.path.parent, "inspection-probe", body))
        self.request = SegmentInspectionRequest(ref.action_id, self.member.member_id,
            CoverageTarget(CoverageMetric.DURATION_S, 60),
            OperationConfig(3, Decimal("10"), Decimal("0")), self.wall[0])
        self.factory = self.new_factory()

    def new_factory(self, *, restricted=False, drivers=None):
        from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly

        return session_capture_assembly(
            devices=self.config.devices, drivers=DriverRegistry((self.entry,)) if drivers is None else drivers,
            results=self.results, staging=Path(self.config.paths.staging),
            wait_config=execution_wait_config, wall_us=lambda: self.wall[0],
            monotonic_ns=lambda: 7_000_000_000, probe_request=self.probe_request,
            repair_request=self.repair_request, media_enabled=not restricted,
            segment_size=4, file_executor=self.executor, **self.collections)

    async def inspect(self):
        from camctl.capture.segment_inspections import inspect_segment
        return await inspect_segment(self.runtime.media, self.request)

    async def rebuild(self):
        self.runtime = await self.factory(self.database, "cam-1")

    def held_ticket(self):
        assert len(self.reader.requests) == 1
        ticket = self.reader.requests[0][0]
        ends = [end for actual, end in self.reader.ends if actual == ticket]
        assert ends and all(end.stopped and end.error is None for end in ends)
        return ticket


async def inspection_case(source, resources, **options):
    return await InspectionCase(source, resources, **options).start()


async def held_inspection_case(source, resources, monkeypatch, digest_state="required"):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository
    from .bootstrap_consumers import TransactionFault

    world = await inspection_case(source, resources, digest_supported=digest_state != "unsupported")
    world.completions = []
    if digest_state == "saved":
        actual_save = OutputsRepository.save_prepared

        def save(repository, request, key, owned):
            if world.completions:
                return actual_save(repository, request, key, owned)
            world.completions.append(request)
            fault = TransactionFault(owned.connection, after_commit=True)
            outcome = actual_save(repository, request, key, replace(owned, connection=fault))
            assert outcome.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            return outcome

        with monkeypatch.context() as fault:
            fault.setattr(OutputsRepository, "save_prepared", save)
            try:
                await world.inspect()
            except ConsistencyError:
                pass
    else:
        actual_open = os.open

        def open_file(path, flags, *args, **kwargs):
            if isinstance(path, (str, Path)) and flags & os.O_ACCMODE == os.O_RDONLY:
                selected = Path(path)
                if selected.is_relative_to(world.roots.staging) and selected.is_file():
                    world.completions.append(selected)
                    raise OSError("检查副本摘要暂不可读")
            return actual_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(os, "open", open_file)
            try:
                await world.inspect()
            except ConsistencyError:
                pass
    ticket = world.held_ticket()
    assert world.owned.connection.execute(
        "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
    assert world.tools() == [] and len(world.completions) == 1
    world.prime_clock()
    return world, ticket


class ObtainRecoveryCase:
    """同一正式工厂持有原申请，实际结束worker后再恢复连接。"""

    async def advance(self):
        await advance_obtain(self.runtime)

    async def reopen(self):
        self.database = await self.resources.restart_database(self.path)
        self.owned.connection.close()
        self.owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
        self.runtime = self.factory(self.database)


async def obtain_recovery_case(environment, resources, change, *, boundary, horizon,
                               declared=True, evidence=True):
    from ..bootstrap.test_output_binding_changes import _NOW, _changed, _read_registry

    cfg, owned, context, driver = environment
    # 原准备会话持有真实worker；它必须先结束，恢复工厂再打开新worker。
    await context.database.close()
    world = ObtainRecoveryCase()
    world.path, world.owned, world.resources = Path(cfg.paths.state_db), owned, resources
    world.database = await resources.database(world.path)
    current = _changed(cfg, change)
    entries = []
    for identity in ("camctl-adb", "alternate-camera"):
        entry = _read_registry(driver).entry(identity)
        if identity == "camctl-adb":
            entry = replace(entry,
                declaration=replace(entry.declaration,
                    adb_foreground_recovery_operations=frozenset({"read"}) if declared else frozenset()),
                evidence=EvidenceRegistry((EvidenceContract(
                    "adb_foreground_recovery", 1, "read", frozenset()),)) if evidence else EvidenceRegistry(()))
        entries.append(entry)
    world.diagnostics = []
    world.factory = session_obtain_assembly(
        devices=current.devices, drivers=DriverRegistry(tuple(entries)),
        staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready),
        processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0,
        recovery_boundary=boundary, recovery_max_event_id=lambda: horizon,
        on_recovery_diagnostic=world.diagnostics.append)
    world.runtime = world.factory(world.database)
    return world


async def work_file_case(directory, resources, count=2):
    """真实受理和READ为每个独立取回建立一份仍受保护的交付副本。"""
    from types import SimpleNamespace
    from camctl.bootstrap.capture_assembly import session_capture_assembly
    from camctl.bootstrap.flows import capture_flow
    from camctl.bootstrap.obtain_assembly import obtain_flow
    from camctl.capture.handlers import ObservedFile
    from camctl.capture.results import FileKind
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.devices.drivers.registry import DriverEntry, DriverStatus
    from camctl.devices.ports import DriverDeclaration
    from camctl.persistence.initialization import initialize_state, InitOutcome
    from .session import accept_plan, session_config
    from ..bootstrap.test_output_binding_changes import Catalog, Driver, _CONTENT, _NOW, _BrokenStream, _EVIDENCE
    from ..capture.test_capture_contract import ResultsDouble
    from camctl.devices.read_session import ReadSession

    class WorkCatalog(Catalog):
        def driver_id(self, device_id):
            return "work-file-source"

        def device_exists(self, device_id):
            return device_id in {f"cam-{index}" for index in range(count)}

    class Reader(Driver):
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            self.reads.append((ticket, offset))
            stream = ObservedReadStream(_BrokenStream(_CONTENT[offset:]))
            session = ReadSession(source, offset, stream, idle_timeout_s)
            world.streams.append(stream)
            world.sessions.append(session)
            return session

    world = SimpleNamespace()
    world.streams, world.sessions = [], []
    world.cfg = session_config(directory, devices={f"cam-{index}": {"kind": "camera", "driver": "work-file-source",
        "copy": {"retry_interval_s": Decimal("3")}} for index in range(count)})
    world.path = Path(world.cfg.paths.state_db)
    assert initialize_state(world.cfg, world.path).outcome is InitOutcome.CREATED
    world.owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    world.database = await resources.database(world.path)
    world.catalog, world.now, world.driver = WorkCatalog(), _NOW, Reader(world.owned)
    entry = DriverEntry("work-file-source", world.driver, DriverDeclaration(
        control_supported=True, stop_supported=False, query_supported=False, result_supported=False,
        read_supported=True, digest_supported=False, delete_supported=False),
        _EVIDENCE,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    registry = DriverRegistry((entry,))
    context = SimpleNamespace(database=world.database, clock=SimpleNamespace(utc_micros=lambda: _NOW))
    await accept_plan(world.database, directory, world.catalog, {
        "request_id": "1", "created_at": "2026-01-15 08:00:00", "name": "文件预算来源",
        "actions": [{"name": f"照片-{index}", "type": "camera_take_photo", "device_id": f"cam-{index}",
            "scheduled_at": "2026-01-15 09:00:00", "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 1000}} for index in range(count)]}, context.clock)
    results = ResultsDouble({index+1: (ObservedFile(identity=f"work-source-{index}", locator={"path": f"/DCIM/work-{index}.jpg"},
        evidence={"listing": "work"}, complete=True, size_bytes=len(_CONTENT), kind=FileKind.PHOTO,
        original_name=f"work-{index}.jpg", media_type="image/jpeg"),) for index in range(count)})
    capture = capture_flow(session_capture_assembly(
        devices=world.cfg.devices, drivers=registry, results=results, staging=Path(world.cfg.paths.staging),
        wall_us=lambda: _NOW, monotonic_ns=lambda: 0,
        wait_config=lambda _: CaptureWaitConfig(target_duration_ms=1000, driver_margin_ms=0)))
    await capture(context)
    await capture.settle()
    assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
    # 每个调用只有一个新取回，旧流程的重试间隔保持原实际尝试。
    obtain = session_obtain_assembly(devices=world.cfg.devices, drivers=registry,
        staging=Path(world.cfg.paths.staging), ready=Path(world.cfg.paths.ready),
        processing=Path(world.cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    for request_id in range(2, count + 2):
        await accept_plan(world.database, directory, world.catalog, {
            "request_id": str(request_id), "created_at": "2026-01-15 08:00:00", "name": "独立文件消费者",
            "actions": [{"name": "取回", "type": "obtain_action_outputs", "scheduled_at": "2026-01-15 09:00:00",
                "params": {"source": {"action_instance_id": str(request_id-1)}}}]}, context.clock)
        await obtain_flow(obtain)(context)
    world.files = world.owned.connection.execute(
        "SELECT c.target_file_id,d.action_id,c.id FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id ORDER BY c.target_file_id").fetchall()
    world.paths = {identity: Path(world.cfg.paths.staging) / relative for identity, relative in world.owned.connection.execute("SELECT id,relative_path FROM intermediate_files")}
    assert len(world.files) == count
    assert world.owned.connection.execute("SELECT committed_bytes FROM file_copies ORDER BY id").fetchall() == [(4,)] * count
    ends = assert_read_ends(world.streams, world.sessions)
    assert len(ends) == count and all(end.bytes_read == 4 and end.error is not None for end in ends)
    await world.database.close()
    world.idle_entry = replace(entry, declaration=replace(entry.declaration,
        control_supported=False, read_supported=False))
    return world


def release_work_consumer(world, position, owned=None, *, release=True):
    """在明确的存储提交边界结束原取回责任，并保存其文件保留解除。"""
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.work_files import RetentionRelease
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository, SettleCanceledObtain
    from .outputs import prepare_cancel_target

    owned = world.owned if owned is None else owned
    file_id, action_id, _copy_id = world.files[position]
    prepare_cancel_target(owned, catalog=world.catalog, now=world.now, action_id=action_id,
        request_id=str(100 + position))
    members = tuple(row[0] for row in owned.connection.execute(
        "SELECT i.id FROM obtain_items i JOIN obtain_source_selections s ON s.id=i.selection_id"
        " JOIN action_dependencies d ON d.id=s.dependency_id WHERE d.action_id=? ORDER BY i.id", (action_id,)))
    repository = OutputsRepository()
    settled = repository.settle_canceled_obtain(SettleCanceledObtain(action_id, world.now, members),
        new_operation_key(), owned)
    assert settled.kind is DbOutcomeKind.COMPLETED and settled.value.complete, settled.error
    from camctl.cancellation.models import RecordCancelResult, CancelOutcomeChoice, FinishCancelAction
    from camctl.persistence.repositories.cancellation import CancellationRepository
    item_id, origin = owned.connection.execute("SELECT id,action_id FROM cancel_items WHERE target_action_id=? ORDER BY id DESC LIMIT 1", (action_id,)).fetchone()
    cancellations = CancellationRepository()
    recorded = cancellations.record_cancel_result(RecordCancelResult(item_id, world.now, outcome=CancelOutcomeChoice.CANCELED), new_operation_key(), owned)
    assert recorded.kind is DbOutcomeKind.COMPLETED, recorded.error
    finished = cancellations.finish_cancel_action(FinishCancelAction(origin, world.now), new_operation_key(), owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    if not release:
        return settled
    released = repository.save_retention_release(RetentionRelease(file_id, world.now), new_operation_key(), owned)
    assert released.kind is DbOutcomeKind.COMPLETED, released.error
    return released


async def default_work_session(world, monkeypatch, *, limit, batch=32, on_initialized=None, on_cleaned=None):
    """完整默认会话只在公开读取/保存边界安排后来获得的文件资格。"""
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
    from camctl.devices.definitions_runtime import reset_driver_definitions
    from camctl.devices.drivers.runtime import register_drivers, reset_drivers
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.models import DbOutcomeKind
    from camctl.contracts.values import new_operation_key

    cfg = replace(world.cfg, cleanup=replace(world.cfg.cleanup,
        work_file_batch_size=batch, work_file_limit_per_run=limit))
    actual_max, actual_read = OutputsRepository.max_cleanup_candidate_id, BusinessDatabase.read
    actual_save = OutputsRepository.save_cleanup_result
    state = {"range_read": False, "initialized": False, "ceilings": [], "cleaned": []}

    def maximum(repository, owned):
        result = actual_max(repository, owned)
        state["range_read"] = True
        state["ceilings"].append(result)
        return result

    async def read(database, operation, **options):
        value = await actual_read(database, operation, **options)
        if state["range_read"] and not state["initialized"]:
            state["initialized"] = True
            if on_initialized is not None:
                import inspect
                if inspect.iscoroutinefunction(on_initialized):
                    await on_initialized(database)
                else:
                    await database.write(on_initialized, key=new_operation_key())
        return value

    def save(repository, request, key, owned):
        result = actual_save(repository, request, key, owned)
        if result.kind is DbOutcomeKind.COMPLETED:
            state["cleaned"].append(request.file_id)
            if on_cleaned is not None:
                on_cleaned(request.file_id, owned)
        return result

    reset_drivers()
    reset_driver_definitions()
    register_drivers(world.idle_entry)
    try:
        with monkeypatch.context() as observe:
            observe.setattr(OutputsRepository, "max_cleanup_candidate_id", maximum)
            observe.setattr(BusinessDatabase, "read", read)
            observe.setattr(OutputsRepository, "save_cleanup_result", save)
            deps = build_runtime(CommandMode.RUN, cfg, catalog=world.catalog)
            try:
                import asyncio
                outcome = await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.001), 20)
                assert outcome.succeeded, outcome
            finally:
                close_runtime(deps)
    finally:
        reset_drivers()
        reset_driver_definitions()
    return state


class SegmentInputCase:
    """真实START/STOP、结果页、分段READ与媒体工具形成来源检查副本。"""

    async def close(self):
        import sys
        original = sys.exception()
        result = await self.executor.drain_required()
        self.owned.connection.close()
        failures = [item.exception or RuntimeError(item.error) for item in result.failed]
        if failures:
            raise BaseExceptionGroup("分段输入实际文件责任收场失败", ([original] if original is not None else []) + failures)
        self.read_ends = assert_read_ends(self.reader.streams, self.reader.sessions)

    async def publish(self, database=None):
        from camctl.capture.handlers import capture_handler
        self.database = self.database if database is None else database
        runtime = await self.factory(self.database, "cam-1")
        await capture_handler("camera_record")(self.source_id, runtime)
        assert self.owned.connection.execute("SELECT status FROM actions WHERE id=?",
            (self.source_id,)).fetchone() == (self.expected_source_status,)
        assert self.owned.connection.execute(
            "SELECT published_event_id IS NOT NULL FROM segment_sets WHERE action_id=?",
            (self.source_id,)).fetchone() == (1,)
        self.inputs = self.owned.connection.execute(
            "SELECT f.id,f.relative_path,s.state FROM intermediate_files f"
            " JOIN file_copies c ON c.target_file_id=f.id"
            " JOIN segment_inspections s ON s.id=c.inspection_id WHERE f.owner_action_id=? ORDER BY f.id",
            (self.source_id,)).fetchall()
        assert self.inputs, "异常普通录像未形成分段检查副本"
        self.paths = {identity: Path(self.cfg.paths.staging) / relative for identity, relative, _state in self.inputs}
        return self.inputs

    async def advance_obtains(self, rounds=4):
        for _ in range(rounds):
            await advance_obtain(self.obtain_factory(self.database))

    async def cancel(self, action_id, *, settle=True):
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from camctl.bootstrap.flows import cancel_flow
        from .session import accept_plan
        from ..outputs.test_segment_default_scope import ObtainCatalog

        instant = datetime.fromtimestamp(self.wall[0] // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await accept_plan(self.database, self.path.parent, ObtainCatalog(), {
            "request_id": str(100 + action_id), "created_at": instant, "name": "取消文件消费者",
            "actions": [{"name": "取消", "type": "cancel_task",
                "params": {"target": {"action_instance_id": str(action_id)}}}]},
            SimpleNamespace(utc_micros=lambda: self.wall[0]))
        await cancel_flow(staging=Path(self.cfg.paths.staging),
            ready=Path(self.cfg.paths.ready), processing=Path(self.cfg.paths.processing))(
            SimpleNamespace(database=self.database, clock=SimpleNamespace(utc_micros=lambda: self.wall[0])))
        if not settle:
            return
        from camctl.persistence.repositories.outputs import OutputsRepository, SettleCanceledObtain
        from camctl.persistence.models import DbOutcomeKind
        from camctl.contracts.values import new_operation_key
        members = tuple(row[0] for row in self.owned.connection.execute("SELECT i.id FROM obtain_items i JOIN obtain_source_selections s ON s.id=i.selection_id JOIN action_dependencies d ON d.id=s.dependency_id WHERE d.action_id=? ORDER BY i.id", (action_id,)))
        request = SettleCanceledObtain(action_id, self.wall[0], members)
        key = new_operation_key()
        outcome = await self.database.write(lambda owned: OutputsRepository().settle_canceled_obtain(request, key, owned), key=key)
        assert outcome.kind is DbOutcomeKind.COMPLETED and outcome.value.complete, outcome.error
        self.cancellations.append((request, key))

    async def add_waiter(self, request_id, mode="default", *, start_only=False):
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from camctl.bootstrap.obtain_assembly import obtain_flow
        from .session import accept_plan
        from ..outputs.test_segment_default_scope import ObtainCatalog

        instant = datetime.fromtimestamp(self.wall[0] // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        params = {"source": {"action_instance_id": str(self.source_id)}}
        if mode == "preview":
            params["filter"] = "preview"
        accepted = await accept_plan(self.database, self.path.parent, ObtainCatalog(), {
            "request_id": str(request_id), "created_at": instant, "name": "等待分段来源",
            "actions": [{"name": "取回", "type": "obtain_action_outputs", "scheduled_at": instant,
                "params": params}]}, SimpleNamespace(utc_micros=lambda: self.wall[0]))
        if start_only:
            from camctl.contracts.values import new_operation_key
            from camctl.outputs.sources import read_source_spec
            from camctl.persistence.repositories.outputs import OutputsRepository, StartObtainAction, ResolveSources
            from camctl.persistence.models import DbOutcomeKind
            identity, fields = self.owned.connection.execute("SELECT id,input_fields_json FROM actions WHERE plan_id=? AND type=4", (accepted.plan_id,)).fetchone()
            repository = OutputsRepository()
            for method, request in ((repository.start_obtain_action, StartObtainAction(identity, self.wall[0])), (repository.resolve_sources, ResolveSources(identity, read_source_spec(fields), self.wall[0]))):
                key = new_operation_key()
                receipt = await self.database.write(lambda owned, method=method, request=request, key=key: method(request, key, owned), key=key)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        else:
            await obtain_flow(self.obtain_factory)(SimpleNamespace(database=self.database,
                clock=SimpleNamespace(utc_micros=lambda: self.wall[0])))
        row = self.owned.connection.execute(
            "SELECT a.id,s.id FROM actions a JOIN action_dependencies d ON d.action_id=a.id"
            " JOIN obtain_source_selections s ON s.dependency_id=d.id WHERE a.plan_id=? AND a.type=4",
            (accepted.plan_id,)).fetchone()
        assert row is not None
        return row


async def segment_input_case(directory, resources, monkeypatch, values, *, mode="default", partial=False,
                             early_waiters=(20, 21), read_retry_s=Decimal("0"),
                             segment_size=4, result_check=None, expected_source_status=3,
                             source_target_s=Decimal("60"), obtain_max_read_attempts=1,
                             obtain_drivers=None, driver_id="segment-file-source", result_entries=None):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
    from camctl.capture.handlers import capture_handler
    from camctl.contracts.values import new_operation_key
    from camctl.devices.drivers.registry import DriverEntry, DriverStatus
    from camctl.devices.ports import DriverDeclaration
    from camctl.host_files.media import ProbeRequest
    from camctl.persistence.initialization import initialize_state, InitOutcome
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.scheduling import SchedulingRepository, ObserveWindowRequest, StartActionRequest, register_window_guard
    from .session import accept_plan, session_config
    from ..bootstrap.test_media_assembly import _SessionDriver, _EVIDENCE, _tool
    from ..capture.test_action6_segmented_normal import Action6RecordCatalog
    from ..capture.test_action6_segmented_recovery import _entries
    from ..capture.test_capture_contract import _NOW
    from ..capture.test_input_copy import _CONTENT
    from ..capture.test_record_file_completion_wait import _completed_result, _stop_context, _REGISTRY
    evidence = _EVIDENCE.with_contract(_REGISTRY.contract("results_returned", 1))
    from .segment_inspection_cases import Stream
    from camctl.devices.read_session import ReadSession

    class Catalog(Action6RecordCatalog):
        def driver_id(self, device_id):
            return driver_id

    world = SegmentInputCase()
    world.expected_source_status = expected_source_status
    world.wall, world.mono, world.source_id = [_NOW], [5_000_000_000], 1
    world.catalog = Catalog()
    world.catalog.duration_s = source_target_s
    world.cfg = session_config(directory, devices={"cam-1": {
        "kind": "camera", "driver": driver_id,
        "recording": {"repair_margin_s": Decimal("2")},
        "copy": {"max_read_attempts": 1 if partial else 3, "retry_interval_s": read_retry_s},
        **({} if result_check is None else {"result_check": result_check})}})
    world.path = Path(world.cfg.paths.state_db)
    assert initialize_state(world.cfg, world.path).outcome is InitOutcome.CREATED
    world.owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    world.database = await resources.database(world.path)
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor
    world.executor = FileTaskExecutor(Supervisor())
    world.cancellations = []
    world.reader = SimpleNamespace(streams=[], sessions=[])

    class Driver(_SessionDriver):
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            stream = ObservedReadStream(Stream(_CONTENT[offset:], fail_after=4 if partial else None))
            session = ReadSession(source, offset, stream, idle_timeout_s)
            world.reader.streams.append(stream)
            world.reader.sessions.append(session)
            return session

        async def list_results(self, request, batch):
            return _completed_result(request.ticket, _stop_context(world.owned, world.source_id),
                entries=_entries(len(values)) if result_entries is None else result_entries, finalized=True)

    world.driver = Driver(_CONTENT)
    entry = DriverEntry(driver_id, world.driver, DriverDeclaration(
        control_supported=True, stop_supported=True, query_supported=False, result_supported=True,
        read_supported=True, digest_supported=True, delete_supported=False), evidence,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    world.idle_entry = replace(entry, declaration=replace(entry.declaration,
        control_supported=False, stop_supported=False, result_supported=False, read_supported=False, digest_supported=False))
    world.tool_log = directory / "segment-probes.log"
    encoded = [None if isinstance(value, BaseException) else str(value) for value in values]
    body = "from pathlib import Path\nimport json,sys,sqlite3\n"
    body += "assert '-count_frames' not in sys.argv\n"
    body += f"source=Path(sys.argv[-1])\nassert source.read_bytes() == {bytes(_CONTENT)!r}\n"
    body += f"relative=source.relative_to(Path({str(Path(world.cfg.paths.staging))!r})).as_posix()\n"
    body += f"with sqlite3.connect({world.path.as_uri()+'?mode=ro'!r}, uri=True) as database:\n"
    body += " rows=database.execute('SELECT c.committed_bytes,c.source_size,c.target_sha256 FROM file_copies c JOIN intermediate_files f ON f.id=c.target_file_id WHERE f.relative_path=?',(relative,)).fetchall()\n"
    body += f"assert len(rows)==1 and rows[0][:2]==({len(_CONTENT)},{len(_CONTENT)}) and rows[0][2] is not None\n"
    body += f"log=Path({str(world.tool_log)!r})\nvalues={encoded!r}\nindex=len(log.read_text().splitlines()) if log.exists() else 0\n"
    body += "with log.open('a') as stream: stream.write(sys.argv[-1]+'\\n')\nvalue=values[index]\n"
    body += "if value is None: raise SystemExit('probe unavailable')\n"
    body += "print(json.dumps({'format':{'duration':value},'streams':[{'index':0,'codec_type':'video','codec_name':'h264','width':64,'height':64,'duration':value,'avg_frame_rate':'30/1','disposition':{'attached_pic':0}}]}))\n"
    probe = ProbeRequest(ffprobe=_tool(directory, "segment-cleanup-probe", body))
    def capture_factory():
        return session_capture_assembly(
            drivers=DriverRegistry((entry,)), devices=world.cfg.devices, staging=Path(world.cfg.paths.staging),
            file_executor=world.executor,
            probe_request=probe, wait_config=execution_wait_config,
            wall_us=lambda: world.wall[0], monotonic_ns=lambda: world.mono[0], segment_size=segment_size)

    world.factory = capture_factory()
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await accept_plan(world.database, directory, world.catalog, {
        "request_id": "1", "created_at": instant, "name": "分段输入来源",
        "actions": [{"name": "录像", "type": "camera_record", "device_id": "cam-1",
            "scheduled_at": instant, "params": {"type": "video"}, "policy": {"max_delay_ms": 5000}}]},
        SimpleNamespace(utc_micros=lambda: world.wall[0]))
    register_window_guard()
    scheduling = SchedulingRepository()
    for save, request in ((scheduling.observe_window, ObserveWindowRequest(1, _NOW, _NOW)),
            (scheduling.start_action, StartActionRequest(1, _NOW, _NOW))):
        key = new_operation_key()
        receipt = await world.database.write(lambda owned, save=save, request=request, key=key:
            save(request, key, owned), key=key)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    from camctl.capture.baseline_models import BaselineFixSave
    key = new_operation_key()
    fixed = await world.database.write(lambda owned: CaptureRepository().fix_baseline(
        BaselineFixSave(1, 0, 0, _NOW), key, owned), key=key)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    runtime = await world.factory(world.database, "cam-1")
    await capture_handler("camera_record")(1, runtime)
    assert world.owned.connection.execute(
        "SELECT started_at IS NOT NULL FROM device_activities WHERE action_id=1").fetchone() == (1,)
    settled = await world.executor.drain_required()
    assert not settled.failed
    world.owned.connection.close()
    world.database = await resources.restart_database(world.path)
    world.owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    world.factory = capture_factory()
    runtime = await world.factory(world.database, "cam-1")
    world.wall[0], world.mono[0] = _NOW + 75_000_000, 80_000_000_000

    class StopPrepared(Exception):
        pass

    actual_conclude = CaptureRepository.conclude_activity

    def conclude(repository, request, key, owned):
        outcome = actual_conclude(repository, request, key, owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        return replace(outcome, handoff_error=StopPrepared())

    with monkeypatch.context() as boundary:
        boundary.setattr(CaptureRepository, "conclude_activity", conclude)
        try:
            await capture_handler("camera_record")(1, runtime)
        except StopPrepared:
            pass
        else:
            raise AssertionError("准备必须在可靠停止及活动结束保存后交付")
    assert world.driver.calls == [("control", "start_recording"), ("stop", "stop_recording")]
    assert world.owned.connection.execute(
        "SELECT control_elapsed_ns FROM device_activities WHERE action_id=1").fetchone() == (None,)
    world.wall[0], world.mono[0] = _NOW + 80_000_000, 85_000_000_000
    obtain_devices = {identity: {**declaration, "copy": {**declaration["copy"], "max_read_attempts": obtain_max_read_attempts}}
        for identity, declaration in world.cfg.devices.items()}
    world.obtain_factory = session_obtain_assembly(devices=obtain_devices, drivers=DriverRegistry((entry,)) if obtain_drivers is None else obtain_drivers,
        staging=Path(world.cfg.paths.staging), ready=Path(world.cfg.paths.ready), processing=Path(world.cfg.paths.processing),
        segment_size=segment_size, occurred_at=lambda: world.wall[0], monotonic_ns=lambda: world.mono[0], file_executor=world.executor)
    world.early = [await world.add_waiter(identity, mode) for identity in early_waiters]
    return world
