"""C2 录像启动编排与真实仓储的组件集成测试。

真实 Q4 授予事务、O2 结果事务与驱动替身组合：授予、派发再检查、
确认保存及未派发保存的持久化事实在各边界保持原样。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.recording import (
    CaptureContext,
    GrantDecision,
    RecordingPhase,
    StartDispatch,
    start_recording,
)
from camctl.contracts.values import new_operation_key
from camctl.contracts.values import DurationMillis
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptStatus,
    AttemptTarget,
    OperationKind,
)
from camctl.operations.models import (
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import (
    GrantRequest,
    SchedulingRepository,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.scheduling.rules import LaunchWindow

from ..persistence.test_runtime import _create_valid_database
from ..support.database import business_database
from ..scheduling.test_resources import (
    _seed_activity,
    _seed_plan,
    _seed_record_action,
)


_NOW = 1_750_000_000_000_000
_ANCHOR_NS = 7_200_000_000_000
_DURATION_MS = DurationMillis(60_000)
_WINDOW = LaunchWindow(scheduled_at=_NOW - 1_000_000, window_end=_NOW + 1_000_000)

_START_OBSERVATION = DeviceObservation(
    type="start_confirmed", version=1, data={"activity_id": "1"}
)


class DriverStub:
    """驱动替身：返回编排的启动响应。"""

    def __init__(self, dispatch: StartDispatch) -> None:
        self.dispatch = dispatch
        self.calls: list = []

    async def start(self, ticket) -> StartDispatch:
        self.calls.append(ticket)
        return self.dispatch


class WallStub:
    def __init__(self, now_us: int) -> None:
        self.current_us = now_us

    def now_us(self) -> int:
        return self.current_us


class StoreAdapter:
    """把 Q4 授予与 O2 结果事务接到启动编排端口。"""

    def __init__(self, scheduling: SchedulingRepository, operations: OperationRepository, database) -> None:
        self._scheduling = scheduling
        self._operations = operations
        self._database = database

    async def grant(self, request) -> GrantDecision:
        grant_request = GrantRequest(
            device_id=request.device_id,
            action_id=request.action_id,
            window=request.window,
            trusted_wall_now=request.trusted_wall_now,
            config=request.config,
            occurred_at=request.trusted_wall_now,
        )
        key = new_operation_key()
        outcome = await self._database.write(
            lambda owned: self._scheduling.grant_start(grant_request, key, owned), key=key
        )
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            return GrantDecision(outcome="error", reason=str(outcome.kind))
        result = outcome.value
        if result.outcome.value != "granted":
            return GrantDecision(outcome="rejected", reason=result.reason)
        return GrantDecision(outcome="granted", ticket=result.ticket)

    async def finish(self, ticket, outcome: CallOutcome) -> None:
        attempt = AttemptFinish(
            ticket=ticket,
            outcome=_validated(ticket, outcome),
            occurred_at=_NOW,
        )
        key = new_operation_key()
        receipt = await self._database.write(
            lambda owned: self._operations.finish_attempt(attempt, key, owned), key=key
        )
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error

    async def finish_prevented(self, ticket, reason: str) -> None:
        outcome = CallOutcome(
            status=AttemptStatus.FAILED,
            error=ErrorValue(code=reason, stage="dispatch"),
            effect=EffectState.NO_EFFECT,
            settlement=Settlement(
                basis=SettlementBasis.NOT_DISPATCHED,
                evidence=EvidenceValue(type="dispatch_prevented", version=1, data={}),
            ),
            observations=(),
        )
        await self.finish(ticket, outcome)


def _validated(ticket, outcome: CallOutcome):
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    from camctl.operations.validation import validate_outcome

    registry = EvidenceRegistry(
        (
            EvidenceContract(
                type="start_confirmed",
                version=1,
                operation="control",
                fields=frozenset({"activity_id"}),
                identity_field="activity_id",
            ),
            EvidenceContract(
                type="operation_returned",
                version=1,
                operation="control",
                fields=frozenset(),
            ),
            EvidenceContract(
                type="dispatch_prevented",
                version=1,
                operation="control",
                fields=frozenset(),
            ),
        )
    )
    return validate_outcome(ticket, outcome, registry)


def _environment(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)",
        ("f" * 32,),
    )
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})),
    )
    _seed_plan(connection, 1)
    _seed_record_action(connection, 1, 1)
    _seed_activity(connection, 1)
    connection.commit()
    return owned


def _context(database, *, driver: DriverStub, wall: WallStub, canceled: bool = False):
    return CaptureContext(
        device_id="cam-1",
        action_id=1,
        window=_WINDOW,
        config=AttemptConfig(max_attempts=2, timeout_s=Decimal("10")),
        duration=_DURATION_MS,
        trusted_wall_now=_NOW,
        wall=wall,
        grants=StoreAdapter(SchedulingRepository(), OperationRepository(), database),
        driver=driver,
        finishes=StoreAdapter(SchedulingRepository(), OperationRepository(), database),
        canceled=canceled,
    )


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


pytestmark = pytest.mark.asyncio


async def test_confirmed_start_persists_facts_and_anchor(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    driver = DriverStub(
        StartDispatch(
            confirmed=True,
            anchor_ns=_ANCHOR_NS,
            observations=(_START_OBSERVATION,),
        )
    )
    try:
        async with business_database(tmp_path / "state.db") as database:
            step = await start_recording(
                _context(database, driver=driver, wall=WallStub(_NOW))
            )
            assert step.phase is RecordingPhase.START_CONFIRMED
            assert step.stop_target_ns == _ANCHOR_NS + 60_000 * 1_000_000

            attempt = _value(
                owned,
                "SELECT a.status, a.effect_state, a.result_json FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'start/1' AND a.attempt_no = 1",
            )
            assert attempt[0] == 2  # SUCCEEDED
            assert attempt[1] == 3  # CONFIRMED
            document = json.loads(attempt[2])
            assert document["settlement"]["basis"] == "observed"
            assert document["observations"][0]["type"] == "start_confirmed"
            used = _value(
                owned,
                "SELECT attempts_used FROM operation_runs WHERE responsibility_key = 'start/1'",
            )
            assert used[0] == 1
    finally:
        owned.connection.close()


async def test_prevented_dispatch_persists_without_driver_call(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    driver = DriverStub(
        StartDispatch(confirmed=True, anchor_ns=_ANCHOR_NS)
    )
    try:
        async with business_database(tmp_path / "state.db") as database:
            step = await start_recording(
                _context(database, driver=driver, wall=WallStub(_WINDOW.window_end + 1))
            )
            assert step.phase is RecordingPhase.DISPATCH_PREVENTED
            assert driver.calls == []

            attempt = _value(
                owned,
                "SELECT a.status, a.effect_state, a.result_json FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'start/1' AND a.attempt_no = 1",
            )
            assert attempt[0] == 3  # FAILED
            assert attempt[1] == 2  # NO_EFFECT
            document = json.loads(attempt[2])
            assert document["settlement"]["basis"] == "not_dispatched"
            used = _value(
                owned,
                "SELECT attempts_used FROM operation_runs WHERE responsibility_key = 'start/1'",
            )
            assert used[0] == 1  # 次数不退还
    finally:
        owned.connection.close()


async def test_late_confirmation_after_window_is_accepted(tmp_path: Path) -> None:
    """窗口内派发，窗口结束后取得确认：接受启动并沿原责任保存。"""
    owned = _environment(tmp_path)
    late_anchor = _ANCHOR_NS + 10_000_000_000
    wall = WallStub(_NOW)

    class LateDriver:
        async def start(self, ticket) -> StartDispatch:
            # 派发时仍在窗口内；确认在窗口结束之后到达。
            assert wall.now_us() <= _WINDOW.window_end
            wall.current_us = _WINDOW.window_end + 1
            return StartDispatch(
                confirmed=True,
                anchor_ns=late_anchor,
                observations=(_START_OBSERVATION,),
            )

    try:
        async with business_database(tmp_path / "state.db") as database:
            step = await start_recording(
                _context(database, driver=LateDriver(), wall=wall)
            )
            assert wall.now_us() > _WINDOW.window_end
            assert step.phase is RecordingPhase.START_CONFIRMED
            assert step.stop_target_ns == late_anchor + 60_000 * 1_000_000
            assert _value(owned,
                "SELECT a.status, a.effect_state FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id=r.id"
                " WHERE r.responsibility_key='start/1' AND a.attempt_no=1"
            ) == (2, 3)
    finally:
        owned.connection.close()
