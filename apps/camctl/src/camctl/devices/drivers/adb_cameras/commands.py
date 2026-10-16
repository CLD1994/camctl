"""交接资料提供的命令候选，不解释调用成功或设备完成。

设备侧 argv 保留大小写及前缀。录像离散选项供参数 Schema 使用；
延时时间规则独立维护，负载只查完整字面量，不生成未提供的编码。
"""
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from camctl.contracts.json_values import JsonValue, is_json_integer
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from .timelapse_parameters import TIMELAPSE_DURATIONS_S, TIMELAPSE_INTERVALS_S


class CameraModel(StrEnum):
    ACTION6 = "dji-action6"
    OSMO360II = "dji-osmo360-ii"


class OutputSelection(StrEnum):
    VIDEO = "video"
    VIDEO_RAW = "video_raw"
    VIDEO_JPEG = "video_jpeg"


Command = tuple[str, ...]
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


# 完整字面量包含既有记录；新请求合法域由独立时间参数定义约束。
TIMELAPSE_PRESETS = {
    CameraModel.ACTION6: (
        TimelapsePreset(30, 5400, OutputSelection.VIDEO, "manual", _cmd("6c", "0400002c01181500000000000000000000")),
        TimelapsePreset(30, 5400, OutputSelection.VIDEO_RAW, "manual", _cmd("6c", "0400032c01181500000000000000000000")),
        TimelapsePreset(30, 5400, OutputSelection.VIDEO_JPEG, "manual", _cmd("6c", "0400022c01181500000000000000000000")),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO, "auto", _cmd("6c", "040000fa00701700000000000000000000")),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO_RAW, "auto", _cmd("6c", "040003fa00701700000000000000000000")),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO_JPEG, "auto", _cmd("6c", "040002fa00701700000000000000000000")),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO, "auto", _cmd("6c", "0400005000080700000000000000000000")),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO_RAW, "auto", _cmd("6c", "0400035000080700000000000000000000")),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO_JPEG, "auto", _cmd("6c", "0400025000080700000000000000000000")),
    ),
    CameraModel.OSMO360II: (
        TimelapsePreset(30, 600, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c015802000000000000000001")),
        TimelapsePreset(30, 1200, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c01b004000000000000000001")),
        TimelapsePreset(30, 1800, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c010807000000000000000001")),
        TimelapsePreset(30, 3600, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c01100e000000000000000001")),
        TimelapsePreset(30, 7200, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c01201c000000000000000001")),
        TimelapsePreset(30, 10800, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c01302a000000000000000001")),
        TimelapsePreset(30, 18000, OutputSelection.VIDEO, "manual", _cmd("6c", "0407002c015046000000000000000001")),
        TimelapsePreset(40, 18000, OutputSelection.VIDEO, "auto", _cmd("6c", "04000090015046000000000000000000")),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO, "auto", _cmd("6c", "04000050000807000000000000000000")),
    ),
}


@dataclass(frozen=True)
class PendingCommand:
    command: Command
    reason: str


def pending_commands(driver_id: str) -> tuple[PendingCommand, ...]:
    """供最后设备试验逐项核实，不将未说明组合加入参数定义。"""
    model = CameraModel(driver_id)
    if model is CameraModel.ACTION6:
        return (PendingCommand(ACTION_APERTURE["f4.0"], "光圈命令有删除线，效力待核实"),
                *(PendingCommand(command, "码率命令有删除线，效力待核实") for command in ACTION_BITRATE.values()),
                PendingCommand(start_for(model, "camera_record"), "启动命令有删除线，效力待核实"))
    return (PendingCommand(_cmd("8e", "010109000102"), "FOV 在全景模式中的适用范围未知"),
            PendingCommand(_cmd("8e", "010109000101"), "FOV 在全景模式中的适用范围未知"),
            PendingCommand(_cmd("8e", "010109000100"), "FOV 在全景模式中的适用范围未知"),
            PendingCommand(_cmd("6c", "040000fa00201c000000000000000000"), "25 秒间隔的 100 分钟与 2 小时说明冲突"))


def start_for(driver_id: str, action_type: str) -> Command:
    """只返回候选触发命令，不能提供 STARTED 等观察。"""
    CameraModel(driver_id)
    if action_type == "camera_record":
        return _cmd("02", "01")
    if action_type == "camera_timelapse":
        return _cmd("01", "01")
    raise ValueError("没有该动作的启动候选命令")


def stop_for(driver_id: str, action_type: str) -> Command:
    model = CameraModel(driver_id)
    if action_type == "camera_record":
        return _cmd("02", "00")
    if action_type == "camera_timelapse" and model is CameraModel.OSMO360II:
        return _cmd("01", "00")
    raise ValueError("没有该动作的停止候选命令")


def _action_manual(iso) -> tuple[Command, ...]:
    return (_cmd("8e", "010100000101"), _cmd("1E", "0400"),
            ACTION_ISO[iso], _cmd("28", "013C8000"),
            _cmd("0x2c", "0634000000", test=True))


def _osmo_manual(iso) -> tuple[Command, ...]:
    return (_cmd("8e", "010100000101"), _cmd("1e", "0400", test=True, reply=True),
            OSMO_ISO[iso], _cmd("28", "013c8000", test=True, reply=True),
            _cmd("2c", "0634000000"))


def settings_for(driver_id: str, action_type: str, params: Mapping[str, JsonValue]) -> tuple[Command, ...]:
    """校验新候选并返回完整设置命令；纯转换，不访问设备或隐藏重试。"""
    from .definitions import candidate_capabilities
    from camctl.acceptance.schema import validate_precise

    model = CameraModel(driver_id)
    capability = next((cap for cap in candidate_capabilities(model) if cap.action_type == action_type), None)
    if capability is None:
        raise ValueError("没有该动作的候选参数定义")
    validate_precise(capability.schema, dict(params))
    return settings_from_effective_params(model, action_type, params)


def settings_from_effective_params(
        driver_id: str, action_type: str, params: Mapping[str, JsonValue]) -> tuple[Command, ...]:
    """编码公共处理器读取的原生效参数，不按当前候选规则重新受理。

    型号、结构和精确数值须可解释，设置只能使用已有完整映射。
    新请求及诊断使用 settings_for；此内部入口不授予设备派发资格。
    """
    model = CameraModel(driver_id)
    _check_effective_structure(model, action_type, params)
    try:
        return _encode_settings(model, action_type, params)
    except KeyError as error:
        raise CapabilityNotDeclaredError(
            f"驱动 {driver_id!r} 尚未提供该生效参数的完整命令映射") from error


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


def _timelapse_command_for(model: CameraModel, params: Mapping[str, JsonValue]) -> Command:
    """优先返回原完整字面量，否则仅替换所选模板的四个时间字节。"""
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
    if (interval not in TIMELAPSE_INTERVALS_S or duration not in TIMELAPSE_DURATIONS_S
            or duration <= interval):
        raise CapabilityNotDeclaredError(
            f"驱动 {model!r} 没有可解释该延时事实的原有映射")

    template = next((preset for preset in presets
                     if preset.outputs == params["outputs"]
                     and preset.exposure_mode == exposure_mode), None)
    if template is None:
        raise CapabilityNotDeclaredError(
            f"驱动 {model!r} 尚未提供该延时参数分区的完整命令模板")

    payload = _validated_timelapse_template(model, template)
    interval_units = _exact_scaled_uint16(params["interval_s"], 10, "延时间隔")
    duration_units = _exact_scaled_uint16(params["duration_s"], 1, "采集时长")
    payload[3:5] = interval_units.to_bytes(2, "little")
    payload[5:7] = duration_units.to_bytes(2, "little")
    return (*template.command[:-1], payload.hex())


def _check_effective_structure(model, action_type, params):
    if action_type not in {"camera_record", "camera_timelapse"}:
        raise ValueError("没有该动作的设置编码")
    prefix = "action6" if model is CameraModel.ACTION6 else "osmo360ii"
    required = {"type", "duration_s", "exposure"}
    optional = set()
    if action_type == "camera_timelapse":
        required.update(("interval_s", "outputs"))
    elif model is CameraModel.ACTION6:
        required.update(("resolution", "fov", "stabilization"))
        optional.update(("aperture", "bitrate"))
    if (not isinstance(params, Mapping) or not required <= set(params)
            or set(params) - required - optional
            or params["type"] != f"{prefix}_{action_type.removeprefix('camera_')}"):
        raise ValueError("原生效参数的结构或型号不匹配")
    if not is_json_integer(params["duration_s"]) or params["duration_s"] <= 0:
        raise ValueError("原采集时长必须为精确正整数")
    for field in set(params) - {"type", "duration_s", "interval_s", "exposure"}:
        if not isinstance(params[field], str):
            raise ValueError("原设置必须为命令映射标识")
    if action_type == "camera_timelapse":
        interval = params["interval_s"]
        if not _exact_number(interval) or interval <= 0:
            raise ValueError("原延时间隔必须为精确正数")
    exposure = params["exposure"]
    if not isinstance(exposure, Mapping):
        raise ValueError("原曝光必须为对象")
    manual = (set(exposure) == {"mode", "iso"} and exposure["mode"] == "manual"
              and is_json_integer(exposure["iso"]))
    auto = (set(exposure) == {"mode"} and exposure["mode"] == "auto"
            if action_type == "camera_timelapse" else
            set(exposure) == {"mode", "compensation_ev"} and exposure["mode"] == "auto"
            and _exact_number(exposure["compensation_ev"]))
    if not (manual or auto):
        raise ValueError("原曝光结构没有对应的完整编码")
    if action_type == "camera_timelapse" and manual and exposure["iso"] != 800:
        raise ValueError("原延时手动曝光没有对应的完整编码")


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
