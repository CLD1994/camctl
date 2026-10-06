"""普通意图和结束输入保持精确类型、身份范围及真实 UTC 时刻。"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from camctl.contracts.values import ObjectId, UtcMicros
from camctl.devices.evidence import OPERATIONS, EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import (
    AttemptConfig, AttemptConfigError, AttemptFinish, AttemptIntent, AttemptTarget,
    OperationKind, QueryPurpose,
)
from camctl.operations.models import (
    AttemptTicket, CallOutcome, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.operations import attempts


def _intent(**changes):
    values = dict(operation="query", action_id=1, kind=OperationKind.QUERY_ACTIVITY,
                  target=AttemptTarget(), query_purpose=QueryPurpose.BEFORE_EXECUTION,
                  config=AttemptConfig(2, Decimal("0.25"), Decimal("0")), occurred_at=1)
    values.update(changes)
    return AttemptIntent(**values)


def _finish(occurred_at):
    ticket = AttemptTicket(1, "query", None, "query/preflight/1", 7)
    registry = EvidenceRegistry((EvidenceContract("query_returned", 1, "query", frozenset()),))
    outcome = CallOutcome(settlement=Settlement(
        SettlementBasis.OBSERVED, EvidenceValue("query_returned", 1)))
    return AttemptFinish(ticket, validate_outcome(ticket, outcome, registry), occurred_at)


def test_intent_requires_explicit_fact_time():
    with pytest.raises(TypeError):
        AttemptIntent(operation="query", action_id=1, kind=OperationKind.QUERY_ACTIVITY,
                      target=AttemptTarget(), query_purpose=QueryPurpose.BEFORE_EXECUTION,
                      config=AttemptConfig(2, "5", "1"))


def test_finish_requires_explicit_fact_time():
    finish = _finish(1)
    with pytest.raises(TypeError):
        AttemptFinish(ticket=finish.ticket, outcome=finish.outcome)


@pytest.mark.parametrize("purpose,activity_id,expected", [
    (QueryPurpose.BEFORE_EXECUTION, None, "query/preflight/7"),
    (QueryPurpose.START_CONFIRMATION, 3, "query/start/7/3"),
    (QueryPurpose.ACTIVITY_OBSERVATION, 3, "query/activity/7/3"),
    (QueryPurpose.STOP_CONFIRMATION, 3, "query/stop/7/3"),
    (QueryPurpose.RESIDUAL_STOP_CONFIRMATION, 3, "query/residual/7/3"),
])
def test_query_key_depends_only_on_fixed_identity(purpose, activity_id, expected):
    assert attempts.query_responsibility_key(7, purpose, activity_id) == expected


@pytest.mark.parametrize("action_id,purpose,activity_id", [
    (False, QueryPurpose.BEFORE_EXECUTION, None),
    (9223372036854775808, QueryPurpose.BEFORE_EXECUTION, None),
    (7, "preflight", None), (7, None, None),
    (7, QueryPurpose.BEFORE_EXECUTION, 3),
    (7, QueryPurpose.STOP_CONFIRMATION, None),
    (7, QueryPurpose.STOP_CONFIRMATION, True),
    (7, QueryPurpose.STOP_CONFIRMATION, 9223372036854775808),
])
def test_query_key_rejects_invalid_identity_combination(action_id, purpose, activity_id):
    with pytest.raises(AttemptConfigError):
        attempts.query_responsibility_key(action_id, purpose, activity_id)


@pytest.mark.parametrize("occurred_at", [-9223372036854775808, -1, 0, 1, 9223372036854775807, UtcMicros(0)])
def test_intent_preserves_legal_utc_microseconds(occurred_at):
    assert _intent(occurred_at=occurred_at).occurred_at == occurred_at


@pytest.mark.parametrize("occurred_at", [False, True, 0.0, 1.0, None, "0", Decimal("0"),
                                         -9223372036854775809, 9223372036854775808])
def test_intent_rejects_invalid_utc_microseconds(occurred_at):
    with pytest.raises(AttemptConfigError):
        _intent(occurred_at=occurred_at)


@pytest.mark.parametrize("occurred_at", [-9223372036854775808, -1, 0, 1, 9223372036854775807, UtcMicros(0)])
def test_finish_preserves_legal_utc_microseconds(occurred_at):
    assert _finish(occurred_at).occurred_at == occurred_at


@pytest.mark.parametrize("occurred_at", [False, True, 0.0, 1.0, None, "0", Decimal("0"),
                                         -9223372036854775809, 9223372036854775808])
def test_finish_rejects_invalid_utc_microseconds(occurred_at):
    with pytest.raises(AttemptConfigError):
        _finish(occurred_at)


@pytest.mark.parametrize("action_id", [1, 9223372036854775807, ObjectId(7)])
def test_intent_preserves_legal_action_identity(action_id):
    assert _intent(action_id=action_id).action_id == action_id


@pytest.mark.parametrize("action_id", [False, True, 1.0, None, "1", 0, -1, 9223372036854775808])
def test_intent_rejects_invalid_action_identity(action_id):
    with pytest.raises(AttemptConfigError):
        _intent(action_id=action_id)


@pytest.mark.parametrize("field", ["activity_id", "copy_id", "cleanup_item_id"])
@pytest.mark.parametrize("identity", [1, 9223372036854775807, ObjectId(7)])
def test_target_preserves_legal_identity(field, identity):
    assert getattr(AttemptTarget(**{field: identity}), field) == identity


@pytest.mark.parametrize("field", ["activity_id", "copy_id", "cleanup_item_id"])
@pytest.mark.parametrize("identity", [False, True, 1.0, "1", 0, -1, 9223372036854775808])
def test_target_rejects_invalid_identity(field, identity):
    with pytest.raises(AttemptConfigError):
        AttemptTarget(**{field: identity})


@pytest.mark.parametrize("maximum", [1, 9223372036854775807])
def test_config_preserves_legal_maximum(maximum):
    assert AttemptConfig(maximum).max_attempts == maximum


@pytest.mark.parametrize("maximum", [False, True, 1.0, None, "1", 0, -1, 9223372036854775808])
def test_config_rejects_invalid_maximum(maximum):
    with pytest.raises(AttemptConfigError):
        AttemptConfig(maximum)


@pytest.mark.parametrize("copy_round", [1, 9223372036854775807])
def test_read_intent_preserves_legal_copy_round(copy_round):
    intent = _intent(operation="read", kind=OperationKind.READ_FILE,
                     target=AttemptTarget(copy_id=7), query_purpose=None, copy_round=copy_round)
    assert intent.copy_round == copy_round


@pytest.mark.parametrize("copy_round", [False, True, 1.0, None, "1", 0, -1, 9223372036854775808])
def test_read_intent_rejects_invalid_copy_round(copy_round):
    with pytest.raises(AttemptConfigError):
        _intent(operation="read", kind=OperationKind.READ_FILE,
                target=AttemptTarget(copy_id=7), query_purpose=None, copy_round=copy_round)


@pytest.mark.parametrize("target", [None, {}, object(),
                                     SimpleNamespace(activity_id=None, copy_id=None, cleanup_item_id=None)])
def test_intent_requires_typed_target(target):
    with pytest.raises(AttemptConfigError):
        _intent(target=target)


@pytest.mark.parametrize("config", [None, {}, object(),
                                     SimpleNamespace(max_attempts=2, timeout_s=None, retry_interval_s=None)])
def test_intent_requires_typed_config(config):
    with pytest.raises(AttemptConfigError):
        _intent(config=config)


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_intent_accepts_registered_operation_category(operation):
    # 本用例只检验 D2 类别登记，业务种类与驱动类别的对应关系由所属契约验证。
    assert _intent(operation=operation).operation == operation


@pytest.mark.parametrize("operation", [None, False, 1, [], {}, "", "unknown", "QUERY", " query"])
def test_intent_rejects_unregistered_operation_category(operation):
    with pytest.raises(AttemptConfigError):
        _intent(operation=operation)


@pytest.mark.parametrize("changes", [
    {"kind": "query"}, {"query_purpose": None},
    {"target": AttemptTarget(activity_id=1)}, {"copy_round": 1},
])
def test_intent_preserves_kind_purpose_target_and_round_constraints(changes):
    with pytest.raises(AttemptConfigError):
        _intent(**changes)


def test_config_preserves_exact_seconds_and_optional_values():
    config = AttemptConfig(2, "0.123456789012345678901", "0")
    assert config.timeout_s == Decimal("0.123456789012345678901")
    assert config.retry_interval_s == Decimal("0")
    assert AttemptConfig(1).timeout_s is None
    assert AttemptConfig(1).retry_interval_s is None


@pytest.mark.parametrize("field,value", [("timeout_s", "0"), ("timeout_s", "Infinity"),
                                         ("retry_interval_s", "-1"), ("retry_interval_s", "NaN")])
def test_config_preserves_seconds_range_constraints(field, value):
    with pytest.raises(AttemptConfigError):
        AttemptConfig(1, **{field: value})


def test_retry_wait_remaining_opens_gate_when_elapsed_reaches_interval():
    # 会话单调钟从锚点起已经过整段间隔（含等号）：可以开始下一次尝试。
    assert attempts.retry_wait_remaining_s(
        Decimal("3"), 100_000_000_000, 103_000_000_000) is None
    assert attempts.retry_wait_remaining_s(
        Decimal("3"), 0, 500_000_000_000) is None


def test_retry_wait_zero_interval_never_waits():
    # 间隔为零表示满足重试资格后不额外等待。
    assert attempts.retry_wait_remaining_s(Decimal("0"), 0, 0) is None


def test_retry_wait_not_elapsed_returns_exact_remaining():
    remaining = attempts.retry_wait_remaining_s(
        Decimal("2.5"), 10_000_000_000, 11_500_000_000)
    assert remaining == Decimal("1.0")
    assert attempts.retry_wait_remaining_s(
        Decimal("3"), 5_000_000_000, 6_000_000_000) == Decimal("2")


def test_retry_wait_gate_skips_first_attempt_and_unset_wait():
    gate = attempts.RetryWaitGate()
    opened = dict(attempts_used=0, retry_wait_required=False,
                  max_attempts_used=3, interval_s=Decimal("3"), now_ns=0)
    assert gate.remaining("stop/1", **opened) is None
    # 流程未处于重试等待：资格判定交还意图事务，不做时间等待。
    assert gate.remaining(
        "stop/1", attempts_used=2, retry_wait_required=False,
        max_attempts_used=3, interval_s=Decimal("3"), now_ns=0) is None
    # 不适用定时重试的流程不做时间等待。
    assert gate.remaining(
        "stop/1", attempts_used=2, retry_wait_required=True,
        max_attempts_used=3, interval_s=None, now_ns=0) is None


def test_retry_wait_gate_opens_promptly_when_budget_exhausted():
    # 预算耗尽不为等待间隔推迟：立即交由意图事务按耗尽收场。
    gate = attempts.RetryWaitGate()
    assert gate.remaining(
        "results/1", attempts_used=3, retry_wait_required=True,
        max_attempts_used=3, interval_s=Decimal("3"), now_ns=0) is None


def test_retry_wait_gate_established_anchor_waits_then_opens():
    gate = attempts.RetryWaitGate()
    gate.established("stop/1", now_ns=1_000_000_000)
    assert gate.remaining(
        "stop/1", attempts_used=1, retry_wait_required=True,
        max_attempts_used=3, interval_s=Decimal("3"),
        now_ns=2_000_000_000) == Decimal("2")
    assert gate.remaining(
        "stop/1", attempts_used=1, retry_wait_required=True,
        max_attempts_used=3, interval_s=Decimal("3"),
        now_ns=4_000_000_000) is None


def test_retry_wait_gate_recounts_cross_session_wait_from_first_observation():
    # 本会话首次观察到既往会话留下的重试等待：按本次间隔从现在重新计时。
    gate = attempts.RetryWaitGate()
    assert gate.remaining(
        "stop/1", attempts_used=1, retry_wait_required=True,
        max_attempts_used=3, interval_s=Decimal("3"),
        now_ns=7_000_000_000) == Decimal("3")
    assert gate.remaining(
        "stop/1", attempts_used=1, retry_wait_required=True,
        max_attempts_used=3, interval_s=Decimal("3"),
        now_ns=9_000_000_000) == Decimal("1")
    assert gate.remaining(
        "stop/1", attempts_used=1, retry_wait_required=True,
        max_attempts_used=3, interval_s=Decimal("3"),
        now_ns=10_000_000_000) is None


def test_retry_wait_gate_cleared_anchor_recounts_on_next_wait():
    gate = attempts.RetryWaitGate()
    gate.established("stop/1", now_ns=0)
    gate.cleared("stop/1")
    # 流程结束清除锚点后再次处于等待：从首次观察重新起算。
    assert gate.remaining(
        "stop/1", attempts_used=1, retry_wait_required=True,
        max_attempts_used=3, interval_s=Decimal("3"),
        now_ns=10_000_000_000) == Decimal("3")


def test_retry_wait_gate_pending_only_waits_recorded_anchors():
    # 拷贝链的等待以本会话记录的失败锚点为准：无锚点不等待。
    gate = attempts.RetryWaitGate()
    assert gate.pending("media-input/1", interval_s=Decimal("3"), now_ns=0) is None
    gate.established("media-input/1", now_ns=1_000_000_000)
    assert gate.pending(
        "media-input/1", interval_s=Decimal("3"), now_ns=2_000_000_000
    ) == Decimal("2")
    assert gate.pending(
        "media-input/1", interval_s=Decimal("3"), now_ns=4_000_000_000) is None
