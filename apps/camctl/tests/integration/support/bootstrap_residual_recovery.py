"""残留收场从真实录像失败与后续公开动作形成，不调用内部推进。"""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

from camctl.acceptance.service import AcceptanceContext, CommandMode
from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.flows import cancel_flow, capture_flow, residual_flow
from camctl.capture.handlers import capture_handler
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.transactions import row_facts

from .acceptance import accept_document
from .bootstrap_recovery import record_world
from ..bootstrap.test_binding_changes import _NOW
from ..bootstrap.test_residual_winddown import _EVIDENCE, _WinddownCatalog
from ..capture.test_capture_contract import ResultsDouble


class Catalog(_WinddownCatalog):
    def __init__(self, identity="camctl-adb"):
        self.identity = identity

    def driver_id(self, device_id):
        return self.identity if self.device_exists(device_id) else None


class ResidualDriver:
    """外部端口编排活动、空闲和失败观察，保留实际请求和票据。"""

    def __init__(self, activity_id, *, stop_error=False, query_mode="active"):
        self.activity_id, self.stop_error, self.query_mode = activity_id, stop_error, query_mode
        self.calls = []

    async def control(self, request):
        self.calls.append(("control", request))
        raise AssertionError("原残留尚未解除，不得派发新的拍摄")

    async def query_state(self, request):
        self.calls.append(("query", request))
        mode = "active" if request.ticket.responsibility_key.startswith("query/preflight/") else self.query_mode
        return DeviceCallResult(observations=((DeviceObservation("activity_status", 1,
            {"activity_id": str(self.activity_id)}),) if mode == "active" else ()),
            error={"code": "device_error"} if mode == "error" else None)

    async def stop(self, request):
        self.calls.append(("stop", request))
        return DeviceCallResult(observations=(), error={"code": "device_error"} if self.stop_error else None)


class ResidualCase:
    def __init__(self, source, resources, *, stop_error=False, query_attempts=3,
                 query_timeout=Decimal("17"), query_interval=Decimal("0")):
        self.source, self.resources = source, resources
        self.owned, self.path, self.home = source.owned, source.path, source.path.parent
        self.old_action = source.action_id
        self.activity, = self.owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (self.old_action,)).fetchone()
        self.wall, self.mono = [_NOW], [100_000_000_000]
        self.context = SimpleNamespace(database=source.database,
            clock=SimpleNamespace(utc_micros=lambda: self.wall[0]))
        self.driver = ResidualDriver(self.activity, stop_error=stop_error)
        self.query_attempts, self.query_timeout, self.query_interval = query_attempts, query_timeout, query_interval
        self.factory = self.new_factory()
        self.trigger = self.flow = None

    def action(self, identity):
        return row_facts(self.owned.connection, "actions", identity)

    def run(self, key):
        return self.owned.connection.execute("SELECT id,status,attempts_used FROM operation_runs"
            " WHERE responsibility_key=?", (key,)).fetchone()

    def new_factory(self, *, identity="camctl-adb", driver=None):
        driver = self.driver if driver is None else driver
        declaration = DriverDeclaration(True, True, True, False, False, False, False)
        registry = DriverRegistry(tuple(DriverEntry(name, driver, declaration, _EVIDENCE,
            DriverStatus.SOFTWARE_CONTRACT_VERIFIED) for name in ("camctl-adb", "alternate-camera")))
        return session_capture_assembly(devices={"cam-1": {"driver": identity,
            "query": {"max_attempts": self.query_attempts, "timeout_s": self.query_timeout,
                "retry_interval_s": self.query_interval},
            "residual_stop": {"max_attempts": 3, "timeout_s": "13", "retry_interval_s": "0"}}},
            drivers=registry, staging=self.home / "staging", results=ResultsDouble({}),
            wait_config=execution_wait_config, wall_us=lambda: self.wall[0],
            monotonic_ns=lambda: self.mono[0])

    async def accept(self, request_id, action, *, driver_id="camctl-adb"):
        result = await accept_document({"request_id": str(request_id),
            "created_at": "2026-01-15 08:00:00", "name": f"plan-{request_id}", "actions": [action]},
            AcceptanceContext(CommandMode.RUN, Catalog(driver_id), AcceptanceRepository(), self.context.clock), self.path)
        assert result.plan_id is not None, result
        return self.owned.connection.execute("SELECT MAX(id) FROM actions").fetchone()[0]

    @staticmethod
    def photo(name):
        return {"name": name, "type": "camera_take_photo", "device_id": "cam-1",
            "scheduled_at": "2026-01-15 09:00:00", "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 1000}}

    async def capture(self, factory=None):
        flow = capture_flow(self.factory if factory is None else factory)
        await flow(self.context)
        await flow.settle()

    async def advance(self):
        await residual_flow(self.factory)(self.context)

    def elapse(self, seconds):
        self.wall[0] += seconds * 1_000_000
        self.mono[0] += seconds * 1_000_000_000

    async def cancel_trigger(self):
        await self.accept(3, {"name": "取消原触发", "type": "cancel_task",
            "params": {"target": {"action_instance_id": str(self.trigger)}}})
        await cancel_flow(staging=self.home / "staging",
            ready=self.home / "ready", processing=self.home / "processing")(self.context)
        assert self.action(self.trigger)["status"] == 6

    async def reconnect(self):
        self.context.database = await self.resources.restart_database(self.path)


@asynccontextmanager
async def residual_case(tmp_path, resources, monkeypatch, *, stop_error=False,
                        cancel_trigger=False, **options):
    source = await record_world(tmp_path, resources, monkeypatch)
    try:
        # STOP 实际返回已保存，下一次公开推进可靠结束原动作和 STOP 责任。
        await capture_handler("camera_record")(source.action_id, source.runtime)
        assert source.owned.connection.execute("SELECT status FROM actions WHERE id=?",
            (source.action_id,)).fetchone() == (4,)
        assert source.owned.connection.execute("SELECT activity_state,occupancy_state"
            " FROM device_activities WHERE action_id=?", (source.action_id,)).fetchone() == (2, 1)
        case = ResidualCase(source, resources, stop_error=stop_error, **options)
        case.trigger = await case.accept(2, case.photo("原收场触发"))
        await case.capture()
        case.flow = case.owned.connection.execute("SELECT id,action_id,activity_id FROM operation_runs"
            " WHERE responsibility_key=?", (f"followup/{case.trigger}/{case.activity}",)).fetchone()
        assert case.flow and case.flow[1:] == (case.trigger, case.activity)
        assert case.run(f"followup/{case.trigger}/{case.activity}")[1:] == (2, 1)
        if cancel_trigger:
            await case.cancel_trigger()
        yield case
    finally:
        source.owned.connection.close()
        source.close()
