"""实际调用结果的唯一 JSON 编码；页历史与尝试结果共同使用。"""
from typing import Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.devices.evidence import DeviceObservation
from camctl.operations.models import (CallOutcome, CallInfo, AttemptStatus, EffectState,
    ErrorValue, EvidenceValue, Settlement, SettlementBasis)


def result_document(outcome: CallOutcome) -> dict:
    """按统一外层结构编码结束结果；状态、错误及效果由行列保存。"""
    settlement = outcome.settlement
    assert settlement is not None
    document = {
        "format_version": outcome.format_version,
        "settlement": {
            "basis": settlement.basis.value,
            "evidence": {
                "type": settlement.evidence.type,
                "version": settlement.evidence.version,
                "data": dict(settlement.evidence.data),
            },
        },
        "observations": [
            {
                "type": observation.type,
                "version": observation.version,
                "data": dict(observation.data),
            }
            for observation in outcome.observations
        ],
    }
    if outcome.call_info is not None:
        call_info: dict[str, Any] = {}
        if outcome.call_info.local_exit_code is not None:
            call_info["local_exit"] = {"exit_code": outcome.call_info.local_exit_code}
        elif outcome.call_info.local_signal is not None:
            call_info["local_exit"] = {"signal": outcome.call_info.local_signal}
        if outcome.call_info.remote_exit_code is not None:
            call_info["remote_exit_code"] = outcome.call_info.remote_exit_code
        document["call_info"] = call_info
    return document


def error_document(error) -> dict | None:
    if error is None:
        return None
    return {"code": error.code, "stage": error.stage, "details": dict(error.details)}


def _evidence(value):
    if (not isinstance(value, Mapping) or set(value) != {"type", "version", "data"}
            or not isinstance(value["type"], str) or not value["type"]
            or type(value["version"]) is not int or value["version"] < 1
            or not isinstance(value["data"], Mapping)):
        raise ValueError("实际依据或观察不是完整登记对象")
    return value["type"], value["version"], dict(value["data"])


def read_result_document(status: int, effect: int, result: Mapping, error: Mapping | None) -> CallOutcome:
    """恢复格式 1 的实际结果；不使用当前驱动重新解释原事实。"""
    if type(status) is not int or type(effect) is not int:
        raise ValueError("实际状态及效果必须是整数枚举")
    status_value = AttemptStatus(enum_for("operation_attempts.status")(status).name.lower())
    effect_value = EffectState(enum_for("operation_attempts.effect_state")(effect).name.lower())
    if (not isinstance(result, Mapping) or not {"format_version", "settlement", "observations"} <= set(result)
            or set(result) - {"format_version", "settlement", "observations", "call_info"}
            or type(result["format_version"]) is not int or result["format_version"] != 1):
        raise ValueError("实际结果版本或登记成员无效")
    raw_settlement = result["settlement"]
    if not isinstance(raw_settlement, Mapping) or set(raw_settlement) != {"basis", "evidence"}:
        raise ValueError("实际收场依据缺少完整登记成员")
    settlement = Settlement(SettlementBasis(raw_settlement["basis"]), EvidenceValue(*_evidence(raw_settlement["evidence"])))
    if not isinstance(result["observations"], list):
        raise ValueError("实际观察必须是数组")
    observations = tuple(DeviceObservation(*_evidence(value)) for value in result["observations"])
    if (status_value is AttemptStatus.SUCCEEDED) != (error is None):
        raise ValueError("实际调用状态与错误组合矛盾")
    if error is not None:
        if (not isinstance(error, Mapping) or set(error) != {"code", "stage", "details"}
                or any(not isinstance(error[field], str) or not error[field] for field in ("code", "stage"))
                or not isinstance(error["details"], Mapping)):
            raise ValueError("实际调用错误缺少完整输入")
        error = ErrorValue(error["code"], error["stage"], dict(error["details"]))
    if effect_value is EffectState.CONFIRMED and not observations:
        raise ValueError("确认效果缺少实际观察")
    if settlement.basis is SettlementBasis.NOT_DISPATCHED and effect_value is not EffectState.NO_EFFECT:
        raise ValueError("未派发依据与效果组合矛盾")
    call_info = None
    if "call_info" in result:
        info = result["call_info"]
        if not isinstance(info, Mapping) or not info or set(info) - {"local_exit", "remote_exit_code"}:
            raise ValueError("实际调用信息成员无效")
        local = info.get("local_exit", {})
        if "local_exit" in info:
            if not isinstance(local, Mapping) or set(local) not in ({"exit_code"}, {"signal"}):
                raise ValueError("实际本地退出信息无效")
            if any(type(value) is not int for value in local.values()) or local.get("signal", 1) <= 0:
                raise ValueError("实际本地退出码或信号类型无效")
        if "remote_exit_code" in info and type(info["remote_exit_code"]) is not int:
            raise ValueError("实际远端退出码类型无效")
        call_info = CallInfo(local_exit_code=local.get("exit_code"), local_signal=local.get("signal"),
                             remote_exit_code=info.get("remote_exit_code"))
    return CallOutcome(status=status_value, effect=effect_value, error=error, settlement=settlement,
                       observations=observations, call_info=call_info)
