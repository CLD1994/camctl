"""交接资料提供的命令候选，不解释调用成功或设备完成。

设备侧 argv 保留大小写及前缀。录像离散选项供参数 Schema 使用；
延时先精确匹配原完整字面量，否则按固定的第一版模板替换四个时间字节。
当前允许时间由新请求 Schema 限定，不属于可信原事实的格式解释条件。
"""
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from camctl.contracts.json_values import JsonValue, is_json_integer
from camctl.devices.drivers.registry import CapabilityNotDeclaredError


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


Command = tuple[str, ...]
_DIAG = ("-R", "diag", "-g", "1", "-t", "0", "-s", "2", "-c")


@dataclass(frozen=True)
class BitrateSetting:
    nominal_mbps: int
    command: Command


@dataclass(frozen=True)
class VideoModeFacts:
    panoramic: bool
    resolution: str
    frame_rate_fps: int
    color_profile: str | None = None


@dataclass(frozen=True)
class FixedSettingRequest:
    kind: str
    value: object
    commands: tuple[Command, ...]


@dataclass(frozen=True)
class ParameterSettingRequest:
    field: str
    choices: Mapping


@dataclass(frozen=True)
class CommandRequest:
    commands: tuple[Command, ...]


SettingStep = FixedSettingRequest | ParameterSettingRequest | CommandRequest


@dataclass(frozen=True)
class TaskSettingPlan:
    before_exposure: tuple[SettingStep, ...]
    exposure: Mapping[str, tuple[SettingStep, ...]]
    after_exposure: tuple[SettingStep, ...]


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
TIMELAPSE_MANUAL_ISO = 800
ACTION_ISO = {100: _cmd("2a", "03"), 200: _cmd("2a", "04"),
              400: _cmd("2a", "05"), TIMELAPSE_MANUAL_ISO: _cmd("2a", "06"),
              1600: _cmd("2a", "07"), 3200: _cmd("2a", "08")}
ACTION_EV = {Decimal("-0.7"): _cmd("0x2e", "0e", test=True),
             0: _cmd("0x2e", "10", test=True),
             Decimal("0.7"): _cmd("0x2e", "12", test=True)}
ACTION_APERTURE = {"f2.8": _cmd("0x26", "1801", test=True),
                   "f4.0": _cmd("0x26", "9001", test=True)}
ACTION_BITRATE = {"standard": ("simulate_device", "-s", "bitrate", "1"),
                  "high": ("simulate_device", "-s", "bitrate", "2")}
OSMO_ISO = {TIMELAPSE_MANUAL_ISO: _cmd("2A", "06", test=True, reply=True)}
OSMO_EV = {0: _cmd("2e", "10")}
OSMO_BITRATE = {
    "standard": BitrateSetting(120, ("simulate_device", "-s", "DeviceRecordRecSettingBitRate", "1")),
    "high": BitrateSetting(170, ("simulate_device", "-s", "DeviceRecordRecSettingBitRate", "2")),
}


_ACTION6_SHUTTER = FixedSettingRequest(
    "shutter", "1/60", (_cmd("28", "013C8000"),))
_ACTION6_WHITE_BALANCE = FixedSettingRequest(
    "white_balance", 5200, (_cmd("0x2c", "0634000000", test=True),))
_OSMO_SHUTTER = FixedSettingRequest(
    "shutter", "1/60", (_cmd("28", "013c8000", test=True, reply=True),))
_OSMO_WHITE_BALANCE = FixedSettingRequest(
    "white_balance", 5200, (_cmd("2c", "0634000000"),))
_ACTION6_MANUAL_MODE = CommandRequest(
    (_cmd("8e", "010100000101"), _cmd("1E", "0400")))
_ACTION6_AUTO_MODE = CommandRequest(
    (_cmd("8e", "010100000101"), _cmd("1E", "0100")))
_OSMO_MANUAL_MODE = CommandRequest(
    (_cmd("8e", "010100000101"), _cmd("1e", "0400", test=True, reply=True)))
_OSMO_RECORD_AUTO_MODE = CommandRequest(
    (_cmd("8e", "010100000101"), _cmd("1e", "0100", test=True, reply=True)))
