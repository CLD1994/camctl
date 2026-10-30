"""取消专项沿正式受理、拍摄工厂及取消流程拥有真实资源。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.flows import cancel_flow
from camctl.capture.handlers import capture_handler
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.pages import Page
from camctl.contracts.values import new_operation_key
from camctl.devices.directory import DirectoryRead
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceContract
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.host_files.tasks import FileTaskExecutor
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.initialization import initialize_state, InitOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, ObserveWindowRequest, StartActionRequest
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.session.supervision import Supervisor

from .session import session_config
from ..capture.result_consumer_fixtures import ResultCatalog, RESULT_EVIDENCE
from ..capture.test_capture_contract import _NOW
from ..capture.test_native_completion_context import NativeWaitCatalog, _osv
from ..capture.test_record_file_completion_wait import _REGISTRY
from ..capture.test_result_page_history import _actual


class CancellationCatalog(NativeWaitCatalog):
    def __init__(self, *, finite=True, startup=None, extra_rules=(), output_scope=None, record_wait_s=None, record_rules=None):
        self.finite, self.startup, self.extra_rules = finite, startup, extra_rules
        self.output_scope = output_scope
        self.record_wait_s = record_wait_s
        self.record_rules = record_rules

    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = (super().parameter_definition(device_id, action_type, parameter_type)
            if self.finite else ResultCatalog().parameter_definition(device_id, action_type, parameter_type))
        if definition is not None and action_type == "camera_record" and self.record_wait_s is not None:
            original = definition.task_factory
            def record_task(params):
                value = original(params)
                return replace(value, file_completion_wait_s=self.record_wait_s,
                    product_rules=value.product_rules if self.record_rules is None else self.record_rules)
            return replace(definition, task_factory=record_task)
        if definition is not None and action_type == "camera_timelapse" and self.finite:
            original = definition.task_factory
            def task(params):
                value = original(params)
                return replace(value, target_duration_s=Decimal("1"),
                    result_wait_margin_s=Decimal(".2"), startup_margin_ms=self.startup,
                    cancel_completion_policy="finite_device_end" if self.finite else value.cancel_completion_policy,
                    product_rules=tuple(value.product_rules) + tuple(self.extra_rules),
                    ownership_mode=value.ownership_mode if self.output_scope is None else 2,
                    output_scope=value.output_scope if self.output_scope is None else self.output_scope)
            return replace(definition, task_factory=task)
        return definition


class CancellationPorts:
    """脚本仅控制真实驱动端口的返回，不改写拍摄运行时。"""

    def __init__(self):
        self.controls, self.stops, self.pages, self.directories = [], [], [], []
        self.control_script = self.stop_script = self.result_script = None

    async def control(self, request):
        self.controls.append(request)
        if self.control_script is not None:
            return await self.control_script(request)
        observation = "timelapse_sent" if request.operation == "start_timelapse" else "start_confirmed"
        from camctl.devices.evidence import DeviceObservation
        return DeviceCallResult((DeviceObservation(observation, 1, {"activity_id": request.ticket.target_id}),), None)

    async def stop(self, request):
        self.stops.append(request)
        if self.stop_script is not None:
            return await self.stop_script(request)
        return DeviceCallResult.from_outcome(CallOutcome(status=AttemptStatus.FAILED,
            effect=EffectState.UNKNOWN, error=ErrorValue("device_error", "device"),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("stop_returned", 1, {}))))

    async def list_results(self, request, batch):
        self.pages.append(request)
        if self.result_script is not None:
            return await self.result_script(request, batch)
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))

    async def read_directory(self, request, *args, **kwargs):
        self.directories.append(request)
        return DirectoryRead(None, None, Page((), None))


class CancellationWorld:
    def __init__(self, resources):
        self.resources = resources
        self.wall, self.mono = [_NOW], [5_000_000_000]
        self.ports = CancellationPorts()
        self.executor = FileTaskExecutor(Supervisor())
        self.database = self.owned = None
        self.requests = 1
        self.factory_options = {}

    def new_factory(self):
        registry = _REGISTRY.with_contract(RESULT_EVIDENCE.contract("timelapse_sent", 1))
        registry = registry.with_contract(RESULT_EVIDENCE.contract("result_files_listed", 1))
        registry = registry.with_contract(RESULT_EVIDENCE.contract("adb_foreground_assumption", 1))
        registry = registry.with_contract(EvidenceContract("adb_foreground_recovery", 1, "control", frozenset()))
        registry = registry.with_contract(EvidenceContract("adb_foreground_recovery", 1, "stop", frozenset()))
        registry = registry.with_contract(EvidenceContract("results_returned", 1, "result", frozenset({
            "file_completion", "file_completion_source_page_event_id", "optional_preview_observations"})))
        entry = DriverEntry("camctl-adb", self.ports, DriverDeclaration(
            control_supported=True, stop_supported=True, result_supported=True,
            query_supported=False, read_supported=False, digest_supported=False,
            delete_supported=False, directory_supported=True,
            adb_foreground_recovery_operations=frozenset({"control", "stop", "result"})),
            registry, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
        self.factory = session_capture_assembly(devices=self.cfg.devices,
            drivers=DriverRegistry((entry,)), staging=Path(self.cfg.paths.staging),
            wall_us=lambda: self.wall[0], monotonic_ns=lambda: self.mono[0],
            wait_config=lambda action: CaptureWaitConfig(1000, 200, 50,
                startup_margin_ms=self.catalog.startup or 0) if self.catalog.finite else CaptureWaitConfig(600000, 0, 0),
            file_executor=self.executor, **self.factory_options)

    async def view(self):
        self.runtime = await self.factory(self.database, "cam-1")
        assert self.runtime is not None
        return self.runtime

    async def advance(self):
        await capture_handler(self.handler)(self.action_id, await self.view())

    def elapse(self, micros):
        self.wall[0] += micros
        self.mono[0] += micros * 1000

    async def cancel(self):
        self.requests += 1
        instant = datetime.fromtimestamp(self.wall[0] // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        result = await accept_input(ParsedInput("cancel.json", {
            "request_id": str(self.requests), "created_at": instant, "name": "取消拍摄",
            "actions": [{"name": "取消", "type": "cancel_task", "params": {
                "target": {"action_instance_id": str(self.action_id)}}}]}),
            AcceptanceContext(CommandMode.RUN, self.catalog, AcceptanceRepository(),
                clock=SimpleNamespace(utc_micros=lambda: self.wall[0])), new_operation_key(), self.database)
        assert result.plan_id is not None, result
        await self.settle_cancel()
        return self.owned.connection.execute("SELECT id FROM actions WHERE plan_id=? AND type=6", (result.plan_id,)).fetchone()[0]

    async def settle_cancel(self):
        await cancel_flow(ready=Path(self.cfg.paths.ready), processing=Path(self.cfg.paths.processing),
            start_entries=self.runtime.start_entries)(SimpleNamespace(database=self.database,
                clock=SimpleNamespace(utc_micros=lambda: self.wall[0])))

    async def reopen(self, *, cold=True):
        await self.drain()
        self.database = await self.resources.restart_database(self.path)
        self.owned.connection.close()
        self.owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
        if cold:
            self.new_factory()
        return await self.view()

    async def drain(self):
        result = await self.executor.drain_required()
        failures = [item.exception or RuntimeError(item.error) for item in result.failed]
        if failures:
            raise BaseExceptionGroup("取消专项文件责任收场失败", failures)

    async def close(self):
        try:
            await self.drain()
        finally:
            if self.owned is not None:
                self.owned.connection.close()


async def cancellation_world(directory, resources, *, finite=True, startup=None, extra_rules=(),
                             handler="camera_timelapse", start=True, canceled=True,
                             check_attempts=3, stop_attempts=3, output_scope=None, record_wait_s=None, record_rules=None,
                             independent_activity=False, stop_timeout_s=Decimal("1"), stop_retry_s=Decimal("0"),
                             check_timeout_s=Decimal("10"), check_retry_s=Decimal("3")):
    world = CancellationWorld(resources)
    try:
        world.catalog = CancellationCatalog(finite=finite, startup=startup, extra_rules=extra_rules, output_scope=output_scope, record_wait_s=record_wait_s, record_rules=record_rules)
        world.handler = handler
        world.cfg = session_config(directory, devices={"cam-1": {"kind": "camera", "driver": "camctl-adb",
            "capture": {"extra_wait_ms": 50 if finite else 0},
            "recording": {"max_stop_attempts": stop_attempts, "stop_timeout_s": stop_timeout_s, "stop_retry_interval_s": stop_retry_s},
            "result_check": {"max_attempts": check_attempts, "call_timeout_s": check_timeout_s, "retry_interval_s": check_retry_s}}})
        world.path = Path(world.cfg.paths.state_db)
        assert initialize_state(world.cfg, world.path).outcome is InitOutcome.CREATED
        world.owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        world.database = await resources.database(world.path)
        instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        actions = [{"name": "报告", "type": "report_status", "params": {"scope": "full"}}] if independent_activity else []
        actions.append({"name": "拍摄", "type": handler, "device_id": "cam-1",
                "scheduled_at": instant, "params": {"type": "timelapse" if handler == "camera_timelapse" else "video"},
                "policy": {"max_delay_ms": 5000}})
        result = await accept_input(ParsedInput("capture.json", {"request_id": "1", "created_at": instant,
            "name": "取消来源", "actions": actions}),
            AcceptanceContext(CommandMode.RUN, world.catalog, AcceptanceRepository(),
                clock=SimpleNamespace(utc_micros=lambda: _NOW)), new_operation_key(), world.database)
        assert result.plan_id is not None, result
        world.action_id, = world.owned.connection.execute("SELECT id FROM actions WHERE type IN (2,3)").fetchone()
        repository = SchedulingRepository()
        for method, request in ((repository.observe_window, ObserveWindowRequest(world.action_id, _NOW, _NOW)),
                (repository.start_action, StartActionRequest(world.action_id, _NOW, _NOW))):
            key = new_operation_key()
            receipt = await world.database.write(lambda owned, method=method, request=request, key=key:
                method(request, key, owned), key=key)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        world.activity_id, = world.owned.connection.execute("SELECT id FROM device_activities WHERE action_id=?", (world.action_id,)).fetchone()
        world.new_factory()
        await world.view()
        if start:
            await world.advance()
        if canceled:
            await world.cancel()
        return world
    except BaseException as error:
        try:
            await world.close()
        except BaseException as closing:
            raise BaseExceptionGroup("取消世界准备与资源关闭均失败", [error, closing])
        raise


def finite_end_proposal(world, *, before_event=None):
    """从真实原历史建立静态事件输入，不调用生产私有守卫或事实装载器。"""
    from camctl.contracts.history_values import HistoryBoundary, TransactionRange
    from camctl.history.events import load_event_registry
    from camctl.history.reads import CaptureResultReadScope
    from camctl.history.validators import EventContext
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.fact_reads import EVENT_COLUMNS
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.persistence.transaction import event_envelope, update_change
    from .result_cases import current_result_ticket

    connection = world.owned.connection
    history = HistoryRepository(world.path)
    boundary = history.current_boundary()
    if before_event is not None:
        transaction, = connection.execute("SELECT transaction_id FROM history_events WHERE id=?", (before_event,)).fetchone()
        boundary = HistoryBoundary(*connection.execute(
            "SELECT id,last_event_id FROM history_transactions WHERE id<? ORDER BY id DESC LIMIT 1", (transaction,)).fetchone())
    image = history.restore_entity("action", world.action_id, boundary)
    state = {}
    for (table, identity), facts in image.items():
        state.setdefault(table, {})[identity] = facts
    rows = connection.execute("SELECT " + EVENT_COLUMNS + " FROM history_events WHERE id<=? ORDER BY id", (boundary.last_event_id,))
    columns = [column[0] for column in rows.description]
    state["history_events"] = {row[0]: dict(zip(columns, row)) for row in rows}
    action = state["actions"][world.action_id]
    activity = state["device_activities"][world.activity_id]
    runs = {row["responsibility_key"]: row for row in state["operation_runs"].values()}
    start, stop, result = (runs[key] for key in (f"start/{world.action_id}", f"stop/{world.action_id}", f"results/{world.activity_id}"))
    def attempt(run):
        return max((row for row in state["operation_attempts"].values() if row["run_id"] == run["id"]), key=lambda row: row["attempt_no"])
    sent, stopped, actual = map(attempt, (start, stop, result))
    # 静态输入只交付三份被引用的原尝试；完整文件范围由有界读取端口提供。
    state["operation_attempts"] = {row["id"]: row for row in (sent, stopped, actual)}
    page = CaptureRepository().read_last_result_page(current_result_ticket(world.owned, world.action_id), world.owned)
    assert page is not None
    evidence = {"policy": action["execution_spec_json"]["cancel_completion_policy"],
        "activity_id": world.activity_id, "start_result_event_id": sent["result_event_id"],
        "stop_run_id": stop["id"], "stop_result_event_id": stopped["result_event_id"],
        "sent_at_us": activity["sent_at"], "target_duration_ms": action["execution_spec_json"]["target_duration_ms"],
        "result_wait_margin_ms": activity["result_wait_margin_ms"], "extra_wait_ms": activity["extra_wait_ms_used"],
        "expected_check_at_us": activity["expected_check_at"], "wait_completed_event_id": activity["wait_completed_event_id"],
        "result_attempt_id": actual["id"], "result_page_event_id": page.ref.event_id}
    if "startup_margin_ms" in action["execution_spec_json"]:
        evidence["startup_margin_ms"] = action["execution_spec_json"]["startup_margin_ms"]
    transaction = connection.execute("SELECT MAX(id)+1 FROM history_transactions").fetchone()[0]
    event_id = connection.execute("SELECT MAX(id)+1 FROM history_events").fetchone()[0]
    registry = load_event_registry()["events"]["DEVICE_OBSERVED"]
    event = event_envelope(event_id, transaction, registry["id"], registry["branches"]["FINITE_CANCEL_END"]["reason"],
        (update_change("device_activities", world.activity_id, {"activity_state": activity["activity_state"]}, {"activity_state": 3}),),
        world.wall[0], evidence={"cancel_completion": evidence})
    event = replace(event, change_seq=connection.execute(
        "SELECT COALESCE(MAX(change_seq),0)+1 FROM history_events").fetchone()[0])
    scope = CaptureResultReadScope(world.action_id, world.activity_id, page.ref.ticket.run_id,
        page.ref.ticket.attempt_id, page.ref.event_id)
    context = EventContext(TransactionRange(transaction, event_id, event_id),
        {("device_activities", world.activity_id): ("action", world.action_id)}, state,
        current_reads=SqlEventReads(connection, scope))
    return event, context


async def stored_result_case(directory, resources, monkeypatch, *, entries, extra_rules=(),
                             prior_entries=None, notes=(), check_attempts=3,
                             check_timeout_s=Decimal("10"), check_retry_s=Decimal("3")):
    """仓储专项消费真实C1已经保存的页，不代替业务保存责任的恢复测试。"""
    from camctl.operations.models import ErrorValue
    from camctl.persistence.repositories.capture import CaptureRepository
    world = await cancellation_world(directory, resources, extra_rules=extra_rules,
        check_attempts=check_attempts, check_timeout_s=check_timeout_s, check_retry_s=check_retry_s)
    calls = []
    async def read(request, batch):
        assert request.timeout_s == check_timeout_s
        assert world.owned.connection.execute(
            "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_runs WHERE responsibility_key=?",
            (f"results/{world.activity_id}",)).fetchone() == (check_attempts, str(check_timeout_s), str(check_retry_s))
        calls.append(request.ticket)
        first = prior_entries is not None and len(calls) == 1
        outcome = _actual(request.ticket, entries=prior_entries if first else entries,
            finalized=not first, error=ErrorValue("device_error", "device") if first else None)
        if notes and not first:
            outcome = replace(outcome, settlement=Settlement(SettlementBasis.OBSERVED,
                EvidenceValue("results_returned", 1, {"optional_preview_observations": list(notes)})))
        return DeviceCallResult.from_outcome(outcome)
    world.ports.result_script = read
    await world.advance()
    world.elapse(1_250_000)
    if prior_entries is not None:
        await world.advance()
        world.elapse(int(check_retry_s * 1_000_000))

    class PageSaved(Exception):
        pass
    original = CaptureRepository.save_result_page
    def saved(repository, request, key, owned):
        receipt = original(repository, request, key, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=PageSaved())
    with monkeypatch.context() as boundary:
        boundary.setattr(CaptureRepository, "save_result_page", saved)
        try:
            await world.advance()
        except PageSaved:
            pass
        else:
            raise AssertionError("必须在真实结果页已提交后交付仓储专项")
    world.result_ticket = calls[-1]
    world.result_page = CaptureRepository().read_last_result_page(world.result_ticket, world.owned)
    assert world.result_page is not None
    world.actual_result_calls = tuple(calls)
    return world


async def store_result_end(world, *, finalized=False):
    """用原可靠页的实际返回结束仓储专项意图，不改写实际结果。"""
    from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.operations import OperationRepository
    registry = _REGISTRY.with_contract(EvidenceContract("results_returned", 1, "result", frozenset({
        "file_completion", "file_completion_source_page_event_id", "optional_preview_observations"})))
    actual = validate_outcome(world.result_ticket, world.result_page.page.outcome, registry)
    finish = AttemptFinish(world.result_ticket, actual, world.result_page.occurred_at,
        run_finish=RunFinish(RunOutcome.SUCCEEDED))
    key = new_operation_key()
    if finalized:
        return await world.database.write(lambda owned: CaptureRepository().finish_result_check(
            finish, None, key, owned, finalized_page=world.result_page.ref), key=key)
    return await world.database.write(lambda owned: OperationRepository().finish_attempt(finish, key, owned), key=key)


def output_set_proposal(world):
    """完整集合的独立静态提案，当前文件来源仍由正式有限读取端口核对。"""
    from camctl.history.events import load_event_registry
    from camctl.persistence.transaction import event_envelope, update_change
    template, context = finite_end_proposal(world)
    registry = load_event_registry()["events"]["RESULT_SET_CONFIRMED"]
    attempt, = (row for row in context.state_rows["operation_attempts"].values()
        if row["run_id"] == world.result_ticket.run_id)
    event = event_envelope(template.event_id, template.transaction_id, registry["id"],
        registry["branches"]["OUTPUTS_FINALIZED"]["reason"],
        (update_change("device_activities", world.activity_id,
            {"output_set_finalized_event_id": None}, {"output_set_finalized_event_id": template.event_id}),),
        world.result_page.occurred_at,
        evidence={"attempt_id": attempt["id"], "result_page_event_id": world.result_page.ref.event_id})
    return event, context
