"""保存消费者专项：正式装配、实际工具和会话数据库的共同准备。

会话集合只作为正式工厂的空输入，由实际操作产生内容。恢复仍交给
生产恢复回调；测试只观察公开保存申请、耐久历史和外部调用。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.flows import capture_flow, residual_flow, winddown_flow, cancel_flow
from camctl.capture.handlers import (
    resume_capture_completions, resume_canceled_recording_results, resume_file_observations,
)
from camctl.capture.media_flow import resume_media_saves, resume_prepared_internal_reads
from camctl.contracts.values import new_operation_key
from camctl.contracts.enums import enum_for
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.host_files.media import ProbeRequest, RepairRequest
from camctl.host_files.tasks import FileTaskExecutor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.session import SessionRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.clock import ClockCheckInput, check_clock, register_clock_guard
from camctl.session.service import run_session
from camctl.session.supervision import Supervisor

from .capture import capture_database
from .session import session_context
from ..bootstrap.test_media_assembly import _SessionDriver, _config, _tool
from ..bootstrap.test_read_execution_runtime import ReadRequests
from ..capture.media_retry_fixtures import _Catalog, _EVIDENCE
from ..capture.test_capture_contract import ResultsDouble
from ..capture.test_input_copy import _CONTENT, _NOW


class TransactionFault:
    """SQLite 真实提交前后或回滚后的响应丢失，不匹配业务 SQL。"""

    def __init__(self, connection, *, after_commit=None, rollback_unknown=False):
        self.connection = connection
        self.after_commit = after_commit
        self.rollback_unknown = rollback_unknown
        self.commit_calls = self.rollback_calls = 0

    def execute(self, sql, parameters=()):
        if sql == "COMMIT" and self.after_commit is not None:
            self.commit_calls += 1
            if self.after_commit:
                self.connection.execute(sql, parameters)
            raise sqlite3.OperationalError("实际 COMMIT 响应丢失")
        if sql == "ROLLBACK":
            self.rollback_calls += 1
            result = self.connection.execute(sql, parameters)
            if self.rollback_unknown:
                raise sqlite3.OperationalError("实际 ROLLBACK 响应丢失")
            return result
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class StorageGate:
    """在 SQLite 正式表/列权限边界拒绝原键核实或指定投影写入。"""

    def __init__(self, *, table=None, column=None, original_key=False, unknown=False,
                 operation=sqlite3.SQLITE_UPDATE):
        self.table, self.column = table, column
        self.operation = operation
        self.original_key, self.unknown = original_key, unknown
        self.hits = self.rollbacks = 0
        self.error = None
        self.receipts = []
        self.enabled = True

    def install(self, monkeypatch, operation_key):
        """实际保存 callback 可能已由公开操作持有；按公开端口的 key 注入。"""
        from camctl.persistence.business import BusinessDatabase
        actual_write = BusinessDatabase.write

        async def write(database, operation, *, key, **options):
            if not self.enabled or key != operation_key:
                return await actual_write(database, operation, key=key, **options)

            def guarded(owned):
                result = self.submit(lambda _request, _key, current: operation(current),
                    None, key, owned)
                self.receipts.append(result)
                return result

            return await actual_write(database, guarded, key=key, **options)

        monkeypatch.setattr(BusinessDatabase, "write", write)

    def submit(self, method, request, key, owned):
        def authorize(operation, table, column, _database, _trigger):
            rejected = ((self.original_key and operation == sqlite3.SQLITE_READ
                         and table == "history_transactions" and column == "operation_key")
                        or (not self.original_key and operation == self.operation
                            and table == self.table and (self.column is None or column == self.column)))
            if rejected:
                self.hits += 1
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        owned.connection.set_authorizer(authorize)
        fault = TransactionFault(owned.connection, rollback_unknown=self.unknown)
        try:
            receipt = method(request, key, replace(owned, connection=fault))
            self.error = receipt.error
            return receipt
        finally:
            self.rollbacks += fault.rollback_calls
            owned.connection.set_authorizer(None)


class ConsumerWorld:
    """同一 CLI 会话跨数据库连接保存；冷会话须另建本对象。"""

    def __init__(self, pipeline, resources, *, branch="repair_complete", duration="75.125", results=None,
                 executor_factory=FileTaskExecutor, collections=None, evidence=_EVIDENCE):
        self.owned, self.roots, self.source_id = pipeline
        self.path = Path(self.owned.connection.execute("PRAGMA database_list").fetchone()[2])
        self.resources = resources
        self.config = _config(self.roots.staging.parent)
        self.wall = [_NOW + 20]
        self.reader = ReadRequests(self.owned, _CONTENT)
        self.ends = []
        reader, ends = self.reader, self.ends
        self.results = ResultsDouble({}) if results is None else results
        from .session import ResultDriver
        result_driver = ResultDriver(self.results)

        class Driver(_SessionDriver):
            async def list_results(self, request, batch):
                return await result_driver.list_results(request, batch)

            async def open_read(self, source, offset, ticket, *, idle_timeout_s):
                session = await reader.open_read(source, offset, ticket, idle_timeout_s=idle_timeout_s)
                actual_wait = session.wait_stopped

                async def wait_stopped():
                    end = await actual_wait()
                    ends.append(end)
                    return end

                session.wait_stopped = wait_stopped
                return session

        self.driver = Driver(_CONTENT)
        self.entry = DriverEntry("camctl-adb", self.driver, DriverDeclaration(
            control_supported=True, stop_supported=True, query_supported=False,
            result_supported=True, read_supported=True, digest_supported=True,
            delete_supported=False), evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
        self.supervisor = Supervisor()
        self.executor = executor_factory(self.supervisor)
        self.tool_log = self.path.parent / "consumer-tools.log"
        probe = _tool(self.path.parent, "consumer-probe", (
            "from pathlib import Path\nimport json\n"
            f"with Path({str(self.tool_log)!r}).open('a') as log: log.write('probe\\n')\n"
            "print(json.dumps({'streams':[{'codec_type':'video'}],"
            f"'format':{{'duration':{duration!r}}}}}))\n"))
        repair = _tool(self.path.parent, "consumer-repair", (
            "from pathlib import Path\nimport sys\n"
            f"with Path({str(self.tool_log)!r}).open('a') as log: log.write('repair\\n')\n"
            + ("sys.exit(7)\n" if branch in ("decision", "repair_failed") else
               "Path(sys.argv[-1]).write_bytes(b'original complete repaired bytes')\n")))
        self.probe_request, self.repair_request = ProbeRequest(ffprobe=probe), RepairRequest(ffmpeg=repair)
        names = (
            "pending_media_results", "pending_read_results", "pending_read_business",
            "pending_read_ends", "continuing_read_tickets", "pending_segment_inspection_results",
            "pending_capture_completions", "pending_result_closes", "pending_file_observations",
            "pending_call_results")
        provided = {} if collections is None else collections
        if provided.keys() - set(names):
            raise ValueError("保存集合必须是正式拍摄工厂声明的会话参数")
        self.collections = {name: provided.get(name, {}) for name in names}
        self.factory = self.new_factory()
        self.database = self.runtime = None
        self.observers = []

    def new_factory(self, *, restricted=False, drivers=None):
        return session_capture_assembly(
            devices=self.config.devices, drivers=DriverRegistry((self.entry,)) if drivers is None else drivers,
            results=self.results, staging=Path(self.config.paths.staging),
            wait_config=execution_wait_config, wall_us=lambda: self.wall[0],
            monotonic_ns=lambda: 7_000_000_000, probe_request=self.probe_request,
            repair_request=self.repair_request, media_enabled=not restricted,
            file_executor=self.executor, **self.collections)

    async def start(self):
        self.database = await capture_database(self.owned)
        self.runtime = await self.factory(self.database, "cam-1")
        assert self.runtime is not None and self.runtime.media is not None
        return self

    def tools(self):
        return self.tool_log.read_text().splitlines() if self.tool_log.exists() else []

    async def reconnect(self):
        await self.settle_files()
        self.owned.connection.close()
        # restart_database 真实结束此前 worker；仅重开观察连接不构成此边界。
        self.database = await self.resources.restart_database(self.path)
        self.owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
        self.observers.append(self.owned)
        self.reader.owned = self.owned

    def prime_clock(self):
        """会话环境预置可信下界；后续快照包括此正式事务。"""
        register_clock_guard()
        previous = self.owned.connection.execute(
            "SELECT trusted_time_lower_bound FROM runtime_state").fetchone()[0]
        clock = SimpleNamespace(utc_micros=lambda: self.wall[0])
        self.session_time = self.wall[0]
        check = check_clock(ClockCheckInput(previous, 0, 0, 0), clock)
        if check.needs_bound_update:
            saved = SessionRepository().update_lower_bound(check, new_operation_key(), self.owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error

    async def replay(self, entrance, *, kind="read", failure=False, binding=None, drivers=None):
        """正式会话声明单一公开 flow；保存通过后在恢复端口交付处收场。

        此专项只验证所属 flow 的保存门槛；完整默认装配另有 flows=None 见证。
        """
        class SaveObserved(BaseException):
            """恢复端口已经交付；专项不继续申请新的业务资格。"""

        async def boundary():
            raise SaveObserved()

        async def reads(database):
            args = {name: self.collections[name] for name in (
                "pending_read_results", "pending_read_business", "pending_read_ends",
                "continuing_read_tickets", "pending_segment_inspection_results")}
            await resume_prepared_internal_reads(database, **args, occurred_at=lambda: self.wall[0])
            if binding is not None:
                from camctl.capture.media_flow import resume_binding_internal_read_ends
                await resume_binding_internal_read_ends(database, **args,
                    binding_check=binding, occurred_at=lambda: self.wall[0])
            if kind == "read":
                await boundary()

        async def media(database):
            await resume_media_saves(database, self.collections["pending_media_results"], executor=self.executor)
            if kind == "media":
                await boundary()

        async def files(database):
            await resume_file_observations(database,
                pending_file_observations=self.collections["pending_file_observations"],
                pending_call_results=self.collections["pending_call_results"])
            if kind == "files":
                await boundary()

        async def completions(database):
            await resume_capture_completions(database,
                pending_capture_completions=self.collections["pending_capture_completions"])
            await resume_canceled_recording_results(database,
                pending_capture_completions=self.collections["pending_capture_completions"])
            if kind == "completion":
                await boundary()

        # capture/residual/winddown 的公开媒体恢复端口同时接收共同拍摄收场。
        async def media_and_completions(database):
            await completions(database)
            await media(database)

        factory = self.new_factory(restricted=entrance.startswith("restricted"), drivers=drivers)
        if entrance.endswith("cancel"):
            flow = cancel_flow(staging=Path(self.config.paths.staging),
                ready=Path(self.config.paths.ready),
                processing=Path(self.config.paths.processing),
                unscheduled_only=entrance.startswith("restricted"),
                resume_capture_completions=completions, resume_media_results=media,
                resume_file_observations=files)
        elif entrance == "normal":
            flow = capture_flow(factory, resume_read_results=reads,
                resume_media_results=media_and_completions, resume_file_observations=files)
        elif entrance == "residual":
            flow = residual_flow(factory, resume_read_results=reads,
                resume_media_results=media_and_completions, resume_file_observations=files)
        else:
            flow = winddown_flow(capture_factory=factory, wait_cap_s=Decimal("6"),
                resume_read_results=reads, resume_media_results=media_and_completions,
                resume_file_observations=files)
        restricted = entrance.startswith("restricted")

        def clock_policy(connection):
            lower = connection.execute("SELECT trusted_time_lower_bound FROM runtime_state").fetchone()[0]
            return ClockCheckInput(lower, self.session_time + 1 if restricted else 0, 0, 0)

        context = session_context(self.path, clock=SimpleNamespace(utc_micros=lambda: self.session_time),
            catalog=_Catalog(), clock_policy=clock_policy,
            flows={} if restricted else {entrance: flow},
            restricted_flows={entrance: flow} if restricted else {})
        if kind == "round":
            async def one_round(current):
                await flow(current)
                settle = getattr(flow, "settle", None)
                if settle is not None:
                    await settle()
                await boundary()

            if restricted:
                context.restricted_flows = {entrance: one_round}
            else:
                context.flows = {entrance: one_round}
        try:
            outcome = await run_session(context, None)
        except SaveObserved:
            assert not failure, "故障保存不应进入后续业务入口"
            return
        assert failure and outcome.reason == "state_db_error", outcome
        return outcome

    async def settle_files(self):
        settled = await self.executor.drain_required()
        failures = [failure.exception or RuntimeError(failure.error) for failure in settled.failed]
        if failures:
            raise BaseExceptionGroup("实际文件责任收场失败", failures)

    async def close(self):
        original = sys.exception()
        errors = []
        try:
            await self.settle_files()
        except BaseException as error:
            errors.append(error)
        finally:
            observers, self.observers = self.observers, []
            for owned in observers:
                try:
                    owned.connection.close()
                except BaseException as error:
                    errors.append(error)
        if errors:
            if original is not None:
                errors.insert(0, original)
            raise BaseExceptionGroup("消费者测试世界与资源收场失败", errors)


async def consumer_world(pipeline, resources, **options):
    return await ConsumerWorld(pipeline, resources, **options).start()


async def accept_cancellation(world, *, occurred_at, request_id="2"):
    """实际文件输入与正式受理保存取消计划，尚不驱动取消生效。"""
    from datetime import datetime, timezone
    from .acceptance import accept_document
    from camctl.acceptance.service import AcceptanceContext, CommandMode
    from camctl.persistence.repositories.acceptance import AcceptanceRepository

    instant = datetime.fromtimestamp(occurred_at // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = await accept_document({"request_id": request_id, "created_at": instant,
        "name": "取消原媒体动作", "actions": [{"name": "取消", "type": "cancel_task",
        "params": {"target": {"action_instance_id": "1"}}}]},
        AcceptanceContext(CommandMode.RUN, _Catalog(), AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: occurred_at)), world.path)
    assert accepted.plan_id is not None, accepted
    origin = world.owned.connection.execute("SELECT MAX(id) FROM actions WHERE type=6").fetchone()[0]
    return origin


async def apply_cancellation(world, terminal):
    """正式受理取消；按用例前置决定是否生效及保存目标终态。"""
    origin = await accept_cancellation(world, occurred_at=_NOW + 10)
    if terminal is None:
        assert world.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (2, 0)
        return origin
    await cancel_flow(staging=Path(world.config.paths.staging),
        ready=Path(world.config.paths.ready),
        processing=Path(world.config.paths.processing))(
            SimpleNamespace(database=world.database, clock=SimpleNamespace(utc_micros=lambda: _NOW + 10)))
    from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
    if terminal and world.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (2, 1):
        key = new_operation_key()
        ended = await world.database.write(lambda owned: CaptureRepository().finish_canceled_capture(
            FinishCanceledCapture(1, _NOW + 10), key, owned), key=key)
        assert ended.kind is DbOutcomeKind.COMPLETED, ended.error
    assert world.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (6 if terminal else 2, 1)
    return origin


async def cancel_original(world):
    return await apply_cancellation(world, terminal=True)


class FirstProbeUnavailableExecutor(FileTaskExecutor):
    """首个真实媒体任务未取得工具结果；下一次仍由正式执行器启动。"""

    def __init__(self, supervisor):
        super().__init__(supervisor)
        self.checkpoints = []

    async def run_async_file_task(self, task, owner):
        if task.stage == "media_probe" and not self.checkpoints:
            async def unavailable(_control):
                self.checkpoints.append(task.task_id)
                raise OSError("实际工具启动前执行端口暂不可用")
            return await super().run_async_file_task(replace(task, body=unavailable), owner)
        return await super().run_async_file_task(task, owner)


async def recording_world(pipeline, resources, *, failed_read=False):
    """实际RESULTS与READ保存后，工具尚无结果，录像原用途仍在等待。"""
    from camctl.capture.handlers import capture_handler
    from ..capture.test_capture_contract import _entry

    results = ResultsDouble({1: (replace(_entry("original-recording", size=len(_CONTENT)),
        locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
    world = await consumer_world(pipeline, resources, duration="61", results=results,
        executor_factory=FirstProbeUnavailableExecutor)
    world.reader.fail = failed_read
    await capture_handler("camera_record")(1, world.runtime)
    assert len(world.reader.requests) == len(world.ends) == 1 and results.calls == [1]
    assert world.tools() == []
    assert world.owned.connection.execute(
        "SELECT status,retry_wait_required FROM operation_runs WHERE responsibility_key='results/1'").fetchone() == (2, 1)
    return world


def save_legacy_capture_cancel(world, *, occurred_at):
    """旧持久历史输入：动作终态已经保存，原RESULTS用途仍独立开放。

    规范事件由公开仓储在独立副本产生；动作/计划事件须通过当前完整
    守卫和实际事务。此输入不表示当前复合完成入口会留下这种状态。
    """
    from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
    from camctl.persistence.transaction import commit_operation
    from .output_planning import capture_operation, EventSequence

    key = new_operation_key()
    proposal = capture_operation(world.owned, lambda owned: CaptureRepository().finish_canceled_capture(
        FinishCanceledCapture(1, occurred_at), key, owned))
    events = tuple(event for event in proposal.events
        if all(row.table != "operation_runs" for row in event.rows))
    assert events and any(row.table == "actions" for event in events for row in event.rows)
    receipt = commit_operation(EventSequence(events, proposal), key, world.owned)
    assert receipt.kind == "completed", receipt.error
    return key


async def held_default_read(pipeline, resources, monkeypatch, stage, after_commit):
    """在完整读取结束依据或原尝试结果的真实事务处中断。"""
    from camctl.capture.media_flow import run_recording_media
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.persistence.repositories import read_end
    from camctl.persistence.transaction import saved_transaction_events

    world = ConsumerWorld(pipeline, resources)
    if stage == "end":
        world.entry = replace(world.entry, declaration=replace(world.entry.declaration, digest_supported=False))
    world.factory = world.new_factory()
    await world.start()
    from camctl.persistence.repositories.outputs import OutputsRepository

    ends, finishes, verifications = [], [], []
    actual_end, actual_finish = read_end.save_read_end, OperationRepository.finish_attempt
    actual_verification = OutputsRepository.save_verification

    def submit(method, request, key, owned, inputs, target):
        inputs.append((request, key))
        if stage == target and len(inputs) == 1:
            fault = TransactionFault(owned.connection, after_commit=after_commit)
            result = method(request, key, replace(owned, connection=fault))
            assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            return result
        return method(request, key, owned)

    def save_end(request, key, owned):
        return submit(actual_end, request, key, owned, ends, "end")

    def finish(repository, request, key, owned):
        if request.ticket.operation != "read":
            return actual_finish(repository, request, key, owned)
        return submit(lambda *args: actual_finish(repository, *args), request, key, owned, finishes, "finish")

    def verification(repository, request, key, owned):
        if stage == "verification" and not verifications:
            state = owned.connection.execute(
                "SELECT read_end_event_id,source_sha256,slot_device_id FROM file_copies WHERE id=?",
                (request.copy_id,)).fetchone()
            assert state[0] is not None and state[1:] == (None, "cam-1")
            assert saved_transaction_events(owned.connection, ends[0][1]) is not None
        return submit(lambda *args: actual_verification(repository, *args), request, key, owned,
            verifications, "verification")

    monkeypatch.setattr(read_end, "save_read_end", save_end)
    monkeypatch.setattr(OperationRepository, "finish_attempt", finish)
    monkeypatch.setattr(OutputsRepository, "save_verification", verification)
    with pytest.raises(ConsistencyError):
        await run_recording_media(world.runtime.media, 1, 1, world.source_id)
    assert len(world.reader.requests) == len(world.ends) == 1
    ticket = world.reader.requests[0][0]
    assert world.ends[0].stopped and world.ends[0].error is None and world.ends[0].bytes_read == len(_CONTENT)
    assert [name for name, _ in world.driver.calls] == ([] if stage == "end" else ["digest"])
    inputs = {"end": ends, "finish": finishes, "verification": verifications}[stage]
    assert len(inputs) == 1
    assert (inputs[0][0].copy_id == int(ticket.target_id) if stage == "verification"
            else inputs[0][0].ticket == ticket)
    assert inputs[0][0].occurred_at == world.wall[0]
    await world.reconnect()
    assert (saved_transaction_events(world.owned.connection, inputs[0][1]) is not None) is after_commit
    expected_slot = "cam-1" if stage in ("end", "verification") and not after_commit else None
    assert world.owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
        (int(ticket.target_id),)).fetchone() == (expected_slot,)
    world.wall[0] += 1000
    world.prime_clock()
    return world, ticket, ends, finishes, verifications
