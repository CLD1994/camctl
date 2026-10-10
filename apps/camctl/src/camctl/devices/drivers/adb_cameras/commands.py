"""交接资料提供的命令候选，不解释调用成功或设备完成。

设备侧 argv 保留大小写及前缀。离散选项和延时组合是参数 Schema
的同一来源；负载只查完整字面量，不生成未提供的编码。
"""
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from camctl.contracts.json_values import JsonValue


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

# 曝光前设置间隔与持续时间；输出选择在曝光设置后另行发送。
ACTION_TIMELAPSE_TIMING = {
    (30, 5400): _cmd("6c", "0400002c01181500000000000000000000"),
    (25, 6000): _cmd("6c", "040000fa00701700000000000000000000"),
    (8, 1800): _cmd("6c", "0400005000080700000000000000000000"),
}


@dataclass(frozen=True)
class TimelapsePreset:
    interval_s: int
    duration_s: int
    outputs: OutputSelection
    exposure_mode: str
    command: Command


TIMELAPSE_PRESETS = {
    CameraModel.ACTION6: (
        TimelapsePreset(30, 5400, OutputSelection.VIDEO_RAW, "manual", _cmd("6c", "0400032c01181500000000000000000000")),
        TimelapsePreset(30, 5400, OutputSelection.VIDEO_JPEG, "manual", _cmd("6c", "0400022c01181500000000000000000000")),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO, "auto", ACTION_TIMELAPSE_TIMING[25, 6000]),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO_RAW, "auto", _cmd("6c", "040003fa00701700000000000000000000")),
        TimelapsePreset(25, 6000, OutputSelection.VIDEO_JPEG, "auto", _cmd("6c", "040002fa00701700000000000000000000")),
        TimelapsePreset(8, 1800, OutputSelection.VIDEO, "auto", ACTION_TIMELAPSE_TIMING[8, 1800]),
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
                PendingCommand(start_for(model, "camera_record"), "启动命令有删除线，效力待核实"),
                PendingCommand(ACTION_TIMELAPSE_TIMING[30, 5400], "未说明输出类别"))
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
    """校验完整候选输入后查表；不发送命令或隐藏重试。"""
    from .definitions import candidate_capabilities
    from camctl.acceptance.schema import validate_precise

    model = CameraModel(driver_id)
    capability = next((cap for cap in candidate_capabilities(model) if cap.action_type == action_type), None)
    if capability is None:
        raise ValueError("没有该动作的候选参数定义")
    validate_precise(capability.schema, dict(params))
    exposure = params["exposure"]
    if action_type == "camera_record":
        if model is CameraModel.ACTION6:
            if exposure["mode"] == "manual":
                exposure_settings = _action_manual(exposure["iso"])
            else:
                exposure_settings = (_cmd("8e", "010100000101"), _cmd("1E", "0100"),
                                     _cmd("0x2c", "0634000000", test=True), ACTION_EV[exposure["compensation_ev"]])
            return (_cmd("0xe1", "01"), ACTION_RESOLUTIONS[params["resolution"]],
                    *exposure_settings, _cmd("42", "3d", test=True),
                    ACTION_FOV[params["fov"]], ACTION_STABILIZATION[params["stabilization"]],
                    ACTION_APERTURE[params["aperture"]], ACTION_BITRATE[params["bitrate"]])
        if exposure["mode"] == "manual":
            exposure_settings = _osmo_manual(exposure["iso"])
        else:
            exposure_settings = (_cmd("8e", "010100000101"), _cmd("1e", "0100", test=True, reply=True),
                                 _cmd("2c", "0634000000"), OSMO_EV[exposure["compensation_ev"]])
        return (_cmd("0x8e", "01013f000101", test=True), _cmd("e1", "38"),
                ("simulate_device", "-s", "BaseFormat", "6", "69", "0"),
                *exposure_settings, _cmd("42", "3d"),
                ("simulate_device", "-s", "DeviceRecordRecSettingBitRate", "2"))
    preset = next(preset for preset in TIMELAPSE_PRESETS[model]
                  if (preset.interval_s, preset.duration_s, preset.outputs, preset.exposure_mode)
                  == (params["interval_s"], params["duration_s"], params["outputs"], exposure["mode"]))
    if model is CameraModel.ACTION6:
        exposure_settings = (_action_manual(800) if preset.exposure_mode == "manual"
                             else (_cmd("0x1e", "0100", test=True),))
        return (_cmd("e1", "02", test=True), ACTION_RESOLUTIONS["4k30"],
                ACTION_TIMELAPSE_TIMING[preset.interval_s, preset.duration_s],
                *exposure_settings, preset.command)
    exposure_settings = (_osmo_manual(800) if preset.exposure_mode == "manual" else
                         (_cmd("8e", "010100000100"), _cmd("1e", "0000")))
    return (_cmd("0x8e", "01013f000101", test=True), _cmd("e1", "3b"),
            _cmd("18", "6f03000000"), *exposure_settings, preset.command)
