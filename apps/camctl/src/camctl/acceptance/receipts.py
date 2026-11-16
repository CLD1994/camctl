"""受理写事务的申请身份与首次答复编码。"""

from decimal import Decimal
from hashlib import sha256

from camctl.acceptance.input import InputDiagnostic, ParsedInput
from camctl.acceptance.service import AcceptanceResult, AckDisposition, PlanDisposition
from camctl.contracts.json_encoding import encode_exact_json
from camctl.contracts.json_values import parse_exact_json


class ReceiptError(ValueError):
    """受理申请或首次答复无法编码、解释或核对。"""


def _encode_identity_value(value):
    # 标签避免对象与数字类型混淆；Decimal 的编码不经过浮点或舍入。
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ReceiptError("受理申请包含非有限数值")
        return ["decimal", str(value)]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return ["array", [_encode_identity_value(item) for item in value]]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ReceiptError("受理申请的对象键必须是字符串")
        return ["object", [[key, _encode_identity_value(value[key])] for key in sorted(value)]]
    raise ReceiptError("受理申请包含无法编码的值")


def calculate_request_identity(command):
    source = command.source
    if isinstance(source, ParsedInput):
        payload = {"kind": "parsed", "document": source.document}
    elif isinstance(source, InputDiagnostic):
        payload = {"kind": "diagnostic", "stage": source.stage.value, "detail": source.detail}
    else:
        raise ReceiptError("无法识别受理申请")
    data = {"source": payload, "path": source.path, "mode": command.mode.value,
            "occurred_at": command.occurred_at}
    encoded = encode_exact_json(_encode_identity_value(data), error_type=ReceiptError)
    return sha256(encoded.encode("utf-8", errors="surrogatepass")).hexdigest()


def verify_request_identity(identity, command):
    """核对保存的身份与本次完整申请。"""
    if identity != calculate_request_identity(command):
        raise ReceiptError("原受理申请与重送申请不一致")


def decode_result(encoded):
    """解释并校验首次完整答复。"""
    try:
        data = parse_exact_json(encoded)
        expected = {"plan_disposition", "plan_id", "ack_disposition", "ack_watermark",
                    "diagnostic_id", "needs_run", "diagnostics"}
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError("受理答复字段不完整")
        for field in ("plan_id", "diagnostic_id"):
            if data[field] is not None and (type(data[field]) is not int or data[field] <= 0):
                raise ValueError("受理答复编号非法")
        if type(data["ack_watermark"]) is not int or data["ack_watermark"] < 0:
            raise ValueError("受理答复水位非法")
        if data["needs_run"] is not None and type(data["needs_run"]) is not bool:
            raise ValueError("受理接管决定非法")
        if not isinstance(data["diagnostics"], list) or any(
                not isinstance(item, dict) for item in data["diagnostics"]):
            raise ValueError("受理诊断非法")
        return AcceptanceResult(plan_disposition=PlanDisposition(data["plan_disposition"]),
            plan_id=data["plan_id"], ack_disposition=AckDisposition(data["ack_disposition"]),
            ack_watermark=data["ack_watermark"], diagnostic_id=data["diagnostic_id"],
            needs_run=data["needs_run"], diagnostics=tuple(data["diagnostics"]))
    except (ValueError, KeyError, TypeError) as error:
        raise ReceiptError("原受理答复无法解释") from error


def encode_result(result):
    """编码首次完整答复。"""
    data = {"plan_disposition": result.plan_disposition.value, "plan_id": result.plan_id,
            "ack_disposition": result.ack_disposition.value, "ack_watermark": result.ack_watermark,
            "diagnostic_id": result.diagnostic_id, "needs_run": result.needs_run,
            "diagnostics": list(result.diagnostics)}
    return encode_exact_json(data, error_type=ReceiptError)
