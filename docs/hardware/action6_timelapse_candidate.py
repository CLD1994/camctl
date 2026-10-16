"""自动生成的独立诊断编码；请运行 render_action6_diagnostic_candidate.py 更新。
模板、字面量及编码函数由生产 commands.py 同源派生，不手工修改。
"""
from __future__ import annotations


from decimal import Decimal

from enum import StrEnum

from dataclasses import dataclass

class CapabilityNotDeclaredError(Exception):
    """驱动静态声明不支持该操作；消费者不得调用对应端口。"""

class CameraModel(StrEnum):
    ACTION6 = "dji-action6"
    OSMO360II = "dji-osmo360-ii"

class OutputSelection(StrEnum):
    VIDEO = "video"
    VIDEO_RAW = "video_raw"
    VIDEO_JPEG = "video_jpeg"

class TimelapseCandidateTemplate(StrEnum):
    """第一版候选格式的八个固定模板身份。"""

    ACTION6_MANUAL_VIDEO = "action6-manual-video"
    ACTION6_MANUAL_VIDEO_RAW = "action6-manual-video-raw"
    ACTION6_MANUAL_VIDEO_JPEG = "action6-manual-video-jpeg"
    ACTION6_AUTO_VIDEO = "action6-auto-video"
    ACTION6_AUTO_VIDEO_RAW = "action6-auto-video-raw"
    ACTION6_AUTO_VIDEO_JPEG = "action6-auto-video-jpeg"
    OSMO360II_MANUAL_VIDEO = "osmo360ii-manual-video"
    OSMO360II_AUTO_VIDEO = "osmo360ii-auto-video"

_DIAG = ("-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c")

def _cmd(code: str, payload: str, *, test=False, reply=False) -> Command:
    return (("dji_mb_ctrl",) + (("-r",) if reply else ())
            + (("-S", "test") if test else ()) + _DIAG + (code, payload))

ACTION_RESOLUTIONS = {"8k30": _cmd("18", "3703000000"),
                      "4k30": _cmd("18", "1003000000")}

ACTION_FOV = {"wide": _cmd("8e", "010109000101"),
              "natural_wide": _cmd("8e", "010109000105"),
              "standard": _cmd("8e", "010109000102")}

ACTION_STABILIZATION = {"off": _cmd("8e", "010108000100"),
                        "rocksteady": _cmd("8e", "010108000101")}

ACTION_ISO = {100: _cmd("2a", "03"), 200: _cmd("2a", "04"),
              400: _cmd("2a", "05"), 800: _cmd("2a", "06"),
              1600: _cmd("2a", "07"), 3200: _cmd("2a", "08")}

ACTION_EV = {Decimal("-0.7"): _cmd("0x2e", "0e", test=True),
             0: _cmd("0x2e", "10", test=True),
             Decimal("0.7"): _cmd("0x2e", "12", test=True)}

ACTION_APERTURE = {"f2.8": _cmd("0x26", "1801", test=True),
                   "f4.0": _cmd("0x26", "9001", test=True)}

ACTION_BITRATE = {"standard": ("simulate_device", "-s", "bitrate", "1"),
                  "high": ("simulate_device", "-s", "bitrate", "2")}

OSMO_ISO = {800: _cmd("2A", "06", test=True, reply=True)}

OSMO_EV = {0: _cmd("2e", "10")}

@dataclass(frozen=True)
class TimelapsePreset:
    """同时配置间隔、采集时长和输出类型的完整命令资料。"""

    interval_s: int
    duration_s: int
    outputs: OutputSelection
    exposure_mode: str
    command: Command
    candidate_template: TimelapseCandidateTemplate | None = None

