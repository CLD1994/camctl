"""默认三入口在业务筛选前，用新连接核实原 READ 完整申请及读取机会释放。"""

from dataclasses import replace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import capture_assembly, lifecycle
from camctl.capture.media_flow import run_recording_media
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.history.events import business_columns
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts, saved_transaction_events
from camctl.session.outcome import SessionOutcome

from ..capture.media_retry_fixtures import _Catalog, _EVIDENCE, media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble
from ..capture.test_input_copy import _CONTENT, _NOW
from .test_binding_transactions import _CommitFailure
from .test_media_assembly import _SessionDriver, _config
from .test_media_saved_result_consumers import _cancel_and_end_original
from .test_read_execution_runtime import ReadRequests


pytestmark = pytest.mark.asyncio


class _NoNewMedia:
    async def probe(self, input):
        raise AssertionError("接手原 READ 保存责任不能新增媒体检查")

    async def repair(self, input, output, *, trim_s):
        raise AssertionError("接手原 READ 保存责任不能新增媒体修复")


class _BeforeBusinessCandidates(Exception):
    """保存门禁已通过；本用例在新业务资格判定前结束该轮。"""


class _CandidateClock:
    def __init__(self, check):
        self.check = check

    def utc_micros(self):
        self.check()
        raise _BeforeBusinessCandidates


class _WinddownQueryBoundary:
    """透传真实 SQLite；在受限入口查询新业务候选前检查保存门禁。"""

    def __init__(self, connection, check):
        self.connection, self.check = connection, check

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT id FROM actions WHERE type = 2 AND status = 2"):
            self.check()
            raise _BeforeBusinessCandidates
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _ReadCommitFailure(_CommitFailure):
    """记录真正命中的 COMMIT；提交前故障保留 UNKNOWN 原连接的事务。"""

    def __init__(self, connection, after):
        super().__init__(connection, after)
        self.commit_calls = 0

    def execute(self, sql, parameters=()):
        if sql == "COMMIT":
            self.commit_calls += 1
        return super().execute(sql, parameters)