_OSMO_TIMELAPSE_AUTO_MODE = CommandRequest(
    (_cmd("8e", "010100000100"), _cmd("1e", "0000")))

_OSMO_RECORD_VIDEO = FixedSettingRequest(
    "video_mode",
    VideoModeFacts(True, "8K", 30, "D-Log M"),
    (_cmd("0x8e", "01013f000101", test=True), _cmd("e1", "38"),
     ("simulate_device", "-s", "BaseFormat", "6", "69", "0")),
)
_OSMO_TIMELAPSE_VIDEO = FixedSettingRequest(
    "video_mode",
    VideoModeFacts(True, "8K", 30),
    (_cmd("0x8e", "01013f000101", test=True), _cmd("e1", "3b"),
     _cmd("18", "6f03000000")),
)
_ACTION6_TIMELAPSE_ISO = FixedSettingRequest(
    "iso", TIMELAPSE_MANUAL_ISO, (ACTION_ISO[TIMELAPSE_MANUAL_ISO],))
_OSMO_TIMELAPSE_ISO = FixedSettingRequest(
    "iso", TIMELAPSE_MANUAL_ISO, (OSMO_ISO[TIMELAPSE_MANUAL_ISO],))

FIXED_SETTING_PLANS: dict[tuple[CameraModel, str], TaskSettingPlan] = {
    (CameraModel.ACTION6, "camera_record"): TaskSettingPlan(
        (),
        {
            "manual": (_ACTION6_MANUAL_MODE, ParameterSettingRequest("iso", ACTION_ISO),
                       _ACTION6_SHUTTER, _ACTION6_WHITE_BALANCE),
            "auto": (_ACTION6_AUTO_MODE, _ACTION6_WHITE_BALANCE,
                     ParameterSettingRequest("compensation_ev", ACTION_EV)),
        },
        (),
    ),
    (CameraModel.ACTION6, "camera_timelapse"): TaskSettingPlan(
        (),
        {
            "manual": (_ACTION6_MANUAL_MODE, _ACTION6_TIMELAPSE_ISO,
                       _ACTION6_SHUTTER, _ACTION6_WHITE_BALANCE),
            "auto": (CommandRequest((_cmd("0x1e", "0100", test=True),)),),
        },
        (),
    ),
    (CameraModel.OSMO360II, "camera_record"): TaskSettingPlan(
        (_OSMO_RECORD_VIDEO,),
        {
            "manual": (_OSMO_MANUAL_MODE, _OSMO_TIMELAPSE_ISO,
                       _OSMO_SHUTTER, _OSMO_WHITE_BALANCE),
            "auto": (_OSMO_RECORD_AUTO_MODE, _OSMO_WHITE_BALANCE,
                     FixedSettingRequest("compensation_ev", 0, (OSMO_EV[0],))),
        },
        (CommandRequest((_cmd("42", "3d"),)),),
    ),
    (CameraModel.OSMO360II, "camera_timelapse"): TaskSettingPlan(
        (_OSMO_TIMELAPSE_VIDEO,),
        {
            "manual": (_OSMO_MANUAL_MODE, _OSMO_TIMELAPSE_ISO,
                       _OSMO_SHUTTER, _OSMO_WHITE_BALANCE),
            "auto": (_OSMO_TIMELAPSE_AUTO_MODE,),
        },
        (),
    ),
}


def fixed_setting_plan_for(driver_id: str, action_type: str) -> TaskSettingPlan:
    return FIXED_SETTING_PLANS[(CameraModel(driver_id), action_type)]


def fixed_setting_value(driver_id: str, action_type: str, mode: str, kind: str):
    steps = fixed_setting_plan_for(driver_id, action_type).exposure[mode]
    return next(step.value for step in steps
                if isinstance(step, FixedSettingRequest) and step.kind == kind)


def _commands_for_steps(steps: tuple[SettingStep, ...], values: Mapping) -> tuple[Command, ...]:
    commands = []
    for step in steps:
        if isinstance(step, ParameterSettingRequest):
            commands.append(step.choices[values[step.field]])
        else:
            commands.extend(step.commands)
    return tuple(commands)


@dataclass(frozen=True)
class TimelapsePreset:
    """同时配置间隔、采集时长和输出类型的完整命令资料。"""

    interval_s: int
    duration_s: int
    outputs: OutputSelection
    exposure_mode: str
    command: Command
    candidate_template: TimelapseCandidateTemplate | None = None


