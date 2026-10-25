"""两机型固件资料来源及稳定指纹；本模块不进行设备 IO。"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import re

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.json_encoding import encode_exact_json
from .commands import CameraModel
from camctl.contracts.enums import enum_for
from camctl.devices.tasks import VideoResultMode


@dataclass(frozen=True)
class FirmwareProfile:
    source_type: str
    command: str


_PROFILES = {
    CameraModel.ACTION6: FirmwareProfile("action6-verinfo-json-sha256/v1", "cat /firmware/verinfo/ver_info.txt"),
    CameraModel.OSMO360II: FirmwareProfile("android-build-fingerprint/v1", "getprop ro.build.fingerprint"),
}


@dataclass(frozen=True)
class RecoveryQualification:
    """驱动维护的精确固件及已验收的原拍摄范围。"""

    driver_id: CameraModel
    fingerprint: str
    action_type: int
    video_result_mode: VideoResultMode
    evidence: str


_RECOVERY_QUALIFICATIONS = (
    RecoveryQualification(
        CameraModel.OSMO360II,
        "android-build-fingerprint/v1:e4/eagle4_oq102/eagle4_oq102:9/PD1A.180720.031/38390:userdebug/test-keys",
        int(enum_for("actions.type").CAMERA_RECORD), VideoResultMode.SEGMENTED,
        "docs/camctl/verification.md#容器与真机分段验证2026-10-23",
    ),
)


def recovery_qualification(driver_id, *, action_type, video_result_mode):
    """仅选择已登记的原动作范围，实际固件仍由恢复身份调用精确核对。"""
    return next((record for record in _RECOVERY_QUALIFICATIONS
        if record.driver_id == driver_id and record.action_type == action_type
        and record.video_result_mode == video_result_mode), None)


def firmware_profile(driver_id):
    return _PROFILES[CameraModel(driver_id)]


def _ordered(value):
    if isinstance(value, dict):
        return {key: _ordered(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    return value


def firmware_fingerprint(driver_id: str, raw_bytes: bytes) -> str:
    """完整资料按来源类型生成精确匹配 token；拒绝不完整或含糊资料。"""
    if not isinstance(raw_bytes, bytes):
        raise ValueError("固件资料必须是原始字节")
    profile = firmware_profile(driver_id)
    text = raw_bytes.decode("utf-8", "strict")
    if CameraModel(driver_id) is CameraModel.OSMO360II:
        text = text.rstrip("\r\n")
        if not text or text != text.strip() or "\n" in text or "\r" in text:
            raise ValueError("Android build fingerprint 必须是单行非空资料")
        return profile.source_type + ":" + text
    document = parse_exact_json(text)
    if not isinstance(document, dict) or not isinstance(document.get("Version"), str):
        raise ValueError("Action6 固件资料缺少完整 Version")
    if re.fullmatch(r"v[0-9]+(?:\.[0-9]+){3}", document["Version"]) is None:
        raise ValueError("Action6 Version 不是完整四段版本文本")
    build = document.get("Metabuild_Info")
    if not isinstance(build, dict) or any(not isinstance(build.get(key), str) or not build[key].strip()
            for key in ("Meta_Build_ID", "Product_Flavor", "Time_Stamp")):
        raise ValueError("Action6 固件资料缺少完整 Metabuild_Info")
    stamp = datetime.strptime(build["Time_Stamp"], "%Y-%m-%d %H:%M:%S")
    if stamp.strftime("%Y-%m-%d %H:%M:%S") != build["Time_Stamp"]:
        raise ValueError("Action6 固件资料的构建时间格式不完整")
    canonical = encode_exact_json(_ordered(document)).encode("utf-8")
    return profile.source_type + ":" + hashlib.sha256(canonical).hexdigest()


def validate_firmware_fingerprint(driver_id, value):
    profile = firmware_profile(driver_id)
    if not isinstance(value, str) or not value.startswith(profile.source_type + ":"):
        raise ValueError("固件指纹必须使用所属驱动的来源类型前缀")
    payload = value[len(profile.source_type) + 1:]
    if CameraModel(driver_id) is CameraModel.ACTION6:
        if re.fullmatch(r"[0-9a-f]{64}", payload) is None:
            raise ValueError("Action6 固件指纹必须具有完整小写 SHA-256")
    elif not payload or payload != payload.strip() or "\n" in payload or "\r" in payload:
        raise ValueError("Android 固件指纹必须具有单行非空内容")


_IDENTITY_HEADER = b"CAMCTL-RECOVERY-IDENTITY/1\n"
_FIRMWARE_SEPARATOR = b"\nCAMCTL-FIRMWARE\n"
_SERIAL_SEPARATOR = b"\nCAMCTL-SERIAL\n"
_IDENTITY_END = b"\nCAMCTL-END\n"
_BOOT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def identity_script(driver_id):
    command = firmware_profile(driver_id).command
    return ("printf 'CAMCTL-RECOVERY-IDENTITY/1\\n'; "
        "cat /proc/sys/kernel/random/boot_id || exit $?; "
        "printf '\\nCAMCTL-FIRMWARE\\n'; " + command + " || exit $?; "
        "printf '\\nCAMCTL-SERIAL\\n'; getprop ro.serialno || exit $?; "
        "printf '\\nCAMCTL-END\\n'")


def decode_identity(driver_id, output):
    if not isinstance(output, bytes) or not output.startswith(_IDENTITY_HEADER) or not output.endswith(_IDENTITY_END):
        raise ValueError("恢复身份响应没有完整版本帧")
    body = output[len(_IDENTITY_HEADER):-len(_IDENTITY_END)]
    if body.count(_FIRMWARE_SEPARATOR) != 1 or body.count(_SERIAL_SEPARATOR) != 1:
        raise ValueError("恢复身份响应成员缺失或重复")
    boot, remaining = body.split(_FIRMWARE_SEPARATOR)
    firmware, serial = remaining.split(_SERIAL_SEPARATOR)
    boot_id = boot.decode("ascii", "strict").strip("\r\n")
    if _BOOT.fullmatch(boot_id) is None:
        raise ValueError("恢复身份响应没有可靠 boot_id")
    reported_serial = serial.decode("utf-8", "strict").strip("\r\n")
    if "\n" in reported_serial or "\r" in reported_serial:
        raise ValueError("额外设备序列号响应有多行")
    return [boot_id, firmware_fingerprint(driver_id, firmware), reported_serial or None]