async def _default_world(pipeline, monkeypatch):
    """只隔离主会话循环和设备／工具；保留默认三工厂及真实 flow。"""
    from camctl.devices.drivers import runtime as driver_runtime

    owned, roots, _source_id = pipeline
    reader, ends = ReadRequests(owned, _CONTENT), []
    driver = _SessionDriver(_CONTENT)

    async def open_read(source, offset, ticket, *, idle_timeout_s):
        session = await reader.open_read(source, offset, ticket, idle_timeout_s=idle_timeout_s)
        original_wait = session.wait_stopped

        async def wait_stopped():
            end = await original_wait()
            ends.append(end)
            return end

        session.wait_stopped = wait_stopped
        return session

    driver.open_read = open_read
    registry = DriverRegistry((DriverEntry("camctl-adb", driver, DriverDeclaration(
        control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=True, digest_supported=True, delete_supported=False),
        _EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    monkeypatch.setattr(driver_runtime, "current_registry", lambda: registry)
    factories, contexts, factory_calls = [], [], []
    wall = [_NOW + 20]
    original_factory = capture_assembly.session_capture_assembly

    def capture_factory(**kwargs):
        actual = original_factory(**kwargs, results=ResultsDouble({}),
            wall_us=lambda: wall[0], monotonic_ns=lambda: 7_000_000_000)

        def factory(current, device_id):
            factory_calls.append((current, device_id))
            runtime = actual(current, device_id)
            if runtime is not None and runtime.media is not None:
                runtime.media.tools = _NoNewMedia()
            return runtime

        factories.append(factory)
        return factory

    async def session(context, _source):
        contexts.append(context)
        return SessionOutcome(succeeded=True)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", capture_factory)
    monkeypatch.setattr(lifecycle, "run_session", create_autospec(lifecycle.run_session, side_effect=session))
    deps = lifecycle.build_runtime(CommandMode.RUN, _config(roots.staging.parent), catalog=_Catalog())
    try:
        assert deps.startup_error is None
        assert (await lifecycle.execute_command(deps, None)).succeeded
        assert len(factories) == 3 and len(contexts) == 1
        # 本夹具核验每轮业务结果；等待生产拥有者交付，之后才能检查状态。
        capture = contexts[0].flows["scheduling"]
        async def advance_capture(context):
            await capture(context)
            await capture.settle()
        contexts[0].flows["scheduling"] = advance_capture
        return deps, factories, contexts[0], reader, driver, ends, wall, factory_calls
    except BaseException:
        lifecycle.close_runtime(deps)
        raise


def _snapshot(owned, table, row_id):
    facts = row_facts(owned.connection, table, row_id)
    return {name: facts[name] for name in business_columns(table)}


async def _held_read(pipeline, monkeypatch, stage, after_commit):
    owned, roots, source_id = pipeline
    world = await _default_world(pipeline, monkeypatch)
    deps, factories, context, reader, driver, ends, wall, factory_calls = world
    finishes, slots = [], []
    actual_finish, actual_slot = OperationRepository.finish_attempt, OutputsRepository.release_read_slot

    def finish(repository, command, key, current):
        if command.ticket.operation != "read":
            return actual_finish(repository, command, key, current)
        finishes.append((command, key))
        if stage == "finish" and len(finishes) == 1:
            faulty = _ReadCommitFailure(current.connection, after_commit)
            result = actual_finish(repository, command, key, replace(current, connection=faulty))
            assert result.kind is DbOutcomeKind.UNKNOWN, result
            assert faulty.commit_calls == 1
            assert current.connection.in_transaction is not after_commit
            return result
        return actual_finish(repository, command, key, current)

    def slot(repository, command, key, current):
        slots.append((command, key))
        if stage == "slot" and len(slots) == 1:
            faulty = _ReadCommitFailure(current.connection, after_commit)
            result = actual_slot(repository, command, key, replace(current, connection=faulty))
            assert result.kind is DbOutcomeKind.UNKNOWN, result
            assert faulty.commit_calls == 1
            assert current.connection.in_transaction is not after_commit
            return result
        return actual_slot(repository, command, key, current)

    monkeypatch.setattr(OperationRepository, "finish_attempt", finish)
    monkeypatch.setattr(OutputsRepository, "release_read_slot", slot)
    reopened = None
    try:
        runtime = factories[0](owned, "cam-1")
        flow = runtime.media
        assert flow is not None
        with pytest.raises(ConsistencyError):
            await run_recording_media(flow, 1, 1, source_id)
        assert len(reader.requests) == 1 and len(finishes) == 1
        ticket = reader.requests[0][0]
        assert ticket is not None and reader.requests[0][2][0:3] == (1, None, 1)
        assert ends and all(end.stopped is True and end.error is None
                            and end.bytes_read == len(_CONTENT) for end in ends)
        assert finishes[0][0].ticket == ticket and finishes[0][0].occurred_at == wall[0]
        assert owned.connection.execute(
            "SELECT committed_bytes,source_size,verification_state FROM file_copies WHERE id=?",
            (int(ticket.target_id),)).fetchone() == (
                len(_CONTENT), len(_CONTENT), int(enum_for("file_copies.verification_state").MATCHED))
        inputs = finishes if stage == "finish" else slots
        assert len(inputs) == 1
        # 持有物来自实际任务，没有向另一个工厂手动复制任何 READ 集合。
        if stage == "finish":
            pending = flow.pending_read_results[(ticket.run_id, ticket.attempt_id)]
            assert (pending.finish, pending.key) == inputs[0]
        else:
            pending = flow.pending_read_business[(int(ticket.target_id), "release_read_slot")]
            assert (pending.request, pending.key) == inputs[0]
            assert not flow.pending_read_results
        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == owned.metadata
        # UNKNOWN 原连接可以看见自己的未提交行；只在关闭后从新连接核可靠 F。
        assert (saved_transaction_events(reopened.connection, inputs[0][1]) is not None) is after_commit
        assert reopened.connection.execute(
            "SELECT status FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (
                int(enum_for("operation_attempts.status").RUNNING)
                if stage == "finish" and not after_commit
                else int(enum_for("operation_attempts.status").SUCCEEDED),)
        assert reopened.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
            (int(ticket.target_id),)).fetchone() == (None if stage == "slot" and after_commit else "cam-1",)
        wall[0] += 1000
        return world, reopened, flow, ticket, finishes, slots
    except BaseException:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)
        raise


def _read_saved(owned, ticket, finishes, slots, stage):
    assert len(finishes) == (2 if stage == "finish" else 1), "原 READ Finish 必须先按原 key 核实"
    if stage == "finish":
        assert finishes[1] == finishes[0]
    assert len(slots) == (1 if stage == "finish" else 2), "原 slot 申请必须沿原完整输入核实"
    if stage == "slot":
        assert slots[1] == slots[0]
    assert slots[0][0].occurred_at == finishes[0][0].occurred_at
    assert owned.connection.execute(
        "SELECT t.status,t.error_json,r.status,r.attempts_used FROM operation_attempts t"
        " JOIN operation_runs r ON r.id=t.run_id WHERE r.id=? AND t.attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone() == (
            int(enum_for("operation_attempts.status").SUCCEEDED), None,
            int(enum_for("operation_runs.status").SUCCEEDED), ticket.attempt_id)
    assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
        (int(ticket.target_id),)).fetchone() == (None,)
    for _command, key in (finishes[0], slots[0]):
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("stage", ["finish", "slot"])
async def test_default_entry_saves_read_before_business_candidates(pipeline, monkeypatch, entrance, after_commit, stage):
    world, reopened, original_flow, ticket, finishes, slots = await _held_read(pipeline, monkeypatch, stage, after_commit)
    deps, _factories, context, reader, driver, _ends, _wall, factory_calls = world
    before_action = _snapshot(reopened, "actions", 1)
    before_processing = _snapshot(reopened, "recording_processing", 1)
    before_copy = _snapshot(reopened, "file_copies", int(ticket.target_id))
    before_calls = tuple(driver.calls)
    original_open, opened = context.open_connection, []

    def saved():
        _read_saved(reopened, ticket, finishes, slots, stage)

    def open_connection():
        current = original_open()
        assert current.metadata == reopened.metadata and current.connection is not reopened.connection
        opened.append(current)
        return replace(current, connection=_WinddownQueryBoundary(current.connection, saved)) \
            if entrance == "restricted" else current

    context.open_connection = open_connection
    if entrance == "normal":
        context.clock = _CandidateClock(saved)
    try:
        if entrance == "residual":
            await context.flows["residual"](context)
        else:
            selected = context.flows["scheduling"] if entrance == "normal" else context.restricted_flows["winddown"]
            with pytest.raises(_BeforeBusinessCandidates):
                await selected(context)
        assert len(opened) == 1 and len(factory_calls) == 1
        _read_saved(reopened, ticket, finishes, slots, stage)
        assert not original_flow.pending_read_results and not original_flow.pending_read_business
        assert len(reader.requests) == 1 and tuple(driver.calls) == before_calls
        assert _snapshot(reopened, "actions", 1) == before_action
        assert _snapshot(reopened, "recording_processing", 1) == before_processing
        after_copy = _snapshot(reopened, "file_copies", int(ticket.target_id))
        assert {**after_copy, "slot_device_id": before_copy["slot_device_id"]} == before_copy
    finally:
        reopened.connection.close()
        lifecycle.close_runtime(deps)


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
async def test_default_entry_confirms_committed_read_child_after_canceled_terminal(pipeline, monkeypatch, entrance):
    world, reopened, original_flow, ticket, finishes, slots = await _held_read(pipeline, monkeypatch, "slot", True)
    deps, _factories, context, reader, driver, _ends, _wall, factory_calls = world
    try:
        # 原 Finish 和 child 均已经提交，此后的公开取消不改写原组。
        _cancel_and_end_original(reopened)
        assert reopened.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (
            int(enum_for("actions.status").CANCELED), 1)
        before = {table: _snapshot(reopened, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1), ("file_copies", int(ticket.target_id)),
            ("operation_runs", ticket.run_id))}
        history_count = reopened.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        before_calls = tuple(driver.calls)
        original_open, opened = context.open_connection, []

        def open_connection():
            current = original_open()
            assert current.metadata == reopened.metadata and current.connection is not reopened.connection
            opened.append(current)
            return current

        context.open_connection = open_connection
        selected = (context.flows["scheduling"] if entrance == "normal" else
                    context.flows["residual"] if entrance == "residual" else context.restricted_flows["winddown"])
        await selected(context)
        assert len(opened) == 1 and len(factory_calls) == 1
        _read_saved(reopened, ticket, finishes, slots, "slot")
        assert not original_flow.pending_read_results and not original_flow.pending_read_business
        assert len(reader.requests) == 1 and tuple(driver.calls) == before_calls
        assert before == {table: _snapshot(reopened, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1), ("file_copies", int(ticket.target_id)),
            ("operation_runs", ticket.run_id))}
        assert reopened.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == history_count
    finally:
        reopened.connection.close()
        lifecycle.close_runtime(deps)
