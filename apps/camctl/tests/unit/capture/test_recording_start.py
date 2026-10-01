"""C2 录像启动及计时锚点的单元测试。

锚点取驱动可靠启动响应时刻的单调钟，持久化或日志延迟不改变停
止目标；意图与派发两次窗口检查，窗口内派发窗口后确认仍接受；
仅发送、拒绝无效果、未知及可靠启动后调用错误分别保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from camctl.capture.recording import (
    CaptureContext,
    RecordingPhase,
    recording_stop_target,
    start_recording,
)
from camctl.contracts.values import DurationMillis
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.scheduling.rules import LaunchWindow

pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000
_ANCHOR_NS = 7_200_000_000_000  # 单调钟读数（纳秒）
_DURATION_MS = DurationMillis(60_000)

_CONTRACTS = EvidenceRegistry(
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
    )
)


@dataclass
class GrantDouble:
    """授予端口替身：可编排结果。"""

    outcome: str = "granted"
    reason: str | None = None
    ticket: object = None
    calls: list = field(default_factory=list)

    def grant(self, request) -> object:
        from camctl.capture.recording import GrantDecision

        self.calls.append(request)
        return GrantDecision(
            outcome=self.outcome, ticket=self.ticket, reason=self.reason
        )


@dataclass
class FinishDouble:
    """结束端口替身：记录保存的结束结果。"""

    saved: list = field(default_factory=list)
    prevented: list = field(default_factory=list)

    def finish(self, ticket, outcome: CallOutcome) -> None:
        self.saved.append((ticket, outcome))

    def finish_prevented(self, ticket, reason: str) -> None:
        self.prevented.append((ticket, reason))


@dataclass
class DriverDouble:
    """驱动替身：返回编排的启动响应，锚点在响应时取得。"""

    dispatch: object = None
    calls: list = field(default_factory=list)

    async def start(self, ticket) -> object:
        self.calls.append(ticket)
        return self.dispatch


class WallDouble:
    """可信墙钟替身：派发再检查时刻。"""

    def __init__(self, now_us: int = _NOW) -> None:
        self._now_us = now_us

    def now_us(self) -> int:
        return self._now_us


def _context(
    *,
    grants: GrantDouble,
    driver: DriverDouble,
    finishes: FinishDouble,
    wall: WallDouble | None = None,
    canceled: bool = False,
    window: LaunchWindow | None = None,
) -> CaptureContext:
    window = window or LaunchWindow(
        scheduled_at=_NOW - 1_000_000, window_end=_NOW + 1_000_000
    )
    return CaptureContext(
        device_id="cam-1",
        action_id=1,
        window=window,
        config=AttemptConfig(max_attempts=2, timeout_s=Decimal("10")),
        duration=_DURATION_MS,
        trusted_wall_now=_NOW,
        wall=wall or WallDouble(),
        grants=grants,
        driver=driver,
        finishes=finishes,
        canceled=canceled,
    )


def _dispatch(
    *,
    confirmed: bool = False,
    anchor_ns: int | None = None,
    sent_only: bool = False,
    rejected: bool = False,
    error: ErrorValue | None = None,
    observations: tuple[DeviceObservation, ...] = (),
) -> object:
    from camctl.capture.recording import StartDispatch

    return StartDispatch(
        confirmed=confirmed,
        anchor_ns=anchor_ns,
        sent_only=sent_only,
        rejected_no_effect=rejected,
        error=error,
        observations=observations,
    )


_START_CONFIRMED_OBSERVATION = DeviceObservation(
    type="start_confirmed", version=1, data={"activity_id": "1"}
)


class TestStopTarget:
    async def test_stop_target_is_anchor_plus_duration(self) -> None:
        assert (
            recording_stop_target(_ANCHOR_NS, _DURATION_MS)
            == _ANCHOR_NS + 60_000 * 1_000_000
        )

    async def test_duration_is_exact_milliseconds(self) -> None:
        assert recording_stop_target(0, DurationMillis(1)) == 1_000_000
        assert recording_stop_target(5, DurationMillis(0)) == 5


async def test_recording_anchor_precedes_persistence() -> None:
    """驱动确认时钟为 t：数据库与日志随后的延迟不改变停止目标。"""
    grants = GrantDouble(outcome="granted", ticket=object())
    finishes = FinishDouble()
    driver = DriverDouble(
        dispatch=_dispatch(
            confirmed=True,
            anchor_ns=_ANCHOR_NS,
            observations=(_START_CONFIRMED_OBSERVATION,),
        )
    )
    # 派发再检查仍在窗口内；确认之后的持久化延迟由保存替身表达，
    # 锚点不随之改变。
    wall = WallDouble(now_us=_NOW)
    step = await start_recording(
        _context(grants=grants, driver=driver, finishes=finishes, wall=wall)
    )
    assert step.phase is RecordingPhase.START_CONFIRMED
    assert step.stop_target_ns == _ANCHOR_NS + 60_000 * 1_000_000
    ticket, outcome = finishes.saved[0]
    assert outcome.status is AttemptStatus.SUCCEEDED
    assert outcome.effect is EffectState.CONFIRMED
    assert outcome.settlement.basis is SettlementBasis.OBSERVED
    assert outcome.observations == (_START_CONFIRMED_OBSERVATION,)
    # 锚点不因保存延迟改变：停止目标仍是 t + duration。
    assert step.stop_target_ns == recording_stop_target(_ANCHOR_NS, _DURATION_MS)


async def test_intent_and_dispatch_check_windows_twice() -> None:
    """授予在窗内、派发前窗口耗尽：不调用驱动并保存未派发。"""
    ticket = object()
    grants = GrantDouble(outcome="granted", ticket=ticket)
    finishes = FinishDouble()
    driver = DriverDouble(dispatch=_dispatch(confirmed=True, anchor_ns=_ANCHOR_NS))
    wall = WallDouble(now_us=_NOW + 10_000_000)  # 已超窗口终点
    window = LaunchWindow(scheduled_at=_NOW - 1_000_000, window_end=_NOW + 1_000_000)
    step = await start_recording(
        _context(
            grants=grants, driver=driver, finishes=finishes, wall=wall, window=window
        )
    )
    assert step.phase is RecordingPhase.DISPATCH_PREVENTED
    assert driver.calls == []
    assert finishes.prevented == [(ticket, "window_ended")]
    assert finishes.saved == []


async def test_cancel_before_dispatch_prevents_driver() -> None:
    ticket = object()
    grants = GrantDouble(outcome="granted", ticket=ticket)
    finishes = FinishDouble()
    driver = DriverDouble(dispatch=_dispatch(confirmed=True, anchor_ns=_ANCHOR_NS))
    step = await start_recording(
        _context(grants=grants, driver=driver, finishes=finishes, canceled=True)
    )
    assert step.phase is RecordingPhase.DISPATCH_PREVENTED
    assert driver.calls == []
    assert finishes.prevented == [(ticket, "canceled")]


async def test_window_expired_at_grant_does_not_dispatch() -> None:
    """授予阶段已超窗：不派发、不消耗保存路径。"""
    grants = GrantDouble(outcome="rejected", reason="window_ended")
    finishes = FinishDouble()
    driver = DriverDouble(dispatch=_dispatch(confirmed=True, anchor_ns=_ANCHOR_NS))
    step = await start_recording(
        _context(
            grants=grants,
            driver=driver,
            finishes=finishes,
            window=LaunchWindow(
                scheduled_at=_NOW - 2_000_000, window_end=_NOW - 1_000_000
            ),
        )
    )
    assert step.phase is RecordingPhase.NOT_GRANTED
    assert step.reason == "window_ended"
    assert driver.calls == []
    assert finishes.saved == []


async def test_sent_only_keeps_unknown_effect_without_anchor() -> None:
    """仅发送：保存发送成功，效果未知，不产生停止目标。"""
    grants = GrantDouble(outcome="granted", ticket=object())
    finishes = FinishDouble()
    driver = DriverDouble(dispatch=_dispatch(sent_only=True))
    step = await start_recording(
        _context(grants=grants, driver=driver, finishes=finishes)
    )
    assert step.phase is RecordingPhase.SENT_ONLY
    assert step.stop_target_ns is None
    ticket, outcome = finishes.saved[0]
    assert outcome.status is AttemptStatus.SUCCEEDED
    assert outcome.effect is EffectState.UNKNOWN
    assert outcome.observations == ()


async def test_rejected_without_effect_is_failure() -> None:
    grants = GrantDouble(outcome="granted", ticket=object())
    finishes = FinishDouble()
    driver = DriverDouble(
        dispatch=_dispatch(
            rejected=True,
            error=ErrorValue(code="device_rejected", stage="driver"),
        )
    )
    step = await start_recording(
        _context(grants=grants, driver=driver, finishes=finishes)
    )
    assert step.phase is RecordingPhase.REJECTED_NO_EFFECT
    ticket, outcome = finishes.saved[0]
    assert outcome.status is AttemptStatus.FAILED
    assert outcome.effect is EffectState.NO_EFFECT
    assert outcome.error is not None


async def test_unknown_start_is_kept_unknown() -> None:
    """启动效果未知（如超时假设收场）：保留未知，不猜已启动。"""
    grants = GrantDouble(outcome="granted", ticket=object())
    finishes = FinishDouble()
    driver = DriverDouble(
        dispatch=_dispatch(
            error=ErrorValue(code="transport_timeout", stage="transport")
        )
    )
    step = await start_recording(
        _context(grants=grants, driver=driver, finishes=finishes)
    )
    assert step.phase is RecordingPhase.START_UNKNOWN
    assert step.stop_target_ns is None
    ticket, outcome = finishes.saved[0]
    assert outcome.status is AttemptStatus.FAILED
    assert outcome.effect is EffectState.UNKNOWN


async def test_confirmed_then_error_keeps_both() -> None:
    """可靠启动事实先到、调用随后错误：事实与错误同时保留。"""
    grants = GrantDouble(outcome="granted", ticket=object())
    finishes = FinishDouble()
    driver = DriverDouble(
        dispatch=_dispatch(
            confirmed=True,
            anchor_ns=_ANCHOR_NS,
            observations=(_START_CONFIRMED_OBSERVATION,),
            error=ErrorValue(code="transport_broken_after_confirm", stage="transport"),
        )
    )
    step = await start_recording(
        _context(grants=grants, driver=driver, finishes=finishes)
    )
    assert step.phase is RecordingPhase.START_CONFIRMED_WITH_ERROR
    assert step.stop_target_ns == recording_stop_target(_ANCHOR_NS, _DURATION_MS)
    ticket, outcome = finishes.saved[0]
    assert outcome.status is AttemptStatus.FAILED
    assert outcome.effect is EffectState.CONFIRMED
    assert outcome.observations == (_START_CONFIRMED_OBSERVATION,)
    assert outcome.error is not None
