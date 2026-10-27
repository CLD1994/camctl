"""独立核验可用性检查的已收场工具事实。"""

from decimal import Decimal
from collections.abc import Mapping

from camctl.contracts.values import ObjectId
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.devices.drivers.hasselblad.responses import doctor_passed, parse_devices, parse_serial
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.tool_diagnostics import ToolStreamState, validate_diagnostics


def require_available(outcome: CallOutcome, check_id: int, identity: CameraIdentity,
                      timeout_s: Decimal) -> None:
    """根据完整原始结果判断是否存在可接受的可用性观察。"""
    if (not isinstance(outcome, CallOutcome) or outcome.status is not AttemptStatus.SUCCEEDED
            or outcome.effect is not EffectState.NO_EFFECT or outcome.error is not None
            or not isinstance(outcome.settlement, Settlement)
            or outcome.settlement.basis is not SettlementBasis.OBSERVED
            or not isinstance(outcome.settlement.evidence, EvidenceValue)
            or outcome.settlement.evidence.type != "availability_returned"
            or outcome.settlement.evidence.version != 1 or outcome.settlement.evidence.data != {}
            or not isinstance(identity, CameraIdentity)
            or not isinstance(timeout_s, Decimal) or not timeout_s.is_finite() or timeout_s <= 0):
        raise ValueError("检查结果不具备可用性释放依据")
    ObjectId(check_id)
    observations = outcome.observations
    if len(observations) != 1 or observations[0].type != "device_availability" or observations[0].version != 1:
        raise ValueError("检查必须包含唯一可用性观察")
    data = observations[0].data
    fields = {"check_id", "expected_serial", "expected_product_id", "observed_serial", "steps",
              "started_monotonic_ns", "finished_monotonic_ns"}
    if (not isinstance(data, Mapping) or set(data) != fields or data["check_id"] != str(check_id)
            or data["expected_serial"] != identity.serial or data["observed_serial"] != identity.serial
            or type(data["expected_product_id"]) is not int or data["expected_product_id"] != identity.product_id):
        raise ValueError("检查观察与固定机身身份不一致")
    started, finished, steps = data["started_monotonic_ns"], data["finished_monotonic_ns"], data["steps"]
    if (type(started) is not int or type(finished) is not int or finished < started
            or not isinstance(steps, list) or len(steps) != 3):
        raise ValueError("检查时间或调用步骤不完整")
    diagnostics = outcome.diagnostics
    validate_diagnostics(diagnostics)
    if diagnostics.call_limit != 3 or len(diagnostics.calls) != 3:
        raise ValueError("检查必须具有三条完整工具结果")
    previous = started
    for index, (step, call, name) in enumerate(zip(steps, diagnostics.calls, ("list", "info", "doctor")), 1):
        if (not isinstance(step, dict) or set(step) != {"name", "returned_monotonic_ns", "exit", "error",
                "used_grace_s", "signal_failures", "output_failure"}
                or step["name"] != name or call.step_index != index or call.step_name != name
                or type(step["returned_monotonic_ns"]) is not int
                or not previous <= step["returned_monotonic_ns"] <= finished
                or not isinstance(step["exit"], dict)
                or set(step["exit"]) != {"exit_code", "signal"}
                or type(step["exit"]["exit_code"]) is not int or step["exit"]["exit_code"] != 0
                or step["exit"]["signal"] is not None
                or step["error"] is not None or step["used_grace_s"] is not None
                or step["signal_failures"] != [] or step["output_failure"] is not None
                or call.stdout.state is not ToolStreamState.COMPLETE
                or call.stderr.state is not ToolStreamState.COMPLETE):
            raise ValueError("检查工具结果不完整或未正常返回")
        previous = step["returned_monotonic_ns"]
    if Decimal(previous - started) >= timeout_s * 1_000_000_000:
        raise ValueError("检查返回已超过整次期限")
    list_output, info_output, doctor_output = diagnostics.calls
    devices = [device for device in parse_devices(list_output.stdout.data)
               if device.vendor_id == 0x2756 and device.product_id == identity.product_id]
    if (len(devices) != 1 or parse_serial(info_output.stdout.data) != identity.serial
            or not doctor_passed(doctor_output.stdout.data, doctor_output.stderr.data)):
        raise ValueError("设备数量、机身身份或 doctor 结果不符合要求")
