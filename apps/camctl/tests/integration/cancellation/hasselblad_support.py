"""Hasselblad 取消边界共用真实受理、处理器和持久事实准备。"""

import asyncio
from decimal import Decimal
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from camctl.acceptance.service import AcceptanceContext, CommandMode
from camctl.bootstrap.flows import cancel_flow
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.catalog import build_catalog, DriverDefinitions
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.host_files.models import BoundDirectories
from camctl.operations.attempts import AttemptConfig, AttemptFinish
from camctl.operations.models import (CallOutcome, AttemptStatus, EffectState, ErrorValue,
    Settlement, SettlementBasis, EvidenceValue)
from camctl.operations.process import ToolStartError
from camctl.operations.validation import validate_outcome
from camctl.devices.evidence import EvidenceRegistry
from camctl.devices.start_entry import PREVENTED
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository, ActivityObservationSave
from camctl.persistence.repositories.scheduling import SchedulingRepository, GrantRequest
from camctl.persistence.transaction import row_facts
from camctl.scheduling.rules import LaunchWindow

from ..capture.test_host_photo_flow import _world, _input_time, _ManagedCaptureTransport
from ..support.acceptance import accept_document
from ..support.database import database_path


class HeldPhotoTransport(_ManagedCaptureTransport):
    """准备命令正常返回；只在实际 capture 工具入口等待。"""

    def __init__(self, *, fail=False):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = fail

    async def run(self, spec, stop):
        if spec.argv[3] == "capture":
            self.entered.set()
            await self.release.wait()
            if self.fail:
                self.calls.append(spec)
                raise ToolStartError("拍照工具尚未创建进程")
        return await super().run(spec, stop)


class ExpiringControl:
    """正式控制端口在取得请求后推进受控时钟，驱动自行检查派发窗口。"""

    def __init__(self, driver, clock):
        self.driver, self.clock = driver, clock

    def __getattr__(self, name):
        return getattr(self.driver, name)

    async def control(self, request):
        self.clock[0] += 5_000_001
        return await self.driver.control(request)


def action(owned, identity):
    return row_facts(owned.connection, "actions", identity)


