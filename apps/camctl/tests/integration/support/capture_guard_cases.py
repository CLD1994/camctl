"""拍摄守卫专项只集中正式历史格式、事务协议和真实资源准备。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from camctl.contracts.json_encoding import encode_exact_json


def history_body(event):
    """按 database/history-formats.md 的事件正文版本1编码静态输入。"""
    return encode_exact_json({
        "reason": event.reason, "evidence": dict(event.evidence),
        "rows": [{"table": row.table, "id": row.row_id,
            "before": {"exists": row.before.exists, "values": dict(row.before.values)},
            "after": {"exists": row.after.exists, "values": dict(row.after.values)}}
            for row in event.rows],
    })


class CompleteResultCommand:
    """接收公开仓储交给 commit_operation 的 AtomicCommand，增加协议回调。"""

    def __init__(self, delegate, callback, *, read_only):
        self.delegate, self.callback, self.read_only = delegate, callback, read_only
        self.plans = []

    def plan(self, scope):
        plan = self.delegate.plan(scope)
        assert plan.read_only is self.read_only
        assert bool(plan.events) == (not self.read_only)
        self.plans.append(plan)
        return replace(plan, complete_result=self.callback)


@asynccontextmanager
async def host_cleanup(owned, staging):
    """清理仓储专项使用正式工厂、真实worker与文件执行器，并完整关闭。"""
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor
    from .database import business_database, database_path

    supervisor = Supervisor()
    async with business_database(database_path(owned)) as database:
        factory = session_cleanup_assembly(devices={}, drivers=None, staging=Path(staging),
            max_delete_attempts=1, max_query_attempts=1,
            file_executor=FileTaskExecutor(supervisor),
            occurred_at=lambda: 1_800_000_000_000_000, monotonic_ns=lambda: 0)
        try:
            yield await factory(database)
        finally:
            result = await supervisor.drain_required()
            errors = [failure.exception or RuntimeError(failure.error) for failure in result.failed]
            if errors:
                raise BaseExceptionGroup("主机源清理实际文件责任收场失败", errors)


async def known_failure_case(directory, resources, monkeypatch, values, entries, *, no_video=False):
    """由正式C1形成75秒控制事实，再由真实READ/工具验证结果失败优先。"""
    from decimal import Decimal
    from types import SimpleNamespace
    import hashlib
    from unittest.mock import create_autospec
    from camctl.capture.handlers import capture_handler
    from camctl.capture.baseline_models import BaselineFixSave
    from camctl.contracts.values import new_operation_key
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.evidence import DeviceObservation
    from camctl.devices.ports import DeviceCallResult, DriverDeclaration, StopDriver
    from camctl.devices.read_session import ReadSession
    from camctl.host_files.media import ProbeRequest
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.operations.attempts import AttemptConfig
    from ..capture.result_consumer_fixtures import consumer_world, returned
    from ..capture.test_action6_segmented_normal import Action6RecordCatalog
    from ..capture.test_capture_contract import _NOW
    from ..capture.test_call_result_save import _outcome
    from ..capture.test_input_copy import _CONTENT
    from ..capture.test_record_file_completion_wait import _completed_result, _stop_context, _REGISTRY
    from ..bootstrap.test_media_assembly import _EVIDENCE, _tool
    from ..bootstrap.test_obtain_flow import _MemoryStream

    class ObservedStream(_MemoryStream):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    world = SimpleNamespace(wall=[_NOW], mono=[5_000_000_000], streams=[], sessions=[], result_calls=[])
    world.tool_log = directory / "guard-probes.log"
    body = "from pathlib import Path\nimport json,sys,sqlite3\nassert '-count_frames' not in sys.argv\n"
    body += f"source=Path(sys.argv[-1])\nassert source.read_bytes()=={_CONTENT!r}\n"
    body += f"relative=source.relative_to(Path({str(directory/'staging')!r})).as_posix()\n"
    body += f"with sqlite3.connect({(directory/'state.db').as_uri()+'?mode=ro'!r},uri=True) as database:\n"
    body += " rows=database.execute('SELECT c.committed_bytes,c.source_size,c.target_sha256 FROM file_copies c JOIN intermediate_files f ON f.id=c.target_file_id WHERE f.relative_path=?',(relative,)).fetchall()\n"
    body += f"assert len(rows)==1 and rows[0][:2]==({len(_CONTENT)},{len(_CONTENT)}) and rows[0][2] is not None\n"
    body += f"log=Path({str(world.tool_log)!r})\nvalues={[str(value) for value in values]!r}\nindex=len(log.read_text().splitlines()) if log.exists() else 0\n"
    body += "with log.open('a') as stream: stream.write(str(source)+'\\n')\n"
    body += ("print(json.dumps({'format':{'duration':'60'},'streams':[]}))\n" if no_video else
        "value=values[index]\nprint(json.dumps({'format':{'duration':value},'streams':[{'index':0,'codec_type':'video','codec_name':'h264','width':64,'height':64,'duration':value,'avg_frame_rate':'30/1','disposition':{'attached_pic':0}}]}))\n")
    probe = ProbeRequest(ffprobe=_tool(directory, "guard-probe", body))
    evidence = _EVIDENCE.with_contract(_REGISTRY.contract("results_returned", 1))

    async def build(owned, *, driver):
        world.owned, world.action_id = owned, 1
        activity_id, = owned.connection.execute("SELECT id FROM device_activities WHERE action_id=1").fetchone()
        stopper = create_autospec(StopDriver, instance=True)
        async def stop(request):
            if stopper.stop.await_count == 1:
                return DeviceCallResult.from_outcome(_outcome("stop"))
            return DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", activity_id))
        stopper.stop.side_effect = stop
        world.control, world.stopper = driver, stopper

        async def read(source, offset, ticket, *, idle_timeout_s):
            stream = ObservedStream(_CONTENT[offset:])
            session = ReadSession(source, offset, stream, idle_timeout_s)
            world.streams.append(stream)
            world.sessions.append(session)
            return session

        async def digest(request):
            return DeviceCallResult((DeviceObservation("file_digest", 1,
                {"file_id": request.params["file_id"], "sha256": hashlib.sha256(_CONTENT).hexdigest()}),), None)

        async def results(request, batch):
            world.result_calls.append(request)
            return _completed_result(request.ticket, _stop_context(owned, 1), entries=entries, finalized=True)

        ports = SimpleNamespace(control=driver.control, stop=stopper.stop,
            open_read=read, digest=digest, list_results=results)
        declaration = DriverDeclaration(control_supported=True, stop_supported=True, query_supported=False,
            result_supported=True, read_supported=True, digest_supported=True, delete_supported=False)
        registry = DriverRegistry((DriverEntry("camctl-adb", ports, declaration, evidence,
            DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        world.factory = await resources.factory(owned, driver=driver, evidence=evidence,
            drivers=registry, devices={"cam-1": {"driver": "camctl-adb",
                "recording": {"repair_margin_s": Decimal(2)},
                "result_check": {"max_attempts": 3, "call_timeout_s": Decimal("1.25"),
                    "retry_interval_s": Decimal(2)}}},
            staging=directory/'staging', probe_request=probe, segment_size=4,
            stop_config=AttemptConfig(3, Decimal("1"), Decimal("3")),
            wall_us=lambda: world.wall[0], monotonic_ns=lambda: world.mono[0])
        return await world.factory(owned)

    catalog = Action6RecordCatalog()
    catalog.duration_s = Decimal(60)
    owned, runtime, action_id, handler = await consumer_world(directory, "record", catalog=catalog,
        start=False, runtime_factory=build)
    try:
        world.runtime, world.handler = runtime, handler
        fixed = CaptureRepository().fix_baseline(BaselineFixSave(1, 0, 0, _NOW), new_operation_key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        await capture_handler(handler)(action_id, runtime)
        world.wall[0], world.mono[0] = _NOW+72_000_000, 77_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert world.stopper.stop.await_count == 1
        world.wall[0], world.mono[0] = _NOW+75_000_000, 80_000_000_000
        actual_conclude = CaptureRepository.conclude_activity

        class StoppedBeforeResults(Exception):
            pass

        def conclude(repository, request, key, current):
            receipt = actual_conclude(repository, request, key, current)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=StoppedBeforeResults())

        with monkeypatch.context() as boundary:
            boundary.setattr(CaptureRepository, "conclude_activity", conclude)
            try:
                await capture_handler(handler)(action_id, runtime)
            except StoppedBeforeResults:
                pass
            else:
                raise AssertionError("真实停止后须在RESULTS前交付准备")
        world.wall[0], world.mono[0] = _NOW+80_000_000, 85_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert len(world.result_calls) == 1
        assert world.streams and all(stream.closed for stream in world.streams)
        ends = [session.poll_stopped() for session in world.sessions]
        assert len(ends) == len(world.streams)
        assert all(end is not None and end.stopped and end.error is None
            and end.bytes_read == len(_CONTENT) for end in ends)
        world.tools = world.tool_log.read_text().splitlines() if world.tool_log.exists() else []
        return world
    except BaseException:
        owned.connection.close()
        raise


def host_success_event_context(action, occurred_at, *, outputs=None):
    """H1静态专项：明确完整空源范围及0/1设备产物，不声称实际可提交。"""
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.events import load_event_registry
    from camctl.history.reads import ReadCoverage
    from camctl.history.validators import EventContext
    from camctl.persistence.transaction import event_envelope, update_change

    spec = load_event_registry()["events"]["ACTION_FINISHED"]
    event = event_envelope(2, 2, spec["id"], spec["branches"]["SUCCEED"]["reason"],
        (update_change("actions", 1, {"status": action["status"]}, {"status": 3}),), occurred_at)
    event = replace(event, change_seq=1)
    outputs = {} if outputs is None else outputs
    context = EventContext(TransactionRange(2, 2, 2), {("actions", 1): ("action", 1)},
        {"actions": {1: action}, "intermediate_files": {}, "outputs": outputs},
        transaction_rows={"outputs": outputs, "output_origins": {}},
        read_coverage=ReadCoverage({("intermediate_files", "owner_action_id"): frozenset({1})}))
    return event, context
