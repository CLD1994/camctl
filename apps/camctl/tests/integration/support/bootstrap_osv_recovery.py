"""真实双视频输入由公开C1生产，媒体修复沿正式工厂与执行端口恢复。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

from camctl.acceptance.service import AcceptanceContext, CommandMode
from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.capture.handlers import capture_handler
from camctl.bootstrap.flows import winddown_flow
from camctl.contracts.values import new_operation_key
from camctl.host_files.media import ProbeRequest, RepairRequest
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskExecutor, FileTaskResult
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, register_acceptance_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.timelapse import register_timelapse_guards
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest, SchedulingRepository, StartActionRequest, register_window_guard)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.supervision import Supervisor

from .acceptance import accept_document
from .capture import capture_database
from ..bootstrap.test_media_assembly import _SessionDriver, _config, _registry, _tool
from ..bootstrap.test_recording_stop import _RecordCatalog
from ..capture.test_capture_contract import _NOW, ResultsDouble, _entry


class HalfSecondCatalog(_RecordCatalog):
    duration_s = Decimal("0.5")


class OsvWorld:
    def __init__(self, home, resources, content):
        self.home, self.resources, self.content = home, resources, content
        self.config = _config(home, repair_margin_s="0")
        self.path = Path(self.config.paths.state_db)
        self.wall, self.mono = [_NOW], [7_000_000_000]
        self.driver = _SessionDriver(content)
        self.roots = BoundDirectories(staging=Path(self.config.paths.staging))
        self.database = self.runtime = None
        self.executors = []
        self.probe_logs = []
        self.pause, self.withdrawn = False, []

    async def factory(self, *, pause=False):
        self.database = await capture_database(self.path)
        self.pause = pause
        executor = FileTaskExecutor(Supervisor())
        self.executors.append(executor)
        probe_log = self.home / f"probe-{len(self.probe_logs)}.log"
        self.probe_logs.append(probe_log)
        ffprobe = shutil.which("ffprobe")
        ffmpeg = shutil.which("ffmpeg")
        assert ffprobe is not None and ffmpeg is not None
        observed_probe = _tool(self.home, f"real-probe-{len(self.probe_logs)}", (
            "from pathlib import Path\nimport subprocess,sys\n"
            f"with Path({str(probe_log)!r}).open('a') as log: log.write(sys.argv[-1]+'\\n')\n"
            f"result=subprocess.run([{ffprobe!r},*sys.argv[1:]])\n"
            "sys.exit(result.returncode)\n"))
        source = replace(_entry("osv-source", size=len(self.content)),
            original_name="clip.OSV", media_type=None, format_id="osv")
        factory = session_capture_assembly(devices=self.config.devices, drivers=_registry(self.driver),
            staging=self.roots.staging, results=ResultsDouble({1: (source,)}),
            wait_config=execution_wait_config, wall_us=lambda: self.wall[0],
            monotonic_ns=lambda: self.mono[0], file_executor=executor,
            probe_request=ProbeRequest(ffprobe=observed_probe), repair_request=RepairRequest(ffmpeg=ffmpeg))
        self.capture_factory = factory
        self.runtime = await factory(self.database, "cam-1")
        assert self.runtime is not None and self.runtime.media is not None
        return self.runtime, executor

    async def reconnect(self):
        await self.settle()
        self.database = await self.resources.restart_database(self.path)

    async def settle(self):
        errors = []
        for executor in self.executors:
            try:
                result = await executor.drain_required()
                errors.extend(failure.exception or RuntimeError(failure.error) for failure in result.failed)
                assert executor.unfinished_files() == ()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("双视频文件任务收场失败", errors)


@asynccontextmanager
async def osv_world(home, resources, content, monkeypatch):
    register_acceptance_guards()
    register_window_guard()
    register_capture_guards()
    register_operation_guards()
    register_outputs_guards()
    register_timelapse_guards()
    world = OsvWorld(home, resources, content)
    assert initialize_state(world.config, world.path).outcome is InitOutcome.CREATED
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        accepted = await accept_document({"request_id": "1", "created_at": instant, "name": "双视频恢复",
            "actions": [{"name": "录像", "type": "camera_record", "device_id": "cam-1",
                "scheduled_at": instant, "params": {"type": "video"}, "policy": {"max_delay_ms": 5000}}]},
            AcceptanceContext(CommandMode.RUN, HalfSecondCatalog(), AcceptanceRepository(),
                SimpleNamespace(utc_micros=lambda: _NOW)), world.path)
        assert accepted.plan_id == 1
        world.database = await capture_database(world.path)
        scheduling = SchedulingRepository()
        for method, request in ((scheduling.observe_window, ObserveWindowRequest(1, _NOW, _NOW)),
                                (scheduling.start_action, StartActionRequest(1, _NOW, _NOW))):
            key = new_operation_key()
            receipt = await world.database.write(lambda current: method(request, key, current), key=key)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        from camctl.bootstrap import capture_assembly
        actual_repair = capture_assembly.repair_media

        async def repair(input, output, roots, request, **options):
            if world.pause:
                world.withdrawn.append((input, output, request))
                return FileTaskResult(options["task_id"], ran=False)
            return await actual_repair(input, output, roots, request, **options)

        with monkeypatch.context() as patch:
            # F6正式主机修复入口；只注入未执行回执，不替换完整媒体保存任务。
            patch.setattr(capture_assembly, "repair_media", repair)
            runtime, _executor = await world.factory()
            await capture_handler("camera_record")(1, runtime)
            # 结束旧worker及本次计时依据；公开保守收场保存待检查进度。
            await world.reconnect()
            world.wall[0] += 1_000_000
            runtime, executor = await world.factory(pause=True)
            await winddown_flow(capture_factory=world.capture_factory, wait_cap_s=Decimal("0"))(
                SimpleNamespace(database=world.database, clock=SimpleNamespace(utc_micros=lambda: world.wall[0])))
            world.input_probe_log = world.probe_logs[-1]
            world.owned = owned
            world.processing_id, world.source_id = owned.connection.execute(
                "SELECT id,source_device_file_id FROM recording_processing WHERE action_id=1").fetchone()
            assert world.source_id is not None
            assert owned.connection.execute("SELECT check_decision,check_state,repair_state FROM recording_processing"
                " WHERE id=?", (world.processing_id,)).fetchone() == (3, 1, 1)
            yield world
    finally:
        original = sys.exception()
        try:
            await world.settle()
        except BaseException as closing:
            if original is not None:
                raise BaseExceptionGroup("双视频验证及实际文件收场均失败", [original, closing])
            raise
        finally:
            owned.connection.close()
