"""O1 完整结束结果及证据校验的单元测试。

本地退出不推出远端退出（255 分区）；缺收场依据、未知证据版本、
观察与操作不匹配分别拒绝；空观察与 NULL 分别处理；成功与错误组
合按结果分区独立证伪。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    CallInfo,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.validation import (
    OutcomeValidationError,
    validate_outcome,
)

STOP_CONFIRMED = EvidenceContract(
    type="stop_confirmed",
    version=1,
    operation="stop",
    fields=frozenset({"activity_id"}),
    identity_field="activity_id",
    max_observations=2,
)
OPERATION_RETURNED = EvidenceContract(
    type="operation_returned",
    version=1,
    operation="stop",
    fields=frozenset({"terminate_grace_s"}),
    max_observations=2,
)
QUERY_SNAPSHOT = EvidenceContract(
    type="state_snapshot",
    version=1,
    operation="query",
    fields=frozenset({"running"}),
    max_observations=1,
)
REGISTRY = EvidenceRegistry([STOP_CONFIRMED, OPERATION_RETURNED, QUERY_SNAPSHOT])

_ERROR = ErrorValue(code="device_start_failed", stage="execution", details={})
_TICKET = AttemptTicket(
    attempt_id=3, operation="stop", target_id="7", responsibility_key="stop/7",
    run_id=1,
)


def _outcome(**overrides) -> CallOutcome:
    values: dict = dict(
        status=AttemptStatus.SUCCEEDED,
        error=None,
        effect=EffectState.CONFIRMED,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED, evidence=EvidenceValue.of(OPERATION_RETURNED)
        ),
        observations=(
            DeviceObservation(
                type="stop_confirmed", version=1, data={"activity_id": "7"}
            ),
        ),
        call_info=None,
    )
    values.update(overrides)
    return CallOutcome(**values)


def test_validated_outcome_preserves_complete_ticket_context():
    validated = validate_outcome(_TICKET, _outcome(), REGISTRY)
    assert validated.ticket == AttemptTicket(
        attempt_id=3, operation="stop", target_id="7",
        responsibility_key="stop/7", run_id=1,
    )


class TestRemoteExitPartition:
    def test_local_255_has_no_remote_result(self) -> None:
        outcome = _outcome(
            call_info=CallInfo(local_exit_code=255, remote_exit_code=None)
        )
        validated = validate_outcome(_TICKET, outcome, REGISTRY)
        assert validated.outcome.call_info is not None
        assert validated.outcome.call_info.remote_exit_code is None
        assert validated.outcome.call_info.local_exit_code == 255

    def test_trusted_remote_255_is_saved(self) -> None:
        outcome = _outcome(
            call_info=CallInfo(local_exit_code=255, remote_exit_code=255)
        )
        validated = validate_outcome(_TICKET, outcome, REGISTRY)
        assert validated.outcome.call_info is not None
        assert validated.outcome.call_info.remote_exit_code == 255


class TestSettlementPartitions:
    def test_missing_settlement_rejected(self) -> None:
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, _outcome(settlement=None), REGISTRY)

    def test_unknown_evidence_version_rejected(self) -> None:
        outcome = _outcome(
            settlement=Settlement(
                basis=SettlementBasis.OBSERVED,
                evidence=EvidenceValue(type="operation_returned", version=9, data={}),
            )
        )
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, outcome, REGISTRY)

    def test_assumed_settlement_with_precise_grace(self) -> None:
        outcome = _outcome(
            status=AttemptStatus.FAILED,
            error=_ERROR,
            effect=EffectState.UNKNOWN,
            settlement=Settlement(
                basis=SettlementBasis.ASSUMED,
                evidence=EvidenceValue(
                    type="adb_foreground_assumption",
                    version=1,
                    data={"terminate_grace_s": Decimal(3)},
                ),
            ),
        )
        with pytest.raises(OutcomeValidationError):
            # 未登记的假设类型同样按未知证据拒绝。
            validate_outcome(_TICKET, outcome, REGISTRY)

    def test_not_dispatched_requires_no_effect_and_error(self) -> None:
        prevented = EvidenceContract(
            type="dispatch_prevented",
            version=1,
            operation="stop",
            fields=frozenset(),
            max_observations=1,
        )
        registry = EvidenceRegistry([STOP_CONFIRMED, prevented])
        outcome = _outcome(
            status=AttemptStatus.FAILED,
            error=_ERROR,
            effect=EffectState.NO_EFFECT,
            observations=(),
            settlement=Settlement(
                basis=SettlementBasis.NOT_DISPATCHED,
                evidence=EvidenceValue.of(prevented),
            ),
        )
        validated = validate_outcome(_TICKET, outcome, registry)
        assert validated.outcome.effect is EffectState.NO_EFFECT
        # 已确认效果与未派发依据矛盾。
        conflict = _outcome(
            status=AttemptStatus.FAILED,
            error=_ERROR,
            effect=EffectState.CONFIRMED,
            observations=(),
            settlement=Settlement(
                basis=SettlementBasis.NOT_DISPATCHED,
                evidence=EvidenceValue.of(prevented),
            ),
        )
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, conflict, registry)


class TestObservationPartitions:
    def test_operation_mismatch_rejected(self) -> None:
        outcome = _outcome(
            observations=(
                DeviceObservation(
                    type="state_snapshot", version=1, data={"running": True}
                ),
            )
        )
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, outcome, REGISTRY)

    def test_unknown_observation_version_rejected(self) -> None:
        outcome = _outcome(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=4, data={"activity_id": "7"}
                ),
            )
        )
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, outcome, REGISTRY)

    def test_wrong_identity_rejected(self) -> None:
        outcome = _outcome(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=1, data={"activity_id": "9"}
                ),
            )
        )
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, outcome, REGISTRY)

    def test_count_over_contract_limit_rejected(self) -> None:
        duplicate = (
            DeviceObservation(
                type="stop_confirmed", version=1, data={"activity_id": "7"}
            ),
        ) * (STOP_CONFIRMED.max_observations + 1)
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, _outcome(observations=duplicate), REGISTRY)

    def test_empty_observations_are_tuple_not_null(self) -> None:
        outcome = _outcome(
            status=AttemptStatus.FAILED,
            error=_ERROR,
            effect=EffectState.NO_EFFECT,
            observations=(),
        )
        validated = validate_outcome(_TICKET, outcome, REGISTRY)
        assert validated.outcome.observations == ()
        # 观察数组必填：NULL 在模型边界即拒绝，不当作空数组解释。
        with pytest.raises(ValueError):
            _outcome(observations=None)  # type: ignore[arg-type]


class TestStatusEffectCombinations:
    def test_success_with_error_rejected(self) -> None:
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, _outcome(error=_ERROR), REGISTRY)

    def test_failure_without_error_rejected(self) -> None:
        with pytest.raises(OutcomeValidationError):
            validate_outcome(
                _TICKET,
                _outcome(status=AttemptStatus.FAILED, error=None),
                REGISTRY,
            )

    def test_failure_with_confirmed_effect_kept(self) -> None:
        """可靠效果先到、调用错误后到：FAILED 与 CONFIRMED 共同保留。"""
        outcome = _outcome(
            status=AttemptStatus.FAILED, error=_ERROR, effect=EffectState.CONFIRMED
        )
        validated = validate_outcome(_TICKET, outcome, REGISTRY)
        assert validated.outcome.status is AttemptStatus.FAILED
        assert validated.outcome.effect is EffectState.CONFIRMED
        assert validated.outcome.error is not None

    def test_local_exit_alone_is_not_business_success(self) -> None:
        """只有本地退出不生成业务成功：效果必须来自观察或明确证据。"""
        outcome = _outcome(
            effect=EffectState.CONFIRMED, observations=(), call_info=CallInfo(local_exit_code=0, remote_exit_code=None)
        )
        with pytest.raises(OutcomeValidationError):
            validate_outcome(_TICKET, outcome, REGISTRY)