TIMELAPSE_PRESETS = {
    CameraModel.ACTION6: (
        TimelapsePreset(30, 5400, OutputSelection.VIDEO, "manual", _cmd("6c", "0400002c01181500000000000000000000"), TimelapseCandidateTemplate.ACTION6_MANUAL_VIDEO),
        TimelapsePreset(30, 5400, OutputSelection.VIDEO_RAW, "manual", _cmd("6c", "0400032c01181500000000000000000000"), TimelapseCandidateTemplate.ACTION6_MANUAL_VIDEO_RAW),
        TimelapsePreset(30, 5400, OutputSelection.VIDEO_JPEG, "manual", _cmd("6c", "0400022c01181500000000000000000000"), TimelapseCandidateTemplate.ACTION6_MANUAL_VIDEO_JPEG),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO, "auto", _cmd("6c", "040000fa00701700000000000000000000")),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO_RAW, "auto", _cmd("6c", "040003fa00701700000000000000000000")),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO_JPEG, "auto", _cmd("6c", "040002fa00701700000000000000000000")),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO, "auto", _cmd("6c", "0400005000080700000000000000000000"), TimelapseCandidateTemplate.ACTION6_AUTO_VIDEO),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO_RAW, "auto", _cmd("6c", "0400035000080700000000000000000000"), TimelapseCandidateTemplate.ACTION6_AUTO_VIDEO_RAW),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO_JPEG, "auto", _cmd("6c", "0400025000080700000000000000000000"), TimelapseCandidateTemplate.ACTION6_AUTO_VIDEO_JPEG),
    ),
    CameraModel.OSMO360II: (
        TimelapsePreset(30, 600, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c015802000000000000000001"), TimelapseCandidateTemplate.OSMO360II_MANUAL_VIDEO),
        TimelapsePreset(30, 1200, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c01b004000000000000000001")),
        TimelapsePreset(30, 1800, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c010807000000000000000001")),
        TimelapsePreset(30, 3600, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c01100e000000000000000001")),
        TimelapsePreset(30, 7200, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c01201c000000000000000001")),
        TimelapsePreset(30, 10800, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c01302a000000000000000001")),
        TimelapsePreset(30, 18000, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c015046000000000000000001")),
        TimelapsePreset(40, 18000, OutputSelection.VIDEO, "auto", _cmd("6c", "04000090015046000000000000000000")),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO, "auto", _cmd("6c", "04000050000807000000000000000000"), TimelapseCandidateTemplate.OSMO360II_AUTO_VIDEO),
    ),
}

def _action_manual(iso) -> tuple[Command, ...]:
    return (_cmd("8e", "010100000101"), _cmd("1E", "0400"),
            ACTION_ISO[iso], _cmd("28", "013C8000"),
            _cmd("0x2c", "0634000000", test=True))

def _osmo_manual(iso) -> tuple[Command, ...]:
    return (_cmd("8e", "010100000101"), _cmd("1e", "0400", test=True, reply=True),
            OSMO_ISO[iso], _cmd("28", "013c8000", test=True, reply=True),
            _cmd("2c", "0634000000"))

def _exact_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, Decimal))
            and (not isinstance(value, Decimal) or value.is_finite()))

def _exact_scaled_uint16(value, scale: int, field: str) -> int:
    """精确缩放为两字节无符号小端窗口，不舍入或截断输入。"""
    if not _exact_number(value):
        raise ValueError(f"{field}不能精确编码")
    maximum = Decimal(0xFFFF) / Decimal(scale)
    if value < 0 or value > maximum:
        raise ValueError(f"{field}超出两字节编码窗口")
    numerator, denominator = (value.as_integer_ratio()
                              if isinstance(value, Decimal) else (value, 1))
    units, remainder = divmod(numerator * scale, denominator)
    if remainder:
        raise ValueError(f"{field}缩放后不是数学整数")
    if not 0 <= units <= 0xFFFF:
        raise ValueError(f"{field}超出两字节编码窗口")
    return units

def _validated_timelapse_template(model: CameraModel, preset: TimelapsePreset) -> bytearray:
    """检查完整模板的命令、长度及其已有时间字节与资料索引相符。"""
    expected_length = 17 if model is CameraModel.ACTION6 else 16
    command = preset.command
    if (not isinstance(command, tuple) or len(command) < 2
            or command[0] != "dji_mb_ctrl" or command[-2] != "6c"):
        raise ValueError("延时完整命令模板的命令结构错误")
    encoded = command[-1]
    if (not isinstance(encoded, str) or len(encoded) != expected_length * 2
            or any(character not in "0123456789abcdefABCDEF" for character in encoded)):
        raise ValueError("延时完整命令模板的十六进制长度或格式错误")
    try:
        payload = bytearray.fromhex(encoded)
    except ValueError as error:
        raise ValueError("延时完整命令模板无法完整解码") from error
    if len(payload) != expected_length or len(payload) < 7:
        raise ValueError("延时完整命令模板长度错误")
    interval_units = _exact_scaled_uint16(preset.interval_s, 10, "模板间隔")
    duration_units = _exact_scaled_uint16(preset.duration_s, 1, "模板时长")
    if (payload[3:5] != interval_units.to_bytes(2, "little")
            or payload[5:7] != duration_units.to_bytes(2, "little")):
        raise ValueError("延时完整命令模板的时间窗口与其资料索引不一致")
    return payload

def _candidate_template_for(model: CameraModel, outputs, exposure_mode: str) -> TimelapsePreset:
    try:
        output = OutputSelection(outputs)
    except ValueError:
        output = None
    role_name = (f"{model.name}_{exposure_mode.upper()}_{output.name}"
                 if output is not None else "")
    role = TimelapseCandidateTemplate.__members__.get(role_name)
    matches = tuple(preset for preset in TIMELAPSE_PRESETS[model]
                    if preset.candidate_template is role and role is not None)
    if not matches:
        raise CapabilityNotDeclaredError(
            f"驱动 {model!r} 尚未提供该延时参数分区的完整命令模板")
    if len(matches) != 1:
        raise ValueError("第一版延时格式的模板身份重复")
    template = matches[0]
    if template.outputs is not output or template.exposure_mode != exposure_mode:
        raise ValueError("第一版延时格式的模板身份与参数分区不一致")
    return template

def _timelapse_command_for(model: CameraModel, params: Mapping[str, JsonValue]) -> Command:
    """优先返回原完整字面量，否则按固定第一版格式替换四个时间字节。"""
    presets = TIMELAPSE_PRESETS[model]
    exposure_mode = params["exposure"]["mode"]
    exact = next((preset for preset in presets
                  if (preset.interval_s, preset.duration_s, preset.outputs, preset.exposure_mode)
                  == (params["interval_s"], params["duration_s"],
                      params["outputs"], exposure_mode)), None)
    if exact is not None:
        _validated_timelapse_template(model, exact)
        return exact.command

    interval = params["interval_s"]
    duration = params["duration_s"]
    template = _candidate_template_for(model, params["outputs"], exposure_mode)
    payload = _validated_timelapse_template(model, template)
    interval_units = _exact_scaled_uint16(params["interval_s"], 10, "延时间隔")
    duration_units = _exact_scaled_uint16(params["duration_s"], 1, "采集时长")
    payload[3:5] = interval_units.to_bytes(2, "little")
    payload[5:7] = duration_units.to_bytes(2, "little")
    return (*template.command[:-1], payload.hex())

def _encode_settings(model, action_type, params):
    exposure = params["exposure"]
    if action_type == "camera_record":
        if model is CameraModel.ACTION6:
            if exposure["mode"] == "manual":
                exposure_settings = _action_manual(exposure["iso"])
            else:
                exposure_settings = (_cmd("8e", "010100000101"), _cmd("1E", "0100"),
                                     _cmd("0x2c", "0634000000", test=True), ACTION_EV[exposure["compensation_ev"]])
            aperture_settings = ((ACTION_APERTURE[params["aperture"]],)
                                 if "aperture" in params else ())
            bitrate_settings = ((ACTION_BITRATE[params["bitrate"]],)
                                if "bitrate" in params else ())
            return (_cmd("0xe1", "01"), ACTION_RESOLUTIONS[params["resolution"]],
                    *exposure_settings, _cmd("42", "3d", test=True),
                    ACTION_FOV[params["fov"]], ACTION_STABILIZATION[params["stabilization"]],
                    *aperture_settings, *bitrate_settings)
        if exposure["mode"] == "manual":
            exposure_settings = _osmo_manual(exposure["iso"])
        else:
            exposure_settings = (_cmd("8e", "010100000101"), _cmd("1e", "0100", test=True, reply=True),
                                 _cmd("2c", "0634000000"), OSMO_EV[exposure["compensation_ev"]])
        return (_cmd("0x8e", "01013f000101", test=True), _cmd("e1", "38"),
                ("simulate_device", "-s", "BaseFormat", "6", "69", "0"),
                *exposure_settings, _cmd("42", "3d"),
                ("simulate_device", "-s", "DeviceRecordRecSettingBitRate", "2"))
    timelapse_command = _timelapse_command_for(model, params)
    if model is CameraModel.ACTION6:
        exposure_settings = (_action_manual(800) if exposure["mode"] == "manual"
                             else (_cmd("0x1e", "0100", test=True),))
        return (_cmd("e1", "02", test=True), ACTION_RESOLUTIONS["4k30"],
                timelapse_command, *exposure_settings)
    exposure_settings = (_osmo_manual(800) if exposure["mode"] == "manual" else
                         (_cmd("8e", "010100000100"), _cmd("1e", "0000")))
    return (_cmd("0x8e", "01013f000101", test=True), _cmd("e1", "3b"),
            _cmd("18", "6f03000000"), *exposure_settings, timelapse_command)