# 完整字面量包含既有记录；新请求合法域由独立时间参数定义约束。
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
    plan = fixed_setting_plan_for(CameraModel.ACTION6, "camera_record")
    return _commands_for_steps(plan.exposure["manual"], {"iso": iso})


def _osmo_manual() -> tuple[Command, ...]:
    plan = fixed_setting_plan_for(CameraModel.OSMO360II, "camera_record")
    return _commands_for_steps(plan.exposure["manual"], {})


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
    outputs = OutputSelection.VIDEO if model is CameraModel.OSMO360II else params["outputs"]
    exact = next((preset for preset in presets
                  if (preset.interval_s, preset.duration_s, preset.outputs, preset.exposure_mode)
                  == (params["interval_s"], params["duration_s"],
                      outputs, exposure_mode)), None)
    if exact is not None:
        _validated_timelapse_template(model, exact)
        return exact.command

    interval = params["interval_s"]
    duration = params["duration_s"]
    template = _candidate_template_for(model, outputs, exposure_mode)
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
    if model is CameraModel.OSMO360II:
        required.add("bitrate")
    if action_type == "camera_timelapse":
        required.add("interval_s")
        if model is CameraModel.ACTION6:
            required.add("outputs")
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
    if model is CameraModel.OSMO360II:
        manual = set(exposure) == {"mode"} and exposure["mode"] == "manual"
        auto = set(exposure) == {"mode"} and exposure["mode"] == "auto"
    else:
        manual = (set(exposure) == {"mode", "iso"} and exposure["mode"] == "manual"
                  and is_json_integer(exposure["iso"]))
        auto = (set(exposure) == {"mode"} and exposure["mode"] == "auto"
                if action_type == "camera_timelapse" else
                set(exposure) == {"mode", "compensation_ev"} and exposure["mode"] == "auto"
                and _exact_number(exposure["compensation_ev"]))
    if not (manual or auto):
        raise ValueError("原曝光结构没有对应的完整编码")
    if model is CameraModel.ACTION6 and action_type == "camera_timelapse" and manual:
        fixed_iso = fixed_setting_value(model, action_type, "manual", "iso")
        if exposure["iso"] != fixed_iso:
            raise ValueError("原延时手动曝光没有对应的完整编码")


def _encode_settings(model, action_type, params):
    exposure = params["exposure"]
    plan = fixed_setting_plan_for(model, action_type)
    if action_type == "camera_record":
        if model is CameraModel.ACTION6:
            if exposure["mode"] == "manual":
                exposure_settings = _action_manual(exposure["iso"])
            else:
                exposure_settings = _commands_for_steps(
                    plan.exposure["auto"], exposure)
            aperture_settings = ((ACTION_APERTURE[params["aperture"]],)
                                 if "aperture" in params else ())
            bitrate_settings = ((ACTION_BITRATE[params["bitrate"]],)
                                if "bitrate" in params else ())
            return (_cmd("0xe1", "01"), ACTION_RESOLUTIONS[params["resolution"]],
                    *exposure_settings, _cmd("42", "3d", test=True),
                    ACTION_FOV[params["fov"]], ACTION_STABILIZATION[params["stabilization"]],
                    *aperture_settings, *bitrate_settings)
        if exposure["mode"] == "manual":
            exposure_settings = _osmo_manual()
        else:
            exposure_settings = _commands_for_steps(
                plan.exposure["auto"], exposure)
        return (*_commands_for_steps(plan.before_exposure, {}),
                *exposure_settings,
                *_commands_for_steps(plan.after_exposure, {}),
                OSMO_BITRATE[params["bitrate"]].command)
    timelapse_command = _timelapse_command_for(model, params)
    exposure_settings = _commands_for_steps(
        plan.exposure[exposure["mode"]], exposure)
    if model is CameraModel.ACTION6:
        return (_cmd("e1", "02", test=True), ACTION_RESOLUTIONS["4k30"],
                timelapse_command, *exposure_settings)
    return (*_commands_for_steps(plan.before_exposure, {}),
            *exposure_settings, timelapse_command, OSMO_BITRATE[params["bitrate"]].command)
