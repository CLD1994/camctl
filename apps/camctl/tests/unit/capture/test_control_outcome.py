"""拍摄适配保留驱动已经形成的正式调用结果。"""

from decimal import Decimal

import pytest

from camctl.capture.handlers import _operation_outcome
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)


@pytest.mark.parametrize("effect", [EffectState.NO_EFFECT, EffectState.UNKNOWN])
def test_complete_control_outcome_keeps_original_settlement_and_error(effect):
    outcome = CallOutcome(
        status=AttemptStatus.FAILED,
        error=ErrorValue("transport_timeout", "transport", {"response_error": "unavailable"}),
        effect=effect,
        settlement=Settlement(SettlementBasis.ASSUMED,
            EvidenceValue("adb_foreground_assumption", 1, {"terminate_grace_s": Decimal("0.25")})),
        observations=(), call_info=CallInfo(local_exit_code=7))

    actual, confirmed = _operation_outcome(DeviceCallResult.from_outcome(outcome), "start_confirmed")

    assert actual is outcome
    assert confirmed is False
