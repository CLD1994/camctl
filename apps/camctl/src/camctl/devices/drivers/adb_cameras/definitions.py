"""尚待设备契约核实的参数定义。

Schema 从命令候选表取得离散选项。返回定义不自动登记到能力目录；
调用返回含义和延时结束方式尚未核实，不提供可执行任务工厂。
视频估算采用固定假定参考值，说明和元数据从同一份定义生成；
厂商核实后调整本模块的参考值，不改变拍摄参数或完成契约。
"""
from decimal import Decimal

from camctl.contracts.values import MAX_OBJECT_ID
from camctl.devices.catalog import ActionCapability
from .commands import (ACTION_APERTURE, ACTION_BITRATE, ACTION_EV, ACTION_FOV,
                       ACTION_ISO, ACTION_RESOLUTIONS, ACTION_STABILIZATION,
                       OSMO_EV, OSMO_ISO, TIMELAPSE_PRESETS, CameraModel)


_ASSUMED_REFERENCE_BITRATE_MBPS = {
    CameraModel.ACTION6: {"camera_record": 130, "camera_timelapse": 80},
    CameraModel.OSMO360II: {"camera_record": 170, "camera_timelapse": 300},
}
_TIMELAPSE_PLAYBACK_FPS = 30


def _video_estimate(model: CameraModel, action_type: str) -> dict:
    bitrate = _ASSUMED_REFERENCE_BITRATE_MBPS[model][action_type]
    duration = ({"method": "direct",
                 "seconds": {"source": "parameter", "path": "/duration_s"}}
                if action_type == "camera_record" else
                {"method": "timelapse_interval",
                 "capture_seconds": {"source": "parameter", "path": "/duration_s"},
                 "interval_seconds": {"source": "parameter", "path": "/interval_s"},
                 "playback_fps": {"source": "constant", "value": _TIMELAPSE_PLAYBACK_FPS}})
    return {"bitrate_mbps": bitrate, "duration": duration}


def _video_estimate_description(estimate: dict) -> str:
    description = (f"视频大小按固定假定参考码率 {estimate['bitrate_mbps']} Mbps 估算；"
                   "该值只覆盖目标视频，不计入额外预览视频和独立照片。"
                   "参考码率不是实测平均值，也不表示码率设置已经生效；实际码率待厂商核实。")
    if estimate["duration"]["method"] == "timelapse_interval":
        fps = estimate["duration"]["playback_fps"]["value"]
        description += (f"预计每次采集形成一帧成片，成片按每秒 {fps} 帧播放；"
                        "该关系只用于估算成片时长，首尾帧和实际完成仍按拍摄契约判定。")
    return description


def output_scope_for(driver_id: str) -> dict:
    """资料给出的完整目录范围，不包含执行时取得的基准。"""
    model = CameraModel(driver_id)
    directories = ["/mnt/media_rw/emulated/DCIM"]
    if model is CameraModel.ACTION6:
        directories.append("/mnt/media_rw/sd/DCIM")
    return {"directories": directories}


def _object(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def _exposure(iso, ev):
    return {"oneOf": [
        _object({"mode": {"const": "manual"}, "iso": {"type": "integer", "enum": list(iso)}}),
        _object({"mode": {"const": "auto"}, "compensation_ev": {"type": "number", "enum": list(ev)}}),
    ]}


def _record_schema(model: CameraModel, parameter_type: str) -> dict:
    properties = {"type": {"const": parameter_type},
                  "duration_s": {"type": "number", "minimum": Decimal("0.001"),
                                 "maximum": Decimal(f"{MAX_OBJECT_ID // 1000}.{MAX_OBJECT_ID % 1000:03d}"),
                                 "multipleOf": Decimal("0.001")}}
    if model is CameraModel.ACTION6:
        properties.update({"resolution": {"enum": list(ACTION_RESOLUTIONS)},
                           "fov": {"enum": list(ACTION_FOV)},
                           "stabilization": {"enum": list(ACTION_STABILIZATION)},
                           "aperture": {"enum": list(ACTION_APERTURE)},
                           "bitrate": {"enum": list(ACTION_BITRATE)},
                           "exposure": _exposure(ACTION_ISO, ACTION_EV)})
    else:
        properties["exposure"] = _exposure(OSMO_ISO, OSMO_EV)
    required = list(properties)
    if model is CameraModel.ACTION6:
        required.remove("aperture")
        required.remove("bitrate")
    return _object(properties, required)


def _timelapse_schema(model: CameraModel, parameter_type: str) -> dict:
    schema = _object({"type": {"const": parameter_type},
                      "interval_s": {"type": "integer"}, "duration_s": {"type": "integer"},
                      "outputs": {"type": "string"}, "exposure": {"type": "object"}})
    schema["oneOf"] = [
        {"properties": {"interval_s": {"const": preset.interval_s},
                        "duration_s": {"const": preset.duration_s},
                        "outputs": {"const": str(preset.outputs)},
                        "exposure": (_object({"mode": {"const": "manual"}, "iso": {"const": 800}})
                                     if preset.exposure_mode == "manual" else _object({"mode": {"const": "auto"}}))}}
        for preset in TIMELAPSE_PRESETS[model]
    ]
    return schema


def candidate_capabilities(driver_id: str) -> tuple[ActionCapability, ...]:
    """生成独立候选定义；驱动登记须另行核实完整可用契约。"""
    model = CameraModel(driver_id)
    prefix = "action6" if model is CameraModel.ACTION6 else "osmo360ii"
    record_type, timelapse_type = f"{prefix}_record", f"{prefix}_timelapse"
    record = _record_schema(model, record_type)
    timelapse = _timelapse_schema(model, timelapse_type)
    for schema in (record, timelapse):
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    record_estimate = _video_estimate(model, "camera_record")
    timelapse_estimate = _video_estimate(model, "camera_timelapse")
    record_description = "手动曝光固定快门 1/60 秒；两种曝光均固定白平衡 5200 K。调用响应待设备核实。"
    if model is CameraModel.ACTION6:
        record_description += "省略 aperture 或 bitrate 时不发送对应设置，保留设备已有值；显式提供时执行对应设置。"
    record_description += _video_estimate_description(record_estimate)
    if model is CameraModel.ACTION6:
        record_description += "该固定估算适用于所列分辨率和码率档位，省略码率时也使用同一参考值。"
    return (
        ActionCapability("camera_record", record_type, "普通录像候选参数",
                         record_description,
                         False, record, {}, video_size_estimate=record_estimate),
        ActionCapability("camera_timelapse", timelapse_type, "原生延时摄影候选参数",
                         "仅接受资料给出的完整组合；正常结束及文件写完依据待设备核实。"
                         + _video_estimate_description(timelapse_estimate),
                         False, timelapse, {}, video_size_estimate=timelapse_estimate),
    )
