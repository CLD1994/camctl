"""原尝试的有界双流诊断；不提供设备事实或恢复推断。"""
from dataclasses import dataclass
from enum import StrEnum
import re
from collections.abc import Mapping

from camctl.operations.process import OUTPUT_LIMIT_BYTES


class ToolStreamState(StrEnum):
    COMPLETE = "complete"
    UNCONFIRMED = "unconfirmed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ToolStream:
    state: ToolStreamState
    data: bytes | None


@dataclass(frozen=True)
class ToolCallOutput:
    step_index: int
    step_name: str
    stdout: ToolStream
    stderr: ToolStream


@dataclass(frozen=True)
class ToolDiagnostics:
    call_limit: int
    calls: tuple[ToolCallOutput, ...]


class ResultEncodingError(ValueError):
    """原结果编码失败；原拥有者须保留 typed 结果与原始字节。"""


def validate_diagnostics(value: ToolDiagnostics) -> None:
    if (type(value) is not ToolDiagnostics or type(value.call_limit) is not int
            or value.call_limit <= 0 or type(value.calls) is not tuple
            or len(value.calls) > value.call_limit):
        raise ValueError("工具诊断必须具有有限正上限及有界元组")
    for index, call in enumerate(value.calls, 1):
        if (type(call) is not ToolCallOutput or type(call.step_index) is not int
                or call.step_index != index or type(call.step_name) is not str
                or not call.step_name.strip()):
            raise ValueError("工具诊断步骤序号或名称无效")
        for stream in (call.stdout, call.stderr):
            if type(stream) is not ToolStream or not isinstance(stream.state, ToolStreamState):
                raise ValueError("工具诊断流必须使用登记状态")
            if stream.state is ToolStreamState.UNAVAILABLE:
                if stream.data is not None:
                    raise ValueError("未取得流不得携带字节")
            elif type(stream.data) is not bytes or len(stream.data) > OUTPUT_LIMIT_BYTES:
                raise ValueError("已取得流必须携带有界字节")
        states = (call.stdout.state, call.stderr.state)
        if ToolStreamState.COMPLETE in states and states != (ToolStreamState.COMPLETE,) * 2:
            raise ValueError("没有完整双流依据时不能单独确认一条流完整")


def encode_diagnostics(value: ToolDiagnostics) -> dict:
    try:
        validate_diagnostics(value)
        def stream(item: ToolStream) -> dict:
            return {"state": item.state.value,
                    "hex": None if item.data is None else item.data.hex()}
        return {"version": 1, "call_limit": value.call_limit, "calls": [
            {"step_index": item.step_index, "step_name": item.step_name,
             "stdout": stream(item.stdout), "stderr": stream(item.stderr)}
            for item in value.calls]}
    except Exception as error:
        raise ResultEncodingError("原工具诊断无法完整编码") from error


def _object(value, fields: set[str]) -> Mapping:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("工具诊断对象成员不完整或未登记")
    return value


def _decode_stream(value) -> ToolStream:
    value = _object(value, {"state", "hex"})
    state = ToolStreamState(value["state"])
    encoded = value["hex"]
    if state is ToolStreamState.UNAVAILABLE:
        if encoded is not None:
            raise ValueError("未取得流必须编码为 null")
        return ToolStream(state, None)
    if (type(encoded) is not str or len(encoded) > 2 * OUTPUT_LIMIT_BYTES
            or len(encoded) % 2 or re.fullmatch(r"[0-9a-f]*", encoded) is None):
        raise ValueError("工具诊断字节不是有界规范十六进制")
    return ToolStream(state, bytes.fromhex(encoded))


def decode_diagnostics(value) -> ToolDiagnostics:
    value = _object(value, {"version", "call_limit", "calls"})
    limit, calls = value["call_limit"], value["calls"]
    if (type(value["version"]) is not int or value["version"] != 1
            or type(limit) is not int or limit <= 0 or type(calls) is not list
            or len(calls) > limit):
        raise ValueError("工具诊断版本或冻结数量上限无效")
    decoded = []
    for call in calls:
        call = _object(call, {"step_index", "step_name", "stdout", "stderr"})
        decoded.append(ToolCallOutput(call["step_index"], call["step_name"],
                                      _decode_stream(call["stdout"]), _decode_stream(call["stderr"])))
    result = ToolDiagnostics(limit, tuple(decoded))
    validate_diagnostics(result)
    return result
