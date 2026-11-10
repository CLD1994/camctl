"""收场、退出、效果及证据校验。

按 format_version=1 及登记契约核对结束结果：缺收场依据、未知证
据版本、观察与操作不匹配、身份不符、数量超限及状态与错误组合矛
盾分别拒绝，不以默认值接受。空观察数组是合法事实，与 NULL 分别
处理；只有本地退出不生成业务成功。
"""

from __future__ import annotations

from enum import Enum

from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceError,
    EvidenceRegistry,
    validate_observation,
)
from camctl.operations.tool_diagnostics import DiagnosticsValidationError
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


class ResultErrorIssue(Enum):
    SUCCESS_ERROR = "success_error"
    MISSING_ERROR = "missing_error"
    UNEXPECTED_ERROR = "unexpected_error"


def result_error_issue(status, error, *, stored=False):
    """返回实际结果的状态与错误矛盾；存储入口保留未登记状态的原边界。"""
    if stored:
        from camctl.contracts.enums import enum_for
        registered = enum_for("operation_attempts.status")
        succeeded = status == int(registered.SUCCEEDED)
        failed = status in (int(registered.FAILED), int(registered.UNKNOWN))
    else:
        succeeded = status is AttemptStatus.SUCCEEDED
        # 类型化入口在调用前已拒绝未登记的结束状态。
        failed = not succeeded
    if succeeded and error is not None:
        return ResultErrorIssue.SUCCESS_ERROR
    if failed and error is None:
        return ResultErrorIssue.MISSING_ERROR
    return None


def confirmed_effect_has_observation(effect, observations, *, stored=False):
    """确认效果必须由至少一个真实观察承载。"""
    if stored:
        from camctl.contracts.enums import enum_for
        confirmed = effect == int(enum_for("operation_attempts.effect_state").CONFIRMED)
    else:
        confirmed = effect is EffectState.CONFIRMED
    return not confirmed or bool(observations)


def not_dispatched_has_no_effect(basis, effect, *, stored=False):
    """未派发依据只允许无效果，不把本地退出解释成设备效果。"""
    if stored:
        from camctl.contracts.enums import enum_for
        not_dispatched = basis == SettlementBasis.NOT_DISPATCHED.value
        no_effect = effect == int(enum_for("operation_attempts.effect_state").NO_EFFECT)
    else:
        not_dispatched = basis is SettlementBasis.NOT_DISPATCHED
        no_effect = effect is EffectState.NO_EFFECT
    return not not_dispatched or no_effect


def run_finish_error_issue(status, error):
    """流程失败或未确认必须保留错误，其他结束状态不携带错误。"""
    from camctl.contracts.enums import enum_for
    registered = enum_for("operation_runs.status")
    if status in (int(registered.FAILED), int(registered.UNCONFIRMED)):
        return ResultErrorIssue.MISSING_ERROR if error is None else None
    return ResultErrorIssue.UNEXPECTED_ERROR if error is not None else None


def preparation_result_is_confirmed(effect, observations, activity_id):
    """成功准备须有原活动唯一的组合完成观察。"""
    from camctl.contracts.enums import enum_for
    expected = {"type": "capture_preparation_completed", "version": 1,
                "data": {"activity_id": str(activity_id)}}
    return (effect == int(enum_for("operation_attempts.effect_state").CONFIRMED)
            and observations.count(expected) == 1)


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
    error_issue = result_error_issue(status, outcome.error)
    if error_issue is ResultErrorIssue.SUCCESS_ERROR:
        raise OutcomeValidationError("成功结果不得携带调用错误")
    if error_issue is ResultErrorIssue.MISSING_ERROR:
        raise OutcomeValidationError("失败或未知结果必须携带调用错误")

    settlement_contract = _validate_settlement(ticket, outcome, evidence)
    if outcome.diagnostics is not None:
        from camctl.operations.tool_diagnostics import validate_diagnostics
        try:
            validate_diagnostics(outcome.diagnostics)
        except Exception as error:
            raise DiagnosticsValidationError("原工具诊断校验失败") from error
        if (settlement_contract.max_diagnostic_calls is None
                or outcome.diagnostics.call_limit != settlement_contract.max_diagnostic_calls):
            raise DiagnosticsValidationError("诊断数量上限与所属调用登记不符")
    observation_contracts = _validate_observations(ticket, outcome, evidence)
    _validate_effect_grounded(outcome)
    _validate_not_dispatched_consistency(outcome)
    return ValidatedOutcome(
        outcome=outcome,
        settlement_contract=settlement_contract,
        observation_contracts=observation_contracts,
        ticket=ticket,
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
    if not confirmed_effect_has_observation(outcome.effect, outcome.observations):
        raise OutcomeValidationError("已确认效果缺少承载观察")


def _validate_not_dispatched_consistency(outcome: CallOutcome) -> None:
    settlement = outcome.settlement
    if settlement is None:
        return
    if not not_dispatched_has_no_effect(settlement.basis, outcome.effect):
        raise OutcomeValidationError(
            "未派发依据只支持无效果；已确认效果与其矛盾"
        )
