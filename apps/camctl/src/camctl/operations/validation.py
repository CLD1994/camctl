"""收场、退出、效果及证据校验。

按 format_version=1 及登记契约核对结束结果：缺收场依据、未知证
据版本、观察与操作不匹配、身份不符、数量超限及状态与错误组合矛
盾分别拒绝，不以默认值接受。空观察数组是合法事实，与 NULL 分别
处理；只有本地退出不生成业务成功。
"""

from __future__ import annotations

from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceError,
    EvidenceRegistry,
    validate_observation,
)
from camctl.operations.models import (
    RESULT_FORMAT_VERSION,
    AttemptStatus,
    AttemptTicket,
    CallOutcome,
    EffectState,
    SettlementBasis,
    ValidatedOutcome,
)

__all__ = ["OutcomeValidationError", "validate_outcome"]


class OutcomeValidationError(ValueError):
    """结束结果不符合正式契约；不降级、不补默认值。"""


def validate_outcome(
    ticket: AttemptTicket,
    outcome: CallOutcome,
    evidence: EvidenceRegistry,
) -> ValidatedOutcome:
    if outcome.format_version != RESULT_FORMAT_VERSION:
        raise OutcomeValidationError("结果外层版本不支持")

    status = outcome.status
    if status not in (AttemptStatus.SUCCEEDED, AttemptStatus.FAILED, AttemptStatus.UNKNOWN):
        raise OutcomeValidationError(f"结束状态未登记: {status!r}")
    if status is AttemptStatus.SUCCEEDED and outcome.error is not None:
        raise OutcomeValidationError("成功结果不得携带调用错误")
    if status is not AttemptStatus.SUCCEEDED and outcome.error is None:
        raise OutcomeValidationError("失败或未知结果必须携带调用错误")

    settlement_contract = _validate_settlement(ticket, outcome, evidence)
    observation_contracts = _validate_observations(ticket, outcome, evidence)
    _validate_effect_grounded(outcome)
    _validate_not_dispatched_consistency(outcome)
    return ValidatedOutcome(
        outcome=outcome,
        settlement_contract=settlement_contract,
        observation_contracts=observation_contracts,
    )


def _validate_settlement(
    ticket: AttemptTicket, outcome: CallOutcome, evidence: EvidenceRegistry
) -> EvidenceContract:
    settlement = outcome.settlement
    if settlement is None:
        raise OutcomeValidationError("结束结果缺少收场依据")
    try:
        contract = evidence.contract(settlement.evidence.type, settlement.evidence.version)
    except EvidenceError as error:
        raise OutcomeValidationError(f"收场依据未登记: {error}") from error
    if contract.operation != ticket.operation:
        raise OutcomeValidationError(
            f"收场依据与操作不匹配: {contract.operation!r} != {ticket.operation!r}"
        )
    unknown = set(settlement.evidence.data) - contract.fields
    if unknown:
        raise OutcomeValidationError(f"收场依据包含未定义成员: {sorted(unknown)!r}")
    return contract


def _validate_observations(
    ticket: AttemptTicket, outcome: CallOutcome, evidence: EvidenceRegistry
) -> tuple[EvidenceContract, ...]:
    contracts: list[EvidenceContract] = []
    counts: dict[str, int] = {}
    for observation in outcome.observations:
        if not isinstance(observation, DeviceObservation):
            raise OutcomeValidationError(f"观察结构错误: {observation!r}")
        try:
            contract = evidence.contract(observation.type, observation.version)
        except EvidenceError as error:
            raise OutcomeValidationError(f"观察证据未登记: {error}") from error
        if contract.operation != ticket.operation:
            raise OutcomeValidationError(
                f"观察与操作不匹配: {observation.type!r} 属 {contract.operation!r}"
            )
        try:
            from camctl.devices.evidence import validate_observation

            validate_observation(observation, contract, expected_identity=ticket.target_id)
        except EvidenceError as error:
            raise OutcomeValidationError(f"观察不合法: {error}") from error
        used = counts.get(observation.type, 0) + 1
        counts[observation.type] = used
        if used > contract.max_observations:
            raise OutcomeValidationError(
                f"观察数量超出契约上限: {observation.type!r} > {contract.max_observations}"
            )
        contracts.append(contract)
    return tuple(contracts)


def _validate_effect_grounded(outcome: CallOutcome) -> None:
    """已确认效果必须有承载它的可靠观察；本地退出本身不算。"""
    if outcome.effect is EffectState.CONFIRMED and not outcome.observations:
        raise OutcomeValidationError("已确认效果缺少承载观察")


def _validate_not_dispatched_consistency(outcome: CallOutcome) -> None:
    settlement = outcome.settlement
    if settlement is None:
        return
    if settlement.basis is SettlementBasis.NOT_DISPATCHED:
        if outcome.effect is not EffectState.NO_EFFECT:
            raise OutcomeValidationError(
                "未派发依据只支持无效果；已确认效果与其矛盾"
            )