def grant_unknown(owned, runtime, identity=1):
    """冷会话只有已提交 START 意图，没有该会话的入口许可。"""
    source = action(owned, identity)
    now = runtime.wall_us()
    saved = SchedulingRepository().grant_start(GrantRequest(
        source["device_id"], identity,
        LaunchWindow(source["scheduled_at"], source["scheduled_at"] + source["max_delay_ms"] * 1000),
        now, AttemptConfig(1, Decimal(180)), now), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.ticket is not None
    return saved.value.ticket


def save_blocked(owned, runtime):
    """存储契约夹具：原 START 票据可靠保存未派发依据与活动观察。"""
    ticket = grant_unknown(owned, runtime)
    outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        settlement=Settlement(SettlementBasis.NOT_DISPATCHED, EvidenceValue.of(PREVENTED)),
        error=ErrorValue("canceled", "dispatch"))
    saved = CaptureRepository().finish_start_result(
        AttemptFinish(ticket, validate_outcome(ticket, outcome, EvidenceRegistry((PREVENTED,))),
            runtime.wall_us()),
        ActivityObservationSave(1, runtime.wall_us(), dispatch_state=1),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error


async def world(tmp_path, state, monkeypatch=None, *, photo_group=None):
    transport = HeldPhotoTransport(fail=state == "no_effect")
    from ..capture.test_host_photo_flow import _NOW
    clock = [_NOW]
    def driver_factory(cfg):
        driver = HasselbladDriver(cfg.devices, BoundDirectories(Path(cfg.paths.staging)),
            transport=transport, monotonic_ns=lambda: 1)
        return ExpiringControl(driver, clock) if state == "zero_tools" else driver
    cfg, owned, driver, runtime, clock = await _world(
        tmp_path, extra_actions=[preview()], cancel_action=True, photo_group=photo_group,
        driver_factory=driver_factory, wall=clock)
    task = None
    if state == "entered":
        task = asyncio.create_task(capture_handler("camera_take_photo")(1, runtime))
        await asyncio.wait_for(transport.entered.wait(), 1)
    elif state == "terminal":
        transport.release.set()
        await capture_handler("camera_take_photo")(1, runtime)
    elif state in ("no_effect", "zero_tools"):
        # 真实结果先提交；随后结束动作的真实写事务受拒，保留可检查的 RUNNING 来源。
        await runtime.database.read(lambda db: db.connection.set_authorizer(
            lambda operation, table, column, *_: sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_UPDATE and table == "actions" and column == "status"
            else sqlite3.SQLITE_OK))
        transport.release.set()
        try:
            with pytest.raises(ConsistencyError, match="事务|保存|完成"):
                await capture_handler("camera_take_photo")(1, runtime)
        finally:
            await runtime.database.read(lambda db: db.connection.set_authorizer(None))
        assert action(owned, 1)["status"] == 2
    elif state == "unknown":
        grant_unknown(owned, runtime)
    elif state == "blocked":
        save_blocked(owned, runtime)
    elif state != "ready":
        raise ValueError(state)
    repository = CancellationRepository()
    from camctl.cancellation.models import StartCancelAction
    started = repository.start_cancel_action(StartCancelAction(3, runtime.wall_us()), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    return cfg, owned, driver, runtime, transport, task, repository


def preview():
    return {"name": "预览", "type": "obtain_action_outputs", "scheduled_at": _input_time(),
        "params": {"source": {"action_name": "单张 RAW"}, "filter": "preview", "purpose": "auto_preview"}}


async def accept_origin(cfg, owned, runtime, target):
    catalog = build_catalog(cfg, DriverDefinitions({DRIVER_ID: driver_definition()}))
    await accept_document({"request_id": "99", "created_at": _input_time(), "name": "固定边界",
        "actions": [{"name": "取消", "type": "cancel_task", "params": {"target": target}}]},
        AcceptanceContext(CommandMode.RUN, catalog, AcceptanceRepository(),
            SimpleNamespace(utc_micros=runtime.wall_us)), database_path(owned))
    identity = owned.connection.execute("SELECT MAX(id) FROM actions WHERE type=6").fetchone()[0]
    from camctl.cancellation.models import StartCancelAction
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(identity, runtime.wall_us()), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    return identity, repository


async def cancel(cfg, runtime):
    await cancel_flow(staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing),
        start_entries=runtime.start_entries)(SimpleNamespace(database=runtime.database,
            clock=SimpleNamespace(utc_micros=runtime.wall_us)))


async def close(owned, transport, task):
    transport.release.set()
    if task:
        await task
    owned.connection.close()


def fixed_members(owned, origin):
    """读取实际固定事件创建时的成员，不用后来 apply 覆盖的当前效果代替。"""
    from camctl.history.decoding import decode_event_row
    decisions = []
    for stored in owned.connection.execute(
        "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json "
        "FROM history_events WHERE event_type=4 ORDER BY id"):
        event = decode_event_row(stored)
        members = [row.after.values for row in event.rows if row.table == "cancel_items"
            and not row.before.exists and row.after.values["action_id"] == origin]
        if members:
            decisions.append(sorted((row["target_action_id"], row["selection_basis"],
                row["cancellation_effect"]) for row in members))
    assert len(decisions) == 1, decisions
    return decisions[0]


def fixed_key(owned, origin):
    """取得实际原固定事务的键；后续 apply 的事务不能替代它。"""
    from camctl.history.decoding import decode_event_row
    from camctl.contracts.values import OperationKey
    transactions = []
    for stored in owned.connection.execute(
        "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json "
        "FROM history_events WHERE event_type=4 ORDER BY id"):
        event = decode_event_row(stored)
        if any(row.table == "cancel_items" and not row.before.exists
               and row.after.values["action_id"] == origin for row in event.rows):
            transactions.append(event.transaction_id)
    assert len(transactions) == 1, transactions
    return OperationKey(owned.connection.execute(
        "SELECT operation_key FROM history_transactions WHERE id=?", transactions).fetchone()[0])


async def interrupt_grant_delivery(owned, runtime, monkeypatch):
    """实际授予已提交并绑定许可后，由数据库公开回执交付原取消对象。"""
    from dataclasses import replace
    from camctl.devices.start_entry import StartEntryPermit
    saved = runtime.scheduling.grant_start
    bind = StartEntryPermit.bind
    permits = []
    control = asyncio.CancelledError("授予已完成，调用者在交付时取消")
    def observe(permit, database_id, ticket):
        result = bind(permit, database_id, ticket)
        permits.append(permit)
        return result
    def interrupted(request, key, owner):
        outcome = saved(request, key, owner)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        return replace(outcome, observer_error=control)
    with monkeypatch.context() as patch:
        patch.setattr(StartEntryPermit, "bind", observe)
        patch.setattr(runtime.scheduling, "grant_start", interrupted)
        with pytest.raises(asyncio.CancelledError) as raised:
            await capture_handler("camera_take_photo")(1, runtime)
        assert raised.value is control
    permit, = permits
    assert permit.ticket is not None
    assert owned.connection.execute(
        "SELECT attempt.status,attempt.result_json FROM operation_attempts AS attempt "
        "JOIN operation_runs AS run ON run.id=attempt.run_id WHERE run.kind=1").fetchall()==[(1,None)]
    return permit
